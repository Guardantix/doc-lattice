"""Render stale-shipped findings as a severity-grouped table or a JSON payload."""

from collections.abc import Sequence

from rich.console import Console
from rich.markup import escape

from .check import EdgeStatus, ambiguous_json
from .constants import Severity
from .report_render import render_ambiguous
from .text_utils import strip_control_chars
from .tickets import Finding

# Tied to the Severity Literal by test_severity_colors_cover_all_severities: a new severity
# member without a color here fails that test instead of raising KeyError at render time.
_SEVERITY_COLORS: dict[Severity, str] = {
    "DANGER": "red",
    "BLOCKED": "magenta",
    "WARNING": "yellow",
    "INFO": "cyan",
}

_SEVERITY_COLUMN_WIDTH = 8  # widest Severity label ("BLOCKED"/"WARNING") plus one space


def render_safe(text: str) -> str:
    """Make any external string safe to print: strip control bytes, then escape markup.

    Args:
        text: A string from a repo or a Linear response.

    Returns:
        The string with control bytes removed and rich markup escaped.
    """
    return escape(strip_control_chars(text))


def findings_json(findings: Sequence[Finding], ambiguous: Sequence[EdgeStatus] = ()) -> dict:
    """Build the JSON payload.

    Args:
        findings: The ordered findings.
        ambiguous: Ambiguous edges in the same lattice, from ``check.ambiguous_edges``. The
            block is always present, empty when there are none, so a consumer never has to
            distinguish "absent" from "none found".

    Returns:
        An object with a ``findings`` key whose entries carry ``severity``, ``node_id``,
        ``node_title``, ``node_path``, ``drifted_refs``, ``ticket_ref``, ``reason``, and
        ``ticket`` (the ticket's JSON dump, or null when it was not resolved), plus the shared
        ``ambiguous`` block.
    """
    return {
        "findings": [
            {
                "severity": finding.severity,
                "node_id": finding.node_id,
                "node_title": finding.node_title,
                "node_path": str(finding.node_path),
                "drifted_refs": list(finding.drifted_refs),
                "ticket_ref": finding.ticket_ref,
                "reason": finding.reason,
                "ticket": (
                    finding.ticket.model_dump(mode="json") if finding.ticket is not None else None
                ),
            }
            for finding in findings
        ],
        "ambiguous": ambiguous_json(ambiguous),
    }


def render_findings(
    console: Console, findings: Sequence[Finding], ambiguous: Sequence[EdgeStatus] = ()
) -> None:
    """Print the findings grouped by severity, escaping every external string.

    Ambiguity is rendered first, before the empty-findings early return, so an ambiguous
    lattice with no drift findings still reports it rather than printing only the all-clear
    line.

    Args:
        console: The output console.
        findings: The ordered findings.
        ambiguous: Ambiguous edges in the same lattice, from ``check.ambiguous_edges``.
    """
    # highlight=False on both prints, matching the three renderers in report_render.py: Rich's
    # default highlighter bolds bare numbers, and bold survives no_color, so it emits ANSI under
    # --no-color. That bites a ticket ref (GTX-96), a numbered node id (adr-001), and any drifted
    # ref carrying a digit. The explicit severity color below is markup, which highlight=False
    # leaves alone.
    # soft_wrap on both prints: each finding and the all-clear line are one record on one line at
    # any width, so a pipe or a grep gets the whole record rather than the fragment Rich's
    # wrapping would leave on a narrow console. Same contract GTX-2 and GTX-48 applied to impact,
    # check, and lint.
    render_ambiguous(console, ambiguous)
    if not findings:
        console.print("no stale-shipped findings", highlight=False, soft_wrap=True)
        return
    for finding in findings:
        color = _SEVERITY_COLORS[finding.severity]
        refs = ", ".join(render_safe(ref) for ref in finding.drifted_refs)
        if finding.ticket is not None:
            detail = render_safe(f"{finding.ticket_ref} [{finding.ticket.state.name}]")
        else:
            detail = render_safe(f"{finding.ticket_ref} ({finding.reason})")
        console.print(
            f"[{color}]{finding.severity:<{_SEVERITY_COLUMN_WIDTH}}[/{color}] "
            f"{render_safe(finding.node_id)}  {detail}  drift: {refs}",
            highlight=False,
            soft_wrap=True,
        )
