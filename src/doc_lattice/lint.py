"""Validate the authority ladder over derives_from edges. Pure: no I/O."""

from dataclasses import dataclass

from .constants import AUTHORITY_LADDER, Authority, SkipReason
from .model import Lattice, TargetId
from .resolve import node_for_path


@dataclass(frozen=True, slots=True)
class LadderViolation:
    """One derives_from edge that inverts the authority ladder."""

    source_id: str
    source_authority: Authority
    target_id: TargetId
    target_ref: str
    target_authority: Authority


@dataclass(frozen=True, slots=True)
class SkippedEdge:
    """One edge the ladder could not judge because an endpoint lacks authority."""

    source_id: str
    target_ref: str
    target_id: TargetId
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class LintResult:
    """Violations that fail the gate, plus the unjudged skips."""

    violations: tuple[LadderViolation, ...]
    skipped: tuple[SkippedEdge, ...]


def lint_json(result: LintResult) -> dict:
    """Build the JSON-ready authority-lint report payload.

    Args:
        result: Authority-lint violations and skipped edges to serialize.

    Returns:
        A plain dictionary containing ordered violation and skipped-edge payloads.
    """
    return {
        "violations": [
            {
                "source_id": violation.source_id,
                "source_authority": violation.source_authority,
                "target_id": violation.target_id.as_ref(),
                "target_ref": violation.target_ref,
                "target_authority": violation.target_authority,
            }
            for violation in result.violations
        ],
        "skipped": [
            {
                "source_id": skipped.source_id,
                "target_ref": skipped.target_ref,
                "target_id": skipped.target_id.as_ref(),
                "reason": skipped.reason,
            }
            for skipped in result.skipped
        ],
    }


def _rank(authority: Authority) -> int:
    """Return the ladder position of an authority; higher means stronger."""
    return AUTHORITY_LADDER.index(authority)


def _target_authority(lattice: Lattice, target_id: TargetId) -> Authority | None:
    """Return the authority of the file node that owns a resolved target id.

    A section anchor inherits the authority of the file that owns it, so both file
    and section targets resolve through the same path index.
    """
    location = lattice.index[target_id]
    return node_for_path(lattice, location.path).authority


def lint_lattice(lattice: Lattice) -> LintResult:
    """Classify every edge as a violation, a skip, or a silent pass.

    Walks nodes in id order and each node's edges in order. A broken edge is left to
    ``check``. An edge with an unannotated endpoint is recorded as a skip. Otherwise a
    target weaker than its source is a violation.

    Args:
        lattice: The built lattice.

    Returns:
        The violations and skips, both in node-id then edge order.
    """
    violations: list[LadderViolation] = []
    skipped: list[SkippedEdge] = []
    for node_id in sorted(lattice.nodes_by_id):
        node = lattice.nodes_by_id[node_id]
        source_authority = node.authority
        for edge in node.derives_from:
            target_id = edge.target_id
            if target_id is None:
                continue  # broken edge: reported by check, not counted here
            target_authority = _target_authority(lattice, target_id)
            if source_authority is None:
                skipped.append(
                    SkippedEdge(node_id, edge.target_ref, target_id, "source-unannotated")
                )
                continue
            if target_authority is None:
                skipped.append(
                    SkippedEdge(node_id, edge.target_ref, target_id, "target-unannotated")
                )
                continue
            if _rank(target_authority) < _rank(source_authority):
                violations.append(
                    LadderViolation(
                        source_id=node_id,
                        source_authority=source_authority,
                        target_id=target_id,
                        target_ref=edge.target_ref,
                        target_authority=target_authority,
                    )
                )
    return LintResult(violations=tuple(violations), skipped=tuple(skipped))
