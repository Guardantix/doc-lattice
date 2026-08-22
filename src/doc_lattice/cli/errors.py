"""Shared command-line error rendering and exit policy."""

from collections.abc import Iterator
from contextlib import contextmanager

import typer
from rich.markup import escape

from ..error_types import DocumentError, ProjectError, exception_details
from .github import warn_unattachable_annotations, write_document_annotation
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
    carries no code keeps the plain ``error: <message>``, which covers the usage checks the
    command adapters write themselves and the ``reconcile --recover`` problem report. A usage
    failure the parser rejects first never reaches this module at all: ``typer.rich_utils``
    renders its own ``Usage:`` line and boxed ``Error``. So the parenthetical marks exactly the
    diagnostics that have a code to match on, not every stderr line that exits 2.

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
def exit_on_project_error(runtime: CliRuntime, *, github: bool = False) -> Iterator[None]:
    """Convert project errors into the standard diagnostic and exit code.

    ``github`` is passed in by the caller rather than read back off the runtime, because the
    selected format belongs to one command invocation and the runtime is shared invocation
    state: parking a mutable format field on it would let one command's choice be observed by
    every other consumer of the same object. Only ``check`` and ``lint`` offer the format, and
    both have validated it before they enter this block.

    When it is set and the failure names one document, the annotation is written to stdout
    before anything goes to stderr. That order is load-bearing under AD-40: a stdout that refuses
    the write raises ``PipeClosed`` out of this handler and reaches the entry point's silent 141
    with nothing printed, while a stderr that refuses one is answered in place, so an annotation
    already on stdout survives and the exit code stays 2 either way.

    A failure with no single document behind it, and every non-annotating format, keep the
    stderr diagnostic and exit 2 they had, byte for byte.

    Args:
        runtime: Active invocation state.
        github: Whether this invocation renders findings as GitHub Actions annotations.

    Yields:
        Control to command orchestration.

    Raises:
        typer.Exit: Exit code 2 when orchestration raises a project error.
        PipeClosed: If the reader on stdout departed before the annotation was written.
    """
    try:
        yield
    except ProjectError as exc:
        if github and isinstance(exc, DocumentError):
            write_document_annotation(runtime, exc)
            # The same unattachable report the finding renderers make. A failing document outside
            # the base is annotated by absolute path, which GitHub drops in silence, and this is
            # the run's only annotation: without the warning the gate fails with nothing on the
            # diff and nothing in the log saying why.
            warn_unattachable_annotations(runtime, [exc.source])
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
