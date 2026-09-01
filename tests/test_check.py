"""Tests for check."""

from collections import Counter
from pathlib import Path

from doc_lattice.check import (
    EdgeStatus,
    ambiguous_edges,
    check_lattice,
    has_drift,
    statuses_json,
    summarize_statuses,
)
from doc_lattice.config import load_config
from doc_lattice.constants import EDGE_STATES
from doc_lattice.hashing import content_hash
from doc_lattice.loader import build_lattice
from doc_lattice.model import Lattice, NodeMeta, ParsedDoc, RawEdge, TargetId
from doc_lattice.orchestrate import load_lattice
from doc_lattice.resolve import cached_target_hash, target_content
from doc_lattice.sections import build_toc, section_spans, section_text


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

    assert statuses_json(statuses, summarize_statuses(statuses)) == {
        "edges": [
            {
                "source_id": "down",
                "target_ref": "up#section",
                "target_id": "up#section",
                "state": "STALE",
                "expected": "old-hash",
                "actual": "new-hash",
                "collision": [],
            },
            {
                "source_id": "broken",
                "target_ref": "missing",
                "target_id": None,
                "state": "BROKEN",
                "expected": None,
                "actual": None,
                "collision": [],
            },
        ],
        "summary": {"OK": 0, "STALE": 1, "UNRECONCILED": 0, "BROKEN": 1, "AMBIGUOUS": 0},
    }


def test_summarize_statuses_covers_every_state_including_zero_counts():
    statuses = [
        EdgeStatus("down", "up", TargetId("up"), "OK", "h", "h"),
        EdgeStatus("other", "up", TargetId("up"), "OK", "h", "h"),
        EdgeStatus("third", "up", TargetId("up"), "STALE", "old", "h"),
    ]

    summary = summarize_statuses(statuses)

    assert summary == {"OK": 2, "STALE": 1, "UNRECONCILED": 0, "BROKEN": 0, "AMBIGUOUS": 0}
    assert tuple(summary) == EDGE_STATES


def test_summarize_statuses_of_no_edges_is_all_zeroes():
    summary = summarize_statuses([])

    assert summary == {"OK": 0, "STALE": 0, "UNRECONCILED": 0, "BROKEN": 0, "AMBIGUOUS": 0}
    assert sum(summary.values()) == 0


def test_statuses_json_summary_counts_are_independent_of_the_serialized_edges():
    # The CLI narrows `edges` with --only while the summary keeps counting every classified
    # edge, so the payload must carry the summary it was given rather than recomputing it.
    every = [
        EdgeStatus("down", "up", TargetId("up"), "OK", "h", "h"),
        EdgeStatus("third", "up", TargetId("up"), "STALE", "old", "h"),
    ]
    displayed = [status for status in every if status.state == "STALE"]

    payload = statuses_json(displayed, summarize_statuses(every))

    assert [edge["state"] for edge in payload["edges"]] == ["STALE"]
    assert payload["summary"] == {
        "OK": 1,
        "STALE": 1,
        "UNRECONCILED": 0,
        "BROKEN": 0,
        "AMBIGUOUS": 0,
    }


def test_statuses_json_summary_of_a_sparse_counter_names_every_state():
    # The parameter is a Mapping, so a caller may pass a counter that omits absent states;
    # the payload still promises a count for each one rather than raising KeyError.
    statuses = [EdgeStatus("down", "up", TargetId("up"), "OK", "h", "h")]

    payload = statuses_json(statuses, Counter(status.state for status in statuses))

    assert payload["summary"] == {
        "OK": 1,
        "STALE": 0,
        "UNRECONCILED": 0,
        "BROKEN": 0,
        "AMBIGUOUS": 0,
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
    span = section_spans(build_toc(up_body), len(up_body.splitlines()))[0]
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


def test_the_edge_state_domain_ends_with_ambiguous():
    assert EDGE_STATES == ("OK", "STALE", "UNRECONCILED", "BROKEN", "AMBIGUOUS")


def _ambiguous_lattice() -> Lattice:
    return build_lattice(
        [
            ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body="# Notes\n\n# Notes\n"),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#notes", "seen": "a" * 32}]}
                ),
                body="# Down\n",
            ),
        ]
    )


def test_an_edge_into_a_collision_component_is_ambiguous():
    statuses = check_lattice(_ambiguous_lattice())

    assert [status.state for status in statuses] == ["AMBIGUOUS"]
    assert statuses[0].actual is None
    assert statuses[0].expected == "a" * 32
    assert [member.label for member in statuses[0].collision] == ["Notes", "Notes"]


def test_check_reports_drift_on_an_ambiguous_edge():
    assert has_drift(check_lattice(_ambiguous_lattice())) is True


def test_ambiguous_edges_finds_the_same_rows_without_hashing():
    lattice = _ambiguous_lattice()

    assert ambiguous_edges(lattice) == tuple(
        status for status in check_lattice(lattice) if status.state == "AMBIGUOUS"
    )


def test_an_edge_into_an_addressable_only_collision_is_ambiguous():
    # The commented-out second "Overview" is invisible to the full CommonMark parse but still
    # addressed by the restricted scanner, so the collision only shows up in the addressable
    # inventory's own trace. An edge into the surviving "overview" id must still go AMBIGUOUS.
    lattice = build_lattice(
        [
            ParsedDoc(
                path=Path("docs/up.md"),
                meta=NodeMeta(id="up"),
                body="# Overview\n\ntext\n\n<!--\n# Overview\n-->\n",
            ),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#overview", "seen": "a" * 32}]}
                ),
                body="# Down\n",
            ),
        ]
    )

    statuses = check_lattice(lattice)

    assert [status.state for status in statuses] == ["AMBIGUOUS"]
    assert [member.label for member in statuses[0].collision] == ["Overview", "Overview"]


def test_the_check_json_payload_carries_the_collision():
    statuses = check_lattice(_ambiguous_lattice())

    payload = statuses_json(statuses, summarize_statuses(statuses))

    assert payload["edges"][0]["collision"] == [
        {"label": "Notes", "line": 1},
        {"label": "Notes", "line": 3},
    ]
    assert payload["summary"]["AMBIGUOUS"] == 1


def test_a_pre_v7_seen_value_on_a_nested_target_reads_stale_and_re_blesses():
    body = "# Parent\n\n## Child\nbody\n"
    up = ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body=body)
    pre_v7 = content_hash(section_text(body, (3, 4)))
    down = ParsedDoc(
        path=Path("docs/down.md"),
        meta=NodeMeta.model_validate(
            {"id": "down", "derives_from": [{"ref": "up#child", "seen": pre_v7}]}
        ),
        body="# Down\n",
    )
    lattice = build_lattice([up, down])

    assert check_lattice(lattice)[0].state == "STALE"

    re_blessed = cached_target_hash(lattice, TargetId("up", "child"), {})
    revived = build_lattice(
        [
            up,
            ParsedDoc(
                path=down.path,
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#child", "seen": re_blessed}]}
                ),
                body=down.body,
            ),
        ]
    )
    assert check_lattice(revived)[0].state == "OK"


def test_rewording_an_ancestor_stales_a_child_targeted_edge():
    before = build_lattice(
        [
            ParsedDoc(
                path=Path("docs/up.md"), meta=NodeMeta(id="up"), body="# Parent\n\n## Child\nbody\n"
            )
        ]
    )
    seen = cached_target_hash(before, TargetId("up", "child"), {})
    after = build_lattice(
        [
            ParsedDoc(
                path=Path("docs/up.md"),
                meta=NodeMeta(id="up"),
                body="# Reworded Parent\n\n## Child\nbody\n",
            ),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#child", "seen": seen}]}
                ),
                body="# Down\n",
            ),
        ]
    )

    assert check_lattice(after)[0].state == "STALE"


def _setext_products(setup_a_heading: str, second_product: str) -> list[ParsedDoc]:
    """Two products with setext headings, each holding a byte-identical '### Setup'."""
    body = (
        "# Products\n\n"
        "Product A\n---------\n\n"
        f"{setup_a_heading}\nrun the installer\n\n"
        f"{second_product}"
    )
    return [ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body=body)]


def test_a_transient_collision_under_setext_parents_reads_stale():
    # GTX-471. The setext 'Product A' is not addressable, so before the level-based chain the
    # ancestor context was empty and '#setup' transferred to Product B under a byte-identical
    # section: no run ever sees a collision and the blessed 'seen' still matches.
    before = _setext_products("### Setup", "")
    blessed = cached_target_hash(build_lattice(before), TargetId("up", "setup"), {})

    # One edit: rename A's heading and add an identical Product B / ### Setup.
    after = _setext_products(
        "### Install", "Product B\n---------\n\n### Setup\nrun the installer\n"
    )
    down = ParsedDoc(
        path=Path("docs/down.md"),
        meta=NodeMeta.model_validate(
            {"id": "down", "derives_from": [{"ref": "up#setup", "seen": blessed}]}
        ),
        body="# Down\n",
    )
    lattice = build_lattice([*after, down])

    # '#setup' now resolves to Product B's section, whose chain names Product B.
    assert lattice.ancestor_context[TargetId("up", "setup")] == ("# Products", "## Product B")
    assert check_lattice(lattice)[0].state == "STALE"


def test_the_same_transient_collision_reads_stale_with_atx_parents():
    # The ATX spelling already worked; it must keep agreeing with the setext one.
    before = "# Products\n\n## Product A\n\n### Setup\nrun the installer\n"
    blessed = cached_target_hash(
        build_lattice([ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body=before)]),
        TargetId("up", "setup"),
        {},
    )
    after = (
        "# Products\n\n## Product A\n\n### Install\nrun the installer\n\n"
        "## Product B\n\n### Setup\nrun the installer\n"
    )
    down = ParsedDoc(
        path=Path("docs/down.md"),
        meta=NodeMeta.model_validate(
            {"id": "down", "derives_from": [{"ref": "up#setup", "seen": blessed}]}
        ),
        body="# Down\n",
    )
    lattice = build_lattice(
        [ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body=after), down]
    )

    assert lattice.ancestor_context[TargetId("up", "setup")] == ("# Products", "## Product B")
    assert check_lattice(lattice)[0].state == "STALE"
