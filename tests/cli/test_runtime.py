"""Tests for per-invocation CLI runtime state."""

import os
import sys
from dataclasses import replace
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path

import typer
from rich.console import Console
from typer.testing import CliRunner

import doc_lattice.cli.runtime as runtime_module
from doc_lattice.cli.application import create_app
from doc_lattice.cli.runtime import (
    CliRuntime,
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
