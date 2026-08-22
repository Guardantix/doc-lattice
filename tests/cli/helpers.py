"""Shared fixtures and helpers for CLI integration tests."""

import os
from io import StringIO
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from doc_lattice.cli import app
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.config import ProjectConfig
from doc_lattice.model import Lattice

runner = CliRunner()


def _contents(console: Console) -> str:
    """Return everything written to a console backed by a StringIO."""
    stream = console.file
    assert isinstance(stream, StringIO)
    return stream.getvalue()


def _stub_runtime(tmp_path: Path, subject: str) -> CliRuntime:
    """Return a runtime whose loaders refuse, for a unit that must not reach a project.

    Both consoles are captured, so the caller reads them back with `_contents`. `subject` names
    the unit under test inside the refusal, so a module that does reach for a config or a
    lattice says which one did it rather than only that something did.

    Args:
        tmp_path: Directory the runtime treats as the invocation cwd.
        subject: The unit under test, as it should read in a refusal.

    Returns:
        A runtime bound to fresh in-memory stdout and stderr consoles.
    """

    def unexpected_config(_config: Path | None, _cwd: Path) -> ProjectConfig:
        raise AssertionError(f"{subject} must not load config")

    def unexpected_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project
        raise AssertionError(
            f"{subject} must not load lattice {require_verified=} {persist_cache=}"
        )

    return CliRuntime(
        stdout=Console(file=StringIO(), no_color=True),
        stderr=Console(file=StringIO(), stderr=True, no_color=True),
        cwd=tmp_path,
        load_config=unexpected_config,
        load_lattice=unexpected_lattice,
    )


def _run(args: list[str], cwd: Path, env: dict[str, str]):
    """Invoke the CLI with cwd and env set for the duration of the call, then restore cwd."""
    old = Path.cwd()
    os.chdir(cwd)
    try:
        return runner.invoke(app, args, env=env)
    finally:
        os.chdir(old)


def _clean_docs(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nsec body\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#sec\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
