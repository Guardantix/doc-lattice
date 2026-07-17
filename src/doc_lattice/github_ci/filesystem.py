"""Local filesystem operations for fixed managed GitHub CI artifacts."""

import difflib
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from doc_lattice.error_types import ConfigError, copy_exception_notes
from doc_lattice.path_utils import safe_resolve
from doc_lattice.persistence import atomic_create_bytes_at, atomic_replace_bytes_at

from .identity import parse_repository, validate_final_release_version
from .model import (
    BOOTSTRAP_SHEBANG,
    MANAGED_SCHEMA_LINE,
    MARKER_PREFIXES,
    VALID_ARTIFACT_ROLES,
    ArtifactChange,
    ArtifactRole,
    InstalledArtifact,
    ManagedArtifact,
    ManagedArtifactTarget,
    ManagedMarker,
    WorkflowDiscovery,
    WorkflowDocument,
)
from .path_display import display_path
from .render import CANONICAL_ARTIFACT_TARGETS
from .workflow_parser import parse_workflow

_CANONICAL_PATHS: dict[ArtifactRole, PurePosixPath] = {
    target.role: target.relative_path for target in CANONICAL_ARTIFACT_TARGETS
}
_WORKFLOWS_DIRECTORY = PurePosixPath(".github/workflows")
_WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
MAX_WORKFLOW_FILES = 256
MAX_WORKFLOW_BYTES = 1_048_576
MAX_CUMULATIVE_WORKFLOW_BYTES = 8_388_608
_NONSTANDARD_LINE_SEPARATORS = ("\v", "\f", "\x85", "\u2028", "\u2029")
_UNIFIED_DIFF_HEADER_RECORDS = 2
_PARTIAL_STATE_NOTE = (
    "managed artifacts are applied in input order without rollback; earlier changes, "
    "if any, remain in place, so inspect the reported path and rerun to converge"
)
_LOCKING_SUPPORTED = os.name != "nt"
_NO_DESCRIPTOR = -1


@dataclass(frozen=True, slots=True)
class _WorkflowCandidate:
    relative_path: Path
    logical_path: Path
    resolved_path: Path
    size: int


@dataclass(frozen=True, slots=True)
class _ManagedArtifactLock:
    """A root-bound capability for managed-artifact publication."""

    root: Path
    directory_identity: tuple[int, int]
    directory_fd: int

    def protects_directory(self, directory_stat: os.stat_result) -> bool:
        """Return whether a directory stat result identifies the locked root."""
        return self.directory_identity == (directory_stat.st_dev, directory_stat.st_ino)


def _real_directory_exists(
    logical_path: Path,
    *,
    inspect_context: str,
    symlink_message: str,
    directory_message: str,
) -> bool:
    """Stat one path and require any present target to be a real directory.

    Args:
        logical_path: Repository-relative path to stat without following symlinks.
        inspect_context: Phrase passed to ``_filesystem_error`` on an inspection failure.
        symlink_message: Exact error when the present target is a symlink.
        directory_message: Exact error when the present target is not a directory.

    Returns:
        ``True`` when a real directory is present, ``False`` when the path is absent so each
        caller can classify absence in its own way.

    Raises:
        ConfigError: If the path cannot be inspected, is a symlink, or is not a directory.
    """
    try:
        directory_stat = logical_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _filesystem_error(inspect_context, exc) from exc
    if stat.S_ISLNK(directory_stat.st_mode):
        raise ConfigError(symlink_message)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise ConfigError(directory_message)
    return True


def discover_workflows(root: Path) -> WorkflowDiscovery:
    """Discover and parse direct repository GitHub Actions workflow files.

    Args:
        root: Repository root containing the optional ``.github/workflows`` directory.

    Returns:
        Whether the workflow directory exists and its parsed YAML documents in stable
        repository-relative path order.

    Raises:
        ConfigError: If the directory or a selected workflow is unsafe, unreadable,
            non-UTF-8, or invalid YAML.
    """
    logical_directory = root / _WORKFLOWS_DIRECTORY
    display_directory = _WORKFLOWS_DIRECTORY.as_posix()
    if not _workflow_parent_exists(root, display_directory):
        _resolve_repository_path(
            logical_directory,
            root,
            display_directory,
            "GitHub workflow directory",
        )
        return WorkflowDiscovery(directory_exists=False, documents=())
    if not _real_directory_exists(
        logical_directory,
        inspect_context=f"cannot inspect GitHub workflow directory {display_directory}",
        symlink_message=(
            f"symlink is not allowed for GitHub workflow directory {display_directory}"
        ),
        directory_message=(
            f"GitHub workflow directory must be a real directory: {display_directory}"
        ),
    ):
        _resolve_repository_path(
            logical_directory,
            root,
            display_directory,
            "GitHub workflow directory",
        )
        _workflow_parent_exists(root, display_directory)
        return WorkflowDiscovery(directory_exists=False, documents=())
    directory = _resolve_repository_path(
        logical_directory,
        root,
        display_directory,
        "GitHub workflow directory",
    )
    try:
        names = _bounded_workflow_names(directory, display_directory)
    except OSError as exc:
        raise _filesystem_error(
            f"cannot list GitHub workflow directory {display_directory}",
            exc,
        ) from exc
    candidates = tuple(
        candidate
        for name in names
        if (candidate := _inspect_workflow_candidate(root, name)) is not None
    )
    declared_total = sum(candidate.size for candidate in candidates)
    if declared_total > MAX_CUMULATIVE_WORKFLOW_BYTES:
        raise ConfigError(
            f"GitHub workflows exceed the cumulative byte limit in {display_directory}"
        )

    documents: list[WorkflowDocument] = []
    actual_total = 0
    for candidate in candidates:
        data = _read_workflow_candidate(root, candidate)
        actual_total += len(data)
        if actual_total > MAX_CUMULATIVE_WORKFLOW_BYTES:
            raise ConfigError(
                f"GitHub workflows exceed the cumulative byte limit in {display_directory}"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            path = display_path(candidate.relative_path)
            raise ConfigError(f"UTF-8 text is required for GitHub workflow {path}") from exc
        documents.append(parse_workflow(candidate.relative_path, text))
    return WorkflowDiscovery(directory_exists=True, documents=tuple(documents))


def _bounded_workflow_names(directory: Path, display_directory: str) -> tuple[str, ...]:
    names: list[str] = []
    for entry in directory.iterdir():
        if entry.suffix not in _WORKFLOW_SUFFIXES:
            continue
        names.append(entry.name)
        if len(names) > MAX_WORKFLOW_FILES:
            raise ConfigError(
                f"more than {MAX_WORKFLOW_FILES} direct workflow files in {display_directory}"
            )
    return tuple(sorted(names))


def inspect_installed_artifacts(
    root: Path,
    expected: tuple[ManagedArtifactTarget, ...],
) -> tuple[InstalledArtifact | None, ...]:
    """Read fixed managed artifact paths and inspect strict ownership markers.

    Args:
        root: Repository root containing the fixed artifact destinations.
        expected: Expected canonical artifacts in caller presentation order.

    Returns:
        One installed artifact or ``None`` per expected path. Invalid ownership headers
        are represented by ``marker_error`` so policy audit can report drift.

    Raises:
        ConfigError: If a requested path is noncanonical, unsafe, unreadable, or non-UTF-8.
    """
    installed: list[InstalledArtifact | None] = []
    for artifact in expected:
        _validate_artifact_path(artifact)
        logical_destination = root / artifact.relative_path
        destination = _resolve_destination(logical_destination, root, artifact, "inspect")
        _require_real_artifact_ancestors(root, artifact.relative_path)
        data = _read_bounded_artifact_bytes(
            logical_destination,
            destination,
            root,
            artifact,
            phase="inspection",
        )
        if data is None:
            installed.append(None)
            continue
        path = artifact.relative_path.as_posix()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"UTF-8 text is required for managed artifact {path}") from exc
        try:
            marker = _parse_managed_marker(text, artifact)
        except ConfigError as exc:
            installed.append(
                InstalledArtifact(
                    expected=artifact,
                    text=text,
                    marker=None,
                    marker_error=str(exc),
                )
            )
        else:
            installed.append(
                InstalledArtifact(
                    expected=artifact,
                    text=text,
                    marker=marker,
                    marker_error=None,
                )
            )
    return tuple(installed)


def preflight_create(
    root: Path,
    artifacts: tuple[ManagedArtifact, ...],
) -> tuple[ArtifactChange, ...]:
    """Inspect managed artifact destinations for a create operation.

    Args:
        root: Repository root containing the fixed artifact destinations.
        artifacts: Rendered artifacts in the order they should be inspected and applied.

    Returns:
        One exact planned change per artifact in input order.

    Raises:
        ConfigError: If a destination escapes the root, has an unsafe type, cannot be read,
            or contains bytes different from the requested artifact.
    """
    return _preflight(root, artifacts, refresh=False)


def preflight_refresh(
    root: Path,
    artifacts: tuple[ManagedArtifact, ...],
) -> tuple[ArtifactChange, ...]:
    """Inspect managed artifact destinations for a refresh operation.

    Args:
        root: Repository root containing the fixed artifact destinations.
        artifacts: Rendered artifacts in the order they should be inspected and applied.

    Returns:
        One exact planned change per artifact in input order.

    Raises:
        ConfigError: If a destination is unsafe or differing bytes do not contain a valid
            compatible ownership marker.
    """
    return _preflight(root, artifacts, refresh=True)


def render_diff(changes: tuple[ArtifactChange, ...]) -> str:
    """Render stable unified diffs for create and replace changes.

    Args:
        changes: Preflight changes in input presentation order.

    Returns:
        Unified diff text with exactly one trailing newline, or an empty string when every
        artifact is current.

    Raises:
        ConfigError: If replacement before-bytes are absent or are not UTF-8.
    """
    rendered_parts: list[str] = []
    for change in changes:
        if change.action == "current":
            continue
        relative_path = change.artifact.relative_path.as_posix()
        if change.action == "create":
            before_text = ""
            old_label = "/dev/null"
        else:
            if change.before is None:
                raise ConfigError(
                    f"replace diff is missing prior bytes for managed artifact {relative_path}"
                )
            try:
                before_text = change.before.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConfigError(
                    f"prior bytes are not UTF-8 for managed artifact {relative_path}"
                ) from exc
            old_label = f"a/{relative_path}"
        diff = difflib.unified_diff(
            _split_lf_records(before_text),
            _split_lf_records(change.artifact.text),
            fromfile=old_label,
            tofile=f"b/{relative_path}",
            lineterm="",
        )
        rendered_parts.extend(
            _render_diff_record(index, record) for index, record in enumerate(diff)
        )
    return "".join(rendered_parts)


def apply_changes(changes: tuple[ArtifactChange, ...]) -> None:
    """Apply preflighted managed artifact changes in input order.

    Args:
        changes: Exact preflight results to apply with contained durable writes.

    Raises:
        ConfigError: If containment, path type, or prior bytes changed after preflight, or
            if a durable create or replacement fails, or advisory locking is unavailable.
            Earlier successful writes are not rolled back and a later rerun can converge them.
    """
    root = _mutable_changes_root(changes)
    if root is None:
        return
    with _managed_artifact_lock(root) as lock:
        for change in changes:
            if change.action == "current":
                continue
            try:
                _require_managed_artifact_lock(lock, change.root)
                if change.action == "create":
                    _apply_create(change, lock)
                else:
                    _apply_replace(change, lock)
            except ConfigError as error:
                if _PARTIAL_STATE_NOTE not in getattr(error, "__notes__", ()):
                    error.add_note(_PARTIAL_STATE_NOTE)
                raise


def _mutable_changes_root(changes: tuple[ArtifactChange, ...]) -> Path | None:
    """Return the one root for mutable changes, rejecting cross-root publication batches."""
    mutable_changes = tuple(change for change in changes if change.action != "current")
    if not mutable_changes:
        return None
    root = mutable_changes[0].root
    if any(change.root != root for change in mutable_changes[1:]):
        raise ConfigError("managed artifact changes span multiple repository roots")
    return root


@contextmanager
def _managed_artifact_lock(root: Path) -> Iterator[_ManagedArtifactLock]:
    """Hold the root directory's nonblocking advisory lock through publication."""
    if not _LOCKING_SUPPORTED:
        raise _unsupported_lock_error()
    fd = _open_lock_directory(root)
    acquired = False
    try:
        _claim_lock(fd)
        acquired = True
        directory_stat = _inspect_lock_directory(fd)
        lock = _new_managed_artifact_lock(root, directory_stat, fd)
        _require_managed_artifact_lock(lock, root)
        yield lock
    finally:
        active_error = sys.exception()
        cleanup_errors: list[tuple[str, BaseException]] = []
        try:
            if acquired:
                _flock(fd, release=True)
        except (ConfigError, OSError) as cause:
            cleanup_errors.append(("lock release", cause))
        try:
            os.close(fd)
        except OSError as cause:
            cleanup_errors.append(("lock close", cause))
        if cleanup_errors:
            details = "; ".join(
                f"{phase} failed: {_lock_error_detail(cause)}" for phase, cause in cleanup_errors
            )
            if active_error is not None:
                active_error.add_note(f"managed artifact {details}")
            else:
                error = ConfigError(f"managed artifact {details}")
                copy_exception_notes(error, cleanup_errors[0][1])
                raise error from cleanup_errors[0][1]


def _open_lock_directory(root: Path) -> int:
    """Open one repository root directory for an advisory publication lock."""
    try:
        return os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as cause:
        raise _lock_setup_error("opening repository root", cause) from cause


def _inspect_lock_directory(fd: int) -> os.stat_result:
    """Inspect the directory inode protected by an acquired advisory lock."""
    try:
        return os.fstat(fd)
    except OSError as cause:
        raise _lock_setup_error("inspecting locked repository root", cause) from cause


def _new_managed_artifact_lock(
    root: Path,
    directory_stat: os.stat_result,
    directory_fd: int,
) -> _ManagedArtifactLock:
    """Create a capability bound to the resolved root and locked directory inode."""
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError) as cause:
        raise _lock_setup_error("resolving locked repository root", cause) from cause
    return _ManagedArtifactLock(
        root=resolved_root,
        directory_identity=(directory_stat.st_dev, directory_stat.st_ino),
        directory_fd=directory_fd,
    )


def _require_managed_artifact_lock(lock: _ManagedArtifactLock, root: Path) -> None:
    """Ensure one mutation root is still the directory protected by ``lock``."""
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError) as cause:
        raise _lock_validation_error("resolving repository root", cause) from cause
    if lock.root != resolved_root:
        raise ConfigError("managed artifact lock protects a different repository root")
    try:
        directory_stat = resolved_root.stat()
    except OSError as cause:
        raise _lock_validation_error("inspecting repository root", cause) from cause
    if not lock.protects_directory(directory_stat):
        raise ConfigError("managed artifact lock protects a different repository root directory")


def _claim_lock(fd: int) -> None:
    """Acquire one nonblocking advisory lock or describe why publication cannot begin."""
    try:
        _flock(fd, release=False)
    except BlockingIOError:
        raise ConfigError("managed artifact refresh is in progress; retry after it exits") from None
    except OSError as cause:
        raise _lock_setup_error("acquiring managed artifact lock", cause) from cause


def _flock(fd: int, *, release: bool) -> None:
    """Apply the POSIX advisory lock operation without importing fcntl at module load."""
    try:
        import fcntl  # noqa: PLC0415 - non-mutating commands must work without POSIX locking
    except ImportError as cause:
        raise _unsupported_lock_error() from cause
    operation = fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(fd, operation)


def _unsupported_lock_error() -> ConfigError:
    """Return the fail-closed error for unsupported advisory locking platforms."""
    return ConfigError(
        f"managed artifact locking is not supported on this platform ({sys.platform})"
    )


def _lock_setup_error(operation: str, cause: OSError | RuntimeError) -> ConfigError:
    """Wrap one directory-lock setup failure without leaking a local root path."""
    error = ConfigError(
        f"managed artifact lock setup failed while {operation}: {_stable_error_detail(cause)}"
    )
    copy_exception_notes(error, cause)
    return error


def _lock_validation_error(operation: str, cause: OSError | RuntimeError) -> ConfigError:
    """Wrap a root-identity validation failure without exposing a local path."""
    error = ConfigError(
        f"managed artifact lock validation failed while {operation}: {_stable_error_detail(cause)}"
    )
    copy_exception_notes(error, cause)
    return error


def _lock_error_detail(cause: BaseException) -> str:
    """Render cleanup failures without exposing an incidental absolute filename."""
    if isinstance(cause, (OSError, RuntimeError)):
        return _stable_error_detail(cause)
    return str(cause)


def _inspect_workflow_candidate(root: Path, name: str) -> _WorkflowCandidate | None:
    """Validate and size one direct workflow before any YAML parsing.

    Returns ``None`` for a real subdirectory entry, which GitHub Actions deterministically
    ignores, so a benign directory named like a workflow does not make the repository
    unauditable. Symlinks and every other non-regular type stay fail-closed errors.
    """
    relative_path = Path(_WORKFLOWS_DIRECTORY / name)
    display = display_path(relative_path)
    logical_path = root / relative_path
    try:
        target_stat = logical_path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ConfigError(f"GitHub workflow changed during discovery: {display}") from exc
    except OSError as exc:
        raise _filesystem_error(f"cannot inspect GitHub workflow {display}", exc) from exc
    if stat.S_ISLNK(target_stat.st_mode):
        raise ConfigError(f"symlink is not allowed for GitHub workflow {display}")
    if stat.S_ISDIR(target_stat.st_mode):
        return None
    if not stat.S_ISREG(target_stat.st_mode):
        raise ConfigError(f"GitHub workflow must be a regular file: {display}")
    if target_stat.st_size > MAX_WORKFLOW_BYTES:
        raise ConfigError(f"GitHub workflow exceeds the byte limit: {display}")
    resolved_path = _resolve_repository_path(
        logical_path,
        root,
        display,
        "GitHub workflow",
    )
    return _WorkflowCandidate(
        relative_path=relative_path,
        logical_path=logical_path,
        resolved_path=resolved_path,
        size=target_stat.st_size,
    )


@dataclass(frozen=True, slots=True)
class _BoundedReadWording:
    """Exact error strings for one bounded read so both call sites keep their wording."""

    error_path: str | None
    read_context: str
    recheck_context: str
    byte_limit: str
    changed: str
    size_changed: str
    symlink: str
    regular: str


def _read_bounded_with_recheck(
    open_path: Path,
    logical_path: Path,
    expected_size: int,
    reresolve: Callable[[], Path],
    wording: _BoundedReadWording,
) -> bytes:
    """Read one bounded file and reject containment, type, or size changes after the read.

    Workflow discovery and managed artifact reads share this open, read, re-resolve, re-stat,
    and size-recheck sequence; only their error wording and re-resolution differ.

    Args:
        open_path: Already resolved path to open, also the expected re-resolution target.
        logical_path: Unresolved path to re-stat after the read.
        expected_size: Size observed before the read that the recheck must still match.
        reresolve: Re-authenticates containment and returns the freshly resolved path.
        wording: Exact error strings for this call site.

    Returns:
        The file bytes bounded to the per-file workflow limit.

    Raises:
        ConfigError: If the read fails, exceeds the byte bound, or containment, type, or size
            changed during the read.
    """
    try:
        with open_path.open("rb") as handle:
            data = handle.read(MAX_WORKFLOW_BYTES + 1)
    except OSError as exc:
        raise _filesystem_error(wording.read_context, exc, path=wording.error_path) from exc
    if len(data) > MAX_WORKFLOW_BYTES:
        raise ConfigError(wording.byte_limit)
    if reresolve() != open_path:
        raise ConfigError(wording.changed)
    try:
        target_stat = logical_path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ConfigError(wording.changed) from exc
    except OSError as exc:
        raise _filesystem_error(wording.recheck_context, exc, path=wording.error_path) from exc
    if stat.S_ISLNK(target_stat.st_mode):
        raise ConfigError(wording.symlink)
    if not stat.S_ISREG(target_stat.st_mode):
        raise ConfigError(wording.regular)
    if target_stat.st_size != expected_size or len(data) != expected_size:
        raise ConfigError(wording.size_changed)
    return data


def _read_workflow_candidate(root: Path, candidate: _WorkflowCandidate) -> bytes:
    """Bound one workflow read and reject containment, type, or size changes."""
    display = display_path(candidate.relative_path)
    changed = f"GitHub workflow changed during discovery: {display}"
    wording = _BoundedReadWording(
        error_path=None,
        read_context=f"cannot read GitHub workflow {display}",
        recheck_context=f"cannot recheck GitHub workflow {display}",
        byte_limit=f"GitHub workflow exceeds the byte limit: {display}",
        changed=changed,
        size_changed=changed,
        symlink=f"symlink is not allowed for GitHub workflow {display}",
        regular=f"GitHub workflow must be a regular file: {display}",
    )
    return _read_bounded_with_recheck(
        candidate.resolved_path,
        candidate.logical_path,
        candidate.size,
        lambda: _resolve_repository_path(
            candidate.logical_path,
            root,
            display,
            "GitHub workflow",
        ),
        wording,
    )


def _workflow_parent_exists(root: Path, display_path: str) -> bool:
    """Validate the real ``.github`` parent before classifying workflows as absent."""
    logical_parent = root / _WORKFLOWS_DIRECTORY.parent
    return _real_directory_exists(
        logical_parent,
        inspect_context=f"cannot inspect GitHub workflow parent for {display_path}",
        symlink_message=f"symlink is not allowed in GitHub workflow path {display_path}",
        directory_message=f"GitHub workflow parent must be a real directory: {display_path}",
    )


def _require_real_artifact_ancestors(root: Path, relative_path: PurePosixPath) -> None:
    """Reject any symlinked or non-directory existing ancestor of a managed artifact.

    The write path resolves fixed destinations with ``safe_resolve``, which accepts an
    in-repo symlinked ancestor such as ``.github/workflows``. The audit read path rejects
    those symlinks, so writing through one installs an artifact that can never be audited.
    Failing closed at every managed read or write keeps both paths consistent. Missing
    ancestors are fine because the create path materializes them as real directories.

    Args:
        root: Repository root containing the fixed artifact destinations.
        relative_path: Canonical repository-relative path of the managed artifact.

    Raises:
        ConfigError: If an existing ancestor is a symlink or is not a real directory.
    """
    for ancestor in reversed(relative_path.parents):
        if ancestor == PurePosixPath("."):
            continue
        display = ancestor.as_posix()
        logical_ancestor = root / ancestor
        if not _real_directory_exists(
            logical_ancestor,
            inspect_context=f"cannot inspect managed artifact ancestor {display}",
            symlink_message=f"symlink is not allowed in managed artifact path {display}",
            directory_message=f"managed artifact ancestor must be a real directory: {display}",
        ):
            return


def _resolve_within_root(
    logical_path: Path,
    root: Path,
    *,
    outside_message: str,
    resolve_context: str,
    path: str | None = None,
) -> Path:
    """Resolve one path within the root, mapping every failure to a stable ConfigError.

    Args:
        logical_path: Unresolved repository-relative path to authenticate.
        root: Repository root that must contain the resolved path.
        outside_message: Exact message when the path escapes the repository root.
        resolve_context: Context passed to ``_filesystem_error`` for I/O failures.
        path: Optional canonical path appended to the I/O failure detail.

    Returns:
        The resolved, root-contained path.

    Raises:
        ConfigError: If the path escapes the root or cannot be resolved.
    """
    try:
        return safe_resolve(logical_path, root)
    except ValueError as exc:
        raise ConfigError(outside_message) from exc
    except (OSError, RuntimeError) as exc:
        raise _filesystem_error(resolve_context, exc, path=path) from exc


def _resolve_repository_path(
    logical_path: Path,
    root: Path,
    display_path: str,
    kind: str,
) -> Path:
    """Resolve one read-only repository path without leaking an absolute display path."""
    return _resolve_within_root(
        logical_path,
        root,
        outside_message=f"{kind} resolves outside the repository root: {display_path}",
        resolve_context=f"cannot resolve {kind} {display_path}",
    )


def _filesystem_error(
    context: str,
    error: OSError | RuntimeError,
    *,
    path: str | None = None,
) -> ConfigError:
    """Wrap one filesystem failure with stable path context and preserved notes.

    Args:
        context: Human phrase describing the failed operation.
        error: The underlying I/O or runtime failure.
        path: Optional canonical path appended as ``; path {path}`` to the detail.

    Returns:
        A ``ConfigError`` carrying the stable detail and any preserved notes.
    """
    detail = _stable_error_detail(error)
    message = f"{context}: {detail}"
    if path is not None:
        message = f"{message}; path {path}"
    wrapped = ConfigError(message)
    copy_exception_notes(wrapped, error)
    return wrapped


def _stable_error_detail(error: OSError | RuntimeError) -> str:
    """Describe an I/O failure without including an absolute filename."""
    if isinstance(error, OSError):
        if error.strerror:
            return error.strerror
        if error.filename is None and str(error):
            return str(error)
    return type(error).__name__


def _preflight(
    root: Path,
    artifacts: tuple[ManagedArtifact, ...],
    *,
    refresh: bool,
) -> tuple[ArtifactChange, ...]:
    """Build exact changes without creating directories or writing files."""
    changes: list[ArtifactChange] = []
    for artifact in artifacts:
        _validate_artifact_path(artifact)
        logical_destination = root / artifact.relative_path
        destination = _resolve_destination(logical_destination, root, artifact, "inspect")
        _require_real_artifact_ancestors(root, artifact.relative_path)
        before = _read_bounded_artifact_bytes(
            logical_destination,
            destination,
            root,
            artifact,
            phase="preflight",
        )
        desired = artifact.text.encode("utf-8")
        if before is None:
            action = "create"
        elif before == desired:
            action = "current"
        elif not refresh:
            path = artifact.relative_path.as_posix()
            raise ConfigError(
                "managed artifact already has different content and will not be "
                f"overwritten: {path}"
            )
        else:
            path = artifact.relative_path.as_posix()
            desired_marker = _parse_managed_marker(artifact.text, artifact)
            prior_marker = _parse_managed_marker(_decode_marker_text(before, path), artifact)
            desired_version = validate_final_release_version(desired_marker.version)
            prior_version = validate_final_release_version(prior_marker.version)
            if prior_version > desired_version:
                raise _marker_error(
                    path,
                    f"version {prior_marker.version!r} is newer than requested "
                    f"{desired_marker.version!r}",
                )
            action = "replace"
        changes.append(
            ArtifactChange(
                artifact=artifact,
                root=root,
                destination=destination,
                action=action,
                before=before,
            )
        )
    return tuple(changes)


def _validate_artifact_path(
    artifact: ManagedArtifactTarget,
) -> None:
    """Require each artifact role to use its single fixed canonical path."""
    expected = _CANONICAL_PATHS.get(artifact.role)
    if expected is None:
        raise ConfigError(f"managed artifact has unsupported role {artifact.role!r}")
    if artifact.relative_path != expected:
        raise ConfigError(
            f"managed artifact role {artifact.role!r} must use canonical path "
            f"{expected.as_posix()}, not {artifact.relative_path.as_posix()}"
        )


def _resolve_destination(
    logical_destination: Path,
    root: Path,
    artifact: ManagedArtifactTarget,
    operation: str,
) -> Path:
    """Resolve one fixed destination with typed path context."""
    path = artifact.relative_path.as_posix()
    return _resolve_within_root(
        logical_destination,
        root,
        outside_message=f"path resolves outside the repository root for managed artifact {path}",
        resolve_context=f"cannot {operation} managed artifact",
        path=path,
    )


def _read_bounded_artifact_bytes(
    logical_destination: Path,
    destination: Path,
    root: Path,
    artifact: ManagedArtifactTarget,
    *,
    phase: str,
) -> bytes | None:
    """Read one managed artifact under the per-file byte bound with change detection.

    Args:
        logical_destination: Unresolved repository-relative destination path.
        destination: Resolved, root-contained destination path.
        root: Repository root used to re-authenticate containment after the read.
        artifact: Fixed managed artifact identity being read.
        phase: Human word ("preflight" or "inspection") used in change diagnostics.

    Returns:
        The existing bytes bounded to the per-file workflow limit, or ``None`` when the
        destination is absent.

    Raises:
        ConfigError: If the target is a symlink or non-regular file, exceeds the byte bound,
            cannot be read, or its containment, type, or size changed during the read.
    """
    path = artifact.relative_path.as_posix()
    try:
        target_stat = logical_destination.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _filesystem_error("cannot inspect managed artifact", exc, path=path) from exc
    _require_regular_not_symlink(target_stat.st_mode, path)
    if target_stat.st_size > MAX_WORKFLOW_BYTES:
        raise ConfigError(f"managed artifact exceeds the byte limit: {path}")
    wording = _BoundedReadWording(
        error_path=path,
        read_context="cannot read managed artifact",
        recheck_context="cannot recheck managed artifact",
        byte_limit=f"managed artifact exceeds the byte limit: {path}",
        changed=f"managed artifact path changed during {phase}: {path}",
        size_changed=f"managed artifact changed during {phase}: {path}",
        symlink=f"symlink target is not allowed for managed artifact {path}",
        regular=f"existing target must be a regular file for managed artifact {path}",
    )
    return _read_bounded_with_recheck(
        destination,
        logical_destination,
        target_stat.st_size,
        lambda: _resolve_destination(logical_destination, root, artifact, "recheck"),
        wording,
    )


def _require_regular_not_symlink(mode: int, path: str) -> None:
    """Reject final-component symlinks and every non-regular target type."""
    if stat.S_ISLNK(mode):
        raise ConfigError(f"symlink target is not allowed for managed artifact {path}")
    if not stat.S_ISREG(mode):
        raise ConfigError(f"existing target must be a regular file for managed artifact {path}")


def _decode_marker_text(data: bytes, path: str) -> str:
    """Decode existing managed artifact bytes at the ownership-marker boundary."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"UTF-8 ownership marker is required for managed artifact {path}"
        ) from exc


def _parse_managed_marker(
    text: str,
    artifact: ManagedArtifactTarget,
) -> ManagedMarker:
    """Parse the exact ownership header from already-decoded managed artifact text."""
    path = artifact.relative_path.as_posix()
    if any(separator in text for separator in _NONSTANDARD_LINE_SEPARATORS):
        raise _marker_error(path, "ownership lines must use LF or CRLF separators")
    if "\r" in text.replace("\r\n", ""):
        raise _marker_error(path, "bare or embedded carriage return is not allowed")
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    offset = 1 if lines and lines[0] == BOOTSTRAP_SHEBANG else 0
    if len(lines) < offset + 4:
        raise _marker_error(path, "the four required ownership lines are missing")

    header = lines[offset : offset + 4]
    if header[0] != MANAGED_SCHEMA_LINE:
        raise _marker_error(path, "managed schema must be exactly github-ci-v1")

    role_prefix = f"{MARKER_PREFIXES[1]} "
    if not header[1].startswith(role_prefix):
        raise _marker_error(path, "artifact role line is missing or malformed")
    role = _parse_artifact_role(header[1][len(role_prefix) :], path)
    if role != artifact.role:
        raise _marker_error(
            path,
            f"artifact role {role!r} does not match canonical role {artifact.role!r}",
        )

    version_prefix = f"{MARKER_PREFIXES[2]} "
    if not header[2].startswith(version_prefix):
        raise _marker_error(path, "version line is missing or malformed")
    version = header[2][len(version_prefix) :]
    try:
        validate_final_release_version(version)
    except ConfigError as exc:
        raise _marker_error(path, str(exc)) from exc

    repository_prefix = f"{MARKER_PREFIXES[3]} "
    if not header[3].startswith(repository_prefix):
        raise _marker_error(path, "repository line is missing or malformed")
    repository_text = header[3][len(repository_prefix) :]
    try:
        repository = parse_repository(repository_text)
    except ConfigError as exc:
        raise _marker_error(path, str(exc)) from exc

    if any(line.startswith(prefix) for line in lines[offset + 4 :] for prefix in MARKER_PREFIXES):
        raise _marker_error(path, "duplicate ownership line appears after the header")
    return ManagedMarker(role=role, version=version, repository=repository)


def _parse_artifact_role(value: str, path: str) -> ArtifactRole:
    """Validate one ownership marker role against the shared role domain and narrow it."""
    if value not in VALID_ARTIFACT_ROLES:
        raise _marker_error(path, f"artifact role {value!r} is not recognized")
    # The value is a member of the shared role domain; narrow it to the Literal so the
    # typed model records an exact ArtifactRole.
    if value == "offline":
        return "offline"
    if value == "linear":
        return "linear"
    return "bootstrap"


def _marker_error(path: str, detail: str) -> ConfigError:
    """Build one path-specific ownership marker error."""
    return ConfigError(f"invalid ownership marker for managed artifact {path}: {detail}")


def _split_lf_records(text: str) -> list[str]:
    """Split text only at LF while retaining every content byte represented by the string."""
    parts = text.split("\n")
    records = [f"{part}\n" for part in parts[:-1]]
    if parts[-1]:
        records.append(parts[-1])
    return records


def _render_diff_record(index: int, record: str) -> str:
    """Render one difflib record without hiding content line-ending differences."""
    is_control = index < _UNIFIED_DIFF_HEADER_RECORDS or record.startswith("@@")
    if is_control:
        return f"{record}\n"
    if record.endswith("\n"):
        return record
    return f"{record}\n\\ No newline at end of file\n"


def _descriptor_open_flags(*, directory: bool) -> int:
    """Return no-follow descriptor-open flags or fail closed when unsupported."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ConfigError("managed artifact descriptor publication requires no-follow path support")
    flags = os.O_RDONLY | nofollow
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _ensure_locked_artifact_ancestor(
    parent_fd: int,
    component: str,
    display: str,
    artifact_path: str,
    *,
    create: bool,
) -> None:
    """Require one descriptor-relative artifact ancestor to be a real directory."""
    try:
        ancestor_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise ConfigError(
                f"destination changed after preflight for managed artifact {artifact_path}"
            ) from None
        try:
            os.mkdir(component, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _filesystem_error(
                "cannot create managed artifact parent",
                exc,
                path=artifact_path,
            ) from exc
        try:
            ancestor_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ConfigError(
                f"destination changed after preflight for managed artifact {artifact_path}"
            ) from exc
        except OSError as exc:
            raise _filesystem_error(
                f"cannot inspect managed artifact ancestor {display}",
                exc,
            ) from exc
    except OSError as exc:
        raise _filesystem_error(
            f"cannot inspect managed artifact ancestor {display}",
            exc,
        ) from exc
    if stat.S_ISLNK(ancestor_stat.st_mode):
        raise ConfigError(f"symlink is not allowed in managed artifact path {display}")
    if not stat.S_ISDIR(ancestor_stat.st_mode):
        raise ConfigError(f"managed artifact ancestor must be a real directory: {display}")


def _close_unowned_artifact_descriptor(
    fd: int,
    primary: BaseException,
    *,
    phase: str,
) -> None:
    """Close one descriptor without replacing an already-selected primary error."""
    try:
        os.close(fd)
    except OSError as cleanup_error:
        primary.add_note(
            f"managed artifact {phase} close failed: {_stable_error_detail(cleanup_error)}"
        )


def _transfer_locked_artifact_parent(
    parent_fd: int,
    child_fd: int,
    artifact_path: str,
) -> int:
    """Transfer parent ownership to a child descriptor after closing the old parent."""
    try:
        os.close(parent_fd)
    except OSError as cause:
        error = _filesystem_error(
            "cannot close managed artifact parent",
            cause,
            path=artifact_path,
        )
        _close_unowned_artifact_descriptor(child_fd, error, phase="child")
        raise error from cause
    return child_fd


def _open_locked_artifact_parent(
    lock: _ManagedArtifactLock,
    artifact: ManagedArtifact,
    *,
    create: bool,
) -> int:
    """Open the artifact parent beneath the locked root without using its pathname."""
    artifact_path = artifact.relative_path.as_posix()
    try:
        parent_fd = os.dup(lock.directory_fd)
    except OSError as exc:
        raise _filesystem_error(
            "cannot access locked managed artifact root",
            exc,
            path=artifact_path,
        ) from exc
    try:
        display_parts: list[str] = []
        for component in artifact.relative_path.parts[:-1]:
            display_parts.append(component)
            display = "/".join(display_parts)
            _ensure_locked_artifact_ancestor(
                parent_fd,
                component,
                display,
                artifact_path,
                create=create,
            )
            try:
                child_fd = os.open(
                    component,
                    _descriptor_open_flags(directory=True),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise _filesystem_error(
                    f"cannot open managed artifact ancestor {display}",
                    exc,
                ) from exc
            try:
                child_stat = os.fstat(child_fd)
            except OSError as exc:
                error = _filesystem_error(
                    f"cannot inspect managed artifact ancestor {display}",
                    exc,
                )
                _close_unowned_artifact_descriptor(child_fd, error, phase="child")
                raise error from exc
            if not stat.S_ISDIR(child_stat.st_mode):
                error = ConfigError(
                    f"managed artifact ancestor must be a real directory: {display}"
                )
                _close_unowned_artifact_descriptor(child_fd, error, phase="child")
                raise error
            old_parent_fd = parent_fd
            parent_fd = _NO_DESCRIPTOR
            parent_fd = _transfer_locked_artifact_parent(
                old_parent_fd,
                child_fd,
                artifact_path,
            )
    except BaseException as error:
        if parent_fd != _NO_DESCRIPTOR:
            _close_unowned_artifact_descriptor(parent_fd, error, phase="parent")
        raise
    return parent_fd


@contextmanager
def _locked_artifact_parent(
    lock: _ManagedArtifactLock,
    artifact: ManagedArtifact,
    *,
    create: bool,
) -> Iterator[int]:
    """Yield one descriptor-relative artifact parent and close it with safe diagnostics."""
    artifact_path = artifact.relative_path.as_posix()
    parent_fd = _open_locked_artifact_parent(lock, artifact, create=create)
    try:
        yield parent_fd
    finally:
        active_error = sys.exception()
        try:
            os.close(parent_fd)
        except OSError as cause:
            if active_error is not None:
                active_error.add_note(
                    f"managed artifact parent close failed: {_stable_error_detail(cause)}"
                )
            else:
                raise _filesystem_error(
                    "cannot close managed artifact parent",
                    cause,
                    path=artifact_path,
                ) from cause


def _apply_create(change: ArtifactChange, lock: _ManagedArtifactLock) -> None:
    """Create one absent artifact beneath the locked root without replacing a winner."""
    artifact = change.artifact
    _validate_artifact_path(artifact)
    path = artifact.relative_path.as_posix()
    _resolve_change(change)
    with _locked_artifact_parent(lock, artifact, create=True) as parent_fd:
        try:
            target_stat = os.stat(
                artifact.relative_path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _filesystem_error("cannot inspect create destination", exc, path=path) from exc
        else:
            if stat.S_ISLNK(target_stat.st_mode):
                raise ConfigError(f"symlink appeared after preflight for managed artifact {path}")
            raise ConfigError(f"destination appeared after preflight for managed artifact {path}")
        try:
            atomic_create_bytes_at(
                parent_fd,
                artifact.relative_path.name,
                artifact.text.encode("utf-8"),
                prefix=f".{artifact.relative_path.name}.doc-lattice-create.",
            )
        except FileExistsError as exc:
            error = ConfigError(f"destination appeared after preflight for managed artifact {path}")
            copy_exception_notes(error, exc)
            raise error from exc
        except OSError as exc:
            raise _filesystem_error("cannot write managed artifact", exc, path=path) from exc


def _apply_replace(change: ArtifactChange, lock: _ManagedArtifactLock) -> None:
    """Replace one artifact beneath the locked root while its preflight bytes remain current."""
    artifact = change.artifact
    _validate_artifact_path(artifact)
    path = artifact.relative_path.as_posix()
    if change.before is None:
        raise ConfigError(f"replace change is missing prior bytes for managed artifact {path}")
    _resolve_change(change)
    with _locked_artifact_parent(lock, artifact, create=False) as parent_fd:
        current = _read_apply_bytes_at(parent_fd, artifact)
        if current != change.before:
            raise ConfigError(f"destination changed after preflight for managed artifact {path}")
        try:
            atomic_replace_bytes_at(
                parent_fd,
                artifact.relative_path.name,
                artifact.text.encode("utf-8"),
                prefix=f".{artifact.relative_path.name}.doc-lattice-replace.",
            )
        except OSError as exc:
            raise _filesystem_error("cannot write managed artifact", exc, path=path) from exc


def _resolve_change(change: ArtifactChange) -> tuple[Path, Path]:
    """Re-resolve and authenticate one preflight destination before mutation."""
    artifact = change.artifact
    logical_destination = change.root / artifact.relative_path
    destination = _resolve_destination(
        logical_destination,
        change.root,
        artifact,
        "resolve",
    )
    if destination != change.destination:
        path = artifact.relative_path.as_posix()
        raise ConfigError(f"managed artifact path changed after preflight: {path}")
    return logical_destination, destination


def _read_apply_bytes_at(parent_fd: int, artifact: ManagedArtifact) -> bytes:
    """Read exact replacement bytes through an already-open artifact parent descriptor."""
    path = artifact.relative_path.as_posix()
    try:
        target_stat = os.stat(
            artifact.relative_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            f"destination changed after preflight for managed artifact {path}"
        ) from exc
    except OSError as exc:
        raise _filesystem_error("cannot inspect replacement destination", exc, path=path) from exc
    _require_regular_not_symlink(target_stat.st_mode, path)
    if target_stat.st_size > MAX_WORKFLOW_BYTES:
        raise ConfigError(f"managed artifact exceeds the byte limit: {path}")
    try:
        target_fd = os.open(
            artifact.relative_path.name,
            _descriptor_open_flags(directory=False),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _filesystem_error("cannot read replacement destination", exc, path=path) from exc
    try:
        opened_stat = os.fstat(target_fd)
        _require_regular_not_symlink(opened_stat.st_mode, path)
        if (target_stat.st_dev, target_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise ConfigError(f"destination changed after preflight for managed artifact {path}")
        if opened_stat.st_size > MAX_WORKFLOW_BYTES:
            raise ConfigError(f"managed artifact exceeds the byte limit: {path}")
        with os.fdopen(target_fd, "rb") as handle:
            target_fd = -1
            data = handle.read(MAX_WORKFLOW_BYTES + 1)
    except OSError as exc:
        raise _filesystem_error("cannot read replacement destination", exc, path=path) from exc
    finally:
        if target_fd != -1:
            os.close(target_fd)
    if len(data) > MAX_WORKFLOW_BYTES:
        raise ConfigError(f"managed artifact exceeds the byte limit: {path}")
    return data
