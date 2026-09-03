"""Decide whether a release workflow should create or reuse its target tag.

A version bump normally gets exactly one chance: the tag is created only when the pre-push
source declares a different version than the release commit, so once a bump has merged, every
later push reads it as already released. That is what makes an ordinary merge a no-op and what
stops a re-push from re-releasing, and it is correct. Its cost is that a run failing *before*
the tag exists strands the version.

The re-arm token is the recovery path, and it is deliberately not a new trigger. A maintainer
adds or changes ``.release-attempt`` in the same protected merge that carries the fix; the gate
treats a token that differs between ``github.event.before`` and the release commit as an
explicit request to release the unchanged version once more. Re-arm therefore inherits the
authority that lands a version bump rather than creating a second one, which is the whole reason
it is a tracked file and not a ``workflow_dispatch`` input.

The file holds one non-blank line, ``<version> <attempt-id>``, for example ``7.0.0 fixture-fix``.
The version binds the token to the release it re-arms, so a token edited on an unrelated push
cannot release anything; the attempt id carries freshness, so a second source fix for the same
stranded version re-arms by changing it again.

What the token cannot do bounds it. Every existing-tag branch is decided before the token is
read, so it can never create, move, or replace a tag for a version that already has one; it is
consulted only when the tag is absent *and* the version is unchanged. A token that is absent,
byte-identical to the pre-push copy, or deleted in the push is not a request and yields the
ordinary no-op, so a spent token left behind after a successful release is inert and needs no
cleanup merge. A token that did change is a deliberate act and is held to it: malformed content
or a version other than the one being released fails the run rather than passing silently.

The last of those bounds is about *when* re-arm is still honest rather than about the token.
Re-arm is the one path that can tag a commit later merges have moved on to, and the release notes
come from the ``## [X.Y.Z]`` section alone -- so entries left under ``## [Unreleased]`` at the
release commit would ship inside the tag with the notes silent about them. The ordinary flow
promotes Unreleased into the versioned section, leaving the heading present and empty, so
requiring that here refuses exactly the drift re-arm introduces and nothing the ordinary path
does. The remedy is a real choice, not a formality: fold the intervening entries into the
version being re-armed, since they will ship in it, or cut a new version and leave the stranded
one behind.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_VERSION_PATH = "src/doc_lattice/__init__.py"
_VERSION_ASSIGNMENT = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
_ATTEMPT_PATH = ".release-attempt"
_ATTEMPT_TOKEN = re.compile(r"(?P<version>\d+\.\d+\.\d+)[ \t]+(?P<attempt>[A-Za-z0-9._-]+)")
_CHANGELOG_PATH = "CHANGELOG.md"
_UNRELEASED_SECTION = re.compile(
    r"^##[ \t]*\[Unreleased\].*?$(?P<body>.*?)(?=^##[ \t]*\[|\Z)",
    re.MULTILINE | re.DOTALL,
)


class GateError(RuntimeError):
    """An invalid release state or unexpected Git failure."""


def _git(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(("git", *args), check=False, capture_output=True, text=True)
    if result.returncode != 0 and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise GateError(f"git {' '.join(args)} failed: {detail}")
    return result


def _resolve_commit(ref: str) -> str:
    return _git("rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _source_at(ref: str, path: str) -> str | None:
    listing = _git("ls-tree", "--name-only", ref, "--", path)
    if not listing.stdout.strip():
        return None
    return _git("show", f"{ref}:{path}").stdout


def _version_at(ref: str, label: str, *, may_be_missing: bool = False) -> str | None:
    source = _source_at(ref, _VERSION_PATH)
    if source is None:
        if may_be_missing:
            return None
        raise GateError(f"{label} is missing {_VERSION_PATH}")
    matches = _VERSION_ASSIGNMENT.findall(source)
    if len(matches) != 1:
        raise GateError(f"{label} has a malformed version declaration in {_VERSION_PATH}")
    return matches[0]


def _pending_unreleased(ref: str) -> bool:
    # The ordinary release flow promotes `## [Unreleased]` into `## [X.Y.Z]`, so the heading is
    # present but empty at a release commit. Re-arm is the one path that can tag a commit reached
    # by later merges, and the notes are extracted from the `## [X.Y.Z]` section alone -- so
    # content sitting under Unreleased here is work that would ship inside the tag while the
    # release notes said nothing about it. An absent changelog is not this failure: the release
    # job's own extraction refuses that, before the tag, on every path.
    changelog = _source_at(ref, _CHANGELOG_PATH)
    if changelog is None:
        return False
    section = _UNRELEASED_SECTION.search(changelog)
    return section is not None and bool(section["body"].strip())


def _re_arm_attempt(current_sha: str, before_sha: str, version: str) -> str | None:
    # Freshness is a byte comparison of the two copies rather than a comparison of parsed
    # tokens, so "changed" means "edited in this push" and no historical content is ever
    # parsed. A whitespace-only edit therefore re-arms, which is harmless: the editor meant to
    # re-arm, and the version check below still has to agree before anything is released.
    current = _source_at(current_sha, _ATTEMPT_PATH)
    if current is None or current == _source_at(before_sha, _ATTEMPT_PATH):
        return None
    lines = [line.strip() for line in current.splitlines() if line.strip()]
    match = _ATTEMPT_TOKEN.fullmatch(lines[0]) if len(lines) == 1 else None
    if match is None:
        raise GateError(f"release commit has a malformed re-arm token in {_ATTEMPT_PATH}")
    if match["version"] != version:
        raise GateError(f"re-arm token names version {match['version']!r}, not {version}")
    if _pending_unreleased(current_sha):
        raise GateError(
            f"re-arm token names {version} but {_CHANGELOG_PATH} still has unreleased entries; "
            f"work landed since the bump would ship inside the tag undocumented. Fold those "
            f"entries into the '## [{version}]' section, or cut a new version instead of "
            f"re-arming."
        )
    return match["attempt"]


def _write_decision(output_path: str, *, proceed: bool, create_tag: bool) -> None:
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"proceed={str(proceed).lower()}\n")
        output.write(f"create_tag={str(create_tag).lower()}\n")


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise GateError(f"required environment variable {name} is missing")
    return value


def main() -> int:
    try:
        tag = _required_environment("TAG")
        version = _required_environment("VERSION")
        github_sha = _required_environment("GITHUB_SHA")
        github_output = _required_environment("GITHUB_OUTPUT")

        current_sha = _resolve_commit(github_sha)
        current_version = _version_at(current_sha, "current source")
        if current_version != version:
            raise GateError(f"current source declares version {current_version!r}, not {version}")

        tag_ref = f"refs/tags/{tag}"
        tag_check = _git(
            "rev-parse", "--verify", "--quiet", f"{tag_ref}^{{commit}}", allow_failure=True
        )
        if tag_check.returncode == 0:
            tagged_sha = tag_check.stdout.strip()
            tagged_version = _version_at(tag_ref, "tagged source")
            if tagged_version != version:
                raise GateError(f"tag {tag} points at version {tagged_version!r}, not {version}")
            if tagged_sha == current_sha:
                print(f"Tag {tag} already identifies this commit; retrying release work.")
                _write_decision(github_output, proceed=True, create_tag=False)
            else:
                print(f"Tag {tag} already exists at version {version}; ordinary no-op.")
                _write_decision(github_output, proceed=False, create_tag=False)
            return 0
        # `git rev-parse --verify --quiet` exits 1 when the ref does not resolve to a
        # commit, which is the ordinary "tag absent" case handled below. Any other
        # nonzero code is an unexpected git failure.
        if tag_check.returncode != 1:
            detail = tag_check.stderr.strip() or "unknown error"
            raise GateError(f"could not inspect tag {tag}: {detail}")

        before_sha = _resolve_commit(_required_environment("GITHUB_BEFORE"))
        before_version = _version_at(before_sha, "pre-push source", may_be_missing=True)
        if before_version != version:
            previous = before_version if before_version is not None else "no version"
            print(
                f"Tag {tag} is absent and the pre-push source declares {previous}; "
                "starting release work."
            )
            _write_decision(github_output, proceed=True, create_tag=True)
            return 0

        attempt = _re_arm_attempt(current_sha, before_sha, version)
        if attempt is None:
            print(
                f"Tag {tag} is absent but the pre-push source already declares {version}; "
                "ordinary no-op."
            )
            _write_decision(github_output, proceed=False, create_tag=False)
        else:
            print(
                f"Tag {tag} is absent and a fresh re-arm token names {version} attempt "
                f"{attempt!r}; starting release work."
            )
            _write_decision(github_output, proceed=True, create_tag=True)
        return 0
    except (GateError, OSError) as error:
        print(f"::error::{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
