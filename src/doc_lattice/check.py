"""Classify every derives_from edge against its locked seen hash."""

from dataclasses import dataclass

from .constants import EdgeState
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


def statuses_json(statuses: list[EdgeStatus]) -> dict:
    """Build the JSON-ready check report payload.

    Args:
        statuses: Edge classifications to serialize.

    Returns:
        A plain dictionary containing the ordered edge payloads.
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
        ]
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
