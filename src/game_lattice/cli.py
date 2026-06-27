"""Command-line interface."""

from typing import Annotated

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _version_callback(value: bool) -> None:
    if value:
        Console().print(__version__)
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Project CLI."""


@app.command()
def hello(name: Annotated[str, typer.Option(help="Name to greet.")] = "world") -> None:
    """Say hello."""
    Console().print(f"Hello, {name}!")


def main() -> None:
    """Console-script entry point."""
    app()
