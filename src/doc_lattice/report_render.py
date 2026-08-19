"""Render check, lint, and impact reports to a console.

Holds the human console renderers for these commands; their JSON payload builders live
beside their result types in check.py/impact.py/lint.py, so this is the render half of
what linear_render.py keeps in one module.
"""

from collections.abc import Mapping

from rich.console import Console
from rich.markup import escape

from .check import EdgeStatus
from .constants import EDGE_STATES, EdgeState
from .lint import LintResult
from .model import Node
from .path_utils import format_path_for_display

_STATE_COL_WIDTH = 13  # widest EdgeState ("UNRECONCILED") is 12 chars, plus one trailing space

# Tied to the EdgeState Literal by test_state_colors_cover_every_edge_state: a new state member
# without a color here fails that test instead of raising KeyError at render time.
_STATE_COLORS: dict[EdgeState, str] = {
    "OK": "green",
    "STALE": "yellow",
    "UNRECONCILED": "yellow",
    "BROKEN": "red",
}


def _skip_summary(result: LintResult) -> str:
    """Render the one-line coverage summary printed after any human lint run."""
    violations = len(result.violations)
    unranked = len(result.skipped)
    targets = sum(1 for skipped in result.skipped if skipped.reason == "target-unannotated")
    sources = sum(1 for skipped in result.skipped if skipped.reason == "source-unannotated")
    label = "violation" if violations == 1 else "violations"
    line = f"{violations} ladder {label}, {unranked} edges unranked"
    if unranked:
        line += f" ({targets} target unannotated, {sources} source unannotated)"
    return line


def _state_summary(summary: Mapping[EdgeState, int]) -> str:
    """Render the one-line verdict printed after any human check run."""
    total = sum(summary.values())
    label = "edge" if total == 1 else "edges"
    # get(state, 0) rather than indexing: the parameter is a Mapping, so a caller may hand
    # over a sparse counter that simply omits a state that did not occur. The rendered line
    # promises every state, and a missing key is that state at zero, not an error.
    breakdown = ", ".join(f"{summary.get(state, 0)} {state}" for state in EDGE_STATES)
    return f"{total} {label}: {breakdown}"


def render_statuses(
    console: Console, statuses: list[EdgeStatus], summary: Mapping[EdgeState, int]
) -> None:
    """Render check statuses to a Rich console, terminated by the verdict line.

    Args:
        console: Destination console.
        statuses: Edge classifications to render in order, already narrowed to what the
            caller wants displayed.
        summary: Per-state counts over every classified edge, not only the displayed ones,
            so a truncated or filtered listing still ends with an honest verdict.
    """
    # highlight=False on both prints: Rich's default highlighter bolds bare numbers, and bold
    # survives no_color, so it emits ANSI under --no-color. That bites the verdict's counts and
    # equally any id carrying a number (adr-001, rfc-2119). The explicit state color below is
    # markup, which highlight=False leaves alone.
    # soft_wrap on both prints: each row and the verdict are one record on one line at any
    # width, so `check | tail -1` (or grep) gets the whole record rather than the fragment
    # Rich's wrapping would leave on a narrow console. Same fix GTX-2 applied to render_impact.
    for status in statuses:
        color = _STATE_COLORS[status.state]
        console.print(
            f"[{color}]{status.state:<{_STATE_COL_WIDTH}}[/{color}] "
            f"{escape(status.source_id)} -> {escape(status.target_ref)}",
            highlight=False,
            soft_wrap=True,
        )
    console.print(_state_summary(summary), highlight=False, soft_wrap=True)


def render_lint(console: Console, result: LintResult) -> None:
    """Render authority-lint findings to a Rich console.

    Args:
        console: Destination console.
        result: Authority-lint violations and skipped edges to render.
    """
    # soft_wrap on both prints: each violation and the skip summary are one record on one
    # line at any width, matching render_statuses. The summary carries no id or ref of its
    # own, but soft_wrap keeps every print in this renderer to the same record contract.
    # highlight=False on both prints for the same reason render_statuses carries it: Rich's
    # default highlighter bolds bare numbers and parentheses, and bold survives no_color, so
    # both the skip summary's counts and any id carrying a number (adr-001, rfc-2119) would
    # leak ANSI under --no-color. The explicit VIOLATION color below is markup, which
    # highlight=False leaves alone.
    for violation in result.violations:
        console.print(
            f"[red]VIOLATION[/red]  {escape(violation.source_id)} "
            f"({violation.source_authority}) -> {escape(violation.target_ref)} "
            f"({violation.target_authority})",
            highlight=False,
            soft_wrap=True,
        )
    console.print(_skip_summary(result), highlight=False, soft_wrap=True)


def render_impact(console: Console, affected: list[tuple[Node, int]]) -> None:
    """Render affected nodes to a Rich console.

    Each node is one record terminated by exactly one newline, so a path never breaks across
    lines and stays copyable and pipeable. `soft_wrap` opts this site out of Rich's wrapping
    and cropping the same way the long, copy-sensitive output sites in the CLI adapters do.
    `highlight=False` matches render_statuses and render_lint: Rich's default highlighter bolds
    bare numbers and parentheses, and bold survives no_color, so a numbered id (adr-001), a
    dated path, or a ticket (GTX-48) would otherwise leak ANSI under --no-color.

    Args:
        console: Destination console.
        affected: Affected nodes paired with their minimum impact depths.
    """
    for node, _impact_depth_not_shown in affected:
        tickets = ", ".join(node.tickets) if node.tickets else "-"
        displayed_path = escape(format_path_for_display(node.path))
        console.print(
            f"{escape(node.id)}  ({displayed_path})  tickets: {escape(tickets)}",
            highlight=False,
            soft_wrap=True,
        )
