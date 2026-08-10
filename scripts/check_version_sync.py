#!/usr/bin/env python3
"""Verify __version__, pyproject.toml, CHANGELOG.md, and pinned-install docs agree.

The pinned-install documents scanned are README.md and MANAGED_CI.md, each of which
carries exact ``doc-lattice==X.Y.Z`` or ``doc-lattice@vX.Y.Z`` install refs.
"""

import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

from doc_lattice import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSION_HEADING = re.compile(r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)
_PINNED_REF = re.compile(
    r"(?<![A-Za-z0-9._-])doc-lattice(?:==|@v)"
    r"(?P<version>\d+\.\d+\.\d+)(?![A-Za-z0-9._+-])"
)


def _pyproject_version(pyproject_text: str) -> str | None:
    """Return the [project] version declared in pyproject text, or None if absent."""
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _changelog_version(changelog_text: str) -> str | None:
    """Return the first versioned ``## [X.Y.Z]`` heading in changelog text, or None.

    A non-version heading such as ``## [Unreleased]`` does not match and is skipped,
    so the first real release heading is returned.
    """
    match = _VERSION_HEADING.search(changelog_text)
    return match.group("version") if match else None


def _stale_pinned_refs(doc_text: str, init_version: str) -> list[str]:
    """Return the distinct pinned versions in doc_text that differ from init_version.

    Order follows first appearance in the text; duplicates of the same stale version
    are collapsed to a single entry.
    """
    stale: list[str] = []
    for match in _PINNED_REF.finditer(doc_text):
        version = match.group("version")
        if version != init_version and version not in stale:
            stale.append(version)
    return stale


def check_version_consistency(
    init_version: str,
    pyproject_text: str,
    changelog_text: str,
    pinned_docs: Mapping[str, str],
) -> list[str]:
    """Return a message for each version source that disagrees with init_version.

    Args:
        init_version: The canonical package version, ``doc_lattice.__version__``.
        pyproject_text: The full text of ``pyproject.toml``.
        changelog_text: The full text of ``CHANGELOG.md``.
        pinned_docs: Documents that carry exact install pins, mapping each document
            filename such as ``README.md`` to that document's full text. Documents
            are scanned in the mapping's insertion order, and a document left out of
            the mapping is not scanned at all.

    Returns:
        One message per disagreeing source, naming the file and the expected value.
        An empty list means every source matches ``init_version``. A source that
        cannot be parsed is reported as a mismatch rather than raising. Each distinct
        stale ``doc-lattice==X.Y.Z`` or ``doc-lattice@vX.Y.Z`` pin found in a pinned
        document produces one message naming that document, no matter how many times
        that stale version occurs; a document with no pinned refs is consistent.
    """
    messages: list[str] = []
    pyproject_version = _pyproject_version(pyproject_text)
    if pyproject_version != init_version:
        messages.append(
            f"pyproject.toml version is {pyproject_version!r}, expected {init_version!r}; "
            f"set [project] version to match doc_lattice.__version__."
        )
    changelog_version = _changelog_version(changelog_text)
    if changelog_version != init_version:
        messages.append(
            f"CHANGELOG.md top version heading is {changelog_version!r}, "
            f"expected {init_version!r}; add or fix the '## [{init_version}]' section."
        )
    for doc_name, doc_text in pinned_docs.items():
        for stale_version in _stale_pinned_refs(doc_text, init_version):
            messages.append(
                f"{doc_name} pins doc-lattice version {stale_version}, "
                f"expected {init_version}; update the pinned install refs."
            )
    return messages


def main() -> None:
    """Read every version source and exit non-zero on any disagreement."""
    pyproject_text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog_text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pinned_docs = {
        "README.md": (_REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        "MANAGED_CI.md": (_REPO_ROOT / "MANAGED_CI.md").read_text(encoding="utf-8"),
    }
    messages = check_version_consistency(__version__, pyproject_text, changelog_text, pinned_docs)
    for message in messages:
        print(message, file=sys.stderr)
    sys.exit(1 if messages else 0)


if __name__ == "__main__":
    main()
