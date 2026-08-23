"""Provide shared durable filesystem persistence primitives."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from .constants import PERSISTENCE_TEMP_SUFFIX
from .path_utils import format_path_for_display

_IS_WINDOWS = os.name == "nt"


class DestinationExistsError(FileExistsError):
    """A create-if-absent found the destination already there and left nothing behind.

    Both halves of that are the point. It is raised only when the destination existed *and* the
    helper-owned stage was cleaned up, which is the one outcome a caller can treat as benign:
    nothing was written, nothing was replaced, and there is no orphan to report. A collision
    that also failed to clean its stage keeps the plain ``FileExistsError`` and carries the
    remediation note, because the orphan is a real failure whatever caused the collision.

    It exists so callers stop inferring that conjunction for themselves. ``init`` used to read
    it off the *absence of notes*, which answers "did cleanup fail" rather than "did the
    destination exist", so any other note-free ``FileExistsError`` reaching it -- ``mkstemp``
    exhausting its candidate names, for one -- was reported to the user as an existing config
    and exited 0.

    It stays an ``OSError`` subclass rather than a ``ProjectError``: this module raises raw
    ``OSError`` by contract and the command adapters wrap it. Being a ``FileExistsError`` is
    also load-bearing rather than incidental, since ``reconcile_transaction.py`` classifies a
    lost journal-create race by that type and must keep matching.
    """


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


def _regular_file_mode(destination_stat: os.stat_result) -> int | None:
    """Return a regular destination's permission bits, or None for any other entry type."""
    if not stat.S_ISREG(destination_stat.st_mode):
        return None
    return stat.S_IMODE(destination_stat.st_mode)


def _unpublished_stage_cleanup_note(staged: str | Path, cleanup_error: OSError) -> str:
    """Render the manual remediation note for an orphaned helper-owned stage.

    The stage is named after the destination it was written beside, so a reconcile stage
    inherits a document filename and this note carries it into text a person reads. AD-34
    therefore applies here even though the same helper also stages ``init``'s config file and
    the load cache, whose paths are not document paths: the spelling is settled once, at the
    only sink, rather than per caller.
    """
    return (
        f"durable cleanup failed for helper-owned stage {format_path_for_display(staged)}: "
        f"{cleanup_error}; it is not governed by a recovery journal, so inspect and remove it "
        "manually when safe"
    )


def _add_unpublished_stage_cleanup_note(
    primary: OSError,
    staged: Path,
    cleanup_error: OSError,
) -> None:
    """Attach exact manual remediation for a helper-owned stage orphan."""
    primary.add_note(_unpublished_stage_cleanup_note(staged, cleanup_error))


def _durable_unlink_preserving_error(staged: Path, primary: OSError) -> bool:
    """Clean a stage without replacing the primary operation error.

    Args:
        staged: The helper-owned stage to remove.
        primary: The error the caller will raise, annotated in place when cleanup fails.

    Returns:
        True when the stage was removed, and False when a noted orphan remains. The caller
        reads this rather than the note it just attached, so "was an orphan left" stays a
        returned fact instead of something recovered from the exception afterwards.
    """
    try:
        durable_unlink(staged)
    except OSError as cleanup_error:
        _add_unpublished_stage_cleanup_note(primary, staged, cleanup_error)
        return False
    return True


def stage_bytes(destination: Path, data: bytes, *, prefix: str) -> Path:
    """Write and synchronize bytes to a unique file beside a destination.

    On POSIX, when ``destination`` already exists as a regular file the stage is chmodded to
    the destination's permission bits so publication preserves them. Otherwise the stage keeps
    the private mode ``mkstemp`` created it with, which then becomes the new file's mode.

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
        destination_mode = _regular_file_mode(destination_stat)
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
        DestinationExistsError: If the destination already existed and the stage was cleaned
            up, which is the benign outcome a caller may treat as "nothing to do".
        OSError: If staging, creation, cleanup, or synchronization fails. A collision that also
            orphaned its stage arrives here rather than above, as the plain
            ``FileExistsError`` carrying the remediation note.
    """
    staged = stage_bytes(path, data, prefix=prefix)
    try:
        os.link(staged, path)
        sync_directory(path.parent)
    except OSError as primary:
        cleaned = _durable_unlink_preserving_error(staged, primary)
        # `os.link` is the only thing in the block that can raise EEXIST, and it means the
        # destination is there, so the type alone settles it. Staging runs before the block, so
        # a `mkstemp` collision is not reachable from here and keeps the plain type. Rebuilt
        # from the original's fields rather than wrapped, so the rendered message stays exactly
        # what the failed link produced. Both filenames are carried: `os.link` records the
        # stage and the destination, and rendering drops the `-> destination` half without the
        # second one.
        if cleaned and isinstance(primary, FileExistsError):
            existing = DestinationExistsError(primary.errno, primary.strerror)
            existing.filename = primary.filename
            existing.filename2 = primary.filename2
            raise existing from primary
        raise
    try:
        durable_unlink(staged)
    except OSError as cleanup_error:
        # Publication already succeeded, so the failed stage cleanup is itself the error the
        # caller sees. It is passed as both the note carrier and the described cause on
        # purpose, so the raised OSError still names the orphan and its remediation.
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
