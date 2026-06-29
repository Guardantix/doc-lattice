"""Path handling utilities."""

from pathlib import Path


def normalize_path(path: str | Path) -> Path:
    """Resolve and normalize a path."""
    return Path(path).resolve()


def ensure_dir(path: Path) -> Path:
    """Create directory if it does not exist, return the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


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
