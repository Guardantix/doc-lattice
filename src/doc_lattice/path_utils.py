"""Path handling utilities."""

from pathlib import Path


def safe_resolve(path: str | Path, root: Path | None = None) -> Path:
    """Resolve a path and verify it stays within a containment root.

    Args:
        path: The path to resolve, absolute or relative.
        root: The containment boundary; defaults to the resolved current working
            directory when omitted.

    Returns:
        The fully resolved path (symlinks followed, "." and ".." segments collapsed).

    Raises:
        ValueError: If the resolved path is not inside root.
    """
    if root is None:
        root = Path.cwd()
    root = root.resolve()
    # Resolve first: .resolve() collapses ".." and follows symlinks, so a path that escapes the
    # root by either route lands outside it and fails the relative_to containment check below.
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        msg = f"Path {path} resolves to {resolved}, which is outside {root}"
        raise ValueError(msg) from None
    return resolved


def format_path_for_display(path: Path) -> str:
    """Spell a discovered path for human-facing output, neutralizing terminal control bytes.

    A document path is a repo-controlled string that reaches diagnostics without passing the
    frontmatter parser, so a filename carrying ESC can forge or corrupt the output it appears
    in. The spelling is exactly ``repr(str(path))`` on the active supported interpreter: a
    single expression rather than a project-owned codec, because ``str.__repr__`` is already
    injective, and injectivity is what makes "no two filenames render alike" checkable instead
    of argued. See AD-34 for the raw-path-versus-display-path boundary this sits on.

    Callers apply this where a path enters a human-facing message, never to the path they then
    open, compare, or write. Machine channels keep their own encoders.

    Args:
        path: A discovered or configured path, as this checkout spells it.

    Returns:
        The path's ``repr(str(path))`` spelling: always quoted, with C0, DEL, and C1 controls
        escaped (named escapes such as ``\\t`` where Python defines one, ``\\xNN`` otherwise),
        literal backslashes doubled, undecodable-byte surrogates rendered as ``\\udcNN`` rather
        than raised on, and printable non-ASCII preserved verbatim.
    """
    return repr(str(path))
