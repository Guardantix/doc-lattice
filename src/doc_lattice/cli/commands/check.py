"""Typer adapter for edge drift classification."""

from typing import Annotated

import typer
from rich.markup import escape

from ...check import EdgeStatus, check_lattice, has_drift, statuses_json
from ...constants import VALID_EDGE_STATES, VALID_REPORT_FORMATS
from ...report_render import render_statuses
from ..errors import EXIT_FINDING, EXIT_TOOL_ERROR, exit_on_project_error
from ..options import ConfigOpt, IndentOpt, JsonOpt
from ..output import github_annotation, select_output, write_json, write_text
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


def register_check(app: typer.Typer) -> None:
    """Register the ``check`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def check(  # noqa: PLR0913
        ctx: typer.Context,
        config: ConfigOpt = None,
        json_out: JsonOpt = False,
        indent: IndentOpt = None,
        fmt: Annotated[str, typer.Option("--format", help="human, json, or github.")] = "human",
        only: Annotated[
            list[str] | None,
            typer.Option(
                "--only",
                help=(
                    "Show only these states (repeatable): OK, STALE, UNRECONCILED, BROKEN. "
                    "Filters display only; the exit code always reflects every edge."
                ),
            ),
        ] = None,
    ) -> None:
        """Classify every edge; exit 1 on drift, 2 on tool error."""
        runtime = get_runtime(ctx)
        selection = select_output(
            runtime,
            fmt=fmt,
            json_alias=json_out,
            valid=VALID_REPORT_FORMATS,
            indent=indent,
        )
        only_states = _parse_only_states(runtime, only)
        with exit_on_project_error(runtime):
            project = runtime.project(config)
            lattice = runtime.lattice(project)
            statuses = check_lattice(lattice)
        displayed = _filter_statuses(statuses, only_states)
        if selection.format == "json":
            write_json(runtime, statuses_json(displayed), indent=selection.indent)
        elif selection.format == "github":
            for status in displayed:
                if status.state == "OK":
                    continue
                path = lattice.nodes_by_id[status.source_id].path
                write_text(
                    runtime,
                    github_annotation(
                        path,
                        runtime.cwd,
                        f"doc-lattice {status.state}",
                        f"{status.source_id} -> {status.target_ref} is {status.state}",
                    ),
                )
        else:
            render_statuses(runtime.stdout, displayed)
        raise typer.Exit(EXIT_FINDING if has_drift(statuses) else 0)
