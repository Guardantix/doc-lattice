"""Tests for check, lint, and impact report rendering."""

from io import StringIO
from pathlib import Path
from typing import get_args

from rich.console import Console

from doc_lattice.check import EdgeStatus
from doc_lattice.constants import EdgeState
from doc_lattice.lint import LadderViolation, LintResult, SkippedEdge
from doc_lattice.model import Node, TargetId
from doc_lattice.report_render import (
    _STATE_COLORS,
    render_impact,
    render_lint,
    render_statuses,
)


def _recording_console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, record=True, width=200, color_system=None), output


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

    render_statuses(console, statuses)

    assert output.getvalue() == "BROKEN        down[/] -> up[bold]\n"


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


def test_state_colors_cover_every_edge_state():
    assert set(_STATE_COLORS) == set(get_args(EdgeState))
