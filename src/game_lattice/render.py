"""Render the lattice as Mermaid or DOT."""

import re

from .model import Lattice, TargetId

_MERMAID_ID_RE = re.compile(r"[^A-Za-z0-9_]")


def _label(lattice: Lattice, node_id: str) -> str:
    """Return the human-readable name for a node: its title, or its id as a fallback.

    The result is raw text; each renderer escapes it for its own quoting rules.
    """
    node = lattice.nodes_by_id.get(node_id)
    return node.title if node is not None and node.title else node_id


def _dot_escape(text: str) -> str:
    """Escape text for a DOT double-quoted string.

    Backslash is doubled first so it does not consume the quote escape, then each double
    quote is escaped. Without this a trailing backslash would escape the closing quote and
    corrupt the label.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _mermaid_escape(text: str) -> str:
    """Escape text for a Mermaid double-quoted label.

    Mermaid has no backslash escape inside ``"..."``; a literal double quote is replaced
    with an apostrophe so the label stays well-formed.
    """
    return text.replace('"', "'")


def _mermaid_id(node_id: str) -> str:
    """Return a Mermaid-safe node identifier (its title carries the readable name).

    Mermaid rejects ids with spaces or reserved characters, so any character outside
    ``[A-Za-z0-9_]`` is replaced with ``_``. The human-readable name is preserved in the
    node's bracketed label.
    """
    return _MERMAID_ID_RE.sub("_", node_id)


def _graph_edges(
    lattice: Lattice, stale_edges: set[tuple[str, TargetId]]
) -> list[tuple[str, str, bool]]:
    """Collapse resolved edges onto tracked file nodes.

    A ``derives_from`` target is often a section anchor, which is not itself a graph node.
    Each edge is drawn from the file that owns its target to the downstream source, so only
    tracked nodes appear (spec 6.4). Multiple section edges between the same two files
    collapse to one edge, marked stale if any contributing edge is stale.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs that are stale.

    Returns:
        Sorted ``(upstream_file_id, source_id, is_stale)`` triples, broken edges omitted.
    """
    collapsed: dict[tuple[str, str], bool] = {}
    for source_id in lattice.nodes_by_id:
        for edge in lattice.nodes_by_id[source_id].derives_from:
            if edge.target_id is None:
                continue
            location = lattice.index.get(edge.target_id)
            if location is None:
                continue
            # Every index location path belongs to a tracked node, so this lookup always hits.
            upstream = lattice.file_id_by_path[location.path]
            is_stale = (source_id, edge.target_id) in stale_edges
            key = (upstream, source_id)
            collapsed[key] = collapsed.get(key, False) or is_stale
    return sorted(
        (upstream, source_id, is_stale) for (upstream, source_id), is_stale in collapsed.items()
    )


def to_mermaid(lattice: Lattice, stale_edges: set[tuple[str, TargetId]]) -> str:
    """Render a Mermaid ``graph TD``.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw with a dashed arrow.

    Returns:
        Mermaid source. Edges run upstream (target) to downstream (source).
    """
    lines = ["graph TD"]
    for node_id in sorted(lattice.nodes_by_id):
        label = _mermaid_escape(_label(lattice, node_id))
        lines.append(f'    {_mermaid_id(node_id)}["{label}"]')
    for upstream, source_id, is_stale in _graph_edges(lattice, stale_edges):
        arrow = "-.->" if is_stale else "-->"
        lines.append(f"    {_mermaid_id(upstream)} {arrow} {_mermaid_id(source_id)}")
    return "\n".join(lines) + "\n"


def to_dot(lattice: Lattice, stale_edges: set[tuple[str, TargetId]]) -> str:
    """Render a Graphviz DOT digraph.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw dashed.

    Returns:
        DOT source. Edges run upstream (target) to downstream (source).
    """
    lines = ["digraph lattice {"]
    for node_id in sorted(lattice.nodes_by_id):
        label = _dot_escape(_label(lattice, node_id))
        lines.append(f'    "{_dot_escape(node_id)}" [label="{label}"];')
    for upstream, source_id, is_stale in _graph_edges(lattice, stale_edges):
        style = " [style=dashed]" if is_stale else ""
        lines.append(f'    "{_dot_escape(upstream)}" -> "{_dot_escape(source_id)}"{style};')
    lines.append("}")
    return "\n".join(lines) + "\n"
