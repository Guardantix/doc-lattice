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
from .pipe_policy import STDERR_FILENO, PipeClosed, file_descriptor, neutralize


def apply_broken_pipe_policy(console: Console) -> None:
    """Answer one console's failed write by channel rather than by process.

    Rich's own ``Console.on_broken_pipe`` points ``sys.stdout`` at ``os.devnull`` and raises
    ``SystemExit(1)``. Every part of that is wrong here. The redirect is aimed at file
    descriptor 1 no matter which stream actually failed, so a dead *stderr* discards the stdout
    a succeeding command is still computing; the ``SystemExit`` abandons that command from
    inside whatever write happened to fail; and 1 is the code ``check`` and ``lint`` reserve for
    drift, so a departed reader would be indistinguishable from a failed gate.

    What replaces it is one policy with two answers, because the two channels carry different
    things. **stdout** is the command's result: losing it truncates the answer, so there is
    nothing left to finish and the write is escalated into ``PipeClosed`` for the entry point to
    turn into a silent 141. **stderr** carries diagnostics *about* a result that is still being
    computed, so losing it must not change what the run concludes: the console is silenced, the
    segment buffer Rich only clears after a successful write is dropped so a later print cannot
    resurface it, the descriptor is neutralized so the interpreter's shutdown flush cannot
    override the exit code with 120, and this returns normally so the caller's own exit path
    continues undisturbed.

    The channel is read from the console rather than guessed. ``Console.stderr`` is the flag
    Rich itself resolves ``Console.file`` through, and it is what both this module and
    ``typer.rich_utils`` declare when they construct one. The resolved descriptor is consulted
    as well, so a console handed an explicit ``file=sys.stderr`` without the flag is still
    treated as the diagnostic channel.

    Args:
        console: The Rich console whose buffered write raised ``BrokenPipeError``.

    Raises:
        PipeClosed: When the failed console was writing to the command's stdout.
    """
    fd = file_descriptor(console.file)
    if not console.stderr and fd != STDERR_FILENO:
        raise PipeClosed
    console.quiet = True
    # Rich clears a console's segment buffer only after a write succeeds. Left queued, the
    # refused diagnostic would be prepended to the next print this console accepts.
    del console._buffer[:]
    if fd is not None:
        neutralize(fd)


class CliConsole(Console):
    """A ``Console`` whose broken-pipe handling stays local to the failed write.

    The policy itself is ``apply_broken_pipe_policy``, shared with the base-class hook
    ``broken_pipe_policy`` installs for the consoles Typer builds for help and usage rendering.
    This subclass exists so the runtime's own two consoles carry it unconditionally, including
    on the entry-point diagnostic paths that run outside that context manager.
    """

    def on_broken_pipe(self) -> None:
        """Apply this CLI's per-channel broken-pipe policy to this console.

        Raises:
            PipeClosed: When this console was writing to the command's stdout.
        """
        apply_broken_pipe_policy(self)


def _on_broken_pipe(console: Console) -> None:
    """Stand in for ``Console.on_broken_pipe`` while the policy is installed.

    Args:
        console: The console Rich invoked the hook on, bound as ``self``.

    Raises:
        PipeClosed: When the failed console was writing to the command's stdout.
    """
    apply_broken_pipe_policy(console)


@contextmanager
def broken_pipe_policy() -> Iterator[None]:
    """Extend this CLI's broken-pipe policy to every ``rich`` console for one invocation.

    Help text and usage errors are rendered by ``typer.rich_utils``, which builds plain
    ``rich.console.Console`` instances outside ``_create_runtime``. ``CliConsole`` cannot reach
    them, so without this they keep Rich's default hook and ``doc-lattice --help`` piped into a
    reader that departs exits 1 -- the drift code -- on a user-visible path.

    Substituting the documented overridable hook on the base class is what reaches them. The
    alternative is replacing ``typer.rich_utils``' private console factory, which would make the
    declared ``typer`` span a second compatibility surface for the sake of a seam ``rich``
    already offers; AD-27 records why one such surface is enough. The subclass keeps precedence
    over the substitution, so the runtime's own consoles are unaffected by it either way.

    The attribute is process-global, and it is restored on both the normal and the exception
    path for the same reason ``rendered_warnings`` restores ``warnings.showwarning``. It carries
    the same unrepaired concurrency cost, recorded in AD-29: the CLI creates no threads and one
    invocation owns its process, and a caller driving this from several threads is the
    unsupported case.

    Yields:
        Control to the wrapped application call.
    """
    previous = Console.on_broken_pipe
    # Rich types the hook as an ordinary method, so ty reads the substitution as a redefinition
    # rather than as the documented override it is.
    Console.on_broken_pipe = _on_broken_pipe  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        Console.on_broken_pipe = previous


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
            except (OSError, ValueError):
                # ValueError is what a closed (rather than broken) stream raises on write;
                # an embedder can hand `CliRuntime` one, and the advisory must not abort the
                # load either way. Rich clears a console's segment buffer only after a write
                # succeeds, so the refused warning is discarded here: left queued, it would
                # resurface prepended to the next successful print on this console.
                unwritable = True
                del self.stderr._buffer[:]

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

        ``highlight=False`` matches the renderers in ``report_render.py`` and
        ``linear_render.py``; ``emoji=False`` is also the console-wide default set in
        ``_create_runtime`` and is repeated here so an injected plain ``Console`` renders
        identically: this text carries discovered paths verbatim, and Rich would otherwise
        rewrite a legal ``:name:`` in one as an emoji and recolor the rest.

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

        The selection happens before rendering rather than inside ``github.github_annotation``
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

        This is the one output path that does not go through Rich, so no ``on_broken_pipe``
        hook governs it and the ``BrokenPipeError`` a departed reader produces arrives with its
        ``errno`` set to ``EPIPE``. Typer's ``_main`` converts exactly that into ``sys.exit(1)``,
        which would put ``check --format json`` piped into ``head`` on the drift code -- and the
        machine-readable formats are the ones most likely to be piped at all. Re-raising as
        ``PipeClosed`` routes it to the same silent 141 the Rich-rendered paths reach; that
        class documents why an absent ``errno`` is what makes the difference.

        Args:
            text: Text to write without Rich rendering.
            newline: Whether to append one newline after ``text``.

        Raises:
            PipeClosed: If the reader on stdout departed before the write completed.
        """
        try:
            self.stdout.file.write(text)
            if newline:
                self.stdout.file.write("\n")
            self.stdout.file.flush()
        except BrokenPipeError as exc:
            raise PipeClosed from exc

    def write_stderr(self, text: str, *, newline: bool = True) -> None:
        """Write exact text to the captured stderr stream, bypassing Rich.

        The stderr analogue of ``write_stdout``, for a diagnostic line that must reach the
        reader byte for byte: a link finding carries a filename, and a filename shaped like Rich
        markup must not become styling. The broken-pipe answer is the stderr half of AD-40,
        applied here directly because no Rich hook governs a raw stream write: the console is
        silenced and its descriptor neutralized, nothing is raised, and the caller's own exit
        code stands. Only a stdout that refuses a write reaches the silent 141.

        Args:
            text: Text to write without Rich rendering.
            newline: Whether to append one newline after ``text``.
        """
        try:
            self.stderr.file.write(text)
            if newline:
                self.stderr.file.write("\n")
            self.stderr.file.flush()
        except BrokenPipeError:
            apply_broken_pipe_policy(self.stderr)


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
    # `emoji=False` is console-wide rather than per-call: nearly every line this CLI prints
    # can carry a discovered path, and a legal `:name:` in one is not an emoji request. A
    # site that repeats the kwarg does so only to render identically through an injected
    # plain `Console`.
    return CliRuntime(
        stdout=CliConsole(
            file=typer.get_text_stream("stdout"),
            no_color=disabled,
            highlight=highlight,
            color_system=color_system,
            emoji=False,
        ),
        stderr=CliConsole(
            file=typer.get_text_stream("stderr"),
            stderr=True,
            no_color=disabled,
            highlight=highlight,
            color_system=color_system,
            emoji=False,
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
