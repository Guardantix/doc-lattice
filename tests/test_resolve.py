"""Tests for ref resolution and content lookup."""

from pathlib import Path

import pytest

from doc_lattice.error_types import BrokenRefError
from doc_lattice.hashing import content_hash
from doc_lattice.loader import build_lattice
from doc_lattice.model import Lattice, Location, Node, NodeMeta, ParsedDoc, TargetId
from doc_lattice.resolve import cached_target_hash, node_for_path, target_content


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
            TargetId("doc"): Location(path=Path("doc.md"), kind="file", span=(1, 6)),
            TargetId("doc", "accent"): Location(path=Path("doc.md"), kind="section", span=(4, 6)),
        },
        dependents={},
        ancestors={},
        file_id_by_path={Path("doc.md"): "doc"},
        anchors_by_path={Path("doc.md"): frozenset({TargetId("doc", "accent")})},
    )


def test_target_content_section():
    content = target_content(_lattice(), TargetId("doc", "accent"))
    assert "accent body" in content
    assert "{#accent}" not in content


def test_target_content_section_exact_via_build_lattice():
    body = "# Up {#up-top}\nintro\n\n## Accent {#accent}\naccent body\nmore\n"
    docs = [ParsedDoc(Path("up.md"), NodeMeta(id="up"), body)]
    lat = build_lattice(docs)
    # heading line keeps its text but loses the {#anchor} marker; span runs to EOF
    assert target_content(lat, TargetId("up", "accent")) == "## Accent\naccent body\nmore"


def test_target_content_file_is_whole_body():
    lat = _lattice()
    assert target_content(lat, TargetId("doc")) == lat.nodes_by_id["doc"].body


def test_cached_target_hash_hashes_each_target_once(monkeypatch: pytest.MonkeyPatch):
    calls = 0

    def counting_content_hash(content: str) -> str:
        nonlocal calls
        calls += 1
        return content_hash(content)

    monkeypatch.setattr("doc_lattice.resolve.content_hash", counting_content_hash)

    lattice = _lattice()
    cache: dict[TargetId, str] = {}
    section_target = TargetId("doc", "accent")
    file_target = TargetId("doc")

    first = cached_target_hash(lattice, section_target, cache)
    repeated = cached_target_hash(lattice, section_target, cache)
    file_hash = cached_target_hash(lattice, file_target, cache)

    assert first == repeated == content_hash(target_content(lattice, section_target))
    assert file_hash == content_hash(target_content(lattice, file_target))
    assert calls == 2


def test_target_content_broken_raises():
    with pytest.raises(BrokenRefError) as exc:
        target_content(_lattice(), TargetId("missing"))
    assert exc.value.code == "BROKEN_REF"
    assert "missing" in str(exc.value)


def test_node_for_path_returns_owning_node_for_an_anchor():
    docs = [ParsedDoc(Path("up.md"), NodeMeta(id="up", authority="binding"), "# Up {#sec}\nbody\n")]
    lat = build_lattice(docs)
    owner = node_for_path(lat, lat.index[TargetId("up", "sec")].path)
    assert owner.id == "up"
    assert owner.authority == "binding"


def test_node_for_path_unowned_path_raises():
    with pytest.raises(BrokenRefError) as exc:
        node_for_path(_lattice(), Path("unknown.md"))
    assert exc.value.code == "BROKEN_REF"
    assert "unknown.md" in str(exc.value)
