"""Provide shared durable filesystem persistence primitives."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from .constants import PERSISTENCE_TEMP_SUFFIX

_IS_WINDOWS = os.name == "nt"


def sha256_bytes(data: bytes) -> str:
    """Return the full SHA-256 hexadecimal digest of bytes.

    Args:
        data: The exact bytes to hash.

    Returns:
        The 64-character hexadecimal digest.
    """
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    """Return the full SHA-256 digest of a file's exact bytes.

    Args:
        path: The file to hash.

    Returns:
        The 64-character hexadecimal digest.
    """
    return sha256_bytes(path.read_bytes())


def sync_directory(path: Path) -> None:
    """Flush directory metadata to durable storage.

    Args:
        path: An existing directory to synchronize.

    Raises:
        OSError: If the directory cannot be opened or synchronized.
    """
    if _IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _add_unpublished_stage_cleanup_note(
    primary: OSError,
    staged: Path,
    cleanup_error: OSError,
) -> None:
    """Attach exact manual remediation for a helper-owned stage orphan."""
    primary.add_note(
        f"durable cleanup failed for helper-owned stage {staged}: {cleanup_error}; "
        "it is not governed by a recovery journal, so inspect and remove it manually "
        "when safe"
    )


def _durable_unlink_preserving_error(staged: Path, primary: OSError) -> None:
    """Clean a stage without replacing the primary operation error."""
    try:
        durable_unlink(staged)
    except OSError as cleanup_error:
        _add_unpublished_stage_cleanup_note(primary, staged, cleanup_error)


def stage_bytes(destination: Path, data: bytes, *, prefix: str) -> Path:
    """Write and synchronize bytes to a unique file beside a destination.

    Args:
        destination: The eventual destination used to select the staging directory.
        data: The exact bytes to stage.
        prefix: The caller-owned temporary filename prefix.

    Returns:
        The path to the synchronized staging file.

    Raises:
        OSError: If staging or synchronization fails.
    """
    try:
        destination_stat = destination.stat(follow_symlinks=False)
    except FileNotFoundError:
        destination_mode = None
    else:
        destination_mode = (
            stat.S_IMODE(destination_stat.st_mode)
            if stat.S_ISREG(destination_stat.st_mode)
            else None
        )
    fd, tmp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=prefix,
        suffix=PERSISTENCE_TEMP_SUFFIX,
    )
    staged = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if destination_mode is not None and not _IS_WINDOWS:
                os.fchmod(handle.fileno(), destination_mode)
            os.fsync(handle.fileno())
        sync_directory(destination.parent)
    except OSError as primary:
        _durable_unlink_preserving_error(staged, primary)
        raise
    return staged


def replace_staged(staged: Path, destination: Path) -> None:
    """Publish a same-directory staged file as a durable atomic replacement.

    Args:
        staged: The staged file to publish from the destination directory.
        destination: The path to create or replace.

    Raises:
        ValueError: If the staged file is not in the destination directory.
        OSError: If replacement or directory synchronization fails.
    """
    if staged.parent.resolve() != destination.parent.resolve():
        msg = "staged and destination paths must be in the same directory"
        raise ValueError(msg)
    os.replace(staged, destination)  # noqa: PTH105 (required atomic replacement primitive)
    sync_directory(destination.parent)


def atomic_replace_bytes(path: Path, data: bytes, *, prefix: str) -> None:
    """Durably replace a path with exact bytes.

    Args:
        path: The path to create or replace.
        data: The exact replacement bytes.
        prefix: The caller-owned temporary filename prefix.

    Raises:
        OSError: If staging, replacement, cleanup, or synchronization fails.
    """
    staged = stage_bytes(path, data, prefix=prefix)
    try:
        replace_staged(staged, path)
    except OSError as primary:
        _durable_unlink_preserving_error(staged, primary)
        raise


def atomic_create_bytes(path: Path, data: bytes, *, prefix: str) -> None:
    """Durably create a path without replacing an existing artifact.

    Args:
        path: The new path to create.
        data: The exact bytes to publish.
        prefix: The caller-owned temporary filename prefix.

    Raises:
        OSError: If staging, creation, cleanup, or synchronization fails.
    """
    staged = stage_bytes(path, data, prefix=prefix)
    try:
        os.link(staged, path)
        sync_directory(path.parent)
    except OSError as primary:
        _durable_unlink_preserving_error(staged, primary)
        raise
    try:
        durable_unlink(staged)
    except OSError as cleanup_error:
        _add_unpublished_stage_cleanup_note(cleanup_error, staged, cleanup_error)
        raise


def durable_unlink(path: Path) -> None:
    """Remove an artifact and durably synchronize its parent directory.

    Args:
        path: The artifact to remove. An absent path is ignored.

    Raises:
        OSError: If removal or directory synchronization fails.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_directory(path.parent)
