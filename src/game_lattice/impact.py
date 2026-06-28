"""Reverse-walk the lattice to find every doc affected by a change to a target."""

from .error_types import ValidationError
from .model import Lattice, Node, split_ref


def expand_targets(lattice: Lattice, token: str) -> set[str]:
    """Expand an impact token into the full set of target ids it touches.

    Args:
        lattice: The built lattice.
        token: A bare id or ``namespace#id`` ref naming a file or section anchor.

    Returns:
        For a file id: the id plus all section anchors in its file. For a section
        anchor: the anchor, its anchored ancestors, and the enclosing file id (editing a
        section also changes the whole-file hash, so a dependent of the file is affected
        too). Empty if the id is unknown.
    """
    target_id = split_ref(token)
    location = lattice.index.get(target_id)
    if location is None:
        return set()
    if location.kind == "file":
        return {target_id} | lattice.anchors_by_path.get(location.path, frozenset())
    expanded = {target_id} | set(lattice.ancestors.get(target_id, ()))
    file_id = lattice.file_id_by_path.get(location.path)
    if file_id is not None:
        expanded.add(file_id)
    return expanded


def impact(lattice: Lattice, token: str) -> list[Node]:
    """Return every downstream node affected by a change to ``token``.

    Args:
        lattice: The built lattice.
        token: A bare id or ``namespace#id`` ref.

    Returns:
        Affected nodes, sorted by id, walking ``dependents`` transitively. An empty list
        means the id is known but has no dependents.

    Raises:
        ValidationError: If ``token`` resolves to no id in the lattice, so a typo is
            reported rather than silently returning an empty result.
    """
    if split_ref(token) not in lattice.index:
        msg = f"unknown impact target {token!r}; run check to list ids"
        raise ValidationError(msg)
    queue = list(expand_targets(lattice, token))
    visited_targets: set[str] = set()
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
            queue.append(source_id)
            node = lattice.nodes_by_id.get(source_id)
            if node is not None:
                queue.extend(lattice.anchors_by_path.get(node.path, frozenset()))
    return [lattice.nodes_by_id[i] for i in sorted(affected)]
