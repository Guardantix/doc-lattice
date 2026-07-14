"""Typer adapter for authority-ladder linting."""

from typing import Annotated

import typer

from ...constants import VALID_REPORT_FORMATS
from ...lint import lint_json, lint_lattice
from ...report_render import render_lint
from ..errors import EXIT_FINDING, exit_on_project_error
from ..options import ConfigOpt, IndentOpt, JsonOpt
from ..output import github_annotation, select_output, write_json, write_text
from ..runtime import get_runtime


def register_lint(app: typer.Typer) -> None:
    """Register the ``lint`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def lint(
        ctx: typer.Context,
        config: ConfigOpt = None,
        json_out: JsonOpt = False,
        indent: IndentOpt = None,
        fmt: Annotated[str, typer.Option("--format", help="human, json, or github.")] = "human",
    ) -> None:
        """Validate the authority ladder; exit 1 on a violation, 2 on tool error."""
        runtime = get_runtime(ctx)
        selection = select_output(
            runtime,
            fmt=fmt,
            json_alias=json_out,
            valid=VALID_REPORT_FORMATS,
            indent=indent,
        )
        with exit_on_project_error(runtime):
            project = runtime.project(config)
            lattice = runtime.lattice(project)
            result = lint_lattice(lattice)
        if selection.format == "json":
            write_json(runtime, lint_json(result), indent=selection.indent)
        elif selection.format == "github":
            for violation in result.violations:
                path = lattice.nodes_by_id[violation.source_id].path
                write_text(
                    runtime,
                    github_annotation(
                        path,
                        runtime.cwd,
                        "doc-lattice ladder violation",
                        f"{violation.source_id} ({violation.source_authority}) -> "
                        f"{violation.target_ref} ({violation.target_authority})",
                    ),
                )
        else:
            render_lint(runtime.stdout, result)
        raise typer.Exit(EXIT_FINDING if result.violations else 0)
