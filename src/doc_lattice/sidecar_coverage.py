"""Enforce declared coverage against the targets enrolled by validated assembly.

Selection, exclusions, and exemptions are fresh on every load. None of them can enroll a
document, and exemptions compare exact spellings before alias deduplication. An exemption waives
an obligation inside the covered corpus and cannot waive a filesystem refusal; an exclusion
prunes the walk, so what it removes is never selected, inspected, or refused at all.
"""

import stat
from collections.abc import Collection
from pathlib import Path

from .config import SidecarCoverage
from .error_types import CoverageError
from .path_selection import Exclusions, SelectedPath, SelectionPolicy, select_paths
from .path_utils import format_path_for_display, safe_resolve

_EXCLUDE_KEY = "sidecar_coverage.exclude"
_POLICY = SelectionPolicy(
    key="sidecar_coverage.select",
    purpose="sidecar coverage policy",
    error_type=CoverageError,
    refuse_symlink_directories=True,
    selector_note=(
        "selected by {selector}; repair the path, narrow the selector, or prune it with "
        f"{_EXCLUDE_KEY}"
    ),
)
_REMEDY = "register this file or add an exact sidecar_coverage.exempt entry with a reason"


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
        Exclusions(_EXCLUDE_KEY, tuple(entry.select for entry in coverage.exclude))
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
                f"repair the path, narrow sidecar_coverage.select, or prune it with "
                f"{_EXCLUDE_KEY}; exemptions cannot waive invalid paths"
            )
            continue
        if not stat.S_ISREG(mode):
            problems.append(
                f"{_context(entry)}; not a regular file; select regular files only, or "
                f"prune it with {_EXCLUDE_KEY}; exemptions cannot waive invalid paths"
            )
        elif target not in targets and entry.path not in exempt:
            problems.append(f"{_context(entry)}; not enrolled in the loaded lattice; {_REMEDY}")
    spellings = {entry.path for entry in selected}
    for path in sorted(exempt - spellings):
        problems.append(
            f"sidecar_coverage.exempt path {format_path_for_display(path)} matches no selected "
            "path; remove this stale exemption or correct its exact project-relative spelling"
        )
    if problems:
        raise CoverageError("sidecar coverage failed:\n  " + "\n  ".join(problems))
