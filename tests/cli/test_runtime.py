"""Tests for per-invocation CLI runtime state."""

import sys
import warnings
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

import doc_lattice.cli.runtime as runtime_module
from doc_lattice.cli.application import create_app
from doc_lattice.cli.runtime import CliRuntime, LatticeLoader, default_runtime, get_runtime
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


def test_lattice_renders_loader_warnings_through_the_invocation_stderr(tmp_path: Path):
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        warnings.warn("skipping /docs/thing.md: no 'id'", stacklevel=1)
        return lattice

    stderr = StringIO()
    runtime = _warning_runtime(stderr, tmp_path, load_lattice)

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

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        raise AssertionError("the config read must not reach the lattice loader")

    stderr = StringIO()
    runtime = CliRuntime(
        stdout=Console(file=StringIO()),
        stderr=Console(file=stderr, stderr=True, no_color=True, color_system=None),
        cwd=tmp_path,
        load_config=load_config,
        load_lattice=load_lattice,
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

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        warnings.warn("\nfound duplicate anchor 'shared'\nfirst occurrence\n", stacklevel=1)
        return lattice

    stderr = StringIO()
    runtime = _warning_runtime(stderr, tmp_path, load_lattice)

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        runtime.lattice(project)

    assert stderr.getvalue() == "warning: found duplicate anchor 'shared'\nfirst occurrence\n"


def test_lattice_warning_renderer_escapes_rich_markup_in_the_message(tmp_path: Path):
    project = ProjectConfig(Config(), tmp_path, (tmp_path / "docs",))
    lattice = Lattice({}, {}, {}, {}, {}, {})

    def load_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        warnings.warn("skipping [bold]docs/a.md[/bold]", stacklevel=1)
        return lattice

    stderr = StringIO()
    runtime = _warning_runtime(stderr, tmp_path, load_lattice)

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
