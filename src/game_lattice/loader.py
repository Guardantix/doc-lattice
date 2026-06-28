"""Assemble parsed docs into a Lattice. Pure: no filesystem access."""

import warnings
from collections import defaultdict

from .error_types import DuplicateIdError
from .model import Edge, Lattice, Location, Node, ParsedDoc, split_ref
from .sections import Heading, build_toc, section_span, split_body_lines


def build_lattice(docs: list[ParsedDoc]) -> Lattice:
    """Build the lattice from parsed docs.

    Args:
        docs: Tracked files with validated frontmatter and bodies.

    Returns:
        A Lattice with the id index, nodes, reverse adjacency, and ancestor map.

    Raises:
        DuplicateIdError: If any two ids (file or anchor) collide.
    """
    index: dict[str, Location] = {}
    sources: dict[str, str] = {}
    ancestors: dict[str, tuple[str, ...]] = {}

    for doc in docs:
        _register(
            doc.meta.id,
            Location(path=doc.path, kind="file", span=(1, _line_count(doc.body))),
            index,
            sources,
            f"file {doc.path}",
        )
        toc = build_toc(doc.body)
        total = _line_count(doc.body)
        anchored = [(i, h) for i, h in enumerate(toc) if h.anchor is not None]
        spans: dict[str, tuple[int, int]] = {}
        for i, head in anchored:
            anchor = head.anchor
            if anchor is None:
                continue
            span = section_span(toc, i, total)
            spans[anchor] = span
            _register(
                anchor,
                Location(path=doc.path, kind="section", span=span),
                index,
                sources,
                f"anchor in {doc.path}",
            )
        _record_ancestors(anchored, spans, ancestors)

    nodes: dict[str, Node] = {}
    dependents: defaultdict[str, set[str]] = defaultdict(set)
    for doc in docs:
        edges = _resolve_edges(doc, index)
        for edge in edges:
            if edge.target_id is not None:
                dependents[edge.target_id].add(doc.meta.id)
        nodes[doc.meta.id] = Node(
            id=doc.meta.id,
            title=doc.meta.title,
            layer=doc.meta.layer,
            authority=doc.meta.authority,
            path=doc.path,
            body=doc.body,
            derives_from=tuple(edges),
            tickets=tuple(doc.meta.tickets),
        )

    file_id_by_path = {node.path: node_id for node_id, node in nodes.items()}
    section_ids_by_path = defaultdict(list)
    for id_, loc in index.items():
        if loc.kind == "section":
            section_ids_by_path[loc.path].append(id_)
    anchors_by_path = {path: frozenset(section_ids_by_path[path]) for path in file_id_by_path}

    return Lattice(
        nodes_by_id=nodes,
        index=index,
        dependents={k: frozenset(v) for k, v in dependents.items()},
        ancestors=ancestors,
        file_id_by_path=file_id_by_path,
        anchors_by_path=anchors_by_path,
    )


def _resolve_edges(doc: ParsedDoc, index: dict[str, Location]) -> list[Edge]:
    """Resolve a node's derives_from entries to edges, deduped by resolved target.

    Edge identity is ``(source_node_id, resolved_target_id)`` (spec 2.2): a node that
    lists the same resolved target twice (for example a bare ref and the same id written
    namespaced) keeps only the last occurrence, last write wins on ``seen``, and a
    warning is raised. Resolution keys on the trailing id even for a broken ref, so two
    refs to the same unresolved id collapse to one broken edge.

    Args:
        doc: The parsed source document.
        index: The id-to-Location index for resolving refs.

    Returns:
        The node's edges in first-seen order, one per distinct resolved target.
    """
    deduped: dict[str, Edge] = {}
    for raw in doc.meta.derives_from:
        tid = split_ref(raw.ref)
        if tid in deduped:
            warnings.warn(
                f"node {doc.meta.id!r} derives from {tid!r} more than once;"
                " keeping the last occurrence",
                stacklevel=2,
            )
        deduped[tid] = Edge.resolve(raw.ref, raw.seen, index)
    return list(deduped.values())


def _line_count(body: str) -> int:
    """Return the 1-based line count of a body, never less than 1 for an empty body."""
    return max(1, len(split_body_lines(body)))


def _register(
    id_: str,
    location: Location,
    index: dict[str, Location],
    sources: dict[str, str],
    where: str,
) -> None:
    """Record an id in the shared index, failing if it collides with an existing id.

    ``sources`` tracks where each id was first seen so a duplicate names both registration
    sites in the error.
    """
    if id_ in index:
        msg = f"duplicate id {id_!r}: already registered at {sources[id_]}, again at {where}"
        raise DuplicateIdError(msg)
    index[id_] = location
    sources[id_] = where


def _record_ancestors(
    anchored: list[tuple[int, Heading]],
    spans: dict[str, tuple[int, int]],
    ancestors: dict[str, tuple[str, ...]],
) -> None:
    """Record each anchor's enclosing anchored sections, outermost to innermost.

    A section encloses another when its span strictly contains the other's; ties on one
    boundary still count as enclosing. Editing a nested section propagates impact to
    dependents of its ancestors, so the order runs outermost first.
    """
    for _, head in anchored:
        anchor = head.anchor
        if anchor is None:
            continue
        start, end = spans[anchor]
        containing: list[tuple[tuple[int, int], str]] = []
        for _, other in anchored:
            oid = other.anchor
            if oid is None or oid == anchor:
                continue
            ostart, oend = spans[oid]
            if (ostart < start and oend >= end) or (ostart <= start and oend > end):
                containing.append(((ostart, oend), oid))
        containing.sort(key=lambda item: item[0][1] - item[0][0], reverse=True)
        ancestors[anchor] = tuple(oid for _, oid in containing)
