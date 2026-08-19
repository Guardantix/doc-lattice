"""Cross-command and CLI entry-point contract tests."""

import io
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from rich.text import Text

import doc_lattice.cli as cli_mod
import doc_lattice.cli.runtime as runtime_module
from doc_lattice import __version__
from doc_lattice.cli import app
from doc_lattice.cli.runtime import default_runtime
from doc_lattice.error_types import ConfigError

from .helpers import _run, runner

_SRC = Path(__file__).resolve().parents[2] / "src"

# Rich wraps console output to the terminal width, so any assertion against rendered text has to
# pin that width or it moves with whatever the surrounding environment happens to set. Shared by
# the help-text tests and by the literal invocation-output expectations further down.
#
# `TERM` is pinned alongside `COLUMNS` because Rich reports a hard 80x25 for a console it considers
# a dumb terminal, ignoring `COLUMNS` entirely, and "dumb" or "unknown" is what non-interactive
# shells commonly export. That path needs the console to also look like a terminal, which typer
# decides once: `typer.rich_utils` freezes `FORCE_TERMINAL` from `FORCE_COLOR` and friends at
# import, and the import happens on whichever test first renders rich help. So without this pin the
# width these assertions depend on is a function of ambient `TERM` and test execution order.
_WIDE_CONSOLE_ENV = {"NO_COLOR": "1", "COLUMNS": "1000", "TERM": "xterm-256color"}


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


class _TTYCapture(io.StringIO):
    """In-memory text stream that reports itself as a terminal.

    Rich's terminal detection (``Console.is_terminal``) falls back to
    ``file.isatty()`` once no ``FORCE_COLOR``/``TTY_COMPATIBLE`` override is set, so this
    is enough to make a ``Console`` built by ``_create_runtime`` believe it is writing to
    a real terminal without needing an actual pty.
    """

    def isatty(self) -> bool:
        """Report this stream as a terminal so Rich enables terminal rendering."""
        return True


_PTY_TIMEOUT_SECONDS = 30


def _run_cli_pty(argv: list[str], env: dict[str, str]) -> bytes:
    """Run ``doc_lattice.cli.main`` under a real pty and return the raw stderr bytes.

    A pty is required over ``CliRunner`` or captured pipes: those are never a terminal,
    so Rich would auto-disable highlighting regardless of the fix under test and the
    assertion could pass without it.

    Skips on any platform without ``pty`` (Windows), rather than failing this whole
    module at import time and taking the platform-independent contract tests with it.
    """
    pty = pytest.importorskip("pty", reason="a pty is unavailable on this platform")
    script = (
        "import sys\n"
        f"sys.argv = {argv!r}\n"
        "from doc_lattice.cli import main\n"
        "try:\n    main()\nexcept SystemExit:\n    pass\n"
    )
    # `pty.openpty()` leaves the window size at 0x0, so Rich falls back to its own
    # default width, and an inherited `COLUMNS` would override even that. Pin a wide
    # value so the diagnostic never soft-wraps and the text assertions below are
    # independent of the shell the suite runs under.
    child_env = {**env, "COLUMNS": "200"}
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv and generated script
            [sys.executable, "-c", script],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        try:
            return _drain_pty(master_fd, process)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)


def _drain_pty(master_fd: int, process: "subprocess.Popen[bytes]") -> bytes:
    """Read a pty master until the child closes it, under a hard deadline.

    A bare blocking read would hang the whole suite forever if the child ever wedged,
    since nothing else bounds it; poll with a deadline and fail loudly instead.
    """
    deadline = time.monotonic() + _PTY_TIMEOUT_SECONDS
    chunks = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(f"pty child did not exit within {_PTY_TIMEOUT_SECONDS}s")
        if not select.select([master_fd], [], [], remaining)[0]:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            # The child closed the last slave descriptor; on Linux that surfaces as EIO.
            break
        if not chunk:
            break
        chunks.extend(chunk)
    process.wait(timeout=_PTY_TIMEOUT_SECONDS)
    return bytes(chunks)


def _pty_plain_text(stderr: bytes) -> str:
    """Decode raw pty stderr bytes to plain text without ANSI or CRLF artifacts.

    The pty line discipline rewrites ``\\n`` to ``\\r\\n``, which is a terminal artifact
    unrelated to the escape-sequence contract under test.
    """
    return Text.from_ansi(stderr.replace(b"\r\n", b"\n").decode("utf-8")).plain


@pytest.mark.parametrize(
    ("lever_argv", "lever_env"),
    [
        (["--no-color"], {}),
        ([], {"NO_COLOR": "1"}),
    ],
    ids=["flag", "env"],
)
def test_no_color_lever_leaves_no_escape_under_pty(
    lever_argv: list[str], lever_env: dict[str, str]
):
    # Regression test for GTX-49: `check --only 123` prints its diagnostic through the
    # shared runtime console with no local `highlight=False`, so under a real terminal
    # Rich's automatic highlighter bolds the quoted tokens even though `_create_runtime`
    # already suppressed color via `no_color`. `no_color` alone leaves bold in place;
    # only `highlight=False` plus `color_system=None` make the disabled branch fully
    # escape-free.
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env.update(lever_env)
    argv = ["doc-lattice", *lever_argv, "check", "--only", "123"]

    stderr = _run_cli_pty(argv, env)

    # Assert the diagnostic itself, not merely that stderr is non-empty: any unrelated
    # child failure (an import error traceback, say) is also escape-free, so the
    # escape assertion alone would go green without the fixed path ever running.
    assert "unknown --only state(s): 123" in _pty_plain_text(stderr), stderr
    assert b"\x1b" not in stderr, stderr


def test_no_color_flag_and_env_produce_byte_identical_pty_output():
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    flag_stderr = _run_cli_pty(["doc-lattice", "--no-color", "check", "--only", "123"], env)
    env_lever = dict(env)
    env_lever["NO_COLOR"] = "1"
    env_stderr = _run_cli_pty(["doc-lattice", "check", "--only", "123"], env_lever)

    # Same reasoning as above: two identical crash tracebacks would also compare equal,
    # so pin the diagnostic before comparing bytes.
    assert "unknown --only state(s): 123" in _pty_plain_text(flag_stderr), flag_stderr
    assert flag_stderr == env_stderr


def test_no_lever_pty_output_still_carries_ansi_styling():
    # Positive control: proves the pty harness above actually observes styling when
    # neither lever is set, so the escape-free assertions in the lever-set tests are
    # meaningful rather than a harness artifact. Run in a fresh subprocess (not
    # sequentially in-process): `main()` mutates `NO_COLOR` and
    # `_TYPER_FORCE_DISABLE_TERMINAL` in the ambient environment, so a case that ran
    # after a lever-set case in the same process could be silently contaminated.
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env.pop("FORCE_COLOR", None)
    # Rich also treats a dumb terminal (TERM=dumb/unknown, common in some CI/sandbox
    # shells) as non-styling-capable even when isatty() is true, which would make this
    # positive control false-negative on ambient TERM alone; pin a styling-capable value
    # so the control is deterministic regardless of the environment it runs under.
    env["TERM"] = "xterm-256color"

    stderr = _run_cli_pty(["doc-lattice", "check", "--only", "123"], env)

    assert b"\x1b[" in stderr, stderr
    plain = _pty_plain_text(stderr)
    assert "unknown --only state(s): 123" in plain
    assert "valid: BROKEN, OK, STALE, UNRECONCILED" in plain


def test_create_runtime_disabled_console_strips_bold_and_link_markup(monkeypatch, tmp_path: Path):
    # Explicit-markup probe: `highlight=False` alone only stops Rich's *automatic*
    # highlighter. An explicit `[bold]` or `[link=...]` request from a future command
    # adapter must also render escape-free once a no-color lever is set, so this probes
    # `_create_runtime` (not a hand-built Console) with a terminal-capable stream and
    # explicit markup on both the stdout and stderr consoles.
    stdout_stream = _TTYCapture()
    stderr_stream = _TTYCapture()

    def fake_get_text_stream(name: str) -> io.StringIO:
        return stdout_stream if name == "stdout" else stderr_stream

    monkeypatch.setattr(runtime_module.typer, "get_text_stream", fake_get_text_stream)
    monkeypatch.chdir(tmp_path)

    runtime = default_runtime(no_color=True)
    runtime.stdout.print("[bold]bold text[/bold]")
    runtime.stdout.print("[link=https://example.com]linked text[/link]")
    runtime.stderr.print("[bold]bold text[/bold]")
    runtime.stderr.print("[link=https://example.com]linked text[/link]")

    # Assert both that the tag rendered (markup parsing is still enabled, so `[bold]`
    # was consumed as a style directive) and that it left no escape byte behind. Either
    # assertion alone would miss a `markup=False` regression: the literal tag text also
    # contains "bold text" and also carries zero escape bytes.
    assert "bold text" in stdout_stream.getvalue()
    assert "[bold]" not in stdout_stream.getvalue()
    assert "\x1b" not in stdout_stream.getvalue()
    assert "\x1b" not in stderr_stream.getvalue()


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


def test_wide_console_env_actually_widens_the_console():
    """The pinned width has to hold whatever the ambient environment and test order are.

    Every assertion against rendered text in this module rests on ``_WIDE_CONSOLE_ENV`` producing
    a console wider than Rich's 80-column fallback. That fallback is reachable in more ways than a
    missing ``COLUMNS``: a console Rich considers a dumb terminal reports a hard 80x25 and ignores
    ``COLUMNS`` outright. Asserting the width directly fails here, once, instead of surfacing as a
    fragment assertion that splits somewhere further down the file.
    """
    result = runner.invoke(app, ["--help"], env=_WIDE_CONSOLE_ENV)
    assert result.exit_code == 0
    output = Text.from_ansi(result.stdout).plain
    widest = max((len(line) for line in output.splitlines()), default=0)

    assert widest > 80, f"console fell back to its narrow default; widest line was {widest}"


def test_configless_commands_reject_config_option():
    result = runner.invoke(app, ["init", "--config", ".doc-lattice.yml"])
    assert result.exit_code == 2
    stderr = Text.from_ansi(result.stderr).plain
    assert "No such option: --config" in stderr


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
    [["check"], ["lint"], ["impact", "d"], ["reconcile", "d"], ["graph"], ["linear"]],
    ids=["check", "lint", "impact", "reconcile", "graph", "linear"],
)
def test_lattice_loading_commands_exit_2_on_a_duplicate_ordered_map_key(
    tmp_path: Path, args: list[str]
):
    # The safe constructor rejects a repeated `!!omap` key with a bare `assert`, which is
    # neither a YAMLError nor one of the builtins a tagged scalar raises. Before it joined
    # the load-error family it escaped every handler and printed as an AssertionError
    # traceback rather than a tool error naming the file.
    docs = tmp_path / "docs"
    docs.mkdir()
    broken = docs / "broken.md"
    broken.write_text("---\nid: d\nextra: !!omap\n- a: 1\n- a: 2\n---\nbody\n", encoding="utf-8")
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1", "COLUMNS": "240"}

    result = _run(args, tmp_path, env)

    assert result.exit_code == 2
    assert "UNREADABLE_DOC" in result.stderr
    assert str(broken) in result.stderr
    assert "Traceback" not in result.stderr


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


def test_main_exits_silently_with_141_on_a_broken_pipe(monkeypatch, capsys):
    # A departed reader is not a tool error, and it must not collide with the tool-error
    # exit code 2 or print anything a script piping into `head` would have to filter.
    def boom():
        raise BrokenPipeError

    monkeypatch.setattr(cli_mod, "app", boom)
    with pytest.raises(SystemExit) as info:
        cli_mod.main()
    assert info.value.code == 141
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "stream_error",
    [BrokenPipeError, lambda: ValueError("I/O operation on closed file")],
    ids=["broken-pipe", "closed-file"],
)
@pytest.mark.parametrize(
    "exc",
    [ConfigError("cfg"), RuntimeError("loop")],
    ids=["project-error", "internal-error"],
)
def test_main_exits_cleanly_when_stderr_refuses_the_error_report(monkeypatch, exc, stream_error):
    # An exception raised inside an `except` clause is never retried against a sibling
    # clause of the same `try`, so an unguarded report to a dead stderr would escape
    # main() as an unhandled BrokenPipeError, or the ValueError a closed (rather than
    # broken) stream raises, instead of the clean tool-error exit.
    class _DeadStream(io.StringIO):
        def write(self, s: str) -> int:
            del s
            raise stream_error()

    def boom():
        raise exc

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli_mod, "app", boom)
    monkeypatch.setattr(runtime_module.typer, "get_text_stream", lambda _name: _DeadStream())

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


def test_check_exits_2_on_non_markdown_docs_roots_entry(tmp_path: Path, monkeypatch):
    # AC3: an existing docs_roots entry that is neither a directory nor a .md file must be
    # rejected at config load, before check does any lattice work, with exit code 2.
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [notes.txt]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 2
    assert "'notes.txt'" in result.stderr
    assert "CONFIG_ERROR" in result.stderr


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


def test_multi_line_config_diagnostic_survives_the_stderr_renderer(tmp_path: Path, monkeypatch):
    # The config diagnostic is now multi-line, and three things in print_project_error carry it
    # to the terminal intact: soft_wrap keeps a narrow terminal from rewrapping the per-error
    # lines and destroying the two-space indent, escape() keeps the cache_key message's literal
    # rich markup from being eaten, and the code lands on the header. None of that is visible to
    # a unit test of the formatter, so it is pinned here.
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: 'a/b'\nbogus: 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "60")

    result = runner.invoke(app, ["check"])

    assert result.exit_code == 2
    lines = result.stderr.splitlines()
    assert lines[0] == f"error (CONFIG_ERROR): invalid config {tmp_path / '.doc-lattice.yml'}:"
    # One line per error, still indented, despite a terminal far narrower than any of them.
    assert len(lines) == 3
    assert all(line.startswith("  ") for line in lines[1:])
    assert lines[1] == (
        "  cache_key: cache_key 'a/b' must be one safe path segment matching "
        r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ (no separators or traversal)"
    )
    # The code no longer trails the last detail line, where it read as part of that field's
    # parenthetical rather than as a property of the error.
    assert lines[2] == (
        "  bogus: Extra inputs are not permitted (accepted keys: cache_key, cache_trust_stat, "
        "docs_roots, ignore_globs, linear_team)"
    )


def test_multi_line_frontmatter_diagnostic_carries_its_code_on_the_header(
    tmp_path: Path, monkeypatch
):
    # The second independently formatted multi-line error type. Config and frontmatter reach
    # format_validation_error through different callers and different models, so pinning both
    # proves the placement is the shared renderer's policy rather than one caller's shape.
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [docs]\n", encoding="utf-8")
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir()
    doc.write_text("---\nid: doc-a\nbogus: 1\n---\n# A\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "60")

    result = runner.invoke(app, ["check"])

    assert result.exit_code == 2
    lines = result.stderr.splitlines()
    assert lines == [
        f"error (FRONTMATTER_ERROR): invalid lattice frontmatter in '{doc}':",
        "  bogus: Extra inputs are not permitted (accepted keys: authority, "
        "derives_from, id, layer, tickets, title)",
    ]


def test_single_line_diagnostic_carries_its_code_beside_the_severity(tmp_path: Path, monkeypatch):
    # A single-line diagnostic keeps one grammar with the multi-line ones rather than retaining
    # the old trailing suffix, so a stderr scraper matches one prefix for every project error.
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [notes.txt]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "1000")

    result = runner.invoke(app, ["check"])

    assert result.exit_code == 2
    assert result.stderr == (
        "error (CONFIG_ERROR): docs_roots entry 'notes.txt' exists but is neither a directory "
        f"nor a regular '.md' file ({tmp_path / 'notes.txt'}); an existing entry must be one "
        "or the other\n"
    )


def _many_broken_docs(tmp_path: Path) -> None:
    """Write a lattice whose ``check`` output exceeds a pipe's kernel buffer.

    One small document can be written to a closed pipe inside the single already-buffered
    Rich flush that starts it, which would only prove the first write is guarded; a corpus
    this size forces the run past a 64KiB pipe buffer, so a reader that closes early proves
    the interpreter's own shutdown flush of the now-dead stream is guarded too.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(3000):
        (docs / f"broken-{i:04d}.md").write_text(
            f"---\nid: broken-{i:04d}\nderives_from:\n  - ref: ghost-{i:04d}\n---\n# {i}\n",
            encoding="utf-8",
        )


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_check_exits_141_silently_when_its_reader_closes_the_pipe(tmp_path: Path):
    _many_broken_docs(tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-c", "from doc_lattice.cli import main; main()", "check"],
        cwd=tmp_path,
        env={**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(_SRC)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Close the reader before the child produces any output, the same way `head -1` closes
    # once it has what it wants: every later write from the child hits a dead pipe.
    assert proc.stdout is not None  # guaranteed by stdout=subprocess.PIPE above
    proc.stdout.close()
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 141
    assert stderr == ""


# GTX-125: a document filename is a repo-controlled string that reaches human output without
# passing the frontmatter parser, so these cases inspect the raw bytes a user's terminal
# receives rather than the decoded text a renderer happened to produce. The name carries an SGR
# and a cursor-up: without the display spelling, `ESC[A` overwrites the line printed above it.
_HOSTILE_DOC_NAME = "pwn\x1b[31m\x1b[Aevil.md"


def _run_bytes(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run one command end to end under NO_COLOR and capture undecoded stdout and stderr."""
    return subprocess.run(  # noqa: S603 - fixed argv and generated script, no untrusted input
        [sys.executable, "-c", "from doc_lattice.cli import main; main()", *argv],
        cwd=cwd,
        env={**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(_SRC)},
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
def test_typed_error_under_no_color_emits_no_escape_byte_from_a_document_filename(tmp_path: Path):
    # An unclosed fence is the shortest route to a path-bearing UNREADABLE_DOC on stderr.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / _HOSTILE_DOC_NAME).write_text("---\nid: broken\n", encoding="utf-8")

    completed = _run_bytes(["check"], tmp_path)

    assert completed.returncode == 2
    assert b"UNREADABLE_DOC" in completed.stderr
    assert b"\x1b" not in completed.stderr
    assert b"\x1b" not in completed.stdout
    # The name is still identifiable, in the escaped spelling rather than as raw bytes.
    assert rb"pwn\x1b[31m\x1b[Aevil.md" in completed.stderr


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
def test_reused_anchor_warning_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # The warning site GTX-148 added, reached through the renderer GTX-124 introduced.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / _HOSTILE_DOC_NAME).write_text(
        "---\nid: anchored\nderives_from:\n  - &a {ref: up}\n  - &a {ref: up2}\n---\n# H\n",
        encoding="utf-8",
    )
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\n", encoding="utf-8")
    (docs / "up2.md").write_text("---\nid: up2\n---\n# Up2\n", encoding="utf-8")

    completed = _run_bytes(["check"], tmp_path)

    assert b"reused anchor in " in completed.stderr  # AD-29: the prefix is load-bearing
    assert b"\x1b" not in completed.stderr
    assert rb"pwn\x1b[31m\x1b[Aevil.md" in completed.stderr


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
def test_recovery_problem_report_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # GTX-209. A reconcile stage is named after the destination it is written beside, so a
    # hostile document filename propagates into the transaction's own artifact paths. Recovery
    # then prints those paths back, which is where a crafted name could overwrite a line of the
    # rollback report a user is reading while deciding how to repair a half-applied
    # transaction. Nothing here raises: `_report_recovery_problems` writes straight to stderr,
    # so no diagnostic renderer is doing the escaping.
    docs = tmp_path / "docs"
    docs.mkdir()
    orphan = docs / f".{_HOSTILE_DOC_NAME}.doc-lattice-after.leaked.tmp"
    orphan.write_bytes(b"leaked stage\n")

    completed = _run_bytes(["reconcile", "--recover"], tmp_path)

    assert completed.returncode == 2
    assert b"orphaned artifact: " in completed.stderr
    assert b"\x1b" not in completed.stderr
    assert b"\x1b" not in completed.stdout
    assert rb"pwn\x1b[31m\x1b[Aevil.md" in completed.stderr
    # Reported, never removed: the recovery contract deletes nothing it cannot account for.
    assert orphan.exists()


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
def test_recovery_json_under_no_color_keeps_the_raw_machine_spelling(tmp_path: Path):
    # The other half of the same run. AD-34 excludes machine channels, so `--format json`
    # still carries the raw filename: JSON's own encoder is what makes it safe to parse, and
    # substituting a display spelling would change values a consumer resolves against.
    docs = tmp_path / "docs"
    docs.mkdir()
    orphan = docs / f".{_HOSTILE_DOC_NAME}.doc-lattice-after.leaked.tmp"
    orphan.write_bytes(b"leaked stage\n")

    completed = _run_bytes(["reconcile", "--recover", "--format", "json"], tmp_path)

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["orphans"] == [f"docs/{orphan.name}"]


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
def test_direct_console_write_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # `impact`'s human report is a success-path write, not a diagnostic: the README promise
    # covers every command's output, so this sink is in scope alongside the error and warning
    # ones. Nothing on this path raises, so no diagnostic renderer could be doing the escaping.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\n", encoding="utf-8")
    (docs / _HOSTILE_DOC_NAME).write_text(
        "---\nid: down\nderives_from:\n  - ref: up\n---\n# Down\n", encoding="utf-8"
    )

    completed = _run_bytes(["impact", "up"], tmp_path)

    assert completed.returncode == 0
    assert b"down" in completed.stdout
    assert b"\x1b" not in completed.stdout
    assert rb"pwn\x1b[31m\x1b[Aevil.md" in completed.stdout


# GTX-208: the other half of the repo-controlled vector. A filename never passes the frontmatter
# parser; a typed value does, and YAML hands it back with the control byte intact whenever the
# document spells it as a double-quoted escape. AD-35 refuses such a document at validation, so
# what these assert end to end is that no lattice-loading command prints the byte and that the
# refusal itself names the code point instead of echoing the value. The bytes are inspected
# undecoded for the same reason the cases above are.
_ESCAPED_CONTROL_DOC = (
    "---\n"
    'id: "down\\u001b[31m"\n'
    'title: "t\\u001b[2J"\n'
    'tickets: ["GTX-1\\u001b[31m"]\n'
    "derives_from:\n"
    '  - ref: "up\\u001b[A"\n'
    '    seen: "h\\u001b[B"\n'
    "---\n"
    "# Down\nbody\n"
)


def _escaped_control_lattice(tmp_path: Path) -> None:
    """Write a two-document lattice whose downstream spells control bytes as YAML escapes."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(_ESCAPED_CONTROL_DOC, encoding="utf-8")


@pytest.mark.parametrize(
    "argv",
    [["check"], ["lint"], ["impact", "up"], ["graph"], ["reconcile", "--all", "--dry-run"]],
    ids=["check", "lint", "impact", "graph", "reconcile"],
)
def test_escaped_control_values_reach_no_command_output_under_no_color(
    tmp_path: Path, argv: list[str]
):
    _escaped_control_lattice(tmp_path)

    completed = _run_bytes(argv, tmp_path)

    assert completed.returncode == 2
    assert b"FRONTMATTER_ERROR" in completed.stderr
    # The refusal names the code point and the position, which is what lets it stay control-free
    # while still identifying the offending value.
    assert b"U+001B" in completed.stderr
    for stream in (completed.stdout, completed.stderr):
        assert b"\x1b" not in stream, (argv, stream)
        assert not _control_characters(stream), (argv, stream)


def test_an_admitted_value_still_reaches_the_machine_channels_verbatim(tmp_path: Path):
    # The other half of AD-35's claim: the rule refuses documents, it does not rewrite values.
    # A document holding the neighbors of the refused ranges still loads, and what JSON and the
    # GitHub annotation carry for it is the value as written, with no display spelling and no
    # stripping applied on the way out.
    neighbors = "".join(chr(code) for code in (0xA0, 0x2028, 0xFEFF))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "down.md").write_text(
        f'---\nid: "down{neighbors}"\nderives_from:\n  - ref: ghost\n---\n# Down\nbody\n',
        encoding="utf-8",
    )

    as_json = _run_bytes(["check", "--format", "json"], tmp_path)
    as_github = _run_bytes(["check", "--format", "github"], tmp_path)

    assert as_json.returncode == 1
    payload = json.loads(as_json.stdout.decode("utf-8"))
    assert payload["edges"][0]["source_id"] == f"down{neighbors}"
    assert as_github.returncode == 1
    assert f"down{neighbors} -> ghost is BROKEN".encode() in as_github.stdout


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
