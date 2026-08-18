"""Lazy CLI compatibility export and console-script entry point."""

import os
import sys
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer

    app: typer.Typer

__all__ = ["app", "main"]


def _load_app() -> "typer.Typer":
    cached = globals().get("app")
    if cached is not None:
        return cached
    from .application import app as application  # noqa: PLC0415

    globals()["app"] = application
    return application


def __getattr__(name: str) -> object:
    """Load the compatibility ``app`` export only when explicitly accessed.

    Args:
        name: Requested module attribute.

    Returns:
        The default Typer application for ``app``.

    Raises:
        AttributeError: If ``name`` is not the lazy compatibility export.
    """
    if name == "app":
        return _load_app()
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def main() -> None:
    """Run the console application with lazy no-color and error setup.

    Intended ``SystemExit`` values raised by Typer propagate unchanged. Mapped project or
    internal errors exit 2. A broken pipe exits 141 silently.

    Raises:
        SystemExit: With exit code 2 for mapped project or internal errors, or 141 for a
            broken pipe.
    """
    no_color = "--no-color" in sys.argv[1:] or os.environ.get("NO_COLOR", "") != ""
    if no_color:
        os.environ["NO_COLOR"] = "1"
        os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

    from ..error_types import ProjectError  # noqa: PLC0415
    from .errors import (  # noqa: PLC0415
        EXIT_PIPE_CLOSED,
        EXIT_TOOL_ERROR,
        print_internal_error,
        print_project_error,
    )
    from .runtime import diagnostic_runtime  # noqa: PLC0415

    application = _load_app()
    try:
        if not callable(application):
            msg = "CLI application is not callable"
            raise RuntimeError(msg)
        application()
    except ProjectError as exc:
        # A report to a stderr that refuses the write cannot be delivered: `CliConsole`
        # raises `BrokenPipeError` for one, and a closed (rather than broken) stream raises
        # `ValueError`. An exception raised inside an `except` clause is never retried
        # against a sibling clause, so without this containment either would escape
        # `main()` as an unhandled traceback instead of the clean tool-error exit.
        with suppress(OSError, ValueError):
            print_project_error(diagnostic_runtime(no_color=no_color), exc)
        raise SystemExit(EXIT_TOOL_ERROR) from exc
    except BrokenPipeError as exc:
        # A departed reader is not a tool error: die the way SIGPIPE would have killed a
        # native tool, silently and with its exit code. The devnull redirect keeps the
        # interpreter's shutdown flush of the dead stream from printing an
        # "Exception ignored" traceback after this handler has already exited cleanly.
        with suppress(OSError, ValueError):
            sys.stdout.flush()
        with suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.dup2(devnull, sys.stderr.fileno())
        raise SystemExit(EXIT_PIPE_CLOSED) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        with suppress(OSError, ValueError):
            print_internal_error(diagnostic_runtime(no_color=no_color), exc)
        raise SystemExit(EXIT_TOOL_ERROR) from exc
