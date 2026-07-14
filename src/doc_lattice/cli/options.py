"""Shared Typer option annotations for command adapters."""

from pathlib import Path
from typing import Annotated

import typer

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to .doc-lattice.yml.")]
IndentOpt = Annotated[
    int | None,
    typer.Option(
        "--indent",
        min=0,
        help="Pretty-print JSON with this indent (requires --format json).",
    ),
]
ReportFormatOpt = Annotated[str, typer.Option("--format", help="human, json, or github.")]
BasicFormatOpt = Annotated[str, typer.Option("--format", help="human or json.")]
