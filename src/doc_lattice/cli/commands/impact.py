"""Typer adapter for downstream impact walks."""

from typing import Annotated

import typer

from ...constants import VALID_BASIC_OUTPUT_FORMATS
from ...impact import impact as impact_walk
from ...impact import impact_json
from ...report_render import render_impact
from ..errors import exit_on_project_error
from ..options import ConfigOpt, IndentOpt, JsonOpt
from ..output import select_output, write_json
from ..runtime import get_runtime


def register_impact(app: typer.Typer) -> None:
    """Register the ``impact`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def impact(  # noqa: PLR0913
        ctx: typer.Context,
        token: str,
        config: ConfigOpt = None,
        json_out: JsonOpt = False,
        indent: IndentOpt = None,
        depth: Annotated[
            int | None,
            typer.Option(
                "--depth",
                min=1,
                help="Limit the walk to this many hops from the target.",
            ),
        ] = None,
    ) -> None:
        """List every downstream doc affected by a change to TOKEN.

        Informational only: it always exits 0 (2 on a tool error), so it never gates CI.
        """
        runtime = get_runtime(ctx)
        selection = select_output(
            runtime,
            fmt="human",
            json_alias=json_out,
            valid=VALID_BASIC_OUTPUT_FORMATS,
            indent=indent,
        )
        with exit_on_project_error(runtime):
            project = runtime.project(config)
            lattice = runtime.lattice(project)
            affected = impact_walk(lattice, token, max_depth=depth)
        if selection.format == "json":
            write_json(runtime, impact_json(affected), indent=selection.indent)
        else:
            render_impact(runtime.stdout, affected)
