"""Tests for build_lattice."""

from pathlib import Path

import pytest

from game_lattice.error_types import DuplicateIdError
from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge, TargetId


def _doc(path: str, body: str, **meta) -> ParsedDoc:
    return ParsedDoc(path=Path(path), meta=NodeMeta(**meta), body=body)


def test_registers_file_and_anchor_ids():
    docs = [_doc("a.md", "# A {#sec}\nbody\n", id="a")]
    lat = build_lattice(docs)
    assert lat.index[TargetId("a")].kind == "file"
    assert lat.index[TargetId("a", "sec")].kind == "section"
    assert lat.index[TargetId("a", "sec")].span == (1, 2)


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
