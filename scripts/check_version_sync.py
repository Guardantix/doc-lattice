#!/usr/bin/env python3
"""Verify __version__, pyproject.toml, CHANGELOG.md, and the release surfaces agree.

Release surfaces are declared, never discovered. ``PIN_MANIFEST`` names each maintained document
that carries live ``doc-lattice==X.Y.Z`` or ``doc-lattice@vX.Y.Z`` install refs together with the
exact number it must carry, ``HISTORICAL_PIN_DOCS`` names the documents whose superseded pins are
preserved on purpose, and a recognized pin in any other maintained document fails as an
unclassified release surface. The two classifications are exclusive: a document named by both is
reported, since the exemption is applied first and would otherwise silence the declared count.
"Maintained documents" means the sorted root ``*.md`` files, the same mechanical set
``scripts/check_doc_links.py`` takes as its link sources.

Counts are exact rather than minimums because a minimum lets a newly added pin mask the deletion
of a required occurrence. What an exact count closes is any change that alters the number of
recognized pins: an uncompensated deletion or reformatting lowers it, and the failure names the
document. What it does not close is a change that preserves that number, and there are two of
those, both because the manifest counts occurrences rather than identifying sites. A change that
deletes one enrolled pin and adds another recognized current pin to the same document nets to the
declared count. And a newly added unrecognized spelling, in an enrolled document or in a pin-free
one, is never seen by ``_PINNED_REF`` at all. Site identity and wider candidate recognition are
both deliberately out of scope: this gate owns release currency, not the survival of one
particular prose install instruction.
"""

import re
import sys
import tomllib
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from doc_lattice import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MARKDOWN_SUFFIX = ".md"
_CHANGELOG_NAME = "CHANGELOG.md"
_VERSION_HEADING = re.compile(r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)
_PINNED_REF = re.compile(
    r"(?<![A-Za-z0-9._-])doc-lattice(?:==|@v)"
    r"(?P<version>\d+\.\d+\.\d+)(?![A-Za-z0-9._+-])"
)

# The live release surfaces, each with the exact number of recognized install pins it carries.
# Changing how many occurrences a document carries is an enrollment decision, so it has to be made
# here as well as in the document.
PIN_MANIFEST: Mapping[str, int] = {"README.md": 3, "MANAGED_CI.md": 5}

# Documents whose recognized pins are historical rather than live. CHANGELOG.md preserves the
# exact install refs superseded releases told adopters to run, so requiring them to equal
# __version__ would rewrite the record every release.
HISTORICAL_PIN_DOCS: AbstractSet[str] = frozenset({_CHANGELOG_NAME})


@dataclass(frozen=True)
class PinPolicy:
    """How each maintained document is classified against the release pins.

    Attributes:
        manifest: The declared release surfaces, mapping each filename to the exact number of
            recognized install pins it must carry.
        historical: Filenames whose recognized pins are preserved history, neither counted nor
            required to be current.
    """

    manifest: Mapping[str, int]
    historical: AbstractSet[str]


PIN_POLICY = PinPolicy(manifest=PIN_MANIFEST, historical=HISTORICAL_PIN_DOCS)


def maintained_documents(repo_root: Path) -> list[Path]:
    """Return the maintained documents: the sorted root Markdown files.

    Spelled here rather than imported, on purpose: this gate reads install pins and nothing
    else, and importing the link gate's selection would pull the Markdown parser into it. The
    repository's link sources are declared in ``.doc-lattice.yml`` and checked by
    ``doc-lattice links``; this selection is this script's own and is held only by its test.

    Args:
        repo_root: The repository root.

    Returns:
        Every root ``*.md`` file in sorted order. Nested directories are not sources.
    """
    return sorted(path for path in repo_root.glob(f"*{_MARKDOWN_SUFFIX}") if path.is_file())


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


def _recognized_pins(doc_text: str) -> list[str]:
    """Return every recognized pinned version in doc_text, in order of appearance.

    Repeated occurrences of one version are each returned, because the manifest counts
    occurrences rather than distinct versions.
    """
    return [match.group("version") for match in _PINNED_REF.finditer(doc_text)]


def _distinct(versions: list[str]) -> list[str]:
    """Return versions with later duplicates dropped, preserving first-appearance order."""
    return list(dict.fromkeys(versions))


def _release_surface_messages(
    doc_name: str, pins: list[str], expected_count: int, init_version: str
) -> list[str]:
    """Return the manifest violations for one declared release-surface document.

    Args:
        doc_name: The document's filename, such as ``README.md``.
        pins: Every recognized pinned version in the document, in order of appearance.
        expected_count: The exact number of recognized pins the manifest declares.
        init_version: The canonical package version.

    Returns:
        One message per distinct stale pin, followed by one count message when the number of
        recognized pins is not exactly ``expected_count``. An empty list means the document
        carries exactly the declared number of pins and every one of them is current.
    """
    messages = [
        f"{doc_name} pins doc-lattice version {stale_version}, "
        f"expected {init_version}; update the pinned install refs."
        for stale_version in _distinct(pins)
        if stale_version != init_version
    ]
    if len(pins) != expected_count:
        messages.append(
            f"{doc_name} carries {len(pins)} recognized doc-lattice install pins, "
            f"expected exactly {expected_count}; restore or remove the pin, or enroll the "
            f"new count in PIN_MANIFEST in scripts/check_version_sync.py."
        )
    return messages


def _pin_messages(
    maintained_docs: Mapping[str, str], pin_policy: PinPolicy, init_version: str
) -> list[str]:
    """Return every release-surface violation across the maintained documents.

    Each document is classified exactly once: an exempt document is skipped, a declared release
    surface is judged against its declared count and currency, and any other document carrying a
    recognized pin fails as an unclassified release surface.

    Args:
        maintained_docs: Each maintained document's filename mapped to its full text, classified
            in the mapping's insertion order.
        pin_policy: The release-surface classification to judge those documents against.
        init_version: The canonical package version.

    Returns:
        One message per violation, in document order.
    """
    messages: list[str] = []
    for doc_name, doc_text in maintained_docs.items():
        if doc_name in pin_policy.historical:
            continue
        pins = _recognized_pins(doc_text)
        if doc_name in pin_policy.manifest:
            messages.extend(
                _release_surface_messages(
                    doc_name, pins, pin_policy.manifest[doc_name], init_version
                )
            )
        elif pins:
            messages.append(
                f"{doc_name} is not a declared release surface but pins doc-lattice "
                f"{', '.join(_distinct(pins))}; enroll it in PIN_MANIFEST or exempt it in "
                f"HISTORICAL_PIN_DOCS in scripts/check_version_sync.py."
            )
    return messages


def _policy_messages(maintained_docs: Mapping[str, str], pin_policy: PinPolicy) -> list[str]:
    """Return the manifest's own errors, which are faults in the policy rather than the tree.

    Args:
        maintained_docs: Each maintained document's filename mapped to its full text.
        pin_policy: The release-surface classification to check for internal consistency.

    Returns:
        One message per document declared and exempted at once, whose exemption is applied first
        and would otherwise silence its declared count, followed by one per manifest entry with
        no matching maintained document, so deleting the document does not read as compliance.
    """
    return [
        f"{doc_name} is declared in PIN_MANIFEST and exempted in HISTORICAL_PIN_DOCS in "
        f"scripts/check_version_sync.py; the exemption is applied first, so the declared pin "
        f"count would never be checked. Keep exactly one of the two."
        for doc_name in pin_policy.manifest
        if doc_name in pin_policy.historical
    ] + [
        f"{doc_name} is declared in PIN_MANIFEST but is not a maintained document; "
        f"restore the document or drop its manifest entry in scripts/check_version_sync.py."
        for doc_name in pin_policy.manifest
        if doc_name not in maintained_docs
    ]


def check_version_consistency(
    init_version: str,
    pyproject_text: str,
    changelog_text: str,
    maintained_docs: Mapping[str, str],
    pin_policy: PinPolicy = PIN_POLICY,
) -> list[str]:
    """Return a message for each version source that disagrees with init_version.

    Args:
        init_version: The canonical package version, ``doc_lattice.__version__``.
        pyproject_text: The full text of ``pyproject.toml``.
        changelog_text: The full text of ``CHANGELOG.md``.
        maintained_docs: Every maintained document, mapping each filename such as ``README.md``
            to that document's full text. Documents are classified in the mapping's insertion
            order, and one left out of the mapping is not scanned at all.
        pin_policy: The release-surface classification to judge those documents against.

    Returns:
        One message per disagreeing source, naming the file and the expected value. An empty
        list means every source agrees. A source that cannot be parsed is reported as a mismatch
        rather than raising. The release-surface violations ``_pin_messages`` finds follow the
        two version checks, and the policy errors ``_policy_messages`` finds follow those.
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
    messages.extend(_pin_messages(maintained_docs, pin_policy, init_version))
    messages.extend(_policy_messages(maintained_docs, pin_policy))
    return messages


def main() -> None:
    """Read every version source and exit non-zero on any disagreement."""
    pyproject_text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    maintained_docs = {
        path.name: path.read_text(encoding="utf-8") for path in maintained_documents(_REPO_ROOT)
    }
    changelog_text = maintained_docs.get(_CHANGELOG_NAME, "")
    messages = check_version_consistency(
        __version__, pyproject_text, changelog_text, maintained_docs
    )
    for message in messages:
        print(message, file=sys.stderr)
    sys.exit(1 if messages else 0)


if __name__ == "__main__":
    main()
