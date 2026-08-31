"""Typer adapter for authority-ladder linting."""

import typer

from ...constants import VALID_REPORT_FORMATS
from ...lint import lint_json, lint_lattice
from ...report_render import render_ambiguous, render_lint
from ..errors import EXIT_FINDING, exit_on_project_error
from ..github import write_annotations
from ..options import ConfigOpt, IndentOpt, ReportFormatOpt
from ..output import select_output, write_json
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
        indent: IndentOpt = None,
        fmt: ReportFormatOpt = "human",
    ) -> None:
        """Validate the authority ladder; exit 1 on a violation, 2 on tool error."""
        runtime = get_runtime(ctx)
        selection = select_output(
            runtime,
            fmt=fmt,
            valid=VALID_REPORT_FORMATS,
            indent=indent,
        )
        with exit_on_project_error(runtime, github=selection.annotates):
            project = runtime.project(config)
            lattice = runtime.lattice(project)
            result = lint_lattice(lattice)
        if selection.format == "json":
            write_json(runtime, lint_json(result), indent=selection.indent)
        elif selection.annotates:
            write_annotations(
                runtime,
                (
                    (
                        lattice.nodes_by_id[violation.source_id].path,
                        "doc-lattice ladder violation",
                        f"{violation.source_id} ({violation.source_authority}) -> "
                        f"{violation.target_ref} ({violation.target_authority})",
                    )
                    for violation in result.violations
                ),
            )
        else:
            render_ambiguous(runtime.stdout, result.ambiguous)
            render_lint(runtime.stdout, result)
        raise typer.Exit(EXIT_FINDING if result.violations else 0)
