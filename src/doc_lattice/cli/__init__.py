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
    internal errors exit 2, as does a warning an escalating filter raised in place of
    displaying it. A broken pipe exits 141 silently.

    Raises:
        SystemExit: With exit code 2 for mapped project, escalated-warning, or internal
            errors, or 141 for a broken pipe.
    """
    no_color = "--no-color" in sys.argv[1:] or os.environ.get("NO_COLOR", "") != ""
    if no_color:
        os.environ["NO_COLOR"] = "1"
        os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

    # Unguarded on purpose, and the only imports that are. `error_types` reaches `constants`
    # and `typing` and nothing else -- no dependency, no engine module, nothing that warns at
    # import time -- so it is the one chain the fallback below can rest on while reporting a
    # failure of every other chain.
    from ..error_types import (  # noqa: PLC0415
        ProjectError,
        escalated_warning_error,
        exception_details,
    )

    try:
        # Guarded separately from the application load, because these two imports are what the
        # `Warning` clause of that block needs in order to report anything: `errors` reaches
        # `runtime`, and `runtime` reaches `config` and `orchestrate`, so this single statement
        # pulls in most of the engine, ruamel, markdown-it, rich, and typer. An escalating
        # filter turns any import-time deprecation among them into an exception here, before a
        # renderer exists to present it.
        from .errors import (  # noqa: PLC0415
            EXIT_PIPE_CLOSED,
            EXIT_TOOL_ERROR,
            print_internal_error,
            print_project_error,
        )
        from .runtime import diagnostic_runtime  # noqa: PLC0415
    except Warning as exc:
        # The reporter is precisely what failed to import, so this cannot use it. A plain write
        # is the whole fallback: same grammar, no Console, and no further import that could
        # raise the same warning again. `escape` is not applied and does not need to be -- it
        # neutralizes Rich markup that Rich then renders back, so the bytes a terminal receives
        # are the same either way, and nothing here goes through Rich.
        error = escalated_warning_error(exc)
        with suppress(OSError, ValueError):
            sys.stderr.write(f"error ({error.code}): {exception_details(error)}\n")
        # `EXIT_TOOL_ERROR` lives in the module that just failed to import. The literal is
        # pinned equal to it by a test, so the two cannot drift apart unnoticed.
        raise SystemExit(2) from exc

    try:
        # Inside the guarded block, not before it: loading the application reaches the command
        # adapters and everything they import that the boundary above did not, and under an
        # escalating warning filter a deprecation raised at import time is exactly the traceback
        # the `Warning` clause below exists to replace.
        application = _load_app()
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
    except Warning as exc:
        # `PYTHONWARNINGS=error` and `-W error` raise the warning instance rather than
        # displaying it, and CPython does that before consulting `showwarning`, so AD-29's
        # renderer is unreachable and no engine-side change could catch it. Catching the base
        # class is what makes the mapping complete: it covers this engine's own warning family
        # and the ones ruamel raises directly from `config.py` and `reconcile`'s reread, with no
        # shared category to keep in sync and no emission site touched. Ordinary runs never
        # reach here, because an unescalated warning is displayed and never raised.
        with suppress(OSError, ValueError):
            print_project_error(diagnostic_runtime(no_color=no_color), escalated_warning_error(exc))
        raise SystemExit(EXIT_TOOL_ERROR) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        with suppress(OSError, ValueError):
            print_internal_error(diagnostic_runtime(no_color=no_color), exc)
        raise SystemExit(EXIT_TOOL_ERROR) from exc
