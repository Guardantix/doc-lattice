"""Tests for the pure stale-shipped join and trigger builders."""

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from game_lattice.check import check_lattice
from game_lattice.constants import VALID_LINEAR_STATE_TYPES, VALID_SEVERITIES
from game_lattice.error_types import ValidationError
from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge
from game_lattice.stale_shipped import (
    _SEVERITY_RANK,
    _STATE_SEVERITY,
    build_audit_trigger,
    build_from_trigger,
    stale_shipped,
)
from game_lattice.tickets import Ticket, TicketState

if TYPE_CHECKING:
    from game_lattice.constants import BlockedReason, LinearStateType


# A wrong seen hash, differing from the real content hash, that forces a STALE edge.
_STALE_SEEN = "staleseenstaleseenstaleseenstale"


def test_state_and_severity_maps_track_their_literals():
    # Tie the grading maps to their Literals: a renamed state member would otherwise leave a
    # stale key that silently grades as None (no finding), and a new severity would KeyError
    # on the rank lookup.
    assert set(_STATE_SEVERITY) <= VALID_LINEAR_STATE_TYPES
    assert set(_STATE_SEVERITY.values()) <= VALID_SEVERITIES
    assert set(_SEVERITY_RANK) == VALID_SEVERITIES


def _ticket(identifier: str, state_type: str) -> Ticket:
    return Ticket(
        identifier=identifier,
        title="t",
        url="https://x/" + identifier,
        state=TicketState(name=state_type, type=cast("LinearStateType", state_type)),
        parent=None,
        children=(),
    )


def _node(id_: str, body: str, *, derives=None, tickets=()) -> ParsedDoc:
    meta = NodeMeta(
        id=id_,
        derives_from=[RawEdge(ref=r, seen=s) for r, s in (derives or [])],
        tickets=list(tickets),
    )
    return ParsedDoc(path=Path(f"docs/{id_}.md"), meta=meta, body=body)


def _two_node_lattice(seen: str | None, tickets=("PC-1",)):
    up = _node("up", "# Up {#sec}\nbody v2\n")
    down = _node("down", "# Down\nb\n", derives=[("up#sec", seen)], tickets=tickets)
    return build_lattice([up, down])


@pytest.mark.parametrize(
    ("state_type", "severity"),
    [
        ("completed", "DANGER"),
        ("started", "WARNING"),
        ("unstarted", "INFO"),
        ("backlog", "INFO"),
    ],
)
def test_grading_by_state_type(state_type, severity):
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", state_type)}, {})
    assert [f.severity for f in findings] == [severity]


@pytest.mark.parametrize("state_type", ["canceled", "triage", "duplicate"])
def test_terminal_states_omitted(state_type):
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", state_type)}, {})
    assert findings == []


def test_unresolved_is_blocked_not_found():
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {}, {})
    assert findings[0].severity == "BLOCKED"
    assert findings[0].reason == "not-found"


def test_rejected_reason_is_carried():
    lattice = _two_node_lattice(seen=_STALE_SEEN, tickets=("SEC-9",))
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {}, {"SEC-9": cast("BlockedReason", "cross-team")})
    assert findings[0].reason == "cross-team"


def test_node_with_no_tickets_yields_nothing():
    lattice = _two_node_lattice(seen=_STALE_SEEN, tickets=())
    trigger = build_audit_trigger(lattice, None)
    assert stale_shipped(lattice, trigger, {}, {}) == []


def test_duplicate_ref_collapses():
    lattice = _two_node_lattice(seen=_STALE_SEEN, tickets=("PC-1", "PC-1"))
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", "completed")}, {})
    assert len(findings) == 1


def test_ok_edge_is_not_a_trigger():
    # seen=None is UNRECONCILED, not STALE, so it is not a trigger in audit mode.
    up = _node("up", "# Up {#sec}\nbody\n")
    down = _node("down", "# Down\nb\n", derives=[("up#sec", None)], tickets=("PC-1",))
    lattice = build_lattice([up, down])
    trigger = build_audit_trigger(lattice, None)
    assert trigger == {}


def test_target_scoping_includes_the_named_node_itself():
    # impact() returns strict dependents, so scoping to a STALE leaf must still audit it,
    # otherwise a gate narrowed to that node would pass while it ships a stale ticket.
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    assert "down" in build_audit_trigger(lattice, "down")


def test_target_scoping_includes_dependents():
    # Scoping to an upstream id still reaches its STALE dependents (existing behavior).
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    assert "down" in build_audit_trigger(lattice, "up")


def test_from_mode_whole_file_dependent_has_refs():
    # leaf derives from the WHOLE FILE 'mid' (not a section). After a change to up#sec, mid is
    # affected, so mid's file target must be in the closure or leaf's justifying ref is dropped.
    up = _node("up", "# Up {#sec}\nbody\n")
    mid = _node("mid", "# Mid\nbody\n", derives=[("up#sec", None)])
    leaf = _node("leaf", "# Leaf\nb\n", derives=[("mid", None)], tickets=("PC-1",))
    lattice = build_lattice([up, mid, leaf])
    trigger = build_from_trigger(lattice, "up#sec")
    assert "leaf" in trigger
    assert trigger["leaf"] == ("mid",)  # the whole-file ref is the justifying ref


def test_from_mode_transitive_dependent_has_refs():
    # up <- mid <- leaf. A change to up must give leaf non-empty drifted_refs.
    up = _node("up", "# Up {#sec}\nbody\n")
    mid = _node("mid", "# Mid {#midsec}\nbody\n", derives=[("up#sec", None)])
    leaf = _node("leaf", "# Leaf\nb\n", derives=[("mid#midsec", None)], tickets=("PC-1",))
    lattice = build_lattice([up, mid, leaf])
    trigger = build_from_trigger(lattice, "up#sec")
    assert "leaf" in trigger
    assert trigger["leaf"]  # non-empty justifying refs


def test_ordering_is_severity_then_node_then_ref():
    up = _node("up", "# Up {#sec}\nbody v2\n")
    a = _node(
        "a",
        "# A\nb\n",
        derives=[("up#sec", _STALE_SEEN)],
        tickets=("PC-2", "PC-1"),
    )
    lattice = build_lattice([up, a])
    trigger = build_audit_trigger(lattice, None)
    tickets = {"PC-1": _ticket("PC-1", "completed"), "PC-2": _ticket("PC-2", "started")}
    findings = stale_shipped(lattice, trigger, tickets, {})
    # DANGER (PC-1) sorts before WARNING (PC-2).
    assert [(f.severity, f.ticket_ref) for f in findings] == [
        ("DANGER", "PC-1"),
        ("WARNING", "PC-2"),
    ]


def test_build_audit_trigger_unknown_target_raises():
    # A typo'd --target must surface as a ValidationError, not silently scope to nothing.
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    with pytest.raises(ValidationError) as exc:
        build_audit_trigger(lattice, "nope")
    assert exc.value.code == "VALIDATION_ERROR"


def test_build_from_trigger_unknown_id_raises():
    # A typo'd --from must surface as a ValidationError, not an empty all-clear trigger.
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    with pytest.raises(ValidationError) as exc:
        build_from_trigger(lattice, "ghost")
    assert exc.value.code == "VALIDATION_ERROR"


def test_ordering_ticket_ref_tiebreak_within_severity():
    # One node, two DANGER tickets in non-sorted ref order; ticket_ref is the only tiebreak.
    # dict.fromkeys preserves frontmatter order (PC-2, PC-1); dropping the ticket_ref sort key
    # would leave the stable-sorted result as [PC-2, PC-1].
    up = _node("up", "# Up {#sec}\nbody v2\n")
    a = _node("a", "# A\nb\n", derives=[("up#sec", _STALE_SEEN)], tickets=("PC-2", "PC-1"))
    lattice = build_lattice([up, a])
    trigger = build_audit_trigger(lattice, None)
    tickets = {"PC-1": _ticket("PC-1", "completed"), "PC-2": _ticket("PC-2", "completed")}
    findings = stale_shipped(lattice, trigger, tickets, {})
    assert [f.ticket_ref for f in findings] == ["PC-1", "PC-2"]


def test_audit_target_scoping_excludes_unrelated_stale_node():
    # Two independent STALE chains; scoping to one must drop the other's node.
    up1 = _node("up1", "# Up1 {#s1}\nv2\n")
    down1 = _node("down1", "# D1\nb\n", derives=[("up1#s1", _STALE_SEEN)], tickets=("PC-1",))
    up2 = _node("up2", "# Up2 {#s2}\nv2\n")
    down2 = _node("down2", "# D2\nb\n", derives=[("up2#s2", _STALE_SEEN)], tickets=("PC-2",))
    lattice = build_lattice([up1, down1, up2, down2])
    trigger = build_audit_trigger(lattice, "down1")
    assert set(trigger) == {"down1"}  # down2 is STALE but out of scope


def test_graded_finding_carries_ticket_and_no_reason():
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    trigger = build_audit_trigger(lattice, None)
    ticket = _ticket("PC-1", "completed")
    [f] = stale_shipped(lattice, trigger, {"PC-1": ticket}, {})
    assert f.ticket is ticket
    assert f.reason is None
    assert f.ticket_ref == "PC-1"
    assert f.drifted_refs == ("up#sec",)  # trigger value flows into the finding


def test_blocked_finding_has_no_ticket():
    lattice = _two_node_lattice(seen=_STALE_SEEN)
    trigger = build_audit_trigger(lattice, None)
    [f] = stale_shipped(lattice, trigger, {}, {})
    assert f.ticket is None
    assert f.reason == "not-found"


def test_multiple_stale_refs_grouped_and_carried():
    up = _node("up", "# Up {#a}\nv2\n\n## B {#b}\nv2\n")
    down = _node(
        "down",
        "# Down\nb\n",
        derives=[("up#a", _STALE_SEEN), ("up#b", _STALE_SEEN)],
        tickets=("PC-1",),
    )
    lattice = build_lattice([up, down])
    trigger = build_audit_trigger(lattice, None)
    assert set(trigger["down"]) == {"up#a", "up#b"}
    [f] = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", "completed")}, {})
    assert set(f.drifted_refs) == {"up#a", "up#b"}


def test_ok_edge_is_not_a_trigger_when_reconciled():
    # A genuinely OK edge (seen == live hash) must not appear in the audit trigger.
    up = _node("up", "# Up {#sec}\nbody\n")
    down0 = _node("down", "# Down\nb\n", derives=[("up#sec", None)], tickets=("PC-1",))
    lat0 = build_lattice([up, down0])
    actual = next(s.actual for s in check_lattice(lat0) if s.source_id == "down")
    down = _node("down", "# Down\nb\n", derives=[("up#sec", actual)], tickets=("PC-1",))
    lattice = build_lattice([up, down])
    assert build_audit_trigger(lattice, None) == {}


def test_broken_edge_is_not_a_trigger():
    # A BROKEN edge (unresolved ref) is not STALE, so it is not an audit trigger.
    down = _node("down", "# Down\nb\n", derives=[("ghost", _STALE_SEEN)], tickets=("PC-1",))
    lattice = build_lattice([down])
    assert build_audit_trigger(lattice, None) == {}


def test_finding_carries_node_title_and_path():
    # _node never sets a title; build the node directly so node_title is non-null here.
    up = _node("up", "# Up {#sec}\nv2\n")
    meta = NodeMeta(
        id="down",
        title="Down Doc",
        derives_from=[RawEdge(ref="up#sec", seen=_STALE_SEEN)],
        tickets=["PC-1"],
    )
    down = ParsedDoc(path=Path("docs/down.md"), meta=meta, body="# D\nb\n")
    lattice = build_lattice([up, down])
    trigger = build_audit_trigger(lattice, None)
    [f] = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", "completed")}, {})
    assert f.node_title == "Down Doc"
    assert f.node_path == Path("docs/down.md")


# State types that grade into a finding; the rest (terminal) are dropped. Derived from the
# source-of-truth severity map so adding/removing a graded state cannot desync this test.
_GRADED_STATES = set(_STATE_SEVERITY)


@given(
    refs=st.lists(st.sampled_from(["PC-1", "PC-2", "PC-3"]), max_size=5),
    states=st.dictionaries(
        st.sampled_from(["PC-1", "PC-2", "PC-3"]),
        st.sampled_from(sorted(VALID_LINEAR_STATE_TYPES)),
    ),
)
def test_findings_are_sorted_and_lossless(refs, states):
    # stale_shipped is a pure deterministic sort/grade: output is sorted by
    # (rank, node_id, ticket_ref), and emitted refs are exactly the non-terminal, deduped
    # input refs (a missing ticket interleaves as a BLOCKED not-found finding).
    up = _node("up", "# Up {#sec}\nv2\n")
    down = _node("down", "# D\nb\n", derives=[("up#sec", _STALE_SEEN)], tickets=tuple(refs))
    lattice = build_lattice([up, down])
    trigger = build_audit_trigger(lattice, None)
    tickets = {r: _ticket(r, t) for r, t in states.items()}
    findings = stale_shipped(lattice, trigger, tickets, {})
    keys = [(_SEVERITY_RANK[f.severity], f.node_id, f.ticket_ref) for f in findings]
    assert keys == sorted(keys)
    emitted = {f.ticket_ref for f in findings}
    expected = {
        r for r in set(refs) if states.get(r, "not-found") in _GRADED_STATES or r not in states
    }
    assert emitted == expected
