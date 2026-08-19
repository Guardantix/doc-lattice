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


@pytest.mark.parametrize(
    "seen", [12, 12.5, True, ["h"], {"h": 1}], ids=["int", "float", "bool", "list", "mapping"]
)
def test_rawedge_rejects_a_non_string_seen(seen):
    # Strict mode is what keeps `seen: 12` out of the accepted input subset while a tagged
    # `!!str 12` stays in it: the constructed type decides, not the spelling. Reconcile's
    # tolerance for a non-string `seen` at write time is defensive recovery, not acceptance.
    with pytest.raises(PydanticValidationError):
        RawEdge.model_validate({"ref": "a#b", "seen": seen})


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


# GTX-208 (AD-35): every string a tracked document keeps is refused a control character here,
# at the one boundary all five of them cross, rather than at each sink that prints them.
@pytest.mark.parametrize(
    "bad",
    [
        {"id": "no\x1bde"},
        {"id": "x", "title": "ti\x9btle"},
        {"id": "x", "tickets": ["GTX-1", "GTX-\t2"]},
        {"id": "x", "derives_from": [{"ref": "up\x7f"}]},
        {"id": "x", "derives_from": [{"ref": "up", "seen": "hash\n"}]},
    ],
    ids=["id", "title", "tickets", "ref", "seen"],
)
def test_nodemeta_rejects_a_control_character_in_every_string_it_keeps(bad):
    with pytest.raises(PydanticValidationError):
        NodeMeta.model_validate(bad)


def test_the_control_character_message_names_the_code_point_and_not_the_value():
    # A diagnostic that echoed the value would print the byte the rule exists to refuse, so the
    # message is pinned to the position-and-code-point spelling rather than only to its type.
    with pytest.raises(PydanticValidationError) as exc:
        NodeMeta.model_validate({"id": "no\x1bde"})

    (error,) = exc.value.errors(include_url=False, include_input=False)
    assert error["loc"] == ("id",)
    assert "U+001B" in error["msg"]
    assert "index 2" in error["msg"]
    assert "\x1b" not in error["msg"]


def test_a_trailing_line_break_is_answered_with_the_chomping_fix():
    # A block scalar written `|` or `>` keeps a trailing break, which is the way an author
    # reaches this rule without meaning to, so that case gets the fix that actually applies
    # rather than the general "remove it".
    with pytest.raises(PydanticValidationError) as exc:
        NodeMeta.model_validate({"id": "x", "title": "A folded title\n"})

    (error,) = exc.value.errors(include_url=False, include_input=False)
    assert "drop the trailing line break" in error["msg"]


def test_an_interior_line_break_is_answered_with_the_joining_fix_not_the_chomping_one():
    # An interior break survives every chomping mode: a `|-` spanning two lines is already
    # chomped and still constructs one, and a double-quoted `"a\nb"` has no chomping indicator
    # to change at all. Sending either author to `-` names a fix they have already applied or
    # cannot apply, so position decides the advice.
    with pytest.raises(PydanticValidationError) as exc:
        NodeMeta.model_validate({"id": "x", "title": "first\nsecond"})

    (error,) = exc.value.errors(include_url=False, include_input=False)
    assert "join the lines" in error["msg"]
    assert "chomp" not in error["msg"]


def test_a_carriage_return_is_answered_with_the_general_fix():
    # No chomping mode and no folding produces a carriage return: YAML normalizes a literal one
    # to a line feed, so a value carrying one was written as an escape and removing it is the
    # only fix that applies.
    with pytest.raises(PydanticValidationError) as exc:
        NodeMeta.model_validate({"id": "x", "title": "a\rb"})

    (error,) = exc.value.errors(include_url=False, include_input=False)
    assert "reaches terminal output as written" in error["msg"]


def test_a_non_break_control_is_answered_with_the_general_fix():
    with pytest.raises(PydanticValidationError) as exc:
        NodeMeta.model_validate({"id": "x", "title": "a\x1b[31mb"})

    (error,) = exc.value.errors(include_url=False, include_input=False)
    assert "reaches terminal output as written" in error["msg"]


def test_nodemeta_keeps_printable_non_ascii_and_the_range_neighbors():
    # The rule is the C0, DEL, and C1 ranges and nothing wider. NBSP sits one above the C1 top,
    # and a line separator, a paragraph separator, a BOM, and ordinary accented text, CJK, and
    # emoji are not controls at all. The neighbors are built from code points rather than
    # written literally, since several of them are invisible in a source file.
    neighbors = "".join(chr(code) for code in (0xA0, 0x2028, 0x2029, 0xFEFF))
    title = f"Cafe {neighbors} nihongo \U0001f3ae"

    meta = NodeMeta.model_validate({"id": "cafe-design", "title": title, "tickets": ["GTX-1"]})

    assert meta.id == "cafe-design"
    assert meta.title == title


def test_rawedge_rejects_a_control_character_in_ref_and_seen():
    with pytest.raises(PydanticValidationError):
        RawEdge.model_validate({"ref": "a#b\x1b"})
    with pytest.raises(PydanticValidationError):
        RawEdge.model_validate({"ref": "a#b", "seen": "h\x1b"})
