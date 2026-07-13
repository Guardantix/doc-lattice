"""Tests for shared durable filesystem persistence primitives."""

import hashlib
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
)


def test_sha256_bytes_returns_full_digest():
    data = b"exact bytes\x00\xff"

    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_file_sha256_hashes_exact_file_bytes(tmp_path: Path):
    data = b"line one\r\nline two\x00\xff"
    path = tmp_path / "artifact.bin"
    path.write_bytes(data)

    assert file_sha256(path) == hashlib.sha256(data).hexdigest()


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


def test_stage_bytes_cleans_temp_when_file_fsync_fails(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.failed."

    def _fail_fsync(fd: int) -> None:  # noqa: ARG001
        raise OSError("fsync failed")

    monkeypatch.setattr(persistence.os, "fsync", _fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        stage_bytes(destination, b"replacement", prefix=prefix)

    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


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


def test_atomic_replace_bytes_replaces_target_and_cleans_stage(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old")
    prefix = ".doc.md.replace."

    atomic_replace_bytes(destination, b"new", prefix=prefix)

    assert destination.read_bytes() == b"new"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_atomic_create_bytes_refuses_existing_target_and_cleans_stage(tmp_path: Path):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"original")
    prefix = ".doc.md.create."

    with pytest.raises(FileExistsError):
        atomic_create_bytes(destination, b"replacement", prefix=prefix)

    assert destination.read_bytes() == b"original"
    assert list(tmp_path.glob(f"{prefix}*.tmp")) == []


def test_atomic_create_bytes_creates_absent_target_and_cleans_stage(tmp_path: Path):
    destination = tmp_path / "doc.md"
    prefix = ".doc.md.create."

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
