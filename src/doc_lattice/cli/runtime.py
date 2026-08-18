"""Immutable per-invocation state for command-line adapters."""

import os
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import typer
from rich.console import Console
from rich.markup import escape

from ..config import ProjectConfig, load_config
from ..model import Lattice
from ..orchestrate import load_lattice


class CliConsole(Console):
    """A ``Console`` whose broken-pipe handling stays local to the failed write.

    Rich's own ``Console.on_broken_pipe`` points ``sys.stdout`` at ``os.devnull`` and
    raises ``SystemExit(1)``. Both halves are wrong for this CLI. The redirect is aimed
    at file descriptor 1 no matter which stream actually failed, so a dead *stderr*
    silently discards the report a succeeding command is still computing; and the
    ``SystemExit`` abandons that command from inside whatever write happened to fail.
    Raising the underlying ``BrokenPipeError`` instead makes a Rich console behave like
    any other stream, so `main()`'s existing ``OSError`` handling governs a failed write
    the way it already governs every other one.
    """

    def on_broken_pipe(self) -> None:
        """Re-raise the broken pipe rather than redirecting the process's stdout.

        Raises:
            BrokenPipeError: Always, in place of Rich's redirect-and-exit default.
        """
        raise BrokenPipeError


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
    """Output streams, cwd, checkout root, and loaders captured for one CLI invocation.

    Attributes:
        stdout: Stream for exact command output.
        stderr: Stream for diagnostics.
        cwd: The invocation working directory.
        load_config: Config loader for this invocation.
        load_lattice: Lattice loader for this invocation.
        workspace: The checkout root captured from ``GITHUB_WORKSPACE`` when the factory found
            an absolute value there, else None. Under GitHub Actions that is the repository
            root, which is why a run invoked from a subdirectory can still emit
            repository-relative annotation paths. It is a candidate base rather than the base:
            ``annotation_root`` yields to the cwd for any document it does not contain.
    """

    stdout: Console
    stderr: Console
    cwd: Path
    load_config: Callable[[Path | None, Path], ProjectConfig]
    load_lattice: LatticeLoader
    workspace: Path | None = None

    def project(self, config: Path | None) -> ProjectConfig:
        """Load a project config relative to this invocation's cwd.

        Args:
            config: Explicit config path, or None for default discovery.

        Returns:
            The loaded project configuration.
        """
        with self.rendered_warnings():
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
        with self.rendered_warnings():
            return self.load_lattice(
                project,
                require_verified=require_verified,
                persist_cache=persist_cache,
            )

    @contextmanager
    def rendered_warnings(self) -> Iterator[None]:
        """Render warnings displayed inside the block through this invocation's stderr.

        Python applies its filters before the replaceable ``showwarning`` stage, so
        substituting only that stage keeps ``PYTHONWARNINGS``, category matching, and
        repeat suppression owned by the engine while the presentation matches AD-9's
        stderr voice. Every phase of a command that can re-enter a parser is wrapped, so
        one invocation never mixes this format with Python's default one.
        ``warnings.showwarning`` is process-global, so the previous callable is restored
        on both the normal and the exception path.

        A write that fails is contained for the whole block rather than at the single
        print: an advisory must never abort a load that is otherwise succeeding, which is
        why CPython's own warning printer swallows ``OSError`` too, and a stream that
        refused one warning will refuse the rest. The guard deliberately does not span the
        ``yield``, because the load itself raises ``OSError`` for real read failures and
        those must keep propagating.

        Yields:
            Control to the wrapped phase.
        """
        unwritable = False

        def show(  # noqa: PLR0913 (signature is `warnings.showwarning`'s, not ours)
            message: Warning | str,
            category: type[Warning],
            filename: str,
            lineno: int,
            file: object = None,
            line: str | None = None,
        ) -> None:
            """Present one displayed warning, or drop it once stderr has refused a write."""
            nonlocal unwritable
            del category, filename, lineno, file, line
            if unwritable:
                return
            try:
                self._render_warning(message)
            except OSError:
                unwritable = True

        previous = warnings.showwarning
        # typeshed declares `showwarning` with `def`, so ty types the attribute as that one
        # function rather than as a callable; substituting the stage is the documented use.
        warnings.showwarning = show  # ty: ignore[invalid-assignment]
        try:
            yield
        finally:
            warnings.showwarning = previous

    def _render_warning(self, message: Warning | str) -> None:
        """Write one displayed warning as a ``warning: <message>`` diagnostic.

        The category, filename, line number, and source line Python's default formatter
        would have shown are discarded by the caller: a skip is reported to a user, not
        to a maintainer of this package. The message is stripped because a dependency can
        raise one that opens with a newline, which would otherwise print the prefix on a
        line of its own; a message that is only whitespace renders as a bare ``warning:``
        rather than a prefix with a trailing space. A message spanning several lines keeps
        the prefix on the first alone, and the rest render unprefixed and unindented.

        ``emoji=False`` and ``highlight=False`` match the renderers in ``report_render.py``
        and ``linear_render.py``: this text carries discovered paths verbatim, and Rich
        would otherwise rewrite a legal ``:name:`` in one as an emoji and recolor the rest.

        Args:
            message: Warning instance or message text Python is displaying.

        Raises:
            OSError: If the stderr stream refuses the write.
        """
        text = str(message).strip()
        body = f" {escape(text)}" if text else ""
        self.stderr.print(
            f"[yellow]warning[/yellow]:{body}", soft_wrap=True, emoji=False, highlight=False
        )

    def annotation_root(self, path: Path) -> Path:
        """Select the base one document's GitHub annotation path is rendered against.

        The selection happens before rendering rather than inside ``output.github_annotation``
        because that renderer falls back to an absolute path for a document outside the root it
        is handed: passing a set-but-non-containing workspace straight through would skip the
        cwd fallback entirely and emit a path GitHub cannot attach to a diff.

        Containment is lexical. ``_github_workspace`` resolves the checkout root, while
        discovery deliberately keeps each document's unresolved spelling as its identity, so a
        checkout reached through a symlink takes the cwd fallback. That is tolerated rather
        than repaired: the fallback is still a correct base for the run.

        Args:
            path: Absolute path of the source document being annotated.

        Returns:
            The checkout root when it is known and contains ``path``, else the invocation cwd.
        """
        if self.workspace is not None and path.is_relative_to(self.workspace):
            return self.workspace
        return self.cwd

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


def _github_workspace() -> Path | None:
    """Read the GitHub Actions checkout root from the environment.

    Only an absolute value is accepted, because resolving a relative one reads the current
    working directory and ``diagnostic_runtime`` must stay usable when that directory is gone.
    A relative value is therefore treated as unset rather than resolved.

    The value is not checked to exist or to be a directory: a wrong-but-absolute checkout root
    simply fails to contain any document and yields to the cwd in ``annotation_root``.

    Returns:
        The resolved ``GITHUB_WORKSPACE`` directory, or None when the variable is unset,
        empty, or relative.
    """
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ""))
    return workspace.resolve() if workspace.is_absolute() else None


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
        stdout=CliConsole(
            file=typer.get_text_stream("stdout"),
            no_color=disabled,
            highlight=highlight,
            color_system=color_system,
        ),
        stderr=CliConsole(
            file=typer.get_text_stream("stderr"),
            stderr=True,
            no_color=disabled,
            highlight=highlight,
            color_system=color_system,
        ),
        cwd=cwd,
        load_config=load_config,
        load_lattice=load_lattice,
        workspace=_github_workspace(),
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
