"""Lazy CLI compatibility export and console-script entry point."""

import os
import sys
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

    Intended ``SystemExit`` values raised by Typer propagate unchanged. Supported
    unexpected process errors use the tool-error exit code 2.

    Raises:
        SystemExit: With exit code 2 for mapped project or internal errors.
    """
    no_color = "--no-color" in sys.argv[1:] or os.environ.get("NO_COLOR", "") != ""
    if no_color:
        os.environ["NO_COLOR"] = "1"
        os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

    from ..error_types import ProjectError  # noqa: PLC0415
    from .errors import (  # noqa: PLC0415
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
        print_project_error(diagnostic_runtime(no_color=no_color), exc)
        raise SystemExit(EXIT_TOOL_ERROR) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        print_internal_error(diagnostic_runtime(no_color=no_color), exc)
        raise SystemExit(EXIT_TOOL_ERROR) from exc
