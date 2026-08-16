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


def _plant_fake_git(directory: Path) -> tuple[Path, Path]:
    """Write an executable named ``git`` into a directory, plus the marker it leaves when run."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "planted-git-ran"
    planted = directory / "git"
    planted.write_text(f'#!/bin/sh\ntouch "{marker}"\necho refs/remotes/origin/pwned\n')
    planted.chmod(0o755)
    return planted, marker


def _seeded_repository(path: Path) -> Path:
    """Initialize a repository carrying one empty commit.

    The initial branch is pinned to a name no test uses, so the local branch cannot collide with
    one a test creates and the repository does not depend on Git's default-branch configuration.
    """
    _git(path, "init", "--quiet", "--initial-branch=seed-base")
    # -c rather than the ambient config: the seed commit must not depend on a global identity
    # that a CI runner does not have.
    _git(
        path,
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
    return path


def _repository_with_origin_head(tmp_path: Path, branch: str, *, dangling: bool = False) -> Path:
    """Build a repository whose origin/HEAD names ``branch``, optionally without the target."""
    _seeded_repository(tmp_path)
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


def test_resolve_git_repository_root_reports_git_missing_from_every_trusted_path(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(git_repository, "which", lambda _name: None)

    with pytest.raises(ConfigError, match="git executable not found"):
        resolve_git_repository_root(tmp_path)


def test_resolve_git_repository_root_refuses_a_git_planted_in_the_worktree(
    tmp_path: Path,
    monkeypatch,
):
    planted, marker = _plant_fake_git(tmp_path)
    monkeypatch.setattr(git_repository, "which", lambda _name: str(planted))

    with pytest.raises(ConfigError, match="git executable not found"):
        resolve_git_repository_root(tmp_path)

    assert not marker.exists()


def test_resolve_git_repository_root_refuses_a_git_reached_through_a_relative_path_entry(
    tmp_path: Path,
    monkeypatch,
):
    checkout = tmp_path / "checkout"
    nested = checkout / "nested"
    nested.mkdir(parents=True)
    _, marker = _plant_fake_git(checkout)
    monkeypatch.chdir(nested)
    monkeypatch.setenv("PATH", "..")

    with pytest.raises(ConfigError, match="git executable not found"):
        resolve_git_repository_root(nested)

    assert not marker.exists()


def test_resolve_git_repository_root_runs_git_from_an_absolute_path(tmp_path: Path, monkeypatch):
    recorded: list[list[str]] = []

    def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0, f"{tmp_path}\n".encode(), b"")

    monkeypatch.setattr(git_repository, "run", record)

    assert resolve_git_repository_root(tmp_path) == tmp_path.resolve()
    assert Path(recorded[0][0]).is_absolute()


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


def test_probe_default_branch_returns_none_when_git_is_not_on_a_trusted_path(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(git_repository, "which", lambda _name: None)

    assert probe_default_branch(tmp_path) is None


def test_probe_default_branch_refuses_a_git_planted_in_the_checkout(tmp_path: Path, monkeypatch):
    # The planted-binary case SECURITY.md's scope promises cannot happen. Ordinary init runs in
    # freshly cloned repositories, so a repository carrying its own git must never be the program
    # this module executes, and no candidate is a better answer than a poisoned one.
    repository = _repository_with_origin_head(tmp_path, "trunk")
    planted, marker = _plant_fake_git(repository)
    monkeypatch.setattr(git_repository, "which", lambda _name: str(planted))

    assert probe_default_branch(repository) is None
    assert not marker.exists()


def test_probe_default_branch_refuses_a_git_planted_in_the_process_directory(
    tmp_path: Path,
    monkeypatch,
):
    # Distinct from the case above: the probe targets one directory while the process stands in
    # another. That second one is what shutil.which prepends to its own search on Windows, and
    # what CreateProcess searches ahead of PATH.
    process_directory = tmp_path / "process"
    planted, marker = _plant_fake_git(process_directory)
    target = tmp_path / "target"
    target.mkdir()
    _repository_with_origin_head(target, "trunk")
    monkeypatch.chdir(process_directory)
    monkeypatch.setattr(git_repository, "which", lambda _name: str(planted))

    assert probe_default_branch(target) is None
    assert not marker.exists()


def test_probe_default_branch_refuses_a_git_reached_through_a_relative_path_entry(
    tmp_path: Path,
    monkeypatch,
):
    # A relative PATH entry such as ".." makes which() return a relative result, which resolves
    # against the process directory and reaches a plant in a parent of the invocation directory
    # that no containment check on that directory can see. Uses the real PATH and the real
    # shutil.which rather than a stub, since the relative return value is the behavior under test.
    checkout = tmp_path / "checkout"
    nested = checkout / "nested"
    nested.mkdir(parents=True)
    _, marker = _plant_fake_git(checkout)
    monkeypatch.chdir(nested)
    monkeypatch.setenv("PATH", "..")

    assert probe_default_branch(nested) is None
    assert not marker.exists()


def test_probe_default_branch_returns_none_when_the_resolved_git_vanishes(
    tmp_path: Path,
    monkeypatch,
):
    # which() reported a path and nothing is there by the time it is resolved. The candidate sits
    # outside the invocation directory, so the resolution failure is the only thing that can be
    # returning no candidate here.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(git_repository, "which", lambda _name: str(tmp_path / "elsewhere/git"))

    assert probe_default_branch(work) is None


def test_probe_default_branch_runs_git_from_an_absolute_path(tmp_path: Path, monkeypatch):
    recorded: list[list[str]] = []

    def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0, b"refs/remotes/origin/trunk\n", b"")

    monkeypatch.setattr(git_repository, "run", record)

    assert probe_default_branch(tmp_path) == "trunk"
    assert recorded
    assert all(Path(command[0]).is_absolute() for command in recorded)


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
    [
        "main",
        "master",
        "trunk",
        "develop",
        "release/2.x",
        "a",
        "v1.0.0",
        "a_b-c.d",
        "x" * 255,
        # Only the exact reserved spelling is refused. Git creates every one of these, so
        # rejecting them would turn a usable branch into a false error.
        "head",
        "Head",
        "HEADx",
        "xHEAD",
        "release/HEAD",
        "HEAD/x",
    ],
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
        "a..b",
        "release/2..x",
        "main.lock",
        "a/b.lock",
        "HEAD",
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


@pytest.mark.parametrize("branch", ["main", "release/2.x", "head", "release/HEAD", "a_b-c.d"])
def test_accepted_names_are_branch_names_git_will_create(tmp_path: Path, branch: str):
    # Pins the policy to Git's own rules rather than to a reading of them, in both directions.
    # Over-rejection turns a usable branch into a false error just as surely as under-rejection
    # renders a filter that can never match.
    _seeded_repository(tmp_path)
    _git(tmp_path, "branch", branch)

    assert validate_default_branch(branch) == branch


@pytest.mark.parametrize("branch", ["HEAD", "a..b", "main.lock", "ma~in"])
def test_structurally_rejected_names_are_ones_git_refuses(tmp_path: Path, branch: str):
    _seeded_repository(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        _git(tmp_path, "branch", branch)

    with pytest.raises(ConfigError, match="must be an ASCII Git branch name"):
        validate_default_branch(branch)


def test_validate_default_branch_error_names_the_glob_hazard_and_the_override():
    with pytest.raises(ConfigError) as excinfo:
        validate_default_branch("release/*")

    message = str(excinfo.value)
    assert "glob" in message.lower()
    assert "--default-branch" in message
