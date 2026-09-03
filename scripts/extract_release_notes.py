#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a version, for use as GitHub release notes."""

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
# `[ \t]*` rather than `\s*`: a heading is one line, and `\s` matches a newline, so a bare `##`
# above a line starting with `[` would bound a section that no Markdown reader ends there. That
# truncates the notes at a boundary that does not exist, and it is load-bearing beyond the notes
# -- `release_gate.py` refuses a re-arm on this same reading, so a phantom boundary reports an
# `## [Unreleased]` section as empty and lets undocumented work ship inside the tag.
_ANY_HEADING = re.compile(r"^##[ \t]*\[", re.MULTILINE)


def changelog_section(changelog_text: str, version: str) -> str | None:
    """Return the body of the ``## [version]`` changelog section, or None if absent.

    The body is everything between that heading and the next ``## [`` heading (or the
    end of the document for the final section), trimmed of leading and trailing blank
    lines. Only ``## [`` lines bound a section, so neither a fenced code block whose
    content starts with ``## `` nor a bare ``##`` above a bracketed line truncates the
    notes: the whitespace between ``##`` and ``[`` is spaces and tabs, never a newline.
    A section that exists but has no content returns the empty string, so the caller can
    distinguish a missing heading (None) from an empty one ("").

    Args:
        changelog_text: The full text of ``CHANGELOG.md``.
        version: The ``X.Y.Z`` version whose section to extract.

    Returns:
        The trimmed section body, "" if the heading exists but is empty, or None if
        no ``## [version]`` heading is present.
    """
    heading = re.compile(r"^##[ \t]*\[" + re.escape(version) + r"\].*$", re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        return None
    following = _ANY_HEADING.search(changelog_text, match.end())
    end = following.start() if following else len(changelog_text)
    return changelog_text[match.end() : end].strip()


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
