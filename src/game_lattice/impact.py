"""Reverse-walk the lattice to find every doc affected by a change to a target."""

from collections import deque

from .error_types import ValidationError
from .model import Lattice, Node, TargetId, parse_ref


def expand_targets(lattice: Lattice, token: str) -> set[TargetId]:
    """Expand an impact token into the full set of TargetIds it touches.

    Args:
        lattice: The built lattice.
        token: A bare file id or a ``file#anchor`` section ref.

    Returns:
        For a file id: the file target plus all section anchors in its file. For a section
        anchor: the anchor, its anchored ancestors, and the enclosing file target. Empty if
        the token resolves to no id.
    """
    target_id = parse_ref(token)
    location = lattice.index.get(target_id)
    if location is None:
        return set()
    if location.kind == "file":
        return {target_id} | lattice.anchors_by_path[location.path]
    # A resolved section is registered in ancestors and its file path is a tracked node, so
    # both keys exist by construction; index direct so an incoherent index fails loud instead
    # of silently under-reporting impact.
    expanded = {target_id} | set(lattice.ancestors[target_id])
    expanded.add(TargetId(lattice.file_id_by_path[location.path]))
    return expanded


def impact(lattice: Lattice, token: str, *, max_depth: int | None = None) -> list[tuple[Node, int]]:
    """Return every downstream node affected by a change to ``token``, paired with its depth.

    The walk is breadth-first over ``dependents``. Every TargetId that ``expand_targets``
    produces is depth 0; a node discovered via the dependents of a depth-``d`` target is at
    depth ``d + 1``, as are the targets that node contributes back into the walk (its own
    whole-file TargetId and its anchored sections). A node's recorded depth is the minimum
    depth at which it is discovered, which breadth-first order makes the first discovery.

    Args:
        lattice: The built lattice.
        token: A bare file id or a ``file#anchor`` section ref.
        max_depth: If set, a target at this depth is not expanded, so no node beyond this many
            hops from ``token`` is reported. ``None`` walks the full transitive closure.

    Returns:
        Affected ``(node, depth)`` pairs, sorted by node id, walking ``dependents``
        transitively within the depth bound. An empty list means the id is known but has no
        dependents in range.

    Raises:
        ValidationError: If ``token`` resolves to no id in the lattice.
    """
    if parse_ref(token) not in lattice.index:
        msg = f"unknown impact target {token!r}; run check to list ids"
        raise ValidationError(msg)
    queue: deque[tuple[TargetId, int]] = deque(
        (target, 0) for target in expand_targets(lattice, token)
    )
    visited_targets: set[TargetId] = set()
    affected: dict[str, int] = {}
    while queue:
        current, depth = queue.popleft()
        if current in visited_targets:
            continue
        visited_targets.add(current)
        if max_depth is not None and depth >= max_depth:
            continue
        for source_id in lattice.dependents.get(current, frozenset()):
            if source_id in affected:
                continue
            affected[source_id] = depth + 1
            if max_depth is None or depth + 1 < max_depth:
                # A source node id is a whole-file target; bridge it to a TargetId to keep walking.
                queue.append((TargetId(source_id), depth + 1))
                node = lattice.nodes_by_id[source_id]
                queue.extend((anchor, depth + 1) for anchor in lattice.anchors_by_path[node.path])
    return [(lattice.nodes_by_id[node_id], affected[node_id]) for node_id in sorted(affected)]
