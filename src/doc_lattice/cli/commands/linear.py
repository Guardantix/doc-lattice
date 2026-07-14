"""Typer adapter for Linear ticket drift audits."""

from typing import Annotated

import typer

from ...constants import VALID_BASIC_OUTPUT_FORMATS
from ...linear_fetch import fetch_tickets
from ...linear_render import findings_json, render_findings
from ...stale_shipped import build_audit_trigger, build_from_trigger, stale_shipped
from ..errors import EXIT_FINDING, EXIT_TOOL_ERROR, exit_on_project_error
from ..options import BasicFormatOpt, ConfigOpt, IndentOpt
from ..output import select_output, write_json
from ..runtime import get_runtime


def register_linear(app: typer.Typer) -> None:
    """Register the ``linear`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def linear(  # noqa: PLR0913
        ctx: typer.Context,
        target: Annotated[
            str,
            typer.Argument(help="Narrow the audit to this id and the nodes that derive from it."),
        ] = "",
        from_id: Annotated[
            str | None, typer.Option("--from", help="Forward-looking: impact-walk from this id.")
        ] = None,
        exit_code: Annotated[
            bool, typer.Option("--exit-code", help="Exit 1 on any DANGER or BLOCKED finding.")
        ] = False,
        warn_exit: Annotated[
            bool, typer.Option("--warn-exit", help="With --exit-code, also exit 1 on WARNING.")
        ] = False,
        config: ConfigOpt = None,
        fmt: BasicFormatOpt = "human",
        indent: IndentOpt = None,
    ) -> None:
        """Report tickets shipped against a spec that has since drifted.

        Exits 0 unless --exit-code is passed, which gates CI on any DANGER or BLOCKED
        finding; add --warn-exit to also gate on WARNING. Tool errors always exit 2.
        """
        runtime = get_runtime(ctx)
        selection = select_output(
            runtime,
            fmt=fmt,
            valid=VALID_BASIC_OUTPUT_FORMATS,
            indent=indent,
        )
        if from_id is not None and target:
            runtime.stderr.print("[red]error[/red]: pass a positional target or --from, not both")
            raise typer.Exit(EXIT_TOOL_ERROR)
        with exit_on_project_error(runtime):
            project = runtime.project(config)
            lattice = runtime.lattice(project)
            if from_id is not None:
                trigger = build_from_trigger(lattice, from_id)
            else:
                trigger = build_audit_trigger(lattice, target or None)
            refs = {ref for node_id in trigger for ref in lattice.nodes_by_id[node_id].tickets}
            tickets, rejected = fetch_tickets(refs, project.config.linear_team)
            findings = stale_shipped(lattice, trigger, tickets, rejected)
        if selection.format == "json":
            write_json(runtime, findings_json(findings), indent=selection.indent)
        else:
            render_findings(runtime.stdout, findings)
        if exit_code:
            gate = {"DANGER", "BLOCKED"} | ({"WARNING"} if warn_exit else set())
            if any(finding.severity in gate for finding in findings):
                raise typer.Exit(EXIT_FINDING)
        raise typer.Exit(0)
