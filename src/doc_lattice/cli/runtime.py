"""Immutable per-invocation state for command-line adapters."""

import os
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

import typer
from rich.console import Console
from rich.markup import escape

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
        with self._rendered_warnings():
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
        with self._rendered_warnings():
            return self.load_lattice(
                project,
                require_verified=require_verified,
                persist_cache=persist_cache,
            )

    @contextmanager
    def _rendered_warnings(self) -> Iterator[None]:
        """Render warnings displayed inside the block through this invocation's stderr.

        Python applies its filters before the replaceable ``showwarning`` stage, so
        substituting only that stage keeps ``PYTHONWARNINGS``, category matching, and
        repeat suppression owned by the engine while the presentation matches AD-9's
        stderr voice. Both loads a command performs are wrapped, so a warning raised
        while reading the config renders the same way as one raised while reading a
        document. ``warnings.showwarning`` is process-global, so the previous callable
        is restored on both the normal and the exception path.

        Yields:
            Control to the wrapped load.
        """
        previous = warnings.showwarning
        # typeshed declares `showwarning` with `def`, so ty types the attribute as that one
        # function rather than as a callable; substituting the stage is the documented use.
        warnings.showwarning = self._show_warning  # ty: ignore[invalid-assignment]
        try:
            yield
        finally:
            warnings.showwarning = previous

    def _show_warning(  # noqa: PLR0913 (signature is `warnings.showwarning`'s, not ours)
        self,
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = None,
        line: str | None = None,
    ) -> None:
        """Write one displayed warning as a ``warning: <message>`` diagnostic.

        Category, filename, line number, and source line are discarded deliberately: a
        skip is reported to a user, not to a maintainer of this package. The message is
        stripped first because a dependency can raise one that opens with a newline,
        which would otherwise print the prefix on a line of its own.

        A dead stderr behaves as it does everywhere else this CLI writes: Rich's
        ``Console.on_broken_pipe`` points ``sys.stdout`` at ``os.devnull`` and raises
        ``SystemExit(1)``. CPython's own ``showwarning`` ends in ``except OSError: pass``
        instead, so a warning on a broken pipe used to be survivable here and now is not.
        The guard is deliberately not reinstated at this one site: by the time it could
        catch, Rich has already redirected the process's stdout, and `cli/errors.py` has
        the identical exposure, so a partial fix here would read as a solved problem.

        Args:
            message: Warning instance or message text Python is displaying.
            category: Warning category, unused by this presentation.
            filename: Raising file, unused by this presentation.
            lineno: Raising line, unused by this presentation.
            file: Stream Python would have written to, unused by this presentation.
            line: Raising source line, unused by this presentation.
        """
        del category, filename, lineno, file, line
        self.stderr.print(
            f"[yellow]warning[/yellow]: {escape(str(message).strip())}", soft_wrap=True
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
