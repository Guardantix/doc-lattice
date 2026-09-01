"""Typer adapter for authority-ladder linting."""

import typer

from ...check import collision_file
from ...constants import VALID_REPORT_FORMATS
from ...lint import lint_json, lint_lattice
from ...model import format_collision
from ...report_render import render_lint
from ..errors import EXIT_FINDING, exit_on_project_error
from ..github import Annotation, write_annotations
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
            # Ambiguity is annotated as well as the ladder violations. The human and JSON forms
            # both report it, and this is the surface a CI reviewer actually sees, so dropping it
            # here would make the one format an adopter gates on the only silent one.
            #
            # It is annotated at warning severity, and the ladder violations at error, because
            # this command's exit code answers only for the ladder: AD-44 gives ambiguity to
            # `check`, which gates on it and annotates it as an error. Emitting both at error
            # here would show a reviewer a red annotation on a run that exits 0, so the severity
            # tracks what this command's own gate acts on rather than how severe the finding is.
            write_annotations(
                runtime,
                (
                    *(
                        Annotation(
                            lattice.nodes_by_id[violation.source_id].path,
                            "doc-lattice ladder violation",
                            f"{violation.source_id} ({violation.source_authority}) -> "
                            f"{violation.target_ref} ({violation.target_authority})",
                        )
                        for violation in result.violations
                    ),
                    *(
                        Annotation(
                            lattice.nodes_by_id[status.source_id].path,
                            "doc-lattice AMBIGUOUS",
                            f"{status.source_id} -> {status.target_ref} is AMBIGUOUS in "
                            f"{collision_file(lattice, status)} "
                            f"({format_collision(status.collision)})",
                            "warning",
                        )
                        for status in result.ambiguous
                    ),
                ),
            )
        else:
            render_lint(runtime.stdout, result)
        raise typer.Exit(EXIT_FINDING if result.violations else 0)
