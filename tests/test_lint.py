"""Tests for the authority-ladder lint."""

from pathlib import Path

from game_lattice.lint import LintResult, lint_lattice
from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge


def _doc(id_, authority=None, derives=(), body="x\n"):
    """Build a ParsedDoc with optional authority and derives_from refs."""
    return ParsedDoc(
        Path(f"{id_}.md"),
        NodeMeta(
            id=id_,
            authority=authority,
            derives_from=[RawEdge(ref=r) for r in derives],
        ),
        body,
    )


def _lattice(*docs):
    return build_lattice(list(docs))


def test_binding_deriving_from_derived_is_a_violation():
    lat = _lattice(
        _doc("up", authority="derived"),
        _doc("down", authority="binding", derives=("up",)),
    )
    result = lint_lattice(lat)
    assert len(result.violations) == 1
    v = result.violations[0]
    assert (v.source_id, v.source_authority) == ("down", "binding")
    assert (v.target_id, v.target_authority) == ("up", "derived")
    assert result.skipped == ()


def test_binding_deriving_from_exploratory_is_a_violation():
    lat = _lattice(
        _doc("up", authority="exploratory"),
        _doc("down", authority="binding", derives=("up",)),
    )
    assert len(lint_lattice(lat).violations) == 1


def test_derived_deriving_from_exploratory_is_a_violation():
    lat = _lattice(
        _doc("up", authority="exploratory"),
        _doc("down", authority="derived", derives=("up",)),
    )
    assert len(lint_lattice(lat).violations) == 1


def test_equal_authority_passes():
    lat = _lattice(
        _doc("up", authority="binding"),
        _doc("down", authority="binding", derives=("up",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert result.skipped == ()


def test_deriving_from_stronger_passes():
    lat = _lattice(
        _doc("up", authority="binding"),
        _doc("down", authority="derived", derives=("up",)),
    )
    assert lint_lattice(lat).violations == ()


def test_unannotated_source_is_skipped_not_failed():
    lat = _lattice(
        _doc("up", authority="binding"),
        _doc("down", authority=None, derives=("up",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "source-unannotated"
    assert result.skipped[0].source_id == "down"


def test_unannotated_target_is_skipped_not_failed():
    lat = _lattice(
        _doc("up", authority=None),
        _doc("down", authority="binding", derives=("up",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "target-unannotated"
    assert result.skipped[0].target_id == "up"


def test_broken_edge_is_not_a_violation_and_not_in_skips():
    lat = _lattice(_doc("down", authority="binding", derives=("ghost",)))
    result = lint_lattice(lat)
    assert result.violations == ()
    assert result.skipped == ()  # broken edges are check's concern, not counted here


def test_section_target_violation_uses_owning_file_authority():
    lat = _lattice(
        _doc("up", authority="derived", body="# Up {#sec}\nbody\n"),
        _doc("down", authority="binding", derives=("up#sec",)),
    )
    result = lint_lattice(lat)
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.target_id == "sec"
    assert v.target_ref == "up#sec"
    assert v.target_authority == "derived"  # inherited from the owning file "up"


def test_section_target_passes_when_owning_file_is_stronger():
    lat = _lattice(
        _doc("up", authority="binding", body="# Up {#sec}\nbody\n"),
        _doc("down", authority="derived", derives=("up#sec",)),
    )
    assert lint_lattice(lat).violations == ()


def test_section_target_skipped_when_owning_file_unannotated():
    lat = _lattice(
        _doc("up", authority=None, body="# Up {#sec}\nbody\n"),
        _doc("down", authority="binding", derives=("up#sec",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert result.skipped[0].reason == "target-unannotated"
    assert result.skipped[0].target_id == "sec"


def test_results_are_in_node_id_then_edge_order():
    lat = _lattice(
        _doc("weak1", authority="exploratory"),
        _doc("weak2", authority="exploratory"),
        _doc("a", authority="binding", derives=("weak1", "weak2")),
        _doc("b", authority="binding", derives=("weak1",)),
    )
    result = lint_lattice(lat)
    assert isinstance(result, LintResult)
    assert [v.source_id for v in result.violations] == ["a", "a", "b"]
    assert [v.target_id for v in result.violations] == ["weak1", "weak2", "weak1"]
