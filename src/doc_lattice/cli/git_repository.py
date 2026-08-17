"""Local Git discovery for the ``ci``, GitHub-mode ``init``, and ordinary ``init`` adapters.

Two discovery contracts live here, and they fail in deliberately different ways.
``resolve_git_repository_root`` is a prerequisite: the managed commands cannot act without a
worktree, so every failure becomes a ``ConfigError``. ``probe_default_branch`` is a hint:
ordinary ``init`` has no Git prerequisite at all, so every discovery failure yields ``None``
and the caller falls back. Only a candidate that was actually supplied or discovered and then
fails the branch-name policy raises, through ``validate_default_branch``.

Both contracts run Git through ``_resolve_git_executable`` rather than by name, so the program
this module executes can never come from the directory it was pointed at. SECURITY.md states
that doc-lattice executes no code from the project directory, and running a bare ``git`` would
break that promise: Windows searches the invoking process's current directory ahead of ``PATH``,
so a repository carrying its own ``git.exe`` would run it, and a relative ``PATH`` entry does the
same on POSIX. Ordinary ``init`` is why this matters now. The managed commands are run by a
maintainer inside their own repository, but ``init`` is run in freshly cloned ones.
"""

import re
from pathlib import Path
from shutil import which
from subprocess import CompletedProcess, TimeoutExpired, run

from ..error_types import ConfigError

_GIT_TIMEOUT_SECONDS = 5
_GIT_EXECUTABLE_NAME = "git"
_MISSING_GIT_MESSAGE = (
    "git executable not found on an absolute PATH entry outside the invocation directory; "
    "install Git, or remove relative entries such as '.' and '..' from PATH, before using "
    "managed GitHub CI commands"
)

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
# in a ref name, and accepting it would render a filter no real branch can ever match. The bare
# name "HEAD" is excluded for that same reason: Git reserves it, so no such branch can exist.
# Only that exact spelling. A differential sweep of every name this allowlist accepts against
# "git check-ref-format --branch" found "HEAD" to be the sole divergence, and "head", "Head",
# "HEADx", and "release/HEAD" are all branch names Git will create, so rejecting more than the
# reserved spelling would turn a usable branch into a false error.
_MAX_DEFAULT_BRANCH_CHARACTERS = 255
_RESERVED_BRANCH_NAME = "HEAD"
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
        ConfigError: If Git is unavailable outside the invocation directory, the directory is
            outside a worktree, or Git's top-level result cannot be validated safely.
    """
    git = _resolve_git_executable(cwd)
    if git is None:
        raise ConfigError(_MISSING_GIT_MESSAGE)
    try:
        completed = run(  # noqa: S603 - resolved executable, arguments are module-local literals
            [git, "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        # Resolution succeeded, so this is the narrow race where Git disappeared in between.
        raise ConfigError(_MISSING_GIT_MESSAGE) from exc
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
        or value == _RESERVED_BRANCH_NAME
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
        Git being absent or resolvable only from inside the invocation directory, ``cwd`` being
        outside a worktree, no remote or no ``origin/HEAD``, a timeout, undecodable or unexpected
        output, and a dangling target all return None. The returned name is unvalidated; callers
        pass it through ``validate_default_branch``.
    """
    # Resolved once and threaded through, so both calls of a single probe run the same program.
    git = _resolve_git_executable(cwd)
    if git is None:
        return None
    target = _git_stdout_line(git, cwd, ["symbolic-ref", "--quiet", _ORIGIN_HEAD_REF])
    if target is None or not target.startswith(_ORIGIN_BRANCH_PREFIX):
        return None
    branch = target.removeprefix(_ORIGIN_BRANCH_PREFIX)
    if not branch:
        return None
    if not _git_succeeded(git, cwd, ["show-ref", "--quiet", "--verify", "--", target]):
        return None
    return branch


def _resolve_git_executable(cwd: Path) -> str | None:
    """Resolve ``git`` to an absolute path outside any directory the invocation can control.

    Rejecting rather than falling back to a bare name is the point: an executable found through
    a directory the invocation can control is exactly the planted-binary case, and running it
    would be worse than reporting no Git at all. Two independent conditions reject a candidate.

    A relative result is refused outright. ``shutil.which`` joins the matched name onto the
    ``PATH`` entry it came from, so a relative return value means, exactly, that the entry was
    relative. That covers every reachable plant rather than one directory of them: ``.`` reaches
    the process directory, ``..`` reaches its parent, and the Windows search that prepends the
    process directory to ``PATH`` yields ``.`` as well. Trying instead to enumerate untrusted
    directories cannot work here, since a relative entry can name any ancestor and the managed
    contract has not resolved the project root at this point.

    An absolute result is then refused when it resolves inside ``cwd`` or the process's own
    working directory. This is the residual case where a ``PATH`` entry is absolute but points
    into the tree being operated on, including one holding a symlink into it, which is why the
    resolution is strict. An absolute ``PATH`` entry elsewhere in the project is a directory the
    user has explicitly chosen to trust, and no repository can put itself on ``PATH``.

    Args:
        cwd: Invocation directory the resolved executable will be run in.

    Returns:
        The absolute path to run Git as, or None when no trusted candidate exists.
    """
    found = which(_GIT_EXECUTABLE_NAME)
    if found is None or not Path(found).is_absolute():
        return None
    try:
        executable = Path(found).resolve(strict=True)
        untrusted = [directory.resolve(strict=True) for directory in (cwd, Path.cwd())]
    except (OSError, RuntimeError, ValueError):
        return None
    if any(executable.is_relative_to(directory) for directory in untrusted):
        return None
    return str(executable)


def _run_git(git: str, cwd: Path, arguments: list[str]) -> CompletedProcess[bytes] | None:
    """Run one Git command, returning None instead of raising when it cannot run at all."""
    try:
        return run(  # noqa: S603 - resolved executable, arguments are module-local literals
            [git, *arguments],
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutExpired):
        return None


def _git_stdout_line(git: str, cwd: Path, arguments: list[str]) -> str | None:
    """Return the single non-empty stdout line of a successful Git command, else None."""
    completed = _run_git(git, cwd, arguments)
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


def _git_succeeded(git: str, cwd: Path, arguments: list[str]) -> bool:
    """Report whether a Git command ran and exited zero."""
    completed = _run_git(git, cwd, arguments)
    return completed is not None and completed.returncode == 0


def _default_branch_error(value: str) -> str:
    """Build the configuration error message for an unsupported default branch name."""
    return (
        f"default branch {value!r} must be an ASCII Git branch name built from letters, digits, "
        "'.', '_', and '-', in '/'-separated parts, for example main or release/2.x. Glob and "
        "pattern characters such as '*', '?', '[', ']', and '!' are rejected because a GitHub "
        "branches filter is a glob pattern rather than a literal. Names Git itself refuses are "
        "rejected too, including '..' anywhere, a leading or trailing '.' on any part, a '.lock' "
        "suffix, and the reserved name HEAD, because no branch can carry one. Pass "
        "--default-branch with a supported name to set the generated workflow's trigger."
    )
