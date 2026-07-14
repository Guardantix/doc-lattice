"""Tests for durable reconcile transaction recovery."""

import json
import os
import stat
from dataclasses import FrozenInstanceError, dataclass
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from doc_lattice import persistence, reconcile_transaction
from doc_lattice.constants import (
    PERSISTENCE_TEMP_SUFFIX,
    RECONCILE_AFTER_IMAGE_INFIX,
    RECONCILE_BEFORE_IMAGE_INFIX,
    RECONCILE_JOURNAL_NAME,
    RECONCILE_JOURNAL_VERSION,
)
from doc_lattice.error_types import (
    ProjectError,
    ReconcileConflictError,
    ReconcileInProgressError,
    ReconcilePersistenceError,
)
from doc_lattice.reconcile_transaction import (
    Journal,
    JournalEntry,
    JournalState,
    RecoveryResult,
    ensure_dry_run_safe,
    reconcile_lock,
)
from doc_lattice.reconcile_transaction import (
    recover_transaction as _recover_transaction_unlocked,
)


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    """Capture every namespace entry without following symlinks or reading special files."""
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            entry = ("symlink", os.fsencode(path.readlink()))
        elif stat.S_ISREG(mode):
            entry = ("file", path.read_bytes())
        elif stat.S_ISDIR(mode):
            entry = ("directory", b"")
        else:
            entry = ("special", b"")
        snapshot[str(path.relative_to(root))] = entry
    return snapshot


def recover_transaction(project_root: Path) -> RecoveryResult:
    """Run recovery through the required project-bound lock capability."""
    with reconcile_lock(project_root) as lock:
        return _recover_transaction_unlocked(project_root, lock=lock)


@dataclass(frozen=True)
class SyntheticTransaction:
    """Paths belonging to one synthetic recovery journal."""

    destination: Path
    before: Path
    after: Path
    journal: Path


def _write_synthetic_transaction(  # noqa: PLR0913
    root: Path,
    *,
    state: JournalState = "prepared",
    destination_bytes: bytes | None = b"after image\n",
    before_bytes: bytes = b"before image\n",
    after_bytes: bytes = b"after image\n",
    before_present: bool = True,
    after_present: bool = True,
) -> SyntheticTransaction:
    """Write a valid synthetic journal plus caller-selected current artifacts."""
    docs = root / "docs"
    docs.mkdir()
    destination = docs / "doc.md"
    before = docs / ".doc.md.doc-lattice-before.before123.tmp"
    after = docs / ".doc.md.doc-lattice-after.after123.tmp"
    journal = root / RECONCILE_JOURNAL_NAME
    if destination_bytes is not None:
        destination.write_bytes(destination_bytes)
    if before_present:
        before.write_bytes(before_bytes)
    if after_present:
        after.write_bytes(after_bytes)
    entry = JournalEntry(
        destination=destination.relative_to(root).as_posix(),
        before_path=before.relative_to(root).as_posix(),
        before_sha256=sha256(before_bytes).hexdigest(),
        after_path=after.relative_to(root).as_posix(),
        after_sha256=sha256(after_bytes).hexdigest(),
    )
    journal.write_text(
        Journal(version=RECONCILE_JOURNAL_VERSION, state=state, entries=(entry,)).model_dump_json(),
        encoding="utf-8",
    )
    return SyntheticTransaction(destination, before, after, journal)


def test_reconcile_constants_are_pinned():
    assert RECONCILE_JOURNAL_NAME == ".doc-lattice-reconcile.json"
    assert RECONCILE_JOURNAL_VERSION == 1
    assert PERSISTENCE_TEMP_SUFFIX == ".tmp"
    assert RECONCILE_BEFORE_IMAGE_INFIX == ".doc-lattice-before."
    assert RECONCILE_AFTER_IMAGE_INFIX == ".doc-lattice-after."


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (ReconcileInProgressError, "RECONCILE_IN_PROGRESS"),
        (ReconcileConflictError, "RECONCILE_CONFLICT"),
        (ReconcilePersistenceError, "RECONCILE_PERSISTENCE"),
    ],
)
def test_reconcile_errors_carry_message_and_code(factory, code):
    error = factory("transaction failed")

    assert isinstance(error, ProjectError)
    assert str(error) == "transaction failed"
    assert error.code == code


def test_second_live_reconcile_holder_is_refused(tmp_path: Path):
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal_bytes = b"sentinel journal bytes\n"
    journal.write_bytes(journal_bytes)

    with reconcile_lock(tmp_path):
        with (
            pytest.raises(ReconcileInProgressError) as caught,
            reconcile_lock(tmp_path),
        ):
            pytest.fail("nested holder unexpectedly acquired the directory lock")
        assert journal.read_bytes() == journal_bytes

    assert str(caught.value) == "another reconcile is in progress; retry after it exits"
    assert journal.read_bytes() == journal_bytes
    with reconcile_lock(tmp_path):
        assert journal.read_bytes() == journal_bytes


def test_reconcile_lock_rejects_unsupported_platform_before_opening_directory(
    tmp_path: Path, monkeypatch
):
    before = _tree_snapshot(tmp_path)
    monkeypatch.setattr(reconcile_transaction, "_LOCKING_SUPPORTED", False)
    monkeypatch.setattr(
        reconcile_transaction,
        "_open_reconcile_lock_directory",
        lambda _root: pytest.fail("unsupported platform opened the project directory"),
    )

    with pytest.raises(ReconcilePersistenceError) as caught, reconcile_lock(tmp_path):
        pytest.fail("unsupported platform acquired the reconcile lock")

    assert "reconcile locking is not supported on this platform" in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


def test_lock_unlock_failure_does_not_mask_body_exception(tmp_path: Path, monkeypatch):
    body_error = ReconcilePersistenceError("body recovery failure")
    real_close = os.close
    close_calls: list[int] = []

    def _fail_unlock(fd: int, *, release: bool) -> None:  # noqa: ARG001
        if release:
            raise OSError("injected lock release failure")

    def _observe_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    monkeypatch.setattr(reconcile_transaction, "_flock", _fail_unlock)
    monkeypatch.setattr(reconcile_transaction.os, "close", _observe_close)

    with (
        pytest.raises(ReconcilePersistenceError) as caught,
        reconcile_lock(tmp_path),
    ):
        raise body_error

    assert caught.value is body_error
    assert any(
        "lock release" in note and "injected lock release failure" in note
        for note in getattr(body_error, "__notes__", [])
    )
    assert len(close_calls) == 1


def test_lock_close_failure_does_not_mask_body_exception(tmp_path: Path, monkeypatch):
    body_error = ReconcilePersistenceError("body recovery failure")
    real_close = os.close

    def _fail_close(fd: int) -> None:
        real_close(fd)
        raise OSError("injected lock close failure")

    monkeypatch.setattr(reconcile_transaction.os, "close", _fail_close)

    with (
        pytest.raises(ReconcilePersistenceError) as caught,
        reconcile_lock(tmp_path),
    ):
        raise body_error

    assert caught.value is body_error
    assert any(
        "lock close" in note and "injected lock close failure" in note
        for note in getattr(body_error, "__notes__", [])
    )


def test_lock_unlock_failure_after_success_is_typed_and_still_closes(tmp_path: Path, monkeypatch):
    real_close = os.close
    close_calls: list[int] = []

    def _fail_unlock(fd: int, *, release: bool) -> None:  # noqa: ARG001
        if release:
            raise OSError("injected lock release failure")

    def _observe_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    monkeypatch.setattr(reconcile_transaction, "_flock", _fail_unlock)
    monkeypatch.setattr(reconcile_transaction.os, "close", _observe_close)

    with (
        pytest.raises(ReconcilePersistenceError) as caught,
        reconcile_lock(tmp_path),
    ):
        pass

    assert "lock release" in str(caught.value)
    assert "injected lock release failure" in str(caught.value)
    assert len(close_calls) == 1


def test_lock_close_failure_after_success_is_typed(tmp_path: Path, monkeypatch):
    real_close = os.close

    def _fail_close(fd: int) -> None:
        real_close(fd)
        raise OSError("injected lock close failure")

    monkeypatch.setattr(reconcile_transaction.os, "close", _fail_close)

    with (
        pytest.raises(ReconcilePersistenceError) as caught,
        reconcile_lock(tmp_path),
    ):
        pass

    assert "lock close" in str(caught.value)
    assert "injected lock close failure" in str(caught.value)


def test_lock_open_failure_is_typed_and_names_operation_and_root(tmp_path: Path, monkeypatch):
    before = _tree_snapshot(tmp_path)

    def fail_open(_path: Path, _flags: int) -> int:
        raise PermissionError("open denied")

    monkeypatch.setattr(reconcile_transaction.os, "open", fail_open)

    with pytest.raises(ReconcilePersistenceError) as caught, reconcile_lock(tmp_path):
        pytest.fail("lock unexpectedly acquired")

    message = str(caught.value)
    assert "opening project directory" in message
    assert str(tmp_path) in message
    assert "open denied" in message
    assert _tree_snapshot(tmp_path) == before


def test_lock_noncontention_flock_failure_is_typed_and_names_operation(tmp_path: Path, monkeypatch):
    before = _tree_snapshot(tmp_path)
    real_flock = reconcile_transaction._flock

    def fail_acquisition(fd: int, *, release: bool) -> None:
        if not release:
            raise OSError("flock device failure")
        real_flock(fd, release=release)

    monkeypatch.setattr(reconcile_transaction, "_flock", fail_acquisition)

    with pytest.raises(ReconcilePersistenceError) as caught, reconcile_lock(tmp_path):
        pytest.fail("lock unexpectedly acquired")

    message = str(caught.value)
    assert "acquiring reconcile lock" in message
    assert str(tmp_path) in message
    assert "flock device failure" in message
    assert _tree_snapshot(tmp_path) == before


def test_lock_fstat_failure_is_typed_and_names_operation(tmp_path: Path, monkeypatch):
    before = _tree_snapshot(tmp_path)

    def fail_fstat(_fd: int):
        raise OSError("fstat failed")

    monkeypatch.setattr(reconcile_transaction.os, "fstat", fail_fstat)

    with pytest.raises(ReconcilePersistenceError) as caught, reconcile_lock(tmp_path):
        pytest.fail("lock unexpectedly acquired")

    message = str(caught.value)
    assert "inspecting locked project directory" in message
    assert str(tmp_path) in message
    assert "fstat failed" in message
    assert _tree_snapshot(tmp_path) == before


def test_lock_setup_failure_preserves_unlock_and_close_cleanup_notes(tmp_path: Path, monkeypatch):
    fake_fd = 8123

    monkeypatch.setattr(reconcile_transaction.os, "open", lambda _path, _flags: fake_fd)

    def fail_flock(_fd: int, *, release: bool) -> None:
        if release:
            raise OSError("unlock cleanup failed")

    def fail_fstat(_fd: int):
        raise OSError("primary fstat failure")

    def fail_close(_fd: int) -> None:
        raise OSError("close cleanup failed")

    monkeypatch.setattr(reconcile_transaction, "_flock", fail_flock)
    monkeypatch.setattr(reconcile_transaction.os, "fstat", fail_fstat)
    monkeypatch.setattr(reconcile_transaction.os, "close", fail_close)

    with pytest.raises(ReconcilePersistenceError) as caught, reconcile_lock(tmp_path):
        pytest.fail("lock unexpectedly acquired")

    assert "primary fstat failure" in str(caught.value)
    notes = "; ".join(getattr(caught.value, "__notes__", ()))
    assert "unlock cleanup failed" in notes
    assert "close cleanup failed" in notes


def test_dry_run_refuses_existing_journal_without_mutation(tmp_path: Path):
    document = tmp_path / "doc.md"
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    document.write_bytes(b"document bytes\x00\xff")
    journal.write_bytes(b'{"incomplete": true}\n')
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        ensure_dry_run_safe(tmp_path)

    message = str(caught.value)
    assert str(journal) in message
    assert "run 'doc-lattice reconcile --recover' first" in message
    assert _tree_snapshot(tmp_path) == before


def test_dry_run_allows_project_without_journal(tmp_path: Path):
    ensure_dry_run_safe(tmp_path)

    assert list(tmp_path.iterdir()) == []


def _create_journal_namespace_collision(root: Path, kind: str) -> Path:
    """Create one unsafe object at the canonical reconcile journal path."""
    journal = root / RECONCILE_JOURNAL_NAME
    if kind == "dangling symlink":
        journal.symlink_to("missing-journal-target")
    elif kind == "live symlink":
        target = root / "journal-target"
        target.write_bytes(b"not canonical journal bytes\n")
        journal.symlink_to(target.name)
    elif kind == "directory":
        journal.mkdir()
    else:
        os.mkfifo(journal)
    return journal


@pytest.mark.parametrize(
    ("operation", "kind"),
    [
        ("dry-run", "dangling symlink"),
        ("dry-run", "live symlink"),
        ("dry-run", "directory"),
        ("dry-run", "FIFO"),
        ("recovery", "dangling symlink"),
        ("recovery", "live symlink"),
        ("recovery", "directory"),
    ],
)
def test_journal_namespace_collision_is_typed_and_never_followed_or_mutated(
    tmp_path: Path, operation: str, kind: str
):
    journal = _create_journal_namespace_collision(tmp_path, kind)
    before = _tree_snapshot(tmp_path)
    operation_fn = ensure_dry_run_safe if operation == "dry-run" else recover_transaction

    with pytest.raises(ReconcilePersistenceError) as caught:
        operation_fn(tmp_path)

    message = str(caught.value)
    assert str(journal) in message
    if "symlink" in kind:
        assert "symlink" in message
    else:
        assert "regular file" in message
    assert _tree_snapshot(tmp_path) == before


def test_journal_namespace_inspection_error_is_typed_and_names_path(tmp_path: Path, monkeypatch):
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal.write_bytes(b"journal evidence\n")
    real_lstat = Path.lstat

    def fail_journal_lstat(path: Path):
        if path == journal:
            raise PermissionError("inspection denied")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_journal_lstat)

    with pytest.raises(ReconcilePersistenceError) as caught:
        ensure_dry_run_safe(tmp_path)

    assert str(journal) in str(caught.value)
    assert "cannot inspect" in str(caught.value)
    assert "inspection denied" in str(caught.value)


def test_recovery_requires_a_valid_active_lock_before_mutation(tmp_path: Path):
    _write_synthetic_transaction(tmp_path)
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcileInProgressError, match="active reconcile lock"):
        reconcile_transaction.recover_transaction(
            tmp_path,
            lock=None,  # ty: ignore[invalid-argument-type] - deliberate misuse
        )

    assert _tree_snapshot(tmp_path) == before


def test_recovery_rejects_wrong_root_lock_before_mutation(tmp_path: Path):
    project_root = tmp_path / "project"
    other_root = tmp_path / "other"
    project_root.mkdir()
    other_root.mkdir()
    _write_synthetic_transaction(project_root)
    before = _tree_snapshot(project_root)

    with (
        reconcile_lock(other_root) as wrong_lock,
        pytest.raises(ReconcileInProgressError, match="different project root"),
    ):
        reconcile_transaction.recover_transaction(project_root, lock=wrong_lock)

    assert _tree_snapshot(project_root) == before


def test_recovery_rejects_replaced_root_directory_before_mutation(tmp_path: Path):
    project_root = tmp_path / "project"
    moved_root = tmp_path / "moved-project"
    project_root.mkdir()

    with reconcile_lock(project_root) as stale_lock:
        project_root.rename(moved_root)
        project_root.mkdir()
        _write_synthetic_transaction(project_root)
        before = _tree_snapshot(project_root)

        with pytest.raises(ReconcileInProgressError, match="different project root directory"):
            reconcile_transaction.recover_transaction(project_root, lock=stale_lock)

    assert _tree_snapshot(project_root) == before


def test_recovery_rejects_released_lock_before_mutation(tmp_path: Path):
    _write_synthetic_transaction(tmp_path)
    before = _tree_snapshot(tmp_path)
    with reconcile_lock(tmp_path) as released_lock:
        pass

    with pytest.raises(ReconcileInProgressError, match="no longer active"):
        reconcile_transaction.recover_transaction(tmp_path, lock=released_lock)

    assert _tree_snapshot(tmp_path) == before


def test_recovery_accepts_current_root_bound_lock(tmp_path: Path):
    transaction = _write_synthetic_transaction(tmp_path)

    with reconcile_lock(tmp_path) as lock:
        result = reconcile_transaction.recover_transaction(tmp_path, lock=lock)

    assert result.action == "rolled_back"
    assert transaction.destination.read_bytes() == b"before image\n"
    assert not transaction.journal.exists()
    assert not transaction.before.exists()
    assert not transaction.after.exists()


def test_journal_models_are_frozen():
    entry = JournalEntry(
        destination="docs/doc.md",
        before_path="docs/.doc.md.doc-lattice-before.before123.tmp",
        before_sha256="a" * 64,
        after_path="docs/.doc.md.doc-lattice-after.after123.tmp",
        after_sha256="b" * 64,
    )
    journal = Journal(version=RECONCILE_JOURNAL_VERSION, state="prepared", entries=(entry,))

    assert journal.entries == (entry,)
    with pytest.raises(ValidationError):
        entry.destination = "other.md"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", True),
        ("before_sha256", "A" * 64),
        ("after_sha256", "short"),
    ],
)
def test_journal_entry_rejects_unknown_field_or_invalid_digest(field: str, value: object):
    payload = {
        "destination": "docs/doc.md",
        "before_path": "docs/.doc.md.doc-lattice-before.before123.tmp",
        "before_sha256": "a" * 64,
        "after_path": "docs/.doc.md.doc-lattice-after.after123.tmp",
        "after_sha256": "b" * 64,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        JournalEntry.model_validate(payload)


def test_journal_rejects_unknown_state():
    with pytest.raises(ValidationError):
        Journal.model_validate({"version": 1, "state": "unknown", "entries": []})


def test_recovery_result_is_frozen_and_slotted(tmp_path: Path):
    result = RecoveryResult(action="none", journal=tmp_path / RECONCILE_JOURNAL_NAME)

    assert result.__slots__ == ("action", "journal")
    with pytest.raises(FrozenInstanceError):
        result.action = "rolled_back"  # ty: ignore[invalid-assignment]


def test_recovery_without_journal_returns_none_without_writes(tmp_path: Path):
    document = tmp_path / "doc.md"
    document.write_bytes(b"unchanged")
    before = _tree_snapshot(tmp_path)

    result = recover_transaction(tmp_path)

    assert result == RecoveryResult(
        action="none",
        journal=tmp_path / RECONCILE_JOURNAL_NAME,
    )
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("journal_bytes", "cause"),
    [
        (b'{"version":', "Invalid JSON"),
        (b"\xff\xfe", "utf-8"),
    ],
)
def test_malformed_journal_is_rejected_with_evidence_and_remediation(
    tmp_path: Path, journal_bytes: bytes, cause: str
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    transaction.journal.write_bytes(journal_bytes)
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    message = str(caught.value)
    assert str(transaction.journal) in message
    assert cause.lower() in message.lower()
    assert "inspect" in message
    assert "destinations" in message
    assert "staged files" in message
    assert "move the invalid journal aside only after manual" in message
    assert "rerun 'doc-lattice reconcile --recover'" in message
    assert caught.value.__cause__ is not None
    assert _tree_snapshot(tmp_path) == before


def test_unsupported_journal_version_is_rejected_without_cleanup(tmp_path: Path):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["version"] = RECONCILE_JOURNAL_VERSION + 1
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert f"unsupported version {RECONCILE_JOURNAL_VERSION + 1}" in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("invalid_version", [True, 1.0, "1"])
def test_journal_version_rejects_non_integer_json_types(tmp_path: Path, invalid_version: object):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["version"] = invalid_version
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError):
        recover_transaction(tmp_path)

    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "unsafe_path",
    ["../escape.md", str(Path("/") / "tmp" / "absolute-escape.md")],
)
def test_unsafe_relative_or_absolute_journal_path_is_rejected(tmp_path: Path, unsafe_path: str):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["before_path"] = unsafe_path
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "before_path" in str(caught.value)
    assert unsafe_path in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


def test_symlink_escape_in_journal_path_is_rejected(tmp_path: Path):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    escaped = outside / "before.tmp"
    escaped.write_bytes(b"outside evidence")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["before_path"] = "escape/before.tmp"
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    journal_bytes = transaction.journal.read_bytes()

    with pytest.raises(ReconcilePersistenceError, match="outside"):
        recover_transaction(tmp_path)

    assert transaction.journal.read_bytes() == journal_bytes
    assert escaped.read_bytes() == b"outside evidence"
    assert transaction.destination.read_bytes() == b"after image\n"


@pytest.mark.parametrize("protected_relative", ["README.md", ".git/HEAD"])
def test_journal_artifact_cannot_name_protected_project_file(
    tmp_path: Path, protected_relative: str
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    protected = tmp_path / protected_relative
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_bytes(b"protected project bytes\n")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["before_path"] = protected_relative
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "invalid reconcile journal" in str(caught.value)
    assert str(protected) in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "invalid_relative",
    [
        "other/.doc.md.doc-lattice-before.token.tmp",
        "docs/.doc.md.wrong-before.token.tmp",
        "docs/.doc.md.doc-lattice-before.token.bad",
        "docs/.doc.md.doc-lattice-before..tmp",
    ],
)
def test_journal_artifact_requires_destination_directory_and_exact_role_name(
    tmp_path: Path, invalid_relative: str
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    invalid = tmp_path / invalid_relative
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(b"before image\n")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["before_path"] = invalid_relative
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "invalid reconcile journal" in str(caught.value)
    assert invalid_relative in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


def test_existing_journal_artifact_symlink_is_rejected_without_mutation(tmp_path: Path):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    target = transaction.destination.parent / "symlink-target.bin"
    target.write_bytes(b"before image\n")
    symlink = transaction.destination.parent / ".doc.md.doc-lattice-before.symlink123.tmp"
    symlink.symlink_to(target.name)
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["before_path"] = symlink.relative_to(tmp_path).as_posix()
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    journal_bytes = transaction.journal.read_bytes()

    with pytest.raises(ReconcilePersistenceError, match="symlink"):
        recover_transaction(tmp_path)

    assert symlink.is_symlink()
    assert target.read_bytes() == b"before image\n"
    assert transaction.journal.read_bytes() == journal_bytes


def test_self_referential_artifact_symlink_is_typed_invalid_journal(
    tmp_path: Path,
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    loop = transaction.destination.parent / ".doc.md.doc-lattice-before.loop123.tmp"
    loop.symlink_to(loop.name)
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["before_path"] = loop.relative_to(tmp_path).as_posix()
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    journal_bytes = transaction.journal.read_bytes()

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "invalid reconcile journal" in str(caught.value)
    assert "symlink" in str(caught.value).lower()
    assert loop.is_symlink()
    assert transaction.journal.read_bytes() == journal_bytes


def test_cleanup_rejects_artifact_replaced_by_symlink_during_authentication(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    target = transaction.destination.parent / "substitution-target.bin"
    target.write_bytes(b"before image\n")
    journal_bytes = transaction.journal.read_bytes()
    real_file_sha256 = reconcile_transaction.file_sha256
    before_reads = 0

    def _substitute_on_delete_check(path: Path) -> str:
        nonlocal before_reads
        if path == transaction.before:
            before_reads += 1
            if before_reads == 2:
                path.unlink()
                path.symlink_to(target.name)
                return real_file_sha256(target)
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _substitute_on_delete_check)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "artifact" in str(caught.value)
    assert "manual" in str(caught.value)
    assert transaction.before.is_symlink()
    assert target.read_bytes() == b"before image\n"
    assert transaction.journal.read_bytes() == journal_bytes


@pytest.mark.parametrize("nonregular_kind", ["directory", "fifo"])
def test_existing_nonregular_journal_artifact_is_rejected_without_mutation(
    tmp_path: Path, nonregular_kind: str
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    nonregular = transaction.destination.parent / ".doc.md.doc-lattice-before.nonregular123.tmp"
    if nonregular_kind == "directory":
        nonregular.mkdir()
    else:
        os.mkfifo(nonregular)
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["before_path"] = nonregular.relative_to(tmp_path).as_posix()
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    journal_bytes = transaction.journal.read_bytes()

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "invalid reconcile journal" in str(caught.value)
    assert "nonregular" in str(caught.value)
    assert nonregular.exists()
    assert transaction.journal.read_bytes() == journal_bytes


@pytest.mark.parametrize("artifact_field", ["before_path", "after_path"])
def test_committed_journal_artifact_cannot_alias_destination(tmp_path: Path, artifact_field: str):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0][artifact_field] = payload["entries"][0]["destination"]
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "alias" in str(caught.value)
    assert "move the invalid journal aside only after manual" in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("role", ["destination", "before_path", "after_path"])
def test_journal_path_cannot_alias_destination_or_artifact(tmp_path: Path, role: str):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0][role] = RECONCILE_JOURNAL_NAME
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert role in str(caught.value)
    assert "journal path" in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("duplicate_role", ["destination", "artifact"])
def test_paths_cannot_alias_across_journal_entries(tmp_path: Path, duplicate_role: str):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    first = payload["entries"][0]
    second = dict(first)
    second_destination = tmp_path / "docs" / "second.md"
    second_before = tmp_path / "docs" / ".second.md.doc-lattice-before.before456.tmp"
    second_after = tmp_path / "docs" / ".second.md.doc-lattice-after.after456.tmp"
    second_destination.write_bytes(b"second destination\n")
    second_before.write_bytes(b"second before\n")
    second_after.write_bytes(b"second after\n")
    second["destination"] = second_destination.relative_to(tmp_path).as_posix()
    second["before_path"] = second_before.relative_to(tmp_path).as_posix()
    second["before_sha256"] = sha256(second_before.read_bytes()).hexdigest()
    second["after_path"] = second_after.relative_to(tmp_path).as_posix()
    second["after_sha256"] = sha256(second_after.read_bytes()).hexdigest()
    if duplicate_role == "destination":
        second["destination"] = first["destination"]
    else:
        second["before_path"] = first["after_path"]
    payload["entries"].append(second)
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert duplicate_role in str(caught.value)
    assert "alias" in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


def test_prepared_after_image_is_rolled_back_and_artifacts_are_cleaned(tmp_path: Path):
    transaction = _write_synthetic_transaction(tmp_path)

    result = recover_transaction(tmp_path)

    assert result == RecoveryResult(action="rolled_back", journal=transaction.journal)
    assert transaction.destination.read_bytes() == b"before image\n"
    assert not transaction.before.exists()
    assert not transaction.after.exists()
    assert not transaction.journal.exists()


def test_prepared_destination_already_at_before_image_is_left_unchanged(tmp_path: Path):
    transaction = _write_synthetic_transaction(tmp_path, destination_bytes=b"before image\n")

    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    assert transaction.destination.read_bytes() == b"before image\n"
    assert not transaction.before.exists()
    assert not transaction.after.exists()
    assert not transaction.journal.exists()


@pytest.mark.parametrize("destination_bytes", [b"unrelated editor change\n", None])
def test_prepared_unrelated_edit_or_deletion_is_preserved_while_artifacts_are_cleaned(
    tmp_path: Path, destination_bytes: bytes | None
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        destination_bytes=destination_bytes,
    )

    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    if destination_bytes is None:
        assert not transaction.destination.exists()
    else:
        assert transaction.destination.read_bytes() == destination_bytes
    assert not transaction.before.exists()
    assert not transaction.after.exists()
    assert not transaction.journal.exists()


def test_committed_recovery_never_reads_or_changes_destination(tmp_path: Path, monkeypatch):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        destination_bytes=b"newer unrelated bytes\n",
    )
    real_file_sha256 = reconcile_transaction.file_sha256

    def _unexpected_digest(path: Path) -> str:
        if path == transaction.destination:
            pytest.fail(f"committed recovery unexpectedly read destination {path}")
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _unexpected_digest)

    result = recover_transaction(tmp_path)

    assert result == RecoveryResult(action="cleaned_committed", journal=transaction.journal)
    assert transaction.destination.read_bytes() == b"newer unrelated bytes\n"
    assert not transaction.before.exists()
    assert not transaction.after.exists()
    assert not transaction.journal.exists()


@pytest.mark.parametrize("state", ["prepared", "committed"])
@pytest.mark.parametrize("role", ["before", "after"])
def test_cleanup_rejects_correctly_named_artifact_with_wrong_digest(
    tmp_path: Path, state: JournalState, role: str
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state=state,
        destination_bytes=b"before image\n" if state == "prepared" else b"after image\n",
    )
    forged = getattr(transaction, role)
    forged.write_bytes(f"forged {role} artifact\n".encode())
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    message = str(caught.value)
    assert str(forged) in message
    assert "digest mismatch" in message
    assert "manual" in message
    assert "rerun 'doc-lattice reconcile --recover'" in message
    assert _tree_snapshot(tmp_path) == before


def test_prepared_recovery_authenticates_all_artifacts_before_rollback_mutation(
    tmp_path: Path,
):
    transaction = _write_synthetic_transaction(tmp_path, state="prepared")
    transaction.after.write_bytes(b"forged after artifact\n")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    message = str(caught.value)
    assert str(transaction.destination) in message
    assert str(transaction.after) in message
    assert "digest mismatch" in message
    assert "manual" in message
    assert _tree_snapshot(tmp_path) == before


def test_prepared_rollback_rejects_before_symlink_substitution_before_replace(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(tmp_path, state="prepared")
    target = transaction.destination.parent / "rollback-substitution-target.bin"
    target.write_bytes(b"before image\n")
    journal_bytes = transaction.journal.read_bytes()
    real_file_sha256 = reconcile_transaction.file_sha256
    before_reads = 0

    def _substitute_on_rollback_check(path: Path) -> str:
        nonlocal before_reads
        if path == transaction.before:
            before_reads += 1
            if before_reads == 2:
                path.unlink()
                path.symlink_to(target.name)
                return real_file_sha256(target)
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _substitute_on_rollback_check)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "artifact changed" in str(caught.value)
    assert transaction.destination.read_bytes() == b"after image\n"
    assert not transaction.destination.is_symlink()
    assert transaction.before.is_symlink()
    assert target.read_bytes() == b"before image\n"
    assert transaction.journal.read_bytes() == journal_bytes


def test_cleanup_preserves_unreadable_artifact_and_journal(tmp_path: Path, monkeypatch):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    before = _tree_snapshot(tmp_path)
    real_file_sha256 = reconcile_transaction.file_sha256

    def _fail_artifact_read(path: Path) -> str:
        if path == transaction.before:
            raise OSError("injected artifact read failure")
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _fail_artifact_read)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    message = str(caught.value)
    assert str(transaction.before) in message
    assert "injected artifact read failure" in message
    assert "manual" in message
    assert _tree_snapshot(tmp_path) == before


def test_committed_recovery_allows_both_staged_artifacts_to_be_absent(tmp_path: Path):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        before_present=False,
        after_present=False,
    )

    result = recover_transaction(tmp_path)

    assert result.action == "cleaned_committed"
    assert transaction.destination.read_bytes() == b"after image\n"
    assert not transaction.journal.exists()


def test_absent_artifact_is_never_passed_to_unlink(tmp_path: Path, monkeypatch):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        before_present=False,
        after_present=False,
    )
    real_unlink = reconcile_transaction.durable_unlink
    injected_paths: list[Path] = []

    def _inject_before_unlink(path: Path) -> None:
        if path in (transaction.before, transaction.after) and not path.exists():
            injected_paths.append(path)
            path.write_bytes(b"forged race artifact\n")
        real_unlink(path)

    monkeypatch.setattr(reconcile_transaction, "durable_unlink", _inject_before_unlink)

    result = recover_transaction(tmp_path)

    assert result.action == "cleaned_committed"
    assert injected_paths == []
    assert not transaction.journal.exists()


@pytest.mark.parametrize("destination_bytes", [b"before image\n", b"unrelated edit\n"])
def test_prepared_recovery_allows_unneeded_staged_artifacts_to_be_absent(
    tmp_path: Path, destination_bytes: bytes
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        destination_bytes=destination_bytes,
        before_present=False,
        after_present=False,
    )

    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    assert transaction.destination.read_bytes() == destination_bytes
    assert not transaction.journal.exists()


def test_repeated_recovery_is_safe(tmp_path: Path):
    transaction = _write_synthetic_transaction(tmp_path)

    first = recover_transaction(tmp_path)
    second = recover_transaction(tmp_path)

    assert first.action == "rolled_back"
    assert second == RecoveryResult(action="none", journal=transaction.journal)
    assert transaction.destination.read_bytes() == b"before image\n"


@pytest.mark.parametrize("before_state", ["missing", "corrupt"])
def test_required_before_image_missing_or_corrupt_preserves_recovery_evidence(
    tmp_path: Path, before_state: str
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        before_present=before_state != "missing",
    )
    if before_state == "corrupt":
        transaction.before.write_bytes(b"corrupt before image\n")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    message = str(caught.value)
    assert str(transaction.destination) in message
    assert str(transaction.before) in message
    assert before_state in message
    assert "rerun 'doc-lattice reconcile --recover'" in message
    assert _tree_snapshot(tmp_path) == before


def test_unsafe_recovery_does_not_claim_an_externally_removed_journal_remains(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(tmp_path, before_present=False)
    real_file_sha256 = reconcile_transaction.file_sha256

    def _remove_journal_before_digest(path: Path) -> str:
        if path == transaction.destination:
            transaction.journal.unlink()
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _remove_journal_before_digest)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert f"journal {transaction.journal} remains" not in str(caught.value)
    assert f"journal {transaction.journal} is not present" in str(caught.value)


def test_prepared_rollback_processes_destinations_in_reverse_order(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    entries: list[JournalEntry] = []
    destinations: list[Path] = []
    for name in ("first.md", "second.md"):
        destination = docs / name
        before = docs / f".{name}.doc-lattice-before.before789.tmp"
        after = docs / f".{name}.doc-lattice-after.after789.tmp"
        before_bytes = f"before {name}\n".encode()
        after_bytes = f"after {name}\n".encode()
        destination.write_bytes(after_bytes)
        before.write_bytes(before_bytes)
        after.write_bytes(after_bytes)
        destinations.append(destination)
        entries.append(
            JournalEntry(
                destination=destination.relative_to(tmp_path).as_posix(),
                before_path=before.relative_to(tmp_path).as_posix(),
                before_sha256=sha256(before_bytes).hexdigest(),
                after_path=after.relative_to(tmp_path).as_posix(),
                after_sha256=sha256(after_bytes).hexdigest(),
            )
        )
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal.write_text(
        Journal(
            version=RECONCILE_JOURNAL_VERSION, state="prepared", entries=tuple(entries)
        ).model_dump_json(),
        encoding="utf-8",
    )
    real_replace = reconcile_transaction.replace_staged
    replacement_order: list[Path] = []

    def _observe_replace(staged: Path, destination: Path) -> None:
        replacement_order.append(destination)
        real_replace(staged, destination)

    monkeypatch.setattr(reconcile_transaction, "replace_staged", _observe_replace)

    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    assert replacement_order == list(reversed(destinations))


def test_replace_failure_keeps_journal_and_can_be_retried(tmp_path: Path, monkeypatch):
    transaction = _write_synthetic_transaction(tmp_path)
    before = _tree_snapshot(tmp_path)
    real_replace = reconcile_transaction.replace_staged

    def _fail_replace(staged: Path, destination: Path) -> None:  # noqa: ARG001
        raise OSError("injected replace failure")

    monkeypatch.setattr(reconcile_transaction, "replace_staged", _fail_replace)

    with pytest.raises(ReconcilePersistenceError, match="injected replace failure") as caught:
        recover_transaction(tmp_path)

    assert str(transaction.destination) in str(caught.value)
    assert "rerun 'doc-lattice reconcile --recover'" in str(caught.value)
    assert _tree_snapshot(tmp_path) == before

    monkeypatch.setattr(reconcile_transaction, "replace_staged", real_replace)
    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    assert transaction.destination.read_bytes() == b"before image\n"
    assert not transaction.journal.exists()


def test_cleanup_failure_after_restore_keeps_journal_for_idempotent_retry(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(tmp_path)
    real_unlink = reconcile_transaction.durable_unlink

    def _fail_after_cleanup(path: Path) -> None:
        if path == transaction.after:
            raise OSError("injected cleanup failure")
        real_unlink(path)

    monkeypatch.setattr(reconcile_transaction, "durable_unlink", _fail_after_cleanup)

    with pytest.raises(ReconcilePersistenceError, match="injected cleanup failure") as caught:
        recover_transaction(tmp_path)

    assert str(transaction.after) in str(caught.value)
    assert transaction.destination.read_bytes() == b"before image\n"
    assert not transaction.before.exists()
    assert transaction.after.exists()
    assert transaction.journal.exists()

    monkeypatch.setattr(reconcile_transaction, "durable_unlink", real_unlink)
    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    assert transaction.destination.read_bytes() == b"before image\n"
    assert not transaction.after.exists()
    assert not transaction.journal.exists()


def test_one_shot_post_unlink_stage_sync_failure_is_healed(tmp_path: Path, monkeypatch):
    transaction = _write_synthetic_transaction(
        tmp_path,
        destination_bytes=b"before image\n",
    )
    real_sync = persistence.sync_directory
    sync_calls = 0

    def _fail_first_sync(path: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise OSError("one-shot stage cleanup sync failure")
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_first_sync)

    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    assert sync_calls >= 3
    assert transaction.destination.read_bytes() == b"before image\n"
    assert not transaction.before.exists()
    assert not transaction.after.exists()
    assert not transaction.journal.exists()


def test_persistent_post_unlink_stage_sync_failure_preserves_journal_for_retry(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        destination_bytes=b"before image\n",
    )
    journal_bytes = transaction.journal.read_bytes()
    real_sync = persistence.sync_directory
    durable_sync_calls: list[Path] = []
    retry_sync_calls: list[Path] = []

    def _fail_durable_sync(path: Path) -> None:
        durable_sync_calls.append(path)
        raise OSError("persistent stage cleanup sync failure")

    def _fail_retry_sync(path: Path) -> None:
        retry_sync_calls.append(path)
        raise OSError("persistent stage cleanup resync failure")

    monkeypatch.setattr(persistence, "sync_directory", _fail_durable_sync)
    monkeypatch.setattr(
        reconcile_transaction,
        "sync_directory",
        _fail_retry_sync,
        raising=False,
    )

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "persistent stage cleanup sync failure" in str(caught.value)
    assert durable_sync_calls == [transaction.before.parent]
    assert retry_sync_calls == [transaction.before.parent]
    assert not transaction.before.exists()
    assert transaction.after.exists()
    assert transaction.journal.is_file()
    assert transaction.journal.read_bytes() == journal_bytes

    monkeypatch.setattr(persistence, "sync_directory", real_sync)
    monkeypatch.setattr(reconcile_transaction, "sync_directory", real_sync)
    result = recover_transaction(tmp_path)

    assert result.action == "rolled_back"
    assert not transaction.after.exists()
    assert not transaction.journal.exists()


def test_retry_syncs_absent_isolated_stage_parent_before_removing_journal(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    isolated = tmp_path / "isolated-stage"
    isolated.mkdir()
    isolated_destination = isolated / "doc.md"
    failed_unlink_artifact = isolated / ".doc.md.doc-lattice-before.failed123.tmp"
    other_absent_artifact = isolated / ".doc.md.doc-lattice-after.absent123.tmp"
    failed_unlink_artifact.write_bytes(b"before image\n")
    transaction.destination.unlink()
    transaction.before.unlink()
    transaction.after.unlink()
    payload = json.loads(transaction.journal.read_text(encoding="utf-8"))
    payload["entries"][0]["destination"] = isolated_destination.relative_to(tmp_path).as_posix()
    payload["entries"][0]["before_path"] = failed_unlink_artifact.relative_to(tmp_path).as_posix()
    payload["entries"][0]["after_path"] = other_absent_artifact.relative_to(tmp_path).as_posix()
    transaction.journal.write_text(json.dumps(payload), encoding="utf-8")
    journal_bytes = transaction.journal.read_bytes()
    real_sync = persistence.sync_directory
    original_sync_calls: list[Path] = []
    immediate_resync_calls: list[Path] = []

    def _fail_original_sync(path: Path) -> None:
        original_sync_calls.append(path)
        raise OSError("original isolated stage sync failure")

    def _fail_immediate_resync(path: Path) -> None:
        immediate_resync_calls.append(path)
        raise OSError("immediate isolated stage resync failure")

    monkeypatch.setattr(persistence, "sync_directory", _fail_original_sync)
    monkeypatch.setattr(reconcile_transaction, "sync_directory", _fail_immediate_resync)

    with pytest.raises(ReconcilePersistenceError):
        recover_transaction(tmp_path)

    assert original_sync_calls == [isolated]
    assert immediate_resync_calls == [isolated]
    assert not failed_unlink_artifact.exists()
    assert list(isolated.iterdir()) == []
    assert transaction.journal.is_file()
    assert transaction.journal.read_bytes() == journal_bytes

    sync_order: list[Path] = []

    def _record_sync(path: Path) -> None:
        sync_order.append(path)
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _record_sync)
    monkeypatch.setattr(reconcile_transaction, "sync_directory", _record_sync)

    result = recover_transaction(tmp_path)

    assert result.action == "cleaned_committed"
    assert sync_order == [isolated, isolated, tmp_path]
    assert not transaction.journal.exists()


@pytest.mark.parametrize(
    ("state", "expected_action"),
    [("prepared", "rolled_back"), ("committed", "cleaned_committed")],
)
def test_absent_artifacts_and_parent_sync_existing_ancestor_before_journal_removal(
    tmp_path: Path,
    monkeypatch,
    state: JournalState,
    expected_action: str,
):
    transaction = _write_synthetic_transaction(tmp_path, state=state)
    for path in (transaction.destination, transaction.before, transaction.after):
        path.unlink()
    transaction.destination.parent.rmdir()
    real_sync = reconcile_transaction.sync_directory
    real_unlink = reconcile_transaction.durable_unlink
    events: list[tuple[str, Path]] = []

    def _record_sync(path: Path) -> None:
        events.append(("sync", path))
        real_sync(path)

    def _observe_unlink(path: Path) -> None:
        if path == transaction.journal:
            events.append(("journal_unlink", path))
        real_unlink(path)

    monkeypatch.setattr(reconcile_transaction, "sync_directory", _record_sync)
    monkeypatch.setattr(reconcile_transaction, "durable_unlink", _observe_unlink)

    result = recover_transaction(tmp_path)

    assert result.action == expected_action
    assert events[:3] == [
        ("sync", tmp_path),
        ("sync", tmp_path),
        ("journal_unlink", transaction.journal),
    ]
    assert not transaction.destination.parent.exists()
    assert not transaction.journal.exists()


def test_missing_artifact_parent_replaced_by_symlink_is_not_synchronized(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    for path in (transaction.destination, transaction.before, transaction.after):
        path.unlink()
    artifact_parent = transaction.destination.parent
    artifact_parent.rmdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-sync-target"
    outside.mkdir()
    journal_bytes = transaction.journal.read_bytes()
    real_sync = reconcile_transaction.sync_directory

    def _substitute_parent_during_absence_sync(path: Path) -> None:
        if path == tmp_path and not artifact_parent.is_symlink():
            artifact_parent.symlink_to(outside, target_is_directory=True)
        real_sync(path)

    monkeypatch.setattr(
        reconcile_transaction, "sync_directory", _substitute_parent_during_absence_sync
    )

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "not a directory" in str(caught.value)
    assert artifact_parent.is_symlink()
    assert list(outside.iterdir()) == []
    assert transaction.journal.read_bytes() == journal_bytes


def test_one_shot_post_unlink_journal_sync_failure_is_healed(tmp_path: Path, monkeypatch):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        before_present=False,
        after_present=False,
    )
    real_sync = persistence.sync_directory
    sync_calls = 0

    def _fail_first_sync(path: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise OSError("one-shot journal cleanup sync failure")
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_first_sync)

    result = recover_transaction(tmp_path)

    assert result.action == "cleaned_committed"
    assert sync_calls == 1
    assert transaction.destination.read_bytes() == b"after image\n"
    assert not transaction.journal.exists()


def test_journal_replaced_after_stage_cleanup_is_preserved_and_reported(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(tmp_path, state="committed")
    collision_bytes = b"intervening recovery authority\n"
    real_cleanup_journal = reconcile_transaction._cleanup_journal

    def _replace_before_journal_cleanup(journal: Path, journal_bytes: bytes) -> None:
        assert not transaction.before.exists()
        assert not transaction.after.exists()
        journal.write_bytes(collision_bytes)
        real_cleanup_journal(journal, journal_bytes)

    monkeypatch.setattr(
        reconcile_transaction,
        "_cleanup_journal",
        _replace_before_journal_cleanup,
    )

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "journal collision" in str(caught.value)
    assert "refusing to remove" in str(caught.value)
    assert transaction.journal.read_bytes() == collision_bytes
    assert transaction.destination.read_bytes() == b"after image\n"


def test_persistent_post_unlink_journal_sync_failure_restores_exact_journal_for_retry(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        before_present=False,
        after_present=False,
    )
    journal_bytes = b" \r\n" + transaction.journal.read_bytes() + b"\r\n "
    transaction.journal.write_bytes(journal_bytes)
    real_sync = persistence.sync_directory
    root_sync_calls = 0

    def _fail_twice_then_sync(path: Path) -> None:
        nonlocal root_sync_calls
        if path == tmp_path:
            root_sync_calls += 1
        if path == tmp_path and root_sync_calls <= 2:
            raise OSError(f"persistent journal cleanup sync failure {root_sync_calls}")
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_twice_then_sync)
    monkeypatch.setattr(
        reconcile_transaction,
        "sync_directory",
        _fail_twice_then_sync,
        raising=False,
    )

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "persistent journal cleanup sync failure 1" in str(caught.value)
    assert root_sync_calls >= 5
    assert transaction.journal.is_file()
    assert transaction.journal.read_bytes() == journal_bytes
    assert "remains for retry" in str(caught.value)
    primary = caught.value.__cause__
    assert isinstance(primary, OSError)
    assert str(primary) == "persistent journal cleanup sync failure 1"
    assert any("resync" in note for note in getattr(primary, "__notes__", []))

    monkeypatch.setattr(persistence, "sync_directory", real_sync)
    monkeypatch.setattr(reconcile_transaction, "sync_directory", real_sync)
    result = recover_transaction(tmp_path)

    assert result.action == "cleaned_committed"
    assert not transaction.journal.exists()
    assert list(tmp_path.glob(f"{RECONCILE_JOURNAL_NAME}.*.tmp")) == []


def test_journal_restoration_collision_is_preserved_and_reported(tmp_path: Path, monkeypatch):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        before_present=False,
        after_present=False,
    )
    collision_bytes = b"external journal collision\n"
    real_sync = persistence.sync_directory

    def _fail_original_journal_sync(path: Path) -> None:
        if path == tmp_path:
            raise OSError("original journal cleanup sync failure")
        real_sync(path)

    def _create_collision_then_fail_resync(path: Path) -> None:
        if path == tmp_path:
            transaction.journal.write_bytes(collision_bytes)
            raise OSError("secondary journal resync failure")
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_original_journal_sync)
    monkeypatch.setattr(reconcile_transaction, "sync_directory", _create_collision_then_fail_resync)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    message = str(caught.value)
    assert "exact recovery journal could not be restored" in message
    assert "collision" in message
    assert "secondary journal resync failure" in message
    assert "remains for retry" not in message
    assert transaction.journal.read_bytes() == collision_bytes


def test_journal_collision_created_during_successful_resync_is_not_accepted(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        before_present=False,
        after_present=False,
    )
    collision_bytes = b"resync-time journal collision\n"
    real_sync = persistence.sync_directory

    def _fail_original_journal_sync(path: Path) -> None:
        if path == tmp_path:
            raise OSError("original journal cleanup sync failure")
        real_sync(path)

    def _create_collision_during_resync(path: Path) -> None:
        if path == tmp_path:
            transaction.journal.write_bytes(collision_bytes)
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_original_journal_sync)
    monkeypatch.setattr(reconcile_transaction, "sync_directory", _create_collision_during_resync)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    assert "exact recovery journal could not be restored" in str(caught.value)
    assert "collision" in str(caught.value)
    assert "remains for retry" not in str(caught.value)
    assert transaction.journal.read_bytes() == collision_bytes


def test_journal_restoration_read_failure_is_reported_without_overwrite(
    tmp_path: Path, monkeypatch
):
    transaction = _write_synthetic_transaction(
        tmp_path,
        state="committed",
        before_present=False,
        after_present=False,
    )
    journal_bytes = transaction.journal.read_bytes()
    real_read_bytes = Path.read_bytes
    real_sync = persistence.sync_directory
    fail_restoration_read = False

    def _fail_original_journal_sync(path: Path) -> None:
        if path == tmp_path:
            raise OSError("original journal cleanup sync failure")
        real_sync(path)

    def _recreate_then_fail_resync(path: Path) -> None:
        nonlocal fail_restoration_read
        if path == tmp_path:
            transaction.journal.write_bytes(journal_bytes)
            fail_restoration_read = True
            raise OSError("secondary journal resync failure")
        real_sync(path)

    def _fail_restoration_read(path: Path) -> bytes:
        if path == transaction.journal and fail_restoration_read:
            raise OSError("injected journal restoration read failure")
        return real_read_bytes(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_original_journal_sync)
    monkeypatch.setattr(reconcile_transaction, "sync_directory", _recreate_then_fail_resync)
    monkeypatch.setattr(Path, "read_bytes", _fail_restoration_read)

    with pytest.raises(ReconcilePersistenceError) as caught:
        recover_transaction(tmp_path)

    message = str(caught.value)
    assert "exact recovery journal could not be restored" in message
    assert "injected journal restoration read failure" in message
    assert "secondary journal resync failure" in message
    assert "remains for retry" not in message
    assert real_read_bytes(transaction.journal) == journal_bytes
