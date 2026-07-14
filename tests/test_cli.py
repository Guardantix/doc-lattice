"""Tests for the CLI."""

import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

import doc_lattice.cli as cli_mod
import doc_lattice.reconcile_transaction as transaction
from doc_lattice import __version__, persistence
from doc_lattice.cli import (
    _escape_github_message,
    _escape_github_property,
    app,
)
from doc_lattice.constants import RECONCILE_JOURNAL_NAME, RECONCILE_JOURNAL_VERSION
from doc_lattice.error_types import ConfigError, ReconcilePersistenceError
from doc_lattice.reconcile_transaction import Journal, JournalEntry, JournalState, reconcile_lock
from doc_lattice.tickets import Ticket, TicketState

runner = CliRunner()


def test_cli_imports_when_fcntl_is_unavailable():
    project_root = Path(__file__).resolve().parents[1]
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


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    """Capture every namespace entry without following symlinks or reading special files."""
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            entry = ("symlink", os.fsencode(path.readlink()))
        elif stat.S_ISREG(mode):
            entry = ("file", path.read_bytes())
        elif stat.S_ISDIR(mode):
            entry = ("directory", b"")
        else:
            entry = ("special", b"")
        snapshot[path.relative_to(root).as_posix()] = entry
    return snapshot


def _write_cli_transaction(
    root: Path,
    destination: Path,
    before_bytes: bytes,
    after_bytes: bytes,
    *,
    state: JournalState = "prepared",
) -> tuple[Path, Path, Path]:
    """Write a valid single-entry recovery transaction for CLI integration tests."""
    before = destination.with_name(f".{destination.name}.doc-lattice-before.test.tmp")
    after = destination.with_name(f".{destination.name}.doc-lattice-after.test.tmp")
    journal = root / RECONCILE_JOURNAL_NAME
    before.write_bytes(before_bytes)
    after.write_bytes(after_bytes)
    entry = JournalEntry(
        destination=destination.relative_to(root).as_posix(),
        before_path=before.relative_to(root).as_posix(),
        before_sha256=sha256(before_bytes).hexdigest(),
        after_path=after.relative_to(root).as_posix(),
        after_sha256=sha256(after_bytes).hexdigest(),
    )
    journal.write_text(
        Journal(
            version=RECONCILE_JOURNAL_VERSION,
            state=state,
            entries=(entry,),
        ).model_dump_json(),
        encoding="utf-8",
    )
    return journal, before, after


def _run(args: list[str], cwd: Path, env: dict[str, str]):
    """Invoke the CLI with cwd and env set for the duration of the call, then restore cwd."""
    old = Path.cwd()
    os.chdir(cwd)
    try:
        return runner.invoke(app, args, env=env)
    finally:
        os.chdir(old)


def test_escape_github_message_encodes_workflow_command_metacharacters():
    assert _escape_github_message("100%\rfirst\nsecond: a,b") == ("100%25%0Dfirst%0Asecond: a,b")


def test_escape_github_property_encodes_message_and_property_metacharacters():
    assert _escape_github_property("100%\rfirst\nsecond: a,b") == (
        "100%25%0Dfirst%0Asecond%3A a%2Cb"
    )


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_color_suppresses_forced_ansi(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    original_out = cli_mod._out
    original_err = cli_mod._err
    with monkeypatch.context() as patch:
        patch.delenv("NO_COLOR", raising=False)
        patch.setenv("FORCE_COLOR", "1")
        patch.setenv("TERM", "xterm-256color")
        patch.setattr(cli_mod, "_err", original_err)
        patch.setattr(
            cli_mod,
            "_out",
            Console(force_terminal=True, color_system="standard", no_color=False),
        )
        colored = runner.invoke(app, ["check"])
        assert colored.exit_code == 1
        assert "\x1b[" in colored.stdout

        patch.setattr(
            cli_mod,
            "_out",
            Console(force_terminal=True, color_system="standard", no_color=False),
        )
        plain = runner.invoke(app, ["--no-color", "check"])
        assert plain.exit_code == 1
        assert cli_mod._out.is_terminal
        assert cli_mod._out.color_system is not None
        assert "\x1b[" not in plain.stdout

    assert cli_mod._out is original_out
    assert cli_mod._err is original_err


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
        ["doc-lattice", "--no-color", "check", "--json", "--indent", "-1"],
    ],
)
def test_no_color_suppresses_typer_rendered_colors(argv):
    # These two invocations never reach _out/_err: --help and a --indent range failure
    # are rendered by typer's own rich_utils consoles before or outside main_callback.
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
        ["doc-lattice", "check", "--json", "--indent", "-1"],
    ],
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
    assert "requires --json" in output


def test_check_exits_1_on_drift(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1


def test_check_human_output_is_byte_identical(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check"])
    assert result.stdout == (
        "BROKEN        gdd -> ghost\n"
        "STALE         pc-design -> art-direction#accent\n"
        "UNRECONCILED  pc-design -> art-direction#motion\n"
    )


def test_check_github_emits_each_drift_annotation(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "github"])

    assert result.exit_code == 1
    gdd_path = "docs/gdd.md"
    pc_path = "docs/pc-design.md"
    assert result.stdout == (
        f"::error file={gdd_path},title=doc-lattice BROKEN::"
        "gdd -> ghost is BROKEN\n"
        f"::error file={pc_path},title=doc-lattice STALE::"
        "pc-design -> art-direction#accent is STALE\n"
        f"::error file={pc_path},title=doc-lattice UNRECONCILED::"
        "pc-design -> art-direction#motion is UNRECONCILED\n"
    )


def test_check_github_escapes_complete_annotation(tmp_path: Path, monkeypatch):
    # Metacharacters live in a subdirectory under docs (part of the repo-relative path)
    # so escaping of the emitted file= property is exercised; the project root is stripped.
    weird = tmp_path / "docs" / "sub%:,\nline"
    weird.mkdir(parents=True)
    (weird / "down.md").write_text(
        '---\nid: "down%:,\\r\\nline"\nderives_from:\n'
        '  - ref: "ghost%:,\\r\\nline"\n---\n# Down\nbody\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["check", "--format", "github"])

    assert result.exit_code == 1
    expected_path = _escape_github_property("docs/sub%:,\nline/down.md")
    assert result.stdout == (
        f"::error file={expected_path},"
        "title=doc-lattice BROKEN::"
        "down%25:,%0D%0Aline -> ghost%25:,%0D%0Aline is BROKEN\n"
    )


def test_check_github_annotation_keeps_config_subdir_prefix(tmp_path: Path, monkeypatch):
    # A --config pointing at a lattice in a subdirectory (a monorepo layout) must not
    # strip that subdirectory from the reported path: GitHub Actions checks out the repo
    # at the invocation cwd, so the annotation needs the full cwd-relative path to land
    # on the right file in the pull request diff.
    project = tmp_path / "packages" / "game"
    docs = project / "docs"
    docs.mkdir(parents=True)
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: ghost\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    (project / ".doc-lattice.yml").write_text("docs_roots:\n  - docs\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, ["check", "--config", "packages/game/.doc-lattice.yml", "--format", "github"]
    )

    assert result.exit_code == 1
    assert result.stdout == (
        "::error file=packages/game/docs/down.md,title=doc-lattice BROKEN::"
        "down -> ghost is BROKEN\n"
    )


def test_check_github_suppresses_ok_edges(tmp_path: Path, monkeypatch):
    _clean_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "down"]).exit_code == 0

    result = runner.invoke(app, ["check", "--format", "github"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_check_format_json_matches_json_alias(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    alias = runner.invoke(app, ["check", "--json"])
    explicit = runner.invoke(app, ["check", "--format", "json"])

    assert explicit.exit_code == alias.exit_code == 1
    assert explicit.stdout == alias.stdout


@pytest.mark.parametrize("command", ["check", "lint"])
def test_report_commands_reject_json_github_conflict(lattice_dir: Path, monkeypatch, command: str):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, [command, "--json", "--format", "github"])

    assert result.exit_code == 2
    assert "--json" in result.stderr
    assert "--format github" in result.stderr


@pytest.mark.parametrize("command", ["check", "lint"])
def test_report_commands_reject_unknown_format(lattice_dir: Path, monkeypatch, command: str):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, [command, "--format", "nonsense"])

    assert result.exit_code == 2
    assert "nonsense" in result.stderr
    assert "human" in result.stderr
    assert "json" in result.stderr
    assert "github" in result.stderr


@pytest.mark.parametrize("command", ["check", "lint"])
def test_report_commands_reject_unknown_format_even_with_json(
    lattice_dir: Path, monkeypatch, command: str
):
    # --json must not mask a typoed --format; the bad format still fails loudly.
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, [command, "--json", "--format", "githu"])

    assert result.exit_code == 2
    assert "githu" in result.stderr


def test_check_json_reports_states(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json"])
    payload = json.loads(result.stdout)
    states = {(e["source_id"], e["target_ref"]): e["state"] for e in payload["edges"]}
    assert states[("gdd", "ghost")] == "BROKEN"


def test_check_json_reports_all_states(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json"])
    payload = json.loads(result.stdout)
    states = {(e["source_id"], e["target_ref"]): e for e in payload["edges"]}
    assert states[("gdd", "ghost")]["state"] == "BROKEN"
    assert states[("pc-design", "art-direction#accent")]["state"] == "STALE"
    assert states[("pc-design", "art-direction#motion")]["state"] == "UNRECONCILED"
    stale = states[("pc-design", "art-direction#accent")]
    assert stale["target_id"] == "art-direction#accent"
    assert stale["expected"] != stale["actual"]


def test_check_json_indent_round_trips_to_compact_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, ["check", "--json"])
    pretty = runner.invoke(app, ["check", "--json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == 1
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert '\n  "edges": [\n' in pretty.stdout


def test_check_json_zero_indent_round_trips_to_compact_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, ["check", "--json"])
    zero_indent = runner.invoke(app, ["check", "--json", "--indent", "0"])
    assert compact.exit_code == zero_indent.exit_code == 1
    assert json.loads(zero_indent.stdout) == json.loads(compact.stdout)
    assert '\n"edges": [\n' in zero_indent.stdout


def test_check_format_json_accepts_indent(lattice_dir: Path, monkeypatch):
    # --format json is the documented equivalent of --json, so --indent must be honored with it.
    monkeypatch.chdir(lattice_dir)
    via_flag = runner.invoke(app, ["check", "--json", "--indent", "2"])
    via_format = runner.invoke(app, ["check", "--format", "json", "--indent", "2"])
    assert via_flag.exit_code == via_format.exit_code == 1
    assert via_format.stdout == via_flag.stdout
    assert '\n  "edges": [\n' in via_format.stdout


def test_lint_format_json_accepts_indent(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    via_flag = runner.invoke(app, ["lint", "--json", "--indent", "2"])
    via_format = runner.invoke(app, ["lint", "--format", "json", "--indent", "2"])
    assert via_flag.exit_code == via_format.exit_code
    assert via_format.stdout == via_flag.stdout
    assert json.loads(via_format.stdout) == json.loads(via_flag.stdout)


def test_check_indent_without_json_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--indent", "2"])
    assert result.exit_code == 2
    assert "--indent requires --json" in result.stderr


def test_check_indent_validation_precedes_project_loading(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", "--config", "missing.yml", "--indent", "0"])
    assert result.exit_code == 2
    assert "--indent requires --json" in result.stderr
    assert "config file not found" not in result.stderr


def test_check_negative_indent_is_rejected(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json", "--indent", "-1"])
    assert result.exit_code == 2


def test_check_only_filters_human_output(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--only", "STALE"])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines
    assert all("STALE" in line for line in lines)


def test_check_only_filters_json_output(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json", "--only", "STALE"])
    payload = json.loads(result.stdout)
    assert payload["edges"]
    assert all(edge["state"] == "STALE" for edge in payload["edges"])


def test_check_only_is_case_insensitive(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--only", "stale"])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines
    assert all("STALE" in line for line in lines)


def test_check_only_unknown_state_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--only", "BOGUS"])
    assert result.exit_code == 2
    assert "BOGUS" in result.stderr
    assert "OK" in result.stderr
    assert "STALE" in result.stderr


def test_check_only_unknown_state_with_markup_does_not_crash(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--only", "BOGUS[/]"])
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "BOGUS[/]" in result.stderr


def test_check_only_ok_still_exits_1_on_drift(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--only", "OK"])
    assert result.exit_code == 1
    assert not result.stdout.strip()


def test_check_only_repeated_flags_combine(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json", "--only", "STALE", "--only", "BROKEN"])
    payload = json.loads(result.stdout)
    states = {edge["state"] for edge in payload["edges"]}
    assert states == {"STALE", "BROKEN"}


def test_check_without_only_shows_all_states(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json"])
    payload = json.loads(result.stdout)
    states = {edge["state"] for edge in payload["edges"]}
    assert states == {"STALE", "UNRECONCILED", "BROKEN"}


def test_check_exits_2_on_bad_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 2


def test_check_error_handler_escapes_markup_in_message(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # The not-found message embeds the config path; bracketed metacharacters in it
    # must be escaped before the error handler prints through rich markup, or it
    # raises MarkupError and exits 1 (drift) instead of the tool-error code 2.
    result = runner.invoke(app, ["check", "--config", "missing[/].yml"])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)


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


def test_impact_lists_dependents(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "art-direction#accent", "--json"])
    payload = json.loads(result.stdout)
    assert "pc-design" in {n["id"] for n in payload["affected"]}


@pytest.mark.parametrize(
    "args",
    [
        ["lint"],
        ["impact", "art-direction#accent"],
        ["linear"],
    ],
)
def test_indent_without_json_exits_2_before_project_loading(tmp_path: Path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [*args, "--indent", "2"])
    assert result.exit_code == 2
    assert "--indent requires --json" in result.stderr


@pytest.mark.parametrize(
    ("args", "expected_exit"),
    [
        (["lint"], 0),
        (["impact", "art-direction#accent"], 0),
    ],
)
def test_offline_json_indent_round_trips(lattice_dir: Path, monkeypatch, args, expected_exit):
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, [*args, "--json"])
    pretty = runner.invoke(app, [*args, "--json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == expected_exit
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert "\n  " in pretty.stdout


def _chain_docs(tmp_path: Path) -> Path:
    # a <- b <- c: c derives from b, b derives from a.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A {#a}\nx\n", encoding="utf-8")
    (docs / "b.md").write_text(
        "---\nid: b\nderives_from:\n  - ref: a\n---\n# B {#b}\nx\n", encoding="utf-8"
    )
    (docs / "c.md").write_text(
        "---\nid: c\nderives_from:\n  - ref: b\n---\n# C {#c}\nx\n", encoding="utf-8"
    )
    return tmp_path


def test_impact_json_includes_depth(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "art-direction#accent", "--json"])
    payload = json.loads(result.stdout)
    entry = next(n for n in payload["affected"] if n["id"] == "pc-design")
    assert entry["depth"] == 1


def test_impact_depth_flag_bounds_the_walk(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(_chain_docs(tmp_path))
    result = runner.invoke(app, ["impact", "a", "--json", "--depth", "1"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [(n["id"], n["depth"]) for n in payload["affected"]] == [("b", 1)]


def test_impact_depth_2_reaches_second_hop(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(_chain_docs(tmp_path))
    result = runner.invoke(app, ["impact", "a", "--json", "--depth", "2"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [(n["id"], n["depth"]) for n in payload["affected"]] == [("b", 1), ("c", 2)]


def test_impact_depth_zero_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(_chain_docs(tmp_path))
    result = runner.invoke(app, ["impact", "a", "--depth", "0"])
    assert result.exit_code == 2


def test_impact_human_output_lists_tickets(lattice_dir: Path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # absolute path makes the line long; stop rich wrapping it
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "art-direction#accent"])
    assert result.exit_code == 0
    assert "pc-design" in result.stdout
    assert "tickets: PC-228" in result.stdout


def test_impact_human_output_dash_when_no_tickets(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#s}\nb\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#s\n---\n# Down\nb\n", encoding="utf-8"
    )
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["impact", "up"])
    assert result.exit_code == 0
    assert "tickets: -" in result.stdout


def test_graph_emits_mermaid(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert result.stdout.startswith("graph TD")


def test_graph_exits_2_on_bad_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 2


def test_reconcile_unknown_id_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "does-not-exist"])
    assert result.exit_code == 2


def test_reconcile_then_check_clean(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "pc-design"]).exit_code == 0
    after = runner.invoke(app, ["check"])
    # gdd's BROKEN ref still drifts, so check is still 1; pc-design itself is clean.
    pc_check = runner.invoke(app, ["check", "--json"])
    payload = json.loads(pc_check.stdout)
    pc_states = [e["state"] for e in payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]
    assert after.exit_code == 1


def test_reconcile_writes_through_in_project_symlink(tmp_path: Path, monkeypatch):
    project_root = tmp_path / "repo"
    docs = project_root / "docs"
    shared = project_root / "shared"
    docs.mkdir(parents=True)
    shared.mkdir()
    (project_root / ".doc-lattice.yml").write_text('docs_roots: ["docs"]\n', encoding="utf-8")
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nupstream\n", encoding="utf-8")
    target = shared / "down.md"
    target.write_text(
        "---\nid: down\nderives_from:\n  - ref: up#sec\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    link = docs / "down.md"
    link.symlink_to(Path("../shared/down.md"))
    before = target.read_text(encoding="utf-8")
    monkeypatch.chdir(project_root)

    result = runner.invoke(app, ["reconcile", "down"])

    assert result.exit_code == 0
    assert link.is_symlink()
    rewritten = target.read_text(encoding="utf-8")
    assert rewritten != before
    assert "seen:" in rewritten
    assert link.read_text(encoding="utf-8") == rewritten


def test_reconcile_all_without_positional_id(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all"])
    assert result.exit_code == 0
    payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    pc_states = [e["state"] for e in payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_all_skips_broken_edge(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "--all"]).exit_code == 0
    payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    states = {(e["source_id"], e["target_ref"]): e["state"] for e in payload["edges"]}
    assert states[("gdd", "ghost")] == "BROKEN"
    assert runner.invoke(app, ["check"]).exit_code == 1


def test_reconcile_requires_id_or_all(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 2


def test_reconcile_recover_without_journal_reports_none_human(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "nothing to recover" in result.stdout
    assert str(lattice_dir / RECONCILE_JOURNAL_NAME) in result.stdout


def test_reconcile_recover_without_journal_reports_exact_json(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--recover", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "action": "none",
        "journal": str(lattice_dir / RECONCILE_JOURNAL_NAME),
    }


def test_reconcile_recover_rolls_back_prepared_without_planning(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    destination = docs / "down.md"
    before_bytes = b"original document\n"
    after_bytes = b"transaction document\n"
    destination.write_bytes(after_bytes)
    journal, before, after = _write_cli_transaction(
        tmp_path, destination, before_bytes, after_bytes
    )
    monkeypatch.chdir(tmp_path)

    def fail_if_loaded(*_args, **_kwargs):
        pytest.fail("recovery-only mode loaded or planned a lattice")

    monkeypatch.setattr(cli_mod, "load_lattice", fail_if_loaded)
    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 0
    assert "rolled back reconcile transaction" in result.stdout
    assert str(journal) in result.stdout
    assert destination.read_bytes() == before_bytes
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


def test_reconcile_recover_cleans_committed_without_planning(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    destination = docs / "down.md"
    before_bytes = b"original document\n"
    after_bytes = b"committed document\n"
    destination.write_bytes(after_bytes)
    journal, before, after = _write_cli_transaction(
        tmp_path,
        destination,
        before_bytes,
        after_bytes,
        state="committed",
    )
    monkeypatch.chdir(tmp_path)

    def fail_if_loaded(*_args, **_kwargs):
        pytest.fail("recovery-only mode loaded or planned a lattice")

    monkeypatch.setattr(cli_mod, "load_lattice", fail_if_loaded)
    result = runner.invoke(app, ["reconcile", "--recover", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "action": "cleaned_committed",
        "journal": str(journal),
    }
    assert destination.read_bytes() == after_bytes
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["downstream", "--recover"],
        ["--recover", "--all"],
        ["--recover", "--ref", "upstream"],
        ["--recover", "--dry-run"],
    ],
)
def test_reconcile_recover_rejects_selection_and_dry_run_flags(
    tmp_path: Path, monkeypatch, args: list[str]
):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["reconcile", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--recover cannot be combined" in result.stderr


def test_reconcile_dry_run_refuses_journal_without_mutating_or_loading(
    lattice_dir: Path, monkeypatch
):
    journal = lattice_dir / RECONCILE_JOURNAL_NAME
    journal.write_bytes(b"sentinel journal bytes\n")
    before = _tree_snapshot(lattice_dir)
    monkeypatch.chdir(lattice_dir)

    def fail_if_loaded(*_args, **_kwargs):
        pytest.fail("dry-run loaded the lattice before refusing recovery")

    monkeypatch.setattr(cli_mod, "load_lattice", fail_if_loaded)
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert str(journal) in result.stderr
    assert "--recover" in result.stderr
    assert _tree_snapshot(lattice_dir) == before


@pytest.mark.parametrize(
    "args",
    [
        ["reconcile", "--recover", "--json"],
        ["reconcile", "--all"],
        ["reconcile", "--all", "--dry-run"],
    ],
)
def test_reconcile_dangling_journal_symlink_never_reports_success_or_mutates_empty_project(
    tmp_path: Path, monkeypatch, args: list[str]
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "node.md").write_text("---\nid: node\n---\n# Node\nbody\n", encoding="utf-8")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal.symlink_to("missing-journal-target")
    before = _tree_snapshot(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert str(journal) in result.stderr
    assert "symlink" in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert _tree_snapshot(tmp_path) == before


def test_reconcile_dangling_journal_symlink_blocks_nonempty_real_plan(
    lattice_dir: Path, monkeypatch
):
    journal = lattice_dir / RECONCILE_JOURNAL_NAME
    journal.symlink_to("missing-journal-target")
    before = _tree_snapshot(lattice_dir)
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "--all"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "symlink" in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert _tree_snapshot(lattice_dir) == before


def test_reconcile_dry_run_does_not_mutate_external_load_cache(
    lattice_dir: Path, tmp_path: Path, monkeypatch
):
    cache_home = tmp_path / "xdg"
    cache_file = cache_home / "doc-lattice" / "dry-run-proof" / "load-cache.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"existing cache sentinel\n")
    (lattice_dir / ".doc-lattice.yml").write_text(
        "cache_key: dry-run-proof\ncache_trust_stat: true\n",
        encoding="utf-8",
    )
    project_before = _tree_snapshot(lattice_dir)
    cache_before = _tree_snapshot(cache_home)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])

    assert result.exit_code == 0
    assert _tree_snapshot(lattice_dir) == project_before
    assert _tree_snapshot(cache_home) == cache_before


def test_reconcile_lock_contention_does_not_inspect_or_mutate_journal(
    lattice_dir: Path, monkeypatch
):
    journal = lattice_dir / RECONCILE_JOURNAL_NAME
    journal.write_bytes(b"not even valid journal json\n")
    before = _tree_snapshot(lattice_dir)
    monkeypatch.chdir(lattice_dir)

    with reconcile_lock(lattice_dir):
        result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "another reconcile is in progress" in result.stderr
    assert "invalid reconcile journal" not in result.stderr
    assert _tree_snapshot(lattice_dir) == before


@pytest.mark.parametrize("failure", ["open", "flock", "fstat"])
def test_reconcile_lock_setup_failure_is_typed_without_internal_error_or_mutation(
    lattice_dir: Path, monkeypatch, failure: str
):
    before = _tree_snapshot(lattice_dir)
    real_open = transaction.os.open
    real_flock = transaction._flock

    if failure == "open":

        def fail_open(path: Path, flags: int) -> int:
            if Path(path) == lattice_dir:
                raise PermissionError("injected open failure")
            return real_open(path, flags)

        monkeypatch.setattr(transaction.os, "open", fail_open)
    elif failure == "flock":

        def fail_flock(fd: int, *, release: bool) -> None:
            if not release:
                raise OSError("injected flock failure")
            real_flock(fd, release=release)

        monkeypatch.setattr(transaction, "_flock", fail_flock)
    else:
        monkeypatch.setattr(
            transaction.os,
            "fstat",
            lambda _fd: (_ for _ in ()).throw(OSError("injected fstat failure")),
        )
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "--recover", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert f"injected {failure} failure" in result.stderr
    assert "internal error" not in result.stderr
    assert "Traceback" not in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _tree_snapshot(lattice_dir) == before


def test_reconcile_real_run_recovers_before_loading_and_plans_recovered_bytes(
    lattice_dir: Path, monkeypatch
):
    destination = lattice_dir / "docs" / "pc-design.md"
    before_bytes = destination.read_bytes()
    after_bytes = b"not a valid lattice document\n"
    destination.write_bytes(after_bytes)
    journal, before, after = _write_cli_transaction(
        lattice_dir, destination, before_bytes, after_bytes
    )
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "pc-design"])

    assert result.exit_code == 0
    assert "reconciled pc-design.md" in result.stdout
    assert "recovered reconcile transaction: rolled_back" in result.stderr
    assert b"seen:" in destination.read_bytes()
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


@pytest.mark.parametrize("json_out", [False, True])
def test_reconcile_concurrent_edit_is_preserved_without_success_report(
    lattice_dir: Path, monkeypatch, json_out: bool
):
    monkeypatch.chdir(lattice_dir)
    real_commit = transaction.commit_rewrites
    editor_bytes = b"editor-owned concurrent bytes\n"
    edited_path: Path | None = None

    def edit_then_commit(project_root, rewrites, write_paths, *, lock):
        nonlocal edited_path
        edited_path = next(iter(write_paths.values()))
        edited_path.write_bytes(editor_bytes)
        return real_commit(project_root, rewrites, write_paths, lock=lock)

    monkeypatch.setattr(cli_mod, "commit_rewrites", edit_then_commit, raising=False)
    args = ["reconcile", "pc-design"]
    if json_out:
        args.append("--json")
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert edited_path is not None
    assert str(edited_path) in result.stderr
    assert "changed after validation" in result.stderr
    assert "RECONCILE_CONFLICT" in result.stderr
    assert edited_path.read_bytes() == editor_bytes


def _two_downstream_project(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#s}\nupstream body\n", encoding="utf-8")
    for name in ("down-a", "down-b"):
        (docs / f"{name}.md").write_text(
            f"---\nid: {name}\nderives_from:\n  - ref: up#s\n---\n# {name}\nbody\n",
            encoding="utf-8",
        )
    return tmp_path


@pytest.mark.parametrize(
    ("failure", "message"),
    [("replace", "disk full"), ("fsync", "directory fsync failed")],
)
def test_reconcile_midbatch_persistence_failure_rolls_back_without_success(
    tmp_path: Path, monkeypatch, failure: str, message: str
):
    project = _two_downstream_project(tmp_path)
    before = _tree_snapshot(project)
    monkeypatch.chdir(project)
    real_replace = transaction.replace_staged
    after_replaces = 0

    def fail_second_after(staged: Path, destination: Path) -> None:
        nonlocal after_replaces
        if "doc-lattice-after" in staged.name:
            after_replaces += 1
            if after_replaces == 2:
                if failure == "fsync":
                    staged.replace(destination)
                raise OSError(message)
        real_replace(staged, destination)

    monkeypatch.setattr(transaction, "replace_staged", fail_second_after)
    result = runner.invoke(app, ["reconcile", "--all"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert message in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert _tree_snapshot(project) == before


def test_reconcile_success_cleans_transaction_artifacts(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["reconciled"]
    assert not (lattice_dir / RECONCILE_JOURNAL_NAME).exists()
    assert not list(lattice_dir.rglob(".*.doc-lattice-before.*.tmp"))
    assert not list(lattice_dir.rglob(".*.doc-lattice-after.*.tmp"))
    assert not list(lattice_dir.glob(f"{RECONCILE_JOURNAL_NAME}.*.tmp"))


@pytest.mark.parametrize("mode", ["recover", "reconcile"])
def test_reconcile_lock_exit_failure_publishes_no_success(
    lattice_dir: Path, monkeypatch, mode: str
):
    real_lock = cli_mod.reconcile_lock

    @contextmanager
    def fail_after_lock_body(project_root: Path):
        with real_lock(project_root) as lock:
            yield lock
        raise ReconcilePersistenceError("injected reconcile lock release failure")

    monkeypatch.setattr(cli_mod, "reconcile_lock", fail_after_lock_body)
    monkeypatch.chdir(lattice_dir)
    args = ["reconcile", "--recover", "--json"]
    if mode == "reconcile":
        args = ["reconcile", "--all", "--json"]

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "injected reconcile lock release failure" in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr


def test_reconcile_write_error_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(transaction, "stage_bytes", boom)
    result = runner.invoke(app, ["reconcile", "pc-design"])
    assert result.exit_code == 2


def test_reconcile_real_run_reports_reconciled_lines(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all"])
    assert result.exit_code == 0
    assert "reconciled pc-design.md: art-direction#accent" in result.stdout
    assert "reconciled pc-design.md: art-direction#motion" in result.stdout


def test_reconcile_dry_run_leaves_files_unchanged(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    docs = lattice_dir / "docs"
    before = {p: p.read_text(encoding="utf-8") for p in docs.glob("*.md")}
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])
    assert result.exit_code == 0
    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text


def test_reconcile_dry_run_lists_stale_and_unreconciled_edges(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])
    assert result.exit_code == 0
    assert "would reconcile pc-design.md: art-direction#accent" in result.stdout
    assert "would reconcile pc-design.md: art-direction#motion" in result.stdout
    # gdd's ghost ref is BROKEN, which --all skips, so gdd never appears.
    assert "gdd" not in result.stdout
    assert "reconciled pc-design" not in result.stdout


def test_reconcile_dry_run_single_node_selection(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(app, ["reconcile", "pc-design", "--dry-run"])
    assert result.exit_code == 0
    assert "would reconcile pc-design.md: art-direction#accent" in result.stdout
    assert "would reconcile pc-design.md: art-direction#motion" in result.stdout
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_dry_run_composes_with_ref(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(
        app, ["reconcile", "pc-design", "--ref", "art-direction#accent", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "would reconcile pc-design.md: art-direction#accent" in result.stdout
    assert "art-direction#motion" not in result.stdout
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_dry_run_json_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1  # single-line JSON
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    entries = payload["reconciled"]
    assert entries == sorted(entries, key=lambda e: (e["path"], e["ref"]))
    stripped = {(Path(e["path"]).name, e["ref"]) for e in entries}
    assert stripped == {
        ("pc-design.md", "art-direction#accent"),
        ("pc-design.md", "art-direction#motion"),
    }
    for entry in entries:
        assert len(entry["new_seen"]) == 32
        int(entry["new_seen"], 16)  # must be hex


def test_reconcile_dry_run_json_leaves_files_unchanged(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_real_run_json_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    stripped = {(Path(e["path"]).name, e["ref"]) for e in payload["reconciled"]}
    assert stripped == {
        ("pc-design.md", "art-direction#accent"),
        ("pc-design.md", "art-direction#motion"),
    }
    # the real run actually wrote: check now reports both edges OK.
    check_payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    pc_states = [e["state"] for e in check_payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_dry_run_after_clean_reports_nothing_to_reconcile(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "--all"]).exit_code == 0  # real run clears drift
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])
    assert result.exit_code == 0
    assert "nothing to reconcile" in result.stdout


def test_reconcile_json_after_clean_reports_empty_list(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "--all"]).exit_code == 0  # real run clears drift
    result = runner.invoke(app, ["reconcile", "--all", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"dry_run": False, "reconciled": []}


def test_impact_unknown_token_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "nonexistent"])
    assert result.exit_code == 2


def test_check_human_output_escapes_markup(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: 'up[/]'\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check"])
    # A bracketed ref must render literally, not crash rich markup parsing.
    assert "BROKEN" in result.stdout
    assert "up[/]" in result.stdout


def test_graph_dot_retains_bracketed_attributes(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["graph", "--format", "dot"])
    assert result.exit_code == 0
    assert result.stdout.startswith("digraph lattice")
    assert "[label=" in result.stdout  # rich markup must not strip DOT attributes


def test_graph_emits_json(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["graph", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {node["id"] for node in payload["nodes"]} == {"art-direction", "pc-design", "gdd"}
    # gdd's broken 'ghost' ref contributes no edge; the two art-direction sections pc-design
    # derives from collapse to one edge, same as the mermaid/dot renderers.
    assert payload["edges"] == [
        {"upstream": "art-direction", "downstream": "pc-design", "stale": True}
    ]


def test_graph_json_edge_set_matches_mermaid(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    mermaid = runner.invoke(app, ["graph"]).stdout
    mermaid_edges = {
        tuple(line.strip().split(" -.-> " if "-.->" in line else " --> "))
        for line in mermaid.splitlines()
        if "->" in line
    }
    payload = json.loads(runner.invoke(app, ["graph", "--format", "json"]).stdout)
    # Mermaid assigns collision-free ids from the same sorted node order as JSON; translate
    # JSON's raw ids before comparing so this checks semantic edge-set agreement.
    mermaid_id = {node["id"]: f"n{index}" for index, node in enumerate(payload["nodes"])}
    json_edges = {
        (mermaid_id[e["upstream"]], mermaid_id[e["downstream"]]) for e in payload["edges"]
    }
    assert json_edges == mermaid_edges


def test_graph_rejects_unknown_format(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["graph", "--format", "dott"])
    assert result.exit_code == 2
    assert "mermaid" in result.stderr
    assert "dot" in result.stderr
    assert "json" in result.stderr


def _clean_docs(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nsec body\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#sec\n---\n# Down\nbody\n",
        encoding="utf-8",
    )


def test_check_exits_0_when_fully_reconciled(tmp_path: Path, monkeypatch):
    _clean_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "down"]).exit_code == 0
    # No broken refs and every edge reconciled, so check reports clean.
    assert runner.invoke(app, ["check"]).exit_code == 0


def test_reconcile_ref_typo_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "pc-design", "--ref", "accnt"])
    assert result.exit_code == 2


def test_reconcile_ref_selects_single_edge(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "pc-design", "--ref", "art-direction#accent"])
    assert result.exit_code == 0
    payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    edges = [e for e in payload["edges"] if e["source_id"] == "pc-design"]
    states = {e["target_ref"]: e["state"] for e in edges}
    assert states["art-direction#accent"] == "OK"
    assert states["art-direction#motion"] == "UNRECONCILED"


def test_reconcile_noop_reports_nothing_to_reconcile(tmp_path: Path, monkeypatch):
    _clean_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["reconcile", "down"])  # first run clears the UNRECONCILED edge
    result = runner.invoke(app, ["reconcile", "down"])  # nothing left to do
    assert result.exit_code == 0
    assert "nothing to reconcile" in result.stdout


@pytest.mark.parametrize(
    "exc", [OSError("io"), RuntimeError("loop"), ValueError("bad"), ConfigError("cfg")]
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


def test_main_passes_systemexit_through_unchanged(monkeypatch):
    def boom():
        raise SystemExit(1)  # typer's own exit must not be remapped to 2

    monkeypatch.setattr(cli_mod, "app", boom)
    with pytest.raises(SystemExit) as info:
        cli_mod.main()
    assert info.value.code == 1


def test_main_sets_no_color_env_before_app_runs(monkeypatch):
    # typer/click build their own rich_utils consoles (help text, parameter-validation
    # errors) from scratch on demand; those are untouched by _disable_color() and only
    # honor NO_COLOR if it is already set in the environment before app() parses argv.
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


def _fake_fetch(tickets):
    def fetch(_identifiers, _team, _client=None):
        return tickets, {}

    return fetch


def _ticket(state: TicketState) -> Ticket:
    return Ticket(
        identifier="PC-228",
        title="t",
        url="https://x/PC-228",
        state=state,
        parent=None,
        children=(),
    )


def test_linear_audit_json_reports_danger(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    danger = [f for f in payload["findings"] if f["severity"] == "DANGER"]
    assert danger
    assert danger[0]["ticket_ref"] == "PC-228"


def test_linear_json_indent_round_trips(lattice_dir: Path, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, ["linear", "--json"])
    pretty = runner.invoke(app, ["linear", "--json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == 0
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert '\n  "findings": [\n' in pretty.stdout


def test_linear_positional_target_scopes_audit(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "pc-design", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    danger = [f for f in payload["findings"] if f["severity"] == "DANGER"]
    assert any(f["ticket_ref"] == "PC-228" and f["node_id"] == "pc-design" for f in danger)


def test_linear_from_grades_downstream(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--from", "art-direction#accent", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert any(f["ticket_ref"] == "PC-228" for f in payload["findings"])


def test_linear_exit_code_gates_on_danger(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["linear"]).exit_code == 0
    assert runner.invoke(app, ["linear", "--exit-code"]).exit_code == 1


def test_linear_warn_exit_gates_on_warning(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="In Progress", type="started"))
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["linear", "--exit-code"]).exit_code == 0
    assert runner.invoke(app, ["linear", "--exit-code", "--warn-exit"]).exit_code == 1


def test_linear_blocked_ticket_fails_gate(lattice_dir, monkeypatch):
    # The completed ticket is replaced by a typo: gate must still fail (fail-closed).
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--exit-code", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["findings"][0]["severity"] == "BLOCKED"


def test_linear_no_tickets_needs_no_key(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A {#s}\nb\n", encoding="utf-8")
    (docs / "b.md").write_text(
        "---\nid: b\nderives_from:\n  - ref: a#s\n    seen: staleseenstaleseenstaleseenstale\n"
        "---\n# B\nb\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["linear", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []


def test_linear_from_and_target_conflict_exits_2(lattice_dir, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "accent", "--from", "accent"])
    assert result.exit_code == 2


def test_linear_unknown_from_exits_2(lattice_dir, monkeypatch):
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--from", "nonexistent"])
    assert result.exit_code == 2


def test_init_delegates_create_only_write_to_shared_persistence(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, bytes, str]] = []

    def capture(path: Path, data: bytes, *, prefix: str) -> None:
        calls.append((path, data, prefix))

    monkeypatch.setattr(cli_mod, "atomic_create_bytes", capture, raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert len(calls) == 1
    target, data, prefix = calls[0]
    assert target == tmp_path / ".doc-lattice.yml"
    assert data.startswith(b"# doc-lattice configuration")
    assert prefix == ".doc-lattice.yml."


def test_init_writes_config_and_prints_codegen(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    config = (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8")
    assert "docs_roots:" in config
    assert "- docs" in config
    assert ".pre-commit-config.yaml" in result.stdout
    assert ".github/workflows/doc-lattice.yml" in result.stdout
    assert f"--from doc-lattice=={__version__}" in result.stdout
    assert "git+" not in result.stdout
    narration = " ".join(result.stderr.split())
    assert f"exact pinned version {__version__} is published on PyPI" in narration
    assert "tag is pushed" not in narration


def test_init_prints_gitignore_guidance_before_other_snippets_and_preserves_existing_file(
    tmp_path: Path, monkeypatch
):
    gitignore = tmp_path / ".gitignore"
    original = b"existing bytes\r\n*.local\n"
    gitignore.write_bytes(original)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    expected = (
        "# ===== .gitignore (append these lines) =====\n"
        ".doc-lattice-reconcile.json\n"
        ".doc-lattice-reconcile.json.*.tmp\n"
        ".*.doc-lattice-before.*.tmp\n"
        ".*.doc-lattice-after.*.tmp\n"
    )
    assert expected in result.stdout
    assert result.stdout.index(expected) < result.stdout.index("# ===== .pre-commit-config.yaml")
    assert gitignore.read_bytes() == original
    assert "Append the .gitignore block" in result.stderr


def test_init_prints_gitignore_guidance_without_creating_gitignore(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert ".doc-lattice-reconcile.json.*.tmp" in result.stdout
    assert not (tmp_path / ".gitignore").exists()


def test_init_skips_existing_config_but_still_prints(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text("SENTINEL\n", encoding="utf-8")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == "SENTINEL\n"
    assert ".github/workflows/doc-lattice.yml" in result.stdout


def test_init_existing_config_with_stage_cleanup_failure_exits_2_and_names_orphan(
    tmp_path: Path, monkeypatch
):
    config = tmp_path / ".doc-lattice.yml"
    config.write_bytes(b"existing config bytes\n")
    cleanup_attempts: list[Path] = []

    def fail_cleanup(staged: Path) -> None:
        cleanup_attempts.append(staged)
        raise OSError("cleanup blocked")

    monkeypatch.setattr(persistence, "durable_unlink", fail_cleanup)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert config.read_bytes() == b"existing config bytes\n"
    assert len(cleanup_attempts) == 1
    orphan = cleanup_attempts[0]
    assert orphan.exists()
    expected_note = (
        f"durable cleanup failed for helper-owned stage {orphan}: cleanup blocked; "
        "it is not governed by a recovery journal, so inspect and remove it manually when safe"
    )
    assert expected_note in result.stderr
    assert "CONFIG_ERROR" in result.stderr


def test_init_other_persistence_error_flattens_exception_notes(tmp_path: Path, monkeypatch):
    error = OSError("publication failed")
    error.add_note("exact orphan remediation note")

    def fail_create(*_args, **_kwargs) -> None:
        raise error

    monkeypatch.setattr(cli_mod, "atomic_create_bytes", fail_create)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot write .doc-lattice.yml: publication failed" in result.stderr
    assert "exact orphan remediation note" in result.stderr
    assert "CONFIG_ERROR" in result.stderr


def test_init_bakes_flag_values(tmp_path: Path, monkeypatch):
    from doc_lattice.config import load_config  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["init", "--docs-root", "design", "--docs-root", "lore", "--linear-team", "PC"]
    )
    assert result.exit_code == 0
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["design", "lore"]
    assert project.config.linear_team == "PC"


@pytest.mark.parametrize("bad", ["/etc", "../escape"])
def test_init_rejects_unsafe_docs_root(tmp_path: Path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", bad])
    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()


def test_init_rejects_control_character_in_flag(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--linear-team", "a\nb"])
    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()


def test_init_rejects_invalid_linear_team(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A lowercase, hyphenated value is not a valid Linear team key, so init must
    # refuse it rather than scaffold a config that the linear command rejects.
    result = runner.invoke(app, ["init", "--linear-team", "my-team-slug"])
    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()


def test_init_rejects_markup_metachar_in_docs_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "../[/]"])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not (tmp_path / ".doc-lattice.yml").exists()


def test_init_crash_during_link_leaves_clean_state(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_link = os.link

    def boom(_src, _dst):
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", boom)
    assert runner.invoke(app, ["init"]).exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())

    monkeypatch.setattr(os, "link", real_link)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").exists()


def _write_lint_docs(root: Path) -> None:
    docs = root / "docs"
    docs.mkdir()
    # "down" is binding but derives from "up" (derived): a ladder inversion.
    (docs / "up.md").write_text(
        "---\nid: up\nauthority: derived\n---\n# Up\nbody\n", encoding="utf-8"
    )
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )


def test_lint_exits_1_on_violation(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 1
    assert "VIOLATION" in result.stdout


def test_lint_github_emits_each_violation_annotation(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint", "--format", "github"])

    assert result.exit_code == 1
    down_path = "docs/down.md"
    assert result.stdout == (
        f"::error file={down_path},title=doc-lattice ladder violation::"
        "down (binding) -> up (derived)\n"
    )


def test_lint_github_escapes_complete_annotation(tmp_path: Path, monkeypatch):
    # Metacharacters live in a subdirectory under docs (part of the repo-relative path)
    # so escaping of the emitted file= property is exercised; the project root is stripped.
    weird = tmp_path / "docs" / "sub%:,\nline"
    weird.mkdir(parents=True)
    (weird / "up.md").write_text(
        '---\nid: "up%:,\\r\\nline"\nauthority: derived\n---\n# Up\nbody\n',
        encoding="utf-8",
    )
    (weird / "down.md").write_text(
        '---\nid: "down%:,\\r\\nline"\nauthority: binding\nderives_from:\n'
        '  - ref: "up%:,\\r\\nline"\n---\n# Down\nbody\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--format", "github"])

    assert result.exit_code == 1
    expected_path = _escape_github_property("docs/sub%:,\nline/down.md")
    assert result.stdout == (
        f"::error file={expected_path},"
        "title=doc-lattice ladder violation::"
        "down%25:,%0D%0Aline (binding) -> up%25:,%0D%0Aline (derived)\n"
    )


def test_lint_github_suppresses_skipped_edges(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--format", "github"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_lint_format_json_matches_json_alias(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    alias = runner.invoke(app, ["lint", "--json"])
    explicit = runner.invoke(app, ["lint", "--format", "json"])

    assert explicit.exit_code == alias.exit_code == 1
    assert explicit.stdout == alias.stdout


def test_lint_json_lists_violations(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint", "--json"])
    payload = json.loads(result.stdout)
    assert payload["violations"][0]["source_id"] == "down"
    assert payload["violations"][0]["target_authority"] == "derived"
    assert payload["skipped"] == []


def test_lint_exits_0_and_reports_skips(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    # down (binding) derives from up, which has no authority: a skip, not a failure.
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 0
    assert "0 ladder violations" in result.stdout
    assert "1 edges unranked" in result.stdout


def test_lint_json_reports_skips(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint", "--json"])
    payload = json.loads(result.stdout)
    assert payload["violations"] == []
    assert payload["skipped"][0]["reason"] == "target-unannotated"


def test_lint_exits_2_on_bad_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "args",
    [["check"], ["lint"], ["impact", "art-direction"], ["graph", "--format", "json"]],
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


def test_reconcile_all_cached_matches_uncached_bytes(lattice_dir: Path, tmp_path: Path):
    # Twin copies of the fixture tree: one uncached, one cached under cache_trust_stat.
    # The resulting file bytes and exit code must match.
    twin = tmp_path / "twin"
    shutil.copytree(lattice_dir, twin)
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1"}
    uncached = _run(["reconcile", "--all"], lattice_dir, env)
    (twin / ".doc-lattice.yml").write_text(
        "cache_key: recon\ncache_trust_stat: true\n", encoding="utf-8"
    )
    cached = _run(["reconcile", "--all"], twin, env)
    assert cached.exit_code == uncached.exit_code
    for name in ["pc-design.md", "art-direction.md", "gdd.md"]:
        assert (twin / "docs" / name).read_bytes() == (lattice_dir / "docs" / name).read_bytes()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["reconcile", "--all"], (True, True)),
        (["reconcile", "--all", "--dry-run"], (True, False)),
        (["check"], (False, True)),
    ],
)
def test_cli_forces_require_verified_only_for_reconcile(
    lattice_dir: Path,
    tmp_path: Path,
    monkeypatch,
    args,
    expected,
):
    # Mutant-killer: spy on the load_lattice that cli.py imported into its own namespace,
    # wrapping the real function so the real command still runs, and record the
    # require_verified kwarg. reconcile must force the verify tier; check must not.
    seen: dict[str, bool] = {}
    real = cli_mod.load_lattice

    def spy(project, *, require_verified=False, persist_cache=True):
        seen["require_verified"] = require_verified
        seen["persist_cache"] = persist_cache
        return real(
            project,
            require_verified=require_verified,
            persist_cache=persist_cache,
        )

    monkeypatch.setattr(cli_mod, "load_lattice", spy)
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1"}
    _run(args, lattice_dir, env)
    assert (seen["require_verified"], seen["persist_cache"]) == expected
