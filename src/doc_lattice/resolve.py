"""Fetch the current content a target id covers, and map location paths to nodes."""

from pathlib import Path

from .error_types import BrokenRefError
from .hashing import content_hash
from .markdown_compat import strip_heading_anchor
from .model import Lattice, Node, TargetId
from .path_utils import format_path_for_display
from .sections import section_text_from_lines, split_body_lines


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
    lines = split_body_lines(node.body)
    section = section_text_from_lines(lines, location.span)
    chain = _ancestor_heading_lines(lattice, target_id, lines)
    return "\n".join([*chain, section]) if chain else section


def ancestor_headings(lattice: Lattice, target_id: TargetId) -> tuple[str, ...]:
    """Return each enclosing section's heading line, outermost first.

    The marker is removed with ``strip_heading_anchor``, which is the same treatment
    ``sections.section_text`` gives a section's own heading line, so adding or removing a
    ``{#anchor}`` on an ancestor does not restale every descendant edge.

    Args:
        lattice: The built lattice.
        target_id: A resolved section TargetId present in ``lattice.index``.

    Returns:
        The ancestors' heading source lines, outermost first, empty for a top-level section.
    """
    if not lattice.ancestors.get(target_id, ()):
        return ()
    lines = split_body_lines(node_for_path(lattice, lattice.index[target_id].path).body)
    return _ancestor_heading_lines(lattice, target_id, lines)


def _ancestor_heading_lines(
    lattice: Lattice, target_id: TargetId, lines: list[str]
) -> tuple[str, ...]:
    """Return each enclosing section's heading line from already-split body lines.

    Shared by ``ancestor_headings`` and ``target_content`` so a caller that already has
    the body split into lines, such as ``target_content``, does not pay for a second
    split of the same body.

    Args:
        lattice: The built lattice.
        target_id: A resolved section TargetId present in ``lattice.index``.
        lines: The owning body's lines, as returned by ``split_body_lines``.

    Returns:
        The ancestors' heading source lines, outermost first, empty for a top-level section.
    """
    ancestors = lattice.ancestors.get(target_id, ())
    if not ancestors:
        return ()
    return tuple(
        strip_heading_anchor(lines[lattice.index[ancestor].span[0] - 1]) for ancestor in ancestors
    )


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
