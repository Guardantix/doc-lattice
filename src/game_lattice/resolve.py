"""Resolve refs to ids and fetch the current content a target id covers."""

from pathlib import Path

from .error_types import BrokenRefError
from .model import Lattice, Node
from .sections import section_text


def split_ref(ref: str) -> str:
    """Return the stable id a ref points at.

    Args:
        ref: A ref written bare (``accent``) or namespaced (``art-direction#accent``).

    Returns:
        The trailing id after the last ``#``; the namespace prefix is display-only.
    """
    return ref.rsplit("#", 1)[-1]


def target_content(lattice: Lattice, target_id: str) -> str:
    """Return the content a target id covers, for hashing.

    Args:
        lattice: The built lattice.
        target_id: A resolved stable id present in ``lattice.index``.

    Returns:
        The whole node body for a ``file`` location, or the anchored section text for a
        ``section`` location.

    Raises:
        BrokenRefError: If ``target_id`` is not in the index.
    """
    location = lattice.index.get(target_id)
    if location is None:
        msg = f"ref resolves to unknown id {target_id!r}; fix the ref or add the anchor"
        raise BrokenRefError(msg)
    node = _node_for_path(lattice, location.path)
    if location.kind == "file":
        return node.body
    return section_text(node.body, location.span)


def _node_for_path(lattice: Lattice, path: Path) -> Node:
    for node in lattice.nodes_by_id.values():
        if node.path == path:
            return node
    msg = f"no node owns location path {path!r}"
    raise BrokenRefError(msg)
