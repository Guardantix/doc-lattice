"""Tests for domain model."""

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from doc_lattice.model import (
    Edge,
    Lattice,
    Location,
    Node,
    NodeMeta,
    ParsedDoc,
    RawEdge,
    TargetId,
    parse_ref,
)


def test_edge_resolve_links_ref_to_index():
    index = {
        TargetId("art-direction", "accent"): Location(
            path=Path("a.md"), kind="section", span=(1, 2)
        )
    }
    edge = Edge.resolve("art-direction#accent", "h", index)
    assert edge.target_ref == "art-direction#accent"
    assert edge.target_id == TargetId("art-direction", "accent")
    assert edge.seen == "h"


def test_edge_resolve_unknown_ref_is_broken():
    edge = Edge.resolve("ghost", None, {})
    assert edge.target_ref == "ghost"
    assert edge.target_id is None
    assert edge.seen is None


def test_edge_resolve_broken_ref_preserves_seen():
    edge = Edge.resolve("ghost", "lockedhashlockedhashlockedhash00", {})
    assert edge.target_id is None
    assert edge.seen == "lockedhashlockedhashlockedhash00"


def test_nodemeta_validates_and_defaults():
    meta = NodeMeta.model_validate({"id": "pc-design"})
    assert meta.id == "pc-design"
    assert meta.derives_from == []
    assert meta.tickets == []


def test_nodemeta_forbids_extra_keys():
    with pytest.raises(PydanticValidationError):
        NodeMeta.model_validate({"id": "x", "typoo": 1})


def test_nodemeta_parses_edges():
    meta = NodeMeta.model_validate(
        {"id": "x", "derives_from": [{"ref": "a#b", "seen": "deadbeef"}]}
    )
    assert meta.derives_from[0] == RawEdge(ref="a#b", seen="deadbeef")


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "x", "layer": "bogus"},  # not a Layer literal
        {"id": "x", "authority": "canonical"},  # not an Authority literal
        {},  # missing required id
        {"id": 123},  # strict: int is not str
        {"id": "x", "tickets": [1]},  # strict: int element is not str
    ],
)
def test_nodemeta_rejects_invalid_frontmatter(bad):
    with pytest.raises(PydanticValidationError):
        NodeMeta.model_validate(bad)


def test_nodemeta_accepts_valid_literals():
    meta = NodeMeta.model_validate({"id": "x", "layer": "design", "authority": "binding"})
    assert meta.layer == "design"
    assert meta.authority == "binding"


def test_rawedge_requires_ref():
    with pytest.raises(PydanticValidationError):
        RawEdge.model_validate({"seen": "h"})


def test_rawedge_forbids_extra_keys():
    with pytest.raises(PydanticValidationError):
        RawEdge.model_validate({"ref": "a#b", "seen": "h", "sen": "typo"})


def test_rawedge_seen_defaults_none():
    assert RawEdge.model_validate({"ref": "a#b"}).seen is None


def test_dataclasses_are_frozen():
    edge = Edge(target_ref="a#b", target_id=TargetId("a", "b"), seen=None)
    with pytest.raises(AttributeError):
        edge.seen = "x"  # ty: ignore[invalid-assignment]


def test_lattice_holds_maps():
    node = Node(
        id="x",
        title=None,
        layer=None,
        authority=None,
        path=Path("x.md"),
        body="",
        derives_from=(),
        tickets=(),
    )
    lat = Lattice(
        nodes_by_id={"x": node},
        index={TargetId("x"): Location(path=Path("x.md"), kind="file", span=(1, 1))},
        dependents={},
        ancestors={},
        file_id_by_path={Path("x.md"): "x"},
        anchors_by_path={Path("x.md"): frozenset()},
    )
    assert lat.nodes_by_id["x"].id == "x"
    assert lat.file_id_by_path[Path("x.md")] == "x"
    assert ParsedDoc(path=Path("x.md"), meta=NodeMeta(id="x"), body="").meta.id == "x"


def test_parse_ref_namespaced_is_file_scoped():
    assert parse_ref("art-direction#accent") == TargetId("art-direction", "accent")


def test_parse_ref_bare_is_a_file_id():
    assert parse_ref("accent") == TargetId("accent")
    assert parse_ref("accent").anchor is None


def test_parse_ref_splits_on_last_hash():
    assert parse_ref("a#b#c") == TargetId("a#b", "c")


def test_target_id_as_ref_roundtrips():
    assert TargetId("save-format", "slot-table").as_ref() == "save-format#slot-table"
    assert TargetId("save-format").as_ref() == "save-format"


def test_target_id_is_hashable_and_frozen():
    tid = TargetId("f", "a")
    assert tid in {TargetId("f", "a")}  # hashable, value-equal
    with pytest.raises(AttributeError):
        tid.anchor = "b"  # ty: ignore[invalid-assignment]


def test_nodemeta_rejects_hash_in_id():
    with pytest.raises(PydanticValidationError):
        NodeMeta.model_validate({"id": "a#b"})
