"""Classify every derives_from edge against its locked seen hash."""

from dataclasses import dataclass

from .constants import EdgeState
from .hashing import content_hash
from .model import Lattice
from .resolve import target_content


@dataclass(frozen=True, slots=True)
class EdgeStatus:
    """The classification of one edge."""

    source_id: str
    target_ref: str
    target_id: str | None
    state: EdgeState
    expected: str | None
    actual: str | None


def check_lattice(lattice: Lattice) -> list[EdgeStatus]:
    """Classify every edge in the lattice.

    Args:
        lattice: The built lattice.

    Returns:
        One EdgeStatus per edge, in node-id then edge order.
    """
    statuses: list[EdgeStatus] = []
    for node_id in sorted(lattice.nodes_by_id):
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            statuses.append(_classify(lattice, node_id, edge.target_ref, edge.target_id, edge.seen))
    return statuses


def _classify(
    lattice: Lattice, source_id: str, target_ref: str, target_id: str | None, seen: str | None
) -> EdgeStatus:
    if target_id is None:
        return EdgeStatus(source_id, target_ref, None, "BROKEN", seen, None)
    actual = content_hash(target_content(lattice, target_id))
    if seen is None:
        return EdgeStatus(source_id, target_ref, target_id, "UNRECONCILED", None, actual)
    state: EdgeState = "OK" if actual == seen else "STALE"
    return EdgeStatus(source_id, target_ref, target_id, state, seen, actual)


def has_drift(statuses: list[EdgeStatus]) -> bool:
    """Return True if any edge is not OK.

    Args:
        statuses: Output of ``check_lattice``.

    Returns:
        True when any edge is STALE, UNRECONCILED, or BROKEN.
    """
    return any(s.state != "OK" for s in statuses)
