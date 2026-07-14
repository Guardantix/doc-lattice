"""Tests for transactional reconcile batch commits."""

import json
from pathlib import Path

import pytest

from doc_lattice import persistence, reconcile_transaction
from doc_lattice.constants import RECONCILE_JOURNAL_NAME, RECONCILE_JOURNAL_VERSION
from doc_lattice.error_types import (
    ReconcileConflictError,
    ReconcileInProgressError,
    ReconcilePersistenceError,
)
from doc_lattice.reconcile import Rewrite

_UNLOCKED_COMMIT_REWRITES = reconcile_transaction.commit_rewrites
_UNLOCKED_RECOVER_TRANSACTION = reconcile_transaction.recover_transaction


def _commit_rewrites(
    project_root: Path,
    rewrites: list[Rewrite],
    write_paths: dict[Path, Path],
) -> None:
    """Run a behavior test's commit through the required lock capability."""
    with reconcile_transaction.reconcile_lock(project_root) as lock:
        _UNLOCKED_COMMIT_REWRITES(
            project_root,
            rewrites,
            write_paths,
            lock=lock,
        )


def _recover_transaction(project_root: Path) -> reconcile_transaction.RecoveryResult:
    """Run a behavior test's recovery through the required lock capability."""
    with reconcile_transaction.reconcile_lock(project_root) as lock:
        return _UNLOCKED_RECOVER_TRANSACTION(project_root, lock=lock)


def _rewrite(path: Path, before: bytes, after: bytes, ref: str) -> Rewrite:
    """Build one exact-byte rewrite for commit tests."""
    return Rewrite(path=path, before=before, after=after, applied=frozenset({ref}))


def _assert_no_transaction_artifacts(root: Path) -> None:
    """Assert that a completed abort left no journal or temporary stages."""
    assert not (root / RECONCILE_JOURNAL_NAME).exists()
    assert not list(root.rglob("*.tmp"))


def test_commit_requires_a_valid_active_lock_before_mutation(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")

    with pytest.raises(ReconcileInProgressError, match="active reconcile lock"):
        _UNLOCKED_COMMIT_REWRITES(
            tmp_path,
            [rewrite],
            {destination: destination},
            lock=None,  # ty: ignore[invalid-argument-type] - deliberate misuse
        )

    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_commit_rejects_wrong_root_lock_before_mutation(tmp_path: Path):
    project_root = tmp_path / "project"
    other_root = tmp_path / "other"
    project_root.mkdir()
    other_root.mkdir()
    destination = project_root / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")

    with (
        reconcile_transaction.reconcile_lock(other_root) as wrong_lock,
        pytest.raises(ReconcileInProgressError, match="different project root"),
    ):
        _UNLOCKED_COMMIT_REWRITES(
            project_root,
            [rewrite],
            {destination: destination},
            lock=wrong_lock,
        )

    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(project_root)


def test_commit_rejects_replaced_root_directory_before_mutation(tmp_path: Path):
    project_root = tmp_path / "project"
    moved_root = tmp_path / "moved-project"
    project_root.mkdir()

    with reconcile_transaction.reconcile_lock(project_root) as stale_lock:
        project_root.rename(moved_root)
        project_root.mkdir()
        destination = project_root / "doc.md"
        destination.write_bytes(b"old bytes")
        rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")

        with pytest.raises(ReconcileInProgressError, match="different project root directory"):
            _UNLOCKED_COMMIT_REWRITES(
                project_root,
                [rewrite],
                {destination: destination},
                lock=stale_lock,
            )

    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(project_root)
    _assert_no_transaction_artifacts(moved_root)


def test_commit_rejects_released_lock_before_mutation(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    with reconcile_transaction.reconcile_lock(tmp_path) as released_lock:
        pass

    with pytest.raises(ReconcileInProgressError, match="no longer active"):
        _UNLOCKED_COMMIT_REWRITES(
            tmp_path,
            [rewrite],
            {destination: destination},
            lock=released_lock,
        )

    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_commit_accepts_current_root_bound_lock(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")

    with reconcile_transaction.reconcile_lock(tmp_path) as lock:
        _UNLOCKED_COMMIT_REWRITES(
            tmp_path,
            [rewrite],
            {destination: destination},
            lock=lock,
        )

    assert destination.read_bytes() == b"new bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_commit_rejects_dangling_canonical_journal_before_staging(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal.symlink_to("missing-journal-target")
    staged: list[Path] = []
    real_stage = reconcile_transaction.stage_bytes

    def observe_stage(path: Path, data: bytes, *, prefix: str) -> Path:
        staged.append(path)
        return real_stage(path, data, prefix=prefix)

    monkeypatch.setattr(reconcile_transaction, "stage_bytes", observe_stage)

    with (
        reconcile_transaction.reconcile_lock(tmp_path) as lock,
        pytest.raises(ReconcilePersistenceError) as caught,
    ):
        _UNLOCKED_COMMIT_REWRITES(
            tmp_path,
            [rewrite],
            {destination: destination},
            lock=lock,
        )

    assert "symlink" in str(caught.value)
    assert staged == []
    assert destination.read_bytes() == b"old bytes"
    assert journal.is_symlink()
    assert not list(tmp_path.glob("*.tmp"))


def test_commit_rejects_same_capability_reentrant_recovery(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_atomic_replace = reconcile_transaction.atomic_replace_bytes
    reentrant_rejections: list[str] = []

    with reconcile_transaction.reconcile_lock(tmp_path) as lock:

        def _attempt_reentrant_recovery(path: Path, data: bytes, *, prefix: str) -> None:
            if path == journal:
                try:
                    _UNLOCKED_RECOVER_TRANSACTION(tmp_path, lock=lock)
                except ReconcileInProgressError as error:
                    reentrant_rejections.append(str(error))
            real_atomic_replace(path, data, prefix=prefix)

        monkeypatch.setattr(
            reconcile_transaction,
            "atomic_replace_bytes",
            _attempt_reentrant_recovery,
        )
        _UNLOCKED_COMMIT_REWRITES(
            tmp_path,
            [rewrite],
            {destination: destination},
            lock=lock,
        )

    assert reentrant_rejections == ["reconcile lock capability is already in use"]
    assert destination.read_bytes() == b"new bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_commit_rewrites_durably_replaces_batch_and_cleans_artifacts(tmp_path: Path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first\x00\xff")
    second.write_bytes(b"old second\r\n")
    rewrites = [
        _rewrite(first, b"old first\x00\xff", b"new first\r\n", "up#a"),
        _rewrite(second, b"old second\r\n", b"new second\x00\xfe", "up#b"),
    ]

    _commit_rewrites(
        tmp_path,
        rewrites,
        {first: first, second: second},
    )

    assert first.read_bytes() == b"new first\r\n"
    assert second.read_bytes() == b"new second\x00\xfe"
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_duplicate_resolved_destinations_fail_before_creating_artifacts(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    first_identity = tmp_path / "first-identity.md"
    second_identity = tmp_path / "second-identity.md"
    rewrites = [
        _rewrite(first_identity, b"old bytes", b"first replacement", "up#a"),
        _rewrite(second_identity, b"old bytes", b"second replacement", "up#b"),
    ]

    with pytest.raises(
        ReconcilePersistenceError,
        match=r"duplicate reconcile destination.*doc\.md",
    ):
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first_identity: destination, second_identity: destination},
        )

    assert destination.read_bytes() == b"old bytes"
    assert list(tmp_path.iterdir()) == [destination]


def test_canonical_duplicate_destinations_fail_before_staging(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    subdirectory = tmp_path / "subdirectory"
    subdirectory.mkdir()
    alias = subdirectory / ".." / destination.name
    first_identity = tmp_path / "first-identity.md"
    second_identity = tmp_path / "second-identity.md"
    rewrites = [
        _rewrite(first_identity, b"old bytes", b"first replacement", "up#a"),
        _rewrite(second_identity, b"old bytes", b"second replacement", "up#b"),
    ]
    real_stage = reconcile_transaction.stage_bytes
    stage_calls = 0

    def _observe_stage(destination_path: Path, data: bytes, *, prefix: str) -> Path:
        nonlocal stage_calls
        stage_calls += 1
        return real_stage(destination_path, data, prefix=prefix)

    monkeypatch.setattr(reconcile_transaction, "stage_bytes", _observe_stage)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first_identity: destination, second_identity: alias},
        )

    assert "duplicate reconcile destination" in str(caught.value)
    assert stage_calls == 0
    assert destination.read_bytes() == b"old bytes"
    assert not list(tmp_path.rglob("*.tmp"))
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()


def test_journal_destination_alias_fails_before_creating_artifacts(tmp_path: Path):
    identity = tmp_path / "doc-identity.md"
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    rewrite = _rewrite(identity, b"", b"replacement", "up#x")

    with pytest.raises(
        ReconcilePersistenceError,
        match=r"reconcile destination.*aliases journal path",
    ):
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {identity: journal},
        )

    assert list(tmp_path.iterdir()) == []


def test_canonical_journal_alias_fails_before_staging(tmp_path: Path, monkeypatch):
    subdirectory = tmp_path / "subdirectory"
    subdirectory.mkdir()
    identity = tmp_path / "doc-identity.md"
    journal_alias = subdirectory / ".." / RECONCILE_JOURNAL_NAME
    rewrite = _rewrite(identity, b"", b"replacement", "up#x")
    real_stage = reconcile_transaction.stage_bytes
    stage_calls = 0

    def _observe_stage(destination_path: Path, data: bytes, *, prefix: str) -> Path:
        nonlocal stage_calls
        stage_calls += 1
        return real_stage(destination_path, data, prefix=prefix)

    monkeypatch.setattr(reconcile_transaction, "stage_bytes", _observe_stage)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {identity: journal_alias},
        )

    assert "reconcile destination" in str(caught.value)
    assert "aliases journal path" in str(caught.value)
    assert stage_calls == 0
    assert not list(tmp_path.rglob("*.tmp"))
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()


def test_commit_journal_order_and_artifact_lifecycle(  # noqa: PLR0915
    tmp_path: Path, monkeypatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    first = docs / "first.md"
    second = docs / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        _rewrite(first, b"old first", b"new first", "up#a"),
        _rewrite(second, b"old second", b"new second", "up#b"),
    ]
    events: list[str] = []
    prepared_payload: dict[str, object] = {}
    stage_calls: list[tuple[Path, bytes, str, Path]] = []
    real_file_sha256 = reconcile_transaction.file_sha256
    real_replace_staged = reconcile_transaction.replace_staged
    real_cleanup = reconcile_transaction._cleanup_transaction_artifacts

    def _stage_bytes(destination: Path, data: bytes, *, prefix: str) -> Path:
        staged = persistence.stage_bytes(destination, data, prefix=prefix)
        stage_calls.append((destination, data, prefix, staged))
        return staged

    def _atomic_create(path: Path, data: bytes, *, prefix: str) -> None:
        assert path == tmp_path / RECONCILE_JOURNAL_NAME
        assert not path.exists()
        assert prefix == f"{RECONCILE_JOURNAL_NAME}."
        prepared_payload.update(json.loads(data))
        persistence.atomic_create_bytes(path, data, prefix=prefix)
        events.append("journal prepared")

    def _fingerprint(path: Path) -> str:
        if path in (first, second):
            events.append(f"check {path.name}")
        return real_file_sha256(path)

    def _replace(staged: Path, destination: Path) -> None:
        if "doc-lattice-after" in staged.name:
            events.append(f"replace {destination.name}")
        real_replace_staged(staged, destination)

    def _atomic_replace(path: Path, data: bytes, *, prefix: str) -> None:
        assert path == tmp_path / RECONCILE_JOURNAL_NAME
        assert prefix == f"{RECONCILE_JOURNAL_NAME}."
        assert json.loads(data)["state"] == "committed"
        persistence.atomic_replace_bytes(path, data, prefix=prefix)
        events.append("journal committed")

    def _cleanup(entries, journal: Path, journal_bytes: bytes) -> None:
        assert json.loads(journal.read_bytes())["state"] == "committed"
        assert journal.read_bytes() == journal_bytes
        events.append("cleanup")
        real_cleanup(entries, journal, journal_bytes)

    monkeypatch.setattr(reconcile_transaction, "stage_bytes", _stage_bytes, raising=False)
    monkeypatch.setattr(reconcile_transaction, "atomic_create_bytes", _atomic_create)
    monkeypatch.setattr(reconcile_transaction, "file_sha256", _fingerprint)
    monkeypatch.setattr(reconcile_transaction, "replace_staged", _replace)
    monkeypatch.setattr(
        reconcile_transaction,
        "atomic_replace_bytes",
        _atomic_replace,
        raising=False,
    )
    monkeypatch.setattr(reconcile_transaction, "_cleanup_transaction_artifacts", _cleanup)

    _commit_rewrites(
        tmp_path,
        rewrites,
        {first: first, second: second},
    )

    assert events == [
        "journal prepared",
        "check first.md",
        "replace first.md",
        "check second.md",
        "replace second.md",
        "journal committed",
        "cleanup",
    ]
    assert [call[:3] for call in stage_calls] == [
        (first, b"old first", ".first.md.doc-lattice-before."),
        (first, b"new first", ".first.md.doc-lattice-after."),
        (second, b"old second", ".second.md.doc-lattice-before."),
        (second, b"new second", ".second.md.doc-lattice-after."),
    ]
    assert len({call[3] for call in stage_calls}) == 4
    assert all(call[3].suffix == ".tmp" for call in stage_calls)
    assert prepared_payload == {
        "version": RECONCILE_JOURNAL_VERSION,
        "state": "prepared",
        "entries": [
            {
                "destination": "docs/first.md",
                "before_path": stage_calls[0][3].relative_to(tmp_path).as_posix(),
                "before_sha256": persistence.sha256_bytes(b"old first"),
                "after_path": stage_calls[1][3].relative_to(tmp_path).as_posix(),
                "after_sha256": persistence.sha256_bytes(b"new first"),
            },
            {
                "destination": "docs/second.md",
                "before_path": stage_calls[2][3].relative_to(tmp_path).as_posix(),
                "before_sha256": persistence.sha256_bytes(b"old second"),
                "after_path": stage_calls[3][3].relative_to(tmp_path).as_posix(),
                "after_sha256": persistence.sha256_bytes(b"new second"),
            },
        ],
    }


def test_commit_detects_edit_before_replace_and_preserves_it(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"validated")
    rewrite = _rewrite(destination, b"validated", b"replacement", "up#x")
    real_file_sha256 = reconcile_transaction.file_sha256
    destination_reads = 0

    def _edit_then_hash(path: Path) -> str:
        nonlocal destination_reads
        if path == destination:
            destination_reads += 1
            if destination_reads == 1:
                destination.write_bytes(b"editor change")
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _edit_then_hash)

    with pytest.raises(ReconcileConflictError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert str(destination) in str(caught.value)
    assert "changed after validation" in str(caught.value)
    assert "rollback complete" in str(caught.value)
    assert destination.read_bytes() == b"editor change"
    _assert_no_transaction_artifacts(tmp_path)


def test_second_conflict_rolls_back_first_and_preserves_editor_bytes(tmp_path: Path, monkeypatch):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        _rewrite(first, b"old first", b"new first", "up#a"),
        _rewrite(second, b"old second", b"new second", "up#b"),
    ]
    real_file_sha256 = reconcile_transaction.file_sha256
    edited = False

    def _edit_second_then_hash(path: Path) -> str:
        nonlocal edited
        if path == second and not edited:
            edited = True
            second.write_bytes(b"unrelated editor bytes")
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _edit_second_then_hash)

    with pytest.raises(ReconcileConflictError, match=r"second.md.*changed after validation"):
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first: first, second: second},
        )

    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"unrelated editor bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_tampered_second_after_stage_rolls_back_first_and_preserves_evidence(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        _rewrite(first, b"old first", b"new first", "up#a"),
        _rewrite(second, b"old second", b"new second", "up#b"),
    ]
    real_file_sha256 = reconcile_transaction.file_sha256
    tampered_after: list[Path] = []

    def _tamper_before_second_check(path: Path) -> str:
        if path == second and not tampered_after:
            stages = list(tmp_path.glob(".second.md.doc-lattice-after.*.tmp"))
            assert len(stages) == 1
            stages[0].write_bytes(b"tampered after stage")
            tampered_after.append(stages[0])
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _tamper_before_second_check)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first: first, second: second},
        )

    message = str(caught.value)
    assert "digest mismatch" in message
    assert "rollback failed" in message
    assert "run 'doc-lattice reconcile --recover'" in message
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"
    assert len(tampered_after) == 1
    assert tampered_after[0].read_bytes() == b"tampered after stage"
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    assert json.loads(journal.read_bytes())["state"] == "prepared"


def test_missing_second_after_stage_rolls_back_first_and_cleans_resolved_transaction(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        _rewrite(first, b"old first", b"new first", "up#a"),
        _rewrite(second, b"old second", b"new second", "up#b"),
    ]
    real_file_sha256 = reconcile_transaction.file_sha256
    missing_after: list[Path] = []

    def _remove_after_before_second_check(path: Path) -> str:
        if path == second and not missing_after:
            stages = list(tmp_path.glob(".second.md.doc-lattice-after.*.tmp"))
            assert len(stages) == 1
            stages[0].unlink()
            missing_after.append(stages[0])
        return real_file_sha256(path)

    monkeypatch.setattr(
        reconcile_transaction,
        "file_sha256",
        _remove_after_before_second_check,
    )

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first: first, second: second},
        )

    message = str(caught.value)
    assert "staged after image" in message
    assert "is missing immediately before replacement" in message
    assert "rollback complete" in message
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"
    assert len(missing_after) == 1
    assert not missing_after[0].exists()
    _assert_no_transaction_artifacts(tmp_path)


def test_missing_destination_during_check_is_persistence_failure_and_rolls_back(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        _rewrite(first, b"old first", b"new first", "up#a"),
        _rewrite(second, b"old second", b"new second", "up#b"),
    ]
    real_file_sha256 = reconcile_transaction.file_sha256
    removed = False

    def _remove_second_then_hash(path: Path) -> str:
        nonlocal removed
        if path == second and not removed:
            removed = True
            second.unlink()
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _remove_second_then_hash)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first: first, second: second},
        )

    assert not isinstance(caught.value, ReconcileConflictError)
    assert str(second) in str(caught.value)
    assert "fingerprinting destination" in str(caught.value)
    assert "rollback complete" in str(caught.value)
    assert first.read_bytes() == b"old first"
    assert not second.exists()
    _assert_no_transaction_artifacts(tmp_path)


def test_unreadable_destination_during_check_is_typed_and_rolls_back(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    real_file_sha256 = reconcile_transaction.file_sha256
    failed = False

    def _fail_destination_read(path: Path) -> str:
        nonlocal failed
        if path == destination and not failed:
            failed = True
            raise PermissionError("injected destination read denial")
        return real_file_sha256(path)

    monkeypatch.setattr(reconcile_transaction, "file_sha256", _fail_destination_read)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    message = str(caught.value)
    assert "fingerprinting destination" in message
    assert str(destination) in message
    assert "injected destination read denial" in message
    assert "rollback complete" in message
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_second_replace_failure_rolls_back_first_and_reports_primary_cause(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        _rewrite(first, b"old first", b"new first", "up#a"),
        _rewrite(second, b"old second", b"new second", "up#b"),
    ]
    real_replace = reconcile_transaction.replace_staged
    after_replaces = 0

    def _fail_second_after(staged: Path, destination: Path) -> None:
        nonlocal after_replaces
        if "doc-lattice-after" in staged.name:
            after_replaces += 1
            if after_replaces == 2:
                raise OSError("disk full")
        real_replace(staged, destination)

    monkeypatch.setattr(reconcile_transaction, "replace_staged", _fail_second_after)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first: first, second: second},
        )

    message = str(caught.value)
    assert "replacing destination" in message
    assert str(second) in message
    assert "disk full" in message
    assert "rollback complete" in message
    assert "no files were reconciled" in message
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"
    _assert_no_transaction_artifacts(tmp_path)


def test_directory_sync_failure_after_replace_rolls_back_consumed_stage(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    real_sync = persistence.sync_directory
    failed = False

    def _fail_after_destination_replace(path: Path) -> None:
        nonlocal failed
        if not failed and destination.exists() and destination.read_bytes() == b"new bytes":
            failed = True
            raise OSError("destination directory fsync failed")
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_after_destination_replace)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "destination directory fsync failed" in str(caught.value)
    assert "rollback complete" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_stage_failure_cleans_attempt_stages_without_changing_destination(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    real_stage = reconcile_transaction.stage_bytes
    calls = 0

    def _fail_after_stage(destination_path: Path, data: bytes, *, prefix: str) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("after-image stage write failed")
        return real_stage(destination_path, data, prefix=prefix)

    monkeypatch.setattr(reconcile_transaction, "stage_bytes", _fail_after_stage)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "after-image stage write failed" in str(caught.value)
    assert "no destination was changed" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_journal_link_failure_cleans_stages_without_changing_destination(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")

    def _fail_journal_link(path: Path, data: bytes, *, prefix: str) -> None:  # noqa: ARG001
        raise OSError("journal hard-link failed")

    monkeypatch.setattr(reconcile_transaction, "atomic_create_bytes", _fail_journal_link)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "publishing prepared journal" in str(caught.value)
    assert "journal hard-link failed" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_journal_stage_failure_cleans_document_stages_without_mutation(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_stage = persistence.stage_bytes

    def _fail_journal_stage(destination_path: Path, data: bytes, *, prefix: str) -> Path:
        if destination_path == journal:
            raise OSError("prepared journal stage failed")
        return real_stage(destination_path, data, prefix=prefix)

    monkeypatch.setattr(persistence, "stage_bytes", _fail_journal_stage)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "prepared journal stage failed" in str(caught.value)
    assert "no destination was changed" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_journal_publish_sync_failure_cleans_visible_journal_and_stages(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_sync = persistence.sync_directory
    failed = False

    def _fail_prepared_journal_sync(path: Path) -> None:
        nonlocal failed
        if not failed and journal.exists():
            failed = True
            assert json.loads(journal.read_bytes())["state"] == "prepared"
            raise OSError("prepared journal directory fsync failed")
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_prepared_journal_sync)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "prepared journal directory fsync failed" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_journal_post_publication_stage_cleanup_failure_names_orphan(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_unlink = persistence.durable_unlink
    helper_stages: list[Path] = []

    def _fail_helper_stage_cleanup(path: Path) -> None:
        if path != journal and path.name.startswith(f"{RECONCILE_JOURNAL_NAME}."):
            assert json.loads(journal.read_bytes())["state"] == "prepared"
            helper_stages.append(path)
            raise OSError("atomic-create staging cleanup failed")
        real_unlink(path)

    monkeypatch.setattr(persistence, "durable_unlink", _fail_helper_stage_cleanup)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert destination.read_bytes() == b"old bytes"
    assert not journal.exists()
    assert len(helper_stages) == 1
    assert helper_stages[0].exists()
    assert list(tmp_path.rglob("*.tmp")) == helper_stages
    message = str(caught.value)
    assert "atomic-create staging cleanup failed" in message
    assert str(helper_stages[0]) in message
    assert "inspect and remove it manually" in message


def test_existing_journal_is_preserved_and_requires_recovery(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal_bytes = b"existing recovery authority\n"
    journal.write_bytes(journal_bytes)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert str(journal) in str(caught.value)
    assert "already exists" in str(caught.value)
    assert "run 'doc-lattice reconcile --recover'" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    assert journal.read_bytes() == journal_bytes
    assert not list(tmp_path.rglob("*.tmp"))


def test_preparation_cleanup_failure_is_secondary_to_primary_error(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    real_stage = reconcile_transaction.stage_bytes
    calls = 0

    def _fail_second_stage(destination_path: Path, data: bytes, *, prefix: str) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("primary staging failure")
        return real_stage(destination_path, data, prefix=prefix)

    def _fail_cleanup(path: Path) -> None:
        raise OSError(f"secondary cleanup failure for {path.name}")

    monkeypatch.setattr(reconcile_transaction, "stage_bytes", _fail_second_stage)
    monkeypatch.setattr(reconcile_transaction, "durable_unlink", _fail_cleanup)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "primary staging failure" in str(caught.value)
    assert "secondary cleanup failure" in str(caught.value)
    assert any(
        "secondary cleanup failure" in note for note in getattr(caught.value, "__notes__", ())
    )
    remaining = list(tmp_path.rglob("*.doc-lattice-before.*.tmp"))
    assert len(remaining) == 1
    assert str(remaining[0]) in str(caught.value)
    assert "inspect and remove it manually" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()


def test_rollback_failure_preserves_prepared_recovery_evidence_and_both_causes(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        _rewrite(first, b"old first", b"new first", "up#a"),
        _rewrite(second, b"old second", b"new second", "up#b"),
    ]
    real_replace = reconcile_transaction.replace_staged

    def _fail_commit_and_rollback(staged: Path, destination: Path) -> None:
        if destination == second and "doc-lattice-after" in staged.name:
            raise OSError("original second replacement failure")
        if destination == first and "doc-lattice-before" in staged.name:
            raise OSError("rollback restoration failure")
        real_replace(staged, destination)

    monkeypatch.setattr(reconcile_transaction, "replace_staged", _fail_commit_and_rollback)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            rewrites,
            {first: first, second: second},
        )

    message = str(caught.value)
    assert "original second replacement failure" in message
    assert "rollback restoration failure" in message
    assert "rollback complete" not in message
    assert "run 'doc-lattice reconcile --recover'" in message
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    assert json.loads(journal.read_bytes())["state"] == "prepared"
    assert first.read_bytes() == b"new first"
    assert second.read_bytes() == b"old second"
    assert list(tmp_path.rglob("*.doc-lattice-before.*.tmp"))


def test_marker_replace_failure_resets_prepared_journal_then_rolls_back(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_replace = persistence.os.replace

    def _fail_marker_replace(source: Path | str, target: Path | str) -> None:
        if Path(target) == journal:
            raise OSError("committed marker replace failed")
        real_replace(source, target)

    monkeypatch.setattr(persistence.os, "replace", _fail_marker_replace)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    message = str(caught.value)
    assert "marking journal committed" in message
    assert "committed marker replace failed" in message
    assert "rollback complete" in message
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_marker_directory_sync_failure_resets_visible_committed_bytes_and_rolls_back(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_sync = persistence.sync_directory
    visible_marker_states: list[str] = []
    failed = False

    def _fail_committed_marker_sync(path: Path) -> None:
        nonlocal failed
        if not failed and journal.exists():
            state = json.loads(journal.read_bytes())["state"]
            if state == "committed":
                failed = True
                visible_marker_states.append(state)
                raise OSError("committed marker directory fsync failed")
        real_sync(path)

    monkeypatch.setattr(persistence, "sync_directory", _fail_committed_marker_sync)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    message = str(caught.value)
    assert "committed marker directory fsync failed" in message
    assert "rollback complete" in message
    assert visible_marker_states == ["committed"]
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_durable_committed_marker_has_no_fallible_pre_cleanup_step(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_unlink = persistence.durable_unlink
    post_marker_cleanup_calls: list[Path] = []

    def _reject_cleanup_after_marker(path: Path) -> None:
        if not path.exists() and journal.exists():
            state = json.loads(journal.read_bytes())["state"]
            if state == "committed":
                post_marker_cleanup_calls.append(path)
                raise OSError("post-marker helper cleanup failed")
        real_unlink(path)

    monkeypatch.setattr(persistence, "durable_unlink", _reject_cleanup_after_marker)

    _commit_rewrites(
        tmp_path,
        [rewrite],
        {destination: destination},
    )

    assert post_marker_cleanup_calls == []
    assert destination.read_bytes() == b"new bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_marker_reset_failure_refuses_unsafe_rollback_and_requires_recovery(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_atomic_replace = reconcile_transaction.atomic_replace_bytes
    marker_calls = 0

    def _fail_marker_and_reset(
        path: Path,
        data: bytes,
        *,
        prefix: str,  # noqa: ARG001 (signature matches persistence primitive)
    ) -> None:
        nonlocal marker_calls
        marker_calls += 1
        if marker_calls == 1:
            path.write_bytes(data)
            raise OSError("marker sync failed after visible replace")
        raise OSError("prepared journal reset failed")

    monkeypatch.setattr(reconcile_transaction, "atomic_replace_bytes", _fail_marker_and_reset)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    message = str(caught.value)
    assert "marker sync failed after visible replace" in message
    assert "prepared journal reset failed" in message
    assert "rollback complete" not in message
    assert "run 'doc-lattice reconcile --recover'" in message
    assert destination.read_bytes() == b"new bytes"
    assert json.loads(journal.read_bytes())["state"] == "committed"
    assert list(tmp_path.rglob("*.doc-lattice-before.*.tmp"))

    monkeypatch.setattr(reconcile_transaction, "atomic_replace_bytes", real_atomic_replace)
    result = _recover_transaction(tmp_path)
    assert result.action == "cleaned_committed"
    assert destination.read_bytes() == b"new bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_cleanup_failure_after_durable_commit_never_rolls_back_and_recovery_finishes(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    real_unlink = reconcile_transaction.durable_unlink
    failed = False

    def _fail_committed_cleanup(path: Path) -> None:
        nonlocal failed
        if not failed and "doc-lattice-before" in path.name:
            failed = True
            raise OSError("committed cleanup unlink failed")
        real_unlink(path)

    monkeypatch.setattr(reconcile_transaction, "durable_unlink", _fail_committed_cleanup)

    with pytest.raises(ReconcilePersistenceError) as caught:
        _commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "committed cleanup unlink failed" in str(caught.value)
    assert "rollback complete" not in str(caught.value)
    assert destination.read_bytes() == b"new bytes"
    assert json.loads(journal.read_bytes())["state"] == "committed"

    result = _recover_transaction(tmp_path)
    assert result.action == "cleaned_committed"
    assert destination.read_bytes() == b"new bytes"
    _assert_no_transaction_artifacts(tmp_path)
