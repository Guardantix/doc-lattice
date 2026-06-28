"""Tests for graph rendering."""

from pathlib import Path

from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge
from game_lattice.render import to_dot, to_mermaid


def _lattice():
    return build_lattice(
        [
            ParsedDoc(Path("up.md"), NodeMeta(id="up", title="Up"), "# Up {#u}\nx\n"),
            ParsedDoc(Path("down.md"), NodeMeta(id="down", derives_from=[RawEdge(ref="u")]), "x\n"),
        ]
    )


def test_mermaid_has_nodes_and_edges():
    out = to_mermaid(_lattice(), set())
    assert out.startswith("graph TD")
    assert "up" in out
    assert "down" in out
    assert "-->" in out


def test_mermaid_styles_stale_edges():
    out = to_mermaid(_lattice(), {("down", "u")})
    assert "-.->" in out  # dashed arrow for stale


def test_dot_is_digraph():
    out = to_dot(_lattice(), set())
    assert out.startswith("digraph lattice")
    assert "->" in out


def test_section_edge_drawn_from_owning_file_not_bare_anchor():
    # 'down' derives from section anchor 'u', which lives in file 'up'. The edge must
    # connect the tracked file node 'up', not the bare anchor 'u' (spec 6.4).
    lines = to_mermaid(_lattice(), set()).splitlines()
    assert "    up --> down" in lines
    assert "    u --> down" not in lines


def test_dot_escapes_backslash_and_quote_in_label():
    # A title with a backslash and quotes must not corrupt the DOT string: the backslash
    # is doubled and each quote escaped. A naive replace leaves a trailing backslash that
    # would escape the closing quote and break the label.
    lat = build_lattice([ParsedDoc(Path("a.md"), NodeMeta(id="a", title='C:\\path "x"'), "body\n")])
    out = to_dot(lat, set())
    assert r'"a" [label="C:\\path \"x\""];' in out


def test_mermaid_sanitizes_node_ids_with_spaces():
    lat = build_lattice(
        [
            ParsedDoc(Path("a.md"), NodeMeta(id="my doc", title="My Doc"), "# A {#sec}\nx\n"),
            ParsedDoc(Path("b.md"), NodeMeta(id="b", derives_from=[RawEdge(ref="my doc")]), "x\n"),
        ]
    )
    out = to_mermaid(lat, set())
    assert 'my_doc["My Doc"]' in out  # id sanitized, title preserved
    assert "    my_doc --> b" in out
    assert "my doc[" not in out  # raw space-bearing id would be invalid mermaid
