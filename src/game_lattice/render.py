"""Render the lattice as Mermaid or DOT."""

import re

from .model import Lattice

_MERMAID_ID_RE = re.compile(r"[^A-Za-z0-9_]")


def _label(lattice: Lattice, node_id: str) -> str:
    node = lattice.nodes_by_id.get(node_id)
    title = node.title if node is not None and node.title else node_id
    return title.replace('"', "'")


def _mermaid_id(node_id: str) -> str:
    """Return a Mermaid-safe node identifier (its title carries the readable name).

    Mermaid rejects ids with spaces or reserved characters, so any character outside
    ``[A-Za-z0-9_]`` is replaced with ``_``. The human-readable name is preserved in the
    node's bracketed label.
    """
    return _MERMAID_ID_RE.sub("_", node_id)


def _graph_edges(
    lattice: Lattice, stale_edges: set[tuple[str, str]]
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
    path_to_file = {node.path: node_id for node_id, node in lattice.nodes_by_id.items()}
    collapsed: dict[tuple[str, str], bool] = {}
    for source_id in lattice.nodes_by_id:
        for edge in lattice.nodes_by_id[source_id].derives_from:
            if edge.target_id is None:
                continue
            location = lattice.index.get(edge.target_id)
            if location is None:
                continue
            upstream = path_to_file.get(location.path, edge.target_id)
            is_stale = (source_id, edge.target_id) in stale_edges
            key = (upstream, source_id)
            collapsed[key] = collapsed.get(key, False) or is_stale
    return sorted((up, src, stale) for (up, src), stale in collapsed.items())


def to_mermaid(lattice: Lattice, stale_edges: set[tuple[str, str]]) -> str:
    """Render a Mermaid ``graph TD``.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw with a dashed arrow.

    Returns:
        Mermaid source. Edges run upstream (target) to downstream (source).
    """
    lines = ["graph TD"]
    for node_id in sorted(lattice.nodes_by_id):
        lines.append(f'    {_mermaid_id(node_id)}["{_label(lattice, node_id)}"]')
    for upstream, source_id, is_stale in _graph_edges(lattice, stale_edges):
        arrow = "-.->" if is_stale else "-->"
        lines.append(f"    {_mermaid_id(upstream)} {arrow} {_mermaid_id(source_id)}")
    return "\n".join(lines) + "\n"


def to_dot(lattice: Lattice, stale_edges: set[tuple[str, str]]) -> str:
    """Render a Graphviz DOT digraph.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw dashed.

    Returns:
        DOT source. Edges run upstream (target) to downstream (source).
    """
    lines = ["digraph lattice {"]
    for node_id in sorted(lattice.nodes_by_id):
        lines.append(f'    "{node_id}" [label="{_label(lattice, node_id)}"];')
    for upstream, source_id, is_stale in _graph_edges(lattice, stale_edges):
        style = " [style=dashed]" if is_stale else ""
        lines.append(f'    "{upstream}" -> "{source_id}"{style};')
    lines.append("}")
    return "\n".join(lines) + "\n"
