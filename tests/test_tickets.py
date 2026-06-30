"""Tests for ticket domain types."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from game_lattice.constants import LinearStateType
from game_lattice.tickets import Finding, Ticket, TicketRef, TicketState


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


def test_identifier_is_control_stripped():
    ticket = Ticket(
        identifier="PC-\x9b228",  # C1 CSI byte (0x9b) inside the identifier
        title=None,
        url="https://x",
        state=_state(),
        parent=None,
        children=(),
    )
    assert ticket.identifier == "PC-228"


def test_ticketref_control_stripping_and_title_default():
    ref = TicketRef(identifier="PC-\x1b200", state=_state("started"))
    assert ref.identifier == "PC-200"  # CleanStr on TicketRef.identifier
    assert ref.title is None  # CleanOptStr default (None branch)
    titled = TicketRef(identifier="PC-1", title="Sub\x07task", state=_state("unstarted"))
    assert titled.title == "Subtask"  # CleanOptStr non-None branch


def test_ticket_children_coerce_list_to_tuple():
    child = TicketRef(identifier="PC-1", title="Sub", state=_state("unstarted"))
    ticket = Ticket(
        identifier="PC-228",
        title="Accent",
        url="https://x",
        state=_state(),
        parent=None,
        children=[child],  # ty: ignore[invalid-argument-type]
    )
    assert ticket.children == (child,)
    assert isinstance(ticket.children, tuple)


def test_pydantic_ticket_models_are_frozen():
    ticket = Ticket(identifier="PC-1", url="https://x", state=_state())
    with pytest.raises(ValidationError):
        ticket.title = "mutated"


def test_finding_is_frozen():
    finding = Finding(
        severity="INFO",
        node_id="n",
        node_title=None,
        node_path=Path("x.md"),
        drifted_refs=(),
        ticket_ref="PC-1",
        reason=None,
        ticket=None,
    )
    with pytest.raises(AttributeError):
        finding.severity = "DANGER"  # ty: ignore[invalid-assignment]
