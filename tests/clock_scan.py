"""The current-time scan the two clock-boundary suites share.

AD-2 keeps one substitutable clock in `datetime_utils.py`, and `scripts/` enforces the same rule
at a boundary that cannot call it, since the auditing workflow runs those files under
``uv run --no-project``. The two roots are checked by two suites because only one of them ships:
`tests/test_conventions.py` reads `src/`, which the sdist carries, while `scripts/` is
repository-only and its suite is denied archive membership.

The matcher lives here rather than in either of them so the two cannot drift into asking
different questions -- the failure `scripts/_ci_report.py` records about its own callers, which
twice diverged while each spelled its own copy.
"""

import ast
from pathlib import Path


def current_time_calls(source: str) -> list[int]:
    """Return the line of every ``.now()``/``.utcnow()`` call, the tz-aware form included.

    Args:
        source: Python source text.

    Returns:
        One line number per call, in the order the tree walks them.
    """
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("now", "utcnow")
    ]


def clock_readers(files: list[Path], root: Path) -> dict[str, int]:
    """Return every file that reads the current time, by root-relative path, with its call count.

    Compared against a manifest as a whole rather than checked file by file, so one equality
    carries all three ways the rule breaks: an unlisted file reading the clock, a listed file
    reading it twice, and a listed file that is gone or moved and would otherwise leave its
    exemption granted to nothing and never visited.

    Args:
        files: The files to scan.
        root: The directory the returned keys are relative to.

    Returns:
        A mapping of root-relative POSIX path to call count, holding only files that read it.
    """
    counts = {}
    for path in files:
        calls = current_time_calls(path.read_text(encoding="utf-8"))
        if calls:
            counts[path.relative_to(root).as_posix()] = len(calls)
    return counts
