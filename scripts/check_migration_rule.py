#!/usr/bin/env python3
"""Enforce the RELEASING.md rule that changed adopter output carries a ``### Migration`` note.

The rule this guard mechanizes is step 4 of the release checklist: a release that changes output
an adopter installs must describe the adopter-visible steps in a ``### Migration`` subsection of
its CHANGELOG.md section. Until now that was prose, so the one moment it matters -- a release
under time pressure -- is the moment nothing was checking it.

What is covered
---------------
Two groups of generated output, read where each one actually lives. The ``init`` blocks come from
the renderers in ``doc_lattice.scaffold``, called here rather than copied, so the guard sees the
same text an adopter is handed. The MANAGED_CI.md blocks are extracted from the document, because
AD-32 retired the managed commands: no init mode regenerates the trusted Linear workflow or the
``gh`` procedure, and the document is their only source. Together they are the surfaces step 4
names.

Normalization
-------------
Only the routine per-release version-pin substitution. The renderers are called with the sentinel
version ``0.0.0``, and every ``doc-lattice==X.Y.Z`` occurrence extracted from MANAGED_CI.md is
rewritten to ``doc-lattice==0.0.0``. That is exactly the change step 4 exempts: every release
performs it, so leaving it in the snapshot would make the subsection mandatory on every release
and the guard would teach nothing. Nothing else is normalized -- whitespace, ordering, and
wording are all part of what an adopter copies.

Baseline authority
------------------
The comparison runs against ``scripts/migration_baseline.json``, a committed snapshot. A golden
file that any change may freely rewrite is not self-authenticating: a renderer and its golden
updated in the same commit read as equal, which is the failure mode this guard exists to catch.
The baseline is therefore bound two ways. Offline, everywhere it runs, its stamp must equal the
first versioned CHANGELOG heading, which forces the rollover into the release commit rather than
letting it happen mid-cycle. And in required pull-request CI the baseline is additionally compared
against the base ref, so a baseline whose content changed without a version promotion fails, as
does a promoted section that changed content without carrying ``### Migration``.

The rollover is one transaction: the release commit that promotes ``## [Unreleased]`` to
``## [X.Y.Z]`` also runs ``scripts/check_migration_rule.py --update`` and commits the regenerated
baseline. Mid-cycle, a change to generated output is authorized by writing the ``### Migration``
subsection under ``## [Unreleased]``, never by advancing the baseline.

Branch coverage
---------------
``render_ci`` takes the adopting repository's default branch, so its output is a family rather
than one string. The snapshot holds a representative matrix -- ``main``, ``master``, ``develop``,
one slashed name (``release/2.x``), and one YAML-1.1 boolean-like name (``on``, which the renderer
must quote) -- which covers the shapes the renderer treats differently. It is an approximation and
not exhaustive: a change confined to some branch shape outside the matrix is not seen.

Execution point
---------------
The required ``Code quality`` CI context is the authority. It runs both halves on a pull request,
against the merge commit and the base ref. The pre-commit hook runs the offline half only, for
early feedback; it is opt-in and inert in a fresh clone, so it is a mirror and never the gate. No
release-job re-assertion is needed: ``Code quality`` also runs on the push to ``main`` that is the
release commit, and the stamp check is what carries the release-only semantics.

What this does not close
------------------------
Edits to this script itself, including its surface list and its normalization, which review owns
exactly as it owns every sibling gate. Branch shapes outside the matrix above. And key additions
or removals between the base and HEAD baselines are deliberately not gated: a key only appears or
disappears alongside an edit to the surface list here, which is a reviewed change, while the
offline per-key comparison still forces the committed baseline to match what this script computes
today.
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from doc_lattice.scaffold import render_ci, render_gitignore, render_precommit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _REPO_ROOT / "scripts" / "migration_baseline.json"
_BASELINE_REPO_PATH = "scripts/migration_baseline.json"
_CHANGELOG_REPO_PATH = "CHANGELOG.md"
_MANAGED_CI_NAME = "MANAGED_CI.md"
_UPDATE_HINT = "python scripts/check_migration_rule.py --update"

# The sentinel every rendered and extracted pin is normalized to. See "Normalization" above.
SENTINEL_VERSION = "0.0.0"

# The representative default-branch matrix. See "Branch coverage" above.
CI_BRANCHES: tuple[str, ...] = ("main", "master", "develop", "release/2.x", "on")

_VERSION_HEADING = re.compile(r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)
_PINNED_REF = re.compile(r"(?<![A-Za-z0-9._-])doc-lattice==\d+\.\d+\.\d+(?![A-Za-z0-9._+-])")
_UNRELEASED_HEADING = "## [Unreleased]"
_MIGRATION_HEADING = "### Migration"
_WORKFLOW_MARKER = "name: doc-lattice Linear\n"
_ENVIRONMENT_SECTION = "### 3. Create the protected environment"
_SECRET_SECTION = "### 4. Set the environment secret and remove repository-scoped copies"
_BLOCK_DIVIDER = "\n---\n"


class SurfaceError(RuntimeError):
    """A guarded surface could not be extracted from the document that owns it."""


@dataclass(frozen=True)
class BaseState:
    """The base ref's view of the two files the pull-request half compares against.

    Attributes:
        baseline_text: The baseline file's text at the base ref, or None when the file did not
            exist there, which is the guard's own introduction.
        changelog_text: CHANGELOG.md's text at the base ref.
    """

    baseline_text: str | None
    changelog_text: str


def normalize_pins(text: str) -> str:
    """Return text with every recognized ``doc-lattice==X.Y.Z`` pin set to the sentinel.

    Args:
        text: The block text to normalize.

    Returns:
        The same text with the routine per-release pin substitution removed.
    """
    return _PINNED_REF.sub(f"doc-lattice=={SENTINEL_VERSION}", text)


def _fenced_blocks(text: str, language: str) -> list[str]:
    """Return the body of every fenced block written in one language, in document order.

    The scan is line-based and deliberately literal: MANAGED_CI.md writes fences as
    ``` followed by the language, and a block runs to the next closing fence at the same
    indentation-insensitive spelling.

    Args:
        text: The document, or one section of it, to scan.
        language: The info string a fence must open with, such as ``bash``.

    Returns:
        Each matching block's body, newline-terminated as written.
    """
    blocks: list[str] = []
    body: list[str] | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if body is None:
            if stripped == f"```{language}":
                body = []
        elif stripped == "```":
            blocks.append("".join(body))
            body = None
        else:
            body.append(line)
    return blocks


def _section(text: str, heading: str) -> str:
    """Return one document section, from its heading line to the next heading at or above it.

    Args:
        text: The full document.
        heading: The exact heading line the section starts at.

    Returns:
        The section text, heading line included.

    Raises:
        SurfaceError: If the heading does not appear in the document.
    """
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        raise SurfaceError(
            f"{_MANAGED_CI_NAME} no longer carries the section {heading!r}; the guard in "
            f"scripts/check_migration_rule.py cannot locate the block it protects."
        )
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith(("### ", "## "))
        ),
        len(lines),
    )
    return "".join(lines[start:end])


def _published_workflow(managed_ci_text: str) -> str:
    """Return the single fenced YAML block publishing the trusted Linear workflow.

    Args:
        managed_ci_text: The full text of MANAGED_CI.md.

    Returns:
        The block body.

    Raises:
        SurfaceError: If the document does not publish exactly one such block.
    """
    blocks = [
        block
        for block in _fenced_blocks(managed_ci_text, "yaml")
        if block.startswith(_WORKFLOW_MARKER)
    ]
    if len(blocks) != 1:
        raise SurfaceError(
            f"{_MANAGED_CI_NAME} must publish exactly one trusted Linear workflow block, "
            f"found {len(blocks)}; the guard in scripts/check_migration_rule.py cannot locate "
            f"the block it protects."
        )
    return blocks[0]


def _joined_bash(managed_ci_text: str, heading: str) -> str:
    """Return one section's ``bash`` blocks joined by a divider, pins normalized.

    Args:
        managed_ci_text: The full text of MANAGED_CI.md.
        heading: The exact heading line the section starts at.

    Returns:
        Every ``bash`` block in that section, in document order, joined by a divider line.

    Raises:
        SurfaceError: If the section is missing or carries no shell block at all.
    """
    blocks = _fenced_blocks(_section(managed_ci_text, heading), "bash")
    if not blocks:
        raise SurfaceError(
            f"{_MANAGED_CI_NAME} section {heading!r} carries no bash block; the guard in "
            f"scripts/check_migration_rule.py cannot locate the procedure it protects."
        )
    return normalize_pins(_BLOCK_DIVIDER.join(blocks))


def compute_surfaces(managed_ci_text: str) -> dict[str, str]:
    """Return the normalized snapshot of every guarded surface.

    Args:
        managed_ci_text: The full text of MANAGED_CI.md, which owns the recipe blocks.

    Returns:
        Each surface's stable key mapped to its normalized text.

    Raises:
        SurfaceError: If a guarded block cannot be located in MANAGED_CI.md.
    """
    surfaces = {
        "init.gitignore": render_gitignore(),
        "init.precommit": render_precommit(SENTINEL_VERSION),
    }
    for branch in CI_BRANCHES:
        surfaces[f"init.ci[{branch}]"] = render_ci(SENTINEL_VERSION, default_branch=branch)
    surfaces["managed-ci.linear-workflow"] = normalize_pins(_published_workflow(managed_ci_text))
    surfaces["managed-ci.gh-environment"] = _joined_bash(managed_ci_text, _ENVIRONMENT_SECTION)
    surfaces["managed-ci.gh-secret"] = _joined_bash(managed_ci_text, _SECRET_SECTION)
    return surfaces


def latest_released_version(changelog_text: str) -> str | None:
    """Return the first versioned ``## [X.Y.Z]`` heading, or None when there is none.

    ``## [Unreleased]`` does not match and is skipped, so the value is the latest release.

    Args:
        changelog_text: The full text of CHANGELOG.md.

    Returns:
        The version string, or None.
    """
    match = _VERSION_HEADING.search(changelog_text)
    return match.group("version") if match else None


def _changelog_section(changelog_text: str, heading_line: str) -> str | None:
    """Return the changelog section starting at one exact ``## `` heading line, or None."""
    lines = changelog_text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.rstrip() == heading_line), None)
    if start is None:
        return None
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _unreleased_section(changelog_text: str) -> str | None:
    """Return the ``## [Unreleased]`` section text, or None when there is none."""
    return _changelog_section(changelog_text, _UNRELEASED_HEADING)


def _first_versioned_section(changelog_text: str) -> str | None:
    """Return the first versioned release section's text, or None when there is none."""
    version = latest_released_version(changelog_text)
    if version is None:
        return None
    lines = changelog_text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if _VERSION_HEADING.match(line) and f"[{version}]" in line
        ),
        None,
    )
    if start is None:
        return None
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _has_migration(section_text: str | None) -> bool:
    """Report whether a changelog section carries a ``### Migration`` subsection."""
    if section_text is None:
        return False
    return any(line.rstrip() == _MIGRATION_HEADING for line in section_text.splitlines())


def parse_baseline(baseline_text: str | None) -> tuple[str, dict[str, str]] | None:
    """Return the baseline's stamp and surfaces, or None when it is missing or malformed.

    The parse validates the documented shape rather than trusting it, since the file is JSON
    read from disk or from a git object and nothing else constrains what it holds.

    Args:
        baseline_text: The baseline file's text, or None when the file is absent.

    Returns:
        A ``(version, surfaces)`` pair, or None when the text is absent, is not JSON, or does
        not carry a string version and a string-to-string surfaces mapping.
    """
    if baseline_text is None:
        return None
    try:
        loaded = json.loads(baseline_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    version = loaded.get("version")
    raw_surfaces = loaded.get("surfaces")
    if not isinstance(version, str) or not isinstance(raw_surfaces, dict):
        return None
    surfaces: dict[str, str] = {}
    for key, value in raw_surfaces.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        surfaces[key] = value
    return version, surfaces


def _differing_keys(current: dict[str, str], recorded: dict[str, str]) -> list[str]:
    """Return every key whose text differs between two snapshots, missing keys included."""
    return sorted(
        key for key in set(current) | set(recorded) if current.get(key) != recorded.get(key)
    )


def render_baseline(version: str, surfaces: dict[str, str]) -> str:
    """Return the baseline file's exact text for one stamp and snapshot.

    Args:
        version: The latest released version to stamp the baseline with.
        surfaces: The normalized snapshot to record.

    Returns:
        The serialized baseline, sorted and newline-terminated.
    """
    payload = {"version": version, "surfaces": surfaces}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _offline_messages(
    current_surfaces: dict[str, str], baseline_text: str | None, changelog_text: str
) -> list[str]:
    """Return the violations visible without a base ref. See ``check_migration_rule``."""
    parsed = parse_baseline(baseline_text)
    if parsed is None:
        return [
            f"{_BASELINE_REPO_PATH} is missing or malformed; regenerate it with "
            f"`{_UPDATE_HINT}` and commit the result."
        ]
    baseline_version, baseline_surfaces = parsed
    messages: list[str] = []
    released = latest_released_version(changelog_text)
    if baseline_version != released:
        messages.append(
            f"{_BASELINE_REPO_PATH} is stamped {baseline_version!r} but the latest released "
            f"CHANGELOG.md heading is {released!r}; the baseline is regenerated with "
            f"`{_UPDATE_HINT}` in the release commit that promotes '## [Unreleased]', never "
            f"mid-cycle."
        )
    changed = _differing_keys(current_surfaces, baseline_surfaces)
    if changed and not _has_migration(_unreleased_section(changelog_text)):
        messages.append(
            f"generated output an adopter installs changed ({', '.join(changed)}) but "
            f"CHANGELOG.md's '{_UNRELEASED_HEADING}' section carries no "
            f"'{_MIGRATION_HEADING}' subsection; add one describing the adopter-visible steps. "
            f"A false positive is answered by writing the subsection, not by editing "
            f"{_BASELINE_REPO_PATH}."
        )
    return messages


def _base_ref_messages(
    baseline_text: str | None, changelog_text: str, base_state: BaseState
) -> list[str]:
    """Return the violations only a comparison against the base ref can see."""
    base_parsed = parse_baseline(base_state.baseline_text)
    if base_parsed is None:
        # The baseline did not exist at the base ref, or was unreadable there. Either way this
        # change is the guard's introduction and there is nothing to compare against.
        return []
    head_parsed = parse_baseline(baseline_text)
    if head_parsed is None:
        # The offline half already reports the malformed baseline in full.
        return []
    base_version, base_surfaces = base_parsed
    head_version, head_surfaces = head_parsed
    shared = sorted(set(base_surfaces) & set(head_surfaces))
    content_changed = sorted(key for key in shared if base_surfaces[key] != head_surfaces[key])
    stamp_changed = base_version != head_version
    if not content_changed and not stamp_changed:
        return []
    released_head = latest_released_version(changelog_text)
    released_base = latest_released_version(base_state.changelog_text)
    if released_head == released_base:
        return [
            f"{_BASELINE_REPO_PATH} changed without a version promotion in the same change; "
            f"revert the baseline edit. The baseline may only advance in a release commit, and "
            f"a mid-cycle change to generated output is authorized by a "
            f"'{_MIGRATION_HEADING}' subsection under '{_UNRELEASED_HEADING}', never by "
            f"advancing the baseline."
        ]
    if content_changed and not _has_migration(_first_versioned_section(changelog_text)):
        return [
            f"the release promoted to '## [{released_head}]' changes generated output "
            f"({', '.join(content_changed)}) but its CHANGELOG.md section carries no "
            f"'{_MIGRATION_HEADING}' subsection; add one describing the adopter-visible steps."
        ]
    return []


def check_migration_rule(
    current_surfaces: dict[str, str],
    baseline_text: str | None,
    changelog_text: str,
    base_state: BaseState | None = None,
) -> list[str]:
    """Return one message per violation of the migration-subsection release rule.

    Offline, the baseline must parse, must be stamped with the latest released version, and must
    match the surfaces computed from the working tree; a surface that differs requires a
    ``### Migration`` subsection under ``## [Unreleased]``.

    Given a base ref, the committed baseline is additionally compared against the base's. Any
    difference -- stamp or content -- requires a version promotion in the same change, and a
    content difference additionally requires the promoted section to carry ``### Migration``. A
    stamp-only advance is a pin-only release and needs no subsection. A baseline absent at the
    base ref skips this half, since that change is the guard's own introduction.

    Args:
        current_surfaces: The snapshot computed from the working tree.
        baseline_text: The committed baseline's text, or None when the file is absent.
        changelog_text: The full text of CHANGELOG.md at HEAD.
        base_state: The base ref's baseline and changelog, or None for the offline half alone.

    Returns:
        One message per violation, offline messages first. An empty list means the rule holds.
    """
    messages = _offline_messages(current_surfaces, baseline_text, changelog_text)
    if base_state is not None:
        messages.extend(_base_ref_messages(baseline_text, changelog_text, base_state))
    return messages


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command, returning the completed process without raising on failure."""
    return subprocess.run(("git", *args), check=False, capture_output=True, text=True)


def _show(ref: str, repo_path: str) -> str | None:
    """Return one path's text at a ref, or None when the ref does not carry that path.

    Args:
        ref: The git ref to read from.
        repo_path: The repository-relative path to read.

    Returns:
        The file's text, or None when the path is absent at that ref.

    Raises:
        SurfaceError: If git fails for a reason other than the path being absent.
    """
    listing = _git("ls-tree", "--name-only", ref, "--", repo_path)
    if listing.returncode != 0:
        detail = listing.stderr.strip() or "unknown error"
        raise SurfaceError(f"git ls-tree {ref} -- {repo_path} failed: {detail}")
    if not listing.stdout.strip():
        return None
    content = _git("show", f"{ref}:{repo_path}")
    if content.returncode != 0:
        detail = content.stderr.strip() or "unknown error"
        raise SurfaceError(f"git show {ref}:{repo_path} failed: {detail}")
    return content.stdout


def _base_state(ref: str) -> BaseState:
    """Build the base ref's view of the baseline and the changelog.

    Args:
        ref: The base ref to read.

    Returns:
        The base state. A missing baseline is recorded as None; a missing changelog is a failure,
        since a ref without one cannot be a base this repository merges onto.

    Raises:
        SurfaceError: If the changelog is absent at that ref, or git fails.
    """
    changelog_text = _show(ref, _CHANGELOG_REPO_PATH)
    if changelog_text is None:
        raise SurfaceError(f"{_CHANGELOG_REPO_PATH} is missing at {ref}")
    return BaseState(baseline_text=_show(ref, _BASELINE_REPO_PATH), changelog_text=changelog_text)


def _read(path: Path) -> str | None:
    """Return a file's text, or None when it does not exist."""
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _parse_args() -> argparse.Namespace:
    """Parse the guard's arguments."""
    parser = argparse.ArgumentParser(
        description="Enforce the CHANGELOG.md '### Migration' rule for changed adopter output."
    )
    parser.add_argument(
        "--base-ref",
        help="also compare the committed baseline against this ref, as required pull-request CI "
        "does against the base branch",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate scripts/migration_baseline.json; run this in the release commit that "
        "promotes '## [Unreleased]'",
    )
    return parser.parse_args()


def main() -> int:
    """Run the guard, or regenerate the baseline, and return the process exit status."""
    args = _parse_args()
    if args.update and args.base_ref:
        print("--update and --base-ref are mutually exclusive", file=sys.stderr)
        return 2
    changelog_text = (_REPO_ROOT / _CHANGELOG_REPO_PATH).read_text(encoding="utf-8")
    managed_ci_text = (_REPO_ROOT / _MANAGED_CI_NAME).read_text(encoding="utf-8")
    try:
        surfaces = compute_surfaces(managed_ci_text)
        if args.update:
            released = latest_released_version(changelog_text)
            if released is None:
                print(
                    f"{_CHANGELOG_REPO_PATH} carries no versioned '## [X.Y.Z]' heading to stamp "
                    f"the baseline with.",
                    file=sys.stderr,
                )
                return 1
            _BASELINE_PATH.write_text(render_baseline(released, surfaces), encoding="utf-8")
            print(
                f"wrote {_BASELINE_REPO_PATH} stamped {released} with {len(surfaces)} surfaces",
                file=sys.stderr,
            )
            return 0
        base_state = _base_state(args.base_ref) if args.base_ref else None
    except SurfaceError as error:
        print(str(error), file=sys.stderr)
        return 1
    messages = check_migration_rule(surfaces, _read(_BASELINE_PATH), changelog_text, base_state)
    for message in messages:
        print(message, file=sys.stderr)
    return 1 if messages else 0


if __name__ == "__main__":
    sys.exit(main())
