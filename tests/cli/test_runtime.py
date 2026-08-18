"""Tests for per-invocation CLI runtime state."""

import os
import sys
import warnings
from dataclasses import replace
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

import doc_lattice.cli.runtime as runtime_module
from doc_lattice.cli.application import create_app
from doc_lattice.cli.runtime import (
    CliConsole,
    CliRuntime,
    LatticeLoader,
    default_runtime,
    diagnostic_runtime,
    get_runtime,
)
from doc_lattice.config import Config, ProjectConfig
from doc_lattice.model import Lattice


def _runtime(stdout: StringIO, stderr: StringIO, cwd: Path, *, no_color: bool) -> CliRuntime:
    def load_config(_config: Path | None, seen_cwd: Path) -> ProjectConfig:
        raise AssertionError(f"unexpected load from {seen_cwd}")

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project
        raise AssertionError(f"unexpected lattice load {require_verified=} {persist_cache=}")

    return CliRuntime(
        stdout=Console(file=stdout, no_color=no_color),
        stderr=Console(file=stderr, stderr=True, no_color=no_color),
        cwd=cwd,
        load_config=load_config,
        load_lattice=load_lattice,
    )


def test_runtime_factory_creates_isolated_invocation_state(tmp_path: Path):
    created: list[CliRuntime] = []

    def factory(*, no_color: bool) -> CliRuntime:
        runtime = _runtime(StringIO(), StringIO(), tmp_path, no_color=no_color)
        created.append(runtime)
        return runtime

    app = create_app(runtime_factory=factory)

    @app.command("runtime-probe")
    def runtime_probe(ctx: typer.Context) -> None:
        runtime = get_runtime(ctx)
        runtime.write_stdout(str(runtime.cwd))

    runner = CliRunner()
    colored = runner.invoke(app, ["runtime-probe"])
    plain = runner.invoke(app, ["--no-color", "runtime-probe"])

    assert colored.exit_code == plain.exit_code == 0
    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].stdout.no_color is False
    assert created[1].stdout.no_color is True


def test_default_runtime_writes_unicode_to_strict_ascii_stdout(monkeypatch):
    buffer = BytesIO()
    stream = TextIOWrapper(buffer, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    runtime = default_runtime(no_color=True)
    runtime.write_stdout("café")

    assert buffer.getvalue() == b"caf\xc3\xa9\n"


def test_default_runtime_captures_an_absolute_workspace(tmp_path: Path, monkeypatch):
    # The captured value is normalized, not merely stored: a raw GITHUB_WORKSPACE can carry
    # redundant segments, and annotation containment compares it lexically against document
    # paths that are already resolved. Asserting against an unnormalized spelling is what makes
    # this test able to fail if the resolve() is dropped.
    unnormalized = tmp_path / "sub" / ".." / "sub"
    (tmp_path / "sub").mkdir()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(unnormalized))

    captured = default_runtime(no_color=True).workspace

    assert captured == tmp_path / "sub"
    assert captured != Path(str(unnormalized))


def test_default_runtime_has_no_workspace_when_the_variable_is_unset_or_empty(monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    assert default_runtime(no_color=True).workspace is None

    monkeypatch.setenv("GITHUB_WORKSPACE", "")
    assert default_runtime(no_color=True).workspace is None


def test_diagnostic_runtime_ignores_a_relative_workspace_without_reading_the_cwd(monkeypatch):
    # `diagnostic_runtime` exists to stay usable when the cwd is gone, so resolving a relative
    # GITHUB_WORKSPACE there would trade a clean exit-2 diagnostic for a FileNotFoundError
    # traceback. A relative value is therefore treated as unset rather than resolved.
    monkeypatch.setenv("GITHUB_WORKSPACE", "../checkout")

    def _no_cwd() -> str:
        msg = "the current working directory is gone"
        raise FileNotFoundError(msg)

    # Resolving a relative path is what reaches os.getcwd(); an absolute one never does.
    monkeypatch.setattr(os, "getcwd", _no_cwd)

    assert diagnostic_runtime(no_color=True).workspace is None


def test_diagnostic_runtime_still_captures_an_absolute_workspace_with_no_cwd(
    tmp_path: Path, monkeypatch
):
    # The other half of the same guard, and the load-bearing one inside Actions: an absolute
    # value must still be captured when the cwd is gone, or a run whose working directory was
    # deleted would lose its annotation base as well as its cwd.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))

    def _no_cwd() -> str:
        msg = "the current working directory is gone"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(os, "getcwd", _no_cwd)

    assert diagnostic_runtime(no_color=True).workspace == tmp_path


def test_get_runtime_reads_context_object(tmp_path: Path):
    runtime = _runtime(StringIO(), StringIO(), tmp_path, no_color=True)
    ctx = typer.Context(typer.main.get_command(create_app()), obj=runtime)

    assert get_runtime(ctx) is runtime


def test_project_forwards_captured_cwd_to_config_loader(tmp_path: Path):
    config_path = tmp_path / "custom.yml"
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    calls: list[tuple[Path | None, Path]] = []

    def load_config(config: Path | None, cwd: Path) -> ProjectConfig:
        calls.append((config, cwd))
        return project

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        return Lattice({}, {}, {}, {}, {}, {})

    runtime = CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=Console(file=StringIO(), stderr=True),
        cwd=tmp_path,
        load_config=load_config,
        load_lattice=load_lattice,
    )

    assert runtime.project(config_path) is project
    assert calls == [(config_path, tmp_path)]


def test_lattice_forwards_loader_keywords(tmp_path: Path):
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})
    calls: list[tuple[ProjectConfig, bool, bool]] = []

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        calls.append((project, require_verified, persist_cache))
        return lattice

    runtime = CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=Console(file=StringIO(), stderr=True),
        cwd=tmp_path,
        load_config=lambda _config, _cwd: project,
        load_lattice=load_lattice,
    )

    assert runtime.lattice(project, require_verified=True, persist_cache=False) is lattice
    assert calls == [(project, True, False)]


def test_no_color_suppresses_forced_ansi(lattice_dir: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(lattice_dir)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    created: list[CliRuntime] = []

    def factory(*, no_color: bool) -> CliRuntime:
        runtime = CliRuntime(
            stdout=Console(
                force_terminal=True,
                color_system="standard",
                no_color=no_color,
            ),
            stderr=Console(
                stderr=True,
                force_terminal=True,
                color_system="standard",
                no_color=no_color,
            ),
            cwd=lattice_dir,
            load_config=runtime_module.load_config,
            load_lattice=runtime_module.load_lattice,
        )
        created.append(runtime)
        return runtime

    isolated_app = create_app(runtime_factory=factory)
    colored = runner.invoke(isolated_app, ["check"])
    plain = runner.invoke(isolated_app, ["--no-color", "check"])

    assert colored.exit_code == plain.exit_code == 1
    assert "\x1b[" in colored.stdout
    assert "\x1b[" not in plain.stdout
    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].stdout.no_color is False
    assert created[1].stdout.no_color is True


def _warning_runtime(stderr: StringIO, tmp_path: Path, load_lattice: LatticeLoader) -> CliRuntime:
    """Build a runtime whose loader is the injected callable under test."""
    return CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=Console(file=stderr, stderr=True, no_color=True, color_system=None),
        cwd=tmp_path,
        load_config=lambda _config, _cwd: ProjectConfig(Config(), tmp_path, (tmp_path,)),
        load_lattice=load_lattice,
    )


def _warning_loader(lattice: Lattice, *messages: str) -> LatticeLoader:
    """Build a loader that raises each message as a warning in order, then succeeds."""

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        for message in messages:
            warnings.warn(message, stacklevel=1)
        return lattice

    return load_lattice


def _unreachable_loader() -> LatticeLoader:
    """Build a loader that fails the test if the config read ever reaches it."""

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        raise AssertionError("the config read must not reach the lattice loader")

    return load_lattice


def test_lattice_renders_loader_warnings_through_the_invocation_stderr(tmp_path: Path):
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})

    stderr = StringIO()
    runtime = _warning_runtime(
        stderr, tmp_path, _warning_loader(lattice, "skipping /docs/thing.md: no 'id'")
    )

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        assert runtime.lattice(project) is lattice

    assert stderr.getvalue() == "warning: skipping /docs/thing.md: no 'id'\n"


def test_project_renders_config_load_warnings_through_the_invocation_stderr(tmp_path: Path):
    # A reused YAML anchor warns from the shared loader on the config read too, so leaving
    # that read unwrapped would print one command's two warnings in two different formats.
    def load_config(config: Path | None, cwd: Path) -> ProjectConfig:
        del config, cwd
        warnings.warn("found duplicate anchor 'names'", stacklevel=1)
        return ProjectConfig(Config(), tmp_path, (tmp_path,))

    stderr = StringIO()
    runtime = CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=Console(file=stderr, stderr=True, no_color=True, color_system=None),
        cwd=tmp_path,
        load_config=load_config,
        load_lattice=_unreachable_loader(),
    )

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        runtime.project(None)

    assert stderr.getvalue() == "warning: found duplicate anchor 'names'\n"


def test_lattice_warning_renderer_strips_a_message_that_opens_with_a_newline(tmp_path: Path):
    # ruamel raises its anchor and duplicate-key warnings with a leading newline, which
    # would otherwise print the `warning:` prefix on a line carrying nothing else.
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})

    stderr = StringIO()
    runtime = _warning_runtime(
        stderr,
        tmp_path,
        _warning_loader(lattice, "\nfound duplicate anchor 'shared'\nfirst occurrence\n"),
    )

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        runtime.lattice(project)

    assert stderr.getvalue() == "warning: found duplicate anchor 'shared'\nfirst occurrence\n"


def test_lattice_warning_renderer_escapes_rich_markup_in_the_message(tmp_path: Path):
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})

    stderr = StringIO()
    runtime = _warning_runtime(
        stderr, tmp_path, _warning_loader(lattice, "skipping [bold]docs/a.md[/bold]")
    )

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        runtime.lattice(project)

    assert stderr.getvalue() == "warning: skipping [bold]docs/a.md[/bold]\n"


def test_lattice_restores_the_previous_showwarning_after_a_normal_return(tmp_path: Path):
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})
    seen: list[object] = []

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        seen.append(warnings.showwarning)
        return lattice

    runtime = _warning_runtime(StringIO(), tmp_path, load_lattice)

    with warnings.catch_warnings():
        sentinel = warnings.showwarning
        runtime.lattice(project)
        assert warnings.showwarning is sentinel

    assert len(seen) == 1
    assert seen[0] is not sentinel


def test_lattice_restores_the_previous_showwarning_after_a_loader_exception(tmp_path: Path):
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    seen: list[object] = []

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        seen.append(warnings.showwarning)
        msg = "loader blew up"
        raise RuntimeError(msg)

    runtime = _warning_runtime(StringIO(), tmp_path, load_lattice)

    with warnings.catch_warnings():
        sentinel = warnings.showwarning
        with pytest.raises(RuntimeError, match="loader blew up"):
            runtime.lattice(project)
        assert warnings.showwarning is sentinel

    assert len(seen) == 1
    assert seen[0] is not sentinel


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


def _refusing_runtime(stream: _RefusingStream, tmp_path: Path, load_lattice: LatticeLoader):
    """Build a runtime whose stderr is a `CliConsole` over a stream that refuses writes."""
    return CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=CliConsole(file=stream, stderr=True, no_color=True, color_system=None),
        cwd=tmp_path,
        load_config=lambda _config, _cwd: ProjectConfig(Config(), tmp_path, (tmp_path,)),
        load_lattice=load_lattice,
    )


def _twice_warning_loader(lattice: Lattice) -> LatticeLoader:
    """Build a loader that raises two distinct warnings and then succeeds."""
    return _warning_loader(
        lattice, "skipping docs/one.md: no 'id'", "skipping docs/two.md: no 'id'"
    )


@pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError(),
        OSError(5, "Input/output error"),
        OSError(28, "No space left on device"),
        ValueError("I/O operation on closed file"),
    ],
    ids=["broken-pipe", "eio", "enospc", "closed-file"],
)
def test_a_stderr_that_refuses_the_warning_does_not_abort_the_load(
    tmp_path: Path, error: OSError | ValueError
):
    # CPython's own warning printer swallows OSError for this reason: the load is
    # succeeding, and its report is the whole point of the invocation. A closed (rather
    # than broken) stream raises ValueError from the write, and must not abort it either.
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})
    stream = _RefusingStream(error)
    runtime = _refusing_runtime(stream, tmp_path, _twice_warning_loader(lattice))

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        assert runtime.lattice(project) is lattice

    assert stream.attempts == 1  # the second warning is dropped, not retried on a dead stream


def test_a_refused_warning_does_not_resurface_in_a_later_print(tmp_path: Path):
    # Rich clears a console's segment buffer only after a successful write, so the refused
    # warning's segments would otherwise stay queued and flush, prepended, with the next
    # successful print on the same console.
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})
    stream = _RefusingStream(OSError(28, "No space left on device"), refusals=1)
    runtime = _refusing_runtime(
        stream, tmp_path, _warning_loader(lattice, "skipping docs/one.md: no 'id'")
    )

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        runtime.lattice(project)
    runtime.stderr.print("error: something else entirely", soft_wrap=True)

    assert stream.getvalue() == "error: something else entirely\n"


def test_a_refusing_stderr_does_not_redirect_the_process_stdout(tmp_path: Path, capsys):
    # rich's own Console.on_broken_pipe points sys.stdout at os.devnull before it raises, so
    # the report would be discarded even by a caller that caught the SystemExit. CliConsole
    # keeps the failure local to the write that failed.
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})
    stream = _RefusingStream(BrokenPipeError())
    runtime = _refusing_runtime(stream, tmp_path, _twice_warning_loader(lattice))

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        runtime.lattice(project)

    print("the report")
    assert capsys.readouterr().out == "the report\n"


def test_a_load_error_still_propagates_through_the_warning_guard(tmp_path: Path):
    # The guard must not span the load: a real read failure is an OSError too.
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        msg = "docs/a.md is unreadable"
        raise OSError(msg)

    runtime = _warning_runtime(StringIO(), tmp_path, load_lattice)

    with pytest.raises(OSError, match=r"docs/a\.md is unreadable"):
        runtime.lattice(project)


def _render_through(
    stderr: StringIO, tmp_path: Path, message: str, *, no_color: bool = True, **console_kwargs
) -> None:
    """Raise one warning inside a wrapped load and let the runtime render it."""
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})

    runtime = CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=Console(file=stderr, stderr=True, no_color=no_color, **console_kwargs),
        cwd=tmp_path,
        load_config=lambda _config, _cwd: ProjectConfig(Config(), tmp_path, (tmp_path,)),
        load_lattice=_warning_loader(lattice, message),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        runtime.lattice(project)


def test_warning_renderer_keeps_a_colon_delimited_word_in_a_path_literal(tmp_path: Path):
    # `rich.markup.escape` only escapes brackets, so without emoji=False Rich rewrites the
    # `:x:` in this legal POSIX filename as an emoji. It does that regardless of color.
    stderr = StringIO()
    _render_through(stderr, tmp_path, "skipping docs/a:x:b.md: no 'id'", color_system=None)

    assert stderr.getvalue() == "warning: skipping docs/a:x:b.md: no 'id'\n"


def test_warning_renderer_does_not_wrap_a_long_message_on_a_narrow_console(tmp_path: Path):
    # soft_wrap=True is load-bearing: a path split across lines is not greppable.
    stderr = StringIO()
    message = "skipping docs/a/very/long/path/to/some/document/named/at/length.md: no 'id'"
    _render_through(stderr, tmp_path, message, color_system=None, width=40)

    assert stderr.getvalue() == f"warning: {message}\n"


def test_warning_renderer_emits_no_trailing_space_for_an_empty_message(tmp_path: Path):
    stderr = StringIO()
    _render_through(stderr, tmp_path, "   ", color_system=None)

    assert stderr.getvalue() == "warning:\n"


def test_warning_renderer_leaves_no_ansi_in_an_escaped_markup_message(tmp_path: Path):
    # The escaping test above runs without color, where it can only catch tag stripping.
    # On a styled console the risk is the opposite one: injected ANSI.
    stderr = StringIO()
    _render_through(
        stderr,
        tmp_path,
        "skipping [bold]docs/a.md[/bold]: no 'id'",
        no_color=False,
        color_system="standard",
        force_terminal=True,
    )

    rendered = stderr.getvalue()
    assert "\x1b[1m" not in rendered  # the message never becomes markup
    assert "\x1b[33m" in rendered  # but the prefix is still styled
    assert "[bold]docs/a.md[/bold]" in rendered


def test_warning_renderer_does_not_highlight_a_path_inside_the_message(tmp_path: Path):
    # highlight=False matches report_render.py and linear_render.py: Rich's ReprHighlighter
    # would otherwise recolor the path and the quoted 'id' inside user-controlled text.
    stderr = StringIO()
    _render_through(
        stderr,
        tmp_path,
        "skipping docs/a.md: no 'id'",
        no_color=False,
        color_system="standard",
        force_terminal=True,
    )

    rendered = stderr.getvalue()
    assert rendered.endswith("skipping docs/a.md: no 'id'\n")  # unstyled after the prefix


def test_wrapped_load_warnings_bypass_an_outer_record_capture(tmp_path: Path):
    # Documented in AD-29 rather than worked around: replacing `showwarning` makes CPython
    # dispatch straight to the substitute, so a recording `catch_warnings` never sees it.
    # Pinned here so a future CLI-level `pytest.warns` cannot pass vacuously by accident.
    stderr = StringIO()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _render_through(stderr, tmp_path, "skipping docs/a.md: no 'id'", color_system=None)

    assert caught == []
    assert stderr.getvalue() == "warning: skipping docs/a.md: no 'id'\n"


def test_project_restores_the_previous_showwarning_after_a_config_error(tmp_path: Path):
    # `project()` and `lattice()` share one context manager; pin both exception paths so a
    # refactor that inlines it into one of them cannot leave the global hook dangling.
    def load_config(config: Path | None, cwd: Path) -> ProjectConfig:
        del config, cwd
        msg = "config blew up"
        raise RuntimeError(msg)

    runtime = CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=Console(file=StringIO(), stderr=True, no_color=True, color_system=None),
        cwd=tmp_path,
        load_config=load_config,
        load_lattice=_unreachable_loader(),
    )

    with warnings.catch_warnings():
        sentinel = warnings.showwarning
        with pytest.raises(RuntimeError, match="config blew up"):
            runtime.project(None)
        assert warnings.showwarning is sentinel


def _annotation_runtime(cwd: Path, workspace: Path | None) -> CliRuntime:
    return replace(
        _runtime(StringIO(), StringIO(), cwd, no_color=True),
        workspace=workspace,
    )


def test_annotation_root_prefers_a_workspace_that_contains_the_document(tmp_path: Path):
    # Under Actions the checkout root is the base that makes an annotation land on the file
    # in the diff, whatever subdirectory the command was invoked from.
    workspace = tmp_path / "checkout"
    nested = workspace / "packages" / "game"
    nested.mkdir(parents=True)
    document = nested / "docs" / "down.md"

    in_workspace = _annotation_runtime(nested, workspace)

    assert in_workspace.annotation_root(document) == workspace


def test_annotation_root_falls_back_to_cwd_when_the_workspace_excludes_the_document(
    tmp_path: Path,
):
    # A set but non-containing GITHUB_WORKSPACE must not reach the renderer: it would emit an
    # absolute path rather than taking the cwd fallback the selection exists to preserve.
    workspace = tmp_path / "other-checkout"
    workspace.mkdir()
    cwd = tmp_path / "elsewhere"
    document = cwd / "docs" / "down.md"

    outside = _annotation_runtime(cwd, workspace)

    assert outside.annotation_root(document) == cwd


def test_annotation_root_falls_back_to_cwd_when_no_workspace_is_set(tmp_path: Path):
    document = tmp_path / "docs" / "down.md"

    no_workspace = _annotation_runtime(tmp_path, None)

    assert no_workspace.workspace is None
    assert no_workspace.annotation_root(document) == tmp_path
