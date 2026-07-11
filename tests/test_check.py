"""Tests for check."""

from pathlib import Path

from doc_lattice.check import EdgeStatus, check_lattice, has_drift, statuses_json
from doc_lattice.config import load_config
from doc_lattice.hashing import content_hash
from doc_lattice.loader import build_lattice
from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge, TargetId
from doc_lattice.orchestrate import load_lattice
from doc_lattice.resolve import target_content
from doc_lattice.sections import build_toc, section_span, section_text


def test_statuses_json_returns_exact_payload_shape():
    statuses = [
        EdgeStatus(
            source_id="down",
            target_ref="up#section",
            target_id=TargetId("up", "section"),
            state="STALE",
            expected="old-hash",
            actual="new-hash",
        ),
        EdgeStatus(
            source_id="broken",
            target_ref="missing",
            target_id=None,
            state="BROKEN",
            expected=None,
            actual=None,
        ),
    ]

    assert statuses_json(statuses) == {
        "edges": [
            {
                "source_id": "down",
                "target_ref": "up#section",
                "target_id": "up#section",
                "state": "STALE",
                "expected": "old-hash",
                "actual": "new-hash",
            },
            {
                "source_id": "broken",
                "target_ref": "missing",
                "target_id": None,
                "state": "BROKEN",
                "expected": None,
                "actual": None,
            },
        ]
    }


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


def test_check_memoizes_shared_target_hash(monkeypatch):
    docs = [
        ParsedDoc(Path("up.md"), NodeMeta(id="up"), "# Up {#sec}\nup body\n"),
        *[
            ParsedDoc(
                Path(f"down-{number}.md"),
                NodeMeta(id=f"down-{number}", derives_from=[RawEdge(ref="up#sec")]),
                "downstream body\n",
            )
            for number in range(3)
        ],
    ]
    lattice = build_lattice(docs)
    calls = 0

    def counting_content_hash(content: str) -> str:
        nonlocal calls
        calls += 1
        return content_hash(content)

    monkeypatch.setattr("doc_lattice.resolve.content_hash", counting_content_hash)

    statuses = check_lattice(lattice)
    target_id = TargetId("up", "sec")
    actual = content_hash(target_content(lattice, target_id))

    assert len(statuses) == 3
    assert all(status.target_id == target_id for status in statuses)
    assert all(status.actual == actual for status in statuses)
    assert calls == 1

    second_statuses = check_lattice(lattice)

    assert second_statuses == statuses
    assert calls == 2


def test_has_drift_false_when_all_ok():
    up_body = "# Up {#accent}\naccent\n"
    span = section_span(build_toc(up_body), 0, len(up_body.splitlines()))
    seen = content_hash(section_text(up_body, span))
    docs = [
        ParsedDoc(Path("up.md"), NodeMeta(id="up"), up_body),
        ParsedDoc(
            Path("down.md"),
            NodeMeta(id="down", derives_from=[RawEdge(ref="up#accent", seen=seen)]),
            "x\n",
        ),
    ]
    statuses = check_lattice(build_lattice(docs))
    assert all(s.state == "OK" for s in statuses)
    assert all(s.expected == s.actual for s in statuses)  # OK means locked == current
    assert has_drift(statuses) is False
