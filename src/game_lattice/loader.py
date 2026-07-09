"""Assemble parsed docs into a Lattice. Pure: no filesystem access."""

import warnings
from collections import defaultdict

from .error_types import DuplicateIdError
from .model import Edge, Lattice, Location, Node, ParsedDoc, TargetId, parse_ref
from .sections import Heading, anchor_ids, build_toc, section_span, split_body_lines


def build_lattice(docs: list[ParsedDoc]) -> Lattice:
    """Build the lattice from parsed docs.

    Args:
        docs: Tracked files with validated frontmatter and bodies.

    Returns:
        A Lattice with the TargetId index, nodes, reverse adjacency, and ancestor map.

    Raises:
        DuplicateIdError: If two file ids collide, or two headings in one file resolve to the
            same anchor id (a marker equal to a computed slug, or two equal markers).
    """
    index: dict[TargetId, Location] = {}
    sources: dict[TargetId, str] = {}
    ancestors: dict[TargetId, tuple[TargetId, ...]] = {}

    for doc in docs:
        file_id = doc.meta.id
        _register(
            TargetId(file_id),
            Location(path=doc.path, kind="file", span=(1, _line_count(doc.body))),
            index,
            sources,
            f"file {doc.path}",
        )
        toc = build_toc(doc.body)
        total_lines = _line_count(doc.body)
        anchored: list[tuple[int, Heading, TargetId]] = []
        spans: dict[TargetId, tuple[int, int]] = {}
        for i, (head, anchor) in enumerate(zip(toc, anchor_ids(toc), strict=True)):
            tid = TargetId(file_id, anchor)
            span = section_span(toc, i, total_lines)
            spans[tid] = span
            anchored.append((i, head, tid))
            _register(
                tid,
                Location(path=doc.path, kind="section", span=span),
                index,
                sources,
                f"anchor {tid.as_ref()!r} in {doc.path}",
            )
        _record_ancestors(anchored, spans, ancestors)

    nodes: dict[str, Node] = {}
    dependents: defaultdict[TargetId, set[str]] = defaultdict(set)
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
    for tid, loc in index.items():
        if loc.kind == "section":
            section_ids_by_path[loc.path].append(tid)
    anchors_by_path = {path: frozenset(section_ids_by_path[path]) for path in file_id_by_path}

    return Lattice(
        nodes_by_id=nodes,
        index=index,
        dependents={k: frozenset(v) for k, v in dependents.items()},
        ancestors=ancestors,
        file_id_by_path=file_id_by_path,
        anchors_by_path=anchors_by_path,
    )


def _resolve_edges(doc: ParsedDoc, index: dict[TargetId, Location]) -> list[Edge]:
    """Resolve a node's derives_from entries to edges, deduped by resolved target.

    Edge identity is ``(source_node_id, resolved TargetId)``: a node that lists the same
    resolved target twice keeps only the last occurrence, last write wins on ``seen``, and a
    warning is raised. Resolution keys on the parsed TargetId even for a broken ref, so two
    refs to the same unresolved target collapse to one broken edge.

    Args:
        doc: The parsed source document.
        index: The TargetId-to-Location index for resolving refs.

    Returns:
        The node's edges in first-seen order, one per distinct resolved target.
    """
    deduped: dict[TargetId, Edge] = {}
    for raw in doc.meta.derives_from:
        target_id = parse_ref(raw.ref)
        if target_id in deduped:
            warnings.warn(
                f"node {doc.meta.id!r} derives from {target_id.as_ref()!r} more than once;"
                " keeping the last occurrence",
                stacklevel=2,
            )
        deduped[target_id] = Edge.resolve(raw.ref, raw.seen, index)
    return list(deduped.values())


def _line_count(body: str) -> int:
    """Return the 1-based line count of a body, never less than 1 for an empty body."""
    return max(1, len(split_body_lines(body)))


def _register(
    id_: TargetId,
    location: Location,
    index: dict[TargetId, Location],
    sources: dict[TargetId, str],
    where: str,
) -> None:
    """Record a TargetId in the shared index, failing if it collides with an existing one.

    ``sources`` tracks where each id was first seen so a duplicate names both registration
    sites in the error. A file id and a section id in different files never collide because
    their TargetIds differ; only a within-file anchor clash or a repeated file id does.
    """
    if id_ in index:
        msg = (
            f"duplicate id {id_.as_ref()!r}: already registered at {sources[id_]}, again at {where}"
        )
        raise DuplicateIdError(msg)
    index[id_] = location
    sources[id_] = where


def _record_ancestors(
    anchored: list[tuple[int, Heading, TargetId]],
    spans: dict[TargetId, tuple[int, int]],
    ancestors: dict[TargetId, tuple[TargetId, ...]],
) -> None:
    """Record each anchor's enclosing anchored sections, outermost to innermost.

    A section encloses another when its span strictly contains the other's; ties on one
    boundary still count as enclosing. Editing a nested section propagates impact to
    dependents of its ancestors, so the order runs outermost first.

    Because ``anchored`` is in document order, span starts strictly increase, so a single
    stack pass suffices: an anchor still on the stack whose end reaches the current anchor's
    end encloses it. Popping ends strictly below the current end leaves exactly the ancestor
    set, bottom-to-top being outermost-to-innermost.
    """
    stack: list[tuple[int, TargetId]] = []
    for _, _head, anchor in anchored:
        _start, end = spans[anchor]
        while stack and stack[-1][0] < end:
            stack.pop()
        ancestors[anchor] = tuple(tid for _, tid in stack)
        stack.append((end, anchor))
