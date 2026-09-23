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
from .path_selection import (
    Exclusions,
    SelectedPath,
    SelectionPolicy,
    SelectionRefusal,
    refusal_from_error,
    select_paths,
    selection_refusal_message,
)
from .path_utils import format_path_for_display, safe_resolve

_POLICY = SelectionPolicy(refuse_symlink_directories=True)
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


def _selection_error(refusal: SelectionRefusal) -> CoverageError:
    """Render a fatal selection refusal with coverage's keys and actionable remedy.

    Coverage owns the error type and the note, which is where its remedy reaches the terminal,
    and names the key each refusal is repaired under: an exclusion defect is reported against the
    list that carried it, not against the selection list.
    """
    key = (
        SIDECAR_COVERAGE_EXCLUDE_KEY if refusal.source == "exclude" else SIDECAR_COVERAGE_SELECT_KEY
    )
    error = CoverageError(
        selection_refusal_message(
            refusal,
            key=key,
            purpose="the sidecar coverage policy",
            exclude_key=SIDECAR_COVERAGE_EXCLUDE_KEY,
        )
    )
    if refusal.selector is not None:
        error.add_note(
            f"selected by {format_path_for_display(refusal.selector)}; repair the path, "
            f"narrow the selector, or prune it with {SIDECAR_COVERAGE_EXCLUDE_KEY}"
        )
    return error


def _traversal_problem(refusal: SelectionRefusal) -> str:
    """Describe one refused directory alongside the selected-file coverage problems."""
    selector = format_path_for_display(refusal.selector or "")
    spelling = format_path_for_display(refusal.spelling)
    return (
        f"{spelling}: selected by {selector}; {SIDECAR_COVERAGE_SELECT_KEY} selection refuses "
        "to traverse symlinked directory; repair the path, narrow "
        f"{SIDECAR_COVERAGE_SELECT_KEY}, or prune it with {SIDECAR_COVERAGE_EXCLUDE_KEY}"
    )


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
        Exclusions(tuple(entry.select for entry in coverage.exclude)) if coverage.exclude else None
    )
    try:
        selection = select_paths(project_root, coverage.select, policy=_POLICY, exclude=exclude)
    except ValueError as exc:
        raise _selection_error(refusal_from_error(exc)) from exc
    selected = selection.paths
    exempt = {entry.path for entry in coverage.exempt or ()}
    targets = set(enrolled)
    problems = [
        (refusal.spelling, _traversal_problem(refusal))
        for refusal in selection.refusals
        if refusal.kind == "symlink-directory"
    ]
    for entry in selected:
        candidate = project_root / entry.path
        try:
            target = safe_resolve(candidate, resolved_root=selection.resolved_root)
            mode = target.stat().st_mode
        except (ValueError, OSError) as exc:
            problems.append(
                (
                    entry.path,
                    f"{_context(entry)}; cannot resolve or inspect selected path: {exc}; "
                    f"repair the path, narrow {SIDECAR_COVERAGE_SELECT_KEY}, or {_INVALID_REMEDY}",
                )
            )
            continue
        if not stat.S_ISREG(mode):
            problems.append(
                (
                    entry.path,
                    f"{_context(entry)}; not a regular file; select regular files only, or "
                    f"{_INVALID_REMEDY}",
                )
            )
        elif target not in targets and entry.path not in exempt:
            problems.append(
                (entry.path, f"{_context(entry)}; not enrolled in the loaded lattice; {_REMEDY}")
            )
    ordered_problems = [message for _, message in sorted(problems)]
    ordered_problems.extend(
        str(_selection_error(refusal))
        for refusal in selection.refusals
        if refusal.kind == "no-match"
    )
    if exempt:
        # Only built for a project that declared an exemption. The common configuration declares
        # none, and this would otherwise spend a set over every selected path on every load to
        # subtract nothing from it.
        spellings = {entry.path for entry in selected}
        for path in sorted(exempt - spellings):
            ordered_problems.append(
                f"{SIDECAR_COVERAGE_EXEMPT_KEY} path {format_path_for_display(path)} matches "
                "no selected path; remove this stale exemption or correct its exact "
                "project-relative spelling"
            )
    if ordered_problems:
        raise CoverageError("sidecar coverage failed:\n  " + "\n  ".join(ordered_problems))
