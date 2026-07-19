"""Provide shared durable filesystem persistence primitives."""

import hashlib
import os
import secrets
import stat
import tempfile
from pathlib import Path

from .constants import PERSISTENCE_TEMP_SUFFIX

_IS_WINDOWS = os.name == "nt"
_STAGE_NAME_ATTEMPTS = 128


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


def _sync_directory_fd(directory_fd: int) -> None:
    """Flush metadata for an already-open directory descriptor."""
    if not _IS_WINDOWS:
        os.fsync(directory_fd)


def _require_directory_entry_name(name: str, *, description: str) -> None:
    """Require one basename so a dirfd operation cannot escape its directory."""
    separators = (os.sep, os.altsep)
    if (
        not name
        or name in {".", ".."}
        or any(separator and separator in name for separator in separators)
    ):
        raise ValueError(f"{description} must name one directory entry")


def _open_stage_at(directory_fd: int, prefix: str) -> tuple[int, str]:
    """Create one private random staging file inside ``directory_fd``."""
    _require_directory_entry_name(prefix, description="staging prefix")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(_STAGE_NAME_ATTEMPTS):
        staged_name = f"{prefix}{secrets.token_hex(16)}{PERSISTENCE_TEMP_SUFFIX}"
        try:
            return os.open(staged_name, flags, 0o600, dir_fd=directory_fd), staged_name
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique durable staging file")


def _add_unpublished_stage_cleanup_note_at(
    primary: OSError,
    staged_name: str,
    cleanup_error: OSError,
) -> None:
    """Attach manual remediation for an unpublished descriptor-relative stage orphan."""
    primary.add_note(
        f"durable cleanup failed for helper-owned stage {staged_name}: {cleanup_error}; "
        "it is not governed by a recovery journal, so inspect and remove it manually "
        "when safe"
    )


def _durable_unlink_at(directory_fd: int, name: str) -> None:
    """Remove one descriptor-relative artifact and synchronize its directory."""
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    _sync_directory_fd(directory_fd)


def _durable_unlink_at_preserving_error(
    directory_fd: int,
    staged_name: str,
    primary: OSError,
) -> None:
    """Clean a descriptor-relative stage without replacing the primary error."""
    try:
        _durable_unlink_at(directory_fd, staged_name)
    except OSError as cleanup_error:
        _add_unpublished_stage_cleanup_note_at(primary, staged_name, cleanup_error)


def _stage_bytes_at(
    directory_fd: int,
    destination_name: str,
    data: bytes,
    *,
    prefix: str,
) -> str:
    """Write and synchronize one private staging file under an open directory."""
    _require_directory_entry_name(destination_name, description="destination name")
    try:
        destination_stat = os.stat(
            destination_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        destination_mode = None
    else:
        destination_mode = (
            stat.S_IMODE(destination_stat.st_mode)
            if stat.S_ISREG(destination_stat.st_mode)
            else None
        )
    fd, staged_name = _open_stage_at(directory_fd, prefix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if destination_mode is not None and not _IS_WINDOWS:
                os.fchmod(handle.fileno(), destination_mode)
            os.fsync(handle.fileno())
        _sync_directory_fd(directory_fd)
    except OSError as primary:
        _durable_unlink_at_preserving_error(directory_fd, staged_name, primary)
        raise
    return staged_name


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


def atomic_replace_bytes_at(
    directory_fd: int,
    destination_name: str,
    data: bytes,
    *,
    prefix: str,
) -> None:
    """Durably replace one descriptor-relative destination with exact bytes.

    The staging file, atomic rename, and directory synchronization all remain relative to
    ``directory_fd``. This lets a caller retain publication ownership after the directory's
    pathname is renamed or recreated.
    """
    staged_name = _stage_bytes_at(
        directory_fd,
        destination_name,
        data,
        prefix=prefix,
    )
    try:
        os.replace(
            staged_name,
            destination_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        _sync_directory_fd(directory_fd)
    except OSError as primary:
        _durable_unlink_at_preserving_error(directory_fd, staged_name, primary)
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


def atomic_create_bytes_at(
    directory_fd: int,
    destination_name: str,
    data: bytes,
    *,
    prefix: str,
) -> None:
    """Durably create one descriptor-relative destination without replacing a winner."""
    staged_name = _stage_bytes_at(
        directory_fd,
        destination_name,
        data,
        prefix=prefix,
    )
    try:
        os.link(
            staged_name,
            destination_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        _sync_directory_fd(directory_fd)
    except OSError as primary:
        _durable_unlink_at_preserving_error(directory_fd, staged_name, primary)
        raise
    try:
        _durable_unlink_at(directory_fd, staged_name)
    except OSError as cleanup_error:
        _add_unpublished_stage_cleanup_note_at(cleanup_error, staged_name, cleanup_error)
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
