"""Load a ``scripts/`` file the way the runner executes it.

``scripts/`` is not a package and every workflow invokes its scripts by path, so the suites that
test one load it with `runpy.run_path` rather than importing it: that builds the same module
object the runner builds, from the same file the workflow names.

One thing direct execution does that `run_path` does not is prepend the script's own directory to
``sys.path``. That insertion is what lets `scripts/audit_action_runtimes.py` reach its sibling
`scripts/_ci_report.py` under ``uv run --no-project``, where nothing else is importable.
`run_path` documents its own temporary insertion only for an argument naming a directory or
another ``sys.path`` entry, not for one naming a file, so a suite calling it directly fails at
collection on a sibling import that works perfectly well in the workflow.

`load_script` restores that one difference and nothing else. It is scoped to the load rather than
put on the whole session's import path, so a script's siblings are importable exactly while the
script is executing, which is the same window the runner gives them. A suite whose script imports
no sibling has nothing to restore and may keep calling `run_path` itself.
"""

import sys
from pathlib import Path
from runpy import run_path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"


def script_path(name: str) -> Path:
    """Return the path of one file in ``scripts/``.

    Args:
        name: The file name, such as ``audit_action_runtimes.py``.

    Returns:
        Its absolute path, resolved against the repository root rather than the caller's
        directory, so a suite reads the same file whatever pytest was invoked from.
    """
    return _SCRIPTS / name


def load_script(path: Path) -> dict[str, Any]:
    """Execute one script by path and return its module namespace.

    The script's own directory is on ``sys.path`` for the duration of the execution and removed
    afterwards, which is what a sibling import inside the script needs and what running the file
    directly would have given it.

    Args:
        path: The script to execute.

    Returns:
        The globals the script left behind, which callers read the names they test out of.
    """
    directory = str(path.parent)
    borrowed = directory not in sys.path
    if borrowed:
        sys.path.insert(0, directory)
    try:
        return run_path(str(path))
    finally:
        if borrowed:
            sys.path.remove(directory)
