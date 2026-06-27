"""Path handling utilities."""

from pathlib import Path


def normalize_path(p: str | Path) -> Path:
    """Resolve and normalize a path."""
    return Path(p).resolve()


def ensure_dir(p: Path) -> Path:
    """Create directory if it does not exist, return the path."""
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_resolve(p: str | Path, root: Path | None = None) -> Path:
    """Resolve path, raising ValueError if it escapes root."""
    if root is None:
        root = Path.cwd()
    root = root.resolve()
    resolved = Path(p).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        msg = f"Path {p} resolves to {resolved}, which is outside {root}"
        raise ValueError(msg) from None
    return resolved
