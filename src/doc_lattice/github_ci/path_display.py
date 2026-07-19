"""Control-safe display formatting for repository-controlled paths."""

import json
from pathlib import PurePath


def display_path(path: PurePath | str, *, quoted: bool = False) -> str:
    """Render one path with JSON escaping while preserving ordinary path output.

    Args:
        path: Path text that may originate from a repository-controlled filename.
        quoted: Keep the surrounding JSON quotes for diagnostics that historically use them.

    Returns:
        An ASCII-safe path display with control characters escaped.
    """
    path_text = path.as_posix() if isinstance(path, PurePath) else path
    rendered = json.dumps(path_text, ensure_ascii=True)
    return rendered if quoted else rendered[1:-1]
