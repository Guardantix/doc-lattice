#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a version, for use as GitHub release notes."""

import argparse
import sys
from pathlib import Path

from doc_lattice.version_check import changelog_section

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    """Write the ``## [version]`` changelog body to stdout, or fail loudly.

    Exits non-zero with a message on stderr when no ``## [version]`` heading exists
    or when the section is empty, so the release job never publishes empty notes.
    """
    parser = argparse.ArgumentParser(description="Print a CHANGELOG.md section as release notes.")
    parser.add_argument("version", help="the X.Y.Z version whose section to extract")
    version = parser.parse_args().version
    changelog_text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog_section(changelog_text, version)
    if section is None:
        print(
            f"CHANGELOG.md has no '## [{version}]' section; add release notes for {version}.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not section:
        print(
            f"CHANGELOG.md '## [{version}]' section is empty; add release notes for {version}.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(section)


if __name__ == "__main__":
    main()
