"""Shared command-line output selection and exact writers."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import typer
from rich.markup import escape

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
    json_alias: bool,
    valid: frozenset[str],
    indent: int | None = None,
) -> OutputSelection:
    """Validate output flags and return their effective selection.

    Explicit format validation precedes resolution of the legacy JSON alias so an
    invalid format can never be hidden by ``--json``.

    Args:
        runtime: Active invocation state.
        fmt: Explicit or implicit format value.
        json_alias: Whether the legacy ``--json`` option was passed.
        valid: Formats accepted by the command.
        indent: Requested JSON indentation, including zero.

    Returns:
        The validated effective format and indentation.

    Raises:
        typer.Exit: Exit code 2 for unsupported or conflicting output flags.
    """
    if fmt not in valid:
        _reject_bad_format(runtime, fmt, valid)
    if json_alias and fmt == "github":
        runtime.stderr.print("[red]error[/red]: --json cannot be combined with --format github")
        raise typer.Exit(EXIT_TOOL_ERROR)
    effective = "json" if json_alias else fmt
    if indent is not None and effective != "json":
        runtime.stderr.print("[red]error[/red]: --indent requires --json")
        raise typer.Exit(EXIT_TOOL_ERROR)
    return OutputSelection(format=effective, indent=indent)


def write_json(runtime: CliRuntime, payload: object, *, indent: int | None = None) -> None:
    """Serialize JSON and write it exactly to the captured stdout stream.

    Args:
        runtime: Active invocation state.
        payload: JSON-serializable value.
        indent: Optional pretty-print indentation.
    """
    runtime.write_stdout(json.dumps(payload, indent=indent))


def write_text(runtime: CliRuntime, text: str, *, newline: bool = True) -> None:
    """Write exact non-Rich text to the captured stdout stream.

    Args:
        runtime: Active invocation state.
        text: Text to write.
        newline: Whether to append one newline.
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
        root: Invocation cwd used for relative path reporting.
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
