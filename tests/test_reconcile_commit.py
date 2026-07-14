"""Tests for transactional reconcile batch commits."""

import json
from pathlib import Path

import pytest

from doc_lattice import persistence, reconcile_transaction
from doc_lattice.constants import RECONCILE_JOURNAL_NAME, RECONCILE_JOURNAL_VERSION
from doc_lattice.error_types import ReconcileConflictError, ReconcilePersistenceError
from doc_lattice.reconcile import Rewrite


def _rewrite(path: Path, before: bytes, after: bytes, ref: str) -> Rewrite:
    """Build one exact-byte rewrite for commit tests."""
    return Rewrite(path=path, before=before, after=after, applied=frozenset({ref}))


def _assert_no_transaction_artifacts(root: Path) -> None:
    """Assert that a completed abort left no journal or temporary stages."""
    assert not (root / RECONCILE_JOURNAL_NAME).exists()
    assert not list(root.rglob("*.tmp"))


def test_commit_rewrites_durably_replaces_batch_and_cleans_artifacts(tmp_path: Path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first\x00\xff")
    second.write_bytes(b"old second\r\n")
    rewrites = [
        _rewrite(first, b"old first\x00\xff", b"new first\r\n", "up#a"),
        _rewrite(second, b"old second\r\n", b"new second\x00\xfe", "up#b"),
    ]

    reconcile_transaction.commit_rewrites(
        tmp_path,
        rewrites,
        {first: first, second: second},
    )

    assert first.read_bytes() == b"new first\r\n"
    assert second.read_bytes() == b"new second\x00\xfe"
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()
    assert not list(tmp_path.rglob("*.tmp"))


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

    reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
            tmp_path,
            rewrites,
            {first: first, second: second},
        )

    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"unrelated editor bytes"
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "prepared journal directory fsync failed" in str(caught.value)
    assert destination.read_bytes() == b"old bytes"
    _assert_no_transaction_artifacts(tmp_path)


def test_existing_journal_is_preserved_and_requires_recovery(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old bytes")
    rewrite = _rewrite(destination, b"old bytes", b"new bytes", "up#x")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal_bytes = b"existing recovery authority\n"
    journal.write_bytes(journal_bytes)

    with pytest.raises(ReconcilePersistenceError) as caught:
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
        reconcile_transaction.commit_rewrites(
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
    result = reconcile_transaction.recover_transaction(tmp_path)
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
        reconcile_transaction.commit_rewrites(
            tmp_path,
            [rewrite],
            {destination: destination},
        )

    assert "committed cleanup unlink failed" in str(caught.value)
    assert "rollback complete" not in str(caught.value)
    assert destination.read_bytes() == b"new bytes"
    assert json.loads(journal.read_bytes())["state"] == "committed"

    result = reconcile_transaction.recover_transaction(tmp_path)
    assert result.action == "cleaned_committed"
    assert destination.read_bytes() == b"new bytes"
    _assert_no_transaction_artifacts(tmp_path)
