"""Classify every derives_from edge against its locked seen hash."""

from collections.abc import Mapping
from dataclasses import dataclass

from .constants import EDGE_STATES, EdgeState
from .model import Edge, Lattice, TargetId
from .resolve import cached_target_hash


@dataclass(frozen=True, slots=True)
class EdgeStatus:
    """The classification of one edge."""

    source_id: str
    target_ref: str
    target_id: TargetId | None
    state: EdgeState
    expected: str | None
    actual: str | None


def summarize_statuses(statuses: list[EdgeStatus]) -> dict[EdgeState, int]:
    """Count edges per state, including the states that did not occur.

    Every member of ``EdgeState`` is present with at least a zero count, keyed in Literal
    declaration order, so a consumer never has to distinguish "absent" from "none found".

    Args:
        statuses: Edge classifications to count. Pass the unfiltered output of
            ``check_lattice``; a display filter such as ``--only`` must not reach this.

    Returns:
        A per-state count of the given classifications.
    """
    summary: dict[EdgeState, int] = dict.fromkeys(EDGE_STATES, 0)
    for status in statuses:
        summary[status.state] += 1
    return summary


def statuses_json(statuses: list[EdgeStatus], summary: Mapping[EdgeState, int]) -> dict:
    """Build the JSON-ready check report payload.

    Args:
        statuses: Edge classifications to serialize.
        summary: Per-state counts from ``summarize_statuses``. It is passed separately
            because it counts every classified edge while ``statuses`` may already be
            narrowed to the displayed subset, so under a display filter the summary counts
            deliberately do not sum to ``len(edges)``.

    Returns:
        A plain dictionary containing the ordered edge payloads and the per-state summary.
    """
    return {
        "edges": [
            {
                "source_id": status.source_id,
                "target_ref": status.target_ref,
                "target_id": status.target_id.as_ref() if status.target_id else None,
                "state": status.state,
                "expected": status.expected,
                "actual": status.actual,
            }
            for status in statuses
        ],
        # get(state, 0) rather than indexing: the parameter is a Mapping, so a caller may hand
        # over a sparse counter that simply omits a state that did not occur. The payload
        # promises every state, and a missing key is that state at zero, not a KeyError.
        "summary": {state: summary.get(state, 0) for state in EDGE_STATES},
    }


def check_lattice(lattice: Lattice) -> list[EdgeStatus]:
    """Classify every edge in the lattice.

    Args:
        lattice: The built lattice.

    Returns:
        One EdgeStatus per edge, in node-id then edge order.
    """
    statuses: list[EdgeStatus] = []
    cache: dict[TargetId, str] = {}
    for node_id in sorted(lattice.nodes_by_id):
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            statuses.append(_classify(lattice, node_id, edge, cache))
    return statuses


def _classify(
    lattice: Lattice, source_id: str, edge: Edge, cache: dict[TargetId, str]
) -> EdgeStatus:
    """Classify one edge as BROKEN, UNRECONCILED, STALE, or OK.

    A broken edge (no resolved target) is BROKEN. Otherwise the live target hash is
    compared against ``seen``: a missing ``seen`` is UNRECONCILED, a mismatch is STALE, and
    a match is OK.
    """
    if edge.target_id is None:
        return EdgeStatus(source_id, edge.target_ref, None, "BROKEN", edge.seen, None)
    actual = cached_target_hash(lattice, edge.target_id, cache)
    if edge.seen is None:
        return EdgeStatus(source_id, edge.target_ref, edge.target_id, "UNRECONCILED", None, actual)
    state: EdgeState = "OK" if actual == edge.seen else "STALE"
    return EdgeStatus(source_id, edge.target_ref, edge.target_id, state, edge.seen, actual)


def has_drift(statuses: list[EdgeStatus]) -> bool:
    """Return True if any edge is not OK.

    Args:
        statuses: Output of ``check_lattice``.

    Returns:
        True when any edge is STALE, UNRECONCILED, or BROKEN.
    """
    return any(s.state != "OK" for s in statuses)
