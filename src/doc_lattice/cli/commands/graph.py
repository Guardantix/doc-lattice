"""Typer adapter for graph serialization."""

from typing import Annotated

import typer

from ...check import check_lattice
from ...constants import VALID_GRAPH_FORMATS
from ...render import to_dot, to_json, to_mermaid
from ..errors import exit_on_project_error
from ..options import ConfigOpt
from ..output import select_output, write_json, write_text
from ..runtime import get_runtime


def register_graph(app: typer.Typer) -> None:
    """Register the ``graph`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def graph(
        ctx: typer.Context,
        fmt: Annotated[str, typer.Option("--format", help="mermaid, dot, or json.")] = "mermaid",
        config: ConfigOpt = None,
    ) -> None:
        """Emit the edge graph as Mermaid, DOT, or JSON."""
        runtime = get_runtime(ctx)
        selection = select_output(
            runtime,
            fmt=fmt,
            valid=VALID_GRAPH_FORMATS,
        )
        with exit_on_project_error(runtime):
            project = runtime.project(config)
            lattice = runtime.lattice(project)
            stale = {
                (status.source_id, status.target_id)
                for status in check_lattice(lattice)
                if status.state == "STALE" and status.target_id is not None
            }
        if selection.format == "json":
            write_json(runtime, to_json(lattice, stale))
        elif selection.format == "dot":
            write_text(runtime, to_dot(lattice, stale), newline=False)
        else:
            write_text(runtime, to_mermaid(lattice, stale), newline=False)
