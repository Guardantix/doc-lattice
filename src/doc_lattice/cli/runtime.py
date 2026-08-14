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
    # `--no-color` and `NO_COLOR` mean "no styling", not merely "no color": deliberately
    # broader than the NO_COLOR standard (https://no-color.org/), which leaves bold,
    # underline, and italic in place. `no_color=True` alone only suppresses color and
    # still lets Rich's automatic highlighter and explicit markup (e.g. `[bold]`, an OSC 8
    # `[link=...]`) render those other escapes, so the disabled branch also turns off the
    # console-wide highlighter and forces `color_system=None`, which makes rendering
    # escape-free even for explicit style requests. The enabled branch is unchanged:
    # `highlight=True` and `color_system="auto"` are Rich's own defaults.
    disabled = no_color or os.environ.get("NO_COLOR", "") != ""
    highlight = not disabled
    color_system = None if disabled else "auto"
    return CliRuntime(
        stdout=Console(
            file=typer.get_text_stream("stdout"),
            no_color=disabled,
            highlight=highlight,
            color_system=color_system,
        ),
        stderr=Console(
            file=typer.get_text_stream("stderr"),
            stderr=True,
            no_color=disabled,
            highlight=highlight,
            color_system=color_system,
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
    """Create a runtime without calling ``Path.cwd()`` for entry-point diagnostics.

    This factory remains safe when the current working directory is inaccessible because
    it does not resolve the relative path.

    Args:
        no_color: Whether the invocation disabled color.

    Returns:
        A new runtime bound to the current streams and a relative ``.`` cwd.
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
