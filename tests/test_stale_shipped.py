"""Tests for the pure stale-shipped join and trigger builders."""

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from game_lattice.constants import VALID_LINEAR_STATE_TYPES, VALID_SEVERITIES
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
