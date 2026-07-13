"""Discover candidate markdown docs under contained roots, and read them as UTF-8."""

import os
import warnings
from collections.abc import Sequence
from pathlib import Path

from .error_types import UnreadableDocError
from .hashing import normalize_newlines
from .path_utils import safe_resolve


def discover_doc_paths(roots: Sequence[Path], ignore_globs: Sequence[str]) -> list[Path]:
    """Return every ``.md`` path under the roots, minus ignored matches, sorted.

    Args:
        roots: Already project-contained docs roots (from ``ProjectConfig``).
        ignore_globs: Glob patterns matched against each file's path relative to its
            root, anchored at the root. ``drafts/*.md`` skips only top-level drafts, not
            a same-named subdirectory; use ``**`` to match at any depth.

    Returns:
        A sorted, de-duplicated list of markdown file paths. A file that resolves outside
        the project root (via a symlink or absolute path) is skipped with a warning rather
        than read, so a silently missing doc does not masquerade as a broken ref later.
    """
    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            if _ignored(path, root, ignore_globs):
                continue
            try:
                safe_resolve(path, root)
            except ValueError:
                warnings.warn(
                    f"skipping {path}: it escapes the project root via a symlink or "
                    "absolute path and was not read",
                    stacklevel=2,
                )
                continue
            found.add(path)
    return sorted(found)


def _ignored(path: Path, root: Path, ignore_globs: Sequence[str]) -> bool:
    relative_path = path.relative_to(root)
    return any(relative_path.full_match(ignore_glob) for ignore_glob in ignore_globs)


def _unreadable(path: Path, exc: OSError | UnicodeDecodeError) -> UnreadableDocError:
    """Build the single UnreadableDocError used by every doc read and stat failure.

    Centralizing the message is what lets the cached load path (byte read, decode, stat)
    produce byte-identical errors to an uncached read.
    """
    return UnreadableDocError(f"cannot read doc {path}: {exc}")


def read_doc_bytes(path: Path) -> bytes:
    """Read a doc's raw bytes.

    Args:
        path: The file to read.

    Returns:
        The file contents as bytes.

    Raises:
        UnreadableDocError: If the file cannot be read.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _unreadable(path, exc) from exc


def read_doc_bytes_and_stat(path: Path) -> tuple[bytes, os.stat_result]:
    """Read a doc's raw bytes and the stat of the same open handle.

    Opening once and stat-ing the open descriptor keeps the returned stat consistent with the
    exact bytes read, so a rewrite racing a separate stat cannot record a stat hint that does
    not correspond to the hashed content.

    Args:
        path: The file to read.

    Returns:
        A tuple of the file contents and the ``os.stat_result`` of the open handle.

    Raises:
        UnreadableDocError: If the file cannot be read.
    """
    try:
        with path.open("rb") as handle:
            st = os.fstat(handle.fileno())
            data = handle.read()
    except OSError as exc:
        raise _unreadable(path, exc) from exc
    return data, st


def decode_doc(path: Path, data: bytes) -> str:
    """Decode a doc's bytes as UTF-8 with universal-newline translation.

    Translates ``\\r\\n`` and lone ``\\r`` to ``\\n`` so the result matches what the historical
    loader produced via ``Path.read_text(encoding="utf-8")`` (default ``newline=None``). Without
    this, a lone-``\\r`` (classic Mac) document keeps its carriage returns, ``split_frontmatter``
    (which splits only on ``\\n``) sees the whole file as one line and its opening fence is never
    matched, and the node is silently dropped from the lattice in both the cached and uncached
    paths. Keeping the translation here preserves byte-parity with that historical read.

    Args:
        path: The file the bytes came from, for the error message.
        data: The raw bytes.

    Returns:
        The decoded text with line endings normalized to ``\\n``.

    Raises:
        UnreadableDocError: If the bytes are not valid UTF-8.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _unreadable(path, exc) from exc
    return normalize_newlines(text)


def read_doc(path: Path) -> str:
    """Read a doc as UTF-8.

    Args:
        path: The file to read.

    Returns:
        The file contents as text.

    Raises:
        UnreadableDocError: If the file cannot be read or is not valid UTF-8.
    """
    return decode_doc(path, read_doc_bytes(path))
