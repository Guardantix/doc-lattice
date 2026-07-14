"""Immutable per-invocation state for command-line adapters."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import typer
from rich.console import Console

from ..config import ProjectConfig, load_config
from ..model import Lattice
from ..orchestrate import load_lattice


class LatticeLoader(Protocol):
    """Callable contract for loading one project lattice."""

    def __call__(
        self,
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        """Load a lattice using the requested cache safety policy."""
        ...


class RuntimeFactory(Protocol):
    """Callable contract for creating fresh invocation state."""

    def __call__(self, *, no_color: bool) -> "CliRuntime":
        """Create a runtime for one CLI invocation."""
        ...


@dataclass(frozen=True, slots=True)
class CliRuntime:
    """Output streams, cwd, and loaders captured for one CLI invocation."""

    stdout: Console
    stderr: Console
    cwd: Path
    load_config: Callable[[Path | None, Path], ProjectConfig]
    load_lattice: LatticeLoader

    def project(self, config: Path | None) -> ProjectConfig:
        """Load a project config relative to this invocation's cwd.

        Args:
            config: Explicit config path, or None for default discovery.

        Returns:
            The loaded project configuration.
        """
        return self.load_config(config, self.cwd)

    def lattice(
        self,
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        """Load a project's lattice with explicit cache safety controls.

        Args:
            project: Loaded project configuration.
            require_verified: Whether every document read must use the verify tier.
            persist_cache: Whether the run may update the external load cache.

        Returns:
            The loaded lattice.
        """
        return self.load_lattice(
            project,
            require_verified=require_verified,
            persist_cache=persist_cache,
        )

    def write_stdout(self, text: str, *, newline: bool = True) -> None:
        """Write exact text to the captured stdout stream.

        Args:
            text: Text to write without Rich rendering.
            newline: Whether to append one newline after ``text``.
        """
        self.stdout.file.write(text)
        if newline:
            self.stdout.file.write("\n")
        self.stdout.file.flush()


def _create_runtime(*, cwd: Path, no_color: bool) -> CliRuntime:
    disabled = no_color or os.environ.get("NO_COLOR", "") != ""
    return CliRuntime(
        stdout=Console(file=typer.get_text_stream("stdout"), no_color=disabled),
        stderr=Console(
            file=typer.get_text_stream("stderr"),
            stderr=True,
            no_color=disabled,
        ),
        cwd=cwd,
        load_config=load_config,
        load_lattice=load_lattice,
    )


def default_runtime(*, no_color: bool) -> CliRuntime:
    """Capture process streams, cwd, and default loaders for one invocation.

    Args:
        no_color: Whether the invocation explicitly disabled color.

    Returns:
        A new immutable runtime bound to the current process state.
    """
    return _create_runtime(cwd=Path.cwd(), no_color=no_color)


def diagnostic_runtime(*, no_color: bool) -> CliRuntime:
    """Create cwd-independent state for rendering an entry-point diagnostic.

    Args:
        no_color: Whether the invocation disabled color.

    Returns:
        A new runtime bound to the current streams and an inert relative cwd.
    """
    return _create_runtime(cwd=Path(), no_color=no_color)


def get_runtime(ctx: typer.Context) -> CliRuntime:
    """Return the initialized runtime stored in a Typer context.

    Args:
        ctx: Active command context.

    Returns:
        The invocation runtime.

    Raises:
        RuntimeError: If the application callback did not initialize the context.
    """
    if not isinstance(ctx.obj, CliRuntime):
        msg = "CLI runtime was not initialized"
        raise RuntimeError(msg)
    return ctx.obj
