"""Tests for graph rendering."""

from pathlib import Path

from doc_lattice.loader import build_lattice
from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge, TargetId
from doc_lattice.render import to_dot, to_json, to_mermaid


def _lattice():
    return build_lattice(
        [
            ParsedDoc(Path("up.md"), NodeMeta(id="up", title="Up"), "# Up {#u}\nx\n"),
            ParsedDoc(
                Path("down.md"), NodeMeta(id="down", derives_from=[RawEdge(ref="up#u")]), "x\n"
            ),
        ]
    )


def test_mermaid_has_nodes_and_edges():
    out = to_mermaid(_lattice(), set())
    assert out.startswith("graph TD")
    assert 'n0["down"]' in out
    assert 'n1["Up"]' in out
    assert "n1 --> n0" in out


def test_mermaid_styles_stale_edges():
    out = to_mermaid(_lattice(), {("down", TargetId("up", "u"))})
    assert "-.->" in out  # dashed arrow for stale


def test_dot_is_digraph():
    out = to_dot(_lattice(), set())
    assert out.startswith("digraph lattice")
    assert "->" in out


def test_section_edge_drawn_from_owning_file_not_bare_anchor():
    # 'down' derives from section anchor 'u', which lives in file 'up'. The edge must
    # connect the generated ids for the tracked files, not a third id for anchor 'u' (spec 6.4).
    lines = to_mermaid(_lattice(), set()).splitlines()
    assert "    n1 --> n0" in lines
    assert len([line for line in lines if "-->" in line]) == 1


def test_dot_escapes_backslash_and_quote_in_label():
    # A title with a backslash and quotes must not corrupt the DOT string: the backslash
    # is doubled and each quote escaped. A naive replace leaves a trailing backslash that
    # would escape the closing quote and break the label.
    lat = build_lattice([ParsedDoc(Path("a.md"), NodeMeta(id="a", title='C:\\path "x"'), "body\n")])
    out = to_dot(lat, set())
    assert r'"a" [label="C:\\path \"x\""];' in out


def test_mermaid_uses_generated_ids_for_node_ids_with_spaces():
    lat = build_lattice(
        [
            ParsedDoc(Path("a.md"), NodeMeta(id="my doc", title="My Doc"), "# A {#sec}\nx\n"),
            ParsedDoc(Path("b.md"), NodeMeta(id="b", derives_from=[RawEdge(ref="my doc")]), "x\n"),
        ]
    )
    out = to_mermaid(lat, set())
    assert 'n1["My Doc"]' in out  # generated id is safe, title preserved
    assert "    n1 --> n0" in out
    assert "my doc[" not in out  # raw space-bearing id would be invalid mermaid


def test_mermaid_uses_distinct_generated_ids_for_colliding_node_ids():
    lat = build_lattice(
        [
            ParsedDoc(Path("hyphen.md"), NodeMeta(id="a-b"), "x\n"),
            ParsedDoc(
                Path("underscore.md"),
                NodeMeta(id="a_b", derives_from=[RawEdge(ref="a-b")]),
                "x\n",
            ),
        ]
    )

    assert to_mermaid(lat, set()) == ('graph TD\n    n0["a-b"]\n    n1["a_b"]\n    n0 --> n1\n')


def test_dot_styles_stale_edges():
    # The DOT dashed-style branch must fire when an edge is stale, mirroring the mermaid
    # dashed-arrow coverage; otherwise drift is rendered solid.
    out = to_dot(_lattice(), {("down", TargetId("up", "u"))})
    assert '    "up" -> "down" [style=dashed];' in out
    assert '    "up" -> "down";' not in out  # solid form absent when stale


def test_mermaid_escapes_double_quote_in_label():
    # A double quote in a title must become an apostrophe so the ["..."] label stays
    # well-formed; mermaid has no backslash escape inside the quotes.
    lat = build_lattice([ParsedDoc(Path("a.md"), NodeMeta(id="a", title='Say "hi"'), "x\n")])
    out = to_mermaid(lat, set())
    assert "n0[\"Say 'hi'\"]" in out  # quotes become apostrophes; label stays well-formed
    assert 'Say "hi"' not in out  # no raw double quote leaks into the bracketed label


def test_mermaid_omits_broken_edge_but_keeps_node():
    # An unresolved derives_from (target_id None) contributes no arrow, but the node that
    # owns the broken ref is still rendered.
    lat = build_lattice(
        [
            ParsedDoc(
                Path("g.md"),
                NodeMeta(id="g", title="G", derives_from=[RawEdge(ref="ghost")]),
                "x\n",
            )
        ]
    )
    out = to_mermaid(lat, set())
    assert 'n0["G"]' in out  # broken-ref node still rendered
    assert "-->" not in out  # the unresolved edge contributes no arrow


def test_multiple_section_edges_collapse_with_stale_or():
    # Two section edges between the same file pair collapse to one edge, drawn dashed if
    # any contributing edge is stale (stale-OR aggregation).
    docs = [
        ParsedDoc(Path("up.md"), NodeMeta(id="up", title="Up"), "# Up {#a}\nx\n## B {#b}\ny\n"),
        ParsedDoc(
            Path("down.md"),
            NodeMeta(id="down", derives_from=[RawEdge(ref="up#a"), RawEdge(ref="up#b")]),
            "x\n",
        ),
    ]
    lat = build_lattice(docs)
    solid = to_mermaid(lat, set()).splitlines()
    assert solid.count("    n1 --> n0") == 1  # two section edges collapse to one
    dashed = to_mermaid(lat, {("down", TargetId("up", "b"))})  # only the 'b' edge stale
    assert "    n1 -.-> n0" in dashed  # single collapsed edge is dashed
    assert "    n1 --> n0" not in dashed


def test_empty_lattice_renders_headers_only():
    # The empty-lattice boundary pins the exact preamble/postamble, notably that DOT still
    # closes its brace with no nodes or edges.
    empty = build_lattice([])
    assert to_mermaid(empty, set()) == "graph TD\n"
    assert to_dot(empty, set()) == "digraph lattice {\n}\n"


def test_label_falls_back_to_id_when_title_missing():
    # 'down' has no title, so _label falls back to the id verbatim as the bracketed label.
    out = to_mermaid(_lattice(), set())
    assert 'n0["down"]' in out  # no title -> id used verbatim as the label


def test_edges_emitted_in_sorted_order():
    # _graph_edges returns a sorted list so the diffed output is deterministic; two nodes
    # deriving from the same upstream emit edges sorted by (upstream, source).
    docs = [
        ParsedDoc(Path("u.md"), NodeMeta(id="u"), "# U {#u1}\nx\n"),
        ParsedDoc(Path("z.md"), NodeMeta(id="z", derives_from=[RawEdge(ref="u#u1")]), "x\n"),
        ParsedDoc(Path("a.md"), NodeMeta(id="a", derives_from=[RawEdge(ref="u#u1")]), "x\n"),
    ]
    out = to_mermaid(build_lattice(docs), set())
    edges = [ln for ln in out.splitlines() if "-->" in ln]
    assert edges == ["    n1 --> n0", "    n1 --> n2"]  # sorted by raw (upstream, source)


def test_dot_escapes_special_chars_in_node_id():
    # DOT does not sanitize ids (unlike mermaid), so _dot_escape on the node id is the only
    # guard keeping a quote-bearing id well-formed.
    lat = build_lattice([ParsedDoc(Path("a.md"), NodeMeta(id='weird"id', title="T"), "x\n")])
    out = to_dot(lat, set())
    assert r'"weird\"id" [label="T"];' in out


def test_json_has_nodes_and_edges_sorted_by_id():
    payload = to_json(_lattice(), set())
    assert payload["nodes"] == [
        {"id": "down", "title": None, "layer": None, "authority": None, "path": "down.md"},
        {"id": "up", "title": "Up", "layer": None, "authority": None, "path": "up.md"},
    ]
    assert payload["edges"] == [{"upstream": "up", "downstream": "down", "stale": False}]


def test_json_marks_stale_edges():
    payload = to_json(_lattice(), {("down", TargetId("up", "u"))})
    assert payload["edges"] == [{"upstream": "up", "downstream": "down", "stale": True}]


def test_json_edge_set_matches_mermaid_edge_set():
    # The JSON edges must be exactly the collapsed file-level triples Mermaid/DOT draw, so
    # the three renderers never disagree about what the graph looks like.
    lat = _lattice()
    stale = {("down", TargetId("up", "u"))}
    mermaid_edges = {
        tuple(line.strip().split(" -.-> " if "-.->" in line else " --> "))
        for line in to_mermaid(lat, stale).splitlines()
        if "->" in line
    }
    payload = to_json(lat, stale)
    mermaid_id = {node["id"]: f"n{index}" for index, node in enumerate(payload["nodes"])}
    json_edges = {
        (mermaid_id[e["upstream"]], mermaid_id[e["downstream"]]) for e in payload["edges"]
    }
    assert json_edges == mermaid_edges


def test_json_omits_broken_edge_but_keeps_node():
    # An unresolved derives_from (target_id None) contributes no edge, but the node that
    # owns the broken ref still appears in the node list.
    lat = build_lattice(
        [
            ParsedDoc(
                Path("g.md"),
                NodeMeta(id="g", title="G", derives_from=[RawEdge(ref="ghost")]),
                "x\n",
            )
        ]
    )
    payload = to_json(lat, set())
    assert payload["nodes"] == [
        {"id": "g", "title": "G", "layer": None, "authority": None, "path": "g.md"}
    ]
    assert payload["edges"] == []


def test_json_collapses_section_edges_with_stale_or():
    # Two section edges between the same file pair collapse to one edge in JSON too,
    # marked stale if any contributing edge is stale.
    docs = [
        ParsedDoc(Path("up.md"), NodeMeta(id="up", title="Up"), "# Up {#a}\nx\n## B {#b}\ny\n"),
        ParsedDoc(
            Path("down.md"),
            NodeMeta(id="down", derives_from=[RawEdge(ref="up#a"), RawEdge(ref="up#b")]),
            "x\n",
        ),
    ]
    lat = build_lattice(docs)
    payload = to_json(lat, {("down", TargetId("up", "b"))})
    assert payload["edges"] == [{"upstream": "up", "downstream": "down", "stale": True}]


def test_json_node_fields_include_layer_authority_and_path():
    lat = build_lattice(
        [ParsedDoc(Path("a.md"), NodeMeta(id="a", layer="design", authority="binding"), "x\n")]
    )
    payload = to_json(lat, set())
    assert payload["nodes"] == [
        {"id": "a", "title": None, "layer": "design", "authority": "binding", "path": "a.md"}
    ]


def test_empty_lattice_renders_json_headers_only():
    empty = build_lattice([])
    assert to_json(empty, set()) == {"nodes": [], "edges": []}
