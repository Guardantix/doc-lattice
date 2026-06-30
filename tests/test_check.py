"""Tests for check."""

from pathlib import Path

from game_lattice.check import check_lattice, has_drift
from game_lattice.config import load_config
from game_lattice.hashing import content_hash
from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge
from game_lattice.orchestrate import load_lattice
from game_lattice.resolve import target_content
from game_lattice.sections import build_toc, section_span, section_text


def test_check_classifies_each_state(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    by_pair = {(s.source_id, s.target_ref): s.state for s in check_lattice(lat)}
    assert by_pair[("pc-design", "art-direction#accent")] == "STALE"
    assert by_pair[("pc-design", "art-direction#motion")] == "UNRECONCILED"
    assert by_pair[("gdd", "ghost")] == "BROKEN"


def test_has_drift_true_when_any_non_ok(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    assert has_drift(check_lattice(lat)) is True


def test_check_populates_expected_and_actual_per_state(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    by_ref = {(s.source_id, s.target_ref): s for s in check_lattice(lat)}

    stale = by_ref[("pc-design", "art-direction#accent")]
    assert stale.state == "STALE"
    assert stale.target_id is not None  # a STALE edge always has a resolved target
    assert stale.expected == "staleseenhashstaleseenhashstale00"  # the locked seen
    assert stale.actual == content_hash(target_content(lat, stale.target_id))
    assert stale.expected != stale.actual

    unrec = by_ref[("pc-design", "art-direction#motion")]
    assert unrec.state == "UNRECONCILED"
    assert unrec.expected is None  # never reconciled, so no locked hash
    assert unrec.actual is not None

    broken = by_ref[("gdd", "ghost")]
    assert broken.state == "BROKEN"
    assert broken.target_id is None
    assert broken.actual is None  # nothing to hash for an unresolved target
    assert broken.expected is None  # fixture's ghost ref was never reconciled


def test_check_output_sorted_by_source_then_edge_order(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    order = [(s.source_id, s.target_ref) for s in check_lattice(lat)]
    # sorted node ids: art-direction (no edges -> absent), gdd, pc-design;
    # within pc-design the frontmatter order (accent before motion) is preserved.
    assert order == [
        ("gdd", "ghost"),
        ("pc-design", "art-direction#accent"),
        ("pc-design", "art-direction#motion"),
    ]


def test_broken_edge_preserves_seen_as_expected():
    docs = [
        ParsedDoc(
            Path("down.md"),
            NodeMeta(
                id="down",
                derives_from=[RawEdge(ref="ghost", seen="deadbeefdeadbeefdeadbeefdeadbeef")],
            ),
            "body\n",
        ),
    ]
    [status] = check_lattice(build_lattice(docs))
    assert status.state == "BROKEN"
    assert status.target_id is None
    assert status.actual is None
    # seen survives even though the ref no longer resolves
    assert status.expected == "deadbeefdeadbeefdeadbeefdeadbeef"


def test_has_drift_false_when_all_ok():
    up_body = "# Up {#accent}\naccent\n"
    span = section_span(build_toc(up_body), 0, len(up_body.splitlines()))
    seen = content_hash(section_text(up_body, span))
    docs = [
        ParsedDoc(Path("up.md"), NodeMeta(id="up"), up_body),
        ParsedDoc(
            Path("down.md"),
            NodeMeta(id="down", derives_from=[RawEdge(ref="accent", seen=seen)]),
            "x\n",
        ),
    ]
    statuses = check_lattice(build_lattice(docs))
    assert all(s.state == "OK" for s in statuses)
    assert all(s.expected == s.actual for s in statuses)  # OK means locked == current
    assert has_drift(statuses) is False
