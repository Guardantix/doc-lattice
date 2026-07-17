"""Tests for managed-CI Git repository root discovery."""

import subprocess
from pathlib import Path

import pytest

from doc_lattice.cli import git_repository
from doc_lattice.cli.git_repository import resolve_git_repository_root
from doc_lattice.error_types import ConfigError


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
