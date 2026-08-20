"""Shared command-line error rendering and exit policy."""

from collections.abc import Iterator
from contextlib import contextmanager

import typer
from rich.markup import escape

from ..error_types import EscalatedWarningError, ProjectError, exception_details
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
    output grammars and makes the placement a function of message content. A diagnostic that
    carries no code keeps the plain ``error: <message>``, which covers every usage error and the
    ``reconcile --recover`` problem report its own adapter prints. So the parenthetical marks
    exactly the diagnostics that have a code to match on, not every stderr line that exits 2.

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


def escalated_warning_error(exc: Warning) -> EscalatedWarningError:
    """Restate a warning that a filter escalated to an exception as a coded project error.

    ``PYTHONWARNINGS=error`` and ``-W error`` raise the warning instance itself, and CPython
    does that before the replaceable ``showwarning`` stage, so AD-29's stderr renderer never
    sees one and cannot present it. Without this, the escalated advisory leaves the entry point
    as an unhandled traceback naming this package's own source and exits 1, the code ``check``
    reserves for drift. Restating it here is what puts it back on the ``error (CODE)`` contract
    the rest of the boundary prints.

    The category name leads the message even though ``CliRuntime._render_warning`` deliberately
    discards it for a displayed warning. The two diagnostics answer different questions: a
    displayed advisory is addressed to someone reading about their documents, while this one is
    addressed to someone who configured the filter that stopped the run, and the category is the
    handle that configuration is written against.

    Args:
        exc: The warning instance a filter raised in place of displaying it.

    Returns:
        The coded project error to render and exit on.
    """
    error = EscalatedWarningError(f"{type(exc).__name__}: {exception_details(exc).strip()}")
    error.add_note(
        "a warning filter escalated this advisory to an error, so the run stopped here instead "
        "of continuing past it"
    )
    return error


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
