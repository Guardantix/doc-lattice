"""Tests for domain model."""

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from game_lattice.model import Edge, Lattice, Location, Node, NodeMeta, ParsedDoc, RawEdge


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


def test_dataclasses_are_frozen():
    edge = Edge(target_ref="a#b", target_id="b", seen=None)
    with pytest.raises(AttributeError):
        edge.seen = "x"  # type: ignore


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
        index={"x": Location(path=Path("x.md"), kind="file", span=(1, 1))},
        dependents={},
        ancestors={},
    )
    assert lat.nodes_by_id["x"].id == "x"
    assert ParsedDoc(path=Path("x.md"), meta=NodeMeta(id="x"), body="").meta.id == "x"
