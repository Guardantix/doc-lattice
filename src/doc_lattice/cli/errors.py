"""Shared command-line error rendering and exit policy."""

from collections.abc import Iterator
from contextlib import contextmanager

import typer
from rich.markup import escape

from ..error_types import ProjectError, exception_details
from .runtime import CliRuntime

EXIT_FINDING = 1
EXIT_TOOL_ERROR = 2
# 128 + SIGPIPE (13). Kept as a literal, not a signal.SIGPIPE-derived value, so this module
# still imports on platforms without SIGPIPE.
EXIT_PIPE_CLOSED = 141


def print_project_error(runtime: CliRuntime, exc: ProjectError) -> None:
    """Render a project error to the invocation's stderr stream.

    The code sits beside the severity rather than after the message, because the message is not
    reliably one line: ``exception_details`` preserves the line breaks a multi-line diagnostic
    is built from, so a trailing code landed at the end of the last detail line and read as that
    field's second parenthetical instead of as the whole error's code. Leading with it is the
    one placement that does not depend on the message's shape.

    Single-line diagnostics move with it deliberately. The alternative -- attaching the code to
    the first line only when the message has a newline -- keeps their old shape but leaves two
    output grammars and makes the placement a function of message content. Usage errors, which
    carry no code, keep the plain ``error: <message>``, so the parenthetical marks exactly the
    diagnostics that have a code to match on.

    Args:
        runtime: Active invocation state.
        exc: Typed project error to report.
    """
    # emoji=False for the same reason the warning renderer carries it: these details embed
    # discovered paths verbatim, and a legal `:name:` in one is not an emoji request.
    runtime.stderr.print(
        f"[red]error[/red] ({exc.code}): {escape(exception_details(exc))}",
        soft_wrap=True,
        emoji=False,
    )


@contextmanager
def exit_on_project_error(runtime: CliRuntime) -> Iterator[None]:
    """Convert project errors into the standard diagnostic and exit code.

    Args:
        runtime: Active invocation state.

    Yields:
        Control to command orchestration.

    Raises:
        typer.Exit: Exit code 2 when orchestration raises a project error.
    """
    try:
        yield
    except ProjectError as exc:
        print_project_error(runtime, exc)
        raise typer.Exit(EXIT_TOOL_ERROR) from exc


def print_internal_error(runtime: CliRuntime, exc: Exception) -> None:
    """Render an unexpected supported error to stderr.

    Args:
        runtime: Fresh invocation state bound to stderr.
        exc: Unexpected error to report.
    """
    runtime.stderr.print(
        f"[red]internal error[/red]: {type(exc).__name__}: {escape(str(exc))}", emoji=False
    )
