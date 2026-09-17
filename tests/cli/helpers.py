"""Shared fixtures and helpers for CLI integration tests."""

import os
from dataclasses import replace
from io import StringIO
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from doc_lattice.cli import app
from doc_lattice.cli.application import create_app
from doc_lattice.cli.runtime import CliRuntime, default_runtime
from doc_lattice.config import Config, ProjectConfig
from doc_lattice.loader import build_lattice
from doc_lattice.model import (
    DocumentOrigin,
    ExternalDeclaration,
    Lattice,
    NodeMeta,
    ParsedDoc,
    RawEdge,
)

runner = CliRunner()


class _RefusingStream(StringIO):
    """A stream that refuses the first ``refusals`` writes, recording how many were attempted."""

    def __init__(self, error: OSError | ValueError, refusals: int | None = None):
        super().__init__()
        self.error = error
        self.refusals = refusals
        self.attempts = 0

    def write(self, s: str) -> int:
        self.attempts += 1
        if self.refusals is None or self.attempts <= self.refusals:
            raise self.error
        return super().write(s)


def _contents(console: Console) -> str:
    """Return everything written to a console backed by a StringIO."""
    stream = console.file
    assert isinstance(stream, StringIO)
    return stream.getvalue()


def _control_characters(stream: bytes) -> list[str]:
    """Every C0, DEL, or C1 character in captured output, newline excepted.

    Decoded before the scan, deliberately: a C1 control reaches a terminal as the two-byte UTF-8
    encoding of its code point, and a byte-level scan of ``0x80`` to ``0x9F`` would also flag the
    continuation byte of ordinary non-ASCII text. The raw-byte assertion for ESC is kept beside
    this rather than folded into it, since ``0x1b`` is never a continuation byte and is the exact
    byte a terminal acts on.

    A newline is how output is written at all, so it is the one member of the range a stream
    legitimately carries.
    """
    text = stream.decode("utf-8", errors="surrogateescape")
    return sorted({char for char in text if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F} - {"\n"})


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


def _external_reporting_app(root, *, authority=None, ambiguous=False):

    project = ProjectConfig(Config(), root, (root,))
    lattice = build_lattice(
        [
            ParsedDoc(
                root / "up.md",
                NodeMeta(id="up", authority="derived"),
                "# Notes\n\n# Notes\n" if ambiguous else "# Notes\nbody\n",
            ),
            ParsedDoc(
                root / "down.md",
                NodeMeta(
                    id="down",
                    authority=authority,
                    derives_from=[RawEdge(ref="up#notes"), RawEdge(ref="missing")],
                ),
                "body\n",
                origin=DocumentOrigin(
                    root / "down.md", ExternalDeclaration("meta/[red]index.yml", 6, "./down.md")
                ),
            ),
        ]
    )

    def factory(*, no_color):
        return replace(
            default_runtime(no_color=no_color),
            load_config=lambda _config, _cwd: project,
            load_lattice=lambda _project, **_kwargs: lattice,
        )

    return create_app(runtime_factory=factory)
