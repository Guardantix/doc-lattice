"""Tests for Git repository root discovery and best-effort default-branch probing."""

import subprocess
from pathlib import Path

import pytest

from doc_lattice.cli import git_repository
from doc_lattice.cli.git_repository import (
    probe_default_branch,
    resolve_git_repository_root,
    validate_default_branch,
)
from doc_lattice.error_types import ConfigError


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603 - arguments are test-local literals
        ["git", *arguments],  # noqa: S607 - tests require the local git executable
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _repository_with_origin_head(tmp_path: Path, branch: str, *, dangling: bool = False) -> Path:
    """Build a repository whose origin/HEAD names ``branch``, optionally without the target."""
    _git(tmp_path, "init", "--quiet")
    # -c rather than the ambient config: the seed commit must not depend on a global identity
    # that a CI runner does not have.
    _git(
        tmp_path,
        "-c",
        "user.name=doc-lattice tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "seed",
    )
    if not dangling:
        _git(tmp_path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    _git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    return tmp_path


def test_resolve_git_repository_root_returns_top_level_from_nested_directory(
    tmp_path: Path,
):
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - test requires the local git executable
        cwd=tmp_path,
        check=True,
    )
    nested = tmp_path / "nested/deeper"
    nested.mkdir(parents=True)

    assert resolve_git_repository_root(nested) == tmp_path.resolve()


def test_resolve_git_repository_root_rejects_non_working_tree(tmp_path: Path):
    with pytest.raises(ConfigError, match="require a Git working tree"):
        resolve_git_repository_root(tmp_path)


def test_resolve_git_repository_root_reports_missing_git(tmp_path: Path, monkeypatch):
    def missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(git_repository, "run", missing)

    with pytest.raises(ConfigError, match="git executable not found"):
        resolve_git_repository_root(tmp_path)


@pytest.mark.parametrize(
    "completed",
    [
        subprocess.CompletedProcess([], 1, b"", b"ignored"),
        subprocess.CompletedProcess([], 0, b"", b""),
        subprocess.CompletedProcess([], 0, b"relative/path\n", b""),
    ],
)
def test_resolve_git_repository_root_rejects_unreliable_results(
    tmp_path: Path,
    monkeypatch,
    completed: subprocess.CompletedProcess[bytes],
):
    monkeypatch.setattr(git_repository, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(ConfigError):
        resolve_git_repository_root(tmp_path)


@pytest.mark.parametrize("branch", ["trunk", "develop", "release/2.x", "v1.0", "a"])
def test_probe_default_branch_reads_a_live_origin_head(tmp_path: Path, branch: str):
    repository = _repository_with_origin_head(tmp_path, branch)

    assert probe_default_branch(repository) == branch


def test_probe_default_branch_reads_origin_head_from_a_nested_directory(tmp_path: Path):
    repository = _repository_with_origin_head(tmp_path, "trunk")
    nested = repository / "nested/deeper"
    nested.mkdir(parents=True)

    assert probe_default_branch(nested) == "trunk"


def test_probe_default_branch_returns_none_without_a_remote(tmp_path: Path):
    # Plain init has no Git prerequisite and a fresh clone often has no origin/HEAD at all, so
    # this degrades rather than raising the way resolve_git_repository_root does.
    _git(tmp_path, "init", "--quiet")

    assert probe_default_branch(tmp_path) is None


def test_probe_default_branch_returns_none_outside_a_worktree(tmp_path: Path):
    assert probe_default_branch(tmp_path) is None


def test_probe_default_branch_returns_none_when_git_is_missing(tmp_path: Path, monkeypatch):
    def missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(git_repository, "run", missing)

    assert probe_default_branch(tmp_path) is None


def test_probe_default_branch_returns_none_on_timeout(tmp_path: Path, monkeypatch):
    def slow(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(git_repository, "run", slow)

    assert probe_default_branch(tmp_path) is None


def test_probe_default_branch_rejects_a_dangling_stale_target(tmp_path: Path):
    # symbolic-ref only prints its target; it does not establish that the target exists. After
    # an upstream rename the cached ref can name a branch that is gone, and rendering that would
    # install a workflow that never triggers. A stale target that still exists locally is not
    # detectable without network access, which is why the caller narrates the source.
    repository = _repository_with_origin_head(tmp_path, "gone", dangling=True)

    assert probe_default_branch(repository) is None


@pytest.mark.parametrize(
    "completed",
    [
        subprocess.CompletedProcess([], 1, b"", b"ignored"),
        subprocess.CompletedProcess([], 0, b"", b""),
        subprocess.CompletedProcess([], 0, b"\xff\xfe\n", b""),
        subprocess.CompletedProcess([], 0, b"refs/heads/main\n", b""),
        subprocess.CompletedProcess([], 0, b"refs/remotes/origin/\n", b""),
        subprocess.CompletedProcess([], 0, b"refs/remotes/origin/a\nrefs/remotes/origin/b\n", b""),
    ],
)
def test_probe_default_branch_returns_none_on_unusable_output(
    tmp_path: Path,
    monkeypatch,
    completed: subprocess.CompletedProcess[bytes],
):
    monkeypatch.setattr(git_repository, "run", lambda *_args, **_kwargs: completed)

    assert probe_default_branch(tmp_path) is None


@pytest.mark.parametrize(
    "branch",
    ["main", "master", "trunk", "develop", "release/2.x", "a", "v1.0.0", "a_b-c.d", "x" * 255],
)
def test_validate_default_branch_accepts_supported_names(branch: str):
    assert validate_default_branch(branch) == branch


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "x" * 256,
        # Glob and pattern forms: a GitHub branches filter is a glob, not a literal.
        "release/*",
        "main?",
        "rel[ease]",
        "!main",
        "**",
        # Shell and YAML injection shapes.
        "main; rm -rf /",
        "main branch",
        "$(whoami)",
        "'main'",
        '"main"',
        "main\nother",
        "main\x00",
        # Non-ASCII.
        "réf",
        "main‐x",  # noqa: RUF001 - a lookalike hyphen, deliberately not the ASCII one
        # Git's own structural exclusions.
        ".hidden",
        "..",
        "a//b",
        "/main",
        "main/",
        "main.",
        "a/.b",
        "main.lock",
        "a/b.lock",
        "main@{1}",
        "@",
        "-main",
        "ma~in",
        "ma^in",
        "ma:in",
        "ma\\in",
    ],
)
def test_validate_default_branch_rejects_unsupported_names(branch: str):
    with pytest.raises(ConfigError, match="must be an ASCII Git branch name"):
        validate_default_branch(branch)


def test_validate_default_branch_error_names_the_glob_hazard_and_the_override():
    with pytest.raises(ConfigError) as excinfo:
        validate_default_branch("release/*")

    message = str(excinfo.value)
    assert "glob" in message.lower()
    assert "--default-branch" in message
