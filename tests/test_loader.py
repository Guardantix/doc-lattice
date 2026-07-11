"""Tests for build_lattice."""

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import game_lattice.loader as loader_module
from game_lattice.error_types import DuplicateIdError
from game_lattice.loader import _line_count, _record_ancestors, build_lattice, derive_file_sections
from game_lattice.model import (
    FileSections,
    NodeMeta,
    ParsedDoc,
    RawEdge,
    SectionRecord,
    TargetId,
)
from game_lattice.sections import anchor_ids, build_toc, section_span


def _doc(path: str, body: str, **meta) -> ParsedDoc:
    return ParsedDoc(path=Path(path), meta=NodeMeta(**meta), body=body)


def _meta(node_id: str) -> NodeMeta:
    return NodeMeta.model_validate({"id": node_id})


def test_derive_file_sections_matches_inline_derivation():
    body = "# Top {#top}\n\n## Accent {#accent}\naccent body\n\n## Motion\nmotion body\n"
    fs = derive_file_sections(body)
    assert isinstance(fs, FileSections)
    assert fs.total_lines == 7
    anchors = [rec.anchor for rec in fs.sections]
    assert anchors == ["top", "accent", "motion"]
    accent = next(rec for rec in fs.sections if rec.anchor == "accent")
    assert (accent.start, accent.end) == (3, 5)


def test_build_lattice_uses_supplied_sections_equal_to_derived():
    body = "# Top {#top}\n\n## Accent {#accent}\naccent body\n\n## Motion\nmotion body\n"
    derived = ParsedDoc(path=Path("docs/a.md"), meta=_meta("a"), body=body)
    supplied = ParsedDoc(
        path=Path("docs/a.md"), meta=_meta("a"), body=body, sections=derive_file_sections(body)
    )
    from_derived = build_lattice([derived])
    from_supplied = build_lattice([supplied])
    assert from_derived == from_supplied


def test_supplied_sections_survive_a_within_file_anchor_clash():
    # A marker equal to a computed slug must still raise DuplicateIdError from cached sections.
    body = "# Accent {#accent}\n\n## Accent\n"
    doc = ParsedDoc(
        path=Path("docs/a.md"), meta=_meta("a"), body=body, sections=derive_file_sections(body)
    )
    with pytest.raises(DuplicateIdError):
        build_lattice([doc])


def test_registers_file_and_anchor_ids():
    docs = [_doc("a.md", "# A {#sec}\nbody\n", id="a")]
    lat = build_lattice(docs)
    assert lat.index[TargetId("a")].kind == "file"
    assert lat.index[TargetId("a", "sec")].kind == "section"
    assert lat.index[TargetId("a", "sec")].span == (1, 2)


def test_build_lattice_counts_lines_once_per_document(monkeypatch):
    docs = [
        _doc("a.md", "# A {#a}\nbody\n", id="a"),
        _doc("b.md", "# B {#b}\nbody\n", id="b"),
    ]
    calls: list[str] = []
    original_line_count = loader_module._line_count

    def counting_line_count(body: str) -> int:
        calls.append(body)
        return original_line_count(body)

    monkeypatch.setattr(loader_module, "_line_count", counting_line_count)

    loader_module.build_lattice(docs)

    assert calls == [doc.body for doc in docs]


def test_resolves_edges_and_builds_dependents():
    docs = [
        _doc("up.md", "# Up {#accent}\nx\n", id="up"),
        _doc("down.md", "body\n", id="down", derives_from=[RawEdge(ref="up#accent", seen="h")]),
    ]
    lat = build_lattice(docs)
    edge = lat.nodes_by_id["down"].derives_from[0]
    assert edge.target_id == TargetId("up", "accent")
    assert lat.dependents[TargetId("up", "accent")] == frozenset({"down"})


def test_broken_ref_is_none_not_error():
    docs = [_doc("d.md", "b\n", id="d", derives_from=[RawEdge(ref="ghost")])]
    lat = build_lattice(docs)
    assert lat.nodes_by_id["d"].derives_from[0].target_id is None
    assert "ghost" not in lat.dependents


def test_path_indexes_map_paths_to_ids():
    docs = [
        _doc("up.md", "# Up {#accent}\n\n## Tone {#tone}\nx\n", id="up"),
        _doc("down.md", "body\n", id="down"),
    ]
    lat = build_lattice(docs)
    assert lat.file_id_by_path[Path("up.md")] == "up"
    assert lat.file_id_by_path[Path("down.md")] == "down"
    assert lat.anchors_by_path[Path("up.md")] == frozenset(
        {TargetId("up", "accent"), TargetId("up", "tone")}
    )
    assert Path("down.md") in lat.anchors_by_path  # every file path is a key
    assert lat.anchors_by_path[Path("down.md")] == frozenset()


def test_duplicate_id_raises():
    docs = [_doc("a.md", "b\n", id="dup"), _doc("b.md", "c\n", id="dup")]
    with pytest.raises(DuplicateIdError):
        build_lattice(docs)


def test_anchor_in_one_file_and_file_id_in_another_do_not_collide():
    # 'a#b' (a section in file a) and file id 'b' are distinct TargetIds: no collision.
    docs = [_doc("a.md", "# A {#b}\n", id="a"), _doc("b.md", "x\n", id="b")]
    lat = build_lattice(docs)  # must not raise
    assert lat.index[TargetId("a", "b")].kind == "section"
    assert lat.index[TargetId("b")].kind == "file"


def test_same_anchor_in_two_files_does_not_collide():
    docs = [
        _doc("a.md", "# A {#a-top}\n\n## Shared {#shared}\nx\n", id="a"),
        _doc("b.md", "# B {#b-top}\n\n## Shared {#shared}\nx\n", id="b"),
    ]
    lat = build_lattice(docs)  # must not raise
    assert lat.index[TargetId("a", "shared")].kind == "section"
    assert lat.index[TargetId("b", "shared")].kind == "section"


def test_ancestors_computed_for_nested_anchor():
    body = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    assert lat.ancestors[TargetId("a", "child")] == (TargetId("a", "parent"),)
    assert lat.ancestors[TargetId("a", "parent")] == ()


def test_duplicate_resolved_target_is_deduped_with_warning():
    docs = [
        _doc("up.md", "# Up {#accent}\nx\n", id="up"),
        _doc(
            "down.md",
            "body\n",
            id="down",
            derives_from=[RawEdge(ref="up#accent", seen="h1"), RawEdge(ref="up#accent", seen="h2")],
        ),
    ]
    with pytest.warns(UserWarning, match="derives from 'up#accent' more than once"):
        lat = build_lattice(docs)
    edges = lat.nodes_by_id["down"].derives_from
    assert len(edges) == 1  # the two refs resolve to the same id, deduped to one edge
    assert edges[0].target_id == TargetId("up", "accent")
    assert edges[0].seen == "h2"  # last write wins on seen
    assert lat.dependents[TargetId("up", "accent")] == frozenset({"down"})


def test_node_carries_frontmatter_fields():
    docs = [
        _doc(
            "a.md",
            "# A\nbody\n",
            id="a",
            title="Alpha",
            layer="design",
            authority="binding",
            tickets=["PC-1", "PC-2"],
        )
    ]
    node = build_lattice(docs).nodes_by_id["a"]
    assert node.title == "Alpha"
    assert node.layer == "design"
    assert node.authority == "binding"
    assert node.tickets == ("PC-1", "PC-2")  # list copied to tuple


def test_two_broken_refs_to_same_id_collapse_to_one_edge():
    docs = [
        _doc(
            "d.md",
            "b\n",
            id="d",
            derives_from=[
                RawEdge(ref="ghost", seen="h1"),
                RawEdge(ref="ghost", seen="h2"),
            ],
        )
    ]
    with pytest.warns(UserWarning, match="derives from 'ghost' more than once"):
        lat = build_lattice(docs)
    edges = lat.nodes_by_id["d"].derives_from
    assert len(edges) == 1
    assert edges[0].target_id is None  # still broken after dedup
    assert edges[0].seen == "h2"  # last write wins
    assert "ghost" not in lat.dependents


def test_ancestors_ordered_outermost_to_innermost_and_siblings_excluded():
    body = "# Top {#top}\n\n## Mid {#mid}\n\n### Leaf {#leaf}\n\nx\n\n## Sibling {#sib}\ny\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    assert lat.ancestors[TargetId("a", "leaf")] == (TargetId("a", "top"), TargetId("a", "mid"))
    assert lat.ancestors[TargetId("a", "mid")] == (TargetId("a", "top"),)
    assert lat.ancestors[TargetId("a", "sib")] == (TargetId("a", "top"),)
    assert TargetId("a", "mid") not in lat.ancestors[TargetId("a", "sib")]
    assert lat.ancestors[TargetId("a", "top")] == ()


def test_duplicate_id_error_carries_code_and_names_both_sites():
    docs = [_doc("a.md", "b\n", id="dup"), _doc("b.md", "c\n", id="dup")]
    with pytest.raises(DuplicateIdError) as exc:
        build_lattice(docs)
    assert exc.value.code == "DUPLICATE_ID"
    msg = str(exc.value)
    assert "dup" in msg
    assert "a.md" in msg  # error names both registration sites
    assert "b.md" in msg


def test_dependents_aggregates_multiple_sources():
    docs = [
        _doc("up.md", "# Up {#accent}\nx\n", id="up"),
        _doc("d1.md", "b\n", id="d1", derives_from=[RawEdge(ref="up#accent")]),
        _doc("d2.md", "b\n", id="d2", derives_from=[RawEdge(ref="up#accent")]),
    ]
    lat = build_lattice(docs)
    assert lat.dependents[TargetId("up", "accent")] == frozenset({"d1", "d2"})


def test_edges_keep_first_seen_order_with_dedup():
    docs = [
        _doc("up.md", "# Up {#accent}\n\n## Tone {#tone}\nx\n", id="up"),
        _doc(
            "d.md",
            "b\n",
            id="d",
            derives_from=[
                RawEdge(ref="up#accent", seen="a1"),
                RawEdge(ref="up#tone", seen="t1"),
                RawEdge(ref="up#accent", seen="a2"),  # later dup of up#accent
            ],
        ),
    ]
    with pytest.warns(UserWarning, match="derives from 'up#accent' more than once"):
        lat = build_lattice(docs)
    edges = lat.nodes_by_id["d"].derives_from
    assert [e.target_id for e in edges] == [TargetId("up", "accent"), TargetId("up", "tone")]
    assert edges[0].seen == "a2"  # last write wins on seen


def test_empty_doc_set_builds_empty_lattice():
    lat = build_lattice([])
    assert lat.nodes_by_id == {}
    assert lat.index == {}
    assert lat.dependents == {}
    assert lat.file_id_by_path == {}


def test_empty_body_file_spans_single_line():
    lat = build_lattice([_doc("a.md", "", id="a")])
    assert lat.index[TargetId("a")].span == (1, 1)  # _line_count floors at 1


def test_same_slug_in_two_files_does_not_collide():
    # The whole point of file-scoping: a plain '## Overview' in two files is two distinct ids.
    docs = [
        _doc("a.md", "## Overview\nx\n", id="a"),
        _doc("b.md", "## Overview\ny\n", id="b"),
    ]
    lat = build_lattice(docs)  # must not raise
    assert lat.index[TargetId("a", "overview")].kind == "section"
    assert lat.index[TargetId("b", "overview")].kind == "section"


def test_marker_equal_to_a_slug_in_same_file_collides():
    # Two headings in one file that resolve to the same anchor id are a real collision.
    docs = [_doc("a.md", "# Foo {#bar}\n\n## Bar\nx\n", id="a")]  # marker 'bar' == slug 'bar'
    with pytest.raises(DuplicateIdError):
        build_lattice(docs)


def test_bare_anchor_ref_is_broken_not_resolved():
    # A bare ref resolves only to a file id; a bare anchor that is not a file id is BROKEN.
    docs = [
        _doc("up.md", "## Accent\nx\n", id="up"),
        _doc("down.md", "b\n", id="down", derives_from=[RawEdge(ref="accent")]),
    ]
    lat = build_lattice(docs)
    assert lat.nodes_by_id["down"].derives_from[0].target_id is None  # BROKEN
    assert lat.nodes_by_id["down"].derives_from[0].target_ref == "accent"


# --- ancestor stack-pass differential test (issue #26) -----------------------------------


def _span_width_reference(span_and_id: tuple[tuple[int, int], TargetId]) -> int:
    """Line width of a ``(span, id)`` pair, for the reference sort."""
    (span_start, span_end), _ = span_and_id
    return span_end - span_start


def _record_ancestors_reference(
    anchored: list[TargetId],
    spans: dict[TargetId, tuple[int, int]],
) -> dict[TargetId, tuple[TargetId, ...]]:
    """Verbatim copy of the pre-issue-26 quadratic ancestor computation.

    Kept as the oracle the stack-pass implementation must match on every generated case.
    """
    ancestors: dict[TargetId, tuple[TargetId, ...]] = {}
    for anchor in anchored:
        start, end = spans[anchor]
        containing: list[tuple[tuple[int, int], TargetId]] = []
        for oid in anchored:
            if oid == anchor:
                continue
            ostart, oend = spans[oid]
            other_encloses = (ostart < start and oend >= end) or (ostart <= start and oend > end)
            if other_encloses:
                containing.append(((ostart, oend), oid))
        containing.sort(key=_span_width_reference, reverse=True)
        ancestors[anchor] = tuple(oid for _, oid in containing)
    return ancestors


def _build_anchored_and_spans(
    levels: list[int],
) -> tuple[list[TargetId], dict[TargetId, tuple[int, int]]]:
    """Build real ``anchored``/``spans`` from a list of heading levels.

    Renders a markdown body (one heading per level, each followed by a content line), then
    runs it through ``build_toc``/``anchor_ids``/``section_span`` exactly as ``build_lattice``
    does, so the inputs mirror what the loader feeds ``_record_ancestors``.
    """
    body_lines: list[str] = []
    for i, level in enumerate(levels):
        body_lines.append(f"{'#' * level} Section {i}")
        body_lines.append(f"content {i}")
    body = "\n".join(body_lines) + "\n"
    toc = build_toc(body)
    total_lines = _line_count(body)
    anchored: list[TargetId] = []
    spans: dict[TargetId, tuple[int, int]] = {}
    for i, anchor in enumerate(anchor_ids(toc)):
        tid = TargetId("f", anchor)
        spans[tid] = section_span(toc, i, total_lines)
        anchored.append(tid)
    return anchored, spans


def _assert_matches_reference(levels: list[int]) -> None:
    """The stack-pass result equals the quadratic reference for the given heading levels."""
    anchored, spans = _build_anchored_and_spans(levels)
    new_ancestors: dict[TargetId, tuple[TargetId, ...]] = {}
    _record_ancestors(anchored, spans, new_ancestors)
    assert new_ancestors == _record_ancestors_reference(anchored, spans)


@pytest.mark.parametrize(
    "levels",
    [
        pytest.param([1, 2, 3, 4, 5, 6], id="deeply-nested-h1-to-h6"),
        pytest.param([2, 2, 2, 2], id="flat-siblings-one-level"),
        pytest.param([1, 2, 3, 1, 2, 3], id="sibling-subtrees"),
        pytest.param([1, 2], id="last-section-runs-to-eof"),
        pytest.param([1], id="single-heading"),
    ],
)
def test_ancestors_stack_pass_matches_reference_fixed_cases(levels: list[int]) -> None:
    _assert_matches_reference(levels)


@given(st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=60))
def test_ancestors_stack_pass_matches_reference_generated(levels: list[int]) -> None:
    _assert_matches_reference(levels)


# --- FileSections cache serialization round-trip (load cache seam) -----------------------


@st.composite
def _markdown_body(draw) -> str:
    lines = draw(
        st.lists(
            st.one_of(
                st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40),
                st.builds(
                    lambda n, t: "#" * n + " " + t, st.integers(1, 6), st.text("abc ", max_size=10)
                ),
            ),
            max_size=25,
        )
    )
    return "\n".join(lines)


@settings(max_examples=200)
@given(_markdown_body())
def test_file_sections_survive_serialization_round_trip(body: str):
    # A FileSections rebuilt from its plain (anchor, start, end) tuples equals the original,
    # which is exactly what the cache stores and reloads.
    original = derive_file_sections(body)
    rebuilt = FileSections(
        total_lines=original.total_lines,
        sections=tuple(SectionRecord(r.anchor, r.start, r.end) for r in original.sections),
    )
    assert rebuilt == original
