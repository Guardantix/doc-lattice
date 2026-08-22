"""Shared command-line output selection and exact writers.

The GitHub Actions workflow-command encoder and its annotation writers live in ``cli/github.py``
rather than here, because this module imports the tool-error exit code from ``cli/errors.py``
and the error boundary needs that encoder too. See AD-41.
"""

import json
from dataclasses import dataclass
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
