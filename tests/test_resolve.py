"""Tests for ref resolution and content lookup."""

from pathlib import Path

import pytest

from game_lattice.error_types import BrokenRefError
from game_lattice.model import Lattice, Location, Node
from game_lattice.resolve import target_content


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
    assert "accent body" in target_content(_lattice(), "accent")
    assert "{#accent}" not in target_content(_lattice(), "accent")


def test_target_content_file_is_whole_body():
    assert target_content(_lattice(), "doc") == _lattice().nodes_by_id["doc"].body


def test_target_content_broken_raises():
    with pytest.raises(BrokenRefError):
        target_content(_lattice(), "missing")
