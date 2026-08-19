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
        # AD-34 applies to this message too. It is not only the boundary's own diagnostic: the
        # reconcile transaction layer embeds it verbatim when a journal records an escaping
        # path, so an unwrapped spelling here would put a hostile filename's raw control bytes
        # straight into recovery output that has otherwise been closed.
        msg = (
            f"Path {format_path_for_display(path)} resolves to "
            f"{format_path_for_display(resolved)}, which is outside "
            f"{format_path_for_display(root)}"
        )
        raise ValueError(msg) from None
    return resolved


def format_path_for_display(path: str | Path) -> str:
    """Spell a discovered path for human-facing output, neutralizing terminal control bytes.

    A document path is a repo-controlled string that reaches diagnostics without passing the
    frontmatter parser, so a filename carrying ESC can forge or corrupt the output it appears
    in. The spelling is exactly ``repr(str(path))`` on the active supported interpreter: a
    single expression rather than a project-owned codec, because ``str.__repr__`` is already
    injective, and injectivity is what makes "no two filenames render alike" checkable instead
    of argued. See AD-34 for the raw-path-versus-display-path boundary this sits on.

    Callers apply this where a path enters a human-facing message, never to the path they then
    open, compare, or write. Machine channels keep their own encoders.

    A ``str`` is accepted alongside a ``Path`` because several sinks hold a path that was
    recorded rather than resolved: a journal entry's own ``destination`` or ``before_path``
    field, a project-relative recovery string, and the expected staged-artifact name pattern
    are all text. ``str(value)`` is the identity on them, so the spelling is the same single
    expression; routing them through ``Path()`` first would instead normalize away ``//``, a
    trailing separator, and a leading ``./``, and a diagnostic that rejects a recorded path
    has to show the string it actually rejected.

    Args:
        path: A discovered or configured path, as this checkout spells it, or a path already
            held as text.

    Returns:
        The path's ``repr(str(path))`` spelling: always quoted, with C0, DEL, and C1 controls
        escaped (named escapes such as ``\\t`` where Python defines one, ``\\xNN`` otherwise),
        literal backslashes doubled, undecodable-byte surrogates rendered as ``\\udcNN`` rather
        than raised on, and printable non-ASCII preserved verbatim.
    """
    return repr(str(path))
