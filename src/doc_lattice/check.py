"""Classify every derives_from edge against its locked seen hash."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .constants import EDGE_STATES, EdgeState
from .model import CollisionMember, Edge, Lattice, TargetId, collision_members_json
from .resolve import cached_target_hash


@dataclass(frozen=True, slots=True)
class EdgeStatus:
    """The classification of one edge.

    ``collision`` is empty except on an ``AMBIGUOUS`` edge, where it names the headings whose
    ids move together, already sanitized for display.
    """

    source_id: str
    target_ref: str
    target_id: TargetId | None
    state: EdgeState
    expected: str | None
    actual: str | None
    collision: tuple[CollisionMember, ...] = ()


def _ambiguous(source_id: str, edge: Edge, collision: tuple[CollisionMember, ...]) -> EdgeStatus:
    """Build the one AMBIGUOUS record shape every command reads.

    ``actual`` is None rather than the live hash: naming a hash for a target the tool refuses to
    identify would read as a drift comparison that was actually made.
    """
    return EdgeStatus(
        source_id, edge.target_ref, edge.target_id, "AMBIGUOUS", edge.seen, None, collision
    )


def ambiguous_edges(lattice: Lattice) -> tuple[EdgeStatus, ...]:
    """Return one AMBIGUOUS record per edge whose resolved target sits in a collision component.

    Hashes nothing, so a command that only needs the ambiguity findings does not pay for a full
    drift classification to get them. ``check_lattice`` produces byte-identical records for the
    same edges.

    Args:
        lattice: The built lattice.

    Returns:
        The ambiguous edges in node-id then edge order.
    """
    found: list[EdgeStatus] = []
    for node_id in sorted(lattice.nodes_by_id):
        for edge in lattice.nodes_by_id[node_id].derives_from:
            if edge.target_id is None:
                continue
            collision = lattice.collisions.get(edge.target_id)
            if collision is not None:
                found.append(_ambiguous(node_id, edge, collision))
    return tuple(found)


def ambiguous_json(statuses: Sequence[EdgeStatus]) -> list[dict]:
    """Build the shared ``ambiguous`` payload block impact, graph, lint, and linear all emit.

    Args:
        statuses: Edge classifications; only ``AMBIGUOUS`` members are serialized.

    Returns:
        One entry per ambiguous edge, naming the colliding headings and their lines.
    """
    return [
        {
            "source_id": status.source_id,
            "target_ref": status.target_ref,
            "target_id": status.target_id.as_ref() if status.target_id else None,
            "collision": collision_members_json(status.collision),
        }
        for status in statuses
        if status.state == "AMBIGUOUS"
    ]


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
                "collision": collision_members_json(status.collision),
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
    """Classify one edge as BROKEN, AMBIGUOUS, UNRECONCILED, STALE, or OK.

    A broken edge (no resolved target) is BROKEN. A resolved target sitting in a slug-collision
    component is AMBIGUOUS. Otherwise the live target hash is
    compared against ``seen``: a missing ``seen`` is UNRECONCILED, a mismatch is STALE, and
    a match is OK.
    """
    if edge.target_id is None:
        return EdgeStatus(source_id, edge.target_ref, None, "BROKEN", edge.seen, None)
    collision = lattice.collisions.get(edge.target_id)
    if collision is not None:
        return _ambiguous(source_id, edge, collision)
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
