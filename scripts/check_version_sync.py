#!/usr/bin/env python3
"""Verify __version__, pyproject.toml, CHANGELOG.md, and README.md pins agree."""

import sys
from pathlib import Path

from doc_lattice import __version__
from doc_lattice.version_check import check_version_consistency

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    """Read the four version sources and exit non-zero on any disagreement."""
    pyproject_text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog_text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme_text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    messages = check_version_consistency(__version__, pyproject_text, changelog_text, readme_text)
    for message in messages:
        print(message, file=sys.stderr)
    sys.exit(1 if messages else 0)


if __name__ == "__main__":
    main()
