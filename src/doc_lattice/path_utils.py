"""Path handling utilities."""

from pathlib import Path


def safe_resolve(path: str | Path, root: Path | None = None) -> Path:
    """Resolve path, raising ValueError if it escapes root."""
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
