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
