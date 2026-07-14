"""Tests for shared durable filesystem persistence primitives."""

import hashlib
import stat
from pathlib import Path

import pytest

from doc_lattice import persistence
from doc_lattice.persistence import (
    atomic_create_bytes,
    atomic_replace_bytes,
    durable_unlink,
    file_sha256,
    replace_staged,
    sha256_bytes,
    stage_bytes,
    sync_directory,
)


def test_sha256_bytes_returns_full_digest():
    data = b"exact bytes\x00\xff"

    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_file_sha256_hashes_exact_file_bytes(tmp_path: Path):
    data = b"line one\r\nline two\x00\xff"
    path = tmp_path / "artifact.bin"
    path.write_bytes(data)

    assert file_sha256(path) == hashlib.sha256(data).hexdigest()


def test_sync_directory_is_noop_on_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(persistence, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        persistence.os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("Windows directory sync opened a directory"),
    )

    sync_directory(tmp_path)


def test_stage_bytes_creates_unique_prefixed_temp_files_beside_destination(tmp_path: Path):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.doc-lattice-before."
    data = b"exact replacement bytes\x00\xff"

    first = stage_bytes(destination, data, prefix=prefix)
    second = stage_bytes(destination, data, prefix=prefix)

    try:
        assert first != second
        assert first.parent == destination.parent
        assert second.parent == destination.parent
        assert first.name.startswith(prefix)
        assert second.name.startswith(prefix)
        assert first.name.endswith(".tmp")
        assert second.name.endswith(".tmp")
        assert first.read_bytes() == data
        assert second.read_bytes() == data
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def test_stage_bytes_skips_fchmod_on_windows(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old")
    destination.chmod(0o754)
    monkeypatch.setattr(persistence, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        persistence.os,
        "fchmod",
        lambda *_args: pytest.fail("Windows staging called os.fchmod"),
    )
    monkeypatch.setattr(persistence, "sync_directory", lambda _path: None)

    staged = stage_bytes(destination, b"new", prefix=".doc.md.windows.")

    try:
        assert staged.read_bytes() == b"new"
    finally:
        staged.unlink(missing_ok=True)


def test_stage_bytes_cleans_temp_when_file_fsync_fails(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.failed."

    def _fail_fsync(fd: int) -> None:  # noqa: ARG001
        raise OSError("fsync failed")

    monkeypatch.setattr(persistence.os, "fsync", _fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        stage_bytes(destination, b"replacement", prefix=prefix)

    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_stage_bytes_preserves_fsync_error_when_durable_cleanup_sync_fails(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.failed."
    real_durable_unlink = persistence.durable_unlink
    cleanup_attempts: list[Path] = []
    fsync_attempts = 0

    def _fail_fsync(fd: int) -> None:  # noqa: ARG001
        nonlocal fsync_attempts
        fsync_attempts += 1
        if fsync_attempts == 1:
            raise OSError("stage fsync failed")
        raise OSError("cleanup sync failed")

    def _observe_cleanup(staged: Path) -> None:
        cleanup_attempts.append(staged)
        real_durable_unlink(staged)

    monkeypatch.setattr(persistence.os, "fsync", _fail_fsync)
    monkeypatch.setattr(persistence, "durable_unlink", _observe_cleanup)

    with pytest.raises(OSError, match="stage fsync failed") as caught:
        stage_bytes(destination, b"replacement", prefix=prefix)

    assert str(caught.value) == "stage fsync failed"
    assert len(cleanup_attempts) == 1
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []
    assert any("cleanup sync failed" in note for note in getattr(caught.value, "__notes__", []))


def test_stage_bytes_cleanup_failure_names_unpublished_orphan_and_manual_remediation(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.failed."
    cleanup_attempts: list[Path] = []

    def _fail_stage_fsync(fd: int) -> None:  # noqa: ARG001
        raise OSError("stage fsync failed")

    def _fail_cleanup_before_unlink(staged: Path) -> None:
        cleanup_attempts.append(staged)
        raise OSError("cleanup unlink blocked")

    monkeypatch.setattr(persistence.os, "fsync", _fail_stage_fsync)
    monkeypatch.setattr(persistence, "durable_unlink", _fail_cleanup_before_unlink)

    with pytest.raises(OSError, match="stage fsync failed") as caught:
        stage_bytes(destination, b"replacement", prefix=prefix)

    assert str(caught.value) == "stage fsync failed"
    assert len(cleanup_attempts) == 1
    orphan = cleanup_attempts[0]
    assert orphan.exists()
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == [orphan]
    notes = "; ".join(getattr(caught.value, "__notes__", ()))
    assert str(orphan) in notes
    assert "not governed by a recovery journal" in notes
    assert "inspect and remove it manually when safe" in notes


def test_replace_staged_publishes_bytes_and_syncs_destination_directory(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old")
    staged = tmp_path / ".doc.md.staged.tmp"
    staged.write_bytes(b"new")
    synced: list[Path] = []
    monkeypatch.setattr(persistence, "sync_directory", synced.append)

    replace_staged(staged, destination)

    assert destination.read_bytes() == b"new"
    assert not staged.exists()
    assert synced == [destination.parent]


def test_replace_staged_refuses_different_directories_without_mutation(tmp_path: Path):
    stage_directory = tmp_path / "staging"
    destination_directory = tmp_path / "destination"
    stage_directory.mkdir()
    destination_directory.mkdir()
    staged = stage_directory / "doc.md.tmp"
    destination = destination_directory / "doc.md"
    staged.write_bytes(b"new")
    destination.write_bytes(b"original")

    with pytest.raises(ValueError, match="same directory"):
        replace_staged(staged, destination)

    assert staged.read_bytes() == b"new"
    assert destination.read_bytes() == b"original"


def test_replace_staged_accepts_symlink_alias_of_destination_directory(tmp_path: Path):
    destination_directory = tmp_path / "destination"
    destination_directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(destination_directory, target_is_directory=True)
    staged = alias / "doc.md.tmp"
    destination = destination_directory / "doc.md"
    staged.write_bytes(b"new")
    destination.write_bytes(b"original")

    replace_staged(staged, destination)

    assert destination.read_bytes() == b"new"
    assert not staged.exists()


def test_atomic_replace_bytes_replaces_target_and_cleans_stage(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old")
    prefix = ".doc.md.replace."

    atomic_replace_bytes(destination, b"new", prefix=prefix)

    assert destination.read_bytes() == b"new"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_atomic_replace_bytes_preserves_existing_destination_mode(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old")
    destination.chmod(0o754)

    atomic_replace_bytes(destination, b"new", prefix=".doc.md.replace.")

    assert stat.S_IMODE(destination.stat().st_mode) == 0o754


def test_atomic_replace_bytes_keeps_absent_destination_private(tmp_path: Path):
    destination = tmp_path / "new.md"

    atomic_replace_bytes(destination, b"new", prefix=".new.md.replace.")

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_atomic_replace_bytes_supports_relative_destination(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    destination = Path("doc.md")
    destination.write_bytes(b"original")
    prefix = ".doc.md.replace."

    atomic_replace_bytes(destination, b"replacement", prefix=prefix)

    assert destination.read_bytes() == b"replacement"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_atomic_replace_bytes_cleans_stage_when_replacement_fails(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"original")
    prefix = ".doc.md.replace."
    replacement_attempts: list[Path] = []
    sync_observations: list[bool] = []

    def _observe_sync(parent: Path) -> None:
        assert parent == destination.parent
        sync_observations.append(bool(list(parent.glob(f"{prefix}*.tmp"))))

    def _fail_replacement(staged: Path, target: Path) -> None:
        assert target == destination
        assert staged.read_bytes() == b"replacement"
        replacement_attempts.append(staged)
        raise OSError("replace failed")

    monkeypatch.setattr(persistence, "sync_directory", _observe_sync)
    monkeypatch.setattr(persistence, "replace_staged", _fail_replacement)

    with pytest.raises(OSError, match="replace failed"):
        atomic_replace_bytes(destination, b"replacement", prefix=prefix)

    assert len(replacement_attempts) == 1
    assert destination.read_bytes() == b"original"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []
    assert sync_observations[0] is True
    assert sync_observations[-1] is False


def test_atomic_replace_bytes_preserves_replace_error_when_cleanup_fails(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"original")
    prefix = ".doc.md.replace."
    cleanup_attempts: list[Path] = []

    def _fail_replacement(staged: Path, target: Path) -> None:  # noqa: ARG001
        raise OSError("replace failed")

    def _fail_cleanup_sync(staged: Path) -> None:
        cleanup_attempts.append(staged)
        staged.unlink()
        raise OSError("cleanup sync failed")

    monkeypatch.setattr(persistence, "replace_staged", _fail_replacement)
    monkeypatch.setattr(persistence, "durable_unlink", _fail_cleanup_sync)

    with pytest.raises(OSError, match="replace failed") as caught:
        atomic_replace_bytes(destination, b"replacement", prefix=prefix)

    assert str(caught.value) == "replace failed"
    assert len(cleanup_attempts) == 1
    assert destination.read_bytes() == b"original"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []
    assert any("cleanup sync failed" in note for note in getattr(caught.value, "__notes__", []))


def test_atomic_replace_bytes_has_no_post_publish_cleanup_step(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"original")

    def _unexpected_cleanup(staged: Path) -> None:
        pytest.fail(f"consumed replace stage unexpectedly cleaned: {staged}")

    monkeypatch.setattr(persistence, "durable_unlink", _unexpected_cleanup)

    atomic_replace_bytes(destination, b"replacement", prefix=".doc.md.replace.")

    assert destination.read_bytes() == b"replacement"


def test_atomic_create_bytes_refuses_existing_target_and_cleans_stage(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"original")
    prefix = ".doc.md.create."

    with pytest.raises(FileExistsError):
        atomic_create_bytes(destination, b"replacement", prefix=prefix)

    assert destination.read_bytes() == b"original"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_atomic_create_bytes_preserves_existing_error_when_cleanup_fails(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"original")
    prefix = ".doc.md.create."
    link_attempts: list[Path] = []
    cleanup_attempts: list[Path] = []

    def _fail_existing_link(staged: Path, target: Path) -> None:
        assert target == destination
        assert destination.exists()
        link_attempts.append(staged)
        raise FileExistsError("target exists")

    def _fail_cleanup_sync(staged: Path) -> None:
        cleanup_attempts.append(staged)
        staged.unlink()
        raise OSError("cleanup sync failed")

    monkeypatch.setattr(persistence.os, "link", _fail_existing_link)
    monkeypatch.setattr(persistence, "durable_unlink", _fail_cleanup_sync)

    with pytest.raises(FileExistsError) as caught:
        atomic_create_bytes(destination, b"replacement", prefix=prefix)

    assert str(caught.value) == "target exists"
    assert len(link_attempts) == 1
    assert len(cleanup_attempts) == 1
    assert destination.read_bytes() == b"original"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []
    assert any("cleanup sync failed" in note for note in getattr(caught.value, "__notes__", []))


def test_atomic_create_link_cleanup_failure_names_unpublished_orphan_and_remediation(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.create."
    cleanup_attempts: list[Path] = []

    def _fail_link(staged: Path, target: Path) -> None:  # noqa: ARG001
        raise OSError("link publication failed")

    def _fail_cleanup_before_unlink(staged: Path) -> None:
        cleanup_attempts.append(staged)
        raise OSError("cleanup unlink blocked")

    monkeypatch.setattr(persistence.os, "link", _fail_link)
    monkeypatch.setattr(persistence, "durable_unlink", _fail_cleanup_before_unlink)

    with pytest.raises(OSError, match="link publication failed") as caught:
        atomic_create_bytes(destination, b"replacement", prefix=prefix)

    assert str(caught.value) == "link publication failed"
    assert not destination.exists()
    assert len(cleanup_attempts) == 1
    orphan = cleanup_attempts[0]
    assert orphan.exists()
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == [orphan]
    notes = "; ".join(getattr(caught.value, "__notes__", ()))
    assert str(orphan) in notes
    assert "not governed by a recovery journal" in notes
    assert "inspect and remove it manually when safe" in notes


def test_atomic_create_bytes_creates_absent_target_and_cleans_stage(tmp_path: Path):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.create."

    atomic_create_bytes(destination, b"created", prefix=prefix)

    assert destination.read_bytes() == b"created"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_atomic_create_bytes_raises_cleanup_error_after_successful_create(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.create."

    def _fail_cleanup_sync(staged: Path) -> None:
        staged.unlink()
        raise OSError("cleanup sync failed")

    monkeypatch.setattr(persistence, "durable_unlink", _fail_cleanup_sync)

    with pytest.raises(OSError, match="cleanup sync failed"):
        atomic_create_bytes(destination, b"created", prefix=prefix)

    assert destination.read_bytes() == b"created"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_durable_unlink_removes_artifact_and_syncs_parent(tmp_path: Path, monkeypatch):
    artifact = tmp_path / "journal.json"
    artifact.write_bytes(b"journal")
    synced: list[Path] = []
    monkeypatch.setattr(persistence, "sync_directory", synced.append)

    durable_unlink(artifact)

    assert not artifact.exists()
    assert synced == [artifact.parent]
