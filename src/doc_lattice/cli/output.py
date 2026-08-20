"""Shared command-line output selection and exact writers."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import typer
from rich.markup import escape

from ..path_utils import format_path_for_display
from .errors import EXIT_TOOL_ERROR
from .runtime import CliRuntime


@dataclass(frozen=True, slots=True)
class OutputSelection:
    """Validated effective output format and optional JSON indentation."""

    format: str
    indent: int | None


def _reject_bad_format(runtime: CliRuntime, fmt: str, valid: frozenset[str]) -> NoReturn:
    options = ", ".join(sorted(valid))
    runtime.stderr.print(
        f"[red]error[/red]: --format {escape(f'{fmt!r}')} must be one of: {options}"
    )
    raise typer.Exit(EXIT_TOOL_ERROR)


def select_output(
    runtime: CliRuntime,
    *,
    fmt: str,
    valid: frozenset[str],
    indent: int | None = None,
) -> OutputSelection:
    """Validate output flags and return their effective selection.

    Args:
        runtime: Active invocation state.
        fmt: Explicit or implicit format value.
        valid: Formats accepted by the command.
        indent: Requested JSON indentation, including zero.

    Returns:
        The validated effective format and indentation.

    Raises:
        typer.Exit: Exit code 2 for an unsupported format or misused ``--indent``.
    """
    if fmt not in valid:
        _reject_bad_format(runtime, fmt, valid)
    effective = fmt
    if indent is not None and effective != "json":
        runtime.stderr.print("[red]error[/red]: --indent requires --format json")
        raise typer.Exit(EXIT_TOOL_ERROR)
    return OutputSelection(format=effective, indent=indent)


def write_json(runtime: CliRuntime, payload: object, *, indent: int | None = None) -> None:
    """Serialize JSON and write it exactly to the captured stdout stream.

    Args:
        runtime: Active invocation state.
        payload: JSON-serializable value.
        indent: Optional pretty-print indentation.

    Raises:
        PipeClosed: If the reader on stdout departed before the write completed.
    """
    runtime.write_stdout(json.dumps(payload, indent=indent))


def write_text(runtime: CliRuntime, text: str, *, newline: bool = True) -> None:
    """Write exact non-Rich text to the captured stdout stream.

    Args:
        runtime: Active invocation state.
        text: Text to write.
        newline: Whether to append one newline.

    Raises:
        PipeClosed: If the reader on stdout departed before the write completed.
    """
    runtime.write_stdout(text, newline=newline)


def escape_github_message(value: str) -> str:
    """Escape a GitHub workflow-command message value.

    Args:
        value: Untrusted message value.

    Returns:
        The workflow-command escaped value.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_github_property(value: str) -> str:
    """Escape a GitHub workflow-command property value.

    Args:
        value: Untrusted property value.

    Returns:
        The workflow-command escaped value.
    """
    return escape_github_message(value).replace(":", "%3A").replace(",", "%2C")


def github_annotation(path: Path, root: Path, title: str, message: str) -> str:
    """Render one ``::error`` GitHub Actions annotation for a finding.

    The ``file`` property is emitted relative to ``root`` so GitHub Actions can attach
    the annotation to the offending document in the pull request diff. When ``path``
    falls outside ``root``, the absolute path is used instead of raising.

    Args:
        path: Absolute path of the source document.
        root: Base for relative path reporting, chosen by ``CliRuntime.annotation_root``.
        title: Annotation title, before escaping.
        message: Annotation message, before escaping.

    Returns:
        A single escaped workflow-command line.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return (
        f"::error file={escape_github_property(str(relative))},"
        f"title={escape_github_property(title)}::{escape_github_message(message)}"
    )


def warn_unattachable_annotations(runtime: CliRuntime, paths: Iterable[Path]) -> None:
    """Warn once when a run emitted annotations GitHub cannot attach to the pull-request diff.

    ``github_annotation`` falls back to an absolute path for a document its base does not
    contain, and GitHub silently drops such an annotation: the gate fails and the pull request
    shows nothing. Reporting it is what keeps that from being undebuggable. The base named is
    always the invocation cwd, because a document contained by the workspace is annotated
    against the workspace and is attachable by construction.

    The warning goes to stderr and fires at most once per run, so stdout stays exactly the
    workflow commands GitHub parses.

    Args:
        runtime: Active invocation state.
        paths: Absolute source-document paths that were annotated during this run.
    """
    outside = sorted(
        {path for path in paths if not path.is_relative_to(runtime.annotation_root(path))}
    )
    if not outside:
        return
    listed = ", ".join(format_path_for_display(path) for path in outside)
    # emoji=False and highlight=False for the reason the load-warning renderer carries them:
    # this line is mostly discovered paths, and Rich would otherwise rewrite a legal `:name:`
    # in one as an icon and recolor the rest.
    runtime.stderr.print(
        f"[yellow]warning[/yellow]: {len(outside)} annotated document(s) fall outside "
        f"{escape(format_path_for_display(runtime.cwd))}, so their annotations use absolute "
        f"paths and will not attach to the pull-request diff: {escape(listed)}",
        soft_wrap=True,
        emoji=False,
        highlight=False,
    )
