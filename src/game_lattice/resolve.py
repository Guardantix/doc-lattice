"""Fetch the current content a target id covers, and map location paths to nodes."""

from pathlib import Path

from .error_types import BrokenRefError
from .model import Lattice, Node, TargetId
from .sections import section_text


def target_content(lattice: Lattice, target_id: TargetId) -> str:
    """Return the content a target id covers, for hashing.

    Args:
        lattice: The built lattice.
        target_id: A resolved TargetId present in ``lattice.index``.

    Returns:
        The whole node body for a ``file`` location, or the anchored section text for a
        ``section`` location.

    Raises:
        BrokenRefError: If ``target_id`` is not in the index.
    """
    location = lattice.index.get(target_id)
    if location is None:
        msg = f"ref resolves to unknown id {target_id.as_ref()!r}; fix the ref or add the anchor"
        raise BrokenRefError(msg)
    node = node_for_path(lattice, location.path)
    if location.kind == "file":
        return node.body
    return section_text(node.body, location.span)


def node_for_path(lattice: Lattice, path: Path) -> Node:
    """Return the tracked node that owns a location path via the loader's path index.

    Args:
        lattice: The built lattice.
        path: A location path drawn from ``lattice.index``.

    Returns:
        The node whose file is ``path``.

    Raises:
        BrokenRefError: If no tracked node owns ``path``.
    """
    node_id = lattice.file_id_by_path.get(path)
    if node_id is None:
        msg = f"no node owns location path {path!r}"
        raise BrokenRefError(msg)
    return lattice.nodes_by_id[node_id]
