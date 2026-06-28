"""Tests for ticket domain types."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from game_lattice.constants import LinearStateType
from game_lattice.tickets import Finding, Ticket, TicketState


def _state(type_: LinearStateType = "completed") -> TicketState:
    return TicketState(name="Done", type=type_)


def test_ticket_construction():
    ticket = Ticket(
        identifier="PC-228",
        title="Accent tokens",
        url="https://linear.app/acme/issue/PC-228",
        state=_state(),
        parent=None,
        children=(),
    )
    assert ticket.state.type == "completed"
    assert ticket.children == ()


def test_string_fields_are_control_stripped():
    ticket = Ticket(
        identifier="PC-228",
        title="a\x1bb",
        url="https://x/\x07PC-228",
        state=TicketState(name="In\x7fReview", type="started"),
        parent=None,
        children=(),
    )
    assert ticket.title == "ab"
    assert ticket.url == "https://x/PC-228"
    assert ticket.state.name == "InReview"


def test_invalid_state_type_rejected():
    with pytest.raises(ValidationError):
        TicketState(name="Weird", type="archived")  # ty: ignore[invalid-argument-type]


def test_graded_finding_has_ticket_and_no_reason():
    ticket = Ticket(
        identifier="PC-228", title=None, url="https://x", state=_state(), parent=None, children=()
    )
    finding = Finding(
        severity="DANGER",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#accent",),
        ticket_ref="PC-228",
        reason=None,
        ticket=ticket,
    )
    assert finding.reason is None
    assert finding.ticket is ticket


def test_blocked_finding_has_reason_and_no_ticket():
    finding = Finding(
        severity="BLOCKED",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#motion",),
        ticket_ref="PC-999",
        reason="not-found",
        ticket=None,
    )
    assert finding.reason == "not-found"
    assert finding.ticket is None
