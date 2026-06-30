"""Tests for the CLI."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import game_lattice.cli as cli_mod
from game_lattice import __version__
from game_lattice.cli import app
from game_lattice.error_types import ConfigError
from game_lattice.tickets import Ticket, TicketState

runner = CliRunner()


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_check_exits_1_on_drift(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1


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
    assert stale["target_id"] == "accent"
    assert stale["expected"] != stale["actual"]


def test_check_exits_2_on_bad_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
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


def test_impact_lists_dependents(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "accent", "--json"])
    payload = json.loads(result.stdout)
    assert "pc-design" in {n["id"] for n in payload["affected"]}


def test_impact_human_output_lists_tickets(lattice_dir: Path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # absolute path makes the line long; stop rich wrapping it
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "accent"])
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
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
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


def test_reconcile_write_error_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)

    def boom(_path, _text):
        raise OSError("disk full")

    monkeypatch.setattr(cli_mod, "_atomic_write", boom)
    result = runner.invoke(app, ["reconcile", "pc-design"])
    assert result.exit_code == 2


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
    assert runner.invoke(app, ["reconcile", "pc-design", "--ref", "accent"]).exit_code == 0
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
    result = runner.invoke(app, ["linear", "--from", "accent", "--json"])
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


def test_atomic_create_writes_when_absent(tmp_path: Path):
    target = tmp_path / ".game-lattice.yml"
    cli_mod._atomic_create(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_atomic_create_refuses_existing_and_preserves_it(tmp_path: Path):
    target = tmp_path / ".game-lattice.yml"
    target.write_text("original\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        cli_mod._atomic_create(target, "new\n")
    assert target.read_text(encoding="utf-8") == "original\n"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_atomic_create_leaves_nothing_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / ".game-lattice.yml"

    def boom(_src, _dst):
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError, match="link failed"):
        cli_mod._atomic_create(target, "data\n")
    assert not target.exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_atomic_create_writes_large_payload_intact(tmp_path: Path):
    target = tmp_path / ".game-lattice.yml"
    # A payload larger than any single os.write buffer would publish; the helper
    # must write every byte before linking, never a truncated file.
    payload = "".join(f"line {i}\n" for i in range(20000))
    cli_mod._atomic_create(target, payload)
    assert target.read_text(encoding="utf-8") == payload
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_init_writes_config_and_prints_codegen(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    config = (tmp_path / ".game-lattice.yml").read_text(encoding="utf-8")
    assert "docs_roots:" in config
    assert "- docs" in config
    assert ".pre-commit-config.yaml" in result.stdout
    assert ".github/workflows/game-lattice.yml" in result.stdout
    assert f"@v{__version__}" in result.stdout


def test_init_skips_existing_config_but_still_prints(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".game-lattice.yml").write_text("SENTINEL\n", encoding="utf-8")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".game-lattice.yml").read_text(encoding="utf-8") == "SENTINEL\n"
    assert ".github/workflows/game-lattice.yml" in result.stdout


def test_init_bakes_flag_values(tmp_path: Path, monkeypatch):
    from game_lattice.config import load_config  # noqa: PLC0415

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
    assert not (tmp_path / ".game-lattice.yml").exists()


def test_init_rejects_control_character_in_flag(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--linear-team", "a\nb"])
    assert result.exit_code == 2
    assert not (tmp_path / ".game-lattice.yml").exists()


def test_init_rejects_invalid_linear_team(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A lowercase, hyphenated value is not a valid Linear team key, so init must
    # refuse it rather than scaffold a config that the linear command rejects.
    result = runner.invoke(app, ["init", "--linear-team", "my-team-slug"])
    assert result.exit_code == 2
    assert not (tmp_path / ".game-lattice.yml").exists()


def test_init_rejects_markup_metachar_in_docs_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "../[/]"])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not (tmp_path / ".game-lattice.yml").exists()


def test_init_crash_during_link_leaves_clean_state(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_link = os.link

    def boom(_src, _dst):
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", boom)
    assert runner.invoke(app, ["init"]).exit_code == 2
    assert not (tmp_path / ".game-lattice.yml").exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())

    monkeypatch.setattr(os, "link", real_link)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (tmp_path / ".game-lattice.yml").exists()


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
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 2
