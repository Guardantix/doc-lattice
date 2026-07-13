"""Render check, lint, and impact reports to a console.

Holds the human console renderers for these commands; their JSON payload builders live
beside their result types in check.py/impact.py/lint.py, so this is the render half of
what linear_render.py keeps in one module.
"""

from rich.console import Console
from rich.markup import escape

from .check import EdgeStatus
from .constants import EdgeState
from .lint import LintResult
from .model import Node

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


def render_statuses(console: Console, statuses: list[EdgeStatus]) -> None:
    """Render check statuses to a Rich console.

    Args:
        console: Destination console.
        statuses: Edge classifications to render in order.
    """
    for status in statuses:
        color = _STATE_COLORS[status.state]
        console.print(
            f"[{color}]{status.state:<{_STATE_COL_WIDTH}}[/{color}] "
            f"{escape(status.source_id)} -> {escape(status.target_ref)}"
        )


def render_lint(console: Console, result: LintResult) -> None:
    """Render authority-lint findings to a Rich console.

    Args:
        console: Destination console.
        result: Authority-lint violations and skipped edges to render.
    """
    for violation in result.violations:
        console.print(
            f"[red]VIOLATION[/red]  {escape(violation.source_id)} "
            f"({violation.source_authority}) -> {escape(violation.target_ref)} "
            f"({violation.target_authority})"
        )
    console.print(_skip_summary(result))


def render_impact(console: Console, affected: list[tuple[Node, int]]) -> None:
    """Render affected nodes to a Rich console.

    Args:
        console: Destination console.
        affected: Affected nodes paired with their minimum impact depths.
    """
    for node, _impact_depth_not_shown in affected:
        tickets = ", ".join(node.tickets) if node.tickets else "-"
        console.print(f"{escape(node.id)}  ({escape(str(node.path))})  tickets: {escape(tickets)}")
