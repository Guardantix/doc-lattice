"""Tests for ref resolution and content lookup."""

from pathlib import Path

import pytest

from game_lattice.error_types import BrokenRefError
from game_lattice.loader import build_lattice
from game_lattice.model import Lattice, Location, Node, NodeMeta, ParsedDoc
from game_lattice.resolve import node_for_path, target_content


def _lattice() -> Lattice:
    body = "# Doc {#doc}\nfile body\n\n## Accent {#accent}\naccent body\n"
    node = Node(
        id="doc",
        title=None,
        layer=None,
        authority=None,
        path=Path("doc.md"),
        body=body,
        derives_from=(),
        tickets=(),
    )
    return Lattice(
        nodes_by_id={"doc": node},
        index={
            "doc": Location(path=Path("doc.md"), kind="file", span=(1, 6)),
            "accent": Location(path=Path("doc.md"), kind="section", span=(4, 6)),
        },
        dependents={},
        ancestors={},
        file_id_by_path={Path("doc.md"): "doc"},
        anchors_by_path={Path("doc.md"): frozenset({"accent"})},
    )


def test_target_content_section():
    content = target_content(_lattice(), "accent")
    assert "accent body" in content
    assert "{#accent}" not in content


def test_target_content_section_exact_via_build_lattice():
    body = "# Up {#up-top}\nintro\n\n## Accent {#accent}\naccent body\nmore\n"
    docs = [ParsedDoc(Path("up.md"), NodeMeta(id="up"), body)]
    lat = build_lattice(docs)
    # heading line keeps its text but loses the {#anchor} marker; span runs to EOF
    assert target_content(lat, "accent") == "## Accent\naccent body\nmore"


def test_target_content_file_is_whole_body():
    lat = _lattice()
    assert target_content(lat, "doc") == lat.nodes_by_id["doc"].body


def test_target_content_broken_raises():
    with pytest.raises(BrokenRefError) as exc:
        target_content(_lattice(), "missing")
    assert exc.value.code == "BROKEN_REF"
    assert "missing" in str(exc.value)


def test_node_for_path_returns_owning_node_for_an_anchor():
    docs = [ParsedDoc(Path("up.md"), NodeMeta(id="up", authority="binding"), "# Up {#sec}\nbody\n")]
    lat = build_lattice(docs)
    owner = node_for_path(lat, lat.index["sec"].path)
    assert owner.id == "up"
    assert owner.authority == "binding"


def test_node_for_path_unowned_path_raises():
    with pytest.raises(BrokenRefError) as exc:
        node_for_path(_lattice(), Path("unknown.md"))
    assert exc.value.code == "BROKEN_REF"
    assert "unknown.md" in str(exc.value)
