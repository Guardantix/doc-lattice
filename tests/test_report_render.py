"""Tests for check, lint, and impact report rendering."""

from collections import Counter
from io import StringIO
from pathlib import Path
from typing import get_args

from rich.console import Console

from doc_lattice.check import EdgeStatus, summarize_statuses
from doc_lattice.constants import EDGE_STATES, EdgeState
from doc_lattice.lint import LadderViolation, LintResult, SkippedEdge
from doc_lattice.model import Node, TargetId
from doc_lattice.report_render import (
    _STATE_COLORS,
    _state_summary,
    render_impact,
    render_lint,
    render_statuses,
)


def _recording_console(width: int = 200) -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, record=True, width=width, color_system=None), output


def test_render_statuses_writes_exact_plain_text_and_escapes_markup():
    console, output = _recording_console()
    statuses = [
        EdgeStatus(
            source_id="down[/]",
            target_ref="up[bold]",
            target_id=None,
            state="BROKEN",
            expected=None,
            actual=None,
        )
    ]

    render_statuses(console, statuses, summarize_statuses(statuses))

    assert output.getvalue() == (
        "BROKEN        down[/] -> up[bold]\n1 edge: 0 OK, 0 STALE, 0 UNRECONCILED, 1 BROKEN\n"
    )


def test_render_statuses_summarizes_a_clean_lattice():
    console, output = _recording_console()
    statuses = [
        EdgeStatus(
            source_id="down",
            target_ref="up",
            target_id=TargetId("up"),
            state="OK",
            expected="hash",
            actual="hash",
        )
    ]

    render_statuses(console, statuses, summarize_statuses(statuses))

    assert output.getvalue() == (
        "OK            down -> up\n1 edge: 1 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN\n"
    )


def test_render_statuses_summarizes_an_empty_lattice():
    console, output = _recording_console()

    render_statuses(console, [], summarize_statuses([]))

    assert output.getvalue() == "0 edges: 0 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN\n"


def test_render_statuses_summary_counts_every_edge_not_only_the_displayed_ones():
    console, output = _recording_console()
    every = [
        EdgeStatus(
            source_id="clean",
            target_ref="up",
            target_id=TargetId("up"),
            state="OK",
            expected="hash",
            actual="hash",
        ),
        EdgeStatus(
            source_id="drifted",
            target_ref="up",
            target_id=TargetId("up"),
            state="STALE",
            expected="old",
            actual="hash",
        ),
    ]
    displayed = [status for status in every if status.state == "STALE"]

    render_statuses(console, displayed, summarize_statuses(every))

    assert output.getvalue() == (
        "STALE         drifted -> up\n2 edges: 1 OK, 1 STALE, 0 UNRECONCILED, 0 BROKEN\n"
    )


def test_render_statuses_keeps_the_verdict_on_one_line_at_any_width():
    # The verdict is the answer a `check | tail -1` reader gets, so Rich's wrapping must not
    # split it into a fragment on a narrow console. Same contract as render_impact's paths.
    console, output = _recording_console(width=20)

    render_statuses(console, [], {"OK": 1234567, "STALE": 2, "UNRECONCILED": 3, "BROKEN": 4})

    assert output.getvalue() == ("1234576 edges: 1234567 OK, 2 STALE, 3 UNRECONCILED, 4 BROKEN\n")


def test_render_statuses_keeps_every_row_on_one_line_at_any_width():
    # Same contract as the verdict test above, but for the per-edge rows: an id or ref that
    # survives a pipe today must stay intact at any terminal width, not just the summary.
    console, output = _recording_console(width=20)
    statuses = [
        EdgeStatus(
            source_id="billing-integration-guide",
            target_ref="api-design#pagination",
            target_id=TargetId("api-design", "pagination"),
            state="STALE",
            expected="old",
            actual="hash",
        )
    ]

    render_statuses(console, statuses, summarize_statuses(statuses))

    assert output.getvalue() == (
        "STALE         billing-integration-guide -> api-design#pagination\n"
        "1 edge: 0 OK, 1 STALE, 0 UNRECONCILED, 0 BROKEN\n"
    )


def test_render_statuses_emits_no_ansi_under_no_color():
    # Rich's default highlighter bolds bare numbers, and bold survives no_color, so an id
    # carrying a digit (adr-001, rfc-2119) used to leak escapes into --no-color output.
    output = StringIO()
    console = Console(file=output, force_terminal=True, color_system="standard", no_color=True)
    statuses = [
        EdgeStatus(
            source_id="rfc-2119",
            target_ref="spec-3#sec",
            target_id=TargetId("spec-3", "sec"),
            state="OK",
            expected="hash",
            actual="hash",
        )
    ]

    render_statuses(console, statuses, summarize_statuses(statuses))

    assert "\x1b[" not in output.getvalue()


def test_state_summary_of_a_sparse_counter_still_names_every_state():
    # The parameter is a Mapping, so a caller may pass a counter that omits absent states.
    assert _state_summary(Counter(["OK", "OK", "STALE"])) == (
        "3 edges: 2 OK, 1 STALE, 0 UNRECONCILED, 0 BROKEN"
    )


def test_state_summary_orders_states_by_literal_declaration():
    assert _state_summary(summarize_statuses([])).endswith(
        ", ".join(f"0 {state}" for state in EDGE_STATES)
    )


def test_render_lint_writes_violations_and_exact_skip_summary():
    console, output = _recording_console()
    result = LintResult(
        violations=(
            LadderViolation(
                source_id="source[/]",
                source_authority="binding",
                target_id=TargetId("target"),
                target_ref="target[bold]",
                target_authority="derived",
            ),
        ),
        skipped=(
            SkippedEdge(
                source_id="source",
                target_ref="bare",
                target_id=TargetId("bare"),
                reason="source-unannotated",
            ),
            SkippedEdge(
                source_id="source",
                target_ref="bare",
                target_id=TargetId("bare"),
                reason="target-unannotated",
            ),
        ),
    )

    render_lint(console, result)

    assert output.getvalue() == (
        "VIOLATION  source[/] (binding) -> target[bold] (derived)\n"
        "1 ladder violation, 2 edges unranked "
        "(1 target unannotated, 1 source unannotated)\n"
    )


def test_render_lint_keeps_every_line_on_one_line_at_any_width():
    # Same contract as render_statuses: a violation's id/ref survives a pipe at any width,
    # and the skip summary (no id/ref of its own) still gets soft_wrap for a consistent
    # per-print record contract across the renderer.
    console, output = _recording_console(width=20)
    result = LintResult(
        violations=(
            LadderViolation(
                source_id="billing-integration-guide",
                source_authority="binding",
                target_id=TargetId("api-design", "pagination"),
                target_ref="api-design#pagination",
                target_authority="derived",
            ),
        ),
        skipped=(),
    )

    render_lint(console, result)

    assert output.getvalue() == (
        "VIOLATION  billing-integration-guide (binding) -> "
        "api-design#pagination (derived)\n"
        "1 ladder violation, 0 edges unranked\n"
    )


def test_render_lint_emits_no_ansi_under_no_color():
    # Same contract as render_statuses: Rich's default highlighter bolds bare numbers and
    # parentheses, and bold survives no_color, so a numbered id (rfc-2119) and the skip
    # summary's counts used to leak escapes into --no-color lint output.
    output = StringIO()
    console = Console(file=output, force_terminal=True, color_system="standard", no_color=True)
    result = LintResult(
        violations=(
            LadderViolation(
                source_id="rfc-2119",
                source_authority="binding",
                target_id=TargetId("spec-3", "sec"),
                target_ref="spec-3#sec",
                target_authority="derived",
            ),
        ),
        skipped=(
            SkippedEdge(
                source_id="source",
                target_ref="bare",
                target_id=TargetId("bare"),
                reason="source-unannotated",
            ),
        ),
    )

    render_lint(console, result)

    assert "\x1b[" not in output.getvalue()


def test_render_impact_writes_exact_plain_text_and_escapes_markup():
    console, output = _recording_console()
    affected = [
        (
            Node(
                id="affected[/]",
                title=None,
                layer=None,
                authority=None,
                path=Path("docs/[plan].md"),
                body="body\n",
                derives_from=(),
                tickets=("GAME-[1]",),
            ),
            2,
        ),
        (
            Node(
                id="unticketed",
                title=None,
                layer=None,
                authority=None,
                path=Path("docs/unticketed.md"),
                body="body\n",
                derives_from=(),
                tickets=(),
            ),
            1,
        ),
    ]

    render_impact(console, affected)

    assert output.getvalue() == (
        "affected[/]  (docs/[plan].md)  tickets: GAME-[1]\n"
        "unticketed  (docs/unticketed.md)  tickets: -\n"
    )


def test_render_impact_keeps_a_path_wider_than_the_console_on_one_line():
    console, output = _recording_console(width=20)
    path = Path("docs/a/deeply/nested/directory/structure/downstream-document.md")
    affected = [
        (
            Node(
                id="downstream",
                title=None,
                layer=None,
                authority=None,
                path=path,
                body="body\n",
                derives_from=(),
                tickets=(),
            ),
            1,
        )
    ]

    render_impact(console, affected)

    assert output.getvalue() == f"downstream  ({path})  tickets: -\n"


def test_render_impact_emits_no_ansi_under_no_color():
    # Same contract as render_statuses and render_lint: a numbered id, a numbered path, and a
    # ticket key all carry digits that Rich's highlighter bolds, and bold survives no_color.
    output = StringIO()
    console = Console(file=output, force_terminal=True, color_system="standard", no_color=True)
    affected = [
        (
            Node(
                id="adr-001",
                title=None,
                layer=None,
                authority=None,
                path=Path("docs/adr-001.md"),
                body="body\n",
                derives_from=(),
                tickets=("GAME-42",),
            ),
            1,
        )
    ]

    render_impact(console, affected)

    assert "\x1b[" not in output.getvalue()


def test_state_colors_cover_every_edge_state():
    assert set(_STATE_COLORS) == set(get_args(EdgeState))
