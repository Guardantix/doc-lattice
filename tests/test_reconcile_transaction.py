"""Tests for durable reconcile transaction recovery."""

import json
from dataclasses import FrozenInstanceError, dataclass
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from doc_lattice import reconcile_transaction
from doc_lattice.constants import RECONCILE_JOURNAL_NAME, RECONCILE_JOURNAL_VERSION
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
    recover_transaction,
)


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    """Capture relative file names and exact bytes under a test root."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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
    before = docs / ".doc.md.before.tmp"
    after = docs / ".doc.md.after.tmp"
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
    with (
        reconcile_lock(tmp_path),
        pytest.raises(ReconcileInProgressError) as caught,
        reconcile_lock(tmp_path),
    ):
        pytest.fail("nested holder unexpectedly acquired the directory lock")

    assert str(caught.value) == "another reconcile is in progress; retry after it exits"
    assert list(tmp_path.iterdir()) == []


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


def test_journal_models_are_frozen_and_reject_unknown_or_invalid_fields():
    entry = JournalEntry(
        destination="docs/doc.md",
        before_path="docs/.doc.md.before.tmp",
        before_sha256="a" * 64,
        after_path="docs/.doc.md.after.tmp",
        after_sha256="b" * 64,
    )
    journal = Journal(version=RECONCILE_JOURNAL_VERSION, state="prepared", entries=(entry,))

    assert journal.entries == (entry,)
    with pytest.raises(ValidationError):
        entry.destination = "other.md"
    with pytest.raises(ValidationError):
        JournalEntry.model_validate(
            {
                "destination": "docs/doc.md",
                "before_path": "docs/.doc.md.before.tmp",
                "before_sha256": "A" * 64,
                "after_path": "docs/.doc.md.after.tmp",
                "after_sha256": "short",
                "unexpected": True,
            }
        )
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

    def _unexpected_digest(path: Path) -> str:
        pytest.fail(f"committed recovery unexpectedly read destination {path}")

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _unexpected_digest)

    result = recover_transaction(tmp_path)

    assert result == RecoveryResult(action="cleaned_committed", journal=transaction.journal)
    assert transaction.destination.read_bytes() == b"newer unrelated bytes\n"
    assert not transaction.before.exists()
    assert not transaction.after.exists()
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


def test_prepared_rollback_processes_destinations_in_reverse_order(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    entries: list[JournalEntry] = []
    destinations: list[Path] = []
    for name in ("first.md", "second.md"):
        destination = docs / name
        before = docs / f".{name}.before.tmp"
        after = docs / f".{name}.after.tmp"
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
