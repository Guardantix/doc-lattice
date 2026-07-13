"""Assemble parsed docs into a Lattice. Pure: no filesystem access."""

import warnings
from collections import defaultdict

from .error_types import DuplicateIdError
from .model import (
    Edge,
    FileSections,
    Lattice,
    Location,
    Node,
    ParsedDoc,
    SectionRecord,
    TargetId,
    parse_ref,
)
from .sections import anchor_ids, build_toc, section_span, split_body_lines


def derive_file_sections(body: str) -> FileSections:
    """Derive a document's total line count and anchored section spans.

    This is the single derivation the load cache stores and replays: the TOC, its
    de-duped anchor ids, and each heading's inclusive line span, so ``build_lattice``
    consumes the same values whether it derives them or reads them from the cache.

    Args:
        body: The verbatim document body after the frontmatter fence.

    Returns:
        A FileSections with the 1-based total line count and one SectionRecord per
        heading, in document order.
    """
    total_lines = _line_count(body)
    toc = build_toc(body)
    records: list[SectionRecord] = []
    for heading_index, anchor in enumerate(anchor_ids(toc)):
        start_line, end_line = section_span(toc, heading_index, total_lines)
        records.append(SectionRecord(anchor=anchor, start=start_line, end=end_line))
    return FileSections(total_lines=total_lines, sections=tuple(records))


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
        file_sections = doc.sections if doc.sections is not None else derive_file_sections(doc.body)
        total_lines = file_sections.total_lines
        _register(
            TargetId(file_id),
            Location(path=doc.path, kind="file", span=(1, total_lines)),
            index,
            sources,
            f"file {doc.path}",
        )
        anchored: list[TargetId] = []
        spans: dict[TargetId, tuple[int, int]] = {}
        for record in file_sections.sections:
            tid = TargetId(file_id, record.anchor)
            span = (record.start, record.end)
            spans[tid] = span
            anchored.append(tid)
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
    anchored: list[TargetId],
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
    for anchor in anchored:
        current_end = spans[anchor][1]
        while stack and stack[-1][0] < current_end:
            stack.pop()
        ancestors[anchor] = tuple(ancestor_id for _, ancestor_id in stack)
        stack.append((current_end, anchor))
