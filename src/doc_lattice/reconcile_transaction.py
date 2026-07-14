"""Serialize reconcile processes and recover durable transaction journals."""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic import ValidationError as PydanticValidationError

from .constants import (
    PERSISTENCE_TEMP_SUFFIX,
    RECONCILE_AFTER_IMAGE_INFIX,
    RECONCILE_BEFORE_IMAGE_INFIX,
    RECONCILE_JOURNAL_NAME,
    RECONCILE_JOURNAL_VERSION,
)
from .error_types import (
    ReconcileConflictError,
    ReconcileInProgressError,
    ReconcilePersistenceError,
    copy_exception_notes,
)
from .path_utils import safe_resolve
from .persistence import (
    atomic_create_bytes,
    atomic_replace_bytes,
    durable_unlink,
    file_sha256,
    replace_staged,
    sha256_bytes,
    stage_bytes,
    sync_directory,
)
from .reconcile import Rewrite

JournalState = Literal["prepared", "committed"]
RecoveryAction = Literal["none", "rolled_back", "cleaned_committed"]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_LOCK_FACTORY_TOKEN = object()
_LOCKING_SUPPORTED = os.name != "nt"


class JournalEntry(BaseModel):
    """One destination and its staged before and after images."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    destination: str
    before_path: str
    before_sha256: Sha256Digest
    after_path: str
    after_sha256: Sha256Digest


class Journal(BaseModel):
    """A versioned reconcile recovery journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(strict=True)
    state: JournalState
    entries: tuple[JournalEntry, ...]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """The action taken for a project reconcile journal."""

    action: RecoveryAction
    journal: Path


class ReconcileLock:
    """An active, root-bound capability for reconcile transaction mutation."""

    __slots__ = (
        "_active",
        "_directory_identity",
        "_in_use",
        "_project_root",
        "_state_lock",
    )

    def __init__(
        self,
        project_root: Path,
        directory_stat: os.stat_result,
        factory_token: object,
    ) -> None:
        """Create a capability only for the reconcile-lock context manager."""
        if factory_token is not _LOCK_FACTORY_TOKEN:
            message = "reconcile lock capabilities must be acquired through reconcile_lock"
            raise ReconcileInProgressError(message)
        self._project_root = project_root.resolve()
        self._directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
        self._state_lock = Lock()
        self._active = True
        self._in_use = False

    @property
    def project_root(self) -> Path:
        """Return the canonical project root protected by this capability."""
        return self._project_root

    @property
    def is_active(self) -> bool:
        """Return whether the context still holds the underlying advisory lock."""
        return self._active

    def _deactivate(self) -> None:
        """Invalidate the capability before releasing its advisory lock."""
        with self._state_lock:
            self._active = False

    def _protects_directory(self, directory_stat: os.stat_result) -> bool:
        """Return whether a stat result identifies the locked directory inode."""
        return self._directory_identity == (directory_stat.st_dev, directory_stat.st_ino)

    def _claim_operation(self) -> None:
        """Reserve this capability for one commit or recovery operation."""
        with self._state_lock:
            if not self._active:
                message = "reconcile lock capability is no longer active"
                raise ReconcileInProgressError(message)
            if self._in_use:
                message = "reconcile lock capability is already in use"
                raise ReconcileInProgressError(message)
            self._in_use = True

    def _release_operation(self) -> None:
        """Release this capability after its current operation returns."""
        with self._state_lock:
            self._in_use = False


@dataclass(frozen=True, slots=True)
class _ResolvedEntry:
    """Contained filesystem paths and fingerprints for one journal entry."""

    destination: Path
    before_path: Path
    before_sha256: str
    after_path: Path
    after_sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedTransaction:
    """A published prepared journal and its validated filesystem entries."""

    journal: Journal
    entries: tuple[_ResolvedEntry, ...]
    journal_path: Path
    journal_bytes: bytes


@dataclass(frozen=True, slots=True)
class _PendingRewrite:
    """A rewrite whose contained destination passed preflight validation."""

    rewrite: Rewrite
    destination: Path
    destination_relative: str


def _invalid_journal_error(journal: Path, cause: object) -> ReconcilePersistenceError:
    """Build the deliberate manual-remediation diagnostic for an invalid journal."""
    message = (
        f"invalid reconcile journal {journal}: {cause}; inspect {journal}, its destinations, "
        "and staged files; move the invalid journal aside only after manual restoration or "
        "preservation; rerun 'doc-lattice reconcile --recover'"
    )
    return ReconcilePersistenceError(message)


def _journal_is_present(journal: Path) -> bool:
    """Inspect the canonical journal entry without following symlinks.

    Only a missing namespace entry is absence. Every present journal must be a regular,
    non-symlink file before any caller reads or treats it as recovery authority.
    """
    try:
        mode = journal.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as cause:
        problem = f"cannot inspect canonical journal path: {cause}"
        raise _invalid_journal_error(journal, problem) from cause
    if stat.S_ISLNK(mode):
        problem = "canonical journal path is a symlink; refusing to follow it"
        raise _invalid_journal_error(journal, problem)
    if not stat.S_ISREG(mode):
        problem = "canonical journal path is not a regular file"
        raise _invalid_journal_error(journal, problem)
    return True


def _resolve_journal_path(project_root: Path, field: str, raw_path: str) -> Path:
    """Resolve one relative journal path while enforcing project containment."""
    path = Path(raw_path)
    if path.is_absolute():
        message = f"{field} must be relative, got {raw_path}"
        raise ValueError(message)
    try:
        return safe_resolve(project_root / path, project_root)
    except (OSError, RuntimeError, ValueError) as cause:
        message = f"unsafe {field} {raw_path}: {cause}"
        raise ValueError(message) from cause


def _validate_artifact_path(
    project_root: Path,
    destination: Path,
    artifact: Path,
    role: str,
    raw_path: str,
) -> None:
    """Validate the location, name, and existing type of one staged artifact."""
    field = f"{role}_path"
    candidate = project_root / Path(raw_path)
    if artifact.parent != destination.parent:
        message = (
            f"{field} {raw_path} ({artifact}) must be in destination directory {destination.parent}"
        )
        raise ValueError(message)
    infix = RECONCILE_BEFORE_IMAGE_INFIX if role == "before" else RECONCILE_AFTER_IMAGE_INFIX
    prefix = f".{destination.name}{infix}"
    suffix = PERSISTENCE_TEMP_SUFFIX
    name = Path(raw_path).name
    component = name[len(prefix) : -len(suffix)]
    if not name.startswith(prefix) or not name.endswith(suffix) or not component:
        message = f"{field} {raw_path} ({artifact}) must match {prefix}<nonempty>{suffix} exactly"
        raise ValueError(message)
    if candidate.is_symlink():
        message = f"{field} {raw_path} ({artifact}) is a symlink, not a recovery artifact"
        raise ValueError(message)
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as cause:
        message = f"cannot inspect {field} {raw_path} ({artifact}): {cause}"
        raise ValueError(message) from cause
    if not stat.S_ISREG(mode):
        message = f"{field} {raw_path} ({artifact}) is a nonregular recovery artifact"
        raise ValueError(message)


def _validate_path_roles(entries: tuple[_ResolvedEntry, ...], journal_path: Path) -> None:
    """Reject aliases between journal destinations and transaction artifacts."""
    canonical_journal = journal_path.resolve()
    destinations: dict[Path, int] = {}
    for index, entry in enumerate(entries):
        if entry.destination == canonical_journal:
            message = f"entry {index} destination aliases journal path {journal_path}"
            raise ValueError(message)
        if entry.destination in destinations:
            first = destinations[entry.destination]
            message = f"destination alias across entries {first} and {index}: {entry.destination}"
            raise ValueError(message)
        destinations[entry.destination] = index

    artifacts: dict[Path, tuple[int, str]] = {}
    for index, entry in enumerate(entries):
        for role, artifact in (
            ("before_path", entry.before_path),
            ("after_path", entry.after_path),
        ):
            if artifact == canonical_journal:
                message = f"entry {index} {role} aliases journal path {journal_path}"
                raise ValueError(message)
            if artifact in destinations:
                message = f"entry {index} {role} artifact {artifact} aliases destination path"
                raise ValueError(message)
            if artifact in artifacts:
                first_index, first_role = artifacts[artifact]
                message = (
                    f"artifact alias between entry {first_index} {first_role} and "
                    f"entry {index} {role}: {artifact}"
                )
                raise ValueError(message)
            artifacts[artifact] = (index, role)


def _load_journal(
    project_root: Path,
    journal_path: Path,
) -> tuple[Journal, tuple[_ResolvedEntry, ...], bytes]:
    """Read, validate, and contain every path in a reconcile journal."""
    if not _journal_is_present(journal_path):
        cause = FileNotFoundError(f"canonical journal {journal_path} is absent")
        raise _invalid_journal_error(journal_path, cause) from cause
    try:
        encoded = journal_path.read_bytes()
        decoded = encoded.decode("utf-8")
        journal = Journal.model_validate_json(decoded)
    except (OSError, UnicodeDecodeError, PydanticValidationError) as cause:
        raise _invalid_journal_error(journal_path, cause) from cause
    if journal.version != RECONCILE_JOURNAL_VERSION:
        cause = ValueError(f"unsupported version {journal.version}")
        raise _invalid_journal_error(journal_path, cause) from cause
    try:
        entries = tuple(
            _ResolvedEntry(
                destination=_resolve_journal_path(project_root, "destination", entry.destination),
                before_path=_resolve_journal_path(project_root, "before_path", entry.before_path),
                before_sha256=entry.before_sha256,
                after_path=_resolve_journal_path(project_root, "after_path", entry.after_path),
                after_sha256=entry.after_sha256,
            )
            for entry in journal.entries
        )
    except ValueError as cause:
        raise _invalid_journal_error(journal_path, cause) from cause
    try:
        _validate_path_roles(entries, journal_path)
        for raw_entry, entry in zip(journal.entries, entries, strict=True):
            _validate_artifact_path(
                project_root,
                entry.destination,
                entry.before_path,
                "before",
                raw_entry.before_path,
            )
            _validate_artifact_path(
                project_root,
                entry.destination,
                entry.after_path,
                "after",
                raw_entry.after_path,
            )
    except ValueError as cause:
        raise _invalid_journal_error(journal_path, cause) from cause
    return journal, entries, encoded


def _recovery_operation_error(
    operation: str,
    path: Path,
    journal: Path,
    journal_bytes: bytes,
    cause: object,
) -> ReconcilePersistenceError:
    """Build a retryable recovery operation diagnostic."""
    journal_status = _journal_retry_status(journal, journal_bytes)
    cause_details = _cause_details(cause)
    message = (
        f"reconcile recovery failed while {operation} {path}: {cause_details}; "
        f"{journal_status}; correct the filesystem problem and rerun "
        "'doc-lattice reconcile --recover'"
    )
    return ReconcilePersistenceError(message)


def _cause_details(cause: object) -> str:
    """Render an operation cause plus diagnostic notes into top-level text."""
    details = [str(cause)]
    details.extend(str(note) for note in getattr(cause, "__notes__", ()))
    return "; ".join(details)


def _exact_journal_status(journal: Path, journal_bytes: bytes) -> tuple[str, str]:
    """Classify whether the canonical journal is a regular exact-byte copy."""
    try:
        mode = journal.lstat().st_mode
    except FileNotFoundError:
        return "absent", f"journal {journal} is not present"
    except OSError as cause:
        return "invalid", f"cannot inspect journal {journal}: {cause}"
    if not stat.S_ISREG(mode):
        return "invalid", f"journal collision at {journal} is not a regular file"
    try:
        current_bytes = journal.read_bytes()
    except OSError as cause:
        return "invalid", f"cannot read journal {journal}: {cause}"
    if current_bytes != journal_bytes:
        return "invalid", f"journal collision at {journal} contains different bytes"
    return "exact", f"journal {journal} is an exact recovery copy"


def _journal_retry_status(journal: Path, journal_bytes: bytes) -> str:
    """Describe only journal bytes verified at the canonical path."""
    status, detail = _exact_journal_status(journal, journal_bytes)
    if status == "exact":
        return f"journal {journal} remains for retry"
    if status == "absent":
        return f"{detail}; preserve all available recovery artifacts"
    return f"exact recovery journal could not be restored: {detail}"


def _unsafe_before_error(
    entry: _ResolvedEntry,
    journal: Path,
    journal_bytes: bytes,
    state: str,
) -> ReconcilePersistenceError:
    """Build a diagnostic for an after-image that cannot be safely restored."""
    journal_status = _journal_retry_status(journal, journal_bytes)
    message = (
        f"cannot safely recover destination {entry.destination}: it still matches the "
        f"transaction after image, but before image {entry.before_path} is {state}; journal "
        f"status: {journal_status}; restore the required before image or "
        "preserve the destination manually, then rerun 'doc-lattice reconcile --recover'"
    )
    return ReconcilePersistenceError(message)


def _unsafe_artifact_error(
    staged: Path,
    destination: Path,
    journal: Path,
    journal_bytes: bytes,
    state: str,
) -> ReconcilePersistenceError:
    """Build a manual-recovery diagnostic for an unauthenticated stage."""
    journal_status = _journal_retry_status(journal, journal_bytes)
    message = (
        f"cannot safely clean staged artifact {staged} for destination {destination}: "
        f"{state}; {journal_status}; preserve the artifact and journal for manual inspection, "
        "correct the recovery evidence, then rerun 'doc-lattice reconcile --recover'"
    )
    return ReconcilePersistenceError(message)


def _nearest_existing_directory(path: Path, project_root: Path) -> Path:
    """Find the closest existing directory at or above a contained path."""
    current = path
    while True:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if current == project_root:
                raise
            current = current.parent
            continue
        if not stat.S_ISDIR(mode):
            message = f"recovery synchronization ancestor is not a directory: {current}"
            raise NotADirectoryError(message)
        return current


def _sync_artifact_parent(path: Path, project_root: Path) -> None:
    """Synchronize an artifact parent or its nearest existing contained ancestor."""
    sync_directory(_nearest_existing_directory(path.parent, project_root))


def _resync_after_unlink(path: Path, project_root: Path, primary: OSError) -> bool:
    """Retry parent synchronization only when an unlink already removed its path."""
    if path.exists():
        return False
    try:
        _sync_artifact_parent(path, project_root)
    except OSError as retry_error:
        primary.add_note(f"directory resync failed after unlink of {path}: {retry_error}")
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError as retry_error:
        primary.add_note(f"cannot verify absence after resync of {path}: {retry_error}")
        return False
    primary.add_note(f"path reappeared during directory resync after unlink of {path}")
    return False


def _authenticate_staged_artifact(
    staged: Path,
    expected_sha256: str,
    destination: Path,
    journal: Path,
    journal_bytes: bytes,
) -> bool:
    """Return whether a present stage is a regular file with its recorded digest."""
    try:
        initial_stat = staged.lstat()
    except FileNotFoundError:
        return False
    except OSError as cause:
        state = f"cannot inspect artifact: {cause}"
        raise _unsafe_artifact_error(staged, destination, journal, journal_bytes, state) from cause
    if not stat.S_ISREG(initial_stat.st_mode):
        raise _unsafe_artifact_error(
            staged,
            destination,
            journal,
            journal_bytes,
            "artifact is not a regular file",
        )
    try:
        actual_sha256 = file_sha256(staged)
    except FileNotFoundError:
        return False
    except OSError as cause:
        raise _unsafe_artifact_error(
            staged,
            destination,
            journal,
            journal_bytes,
            f"cannot read artifact: {cause}",
        ) from cause
    try:
        verified_stat = staged.lstat()
    except FileNotFoundError:
        return False
    except OSError as cause:
        state = f"cannot re-inspect artifact after reading: {cause}"
        raise _unsafe_artifact_error(staged, destination, journal, journal_bytes, state) from cause
    initial_identity = (initial_stat.st_dev, initial_stat.st_ino)
    verified_identity = (verified_stat.st_dev, verified_stat.st_ino)
    if not stat.S_ISREG(verified_stat.st_mode) or verified_identity != initial_identity:
        state = "artifact changed or became nonregular during authentication"
        raise _unsafe_artifact_error(staged, destination, journal, journal_bytes, state)
    if actual_sha256 != expected_sha256:
        state = (
            f"artifact is corrupt: digest mismatch "
            f"(expected {expected_sha256}, got {actual_sha256})"
        )
        raise _unsafe_artifact_error(staged, destination, journal, journal_bytes, state)
    return True


def _cleanup_staged_artifact(
    staged: Path,
    expected_sha256: str,
    destination: Path,
    journal: Path,
    journal_bytes: bytes,
) -> None:
    """Remove one authenticated stage, healing a post-unlink sync failure."""
    is_present = _authenticate_staged_artifact(
        staged, expected_sha256, destination, journal, journal_bytes
    )
    if not is_present:
        try:
            _sync_artifact_parent(staged, journal.parent)
        except OSError as cause:
            raise _recovery_operation_error(
                "synchronizing absent staged artifact parent",
                staged,
                journal,
                journal_bytes,
                cause,
            ) from cause
        try:
            staged.lstat()
        except FileNotFoundError:
            return
        except OSError as cause:
            state = f"cannot verify artifact absence after synchronization: {cause}"
            raise _unsafe_artifact_error(
                staged, destination, journal, journal_bytes, state
            ) from cause
        state = "artifact appeared while synchronizing its previously observed absence"
        raise _unsafe_artifact_error(staged, destination, journal, journal_bytes, state)
    try:
        durable_unlink(staged)
    except OSError as primary:
        if _resync_after_unlink(staged, journal.parent, primary):
            return
        raise _recovery_operation_error(
            "cleaning staged artifact", staged, journal, journal_bytes, primary
        ) from primary


def _restore_journal(journal: Path, journal_bytes: bytes, primary: OSError) -> bool:
    """Restore exact bytes only when the canonical journal path is absent."""
    status, detail = _exact_journal_status(journal, journal_bytes)
    if status == "exact":
        return True
    if status != "absent":
        primary.add_note(
            f"exact recovery journal could not be restored: {detail}; refusing to overwrite"
        )
        return False
    try:
        atomic_create_bytes(
            journal,
            journal_bytes,
            prefix=f"{RECONCILE_JOURNAL_NAME}.",
        )
    except OSError as restore_error:
        primary.add_note(f"journal restoration failed for {journal}: {restore_error}")
    status, detail = _exact_journal_status(journal, journal_bytes)
    if status == "exact":
        return True
    primary.add_note(f"exact recovery journal could not be restored: {detail}")
    return False


def _cleanup_journal(journal: Path, journal_bytes: bytes) -> None:
    """Remove the journal last, restoring it if persistent post-unlink sync fails."""
    status, detail = _exact_journal_status(journal, journal_bytes)
    if status != "exact":
        message = (
            f"cannot safely clean reconcile journal {journal}: {detail}; refusing to remove "
            "an unverified journal path; preserve all available recovery evidence for manual "
            "inspection"
        )
        raise ReconcilePersistenceError(message)
    try:
        durable_unlink(journal)
    except OSError as primary:
        if _resync_after_unlink(journal, journal.parent, primary):
            return
        _restore_journal(journal, journal_bytes, primary)
        raise _recovery_operation_error(
            "cleaning journal", journal, journal, journal_bytes, primary
        ) from primary


def _cleanup_transaction_artifacts(
    entries: tuple[_ResolvedEntry, ...],
    journal: Path,
    journal_bytes: bytes,
) -> None:
    """Durably remove staged images and then remove the journal last."""
    staged_artifacts = _staged_artifacts(entries)
    _authenticate_transaction_artifacts(staged_artifacts, journal, journal_bytes)
    for staged, expected_sha256, destination in staged_artifacts:
        _cleanup_staged_artifact(
            staged,
            expected_sha256,
            destination,
            journal,
            journal_bytes,
        )
    _cleanup_journal(journal, journal_bytes)


def _staged_artifacts(
    entries: tuple[_ResolvedEntry, ...],
) -> tuple[tuple[Path, str, Path], ...]:
    """Return every journal stage paired with its role-specific digest."""
    return tuple(
        staged
        for entry in entries
        for staged in (
            (entry.before_path, entry.before_sha256, entry.destination),
            (entry.after_path, entry.after_sha256, entry.destination),
        )
    )


def _authenticate_transaction_artifacts(
    staged_artifacts: tuple[tuple[Path, str, Path], ...],
    journal: Path,
    journal_bytes: bytes,
) -> None:
    """Authenticate every present stage before any recovery mutation begins."""
    for staged, expected_sha256, destination in staged_artifacts:
        _authenticate_staged_artifact(
            staged,
            expected_sha256,
            destination,
            journal,
            journal_bytes,
        )


def _rollback_prepared(
    entries: tuple[_ResolvedEntry, ...],
    journal: Path,
    journal_bytes: bytes,
) -> None:
    """Restore transaction-owned after-images while preserving unrelated changes."""
    for entry in reversed(entries):
        try:
            current_sha256 = file_sha256(entry.destination)
        except FileNotFoundError:
            continue
        except OSError as cause:
            raise _recovery_operation_error(
                "fingerprinting destination",
                entry.destination,
                journal,
                journal_bytes,
                cause,
            ) from cause
        if current_sha256 != entry.after_sha256:
            continue
        is_present = _authenticate_staged_artifact(
            entry.before_path,
            entry.before_sha256,
            entry.destination,
            journal,
            journal_bytes,
        )
        if not is_present:
            raise _unsafe_before_error(entry, journal, journal_bytes, "missing")
        try:
            replace_staged(entry.before_path, entry.destination)
        except (OSError, ValueError) as cause:
            raise _recovery_operation_error(
                "restoring destination",
                entry.destination,
                journal,
                journal_bytes,
                cause,
            ) from cause
    _cleanup_transaction_artifacts(entries, journal, journal_bytes)


def _journal_path(project_root: Path) -> Path:
    """Return the reconcile journal path for a project root."""
    return project_root / RECONCILE_JOURNAL_NAME


def _require_reconcile_lock(lock: object, project_root: Path) -> ReconcileLock:
    """Validate an active capability for the requested project root."""
    if not isinstance(lock, ReconcileLock):
        message = "reconcile mutation requires an active reconcile lock"
        raise ReconcileInProgressError(message)
    if not lock.is_active:
        message = "reconcile lock capability is no longer active"
        raise ReconcileInProgressError(message)
    try:
        requested_root = project_root.resolve()
    except (OSError, RuntimeError) as cause:
        message = f"cannot validate reconcile lock project root {project_root}: {cause}"
        raise ReconcileInProgressError(message) from cause
    if lock.project_root != requested_root:
        message = (
            f"reconcile lock capability protects a different project root: "
            f"{lock.project_root}, not {requested_root}"
        )
        raise ReconcileInProgressError(message)
    try:
        directory_stat = requested_root.stat()
    except OSError as cause:
        message = f"cannot validate reconcile lock project root directory {requested_root}: {cause}"
        raise ReconcileInProgressError(message) from cause
    if not lock._protects_directory(directory_stat):
        message = (
            "reconcile lock capability protects a different project root directory "
            f"than the directory currently at {requested_root}"
        )
        raise ReconcileInProgressError(message)
    return lock


@contextmanager
def _reconcile_operation_lease(
    lock: object,
    project_root: Path,
) -> Iterator[None]:
    """Reserve a validated lock capability for one mutation operation."""
    validated_lock = _require_reconcile_lock(lock, project_root)
    validated_lock._claim_operation()
    try:
        yield
    finally:
        validated_lock._release_operation()


def _cleanup_unpublished_stages(staged_paths: list[Path], primary: BaseException) -> None:
    """Durably clean this preparation attempt's unpublished staged images."""
    for staged in staged_paths:
        try:
            durable_unlink(staged)
        except OSError as cleanup_error:
            primary.add_note(
                f"durable cleanup failed for unpublished stage {staged}: {cleanup_error}; "
                "it has no recovery journal, so inspect and remove it manually after "
                "confirming it is not a destination"
            )


def _cleanup_failed_journal_publication(
    project_root: Path,
    journal_path: Path,
    prepared_bytes: bytes,
    staged_paths: list[Path],
    primary: OSError,
) -> None:
    """Clean a failed preparation without touching a pre-existing journal."""
    if isinstance(primary, FileExistsError):
        _cleanup_unpublished_stages(staged_paths, primary)
        return
    status, _detail = _exact_journal_status(journal_path, prepared_bytes)
    if status != "exact":
        _cleanup_unpublished_stages(staged_paths, primary)
        return
    try:
        _loaded, entries, journal_bytes = _load_journal(project_root, journal_path)
        _cleanup_transaction_artifacts(entries, journal_path, journal_bytes)
    except ReconcilePersistenceError as cleanup_error:
        primary.add_note(f"failed preparation cleanup: {cleanup_error}")


def _preflight_rewrite_destinations(
    project_root: Path,
    journal_path: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
) -> tuple[_PendingRewrite, ...]:
    """Validate all destination journal invariants knowable before staging."""
    pending: list[_PendingRewrite] = []
    destination_indices: dict[Path, int] = {}
    canonical_root = project_root.resolve()
    canonical_journal = canonical_root / journal_path.name
    for index, rewrite in enumerate(rewrites):
        destination = safe_resolve(write_paths[rewrite.path], canonical_root)
        destination_relative = destination.relative_to(canonical_root).as_posix()
        if destination == canonical_journal:
            message = f"reconcile destination {destination} aliases journal path {journal_path}"
            raise ValueError(message)
        if destination in destination_indices:
            first_index = destination_indices[destination]
            message = (
                f"duplicate reconcile destination {destination} for rewrites "
                f"{first_index} and {index}"
            )
            raise ValueError(message)
        destination_indices[destination] = index
        pending.append(_PendingRewrite(rewrite, destination, destination_relative))
    return tuple(pending)


def _copy_notes(target: BaseException, source: BaseException) -> None:
    """Copy diagnostic notes from a lower-level failure to its typed wrapper."""
    copy_exception_notes(target, source)


def _prepare_transaction(
    project_root: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
) -> _PreparedTransaction:
    """Stage exact images and durably publish an ordered prepared journal."""
    journal_path = _journal_path(project_root)
    if _journal_is_present(journal_path):
        message = (
            f"reconcile journal {journal_path} already exists; preserve it and run "
            "'doc-lattice reconcile --recover'"
        )
        raise ReconcilePersistenceError(message)
    staged_paths: list[Path] = []
    journal_entries: list[JournalEntry] = []
    operation = "validating transaction destinations"
    operation_path = project_root
    prepared_bytes = b""
    try:
        pending_rewrites = _preflight_rewrite_destinations(
            project_root,
            journal_path,
            rewrites,
            write_paths,
        )
        operation = "staging transaction image"
        for pending in pending_rewrites:
            rewrite = pending.rewrite
            destination = pending.destination
            operation_path = destination
            before_path = stage_bytes(
                destination,
                rewrite.before,
                prefix=f".{destination.name}{RECONCILE_BEFORE_IMAGE_INFIX}",
            )
            staged_paths.append(before_path)
            after_path = stage_bytes(
                destination,
                rewrite.after,
                prefix=f".{destination.name}{RECONCILE_AFTER_IMAGE_INFIX}",
            )
            staged_paths.append(after_path)
            journal_entries.append(
                JournalEntry(
                    destination=pending.destination_relative,
                    before_path=before_path.relative_to(project_root).as_posix(),
                    before_sha256=sha256_bytes(rewrite.before),
                    after_path=after_path.relative_to(project_root).as_posix(),
                    after_sha256=sha256_bytes(rewrite.after),
                )
            )
        prepared = Journal(
            version=RECONCILE_JOURNAL_VERSION,
            state="prepared",
            entries=tuple(journal_entries),
        )
        prepared_bytes = prepared.model_dump_json().encode("utf-8")
        operation = "publishing prepared journal"
        operation_path = journal_path
        atomic_create_bytes(
            journal_path,
            prepared_bytes,
            prefix=f"{RECONCILE_JOURNAL_NAME}.",
        )
    except (KeyError, OSError, RuntimeError, ValueError) as primary:
        if operation == "publishing prepared journal" and isinstance(primary, OSError):
            _cleanup_failed_journal_publication(
                project_root,
                journal_path,
                prepared_bytes,
                staged_paths,
                primary,
            )
        else:
            _cleanup_unpublished_stages(staged_paths, primary)
        if isinstance(primary, FileExistsError):
            message = (
                f"reconcile journal {journal_path} already exists; preserve it and run "
                "'doc-lattice reconcile --recover'"
            )
        else:
            message = (
                f"reconcile preparation failed while {operation} {operation_path}: "
                f"{_cause_details(primary)}; no destination was changed"
            )
        error = ReconcilePersistenceError(message)
        _copy_notes(error, primary)
        raise error from primary
    loaded, entries, journal_bytes = _load_journal(project_root, journal_path)
    return _PreparedTransaction(loaded, entries, journal_path, journal_bytes)


def _commit_operation_error(
    operation: str,
    path: Path,
    cause: OSError | ValueError,
) -> ReconcilePersistenceError:
    """Wrap one commit I/O failure with its operation and destination."""
    error = ReconcilePersistenceError(
        f"reconcile commit failed while {operation} {path}: {_cause_details(cause)}"
    )
    _copy_notes(error, cause)
    return error


def _abort_prepared(
    prepared: _PreparedTransaction,
    primary: ReconcileConflictError | ReconcilePersistenceError,
    *,
    authenticate_all: bool = True,
) -> NoReturn:
    """Roll back a prepared transaction, preserving the primary failure."""
    try:
        if authenticate_all:
            _authenticate_transaction_artifacts(
                _staged_artifacts(prepared.entries),
                prepared.journal_path,
                prepared.journal_bytes,
            )
        _rollback_prepared(
            prepared.entries,
            prepared.journal_path,
            prepared.journal_bytes,
        )
    except ReconcilePersistenceError as rollback_error:
        error = ReconcilePersistenceError(
            f"{primary}; rollback failed: {rollback_error}; recovery artifacts remain; "
            "run 'doc-lattice reconcile --recover'"
        )
        error.add_note(f"original commit failure: {_cause_details(primary)}")
        error.add_note(f"rollback failure: {_cause_details(rollback_error)}")
        raise error from primary
    message = f"{primary}; no files were reconciled (rollback complete)"
    if isinstance(primary, ReconcileConflictError):
        error = ReconcileConflictError(message)
    else:
        error = ReconcilePersistenceError(message)
    _copy_notes(error, primary)
    raise error from primary


def _reset_prepared_journal(
    prepared: _PreparedTransaction,
    committed_bytes: bytes,
) -> None:
    """Durably restore the prepared journal after a failed marker update."""
    journal_path = prepared.journal_path
    try:
        current_bytes = journal_path.read_bytes()
    except FileNotFoundError:
        try:
            atomic_create_bytes(
                journal_path,
                prepared.journal_bytes,
                prefix=f"{RECONCILE_JOURNAL_NAME}.",
            )
        except OSError as cause:
            raise _commit_operation_error(
                "restoring prepared journal", journal_path, cause
            ) from cause
    except OSError as cause:
        raise _commit_operation_error("reading journal for reset", journal_path, cause) from cause
    else:
        if current_bytes == prepared.journal_bytes:
            return
        if current_bytes != committed_bytes:
            message = (
                f"reconcile commit failed while resetting prepared journal {journal_path}: "
                "the visible journal contains unexpected bytes"
            )
            raise ReconcilePersistenceError(message)
        try:
            atomic_replace_bytes(
                journal_path,
                prepared.journal_bytes,
                prefix=f"{RECONCILE_JOURNAL_NAME}.",
            )
        except OSError as cause:
            raise _commit_operation_error(
                "resetting prepared journal", journal_path, cause
            ) from cause
    status, detail = _exact_journal_status(journal_path, prepared.journal_bytes)
    if status != "exact":
        message = (
            f"reconcile commit failed while resetting prepared journal {journal_path}: {detail}"
        )
        raise ReconcilePersistenceError(message)


def _abort_failed_marker(
    prepared: _PreparedTransaction,
    committed_bytes: bytes,
    primary: ReconcilePersistenceError,
) -> NoReturn:
    """Reset a failed commit marker before allowing document rollback."""
    try:
        _reset_prepared_journal(prepared, committed_bytes)
    except ReconcilePersistenceError as reset_error:
        error = ReconcilePersistenceError(
            f"{primary}; prepared journal reset failed: {reset_error}; rollback was not attempted; "
            "preserve the journal and staged evidence, then run "
            "'doc-lattice reconcile --recover'"
        )
        error.add_note(f"original marker failure: {_cause_details(primary)}")
        error.add_note(f"journal reset failure: {_cause_details(reset_error)}")
        raise error from primary
    _abort_prepared(prepared, primary)


def commit_rewrites(
    project_root: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
    *,
    lock: ReconcileLock,
) -> None:
    """Commit exact-byte reconcile rewrites as one durable transaction.

    Args:
        project_root: Configured project root containing the transaction journal.
        rewrites: Ordered fresh-read rewrites to publish.
        write_paths: Contained resolved destinations keyed by rewrite identity path.
        lock: Active capability protecting this project root.

    Raises:
        ReconcileConflictError: If a destination changed after rewrite validation.
        ReconcileInProgressError: If the lock capability is absent or invalid.
        ReconcilePersistenceError: If preparation or durable commit cannot complete.
    """
    with _reconcile_operation_lease(lock, project_root):
        _commit_rewrites_locked(project_root, rewrites, write_paths)


def _commit_rewrites_locked(
    project_root: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
) -> None:
    """Commit rewrites while the public API holds its capability lease."""
    prepared = _prepare_transaction(project_root, rewrites, write_paths)
    for entry in prepared.entries:
        try:
            current_sha256 = file_sha256(entry.destination)
        except OSError as cause:
            primary = _commit_operation_error(
                "fingerprinting destination", entry.destination, cause
            )
            _abort_prepared(prepared, primary)
        if current_sha256 != entry.before_sha256:
            primary = ReconcileConflictError(
                f"reconcile destination {entry.destination} changed after validation"
            )
            _abort_prepared(prepared, primary)
        try:
            after_present = _authenticate_staged_artifact(
                entry.after_path,
                entry.after_sha256,
                entry.destination,
                prepared.journal_path,
                prepared.journal_bytes,
            )
        except ReconcilePersistenceError as primary:
            _abort_prepared(prepared, primary, authenticate_all=False)
        if not after_present:
            primary = ReconcilePersistenceError(
                f"cannot apply reconcile destination {entry.destination}: staged after image "
                f"{entry.after_path} is missing immediately before replacement"
            )
            _abort_prepared(prepared, primary, authenticate_all=False)
        try:
            replace_staged(entry.after_path, entry.destination)
        except (OSError, ValueError) as cause:
            primary = _commit_operation_error("replacing destination", entry.destination, cause)
            _abort_prepared(prepared, primary)
    committed = Journal(
        version=prepared.journal.version,
        state="committed",
        entries=prepared.journal.entries,
    )
    committed_bytes = committed.model_dump_json().encode("utf-8")
    committed_durably = False
    try:
        atomic_replace_bytes(
            prepared.journal_path,
            committed_bytes,
            prefix=f"{RECONCILE_JOURNAL_NAME}.",
        )
    except OSError as cause:
        primary = _commit_operation_error("marking journal committed", prepared.journal_path, cause)
        _abort_failed_marker(prepared, committed_bytes, primary)
    committed_durably = True
    if committed_durably:
        _cleanup_transaction_artifacts(
            prepared.entries,
            prepared.journal_path,
            committed_bytes,
        )


def _open_reconcile_lock_directory(project_root: Path) -> int:
    """Open the project directory or raise a typed lock setup error."""
    try:
        return os.open(project_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as cause:
        raise _lock_setup_error("opening project directory", project_root, cause) from cause


def _claim_reconcile_lock(fd: int, project_root: Path) -> None:
    """Acquire the nonblocking advisory lock or raise its typed domain error."""
    try:
        _flock(fd, release=False)
    except BlockingIOError:
        message = "another reconcile is in progress; retry after it exits"
        raise ReconcileInProgressError(message) from None
    except OSError as cause:
        raise _lock_setup_error("acquiring reconcile lock", project_root, cause) from cause


def _flock(fd: int, *, release: bool) -> None:
    """Apply the platform advisory lock operation to a directory descriptor."""
    try:
        import fcntl  # noqa: PLC0415 (deferred so non-reconcile commands work without fcntl)
    except ImportError as cause:
        raise _unsupported_lock_error() from cause
    operation = fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(fd, operation)


def _inspect_reconcile_lock_directory(fd: int, project_root: Path) -> os.stat_result:
    """Inspect the locked directory descriptor or raise a typed setup error."""
    try:
        return os.fstat(fd)
    except OSError as cause:
        operation = "inspecting locked project directory"
        raise _lock_setup_error(operation, project_root, cause) from cause


def _new_reconcile_lock(
    project_root: Path,
    directory_stat: os.stat_result,
) -> ReconcileLock:
    """Create the root-bound capability or raise a typed setup error."""
    try:
        return ReconcileLock(project_root, directory_stat, _LOCK_FACTORY_TOKEN)
    except (OSError, RuntimeError) as cause:
        operation = "creating reconcile lock capability"
        raise _lock_setup_error(operation, project_root, cause) from cause


@contextmanager
def reconcile_lock(project_root: Path) -> Iterator[ReconcileLock]:
    """Hold the existing project directory's nonblocking advisory reconcile lock.

    Args:
        project_root: The existing configured project-root directory.

    Yields:
        An active capability while this process exclusively holds the advisory lock.

    Raises:
        ReconcileInProgressError: If another reconcile process holds the lock.
        ReconcilePersistenceError: If lock setup, release, or close fails.
    """
    if not _LOCKING_SUPPORTED:
        raise _unsupported_lock_error()
    fd = _open_reconcile_lock_directory(project_root)
    acquired = False
    capability: ReconcileLock | None = None
    try:
        _claim_reconcile_lock(fd, project_root)
        acquired = True
        directory_stat = _inspect_reconcile_lock_directory(fd, project_root)
        capability = _new_reconcile_lock(project_root, directory_stat)
        yield capability
    finally:
        active_error = sys.exception()
        cleanup_errors: list[tuple[str, OSError]] = []
        if capability is not None:
            capability._deactivate()
        try:
            if acquired:
                _flock(fd, release=True)
        except OSError as cause:
            cleanup_errors.append(("lock release", cause))
        try:
            os.close(fd)
        except OSError as cause:
            cleanup_errors.append(("lock close", cause))
        if cleanup_errors:
            details = "; ".join(f"{phase} failed: {cause}" for phase, cause in cleanup_errors)
            if active_error is not None:
                active_error.add_note(f"reconcile {details}")
            else:
                message = f"reconcile {details} for project directory {project_root}"
                error = ReconcilePersistenceError(message)
                raise error from cleanup_errors[0][1]


def _unsupported_lock_error() -> ReconcilePersistenceError:
    """Return the typed error for platforms without POSIX advisory locking."""
    return ReconcilePersistenceError(
        f"reconcile locking is not supported on this platform ({sys.platform})"
    )


def _lock_setup_error(
    operation: str,
    project_root: Path,
    cause: BaseException,
) -> ReconcilePersistenceError:
    """Wrap one lock context-entry failure with its operation and project root."""
    error = ReconcilePersistenceError(
        f"reconcile lock setup failed while {operation} for project root {project_root}: {cause}"
    )
    copy_exception_notes(error, cause)
    return error


def ensure_dry_run_safe(project_root: Path) -> None:
    """Refuse a read-only dry run while a reconcile journal needs recovery.

    Args:
        project_root: The configured project root to inspect without mutation.

    Raises:
        ReconcilePersistenceError: If a reconcile journal already exists.
    """
    journal = _journal_path(project_root)
    if _journal_is_present(journal):
        message = (
            f"reconcile journal {journal} requires recovery; "
            "run 'doc-lattice reconcile --recover' first"
        )
        raise ReconcilePersistenceError(message)


def recover_transaction(
    project_root: Path,
    *,
    lock: ReconcileLock,
) -> RecoveryResult:
    """Recover or finish cleanup for a durable reconcile journal.

    Args:
        project_root: The configured project root containing transaction artifacts.
        lock: Active capability protecting this project root.

    Returns:
        The recovery action and project journal path.

    Raises:
        ReconcileInProgressError: If the lock capability is absent or invalid.
    """
    with _reconcile_operation_lease(lock, project_root):
        return _recover_transaction_locked(project_root)


def _recover_transaction_locked(project_root: Path) -> RecoveryResult:
    """Recover one journal while the public API holds its capability lease."""
    journal = _journal_path(project_root)
    if not _journal_is_present(journal):
        return RecoveryResult(action="none", journal=journal)
    loaded, entries, journal_bytes = _load_journal(project_root, journal)
    _authenticate_transaction_artifacts(_staged_artifacts(entries), journal, journal_bytes)
    if loaded.state == "prepared":
        _rollback_prepared(entries, journal, journal_bytes)
        return RecoveryResult(action="rolled_back", journal=journal)
    _cleanup_transaction_artifacts(entries, journal, journal_bytes)
    return RecoveryResult(action="cleaned_committed", journal=journal)
