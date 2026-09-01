"""Render check, lint, and impact reports to a console.

Holds the human console renderers for these commands; their JSON payload builders live
beside their result types in check.py/impact.py/lint.py, so this is the render half of
what linear_render.py keeps in one module.
"""

from collections.abc import Mapping, Sequence

from rich.console import Console
from rich.markup import escape

from .check import EdgeStatus
from .constants import EDGE_STATES, EdgeState
from .lint import LintResult
from .model import Node, format_collision
from .path_utils import format_path_for_display

_STATE_COL_WIDTH = 13  # widest EdgeState ("UNRECONCILED") is 12 chars, plus one trailing space

# Tied to the EdgeState Literal by test_state_colors_cover_every_edge_state: a new state member
# without a color here fails that test instead of raising KeyError at render time.
_STATE_COLORS: dict[EdgeState, str] = {
    "OK": "green",
    "STALE": "yellow",
    "UNRECONCILED": "yellow",
    "BROKEN": "red",
    "AMBIGUOUS": "red",
}


def _skip_summary(result: LintResult) -> str:
    """Render the one-line coverage summary printed after any human lint run.

    The ambiguous count is appended only when it is non-zero, the way the unranked breakdown
    already is. Naming it matters because this is the line a reader tails: a run that printed
    ambiguity rows above and then summarized only the ladder read as clean on the one line most
    likely to be quoted out of the run. It stays outside the ``ladder`` count rather than folded
    into it, because this command does not gate on ambiguity and that number is what its exit
    code answers for.
    """
    violations = len(result.violations)
    unranked = len(result.skipped)
    targets = sum(1 for skipped in result.skipped if skipped.reason == "target-unannotated")
    sources = sum(1 for skipped in result.skipped if skipped.reason == "source-unannotated")
    label = "violation" if violations == 1 else "violations"
    line = f"{violations} ladder {label}, {unranked} edges unranked"
    if unranked:
        line += f" ({targets} target unannotated, {sources} source unannotated)"
    if result.ambiguous:
        line += f", {len(result.ambiguous)} ambiguous"
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
        detail = f" ({escape(format_collision(status.collision))})" if status.collision else ""
        console.print(
            f"[{color}]{status.state:<{_STATE_COL_WIDTH}}[/{color}] "
            f"{escape(status.source_id)} -> {escape(status.target_ref)}{detail}",
            highlight=False,
            soft_wrap=True,
        )
    console.print(_state_summary(summary), highlight=False, soft_wrap=True)


def render_lint(console: Console, result: LintResult) -> None:
    """Render authority-lint findings to a Rich console.

    Renders the ambiguous-target block first, then the authority-ladder findings, mirroring
    render_impact and render_findings so lint's stdout ordering does not depend on its caller.

    Args:
        console: Destination console.
        result: Authority-lint violations, ambiguous edges, and skipped edges to render.
    """
    render_ambiguous(console, result.ambiguous)
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


def render_ambiguous(console: Console, statuses: Sequence[EdgeStatus]) -> None:
    """Render ambiguous-target findings, one record per line.

    The one human spelling ``lint``, ``impact``, and ``linear`` share, so the same condition
    reads the same way wherever it is reported. ``check`` renders its own row instead, because
    the state is part of that command's per-edge listing rather than an appended block. The
    colour and the state column width are read from the same two declarations ``render_statuses``
    reads, so the state cannot end up rendered two ways depending on which command emitted it.

    Args:
        console: Destination console.
        statuses: Edge classifications; only ``AMBIGUOUS`` members are printed.
    """
    # highlight=False and soft_wrap for the reason every renderer in this module carries them:
    # Rich's highlighter bolds bare numbers and bold survives no_color, and each record must
    # stay one line at any width so a pipe or a grep gets the whole record.
    color = _STATE_COLORS["AMBIGUOUS"]
    for status in statuses:
        if status.state != "AMBIGUOUS":
            continue
        console.print(
            f"[{color}]{'AMBIGUOUS':<{_STATE_COL_WIDTH}}[/{color}] {escape(status.source_id)} -> "
            f"{escape(status.target_ref)} ({escape(format_collision(status.collision))})",
            highlight=False,
            soft_wrap=True,
        )


def render_impact(
    console: Console, affected: list[tuple[Node, int]], ambiguous: Sequence[EdgeStatus] = ()
) -> None:
    """Render affected nodes to a Rich console.

    Each node is one record terminated by exactly one newline, so a path never breaks across
    lines and stays copyable and pipeable. `soft_wrap` opts this site out of Rich's wrapping
    and cropping the same way the long, copy-sensitive output sites in the CLI adapters do.
    `highlight=False` matches render_statuses and render_lint: Rich's default highlighter bolds
    bare numbers and parentheses, and bold survives no_color, so a numbered id (adr-001), a
    dated path, or a ticket (GTX-48) would otherwise leak ANSI under --no-color.

    Ambiguity is printed first because it is a finding while the node list below it is merely
    informational: a reader scanning top to bottom sees what needs attention before what merely
    changed.

    Args:
        console: Destination console.
        affected: Affected nodes paired with their minimum impact depths.
        ambiguous: Ambiguous edges in the same lattice, from ``check.ambiguous_edges``.
    """
    render_ambiguous(console, ambiguous)
    for node, _impact_depth_not_shown in affected:
        tickets = ", ".join(node.tickets) if node.tickets else "-"
        displayed_path = escape(format_path_for_display(node.path))
        console.print(
            f"{escape(node.id)}  ({displayed_path})  tickets: {escape(tickets)}",
            highlight=False,
            soft_wrap=True,
        )
