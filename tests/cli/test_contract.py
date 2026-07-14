"""Cross-command and CLI entry-point contract tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.text import Text

import doc_lattice.cli as cli_mod
import doc_lattice.cli.runtime as runtime_module
from doc_lattice import __version__
from doc_lattice.cli import app
from doc_lattice.error_types import ConfigError

from .helpers import _run, runner


def test_cli_imports_when_fcntl_is_unavailable():
    project_root = Path(__file__).resolve().parents[2]
    code = """
import builtins

real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fcntl":
        raise ModuleNotFoundError("fcntl blocked for portability test")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import doc_lattice.cli
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    completed = subprocess.run(  # noqa: S603 (fixed interpreter and static test program)
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def _run_cli_subprocess(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = (
        "import sys\n"
        f"sys.argv = {argv!r}\n"
        "from doc_lattice.cli import main\n"
        "try:\n    main()\nexcept SystemExit:\n    pass\n"
    )
    return subprocess.run(  # noqa: S603 - fixed argv and generated script, no untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=False
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["doc-lattice", "--no-color", "--help"],
        ["doc-lattice", "--no-color", "check", "--format", "json", "--indent", "-1"],
    ],
    ids=["help", "invalid-indent"],
)
def test_no_color_suppresses_typer_rendered_colors(argv):
    # These two invocations never create a callback runtime: --help and an --indent
    # range failure are rendered by Typer's parsing/help consoles first.
    # Regression test for the review finding that --no-color left them styled: even with an
    # ambient FORCE_COLOR (as CI sets), the explicit flag must yield escape-free captured output,
    # so we assert on raw ANSI, not just color spans (bold/dim escapes would otherwise survive).
    env: dict[str, str] = dict(os.environ)
    env["FORCE_COLOR"] = "1"
    env["TERM"] = "xterm-256color"
    env.pop("NO_COLOR", None)
    result = _run_cli_subprocess(argv, env)
    combined = result.stdout + result.stderr
    assert "\x1b[" not in combined, combined


@pytest.mark.parametrize(
    "argv",
    [
        ["doc-lattice", "--help"],
        ["doc-lattice", "check", "--format", "json", "--indent", "-1"],
    ],
    ids=["help", "invalid-indent"],
)
def test_no_color_env_var_suppresses_typer_rendered_colors(argv):
    # The documented NO_COLOR environment variable, not just the --no-color flag, must reach
    # typer's own rich_utils consoles: with a forcing FORCE_COLOR set, NO_COLOR alone otherwise
    # leaves help and parse errors styled. Regression test for that env-only review finding.
    env: dict[str, str] = dict(os.environ)
    env["FORCE_COLOR"] = "1"
    env["TERM"] = "xterm-256color"
    env["NO_COLOR"] = "1"
    result = _run_cli_subprocess(argv, env)
    combined = result.stdout + result.stderr
    assert "\x1b[" not in combined, combined


def test_global_help_lists_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    output = Text.from_ansi(result.stdout).plain
    assert "--no-color" in output


@pytest.mark.parametrize("command", ["check", "lint", "impact", "linear"])
def test_json_commands_help_lists_indent(command, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    output = Text.from_ansi(result.stdout).plain
    assert "--indent" in output
    assert "requires --format json" in output


@pytest.mark.parametrize(
    "command",
    ["check", "lint", "impact", "reconcile", "linear"],
)
def test_removed_json_alias_is_an_unknown_option(lattice_dir: Path, monkeypatch, command: str):
    # The silent --json alias is gone in 2.0; Click must reject it as an unknown option.
    monkeypatch.chdir(lattice_dir)
    args = [command, "--json"]
    if command in {"impact", "reconcile"}:
        args = [command, "target", "--json"]
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    stderr = Text.from_ansi(result.stderr).plain
    assert "No such option: --json" in stderr


def test_reconcile_recover_rejects_removed_json_alias(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--recover", "--json"])

    assert result.exit_code == 2
    stderr = Text.from_ansi(result.stderr).plain
    assert "No such option: --json" in stderr


@pytest.mark.parametrize("command", ["check", "lint"])
def test_report_commands_reject_unknown_format(lattice_dir: Path, monkeypatch, command: str):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, [command, "--format", "nonsense"])

    assert result.exit_code == 2
    assert "nonsense" in result.stderr
    assert "human" in result.stderr
    assert "json" in result.stderr
    assert "github" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["impact", "art-direction#accent", "--format", "nonsense"],
        ["reconcile", "--all", "--format", "nonsense"],
        ["linear", "--format", "nonsense"],
    ],
    ids=["impact", "reconcile", "linear"],
)
def test_basic_commands_reject_unknown_format(lattice_dir: Path, monkeypatch, args: list[str]):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert "nonsense" in result.stderr
    assert "human" in result.stderr
    assert "json" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["check"],
        ["lint"],
        ["impact", "vanished"],
        ["reconcile", "vanished"],
        ["graph"],
        ["linear"],
    ],
    ids=["check", "lint", "impact", "reconcile", "graph", "linear"],
)
@pytest.mark.parametrize("cache_enabled", [False, True], ids=["uncached", "cached"])
def test_lattice_loading_commands_exit_2_on_unclosed_frontmatter(
    tmp_path: Path, args: list[str], cache_enabled: bool
):
    docs = tmp_path / "docs"
    docs.mkdir()
    broken = docs / "broken.md"
    broken.write_text("---\nid: vanished\n# Missing close\n", encoding="utf-8")
    if cache_enabled:
        (tmp_path / ".doc-lattice.yml").write_text("cache_key: cli-unclosed\n", encoding="utf-8")
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1", "COLUMNS": "240"}

    result = _run(args, tmp_path, env)

    assert result.exit_code == 2
    assert "unclosed YAML frontmatter" in result.stderr
    assert str(broken) in result.stderr
    assert "add a closing '---' fence" in result.stderr
    assert "UNREADABLE_DOC" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["lint"],
        ["impact", "art-direction#accent"],
        ["linear"],
    ],
    ids=["lint", "impact", "linear"],
)
def test_indent_without_json_exits_2_before_project_loading(tmp_path: Path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [*args, "--indent", "2"])
    assert result.exit_code == 2
    assert "--indent requires --format json" in result.stderr


@pytest.mark.parametrize(
    ("args", "expected_exit"),
    [
        (["lint"], 0),
        (["impact", "art-direction#accent"], 0),
    ],
    ids=["lint", "impact"],
)
def test_offline_json_indent_round_trips(lattice_dir: Path, monkeypatch, args, expected_exit):
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, [*args, "--format", "json"])
    pretty = runner.invoke(app, [*args, "--format", "json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == expected_exit
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert "\n  " in pretty.stdout


@pytest.mark.parametrize(
    "exc",
    [OSError("io"), RuntimeError("loop"), ValueError("bad"), ConfigError("cfg")],
    ids=["os-error", "runtime-error", "value-error", "config-error"],
)
def test_main_maps_errors_to_exit_2(monkeypatch, exc):
    # An unexpected (non-ProjectError) failure or a ProjectError must not exit 1 and
    # collide with check's drift code; main() maps both to the tool-error code 2.
    def boom():
        raise exc

    monkeypatch.setattr(cli_mod, "app", boom)
    with pytest.raises(SystemExit) as info:
        cli_mod.main()
    assert info.value.code == 2


def test_main_maps_non_callable_app_to_internal_error(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli_mod, "app", object())

    with pytest.raises(SystemExit) as info:
        cli_mod.main()

    assert info.value.code == 2
    assert capsys.readouterr().err == (
        "internal error: RuntimeError: CLI application is not callable\n"
    )


def test_main_renders_internal_error_when_cwd_capture_fails(monkeypatch, capsys):
    class FailingCwdPath:
        def __new__(cls, value: str = ".") -> Path:
            return Path(value)

        @staticmethod
        def cwd() -> Path:
            raise OSError("cwd unavailable")

    def boom() -> None:
        raise OSError("app failure")

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli_mod, "app", boom)
    monkeypatch.setattr(runtime_module, "Path", FailingCwdPath)

    with pytest.raises(SystemExit) as info:
        cli_mod.main()

    assert info.value.code == 2
    assert capsys.readouterr().err == "internal error: OSError: app failure\n"


def test_main_passes_systemexit_through_unchanged(monkeypatch):
    def boom():
        raise SystemExit(1)  # typer's own exit must not be remapped to 2

    monkeypatch.setattr(cli_mod, "app", boom)
    with pytest.raises(SystemExit) as info:
        cli_mod.main()
    assert info.value.code == 1


def test_main_sets_no_color_env_before_app_runs(monkeypatch):
    # Typer/Click build their parsing/help consoles on demand, before a callback-created
    # runtime exists. They honor NO_COLOR when it is set before app() parses argv.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "argv", ["doc-lattice", "--no-color", "check"])
    seen = {}

    def fake_app():
        seen["NO_COLOR"] = os.environ.get("NO_COLOR")

    monkeypatch.setattr(cli_mod, "app", fake_app)
    cli_mod.main()
    assert seen["NO_COLOR"] == "1"


def test_main_leaves_no_color_env_unset_without_flag(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "argv", ["doc-lattice", "check"])
    seen = {}

    def fake_app():
        seen["NO_COLOR"] = os.environ.get("NO_COLOR")

    monkeypatch.setattr(cli_mod, "app", fake_app)
    cli_mod.main()
    assert seen["NO_COLOR"] is None


@pytest.mark.parametrize(
    "args",
    [["check"], ["lint"], ["impact", "art-direction"], ["graph", "--format", "json"]],
    ids=["check", "lint", "impact", "graph-json"],
)
def test_cached_cli_output_matches_uncached(lattice_dir: Path, tmp_path: Path, args):
    # Cold (cache miss, writes the cache) and warm (cache hit) runs must reproduce the
    # uncached run's stdout and exit code byte-for-byte at the CLI layer.
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1"}
    uncached = _run(args, lattice_dir, env)
    (lattice_dir / ".doc-lattice.yml").write_text("cache_key: cli\n", encoding="utf-8")
    cold = _run(args, lattice_dir, env)  # writes cache
    warm = _run(args, lattice_dir, env)  # reads cache
    assert cold.stdout == uncached.stdout
    assert cold.exit_code == uncached.exit_code
    assert warm.stdout == uncached.stdout
    assert warm.exit_code == uncached.exit_code
