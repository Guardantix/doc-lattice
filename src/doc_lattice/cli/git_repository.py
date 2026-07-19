"""Local Git repository discovery for managed GitHub CI commands."""

from pathlib import Path
from subprocess import TimeoutExpired, run

from ..error_types import ConfigError

_GIT_TIMEOUT_SECONDS = 5


def resolve_git_repository_root(cwd: Path) -> Path:
    """Resolve and validate the Git top-level containing an invocation directory.

    Args:
        cwd: Existing invocation directory from which Git should resolve the worktree.

    Returns:
        The canonical absolute Git worktree root containing ``cwd``.

    Raises:
        ConfigError: If Git is unavailable, the directory is outside a worktree, or Git's
            top-level result cannot be validated safely.
    """
    try:
        completed = run(
            [  # noqa: S607 - git is intentionally resolved from the maintainer's PATH
                "git",
                "rev-parse",
                "--show-toplevel",
            ],
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            "git executable not found; install Git before using managed GitHub CI commands"
        ) from exc
    except (OSError, TimeoutExpired) as exc:
        raise ConfigError("cannot resolve Git repository root") from exc
    if completed.returncode != 0:
        raise ConfigError("managed GitHub CI commands require a Git working tree")
    try:
        stdout = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("cannot decode Git repository root as UTF-8") from exc
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise ConfigError("cannot resolve Git repository root")
    logical_root = Path(lines[0])
    if not logical_root.is_absolute():
        raise ConfigError("cannot resolve Git repository root")
    try:
        root = logical_root.resolve(strict=True)
        invocation = cwd.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError("cannot resolve Git repository root") from exc
    if not root.is_dir() or not invocation.is_relative_to(root):
        raise ConfigError("cannot resolve Git repository root")
    return root
