#!/usr/bin/env python3
"""Refuse a version bump that leaves ``## [Unreleased]`` anything but present and empty.

Release notes come from the ``## [X.Y.Z]`` section alone, so an entry left under
``## [Unreleased]`` when the version is bumped ships inside that release's tag with the notes
silent about it, and then reads as part of the *next* release unless someone moves it by hand.
The ordinary release flow promotes Unreleased into the versioned section and leaves the heading
behind, present and empty, for the next cycle. This guard holds a bump pull request to that shape.
AD-52 records the decision, and why a deleted heading fails as well as a leftover entry.

What a bump is
--------------
The version ``src/doc_lattice/__init__.py`` declares differs between the base ref and ``HEAD``.
Both are read with ``release_gate.version_at``, the reading that decides whether a push to
``main`` is a release, so this guard and the release gate cannot disagree about which change is a
bump. Anything else is not a bump and passes whatever Unreleased holds, since entries accumulating
there between releases is the section's whole purpose. Whether the changelog heading agrees with
the version is ``check_version_sync.py``'s question, not this one's.

What is read
------------
The changelog at ``HEAD``, the candidate commit, and never the commit that first changed the
version. A pull request may bump in one commit and add an entry in a later one, and the tag names
the final landed commit, so a check on the bump commit alone would miss work the tag includes.
In required pull-request CI ``HEAD`` is the merge commit the job checked out.

The section is read through ``changelog_section``, the notes extractor's own parser, so the
reading that passes a bump and the reading that produces its notes cannot drift. That aligns the
two readings; it does not validate the whole changelog. The parser takes the first
``## [Unreleased]`` heading, so a second one further down is not seen here.

Execution point
---------------
The required ``Code quality`` CI context, on a pull request only, against the base ref the
migration guard already fetches. There is no pre-commit hook and no base-less mode: without a
base there is no way to tell a bump from an ordinary change, and a check that guessed would either
fail every commit between releases or pass the one it exists for.

Failure kinds
-------------
A bump that leaves the section anything but present and empty, and a run that could not establish
whether the change is a bump at all, both exit 1 with different messages. An unreadable ref or a
missing or malformed version declaration is never read as permission to pass.
"""

import argparse
import sys

from extract_release_notes import changelog_section
from release_gate import CHANGELOG_PATH, UNRELEASED_HEADING, GateError, source_at, version_at

_CANDIDATE_REF = "HEAD"


def bump_message(base_version: str, head_version: str, changelog_text: str | None) -> str | None:
    """Return why a bump leaves ``## [Unreleased]`` other than present and empty, if it does.

    Args:
        base_version: The version the base ref declares.
        head_version: The version the candidate commit declares.
        changelog_text: The candidate commit's full ``CHANGELOG.md`` text, or None when the
            commit carries no changelog, which on a bump is a missing heading like any other.

    Returns:
        None when the versions agree, which is not a bump, or when the bump leaves the heading
        present and empty. Otherwise a message naming what to fix.
    """
    if base_version == head_version:
        return None
    section = (
        None if changelog_text is None else changelog_section(changelog_text, UNRELEASED_HEADING)
    )
    if section is None:
        return (
            f"this change bumps the version from {base_version} to {head_version} but "
            f"{CHANGELOG_PATH} has no '## [{UNRELEASED_HEADING}]' heading; keep the heading, "
            f"empty, above '## [{head_version}]' so the next cycle's entries have somewhere to "
            f"land."
        )
    if section:
        return (
            f"this change bumps the version from {base_version} to {head_version} but "
            f"{CHANGELOG_PATH} still has entries under '## [{UNRELEASED_HEADING}]'; the release "
            f"notes come from '## [{head_version}]' alone, so those entries would ship in the tag "
            f"undocumented. Move them into '## [{head_version}]' and leave the heading empty."
        )
    return None


def _parse_args() -> argparse.Namespace:
    """Parse the guard's arguments."""
    parser = argparse.ArgumentParser(
        description="Refuse a version bump that leaves CHANGELOG.md's '## [Unreleased]' section "
        "anything but present and empty."
    )
    parser.add_argument(
        "--base-ref",
        required=True,
        help="the ref to compare the declared version against, as required pull-request CI "
        "does against the base branch",
    )
    return parser.parse_args()


def main() -> int:
    """Run the guard against the base ref and ``HEAD``, and return the process exit status."""
    args = _parse_args()
    try:
        base_version = version_at(args.base_ref, f"base ref {args.base_ref}")
        head_version = version_at(_CANDIDATE_REF, "candidate commit")
        changelog_text = source_at(_CANDIDATE_REF, CHANGELOG_PATH)
    except GateError as error:
        print(
            f"could not establish whether this change is a version bump: {error}", file=sys.stderr
        )
        return 1
    # `version_at` only returns None when told the file may be missing, which neither call does.
    assert base_version is not None
    assert head_version is not None
    message = bump_message(base_version, head_version, changelog_text)
    if message is None:
        return 0
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
