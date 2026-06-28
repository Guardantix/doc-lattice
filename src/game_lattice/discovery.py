"""Discover candidate markdown docs under contained roots, and read them as UTF-8."""

import warnings
from collections.abc import Sequence
from pathlib import Path

from .error_types import UnreadableDocError
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
    rel = path.relative_to(root)
    return any(rel.full_match(pattern) for pattern in ignore_globs)


def read_doc(path: Path) -> str:
    """Read a doc as UTF-8.

    Args:
        path: The file to read.

    Returns:
        The file contents as text.

    Raises:
        UnreadableDocError: If the file cannot be read or is not valid UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read doc {path}: {exc}"
        raise UnreadableDocError(msg) from exc
