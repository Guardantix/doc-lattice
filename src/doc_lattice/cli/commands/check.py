"""Typer adapter for edge drift classification."""

from typing import Annotated

import typer
from rich.markup import escape

from ...check import (
    EdgeStatus,
    ambiguity_annotation_message,
    check_lattice,
    has_drift,
    statuses_json,
    summarize_statuses,
)
from ...constants import VALID_EDGE_STATES, VALID_REPORT_FORMATS
from ...report_render import render_statuses
from ..errors import EXIT_FINDING, EXIT_TOOL_ERROR, exit_on_project_error
from ..github import Annotation, write_annotations
from ..options import ConfigOpt, IndentOpt, ReportFormatOpt
from ..output import select_output, write_json
from ..runtime import CliRuntime, get_runtime


def _parse_only_states(runtime: CliRuntime, only: list[str] | None) -> frozenset[str] | None:
    if not only:
        return None
    states = frozenset(value.upper() for value in only)
    unknown = states - VALID_EDGE_STATES
    if unknown:
        valid = ", ".join(sorted(VALID_EDGE_STATES))
        bad = ", ".join(sorted(unknown))
        runtime.stderr.print(
            f"[red]error[/red]: unknown --only state(s): {escape(bad)} (valid: {valid})"
        )
        raise typer.Exit(EXIT_TOOL_ERROR)
    return states


def _filter_statuses(statuses: list[EdgeStatus], only: frozenset[str] | None) -> list[EdgeStatus]:
    if only is None:
        return statuses
    return [status for status in statuses if status.state in only]


def _human_rows(displayed: list[EdgeStatus], only: frozenset[str] | None) -> list[EdgeStatus]:
    """Narrow the human listing to problems when no --only was supplied.

    This is the implicit human default only. It branches on whether the flag was given, not
    on what it selects, so an explicit ``--only OK`` still lists OK rows. It also lives here
    rather than in ``_filter_statuses`` or ``render_statuses`` because both of those are
    shared: narrowing there would strip OK records from default JSON output, and the renderer
    contracts to display exactly the rows its caller hands it.

    Args:
        displayed: Statuses already narrowed by any explicit ``--only``.
        only: The parsed ``--only`` selection, or None when the flag was not supplied.

    Returns:
        The rows the human renderer should list.
    """
    if only is not None:
        return displayed
    return [status for status in displayed if status.state != "OK"]


def register_check(app: typer.Typer) -> None:
    """Register the ``check`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def check(
        ctx: typer.Context,
        config: ConfigOpt = None,
        indent: IndentOpt = None,
        fmt: ReportFormatOpt = "human",
        only: Annotated[
            list[str] | None,
            typer.Option(
                "--only",
                help=(
                    "Show only these states (repeatable): OK, STALE, UNRECONCILED, BROKEN, "
                    "AMBIGUOUS. "
                    "Without it, human output lists problem edges only; pass --only OK to "
                    "list OK edges. Filters display only; the exit code and the summary "
                    "counts always reflect every edge."
                ),
            ),
        ] = None,
    ) -> None:
        """Classify every edge; exit 1 on drift, 2 on tool error."""
        runtime = get_runtime(ctx)
        selection = select_output(
            runtime,
            fmt=fmt,
            valid=VALID_REPORT_FORMATS,
            indent=indent,
        )
        only_states = _parse_only_states(runtime, only)
        with exit_on_project_error(runtime, github=selection.annotates):
            project = runtime.project(config)
            lattice = runtime.lattice(project)
            statuses = check_lattice(lattice)
        # The summary is computed before filtering so --only narrows the records shown
        # without distorting the verdict; the exit code already works that way.
        summary = summarize_statuses(statuses)
        displayed = _filter_statuses(statuses, only_states)
        if selection.format == "json":
            write_json(runtime, statuses_json(displayed, summary), indent=selection.indent)
        elif selection.annotates:
            write_annotations(
                runtime,
                (
                    Annotation(
                        lattice.nodes_by_id[status.source_id].path,
                        f"doc-lattice {status.state}",
                        # An ambiguous finding takes the shared sentence, so this annotation and
                        # lint's stay identical: the collision members are its only actionable
                        # part, and the annotation attaches to the downstream file while the
                        # member lines are in the upstream one, which has to be named.
                        ambiguity_annotation_message(lattice, status)
                        if status.collision
                        else f"{status.source_id} -> {status.target_ref} is {status.state}",
                    )
                    for status in displayed
                    if status.state != "OK"
                ),
            )
        else:
            render_statuses(runtime.stdout, _human_rows(displayed, only_states), summary)
        raise typer.Exit(EXIT_FINDING if has_drift(statuses) else 0)
