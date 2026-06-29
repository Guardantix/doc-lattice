"""Tests for the linear renderer."""

import io
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from rich.console import Console

from game_lattice.constants import VALID_SEVERITIES
from game_lattice.linear_render import (
    _SEVERITY_COLORS,
    findings_json,
    render_findings,
    render_safe,
)
from game_lattice.tickets import Finding, Ticket, TicketState


def _ticket():
    return Ticket(
        identifier="PC-1",
        title="Accent",
        url="https://x/PC-1",
        state=TicketState(name="Done", type="completed"),
        parent=None,
        children=(),
    )


def _danger():
    return Finding(
        severity="DANGER",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#accent",),
        ticket_ref="PC-1",
        reason=None,
        ticket=_ticket(),
    )


def _blocked():
    return Finding(
        severity="BLOCKED",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#motion",),
        ticket_ref="PC-999",
        reason="not-found",
        ticket=None,
    )


def test_json_shape_for_graded_and_blocked():
    payload = findings_json([_danger(), _blocked()])
    assert list(payload) == ["findings"]
    danger, blocked = payload["findings"]
    assert danger["ticket"]["state"]["type"] == "completed"
    assert danger["ticket_ref"] == "PC-1"
    assert blocked["ticket"] is None
    assert blocked["reason"] == "not-found"


def _is_control(codepoint: int) -> bool:
    """True for a C0, DEL, or C1 control byte."""
    return codepoint < 0x20 or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F


@given(st.text())
def test_render_safe_output_is_control_free(text: str):
    # render_safe is NOT idempotent: rich.markup.escape re-escapes a balanced ``[tag]`` on
    # each pass (render_safe("[/]") != render_safe(render_safe("[/]"))). The universal
    # property is only that no control byte (C0, DEL, or C1) survives.
    once = render_safe(text)
    assert not any(_is_control(ord(ch)) for ch in once)


def test_severity_colors_cover_all_severities():
    # A new Severity member without a color would raise KeyError at render time; pinning the
    # color map to the Literal surfaces that gap here instead.
    assert set(_SEVERITY_COLORS) == VALID_SEVERITIES


def test_render_table_escapes_and_shows_severity():
    finding = Finding(
        severity="DANGER",
        node_id="node[/]",
        node_title=None,
        node_path=Path("docs/x.md"),
        drifted_refs=("ref\x1bx",),
        ticket_ref="PC-1",
        reason=None,
        ticket=_ticket(),
    )
    output = io.StringIO()
    console = Console(file=output, width=200)
    render_findings(console, [finding])
    out = output.getvalue()
    assert "DANGER" in out
    assert "\x1b" not in out  # control byte stripped
    assert "node[/]" in out  # markup-escaped, rendered literally


def test_render_does_not_let_state_name_inject_markup():
    ticket = Ticket(
        identifier="PC-1",
        title="t",
        url="https://x/PC-1",
        state=TicketState(name="bold red", type="completed"),  # a real rich style
        parent=None,
        children=(),
    )
    finding = Finding(
        severity="DANGER",
        node_id="n",
        node_title=None,
        node_path=Path("docs/x.md"),
        drifted_refs=("a#b",),
        ticket_ref="PC-1",
        reason=None,
        ticket=ticket,
    )
    output = io.StringIO()
    console = Console(file=output, width=200)
    render_findings(console, [finding])
    out = output.getvalue()
    assert "[bold red]" in out  # rendered literally, not consumed as a style tag
    assert "bold red" in out  # the state-name text is not lost
