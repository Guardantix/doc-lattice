"""Shared Typer option annotations for command adapters."""

from pathlib import Path
from typing import Annotated

import typer

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to .doc-lattice.yml.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]
IndentOpt = Annotated[
    int | None,
    typer.Option("--indent", min=0, help="Pretty-print JSON with this indent (requires --json)."),
]
