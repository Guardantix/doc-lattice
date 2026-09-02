"""Fetch the current content a target id covers, and map location paths to nodes."""

from pathlib import Path

from .error_types import BrokenRefError
from .hashing import content_hash
from .model import Lattice, Node, TargetId
from .path_utils import format_path_for_display
from .sections import section_text


def target_content(lattice: Lattice, target_id: TargetId) -> str:
    """Return the content a target id covers, for hashing.

    A section target's content is prefixed with its ancestor heading chain, so context is part
    of target identity. Two byte-identical sections under different parents (a templated
    ``### Setup`` under ``## Product A`` and ``## Product B``) no longer hash the same, which is
    what closes the transient-collision hole: adding B's ``Setup`` and renaming A's in one
    change would otherwise transfer ``#setup`` between products with no run ever seeing a
    collision and the old ``seen`` still matching. A section that moves under a different
    parent, or whose ancestor is reworded, therefore goes STALE even when its own bytes did not
    change, which is correct: the context is part of what the downstream document derived from.
    Whole-file targets are unaffected.

    The chain comes from ``lattice.ancestor_context``, derived by heading level over every form
    GitHub assigns an id to and rendered as normalized ATX. A setext parent, an ATX parent
    indented one to three spaces, and a parent nested in a list item or a block quote all count,
    even though none of them is addressable and none can appear in ``lattice.ancestors``. That
    is deliberately not the span-containment chain ``impact`` walks; see ``model.Lattice``.

    One benign residual collision survives this: a parent section immediately followed by its
    first subheading, with no text between the two heading lines, hashes identically to that
    child, since the parent's own span runs through the child's heading and body with nothing
    of the parent's own in between. This is spec-tolerated rather than fixed, since either
    target id names the same bytes and an edge into either classifies drift the same way.

    Args:
        lattice: The built lattice.
        target_id: A resolved TargetId present in ``lattice.index``.

    Returns:
        The whole node body for a ``file`` location, or the ancestor heading chain followed by
        the anchored section text for a ``section`` location.

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
    section = section_text(node.body, location.span)
    chain = lattice.ancestor_context.get(target_id, ())
    return "\n".join([*chain, section]) if chain else section


def cached_target_hash(lattice: Lattice, target_id: TargetId, cache: dict[TargetId, str]) -> str:
    """Return the content hash for ``target_id``, computing it once per cache.

    A second-level cache of split body lines per path is a possible follow-up and out of scope.

    Args:
        lattice: The built lattice.
        target_id: A resolved TargetId present in ``lattice.index``.
        cache: Per-call target-content hash cache.

    Returns:
        The content hash for ``target_id``.
    """
    if target_id not in cache:
        cache[target_id] = content_hash(target_content(lattice, target_id))
    return cache[target_id]


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
        msg = f"no node owns location path {format_path_for_display(path)}"
        raise BrokenRefError(msg)
    return lattice.nodes_by_id[node_id]
