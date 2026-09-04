#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a version, or check it exists and has a body.

The default mode writes the ``## [X.Y.Z]`` section to stdout, and the release job runs it that
way to produce the notes it publishes. ``--check`` runs the same validation, defaults the version
to the declared package version, and prints nothing. That is the pre-merge counterpart: the
code-quality job and the pre-commit hook run it beside ``check_version_sync.py``, so a promoted
heading with nothing under it fails on the pull request rather than in the release job, where the
version is already merged and the failure strands it.

The check lives here rather than in ``check_version_sync.py`` because the section boundary has
exactly one reader. ``changelog_section`` is what the release job extracts the notes with and what
``release_gate.py`` refuses a re-arm on; a second implementation beside version sync would be a
third reading of the same lines, free to disagree with both. What version sync owns is agreement
among the release surfaces, which is why it reads the heading and not the body. What the release
job checks is unchanged: this mode is an addition ahead of it, never a move.
"""

import argparse
import re
import sys
from pathlib import Path

from doc_lattice import __version__

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
    """Write the ``## [version]`` changelog body to stdout, or validate it and stay silent.

    Exits non-zero with a message on stderr when no ``## [version]`` heading exists or when the
    section is empty. Both modes refuse both shapes: the release job never publishes empty notes,
    and the pre-merge gate never lets a bump reach it that would.
    """
    parser = argparse.ArgumentParser(description="Print a CHANGELOG.md section as release notes.")
    parser.add_argument(
        "version",
        nargs="?",
        default=__version__,
        help="the X.Y.Z version whose section to read (default: the declared package version)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the section and print nothing, for the pre-merge gate",
    )
    args = parser.parse_args()
    version = args.version
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
    if not args.check:
        print(section)


if __name__ == "__main__":
    main()
