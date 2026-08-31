"""Cross-command and CLI entry-point contract tests."""

import io
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
from rich.console import Console
from rich.text import Text

import doc_lattice.cli as cli_mod
import doc_lattice.cli.runtime as runtime_module
from doc_lattice import __version__
from doc_lattice.cli import app
from doc_lattice.cli.errors import EXIT_TOOL_ERROR
from doc_lattice.cli.github import escape_github_property
from doc_lattice.cli.runtime import default_runtime
from doc_lattice.error_types import ConfigError
from doc_lattice.path_utils import format_path_for_display

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


def test_version_flag_is_eager_and_ignores_a_broken_config(tmp_path: Path, monkeypatch):
    """--version answers before any command runs, so a config that would fail cannot stop it.

    README publishes this as the check to run against a fresh install: no config file, no docs
    root, no network. A config that any command would reject with CONFIG_ERROR is the sharpest
    way to pin that, since it fails only if config loading moved ahead of the eager callback.
    """
    (tmp_path / ".doc-lattice.yml").write_text("bogus: 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["check"]).exit_code == EXIT_TOOL_ERROR

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
    assert "valid: AMBIGUOUS, BROKEN, OK, STALE, UNRECONCILED" in plain


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


def test_parser_rejection_carries_neither_a_code_nor_the_error_prefix():
    """Pin the shape README's error-code section gives a parser-rejected usage failure.

    A usage check an adapter writes itself prints ``error: ...`` through ``print_project_error``'s
    uncoded sibling path. A failure Typer rejects before any command runs never reaches that
    module, so it carries neither a code nor that prefix, and README documents the two separately.
    """
    result = runner.invoke(app, ["--bogus"])
    assert result.exit_code == 2
    stderr = Text.from_ansi(result.stderr).plain
    # The runner invokes the app object, so the program name is its own, not the console script's.
    # The shape is what README documents, so pin the lead-in and not the name in front of it.
    assert stderr.startswith("Usage: ")
    assert "No such option: --bogus" in stderr
    assert "error:" not in stderr
    assert "error (" not in stderr


def test_no_arguments_prints_help_and_exits_two_with_no_diagnostic():
    """The one exit 2 that prints no diagnostic at all, which README names as such."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    # Compared raw, not through Text.from_ansi: that helper drops a trailing newline on the
    # rich floor (13.8.0) and keeps it on current rich, so it cannot carry an exact-equality
    # assertion across the supported range. It stays for the substring reads below.
    assert result.stderr == ""
    assert "Usage: " in Text.from_ansi(result.stdout).plain


def test_adapter_authored_usage_check_prints_the_uncoded_error_prefix():
    """The contrasting shape: an adapter's own usage check, uncoded but prefixed."""
    result = runner.invoke(app, ["check", "--indent", "2"])
    assert result.exit_code == 2
    assert result.stderr == "error: --indent requires --format json\n"


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
    [ConfigError("cfg"), RuntimeError("loop"), UserWarning("advisory")],
    ids=["project-error", "internal-error", "escalated-warning"],
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


class _DependencyWarning(Warning):
    """Stands in for a category a dependency declares, such as ruamel's ``ReusedAnchorWarning``.

    Declared here rather than imported so the case stays about the base class the boundary
    catches: a dependency that renames or reparents its own category must not change what this
    asserts.
    """


@pytest.mark.parametrize(
    "exc",
    [
        UserWarning("skipping 'docs/x.md': its frontmatter declares no 'id'"),
        DeprecationWarning("a dependency deprecated something"),
        _DependencyWarning("found duplicate anchor 't'"),
    ],
    ids=["engine-category", "stdlib-category", "dependency-category"],
)
def test_main_renders_an_escalated_warning_as_a_coded_project_error(monkeypatch, capsys, exc):
    # Three categories from three owners, because catching the base `Warning` is what makes the
    # mapping complete: no shared engine category exists to keep in sync, and the two paths
    # AD-29 leaves outside the strict frontmatter boundary raise a dependency's own class.
    def boom():
        raise exc

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli_mod, "app", boom)

    with pytest.raises(SystemExit) as info:
        cli_mod.main()

    assert info.value.code == 2  # never 1, which check reserves for drift
    assert capsys.readouterr().err == (
        f"error (WARNING_AS_ERROR): {type(exc).__name__}: {exc}; a warning filter escalated "
        "this advisory to an error, so the run stopped here instead of continuing past it\n"
    )


def test_main_maps_a_warning_escalated_while_loading_the_application(monkeypatch, capsys):
    # `_load_app()` imports every engine module and their dependencies, so under an escalating
    # filter a deprecation raised at import time is an escalated warning like any other. It sits
    # inside the guarded block for that reason; run before it, this case is a bare traceback.
    def boom():
        raise DeprecationWarning("imported module is deprecated")

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli_mod, "_load_app", boom)

    with pytest.raises(SystemExit) as info:
        cli_mod.main()

    assert info.value.code == 2
    assert capsys.readouterr().err == (
        "error (WARNING_AS_ERROR): DeprecationWarning: imported module is deprecated; a warning "
        "filter escalated this advisory to an error, so the run stopped here instead of "
        "continuing past it\n"
    )


def test_main_maps_a_warning_escalated_while_importing_the_boundarys_own_reporter():
    """The support imports are guarded too, and report without the reporter that failed.

    ``from .errors import ...`` reaches ``cli/runtime.py``, and through it ``config`` and
    ``orchestrate`` -- 25 engine modules plus ruamel, markdown-it, rich, and typer. Under an
    escalating filter an import-time deprecation among them raises *before* a renderer exists,
    so the clause that handles it cannot use ``print_project_error``: that function is what
    failed to import. It falls back to a plain ``sys.stderr`` write in the same grammar.

    A subprocess with a patched ``__import__`` is what reaches this. In-process, the module is
    already in ``sys.modules`` and the import statement never runs a finder at all.
    """
    project_root = Path(__file__).resolve().parents[2]
    code = """
import builtins

real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "errors" and level == 1:
        raise DeprecationWarning("a dependency deprecated something at import time")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
from doc_lattice.cli import main
main()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")
    env["NO_COLOR"] = "1"

    completed = subprocess.run(  # noqa: S603 (fixed interpreter and static test program)
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stderr == (
        "error (WARNING_AS_ERROR): DeprecationWarning: a dependency deprecated something at "
        "import time; a warning filter escalated this advisory to an error, so the run stopped "
        "here instead of continuing past it\n"
    )
    assert "Traceback" not in completed.stderr


def test_the_support_import_fallback_renders_without_the_reporter_it_lost(monkeypatch, capsys):
    # The same clause as the subprocess case above, reached in-process so what it writes is
    # asserted against a captured stream rather than against a process's bytes. A module object
    # that raises on attribute access is what `from .errors import ...` meets once the real
    # module is already cached, and `monkeypatch.setitem` puts the real one back afterwards.
    class _WarnsOnAccess(ModuleType):
        def __getattr__(self, name: str) -> object:
            # Dunders are the import machinery's own probes -- it reads `__path__` before any
            # name in the statement -- so they answer the way a plain module would and only the
            # imported name raises.
            if name.startswith("__"):
                raise AttributeError(name)
            raise DeprecationWarning(f"reading {name} is deprecated")

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setitem(
        sys.modules, "doc_lattice.cli.errors", _WarnsOnAccess("doc_lattice.cli.errors")
    )

    with pytest.raises(SystemExit) as info:
        cli_mod.main()

    assert info.value.code == 2
    assert capsys.readouterr().err == (
        "error (WARNING_AS_ERROR): DeprecationWarning: reading EXIT_PIPE_CLOSED is deprecated; "
        "a warning filter escalated this advisory to an error, so the run stopped here instead "
        "of continuing past it\n"
    )


def test_the_support_import_fallback_exit_code_matches_the_shared_tool_error_code():
    # That fallback cannot import `EXIT_TOOL_ERROR` -- importing the module that defines it is
    # exactly what failed -- so it raises the literal 2. This is the pin that keeps the literal
    # and the constant from drifting apart in silence.
    assert EXIT_TOOL_ERROR == 2


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
    displayed = format_path_for_display(tmp_path / ".doc-lattice.yml")
    assert lines[0] == f"error (CONFIG_ERROR): invalid config {displayed}:"
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


# ---------------------------------------------------------------------------
# GTX-204: a document-scoped failure is annotated on the document it names.
#
# Parameterized over both annotating commands rather than pinned to one: the rule lives in the
# shared `exit_on_project_error` boundary, and a test that only ran `check` would pass on an
# implementation that wired the flag into one adapter and forgot the other.

_ANNOTATING_COMMANDS = ("check", "lint")

_BOGUS_KEY_DETAIL = (
    "  bogus: Extra inputs are not permitted (accepted keys: authority, derives_from, id, "
    "layer, tickets, title)"
)


def _broken_frontmatter_project(root: Path) -> Path:
    """Write a project whose only document fails NodeMeta validation.

    Returns:
        The document that fails, for the diagnostic the caller asserts on.
    """
    (root / ".doc-lattice.yml").write_text("docs_roots: [docs]\n", encoding="utf-8")
    doc = root / "docs" / "a.md"
    doc.parent.mkdir()
    doc.write_text("---\nid: doc-a\nbogus: 1\n---\n# A\n", encoding="utf-8")
    return doc


@pytest.mark.parametrize("command", _ANNOTATING_COMMANDS)
def test_github_format_annotates_a_document_whose_frontmatter_is_broken(
    command: str, tmp_path: Path, monkeypatch
):
    # Before GTX-204 this exited 2 with a stderr line and no annotation at all, so a pull request
    # whose gate failed on a frontmatter defect showed nothing on the diff.
    doc = _broken_frontmatter_project(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, "--format", "github"])

    assert result.exit_code == 2
    assert result.stdout == (
        "::error file=docs/a.md,title=doc-lattice FRONTMATTER_ERROR::"
        f"invalid lattice frontmatter in '{doc}':%0A{_BOGUS_KEY_DETAIL}\n"
    )


@pytest.mark.parametrize("command", _ANNOTATING_COMMANDS)
def test_github_annotated_failure_keeps_its_stderr_diagnostic_and_exit_code(
    command: str, tmp_path: Path, monkeypatch
):
    # The annotation is an addition, not a replacement: a human reading the job log still gets
    # the coded diagnostic, and a caller matching on exit 2 is unaffected.
    doc = _broken_frontmatter_project(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, "--format", "github"])

    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        f"error (FRONTMATTER_ERROR): invalid lattice frontmatter in '{doc}':",
        _BOGUS_KEY_DETAIL,
    ]


@pytest.mark.parametrize("command", _ANNOTATING_COMMANDS)
def test_github_annotation_resolves_against_a_nested_annotation_root(
    command: str, tmp_path: Path, monkeypatch
):
    # The same base selection drift findings use: invoked from a subdirectory, the annotation is
    # still workspace-relative, which is the only spelling GitHub attaches to a diff.
    _broken_frontmatter_project(tmp_path)
    nested = tmp_path / "tools" / "scripts"
    nested.mkdir(parents=True)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app, [command, "--config", "../../.doc-lattice.yml", "--format", "github"]
    )

    assert result.exit_code == 2
    assert result.stdout.startswith("::error file=docs/a.md,title=doc-lattice FRONTMATTER_ERROR::")


@pytest.mark.parametrize("command", _ANNOTATING_COMMANDS)
def test_github_format_annotates_an_unreadable_document(command: str, tmp_path: Path, monkeypatch):
    # The rule is the document-scoped base, not the frontmatter type: an unreadable document is
    # equally a per-document defect and is annotated by the same branch.
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [docs]\n", encoding="utf-8")
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir()
    doc.write_text("---\nid: doc-a\n# A\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, "--format", "github"])

    assert result.exit_code == 2
    assert result.stdout == (
        "::error file=docs/a.md,title=doc-lattice UNREADABLE_DOC::"
        f"unclosed YAML frontmatter in '{doc}': add a closing '---' fence\n"
    )


@pytest.mark.parametrize("command", _ANNOTATING_COMMANDS)
@pytest.mark.parametrize("fmt", ["human", "json"])
def test_a_non_annotating_format_writes_nothing_to_stdout_on_a_document_failure(
    command: str, fmt: str, tmp_path: Path, monkeypatch
):
    # The pre-GTX-204 shape for every renderer that is not the annotation channel: stderr only,
    # exit 2, and an empty stdout a JSON consumer can still tell apart from an empty report.
    doc = _broken_frontmatter_project(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, "--format", fmt])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        f"error (FRONTMATTER_ERROR): invalid lattice frontmatter in '{doc}':",
        _BOGUS_KEY_DETAIL,
    ]


@pytest.mark.parametrize("command", _ANNOTATING_COMMANDS)
def test_github_format_does_not_annotate_a_failure_with_no_document_subject(
    command: str, tmp_path: Path, monkeypatch
):
    # A config defect names no document, so there is nothing for GitHub to attach an annotation
    # to; emitting one against an arbitrary base would put the failure on the wrong file.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, "--config", "missing.yml", "--format", "github"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "CONFIG_ERROR" in result.stderr


@pytest.mark.parametrize("command", _ANNOTATING_COMMANDS)
def test_an_unattachable_failure_annotation_is_reported_on_stderr(
    command: str, tmp_path: Path, monkeypatch
):
    # This run's only annotation is the failure's, so an absolute path GitHub drops leaves the
    # gate failing with nothing on the diff. The findings renderers already warn about that; the
    # failure path owes the same report or the case is undiagnosable from the workflow log.
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [docs]\n", encoding="utf-8")
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir()
    doc.write_text("---\nid: doc-a\nbogus: 1\n---\n# A\n", encoding="utf-8")
    nested = tmp_path / "tools"
    nested.mkdir()
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, [command, "--config", "../.doc-lattice.yml", "--format", "github"])

    assert result.exit_code == 2
    assert result.stdout.startswith(f"::error file={escape_github_property(str(doc))},")
    assert "1 annotated document(s) fall outside" in result.stderr
    assert "FRONTMATTER_ERROR" in result.stderr


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
        f"nor a regular '.md' file ({format_path_for_display(tmp_path / 'notes.txt')}); an "
        "existing entry must be one or the other\n"
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


# GTX-212: the config file and the cache location are user-supplied the way a document filename
# is repo-controlled, and their modules were exempt from the display guard as a whole until now.
_HOSTILE_CONFIG_NAME = "cfg\x1b[31m\x1b[Aevil.yml"
_HOSTILE_CACHE_DIR = "pwn\x1b[31m\x1b[Aevil-cache"

# Every C0 code point plus DEL and the C1 range, which is the width README's escape-free promise
# covers. A line feed is excluded: a diagnostic may legitimately span lines, and the promise is
# about what a filename can smuggle into one.
_FORBIDDEN_CONTROLS = frozenset(chr(code) for code in [*range(0x20), 0x7F, *range(0x80, 0xA0)]) - {
    "\n"
}


def _assert_control_free(raw: bytes) -> None:
    """Assert a captured stream carries no code point a terminal acts on."""
    text = raw.decode("utf-8", errors="surrogateescape")
    leaked = sorted({char for char in text if char in _FORBIDDEN_CONTROLS})
    assert not leaked, f"output leaked raw control code points {leaked!r}: {text!r}"


def _run_bytes(
    argv: list[str], cwd: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run one command end to end under NO_COLOR and capture undecoded stdout and stderr."""
    return subprocess.run(  # noqa: S603 - fixed argv and generated script, no untrusted input
        [sys.executable, "-c", "from doc_lattice.cli import main; main()", *argv],
        cwd=cwd,
        env={**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(_SRC), **(extra_env or {})},
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
def test_missing_config_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # GTX-212's first reproduction. `--config` is user-supplied rather than repo-controlled, and
    # `config.py` was exempt from the display guard as a whole module, so this printed the raw
    # bytes: `ESC[A` here overwrites whatever the terminal drew on the line above.
    completed = _run_bytes(["check", "--config", _HOSTILE_CONFIG_NAME], tmp_path)

    assert completed.returncode == 2
    assert b"config file not found" in completed.stderr
    _assert_control_free(completed.stderr)
    _assert_control_free(completed.stdout)
    assert rb"cfg\x1b[31m\x1b[Aevil.yml" in completed.stderr


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
def test_unparseable_config_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # GTX-212's second reproduction, and the composition boundary with AD-37: the path is
    # spelled by this issue's helper and the parser's own message by GTX-219's, in one line.
    (tmp_path / _HOSTILE_CONFIG_NAME).write_text("docs_roots: [\n", encoding="utf-8")

    completed = _run_bytes(["check", "--config", _HOSTILE_CONFIG_NAME], tmp_path)

    assert completed.returncode == 2
    assert b"cannot parse config" in completed.stderr
    _assert_control_free(completed.stderr)
    assert rb"cfg\x1b[31m\x1b[Aevil.yml" in completed.stderr


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
def test_cache_write_warning_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # The cache path is built from the user's cache home, so a hostile directory there reaches
    # the one diagnostic `cache/store.py` writes straight to stderr with no renderer in front of
    # it. The write is made to fail by putting a regular file where the cache home belongs,
    # which turns the store's own `mkdir` into a NotADirectoryError.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: slot\n", encoding="utf-8")
    blocked = tmp_path / _HOSTILE_CACHE_DIR
    blocked.write_text("a file, not a directory\n", encoding="utf-8")

    completed = _run_bytes(["check"], tmp_path, extra_env={"XDG_CACHE_HOME": str(blocked)})

    assert b"could not write load cache at" in completed.stderr
    _assert_control_free(completed.stderr)
    assert rb"pwn\x1b[31m\x1b[Aevil-cache" in completed.stderr


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


def _run_bytes_under_lever(
    argv: list[str], cwd: Path, lever_env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Run one command under exactly one no-color lever and capture undecoded output.

    `_run_bytes` always sets `NO_COLOR`, which would mask the `--no-color` flag it is meant to be
    compared against. This clears both levers first so the caller's choice is the only one in
    effect.
    """
    env: dict[str, str] = {**os.environ, "PYTHONPATH": str(_SRC)}
    env.pop("NO_COLOR", None)
    env.pop("FORCE_COLOR", None)
    env.update(lever_env)
    return subprocess.run(  # noqa: S603 - fixed argv and generated script, no untrusted input
        [sys.executable, "-c", "from doc_lattice.cli import main; main()", *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
@pytest.mark.parametrize(
    ("lever_argv", "lever_env"),
    [
        (["--no-color"], {}),
        ([], {"NO_COLOR": "1"}),
    ],
    ids=["flag", "env"],
)
def test_github_annotation_file_is_excluded_from_the_escape_free_promise(
    tmp_path: Path, lever_argv: list[str], lever_env: dict[str, str]
):
    # GTX-214 / AD-38. Every other case in this block asserts that no escape byte survives a
    # no-color lever; this one asserts that in exactly one channel it must. GitHub resolves an
    # annotation's `file=` against the document it attaches to, and the workflow-command grammar
    # substitutes only `%`, `:`, `,`, CR, and LF, so there is no spelling of an ESC it decodes
    # back to the original filename. Sanitizing here would silently detach the annotation
    # instead, which is why README names this exclusion rather than making the promise hold.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text(
        "---\nid: up\nlayer: design\n---\n# Up {#up-top}\n\n## Sec {#sec}\nbody v2\n",
        encoding="utf-8",
    )
    (docs / _HOSTILE_DOC_NAME).write_text(
        "---\nid: down\nlayer: design\nderives_from:\n"
        "  - ref: up#sec\n    seen: staleseenhashstaleseenhashstale00\n---\n# Down\nbody\n",
        encoding="utf-8",
    )

    completed = _run_bytes_under_lever(
        [*lever_argv, "check", "--format", "github"], tmp_path, lever_env
    )

    assert completed.returncode == 1
    expected = b"::error file=docs/" + _HOSTILE_DOC_NAME.encode() + b",title=doc-lattice STALE::"
    assert completed.stdout.startswith(expected)
    assert b"\x1b" in completed.stdout
    # The exclusion is exactly this wide: the run's other stream still keeps the guarantee.
    assert b"\x1b" not in completed.stderr


@pytest.mark.skipif(os.name != "posix", reason="a filename holding ESC is POSIX-only")
@pytest.mark.parametrize(
    ("lever_argv", "lever_env"),
    [
        (["--no-color"], {}),
        ([], {"NO_COLOR": "1"}),
    ],
    ids=["flag", "env"],
)
def test_human_check_of_the_same_lattice_stays_escape_free(
    tmp_path: Path, lever_argv: list[str], lever_env: dict[str, str]
):
    # The other half of AD-38's narrowing, on the identical lattice: narrowing the promise for
    # the annotation channel must not quietly narrow it for the default one. Without this, a
    # regression that started leaking the filename into human output would still pass the case
    # above and contradict nothing the suite checks.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text(
        "---\nid: up\nlayer: design\n---\n# Up {#up-top}\n\n## Sec {#sec}\nbody v2\n",
        encoding="utf-8",
    )
    (docs / _HOSTILE_DOC_NAME).write_text(
        "---\nid: down\nlayer: design\nderives_from:\n"
        "  - ref: up#sec\n    seen: staleseenhashstaleseenhashstale00\n---\n# Down\nbody\n",
        encoding="utf-8",
    )

    completed = _run_bytes_under_lever([*lever_argv, "check"], tmp_path, lever_env)

    assert completed.returncode == 1
    assert b"STALE" in completed.stdout
    assert b"\x1b" not in completed.stdout
    assert b"\x1b" not in completed.stderr


# GTX-219: a YAML load failure's message is built by `ruamel` rather than by this project, and it
# echoes the document back at the reader, so these two inspect the raw bytes a terminal receives
# rather than the decoded text. No POSIX guard is needed, unlike the filename-bearing cases above:
# the control bytes live inside the file's own content, which every platform can hold. The block
# defeats the value rule of GTX-208 outright, because a duplicate key fails the load before any
# value is validated.
_ECHOED_DUPLICATE_KEY = 'k: "v\\u001b[31mA"\nk: "v\\u001b[31mB"\n'


def test_frontmatter_load_failure_under_no_color_emits_no_escape_byte(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "down.md").write_text(
        f"---\nid: down\n{_ECHOED_DUPLICATE_KEY}---\n# H\n", encoding="utf-8"
    )

    completed = _run_bytes(["check"], tmp_path)

    assert completed.returncode == 2
    assert b"UNREADABLE_DOC" in completed.stderr
    assert b"\x1b" not in completed.stderr
    assert b"\x1b" not in completed.stdout
    # The echoed value is still identifiable, in the escaped spelling rather than as raw bytes.
    assert rb"\x1b" in completed.stderr


def test_config_load_failure_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # The config boundary shares the shape and is reached before any document is read.
    (tmp_path / ".doc-lattice.yml").write_text(_ECHOED_DUPLICATE_KEY, encoding="utf-8")

    completed = _run_bytes(["check"], tmp_path)

    assert completed.returncode == 2
    assert b"CONFIG_ERROR" in completed.stderr
    assert b"\x1b" not in completed.stderr
    assert b"\x1b" not in completed.stdout
    assert rb"\x1b" in completed.stderr


# GTX-196. A hand-edited journal, spelled here as the literal bytes one would carry, because the
# engine cannot write these values itself: `tool_version` comes from the package constant, and a
# selector is only recorded once its id or ref has matched a control-free frontmatter value. No
# POSIX guard is needed, unlike the filename-bearing cases above: the control bytes live inside a
# JSON file, which every platform can hold.
_CONTROL_BEARING_JOURNAL = """{
  "version": 2,
  "state": "committed",
  "provenance": {
    "created_at": "2026-08-17T12:00:00Z",
    "tool_version": "5.0.0\\u001b[31m",
    "selector": {
      "mode": "downstream",
      "downstream_id": "pc\\u001b[Adesign",
      "ref": null
    }
  },
  "entries": []
}
"""


def test_journal_provenance_under_no_color_emits_no_escape_byte(tmp_path: Path):
    # AD-36 rests on this: provenance is spelled for display rather than refused, so the whole
    # security argument for that choice is that the spelling leaves no byte a terminal acts on.
    # Asserted at the file-descriptor level like its GTX-209 siblings above, because the in-process
    # runner cannot see a Console that decided to emit color for a real terminal.
    (tmp_path / ".doc-lattice-reconcile.json").write_text(
        _CONTROL_BEARING_JOURNAL, encoding="utf-8"
    )

    completed = _run_bytes(["reconcile", "--recover"], tmp_path)

    assert completed.returncode == 0
    assert b"cleaned committed reconcile transaction" in completed.stdout
    assert b"\x1b" not in completed.stdout
    assert b"\x1b" not in completed.stderr
    assert rb"tool_version: '5.0.0\x1b[31m'" in completed.stdout
    assert rb"downstream_id 'pc\x1b[Adesign'" in completed.stdout


# GTX-227. The other journal-sourced string that reaches human output, and the one AD-36 left
# open: every wire model forbids extra keys, so a hand-edited key is reported as the pydantic
# error *location*, which the renderer spells rather than drops. Parametrized over both wire
# versions because each selects a different model, and a v2-only case would pass even if v1 were
# rendered against v2. Spelled as literal bytes for the reason the provenance case above is: the
# engine cannot write this file.
_HOSTILE_JOURNAL_KEY = "bad\x1b[31m\x1b[Akey"

_HOSTILE_KEY_JOURNALS = {
    1: {
        "version": 1,
        "state": "committed",
        "entries": [],
        _HOSTILE_JOURNAL_KEY: 1,
    },
    2: {
        "version": 2,
        "state": "committed",
        "provenance": {
            "created_at": "2026-08-17T12:00:00Z",
            "tool_version": "5.0.0",
            "selector": {"mode": "all", "downstream_id": None, "ref": None},
        },
        "entries": [],
        _HOSTILE_JOURNAL_KEY: 1,
    },
}


@pytest.mark.parametrize("version", [1, 2])
def test_journal_extra_key_under_no_color_emits_no_escape_byte(tmp_path: Path, version: int):
    # Asserted over raw stderr with the shared helper rather than a width restated beside it, so
    # this case and its GTX-125 siblings cannot disagree about what a control character is.
    (tmp_path / ".doc-lattice-reconcile.json").write_text(
        json.dumps(_HOSTILE_KEY_JOURNALS[version]), encoding="utf-8"
    )

    completed = _run_bytes(["reconcile", "--recover"], tmp_path)

    assert completed.returncode == EXIT_TOOL_ERROR
    assert b"RECONCILE_PERSISTENCE" in completed.stderr
    _assert_control_free(completed.stderr)
    _assert_control_free(completed.stdout)
    # The key is still named, spelled the way AD-35 spells a rejected frontmatter key.
    assert rb"'bad\x1b[31m\x1b[Akey'" in completed.stderr
    assert b"Extra inputs are not permitted" in completed.stderr
    # The remediation stayed its own line instead of trailing the field line it would read as
    # part of, and pydantic's own renderer never reached the user.
    assert completed.stderr.splitlines()[-1].startswith(b"inspect ")
    assert b"errors.pydantic.dev" not in completed.stderr


def test_journal_extra_key_names_only_the_keys_its_own_version_accepts(tmp_path: Path):
    # The invariant a single-version case cannot see: the accepted-key help comes from the model
    # that actually rejected the text, so a v1 journal is never offered v2's provenance key.
    (tmp_path / ".doc-lattice-reconcile.json").write_text(
        json.dumps(_HOSTILE_KEY_JOURNALS[1]), encoding="utf-8"
    )

    completed = _run_bytes(["reconcile", "--recover"], tmp_path)

    assert completed.returncode == EXIT_TOOL_ERROR
    assert b"accepted keys: entries, state, version" in completed.stderr
    assert b"provenance" not in completed.stderr


def test_journal_provenance_json_under_no_color_keeps_the_recorded_value(tmp_path: Path):
    # The other half of the same run, and the reason AD-36 needs no refusal to keep this channel
    # safe: the machine payload carries the recorded value rather than a display spelling, and
    # `json.dumps` escapes the control byte on its own rather than emitting it raw.
    (tmp_path / ".doc-lattice-reconcile.json").write_text(
        _CONTROL_BEARING_JOURNAL, encoding="utf-8"
    )

    completed = _run_bytes(["reconcile", "--recover", "--format", "json"], tmp_path)

    assert completed.returncode == 0
    assert b"\x1b" not in completed.stdout
    provenance = json.loads(completed.stdout)["provenance"]
    assert provenance["tool_version"] == "5.0.0\x1b[31m"
    assert provenance["selector"]["downstream_id"] == "pc\x1b[Adesign"


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


# GTX-201: the per-channel broken-pipe policy. Every case below needs a real subprocess with two
# separate descriptors, because all of them live between the command adapter and the interpreter:
# `CliRunner` and in-process monkeypatching of `app` reach neither typer's own consoles, nor
# typer's `_main` EPIPE branch, nor the shutdown flush that turns a dead stream into exit 120.
_RUN_MAIN = "from doc_lattice.cli import main; main()"

# The pre-renderer fallback: an escalated warning raised while importing the reporter itself, so
# the clause that handles it cannot use the reporter. Its stderr write is stdlib-only, and so is
# the neutralization that has to follow a failed one.
_SUPPORT_IMPORT_FAILURE = """
import builtins

real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "errors" and level == 1:
        raise DeprecationWarning("a dependency deprecated something at import time")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
from doc_lattice.cli import main
main()
"""


def _run_with_departed_reader(
    argv: list[str],
    cwd: Path,
    *,
    channel: str,
    code: str = _RUN_MAIN,
) -> tuple[int, str]:
    """Run one command with exactly one of its two readers closed before it writes.

    Closing one pipe while holding the other is what separates the two channels. A test that
    closed both, or that redirected stderr onto stdout, could not tell "the diagnostic stream
    died" from "the result stream died" -- which is the entire distinction under test.

    Args:
        argv: Command-line arguments after the interpreter program.
        cwd: Working directory for the child.
        channel: Which reader to close, ``"stdout"`` or ``"stderr"``.
        code: Program text for ``python -c``.

    Returns:
        The child's exit status and whatever the still-open reader received.
    """
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and static test program
        [sys.executable, "-c", code, *argv],
        cwd=cwd,
        env={**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(_SRC), "COLUMNS": "1000"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None  # guaranteed by stdout=subprocess.PIPE
    assert proc.stderr is not None  # guaranteed by stderr=subprocess.PIPE
    # Closed before the child produces any output, the way `head -1` closes once it has what it
    # wants: every later write from the child hits a dead pipe.
    if channel == "stdout":
        proc.stdout.close()
        _, survivor = proc.communicate(timeout=60)
    else:
        proc.stderr.close()
        survivor, _ = proc.communicate(timeout=60)
    return proc.returncode, survivor


def _duplicate_id_lattice(tmp_path: Path) -> None:
    """Write the shortest lattice whose load raises an adapter-caught ``ProjectError``."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: pipe-policy\n", encoding="utf-8")
    for name in ("first.md", "second.md"):
        (docs / name).write_text("---\nid: duplicated\n---\n# t\n", encoding="utf-8")


def _advisory_then_stdout_lattice(tmp_path: Path) -> None:
    """Write a lattice that warns on stderr and then does real stdout work that drifts."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: pipe-policy\n", encoding="utf-8")
    (docs / "no-id.md").write_text("---\ntitle: prose\n---\n# t\n", encoding="utf-8")
    (docs / "tracked.md").write_text(
        "---\nid: tracked\nderives_from:\n  - ref: ghost\n---\n# r\n", encoding="utf-8"
    )


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_a_dead_stderr_keeps_the_adapter_caught_tool_error_exit(tmp_path: Path):
    # Every command wraps orchestration in `exit_on_project_error`, which reports before it
    # raises `typer.Exit(2)`. A dead stderr used to raise out of that report first, so the entry
    # point took its broken-pipe branch and the run exited 141 -- reporting "something
    # downstream stopped reading" for a lattice that is genuinely broken.
    _duplicate_id_lattice(tmp_path)

    status, _ = _run_with_departed_reader(["check"], tmp_path, channel="stderr")

    assert status == EXIT_TOOL_ERROR


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_a_dead_stderr_leaves_an_advisory_run_on_its_ordinary_exit_code(tmp_path: Path):
    # AD-29's phase guard already contained the BrokenPipeError, but not the bytes the failed
    # write left buffered in sys.stderr: the interpreter's shutdown flush retried them, failed,
    # and replaced the run's own exit code with CPython's 120.
    _advisory_then_stdout_lattice(tmp_path)

    dead_stderr, surviving_stdout = _run_with_departed_reader(["check"], tmp_path, channel="stderr")
    dead_stdout, _ = _run_with_departed_reader(["check"], tmp_path, channel="stdout")

    assert dead_stderr == 1  # the drift the corpus actually has, not 120 and not 141
    assert "ghost" in surviving_stdout  # and the stdout work finished after the advisory was lost
    # The same corpus with the other reader closed, so the contrast is the channel and nothing
    # else: only the stream carrying the command's result decides 141.
    assert dead_stdout == 141


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_a_dead_stderr_does_not_truncate_the_stdout_report(tmp_path: Path):
    # The half PR #267 fixed, held here against the channel policy that replaced it: whatever a
    # succeeding command computes for stdout still arrives in full when only stderr died.
    _advisory_then_stdout_lattice(tmp_path)

    _, with_dead_stderr = _run_with_departed_reader(["check"], tmp_path, channel="stderr")
    complete = _run_bytes(["check"], tmp_path, {"COLUMNS": "1000"}).stdout.decode()

    assert with_dead_stderr == complete


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_help_rendered_into_a_departed_reader_exits_141(tmp_path: Path):
    # The original symptom, on the path that survived PR #267. Help is rendered by
    # typer.rich_utils' plain consoles, built outside `_create_runtime`, so `CliConsole` never
    # governed them and rich's default hook exited 1 -- the code check and lint reserve for
    # drift, reached here by a user simply piping `--help` into `head`.
    status, _ = _run_with_departed_reader(["--help"], tmp_path, channel="stdout")

    assert status == 141


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_a_usage_error_reported_to_a_dead_stderr_keeps_its_exit_code(tmp_path: Path):
    # The other half of typer's rendering. A usage error is a real tool error whose report
    # happens to be undeliverable, so it keeps exit 2 rather than becoming 120 at shutdown.
    argv = ["check", "--format", "json", "--indent", "-1"]

    status, _ = _run_with_departed_reader(argv, tmp_path, channel="stderr")

    assert status == EXIT_TOOL_ERROR


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_the_machine_readable_formats_exit_141_on_a_departed_reader(tmp_path: Path):
    # `--format json` writes through `CliRuntime.write_stdout`, the one output path that does
    # not go through rich. Its BrokenPipeError therefore arrives carrying errno EPIPE, which is
    # exactly what typer's `_main` converts into sys.exit(1) -- and the machine-readable formats
    # are the ones most likely to be piped into `head` or `jq` in the first place.
    _duplicate_id_lattice(tmp_path)
    (tmp_path / "docs" / "second.md").write_text("---\nid: second\n---\n# t\n", encoding="utf-8")

    status, _ = _run_with_departed_reader(["check", "--format", "json"], tmp_path, channel="stdout")

    assert status == 141


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
def test_the_support_import_fallback_keeps_its_exit_code_on_a_dead_stderr(tmp_path: Path):
    # The one path that cannot use the policy, because the policy is part of what failed to
    # import. It carries its own stdlib-only neutralization of file descriptor 2 for exactly
    # that reason; without it the explicit SystemExit(2) below became CPython's 120 at shutdown,
    # and an escalated warning read to a caller as an unrelated interpreter fault.
    status, _ = _run_with_departed_reader(
        [], tmp_path, channel="stderr", code=_SUPPORT_IMPORT_FAILURE
    )

    assert status == EXIT_TOOL_ERROR


@pytest.mark.skipif(os.name != "posix", reason="SIGPIPE/EPIPE semantics are POSIX-only")
@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["check"],
        ["check", "--format", "json"],
        ["check", "--format", "json", "--indent", "-1"],
    ],
    ids=["help", "human", "json", "usage-error"],
)
def test_no_command_exits_1_because_a_reader_departed(argv: list[str], tmp_path: Path):
    # The contract stated as one sweep rather than per path: 1 is the drift code, and a departed
    # reader is never drift. Both channels, so a repair that fixed one by breaking the other
    # cannot pass.
    _duplicate_id_lattice(tmp_path)
    (tmp_path / "docs" / "second.md").write_text("---\nid: second\n---\n# t\n", encoding="utf-8")

    for channel in ("stdout", "stderr"):
        status, _ = _run_with_departed_reader(argv, tmp_path, channel=channel)
        assert status != 1, f"{argv} exited 1 with a dead {channel}"
        assert status != 120, f"{argv} exited 120 with a dead {channel}"


def test_rich_still_offers_the_hook_the_policy_is_built_on():
    # `broken_pipe_policy` substitutes this documented method on rich's own Console so typer's
    # consoles inherit the policy without this project reaching into typer's internals. AD-27
    # now records rich's hook as a read compatibility surface for that reason, and this is the
    # assertion that turns its removal in a future rich into a failure here rather than a
    # silent return to exit 1 on `--help`.
    assert callable(getattr(Console, "on_broken_pipe", None))
