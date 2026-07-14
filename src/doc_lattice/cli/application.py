"""Typer application construction and command registration."""

from typing import Annotated

import typer

from .. import __version__
from .commands.check import register_check
from .commands.graph import register_graph
from .commands.impact import register_impact
from .commands.init import register_init
from .commands.linear import register_linear
from .commands.lint import register_lint
from .commands.reconcile import register_reconcile
from .runtime import RuntimeFactory, default_runtime


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


def create_app(*, runtime_factory: RuntimeFactory = default_runtime) -> typer.Typer:
    """Create a fully registered Typer application.

    Args:
        runtime_factory: Factory called once for every application invocation.

    Returns:
        A new Typer application with all seven commands registered.
    """
    application = typer.Typer(no_args_is_help=True, add_completion=False)

    @application.callback()
    def main_callback(
        ctx: typer.Context,
        version: Annotated[  # noqa: ARG001
            bool,
            typer.Option(
                "--version",
                callback=_version_callback,
                is_eager=True,
                help="Show the version and exit.",
            ),
        ] = False,
        no_color: Annotated[
            bool, typer.Option("--no-color", help="Disable colored output.")
        ] = False,
    ) -> None:
        """doc-lattice: documentation traceability engine."""
        ctx.obj = runtime_factory(no_color=no_color)

    register_check(application)
    register_lint(application)
    register_impact(application)
    register_reconcile(application)
    register_graph(application)
    register_linear(application)
    register_init(application)
    return application


app = create_app()
