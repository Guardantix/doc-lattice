"""Local Git discovery for the ``ci``, GitHub-mode ``init``, and ordinary ``init`` adapters.

Two discovery contracts live here, and they fail in deliberately different ways.
``resolve_git_repository_root`` is a prerequisite: the managed commands cannot act without a
worktree, so every failure becomes a ``ConfigError``. ``probe_default_branch`` is a hint:
ordinary ``init`` has no Git prerequisite at all, so every discovery failure yields ``None``
and the caller falls back. Only a candidate that was actually supplied or discovered and then
fails the branch-name policy raises, through ``validate_default_branch``.
"""

import re
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired, run

from ..error_types import ConfigError

_GIT_TIMEOUT_SECONDS = 5

# The ordinary workflow's branch filter when nothing better is known. Deliberately not shared
# with the ``main`` literals in ``github_ci/render.py``: those are a security control that pins
# the managed environment to one exact branch, and a shared constant would let a change here
# relax that control silently.
DEFAULT_BRANCH_FALLBACK = "main"

_ORIGIN_HEAD_REF = "refs/remotes/origin/HEAD"
_ORIGIN_BRANCH_PREFIX = "refs/remotes/origin/"

# ASCII allowlist for common literal branch names, in the style of github_ci/identity.py. Each
# slash-separated component starts with a letter, digit, or underscore and cannot end in a dot,
# which covers Git's own structural exclusions (no leading dot, no empty or "." component, no
# trailing dot) while excluding every glob and pattern metacharacter by construction. That
# second property is the load-bearing one: a GitHub ``branches:`` filter is a glob pattern
# rather than a literal, so "*", "?", "[", "]", and "!" must never reach it. Consecutive dots
# survive the component pattern, so ".." is excluded separately below; Git rejects it anywhere
# in a ref name, and accepting it would render a filter no real branch can ever match.
_MAX_DEFAULT_BRANCH_CHARACTERS = 255
_BRANCH_COMPONENT = r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?"
_DEFAULT_BRANCH_RE = re.compile(
    rf"{_BRANCH_COMPONENT}(?:/{_BRANCH_COMPONENT})*",
    flags=re.ASCII,
)


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


def validate_default_branch(value: str) -> str:
    """Validate one default branch name against the generated workflow's accepted domain.

    Args:
        value: Branch name supplied by ``--default-branch`` or returned by the local probe.

    Returns:
        The same value, unchanged, once it is known to be safe to render.

    Raises:
        ConfigError: If the name is outside the supported ASCII domain, or carries a glob or
            pattern character that a GitHub branch filter would interpret rather than match.
    """
    if (
        not value
        or len(value) > _MAX_DEFAULT_BRANCH_CHARACTERS
        or _DEFAULT_BRANCH_RE.fullmatch(value) is None
        or ".." in value
        or any(component.endswith(".lock") for component in value.split("/"))
    ):
        raise ConfigError(_default_branch_error(value))
    return value


def probe_default_branch(cwd: Path) -> str | None:
    """Read the local ``origin/HEAD`` remote-tracking default branch, best effort.

    This is a local hint, never an authority. ``refs/remotes/origin/HEAD`` is cached state: it
    is frequently absent in fresh or shallow clones, and after an upstream rename it can name a
    branch that no longer exists. Reading the symbolic ref only prints its target and does not
    establish that the target exists, so the target is verified separately and a dangling one
    yields no candidate. A stale target that still exists locally cannot be distinguished from
    a current one without network access; that residual is why the caller narrates the value and
    its source, and why ``--default-branch`` overrides this entirely.

    Args:
        cwd: Invocation directory Git should read the ref from.

    Returns:
        The branch name ``origin/HEAD`` resolves to, or None when no candidate is available.
        Git being absent, ``cwd`` being outside a worktree, no remote or no ``origin/HEAD``, a
        timeout, undecodable or unexpected output, and a dangling target all return None. The
        returned name is unvalidated; callers pass it through ``validate_default_branch``.
    """
    target = _git_stdout_line(cwd, ["symbolic-ref", "--quiet", _ORIGIN_HEAD_REF])
    if target is None or not target.startswith(_ORIGIN_BRANCH_PREFIX):
        return None
    branch = target.removeprefix(_ORIGIN_BRANCH_PREFIX)
    if not branch:
        return None
    if not _git_succeeded(cwd, ["show-ref", "--quiet", "--verify", "--", target]):
        return None
    return branch


def _run_git(cwd: Path, arguments: list[str]) -> CompletedProcess[bytes] | None:
    """Run one Git command, returning None instead of raising when it cannot run at all."""
    try:
        return run(  # noqa: S603 - arguments are module-local literals, never user input
            ["git", *arguments],  # noqa: S607 - git is intentionally resolved from PATH
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutExpired):
        return None


def _git_stdout_line(cwd: Path, arguments: list[str]) -> str | None:
    """Return the single non-empty stdout line of a successful Git command, else None."""
    completed = _run_git(cwd, arguments)
    if completed is None or completed.returncode != 0:
        return None
    try:
        stdout = completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0]:
        return None
    return lines[0]


def _git_succeeded(cwd: Path, arguments: list[str]) -> bool:
    """Report whether a Git command ran and exited zero."""
    completed = _run_git(cwd, arguments)
    return completed is not None and completed.returncode == 0


def _default_branch_error(value: str) -> str:
    """Build the configuration error message for an unsupported default branch name."""
    return (
        f"default branch {value!r} must be an ASCII Git branch name built from letters, digits, "
        "'.', '_', and '-', in '/'-separated parts, for example main or release/2.x. Glob and "
        "pattern characters such as '*', '?', '[', ']', and '!' are rejected because a GitHub "
        "branches filter is a glob pattern rather than a literal. Pass --default-branch with a "
        "supported name to set the generated workflow's trigger."
    )
