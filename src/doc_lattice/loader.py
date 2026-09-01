"""Assemble parsed docs into a Lattice. Pure: no filesystem access."""

import warnings
from collections import defaultdict

from .error_types import DuplicateIdError
from .markdown_compat import (
    SluggedHeading,
    addressable_heading_inventory,
    collision_components,
    full_heading_inventory,
)
from .model import (
    CollisionMember,
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
from .path_utils import format_path_for_display
from .sections import ancestor_chains, build_toc, section_spans, split_body_lines
from .text_utils import safe_heading_label


def derive_file_sections(body: str, *, first_line: int = 1) -> FileSections:
    """Derive a document's line count, section spans, ancestor context, and collision provenance.

    This is the single derivation the load cache stores and replays: the TOC, its de-duped
    anchor ids, each heading's inclusive line span, each heading's enclosing heading chain, and,
    for a heading whose id sits in a slug-collision component, the component's members as safe
    display labels. AD-12 requires a cache hit to match the uncached result exactly, and a
    diagnostic naming colliding headings cannot be reconstructed from a boolean, so the members
    are derived here and persisted.

    The ancestor chain is derived here rather than at hash time because it needs the full
    CommonMark parse, which this derivation already runs for collision tracing. That parse is
    hoisted into this function and handed to both consumers, so carrying the chain costs no
    additional parse.

    Spans stay body-relative because they are slicing coordinates for ``body`` itself. Collision
    member lines are the opposite: they are only ever printed to a reader looking at the file, so
    they are shifted by ``first_line`` into file coordinates.

    The addressable inventory is computed once and reused for both the anchor ids and the
    collision trace. ``addressable_heading_inventory`` runs the same pinned document-order dedup
    over the same texts ``github_heading_ids`` does and guarantees the same dedup state, so
    taking each generated id from that inventory is the ``anchor_ids`` result without slugging
    every heading a second time.

    Args:
        body: The verbatim document body after the frontmatter envelope.
        first_line: The 1-based file line that body line 1 occupies. Defaults to 1, which is
            correct for a file with no envelope and for a caller holding a whole file.

    Returns:
        A FileSections with the 1-based total line count and one SectionRecord per heading, in
        document order.
    """
    total_lines = _line_count(body)
    toc = build_toc(body)
    inventory = addressable_heading_inventory(toc)
    full = full_heading_inventory(body)
    anchors = [
        heading.anchor if heading.anchor is not None else record.github_id
        for heading, record in zip(toc, inventory, strict=True)
    ]
    spans = section_spans(toc, total_lines)
    chains = ancestor_chains(full, toc)
    members_by_line = _collision_members_by_line(full, inventory, first_line=first_line)
    records: list[SectionRecord] = []
    for heading, anchor, (start_line, end_line), context in zip(
        toc, anchors, spans, chains, strict=True
    ):
        # A marker-set id is reword-stable by construction, so it is never ambiguous.
        collision = None if heading.anchor is not None else members_by_line.get(heading.line)
        records.append(
            SectionRecord(
                anchor=anchor,
                start=start_line,
                end=end_line,
                collision=collision,
                context=context,
            )
        )
    return FileSections(total_lines=total_lines, sections=tuple(records))


def _collision_members_by_line(
    full: list[SluggedHeading], addressable: list[SluggedHeading], *, first_line: int
) -> dict[int, tuple[CollisionMember, ...]]:
    """Map each colliding heading's body line to its component's safe display members.

    Keyed by line because that is what the two heading inventories share: the full GitHub
    inventory and the addressable ATX subset read the same normalized text, so a heading both
    see occupies the same 1-based line in each. Tracing collisions over only one inventory
    leaves a hole: the full inventory misses a heading the addressable scanner still addresses
    (a column-zero ``#`` line inside an HTML comment or another container the full parse treats
    as inert), so that heading's ambiguity would never surface. Both inventories' components are
    unioned by line to close that hole in either direction.

    The union is over components, not over lines alone. Two components from different
    inventories that share even one line describe one connected hazard, so they are merged into
    a single member listing rather than left as two partial ones: every sink prints the listing
    a reader is meant to act on whole, and a line must map to exactly one of them. A line seen
    by both inventories is not double-counted, and the full inventory's record wins, since it is
    the inventory the existing cases and this function's return contract were already written
    against; it is traversed first, and an already-recorded line is never overwritten.

    The merge is a disjoint-set union over line numbers, the same primitive
    ``markdown_compat.collision_components`` runs over inventory indices, rather than a scan of
    every group already merged. A pairwise scan is quadratic in the number of components, and
    the two inventories almost always see the same headings, so every component from the second
    inventory overlaps its twin from the first and the rebuild fires every time.

    Args:
        full: The full GitHub heading inventory's allocation trace, from
            ``full_heading_inventory(body)``. Taken as an argument rather than derived here so
            the caller's single full parse serves the ancestor chains as well.
        addressable: The addressable ATX subset's own allocation trace, from
            ``addressable_heading_inventory(build_toc(body))``.
        first_line: The 1-based file line that body line 1 occupies. Member lines are reported
            in file coordinates; the returned keys stay body-relative, matching the TOC.

    Returns:
        One entry per heading in a collision component, in either inventory's terms, every
        member of one merged component sharing one tuple.
    """
    parent: dict[int, int] = {}
    headings: dict[int, SluggedHeading] = {}

    def find(line: int) -> int:
        while parent[line] != line:
            parent[line] = parent[parent[line]]
            line = parent[line]
        return line

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for inventory in (full, addressable):
        for component in collision_components(inventory):
            anchor_line = component[0].line
            for heading in component:
                # setdefault, with the full inventory traversed first, is what makes its record
                # win for a line both inventories see.
                headings.setdefault(heading.line, heading)
                parent.setdefault(heading.line, heading.line)
                union(anchor_line, heading.line)

    grouped: dict[int, list[int]] = {}
    for line in parent:
        grouped.setdefault(find(line), []).append(line)

    found: dict[int, tuple[CollisionMember, ...]] = {}
    for lines in grouped.values():
        members = tuple(
            CollisionMember(
                label=safe_heading_label(headings[line].text), line=line + first_line - 1
            )
            for line in sorted(lines)
        )
        for line in lines:
            found[line] = members
    return found


def build_lattice(docs: list[ParsedDoc]) -> Lattice:
    """Build the lattice from parsed docs.

    A doc that carries no ``sections`` has them derived here as a fallback. Both production load
    paths derive them ahead of this call, because only they know where the body starts in the
    file; the fallback has no such offset and so yields body-relative collision member lines. It
    exists for a synthetic caller that builds a ``ParsedDoc`` by hand, where body and file
    coordinates coincide anyway.

    Args:
        docs: Tracked files with validated frontmatter and bodies.

    Returns:
        A Lattice with the TargetId index, nodes, reverse adjacency, and both ancestor maps.

    Raises:
        DuplicateIdError: If two file ids collide, or two headings in one file resolve to the
            same anchor id (a marker equal to a computed slug, or two equal markers).
    """
    index: dict[TargetId, Location] = {}
    sources: dict[TargetId, str] = {}
    ancestors: dict[TargetId, tuple[TargetId, ...]] = {}
    collisions: dict[TargetId, tuple[CollisionMember, ...]] = {}
    ancestor_context: dict[TargetId, tuple[str, ...]] = {}

    for doc in docs:
        file_id = doc.meta.id
        file_sections = doc.sections if doc.sections is not None else derive_file_sections(doc.body)
        total_lines = file_sections.total_lines
        _register(
            TargetId(file_id),
            Location(path=doc.path, kind="file", span=(1, total_lines)),
            index,
            sources,
            f"file {format_path_for_display(doc.path)}",
        )
        anchored: list[TargetId] = []
        spans: dict[TargetId, tuple[int, int]] = {}
        for record in file_sections.sections:
            tid = TargetId(file_id, record.anchor)
            span = (record.start, record.end)
            spans[tid] = span
            anchored.append(tid)
            if record.collision is not None:
                collisions[tid] = record.collision
            if record.context:
                ancestor_context[tid] = record.context
            _register(
                tid,
                Location(path=doc.path, kind="section", span=span),
                index,
                sources,
                f"anchor {tid.as_ref()!r} in {format_path_for_display(doc.path)}",
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
        collisions=collisions,
        ancestor_context=ancestor_context,
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
