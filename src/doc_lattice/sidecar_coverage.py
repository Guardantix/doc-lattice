"""Enforce declared coverage against the targets enrolled by validated assembly.

Selection, exclusions, and exemptions are fresh on every load. None of them can enroll a
document, and exemptions compare exact spellings before alias deduplication. An exemption waives
an obligation inside the covered corpus and cannot waive a filesystem refusal; an exclusion
prunes the walk, so what it removes is never selected, inspected, or refused at all.
"""

import stat
from collections.abc import Collection
from pathlib import Path

from .config import (
    SIDECAR_COVERAGE_EXCLUDE_KEY,
    SIDECAR_COVERAGE_EXEMPT_KEY,
    SIDECAR_COVERAGE_SELECT_KEY,
    SidecarCoverage,
)
from .error_types import CoverageError
from .path_selection import Exclusions, SelectedPath, SelectionPolicy, select_paths
from .path_utils import format_path_for_display, safe_resolve

_POLICY = SelectionPolicy(
    key=SIDECAR_COVERAGE_SELECT_KEY,
    purpose="sidecar coverage policy",
    error_type=CoverageError,
    refuse_symlink_directories=True,
    selector_note=(
        "selected by {selector}; repair the path, narrow the selector, or prune it with "
        f"{SIDECAR_COVERAGE_EXCLUDE_KEY}"
    ),
)
_REMEDY = f"register this file or add an exact {SIDECAR_COVERAGE_EXEMPT_KEY} entry with a reason"
# The remedy both invalid-path refusals end with. Beside ``_REMEDY`` for the same reason: AD-51
# treats it as one contract, so the two branches share a definition rather than a wording.
_INVALID_REMEDY = (
    f"prune it with {SIDECAR_COVERAGE_EXCLUDE_KEY}; exemptions cannot waive invalid paths"
)


def _context(entry: SelectedPath) -> str:
    """Name one selected spelling and every declaration that selected it.

    Built only for a spelling that turns out to be a problem. A configured project reaches this
    module on every load, cache hits included, and the common outcome is that every selected
    path is covered, so building the prose up front would spend a string and a join per selected
    file on every command for output nobody sees.

    Args:
        entry: One selected spelling with its sorted, unique selecting declarations.

    Returns:
        The ``'path': selected by 'selector', ...`` prefix every coverage problem opens with.
    """
    selectors = ", ".join(format_path_for_display(selector) for selector in entry.selectors)
    return f"{format_path_for_display(entry.path)}: selected by {selectors}"


def enforce_coverage(
    project_root: Path, coverage: SidecarCoverage, enrolled: Collection[Path]
) -> None:
    """Refuse selected paths absent from the loaded lattice and exact exemptions.

    Args:
        project_root: Containment root and base for selection.
        coverage: Validated configuration, whose selections have not been expanded yet.
        enrolled: Resolved targets actually enrolled during assembly, after its validation.

    Raises:
        CoverageError: If selection cannot complete, a selected path is invalid or uncovered,
            or an exemption matches no selected spelling. A declared exclusion that prunes
            nothing is not a refusal: it can only fail to prevent one, so it errs loud.
    """
    exclude = (
        Exclusions(SIDECAR_COVERAGE_EXCLUDE_KEY, tuple(entry.select for entry in coverage.exclude))
        if coverage.exclude
        else None
    )
    selected = select_paths(project_root, coverage.select, policy=_POLICY, exclude=exclude)
    exempt = {entry.path for entry in coverage.exempt or ()}
    targets = set(enrolled)
    problems: list[str] = []
    for entry in selected:
        candidate = project_root / entry.path
        try:
            target = safe_resolve(candidate, project_root)
            mode = target.stat().st_mode
        except (ValueError, OSError) as exc:
            problems.append(
                f"{_context(entry)}; cannot resolve or inspect selected path: {exc}; "
                f"repair the path, narrow {_POLICY.key}, or {_INVALID_REMEDY}"
            )
            continue
        if not stat.S_ISREG(mode):
            problems.append(
                f"{_context(entry)}; not a regular file; select regular files only, or "
                f"{_INVALID_REMEDY}"
            )
        elif target not in targets and entry.path not in exempt:
            problems.append(f"{_context(entry)}; not enrolled in the loaded lattice; {_REMEDY}")
    if exempt:
        # Only built for a project that declared an exemption. The common configuration declares
        # none, and this would otherwise spend a set over every selected path on every load to
        # subtract nothing from it.
        spellings = {entry.path for entry in selected}
        for path in sorted(exempt - spellings):
            problems.append(
                f"{SIDECAR_COVERAGE_EXEMPT_KEY} path {format_path_for_display(path)} matches no "
                "selected path; remove this stale exemption or correct its exact "
                "project-relative spelling"
            )
    if problems:
        raise CoverageError("sidecar coverage failed:\n  " + "\n  ".join(problems))
