"""Reverse-walk the lattice to find every doc affected by a change to a target."""

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


def impact(lattice: Lattice, token: str) -> list[Node]:
    """Return every downstream node affected by a change to ``token``.

    Args:
        lattice: The built lattice.
        token: A bare file id or a ``file#anchor`` section ref.

    Returns:
        Affected nodes, sorted by id, walking ``dependents`` transitively. An empty list
        means the id is known but has no dependents.

    Raises:
        ValidationError: If ``token`` resolves to no id in the lattice.
    """
    if parse_ref(token) not in lattice.index:
        msg = f"unknown impact target {token!r}; run check to list ids"
        raise ValidationError(msg)
    queue = list(expand_targets(lattice, token))
    visited_targets: set[TargetId] = set()
    affected: set[str] = set()
    while queue:
        current = queue.pop()
        if current in visited_targets:
            continue
        visited_targets.add(current)
        for source_id in lattice.dependents.get(current, frozenset()):
            if source_id in affected:
                continue
            affected.add(source_id)
            # A source node id is a whole-file target; bridge it to a TargetId to keep walking.
            queue.append(TargetId(source_id))
            node = lattice.nodes_by_id[source_id]
            queue.extend(lattice.anchors_by_path[node.path])
    return [lattice.nodes_by_id[node_id] for node_id in sorted(affected)]
