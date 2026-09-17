"""Enforce declared coverage against the targets enrolled by validated assembly.

Selection and exemptions are fresh on every load. Neither can enroll a document or waive a
filesystem refusal, and exemptions compare exact spellings before alias deduplication.
"""

import stat
from collections.abc import Collection
from pathlib import Path

from .config import SidecarCoverage
from .error_types import CoverageError
from .path_selection import SelectionPolicy, select_paths
from .path_utils import format_path_for_display, safe_resolve

_POLICY = SelectionPolicy(
    key="sidecar_coverage.select",
    purpose="sidecar coverage policy",
    error_type=CoverageError,
    refuse_symlink_directories=True,
)
_REMEDY = "register this file or add an exact sidecar_coverage.exempt entry with a reason"


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
            or an exemption matches no selected spelling.
    """
    selected = select_paths(project_root, coverage.select, policy=_POLICY)
    exempt = {entry.path for entry in coverage.exempt or ()}
    targets = set(enrolled)
    problems: list[str] = []
    for entry in selected:
        context = f"{format_path_for_display(entry.path)}: selected by " + ", ".join(
            format_path_for_display(selector) for selector in entry.selectors
        )
        candidate = project_root / entry.path
        try:
            target = safe_resolve(candidate, project_root)
            mode = target.stat().st_mode
        except (ValueError, OSError) as exc:
            problems.append(
                f"{context}; cannot resolve or inspect selected path: {exc}; "
                "repair the path or narrow sidecar_coverage.select; exemptions cannot waive "
                "invalid paths"
            )
            continue
        if not stat.S_ISREG(mode):
            problems.append(
                f"{context}; not a regular file; select regular files only; "
                "exemptions cannot waive invalid paths"
            )
        elif target not in targets and entry.path not in exempt:
            problems.append(f"{context}; not enrolled in the loaded lattice; {_REMEDY}")
    spellings = {entry.path for entry in selected}
    for path in sorted(exempt - spellings):
        problems.append(
            f"sidecar_coverage.exempt path {format_path_for_display(path)} matches no selected "
            "path; remove this stale exemption or correct its exact project-relative spelling"
        )
    if problems:
        raise CoverageError("sidecar coverage failed:\n  " + "\n  ".join(problems))
