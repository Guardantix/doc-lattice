"""Render the lattice as Mermaid, DOT, or JSON."""

from .model import (
    CollisionMember,
    Lattice,
    TargetId,
    collision_members_json,
    format_collision_members,
)


def _ambiguous_components(lattice: Lattice) -> list[tuple[TargetId, tuple[CollisionMember, ...]]]:
    """Return one ``(canonical target id, members)`` pair per collision component.

    ``Lattice.collisions`` maps every target id in a component to the same members tuple (a
    two-heading component is keyed under both its ids), so naming each side separately would
    repeat the same finding twice under two ids. This collapses that back to one row per
    component, keyed under the lexicographically smallest ref.

    The dedup key is scoped to ``(file_id, members)`` rather than ``members`` alone: within one
    file an identical members tuple is necessarily the same component, since one set of lines
    cannot hold two components, but two different files can coincidentally produce
    value-equal members tuples (the same headings at the same line numbers) for two genuinely
    distinct components. Keying on the bare tuple would wrongly merge those and drop one file's
    row from the naming block.

    Args:
        lattice: The built lattice.

    Returns:
        Component rows sorted by the canonical target ref.
    """
    canonical: dict[tuple[str, tuple[CollisionMember, ...]], TargetId] = {}
    for target_id, members in lattice.collisions.items():
        key = (target_id.file_id, members)
        current = canonical.get(key)
        if current is None or target_id.as_ref() < current.as_ref():
            canonical[key] = target_id
    return sorted(
        ((target_id, members) for (_file_id, members), target_id in canonical.items()),
        key=lambda item: item[0].as_ref(),
    )


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


def _ambiguous_lines(lattice: Lattice, prefix: str) -> list[str]:
    """Render one comment line per ambiguous target, naming its colliding headings.

    The same facts every format carries, spelled as a comment in DOT and Mermaid because neither
    has a place for a finding that is not an edge. The labels are already control-free, so a
    comment cannot be broken out of.

    Args:
        lattice: The built lattice.
        prefix: The destination format's line-comment marker.

    Returns:
        One line per ambiguous target, ordered by target ref.
    """
    return [
        f"{prefix} ambiguous {target_id.as_ref()}: " + format_collision_members(members)
        for target_id, members in _ambiguous_components(lattice)
    ]


def _graph_edges(
    lattice: Lattice,
    stale_edges: set[tuple[str, TargetId]],
) -> list[tuple[str, str, bool, bool]]:
    """Collapse resolved edges onto tracked file nodes.

    A ``derives_from`` target is often a section anchor, which is not itself a graph node.
    Each edge is drawn from the file that owns its target to the downstream source, so only
    tracked nodes appear. Multiple section edges between the same two files
    collapse to one edge, marked stale or ambiguous if any contributing edge is.

    Ambiguity is read from ``lattice.collisions`` rather than taken as a parameter, because
    that map is the definition: ``check._classify`` marks an edge AMBIGUOUS exactly when its
    resolved target sits in the map. Deriving it here keeps the arrow styling and the
    ``_ambiguous_components`` naming block reading one source. Staleness cannot follow suit,
    since it requires the target hashes a caller has already paid for.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs that are stale.

    Returns:
        Sorted ``(upstream_file_id, source_id, is_stale, is_ambiguous)`` tuples, broken edges
        omitted.
    """
    collapsed: dict[tuple[str, str], tuple[bool, bool]] = {}
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
            is_ambiguous = edge.target_id in lattice.collisions
            key = (upstream, source_id)
            prior_stale, prior_ambiguous = collapsed.get(key, (False, False))
            collapsed[key] = (prior_stale or is_stale, prior_ambiguous or is_ambiguous)
    return sorted(
        (upstream, source_id, is_stale, is_ambiguous)
        for (upstream, source_id), (is_stale, is_ambiguous) in collapsed.items()
    )


def to_mermaid(
    lattice: Lattice,
    stale_edges: set[tuple[str, TargetId]],
) -> str:
    """Render a Mermaid ``graph TD``.

    An edge into a collision component is drawn with a dotted arrow and each component is
    named in a leading comment; both are derived from ``lattice.collisions``.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw with a dashed arrow.

    Returns:
        Mermaid source. Edges run upstream (target) to downstream (source).
    """
    mermaid_ids = {
        node_id: f"n{index}" for index, node_id in enumerate(sorted(lattice.nodes_by_id))
    }
    lines = ["graph TD"]
    lines.extend(_ambiguous_lines(lattice, "    %%"))
    for node_id, mermaid_id in mermaid_ids.items():
        label = _mermaid_escape(_label(lattice, node_id))
        lines.append(f'    {mermaid_id}["{label}"]')
    for upstream, source_id, is_stale, is_ambiguous in _graph_edges(lattice, stale_edges):
        # Ambiguity beats staleness, for the reason spelled out in to_dot: _graph_edges collapses
        # several section edges onto one graph edge, so both flags are routinely true at once.
        arrow = "-. ambiguous .->" if is_ambiguous else "-.->" if is_stale else "-->"
        lines.append(f"    {mermaid_ids[upstream]} {arrow} {mermaid_ids[source_id]}")
    return "\n".join(lines) + "\n"


def to_dot(
    lattice: Lattice,
    stale_edges: set[tuple[str, TargetId]],
) -> str:
    """Render a Graphviz DOT digraph.

    An edge into a collision component is drawn dotted red and each component is named in a
    leading comment; both are derived from ``lattice.collisions``.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw dashed.

    Returns:
        DOT source. Edges run upstream (target) to downstream (source).
    """
    lines = ["digraph lattice {"]
    lines.extend(_ambiguous_lines(lattice, "    //"))
    for node_id in sorted(lattice.nodes_by_id):
        label = _dot_escape(_label(lattice, node_id))
        lines.append(f'    "{_dot_escape(node_id)}" [label="{label}"];')
    for upstream, source_id, is_stale, is_ambiguous in _graph_edges(lattice, stale_edges):
        # An edge that is both stale and ambiguous takes the ambiguous style. This is ordinary
        # CLI output, not a defensive branch: check_lattice gives each *edge* exactly one state,
        # but _graph_edges collapses every section edge between two files onto one graph edge and
        # ORs the flags, so a node deriving from one STALE and one AMBIGUOUS section of the same
        # upstream file draws a single edge that is both. Ambiguity wins because it is the
        # condition reconcile refuses; do not delete this branch as unreachable.
        style = (
            ' [style=dotted color="red"]' if is_ambiguous else " [style=dashed]" if is_stale else ""
        )
        lines.append(f'    "{_dot_escape(upstream)}" -> "{_dot_escape(source_id)}"{style};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def to_json(
    lattice: Lattice,
    stale_edges: set[tuple[str, TargetId]],
) -> dict:
    """Render the lattice as a JSON-serializable node/edge dump.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs that are stale.

    Returns:
        A dict with a ``nodes`` list (one entry per tracked node, sorted by id), an ``edges``
        list holding the same collapsed file-level tuples that :func:`to_mermaid` and
        :func:`to_dot` draw (broken edges omitted, section edges collapsed onto their owning
        file, stale/ambiguous if any contributing edge is), and an ``ambiguous_targets`` list
        naming each collision component's colliding headings.
    """
    nodes = [
        {
            "id": node_id,
            "title": node.title,
            "layer": node.layer,
            "authority": node.authority,
            "path": str(node.path),
        }
        for node_id, node in sorted(lattice.nodes_by_id.items())
    ]
    edges = [
        {
            "upstream": upstream,
            "downstream": source_id,
            "stale": is_stale,
            "ambiguous": is_ambiguous,
        }
        for upstream, source_id, is_stale, is_ambiguous in _graph_edges(lattice, stale_edges)
    ]
    ambiguous_targets = [
        {
            "target_id": target_id.as_ref(),
            "members": collision_members_json(members),
        }
        for target_id, members in _ambiguous_components(lattice)
    ]
    return {"nodes": nodes, "edges": edges, "ambiguous_targets": ambiguous_targets}
