"""Typer adapter for the Markdown link gate."""

from pathlib import Path

import typer

from ...config import DEFAULT_CONFIG_NAME, ProjectConfig
from ...constants import VALID_LINK_REPORT_FORMATS
from ...error_types import ConfigError
from ...link_check import LinkFinding, check_links, select_link_sources
from ...path_utils import format_path_for_display
from ..errors import EXIT_FINDING, exit_on_project_error
from ..github import Annotation, write_annotations
from ..options import ConfigOpt, LinkFormatOpt
from ..output import select_output
from ..runtime import CliRuntime, get_runtime

ANNOTATION_TITLE = "doc-lattice links"


def _require_link_sources(project: ProjectConfig) -> list[str]:
    """Return the configured selectors, refusing an empty set as a config error.

    Empty and omitted are one case, and both are refused rather than defaulted: the generated
    hook and workflow run this command unconditionally, and a default would let that mandatory
    gate pass over zero files. The message says which of the two shapes it met, because "your
    config lacks a key" and "you have no config" call for different edits.
    """
    if project.config.link_sources:
        return project.config.link_sources
    if project.config_path is None:
        msg = (
            f"no {DEFAULT_CONFIG_NAME} was found in "
            f"{format_path_for_display(project.project_root)}, and the links command requires "
            "a link_sources list naming the files to check; write one, for example "
            "link_sources: ['docs/**/*.md']"
        )
    else:
        msg = (
            f"config {format_path_for_display(project.config_path)} declares no link_sources, "
            "and the links command requires at least one selector; add link_sources: "
            "['docs/**/*.md'] or whichever files you want checked"
        )
    raise ConfigError(msg)


def format_finding(finding: LinkFinding) -> str:
    """Render one finding as the ``'path'[:line]: message`` line the hook prints.

    Args:
        finding: The finding to render.

    Returns:
        The line, with the path in the display spelling and no trailing newline.
    """
    if finding.line is None:
        return f"{format_path_for_display(finding.path)}: {finding.message}"
    return f"{format_path_for_display(finding.path)}:{finding.line}: {finding.message}"


def _annotations(project_root: Path, findings: list[LinkFinding]) -> list[Annotation]:
    return [
        Annotation(
            project_root / finding.path, ANNOTATION_TITLE, finding.message, line=finding.line
        )
        for finding in findings
    ]


def _write_findings(runtime: CliRuntime, findings: list[LinkFinding]) -> None:
    for finding in findings:
        runtime.write_stderr(format_finding(finding))


def register_links(app: typer.Typer) -> None:
    """Register the ``links`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def links(
        ctx: typer.Context,
        config: ConfigOpt = None,
        fmt: LinkFormatOpt = "human",
    ) -> None:
        """Validate relative links and heading fragments; exit 1 on a finding, 2 on tool error."""
        runtime = get_runtime(ctx)
        selection = select_output(runtime, fmt=fmt, valid=VALID_LINK_REPORT_FORMATS)
        with exit_on_project_error(runtime, github=selection.annotates):
            project = runtime.project(config)
            selectors = _require_link_sources(project)
            sources = select_link_sources(project.project_root, selectors)
            findings = check_links(project.project_root, sources)
        if selection.annotates:
            write_annotations(runtime, _annotations(project.project_root, findings))
        else:
            # Human findings go to stderr, the channel the script always used, and through the
            # exact writer so a filename shaped like Rich markup stays a filename.
            _write_findings(runtime, findings)
        raise typer.Exit(EXIT_FINDING if findings else 0)
