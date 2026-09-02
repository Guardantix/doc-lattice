"""Helpers the two link-gate suites share: the source writer and the permission skip.

`tests/test_link_check.py` drives the engine and `tests/cli/test_links.py` drives the command,
so both build their fixtures by writing Markdown under a `tmp_path` root, and both skip the
cases that need a filesystem which enforces modes. Holding one copy of the skip is what keeps it
a single portability rule rather than two that can drift apart while still both looking right.

The names keep their leading underscore across the move, matching `tests/failing_streams.py`.
"""

import os
from pathlib import Path

import pytest

_requires_permission_enforcement = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX filesystem that enforces modes"
)


def _write(root: Path, name: str, text: str) -> None:
    """Write one UTF-8 document under `root`, creating the directories above it."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
