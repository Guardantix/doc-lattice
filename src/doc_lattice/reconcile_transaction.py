"""Serialize reconcile processes and recover durable transaction journals."""

import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, NoReturn, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from . import __version__
from .constants import (
    PERSISTENCE_TEMP_SUFFIX,
    RECONCILE_AFTER_IMAGE_INFIX,
    RECONCILE_BEFORE_IMAGE_INFIX,
    RECONCILE_JOURNAL_LEGACY_VERSION,
    RECONCILE_JOURNAL_NAME,
    RECONCILE_JOURNAL_VERSION,
    ReconcileSelectorMode,
)
from .datetime_utils import utc_now
from .error_types import (
    ReconcileConflictError,
    ReconcileInProgressError,
    ReconcilePersistenceError,
    copy_exception_notes,
    exception_details,
)
from .path_utils import format_path_for_display, safe_resolve
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
RecoveryAction = Literal[
    "none",
    "rolled_back",
    "partially_rolled_back",
    "cleaned_committed",
]
_DestinationState = Literal["after", "before", "other"]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_JournalStatus = Literal["absent", "invalid", "exact"]
_LOCK_FACTORY_TOKEN = object()
_LOCKING_SUPPORTED = os.name != "nt"
_JOURNAL_STAGE_PREFIX = f"{RECONCILE_JOURNAL_NAME}."
_PREPARE_VALIDATING = "validating transaction destinations"
_PREPARE_STAGING = "staging transaction image"
_PREPARE_PUBLISHING_JOURNAL = "publishing prepared journal"


class JournalEntry(BaseModel):
    """One destination and its staged before and after images."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    destination: str
    before_path: str
    before_sha256: Sha256Digest
    after_path: str
    after_sha256: Sha256Digest


# The date-time syntax a journal timestamp may use: a calendar date, a T separator, a full clock
# time, optional fractional seconds, and a zone designator. Case in the T and Z designators is
# tolerated because a hand-normalized file may lower them; the space separator RFC 3339 permits by
# agreement is not, since doc-lattice is the only writer of this file and never emits one.
#
# The designator's value is otherwise not judged here: _require_utc_offset owns that, so a syntax
# failure and a wrong-zone failure each report as themselves. The single exception is -00:00,
# which has to be refused on the text because datetime parsing normalizes it to an ordinary zero
# offset, leaving nothing downstream to distinguish it by. It denotes the same instant as Z, so
# this is a conformance rule rather than a correctness one: ISO 8601 forbids the negative-zero
# offset, RFC 3339 gives it the separate meaning "UTC time known, local offset unknown", and this
# field records an instant and has no such meaning to carry.
_TIMESTAMP_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|\+\d{2}:\d{2}|-(?!00:00)\d{2}:\d{2})"
)


def _require_timestamp_text(value: object) -> object:
    """Refuse any ``created_at`` that is not spelled as a date-time string.

    An allowlist, deliberately, rather than a list of refusals. Datetime validation accepts
    several inputs that denote a different instant than they appear to: a bare number and a
    numeric *string* are both read as Unix timestamps, so ``0`` and ``"0"`` alike land on
    1970-01-01, and ``"-1"`` lands a second earlier. Enumerating those one at a time only holds
    until the next coercion exists, so this matches the syntax the format documents and refuses
    everything else.

    Args:
        value: The raw ``created_at`` input, before datetime validation.

    Returns:
        The value unchanged when it is an accepted timestamp string, or the ``datetime`` this
        module passes when it builds provenance directly rather than through JSON.

    Raises:
        ValueError: If the value is neither a datetime nor a matching timestamp string.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and _TIMESTAMP_TEXT.fullmatch(value):
        return value
    message = f"created_at must be an ISO 8601 timestamp string, got {value!r}"
    raise ValueError(message)


def _require_utc_offset(value: datetime) -> datetime:
    """Refuse an aware timestamp that is not expressed in UTC.

    Args:
        value: The validated timezone-aware timestamp.

    Returns:
        The value unchanged when its offset is zero.

    Raises:
        ValueError: If the timestamp carries a nonzero offset. The engine only ever writes UTC,
            so an offset journal did not come from this tool and is not the documented format.
    """
    offset = value.utcoffset()
    if offset != timedelta(0):
        message = f"created_at must be expressed in UTC, got offset {offset}"
        raise ValueError(message)
    return value


# The journal's timestamp type: aware, UTC, and spelled as a string on the wire. AwareDatetime
# alone rules out only a naive value; these two validators close the coercions it still permits.
UtcTimestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_require_timestamp_text),
    AfterValidator(_require_utc_offset),
]


def journal_timestamp_text(value: datetime) -> str:
    """Render a validated journal timestamp in the one spelling this project emits.

    The wire format accepts both ``Z`` and ``+00:00`` and parses either into a datetime, so
    the original token is gone by the time anything reports on it. Recovery output therefore
    normalizes rather than echoing, and it normalizes onto the spelling the serializer already
    writes, so a journal and the report about it never disagree about how an instant is
    written. ``tests/test_reconcile_transaction.py`` pins the two against each other.

    Args:
        value: A timestamp already validated as aware and UTC by ``UtcTimestamp``.

    Returns:
        The ISO 8601 spelling with ``Z`` in place of the zero offset, keeping microseconds
        exactly when the value carries them.
    """
    return f"{value.isoformat().removesuffix('+00:00')}Z"


class JournalSelector(BaseModel):
    """The reconcile selection a transaction was planned from.

    Recorded as typed fields rather than the run's argv, so recovery never parses a command
    line and each selector form stays directly constructible in a test. ``downstream_id`` and
    ``ref`` are nullable but not optional: a v2 journal must spell both keys, because a
    defaulted key would let a journal that lost one recover as though it never had it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ReconcileSelectorMode
    downstream_id: str | None
    ref: str | None

    @model_validator(mode="after")
    def _reject_mode_and_downstream_id_disagreement(self) -> Self:
        """Reject a selector whose downstream id contradicts its mode.

        Returns:
            The validated selector.

        Raises:
            ValueError: If a downstream selector carries no id, or an all selector carries one.
        """
        if self.mode == "downstream" and not self.downstream_id:
            message = "a downstream selector requires a downstream_id"
            raise ValueError(message)
        if self.mode == "all" and self.downstream_id is not None:
            message = "an all selector cannot carry a downstream_id"
            raise ValueError(message)
        return self


class JournalProvenance(BaseModel):
    """What produced a journal: when it was written, by which version, from which selection.

    Every field is required and the model is frozen, so a v2 journal that lost one fails
    validation instead of recovering with a blank an operator would read as fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    created_at: UtcTimestamp
    tool_version: str = Field(min_length=1)
    selector: JournalSelector


class JournalV1(BaseModel):
    """The version 1 wire format: state and entries, with no record of what produced them.

    Read-only. Nothing writes a v1 journal any more, but one left behind by a pre-v2 release
    is still recoverable, so an upgrade never strands an operator holding a crash journal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    state: JournalState
    entries: tuple[JournalEntry, ...]


class JournalV2(BaseModel):
    """The version 2 wire format: the v1 fields plus required, immutable provenance.

    This is the only format the engine writes. It is kept a separate strict model rather than
    a relaxed shared one, because relaxing ``extra`` to admit both shapes would also admit a
    malformed journal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2]
    state: JournalState
    provenance: JournalProvenance
    entries: tuple[JournalEntry, ...]


class _JournalVersionProbe(BaseModel):
    """The version declaration, read before any format-specific validation.

    Deliberately the one lax model here: it ignores every other key so that version dispatch
    happens before a strict model can reject a field belonging to a format it does not know.
    A v2 journal parsed by the v1 model fails on its provenance keys long before any version
    check would run, which is why inspection has to come first.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: int = Field(strict=True)


@dataclass(frozen=True, slots=True)
class ScanFailure:
    """One directory the orphan scan could not enumerate, kept structured until encoding.

    ``unresolved`` and ``orphans`` stay project-relative strings a human sink can convert back
    to a path, but a scan failure fuses its path into prose at the producer. Formatting it
    there would move the machine payload, and escaping the finished sentence at the sink would
    apply the path spelling to a whole sentence, so the two components are retained separately
    and joined by whichever encoder the run selected.
    """

    filename: str
    detail: str

    @property
    def legacy_text(self) -> str:
        """Render the pre-GTX-209 spelling, which the JSON payload still emits verbatim."""
        return f"cannot scan {self.filename} for orphaned artifacts: {self.detail}"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """The action taken for a project reconcile journal.

    ``journal_version`` and ``provenance`` are captured from the journal while it is still
    loaded, because a successful recovery deletes the file before the caller reports on it.
    Both are None when no journal was found at all. A recovered version 1 journal carries its
    version with a None ``provenance``, which is what lets a report distinguish "this format
    recorded none" from "there was nothing to recover" rather than rendering both as blank.
    """

    action: RecoveryAction
    journal: Path
    restored: int = 0
    already_before: int = 0
    unresolved: tuple[str, ...] = ()
    orphans: tuple[str, ...] = ()
    scan_errors: tuple[ScanFailure, ...] = ()
    journal_version: int | None = None
    provenance: JournalProvenance | None = None

    @property
    def is_incomplete(self) -> bool:
        """Return whether recovery left state an operator still has to resolve.

        Returns:
            True when any destination stayed unresolved, any orphaned artifact was
            found, or the artifact scan could not enumerate part of the project.
        """
        return bool(self.unresolved or self.orphans or self.scan_errors)


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
class _LoadedJournal:
    """One journal of any supported version, normalized to the shared recovery shape.

    ``provenance`` is None for a v1 journal, which recorded none. The rollback itself never
    reads it. The field exists so a freshly published prepared journal can be checked against
    the provenance it was built from, and so recovery can carry it out to a report under AD-36
    before a successful cleanup deletes the file it came from.
    """

    version: int
    state: JournalState
    entries: tuple[JournalEntry, ...]
    provenance: JournalProvenance | None


@dataclass(frozen=True, slots=True)
class _PreparedTransaction:
    """A published prepared journal and its validated filesystem entries.

    ``provenance`` is the single capture this transaction made before staging. Commit copies
    it into the committed marker unchanged rather than re-reading the clock, which would
    leave a crash journal inconsistent with itself.
    """

    journal: _LoadedJournal
    provenance: JournalProvenance
    entries: tuple[_ResolvedEntry, ...]
    journal_path: Path
    journal_bytes: bytes


@dataclass(frozen=True, slots=True)
class _RollbackOutcome:
    """Every prepared destination classified by what one rollback found and did.

    ``restored`` still held the transaction's after image and was replaced with its
    before image. ``already_before`` was a full rollback that needed no mutation.
    ``unresolved`` matched neither recorded image, including absence, on a destination
    the transaction may have applied. ``untouched`` is that same mismatch on a
    destination the transaction never attempted, which no rollback owns.
    """

    restored: tuple[Path, ...]
    already_before: tuple[Path, ...]
    unresolved: tuple[Path, ...]
    untouched: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _PendingRewrite:
    """A rewrite whose contained destination passed preflight validation."""

    rewrite: Rewrite
    destination: Path
    destination_relative: str


def _invalid_journal_error(journal: Path, cause: object) -> ReconcilePersistenceError:
    """Build the deliberate manual-remediation diagnostic for an invalid journal."""
    message = (
        f"invalid reconcile journal {format_path_for_display(journal)}: {cause}; inspect "
        f"{format_path_for_display(journal)}, its destinations, "
        "and staged files; move the invalid journal aside only after manual restoration or "
        "preservation; rerun 'doc-lattice reconcile --recover'"
    )
    return ReconcilePersistenceError(message)


def _journal_already_exists_message(journal_path: Path) -> str:
    """Render the identical diagnostic for a pre-existing or racing journal."""
    return (
        f"reconcile journal {format_path_for_display(journal_path)} already exists; preserve "
        "it and run 'doc-lattice reconcile --recover'"
    )


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
    # The recorded string, not the parsed Path: this diagnostic rejects exactly what the
    # journal spells, and Path() would normalize the spelling it is reporting on.
    recorded = format_path_for_display(raw_path)
    if path.is_absolute():
        message = f"{field} must be relative, got {recorded}"
        raise ValueError(message)
    try:
        return safe_resolve(project_root / path, project_root)
    except (OSError, RuntimeError, ValueError) as cause:
        message = f"unsafe {field} {recorded}: {cause}"
        raise ValueError(message) from cause


def _validate_artifact_path(
    project_root: Path,
    destination: Path,
    artifact: Path,
    role: Literal["before", "after"],
    raw_path: str,
) -> None:
    """Validate the location, name, and existing type of one staged artifact."""
    field = f"{role}_path"
    # artifact is the safe_resolve'd path, so it is the right subject for containment and
    # directory-membership checks. The symlink and file-type checks below must run on the
    # unresolved recorded path instead: resolving would follow a symlink planted at that
    # name and report the target's type, hiding exactly the substitution being rejected.
    candidate = project_root / Path(raw_path)
    # Every message below names the same pair, so the display spelling is applied once here
    # rather than at each of the six sinks. `recorded` is the journal's own string; `resolved`
    # is what it resolved to.
    recorded = format_path_for_display(raw_path)
    resolved = format_path_for_display(artifact)
    if artifact.parent != destination.parent:
        message = (
            f"{field} {recorded} ({resolved}) must be in destination directory "
            f"{format_path_for_display(destination.parent)}"
        )
        raise ValueError(message)
    infix = RECONCILE_BEFORE_IMAGE_INFIX if role == "before" else RECONCILE_AFTER_IMAGE_INFIX
    prefix = f".{destination.name}{infix}"
    suffix = PERSISTENCE_TEMP_SUFFIX
    name = Path(raw_path).name
    component = name[len(prefix) : -len(suffix)]
    if not name.startswith(prefix) or not name.endswith(suffix) or not component:
        # The expected pattern embeds destination.name, so it carries a document filename and
        # is displayed whole. Displaying `prefix` and `suffix` separately would quote the
        # pattern in three pieces and read as three unrelated values.
        expected = format_path_for_display(prefix + "<nonempty>" + suffix)
        message = f"{field} {recorded} ({resolved}) must match {expected} exactly"
        raise ValueError(message)
    if candidate.is_symlink():
        message = f"{field} {recorded} ({resolved}) is a symlink, not a recovery artifact"
        raise ValueError(message)
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as cause:
        message = f"cannot inspect {field} {recorded} ({resolved}): {cause}"
        raise ValueError(message) from cause
    if not stat.S_ISREG(mode):
        message = f"{field} {recorded} ({resolved}) is a nonregular recovery artifact"
        raise ValueError(message)


def _validate_path_roles(entries: tuple[_ResolvedEntry, ...], journal_path: Path) -> None:
    """Reject aliases between journal destinations and transaction artifacts."""
    canonical_journal = journal_path.resolve()
    destinations: dict[Path, int] = {}
    for index, entry in enumerate(entries):
        if entry.destination == canonical_journal:
            message = (
                f"entry {index} destination aliases journal path "
                f"{format_path_for_display(journal_path)}"
            )
            raise ValueError(message)
        if entry.destination in destinations:
            first = destinations[entry.destination]
            message = (
                f"destination alias across entries {first} and {index}: "
                f"{format_path_for_display(entry.destination)}"
            )
            raise ValueError(message)
        destinations[entry.destination] = index

    artifacts: dict[Path, tuple[int, str]] = {}
    for index, entry in enumerate(entries):
        for role_field, artifact in (
            ("before_path", entry.before_path),
            ("after_path", entry.after_path),
        ):
            if artifact == canonical_journal:
                message = (
                    f"entry {index} {role_field} aliases journal path "
                    f"{format_path_for_display(journal_path)}"
                )
                raise ValueError(message)
            if artifact in destinations:
                message = (
                    f"entry {index} {role_field} artifact {format_path_for_display(artifact)} "
                    "aliases destination path"
                )
                raise ValueError(message)
            if artifact in artifacts:
                first_index, first_field = artifacts[artifact]
                message = (
                    f"artifact alias between entry {first_index} {first_field} and "
                    f"entry {index} {role_field}: {format_path_for_display(artifact)}"
                )
                raise ValueError(message)
            artifacts[artifact] = (index, role_field)


def _serialize_journal(journal: JournalV2) -> bytes:
    """Render a journal to the exact bytes every publication of it writes.

    Args:
        journal: The prepared or committed journal to publish.

    Returns:
        Pretty-printed UTF-8 JSON with a trailing newline. Prepared and committed bytes both
        come from here, so the two forms cannot drift in formatting, and an operator holding a
        crash journal can read it without reformatting it first.
    """
    return f"{journal.model_dump_json(indent=2)}\n".encode()


def _parse_journal(decoded: str) -> _LoadedJournal:
    """Dispatch on the declared version, then validate against that version's wire model.

    Args:
        decoded: The journal's UTF-8 text.

    Returns:
        The journal normalized to the shared recovery shape, whichever version wrote it.

    Raises:
        ValueError: If the text is not valid JSON, declares no usable version, does not match
            the model for the version it declares, or declares a version this release cannot
            read. Pydantic's own validation error is a ValueError, so both arrive as one kind.
    """
    declared = _JournalVersionProbe.model_validate_json(decoded)
    if declared.version == RECONCILE_JOURNAL_LEGACY_VERSION:
        legacy = JournalV1.model_validate_json(decoded)
        return _LoadedJournal(
            version=legacy.version,
            state=legacy.state,
            entries=legacy.entries,
            provenance=None,
        )
    if declared.version == RECONCILE_JOURNAL_VERSION:
        current = JournalV2.model_validate_json(decoded)
        return _LoadedJournal(
            version=current.version,
            state=current.state,
            entries=current.entries,
            provenance=current.provenance,
        )
    message = f"unsupported version {declared.version}"
    raise ValueError(message)


def _load_journal(
    project_root: Path,
    journal_path: Path,
) -> tuple[_LoadedJournal, tuple[_ResolvedEntry, ...], bytes]:
    """Read, validate, and contain every path in a reconcile journal."""
    if not _journal_is_present(journal_path):
        cause = FileNotFoundError(
            f"canonical journal {format_path_for_display(journal_path)} is absent"
        )
        raise _invalid_journal_error(journal_path, cause) from cause
    try:
        encoded = journal_path.read_bytes()
        decoded = encoded.decode("utf-8")
        journal = _parse_journal(decoded)
    # ValueError is the single kind every non-I/O failure here arrives as: the UTF-8 decode
    # failure, pydantic's ValidationError, and the unsupported-version refusal _parse_journal
    # raises itself all derive from it. Naming the subclasses too would suggest they are
    # handled apart from it, which is exactly the confusion _parse_journal documents away.
    except (OSError, ValueError) as cause:
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
                role="before",
                raw_path=raw_entry.before_path,
            )
            _validate_artifact_path(
                project_root,
                entry.destination,
                entry.after_path,
                role="after",
                raw_path=raw_entry.after_path,
            )
    except ValueError as cause:
        raise _invalid_journal_error(journal_path, cause) from cause
    return journal, entries, encoded


def _recovery_operation_error(
    operation: str,
    path: Path,
    journal: Path,
    journal_bytes: bytes,
    cause: BaseException,
) -> ReconcilePersistenceError:
    """Build a retryable recovery operation diagnostic."""
    journal_status = _journal_retry_status(journal, journal_bytes)
    cause_details = exception_details(cause)
    message = (
        f"reconcile recovery failed while {operation} {format_path_for_display(path)}: "
        f"{cause_details}; {journal_status}; correct the filesystem problem and rerun "
        "'doc-lattice reconcile --recover'"
    )
    return ReconcilePersistenceError(message)


def _exact_journal_status(journal: Path, journal_bytes: bytes) -> tuple[_JournalStatus, str]:
    """Classify whether the canonical journal is a regular exact-byte copy."""
    # Every branch returns text a person reads, and the returned detail is embedded again by
    # `_journal_retry_status` and `_cleanup_journal`. The journal is displayed here, where it
    # first enters text, so those outer sinks compose already-displayed text rather than
    # wrapping a path a second time.
    displayed = format_path_for_display(journal)
    try:
        mode = journal.lstat().st_mode
    except FileNotFoundError:
        return "absent", f"journal {displayed} is not present"
    except OSError as cause:
        return "invalid", f"cannot inspect journal {displayed}: {cause}"
    if not stat.S_ISREG(mode):
        return "invalid", f"journal collision at {displayed} is not a regular file"
    try:
        current_bytes = journal.read_bytes()
    except OSError as cause:
        return "invalid", f"cannot read journal {displayed}: {cause}"
    if current_bytes != journal_bytes:
        return "invalid", f"journal collision at {displayed} contains different bytes"
    return "exact", f"journal {displayed} is an exact recovery copy"


def _journal_retry_status(journal: Path, journal_bytes: bytes) -> str:
    """Describe only journal bytes verified at the canonical path."""
    status, detail = _exact_journal_status(journal, journal_bytes)
    if status == "exact":
        return f"journal {format_path_for_display(journal)} remains for retry"
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
        f"cannot safely recover destination {format_path_for_display(entry.destination)}: it "
        "still matches the transaction after image, but before image "
        f"{format_path_for_display(entry.before_path)} is {state}; journal "
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
        f"cannot safely clean staged artifact {format_path_for_display(staged)} for "
        f"destination {format_path_for_display(destination)}: "
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
            message = (
                "recovery synchronization ancestor is not a directory: "
                f"{format_path_for_display(current)}"
            )
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
        primary.add_note(
            f"directory resync failed after unlink of {format_path_for_display(path)}: "
            f"{retry_error}"
        )
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError as retry_error:
        primary.add_note(
            f"cannot verify absence after resync of {format_path_for_display(path)}: {retry_error}"
        )
        return False
    primary.add_note(
        f"path reappeared during directory resync after unlink of {format_path_for_display(path)}"
    )
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
            prefix=_JOURNAL_STAGE_PREFIX,
        )
    except OSError as restore_error:
        primary.add_note(
            f"journal restoration failed for {format_path_for_display(journal)}: {restore_error}"
        )
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
            f"cannot safely clean reconcile journal {format_path_for_display(journal)}: "
            f"{detail}; refusing to remove "
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
    """Return every journal stage with its role-specific digest and owning destination."""
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


def _classify_destination(
    entry: _ResolvedEntry,
    journal: Path,
    journal_bytes: bytes,
) -> _DestinationState:
    """Compare one live destination against the images the transaction recorded."""
    try:
        current_sha256 = file_sha256(entry.destination)
    except FileNotFoundError:
        # Absence matches neither recorded image. Reconcile only ever rewrites an existing
        # tracked document, so a destination that vanished is a state the transaction
        # cannot account for rather than a rollback it already achieved.
        return "other"
    except OSError as cause:
        raise _recovery_operation_error(
            "fingerprinting destination",
            entry.destination,
            journal,
            journal_bytes,
            cause,
        ) from cause
    if current_sha256 == entry.after_sha256:
        return "after"
    if current_sha256 == entry.before_sha256:
        return "before"
    return "other"


def _restore_destination(
    entry: _ResolvedEntry,
    journal: Path,
    journal_bytes: bytes,
) -> None:
    """Replace one destination still holding its after image with its before image."""
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


def _rollback_prepared(
    entries: tuple[_ResolvedEntry, ...],
    journal: Path,
    journal_bytes: bytes,
    *,
    candidates: frozenset[Path] | None = None,
) -> _RollbackOutcome:
    """Restore transaction-owned after images while preserving unrelated changes.

    Args:
        entries: Validated journal entries to roll back in reverse order.
        journal: Canonical journal path serving as recovery authority.
        journal_bytes: Exact bytes of that journal.
        candidates: Destinations the caller may already have applied, or None when
            every entry is a candidate because the caller cannot know how far a
            crashed transaction progressed.

    Returns:
        The per-destination classification this rollback produced.

    Raises:
        ReconcilePersistenceError: If restoring or cleaning an entry cannot proceed safely.
    """
    restored: list[Path] = []
    already_before: list[Path] = []
    unresolved: list[Path] = []
    untouched: list[Path] = []
    for entry in reversed(entries):
        state = _classify_destination(entry, journal, journal_bytes)
        if state == "after":
            _restore_destination(entry, journal, journal_bytes)
            restored.append(entry.destination)
        elif state == "before":
            already_before.append(entry.destination)
        elif candidates is None or entry.destination in candidates:
            unresolved.append(entry.destination)
        else:
            untouched.append(entry.destination)
    outcome = _RollbackOutcome(
        restored=tuple(restored),
        already_before=tuple(already_before),
        unresolved=tuple(unresolved),
        untouched=tuple(untouched),
    )
    # An unresolved entry keeps the whole journal and every remaining stage. The journal
    # holds the destination, path, and digest associations a manual repair needs, and
    # selective cleanup would add a fallible mutation path without helping correctness.
    if not outcome.unresolved:
        _cleanup_transaction_artifacts(entries, journal, journal_bytes)
    return outcome


def _is_transaction_artifact_name(name: str) -> bool:
    """Return whether one namespace entry name is a reconcile transaction stage."""
    if not name.endswith(PERSISTENCE_TEMP_SUFFIX):
        return False
    if name.startswith(_JOURNAL_STAGE_PREFIX):
        return True
    return name.startswith(".") and (
        RECONCILE_BEFORE_IMAGE_INFIX in name or RECONCILE_AFTER_IMAGE_INFIX in name
    )


def _retained_artifacts(entries: tuple[_ResolvedEntry, ...], journal: Path) -> frozenset[Path]:
    """Return every artifact an incomplete rollback deliberately keeps as authority."""
    retained = {journal}
    for entry in entries:
        retained.add(entry.before_path)
        retained.add(entry.after_path)
    return frozenset(retained)


def _project_relative(paths: tuple[Path, ...], canonical_root: Path) -> tuple[str, ...]:
    """Render contained paths as sorted, deterministic project-relative strings."""
    return tuple(sorted(path.relative_to(canonical_root).as_posix() for path in paths))


def _scan_orphan_artifacts(
    canonical_root: Path,
    referenced: frozenset[Path],
) -> tuple[tuple[str, ...], tuple[ScanFailure, ...]]:
    """Find transaction artifacts that no retained journal accounts for.

    Args:
        canonical_root: The resolved project root to enumerate without following symlinks.
            Journal entries resolve against this same root, so orphan and retained paths
            are directly comparable.
        referenced: Artifacts a retained journal still owns, which are never orphans.

    Returns:
        Sorted project-relative orphan paths and enumeration failures, the latter ordered by
        their legacy rendered text so the JSON payload's array order is unchanged. Sorting the
        records by field would reorder the array whenever a filename and its rendered line
        disagree about order, which the byte-identity contract does not allow.
    """
    orphans: list[Path] = []
    scan_errors: list[ScanFailure] = []

    def _record_scan_error(cause: OSError) -> None:
        # str(), not an f-string: this captures structured data for an encoder to spell
        # later, and building it as text here is what the display guard exists to catch.
        scan_errors.append(ScanFailure(filename=str(cause.filename), detail=str(cause)))

    for directory, subdirectories, names in os.walk(canonical_root, onerror=_record_scan_error):
        parent = Path(directory)
        for name in (*subdirectories, *names):
            if not _is_transaction_artifact_name(name):
                continue
            candidate = parent / name
            if candidate not in referenced:
                orphans.append(candidate)
    return (
        _project_relative(tuple(orphans), canonical_root),
        tuple(sorted(scan_errors, key=lambda failure: failure.legacy_text)),
    )


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
                "durable cleanup failed for unpublished stage "
                f"{format_path_for_display(staged)}: {cleanup_error}; "
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
            message = (
                f"reconcile destination {format_path_for_display(destination)} aliases journal "
                f"path {format_path_for_display(journal_path)}"
            )
            raise ValueError(message)
        if destination in destination_indices:
            first_index = destination_indices[destination]
            message = (
                f"duplicate reconcile destination {format_path_for_display(destination)} "
                f"for rewrites {first_index} and {index}"
            )
            raise ValueError(message)
        destination_indices[destination] = index
        pending.append(_PendingRewrite(rewrite, destination, destination_relative))
    return tuple(pending)


def _prepare_transaction(
    project_root: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
    selector: JournalSelector,
) -> _PreparedTransaction:
    """Stage exact images and durably publish an ordered prepared journal."""
    journal_path = _journal_path(project_root)
    if _journal_is_present(journal_path):
        raise ReconcilePersistenceError(_journal_already_exists_message(journal_path))
    # Captured once, here, and carried through commit unchanged. Re-reading the clock or the
    # selector when the committed marker is written would let a crash journal disagree with
    # the prepared journal it replaced about when and why the transaction ran.
    provenance = JournalProvenance(
        created_at=utc_now(),
        tool_version=__version__,
        selector=selector,
    )
    staged_paths: list[Path] = []
    journal_entries: list[JournalEntry] = []
    operation = _PREPARE_VALIDATING
    operation_path = project_root
    prepared_bytes = b""
    try:
        pending_rewrites = _preflight_rewrite_destinations(
            project_root,
            journal_path,
            rewrites,
            write_paths,
        )
        operation = _PREPARE_STAGING
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
        prepared = JournalV2(
            version=RECONCILE_JOURNAL_VERSION,
            state="prepared",
            provenance=provenance,
            entries=tuple(journal_entries),
        )
        prepared_bytes = _serialize_journal(prepared)
        operation = _PREPARE_PUBLISHING_JOURNAL
        operation_path = journal_path
        atomic_create_bytes(
            journal_path,
            prepared_bytes,
            prefix=_JOURNAL_STAGE_PREFIX,
        )
    except (KeyError, OSError, RuntimeError, ValueError) as primary:
        if operation == _PREPARE_PUBLISHING_JOURNAL and isinstance(primary, OSError):
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
            message = _journal_already_exists_message(journal_path)
        else:
            message = (
                f"reconcile preparation failed while {operation} "
                f"{format_path_for_display(operation_path)}: "
                f"{exception_details(primary)}; no destination was changed"
            )
        error = ReconcilePersistenceError(message)
        copy_exception_notes(error, primary)
        raise error from primary
    loaded, entries, journal_bytes = _load_journal(project_root, journal_path)
    if loaded.provenance != provenance:
        # The published journal is the record commit copies from, so a serializer that lost
        # or rounded a provenance field would silently hand the committed marker something
        # the prepared journal never said.
        cause = ValueError("published prepared journal did not preserve its provenance")
        raise _invalid_journal_error(journal_path, cause) from cause
    return _PreparedTransaction(loaded, provenance, entries, journal_path, journal_bytes)


def _commit_operation_error(
    operation: str,
    path: Path,
    cause: OSError | ValueError,
) -> ReconcilePersistenceError:
    """Wrap one commit I/O failure with its operation and destination."""
    error = ReconcilePersistenceError(
        f"reconcile commit failed while {operation} {format_path_for_display(path)}: "
        f"{exception_details(cause)}"
    )
    copy_exception_notes(error, cause)
    return error


def _abort_prepared(
    prepared: _PreparedTransaction,
    primary: ReconcileConflictError | ReconcilePersistenceError,
    *,
    candidates: frozenset[Path],
    authenticate_all: bool = True,
) -> NoReturn:
    """Roll back a prepared transaction, preserving the primary failure.

    Args:
        prepared: The published prepared transaction to roll back.
        primary: The conflict or persistence failure that triggered the abort.
        candidates: Destinations whose replacement this process already entered, and
            which therefore may already hold the after image. Required rather than
            defaulted, because an empty set claims the run applied nothing and would let
            cleanup delete the very journal an unresolved destination still needs.
        authenticate_all: Whether to authenticate every stage before mutating.

    Raises:
        ReconcileConflictError: The primary conflict, once rollback completed in full.
        ReconcilePersistenceError: The primary persistence failure once rollback
            completed in full, or a compound diagnostic when it did not.
    """
    try:
        if authenticate_all:
            _authenticate_transaction_artifacts(
                _staged_artifacts(prepared.entries),
                prepared.journal_path,
                prepared.journal_bytes,
            )
        outcome = _rollback_prepared(
            prepared.entries,
            prepared.journal_path,
            prepared.journal_bytes,
            candidates=candidates,
        )
    except ReconcilePersistenceError as rollback_error:
        error = ReconcilePersistenceError(
            f"{primary}; rollback failed: {rollback_error}; recovery artifacts remain; "
            "run 'doc-lattice reconcile --recover'"
        )
        error.add_note(f"original commit failure: {exception_details(primary)}")
        error.add_note(f"rollback failure: {exception_details(rollback_error)}")
        raise error from primary
    if outcome.unresolved:
        listed = ", ".join(
            format_path_for_display(destination) for destination in outcome.unresolved
        )
        error = ReconcilePersistenceError(
            f"{primary}; rollback was incomplete: {listed} matched neither the "
            "transaction before image nor its after image; the journal and every remaining "
            "staged image were retained; run 'doc-lattice reconcile --recover'"
        )
        copy_exception_notes(error, primary)
        raise error from primary
    message = f"{primary}; no files were reconciled (rollback complete)"
    if isinstance(primary, ReconcileConflictError):
        error = ReconcileConflictError(message)
    else:
        error = ReconcilePersistenceError(message)
    copy_exception_notes(error, primary)
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
                prefix=_JOURNAL_STAGE_PREFIX,
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
                "reconcile commit failed while resetting prepared journal "
                f"{format_path_for_display(journal_path)}: the visible journal contains "
                "unexpected bytes"
            )
            raise ReconcilePersistenceError(message)
        try:
            atomic_replace_bytes(
                journal_path,
                prepared.journal_bytes,
                prefix=_JOURNAL_STAGE_PREFIX,
            )
        except OSError as cause:
            raise _commit_operation_error(
                "resetting prepared journal", journal_path, cause
            ) from cause
    status, detail = _exact_journal_status(journal_path, prepared.journal_bytes)
    if status != "exact":
        message = (
            "reconcile commit failed while resetting prepared journal "
            f"{format_path_for_display(journal_path)}: {detail}"
        )
        raise ReconcilePersistenceError(message)


def _abort_failed_marker(
    prepared: _PreparedTransaction,
    committed_bytes: bytes,
    primary: ReconcilePersistenceError,
    candidates: frozenset[Path],
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
        error.add_note(f"original marker failure: {exception_details(primary)}")
        error.add_note(f"journal reset failure: {exception_details(reset_error)}")
        raise error from primary
    _abort_prepared(prepared, primary, candidates=candidates)


def commit_rewrites(
    project_root: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
    *,
    selector: JournalSelector,
    lock: ReconcileLock,
) -> None:
    """Commit exact-byte reconcile rewrites as one durable transaction.

    Args:
        project_root: Configured project root containing the transaction journal.
        rewrites: Ordered fresh-read rewrites to publish.
        write_paths: Contained resolved destinations keyed by rewrite identity path.
        selector: The selection this batch was planned from, recorded in the journal so a
            crash journal says what produced it. The caller builds it, because the arguments
            it describes never reach this boundary.
        lock: Active capability protecting this project root.

    Raises:
        ReconcileConflictError: If a destination changed after rewrite validation.
        ReconcileInProgressError: If the lock capability is absent or invalid.
        ReconcilePersistenceError: If preparation or durable commit cannot complete.
    """
    with _reconcile_operation_lease(lock, project_root):
        _commit_rewrites_locked(project_root, rewrites, write_paths, selector)


def _commit_rewrites_locked(
    project_root: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
    selector: JournalSelector,
) -> None:
    """Commit rewrites while the public API holds its capability lease."""
    prepared = _prepare_transaction(project_root, rewrites, write_paths, selector)
    # Destinations this process may already have applied. An entry joins before its
    # replacement is attempted, never after: replace_staged renames first and only then
    # synchronizes the directory, so it can fail with the destination already changed.
    # Entries that never reach that call stay out, which is what keeps an ordinary
    # pre-replace conflict from being reported as an incomplete rollback.
    candidates: set[Path] = set()
    for entry in prepared.entries:
        try:
            current_sha256 = file_sha256(entry.destination)
        except OSError as cause:
            primary = _commit_operation_error(
                "fingerprinting destination", entry.destination, cause
            )
            _abort_prepared(prepared, primary, candidates=frozenset(candidates))
        if current_sha256 != entry.before_sha256:
            primary = ReconcileConflictError(
                f"reconcile destination {format_path_for_display(entry.destination)} changed "
                "after validation"
            )
            _abort_prepared(prepared, primary, candidates=frozenset(candidates))
        try:
            after_present = _authenticate_staged_artifact(
                entry.after_path,
                entry.after_sha256,
                entry.destination,
                prepared.journal_path,
                prepared.journal_bytes,
            )
        except ReconcilePersistenceError as primary:
            _abort_prepared(
                prepared,
                primary,
                authenticate_all=False,
                candidates=frozenset(candidates),
            )
        if not after_present:
            primary = ReconcilePersistenceError(
                "cannot apply reconcile destination "
                f"{format_path_for_display(entry.destination)}: staged after image "
                f"{format_path_for_display(entry.after_path)} is missing immediately before "
                "replacement"
            )
            _abort_prepared(
                prepared,
                primary,
                authenticate_all=False,
                candidates=frozenset(candidates),
            )
        candidates.add(entry.destination)
        try:
            replace_staged(entry.after_path, entry.destination)
        except (OSError, ValueError) as cause:
            primary = _commit_operation_error("replacing destination", entry.destination, cause)
            _abort_prepared(prepared, primary, candidates=frozenset(candidates))
    committed = JournalV2(
        version=RECONCILE_JOURNAL_VERSION,
        state="committed",
        provenance=prepared.provenance,
        entries=prepared.journal.entries,
    )
    committed_bytes = _serialize_journal(committed)
    try:
        atomic_replace_bytes(
            prepared.journal_path,
            committed_bytes,
            prefix=_JOURNAL_STAGE_PREFIX,
        )
    except OSError as cause:
        primary = _commit_operation_error("marking journal committed", prepared.journal_path, cause)
        _abort_failed_marker(prepared, committed_bytes, primary, frozenset(candidates))
    # _abort_failed_marker never returns, so reaching this line is AD-5's point of no return:
    # every destination is durable and the journal is durably marked committed. From here on
    # recovery only cleans staged evidence, it never rolls a destination back.
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
        # sys.exception() reports the exception propagating out of the with body, if any.
        # A lock release or close failure must never replace that exception, since it is the
        # reconcile failure the operator has to act on, so it is attached as a note instead
        # and only surfaces as its own error on an otherwise clean exit.
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
            f"reconcile journal {format_path_for_display(journal)} requires recovery; "
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
        The recovery action, project journal path, rollback classification counts, and
        any unresolved destination or orphaned artifact the caller must still act on.

    Raises:
        ReconcileInProgressError: If the lock capability is absent or invalid.
    """
    with _reconcile_operation_lease(lock, project_root):
        return _recover_transaction_locked(project_root)


def _recover_transaction_locked(project_root: Path) -> RecoveryResult:
    """Recover one journal while the public API holds its capability lease."""
    journal = _journal_path(project_root)
    canonical_root = project_root.resolve()
    # Orphan scanning runs after journal handling in every branch, not only when no journal
    # was found. An interrupted journal publication can leave both the canonical journal and
    # its helper stage, so recovering the journal first is what exposes the helper.
    if not _journal_is_present(journal):
        orphans, scan_errors = _scan_orphan_artifacts(canonical_root, frozenset())
        return RecoveryResult(
            action="none",
            journal=journal,
            orphans=orphans,
            scan_errors=scan_errors,
        )
    loaded, entries, journal_bytes = _load_journal(project_root, journal)
    _authenticate_transaction_artifacts(_staged_artifacts(entries), journal, journal_bytes)
    if loaded.state != "prepared":
        _cleanup_transaction_artifacts(entries, journal, journal_bytes)
        orphans, scan_errors = _scan_orphan_artifacts(canonical_root, frozenset())
        return RecoveryResult(
            action="cleaned_committed",
            journal=journal,
            orphans=orphans,
            scan_errors=scan_errors,
            journal_version=loaded.version,
            provenance=loaded.provenance,
        )
    outcome = _rollback_prepared(entries, journal, journal_bytes)
    retained = _retained_artifacts(entries, journal) if outcome.unresolved else frozenset()
    orphans, scan_errors = _scan_orphan_artifacts(canonical_root, retained)
    return RecoveryResult(
        action="partially_rolled_back" if outcome.unresolved else "rolled_back",
        journal=journal,
        restored=len(outcome.restored),
        already_before=len(outcome.already_before),
        unresolved=_project_relative(outcome.unresolved, canonical_root),
        orphans=orphans,
        scan_errors=scan_errors,
        journal_version=loaded.version,
        provenance=loaded.provenance,
    )
