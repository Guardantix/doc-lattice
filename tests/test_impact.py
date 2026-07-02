"""Tests for impact."""

from pathlib import Path

import pytest

from game_lattice.error_types import ValidationError
from game_lattice.impact import expand_targets, impact
from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge, TargetId


def _doc(path: str, body: str, **meta) -> ParsedDoc:
    return ParsedDoc(Path(path), NodeMeta(**meta), body)


def test_section_token_expands_to_ancestors_and_file():
    body = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    # Editing 'child' also changes the whole-file hash, so the file id 'a' is included.
    assert expand_targets(lat, "a#child") == {
        TargetId("a", "child"),
        TargetId("a", "parent"),
        TargetId("a"),
    }


def test_impact_section_reaches_whole_file_dependents():
    # 'whole' derives from the file 'up'; 'sub' derives from section 'sec' inside up.
    # Editing 'sec' changes up's whole-file hash, so 'whole' is affected too.
    parent = "# Up {#up-top}\n\n## Sec {#sec}\nx\n"
    lat = build_lattice(
        [
            _doc("up.md", parent, id="up"),
            _doc("whole.md", "x\n", id="whole", derives_from=[RawEdge(ref="up")]),
            _doc("sub.md", "x\n", id="sub", derives_from=[RawEdge(ref="up#sec")]),
        ]
    )
    assert {n.id for n in impact(lat, "up#sec")} == {"whole", "sub"}


def test_file_token_expands_to_its_anchors():
    body = "# A {#a-top}\n\n## Sec {#sec}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    assert expand_targets(lat, "a") == {
        TargetId("a"),
        TargetId("a", "a-top"),
        TargetId("a", "sec"),
    }


def test_impact_includes_parent_dependents_for_nested_edit():
    parent = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice(
        [
            _doc("up.md", parent, id="up"),
            _doc("d-parent.md", "x\n", id="d-parent", derives_from=[RawEdge(ref="up#parent")]),
            _doc("d-child.md", "x\n", id="d-child", derives_from=[RawEdge(ref="up#child")]),
        ]
    )
    affected = {n.id for n in impact(lat, "up#child")}
    assert affected == {"d-parent", "d-child"}


def test_impact_is_transitive():
    lat = build_lattice(
        [
            _doc("up.md", "# Up {#u}\nx\n", id="up"),
            _doc("mid.md", "x\n", id="mid", derives_from=[RawEdge(ref="up#u")]),
            _doc("low.md", "x\n", id="low", derives_from=[RawEdge(ref="mid")]),
        ]
    )
    assert {n.id for n in impact(lat, "up#u")} == {"mid", "low"}


def test_impact_unknown_token_raises():
    lat = build_lattice([_doc("a.md", "# A {#a-top}\nx\n", id="a")])
    with pytest.raises(ValidationError) as exc:
        impact(lat, "nonexistent")
    assert exc.value.code == "VALIDATION_ERROR"
    assert "nonexistent" in str(exc.value)


def test_impact_known_id_with_no_dependents_is_empty():
    lat = build_lattice([_doc("a.md", "# A {#a-top}\nx\n", id="a")])
    assert impact(lat, "a") == []


def test_impact_diamond_reaches_each_node_once():
    # 'd' derives from both the file 'a' and its section 'a-sec'; editing 'a' reaches d
    # via two targets, but it must appear exactly once.
    lat = build_lattice(
        [
            _doc("a.md", "# A {#a-top}\n\n## Sec {#a-sec}\nx\n", id="a"),
            _doc("d.md", "x\n", id="d", derives_from=[RawEdge(ref="a"), RawEdge(ref="a#a-sec")]),
        ]
    )
    assert [n.id for n in impact(lat, "a")] == ["d"]


def test_impact_cycle_terminates():
    # 'a' and 'b' mutually derive from each other; the walk must terminate, not loop.
    lat = build_lattice(
        [
            _doc("a.md", "# A {#a-top}\nx\n", id="a", derives_from=[RawEdge(ref="b")]),
            _doc("b.md", "# B {#b-top}\nx\n", id="b", derives_from=[RawEdge(ref="a")]),
        ]
    )
    assert {n.id for n in impact(lat, "a")} == {"a", "b"}


def test_impact_reaches_dependents_of_an_affected_nodes_sections():
    # 'mid' derives from file 'top' and itself contains section {#mid-sec};
    # 'deep' derives from that section. Editing 'top' must reach 'deep' because
    # mid's file (hence mid-sec) is effectively changed by mid being affected.
    lat = build_lattice(
        [
            _doc("top.md", "# Top {#top-h}\nx\n", id="top"),
            _doc(
                "mid.md",
                "# Mid {#mid-h}\n\n## Sec {#mid-sec}\nx\n",
                id="mid",
                derives_from=[RawEdge(ref="top")],
            ),
            _doc("deep.md", "x\n", id="deep", derives_from=[RawEdge(ref="mid#mid-sec")]),
        ]
    )
    assert {n.id for n in impact(lat, "top")} == {"mid", "deep"}


def test_expand_targets_unknown_id_returns_empty():
    lat = build_lattice([_doc("a.md", "# A {#a-top}\nx\n", id="a")])
    assert expand_targets(lat, "ghost") == set()


def test_expand_targets_file_without_anchors_is_just_its_id():
    lat = build_lattice([_doc("a.md", "plain body, no headings\n", id="a")])
    assert expand_targets(lat, "a") == {TargetId("a")}


def test_impact_results_sorted_by_id():
    # Three dependents enqueued in non-sorted insertion order; impact promises sorted ids.
    lat = build_lattice(
        [
            _doc("up.md", "# Up {#u}\nx\n", id="up"),
            _doc("zeta.md", "x\n", id="zeta", derives_from=[RawEdge(ref="up#u")]),
            _doc("alpha.md", "x\n", id="alpha", derives_from=[RawEdge(ref="up#u")]),
            _doc("mid.md", "x\n", id="mid", derives_from=[RawEdge(ref="up#u")]),
        ]
    )
    assert [n.id for n in impact(lat, "up#u")] == ["alpha", "mid", "zeta"]


def test_bare_anchor_token_is_unknown_but_namespaced_resolves():
    # A bare anchor token no longer resolves; the namespaced form does.
    lat = build_lattice(
        [
            _doc("a.md", "# A {#a-top}\n\n## Sec {#sec}\nx\n", id="a"),
            _doc("d.md", "x\n", id="d", derives_from=[RawEdge(ref="a#sec")]),
        ]
    )
    with pytest.raises(ValidationError):
        impact(lat, "sec")  # bare anchor is not a file id -> unknown
    assert {n.id for n in impact(lat, "a#sec")} == {"d"}
