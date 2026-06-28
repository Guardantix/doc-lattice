"""Tests for impact."""

from pathlib import Path

import pytest

from game_lattice.error_types import ValidationError
from game_lattice.impact import expand_targets, impact
from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge


def _doc(path: str, body: str, **meta) -> ParsedDoc:
    return ParsedDoc(Path(path), NodeMeta(**meta), body)


def test_section_token_expands_to_ancestors_and_file():
    body = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    # Editing 'child' also changes the whole-file hash, so the file id 'a' is included.
    assert expand_targets(lat, "child") == {"child", "parent", "a"}


def test_impact_section_reaches_whole_file_dependents():
    # 'whole' derives from the file 'up'; 'sub' derives from section 'sec' inside up.
    # Editing 'sec' changes up's whole-file hash, so 'whole' is affected too.
    parent = "# Up {#up-top}\n\n## Sec {#sec}\nx\n"
    lat = build_lattice(
        [
            _doc("up.md", parent, id="up"),
            _doc("whole.md", "x\n", id="whole", derives_from=[RawEdge(ref="up")]),
            _doc("sub.md", "x\n", id="sub", derives_from=[RawEdge(ref="sec")]),
        ]
    )
    assert {n.id for n in impact(lat, "sec")} == {"whole", "sub"}


def test_file_token_expands_to_its_anchors():
    body = "# A {#a-top}\n\n## Sec {#sec}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    assert expand_targets(lat, "a") == {"a", "a-top", "sec"}


def test_impact_includes_parent_dependents_for_nested_edit():
    parent = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice(
        [
            _doc("up.md", parent, id="up"),
            _doc("d-parent.md", "x\n", id="d-parent", derives_from=[RawEdge(ref="parent")]),
            _doc("d-child.md", "x\n", id="d-child", derives_from=[RawEdge(ref="child")]),
        ]
    )
    affected = {n.id for n in impact(lat, "child")}
    assert affected == {"d-parent", "d-child"}


def test_impact_is_transitive():
    lat = build_lattice(
        [
            _doc("up.md", "# Up {#u}\nx\n", id="up"),
            _doc("mid.md", "x\n", id="mid", derives_from=[RawEdge(ref="u")]),
            _doc("low.md", "x\n", id="low", derives_from=[RawEdge(ref="mid")]),
        ]
    )
    assert {n.id for n in impact(lat, "u")} == {"mid", "low"}


def test_impact_unknown_token_raises():
    lat = build_lattice([_doc("a.md", "# A {#a-top}\nx\n", id="a")])
    with pytest.raises(ValidationError):
        impact(lat, "nonexistent")


def test_impact_known_id_with_no_dependents_is_empty():
    lat = build_lattice([_doc("a.md", "# A {#a-top}\nx\n", id="a")])
    assert impact(lat, "a") == []


def test_impact_diamond_reaches_each_node_once():
    # 'd' derives from both the file 'a' and its section 'a-sec'; editing 'a' reaches d
    # via two targets, but it must appear exactly once.
    lat = build_lattice(
        [
            _doc("a.md", "# A {#a-top}\n\n## Sec {#a-sec}\nx\n", id="a"),
            _doc("d.md", "x\n", id="d", derives_from=[RawEdge(ref="a"), RawEdge(ref="a-sec")]),
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
