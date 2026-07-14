"""CLI integration tests for the linear command."""

import json
from pathlib import Path

import doc_lattice.cli.commands.linear as linear_command
from doc_lattice.cli import app
from doc_lattice.tickets import Ticket, TicketState

from .helpers import runner


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
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    danger = [f for f in payload["findings"] if f["severity"] == "DANGER"]
    assert danger
    assert danger[0]["ticket_ref"] == "PC-228"


def test_linear_json_indent_round_trips(lattice_dir: Path, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, ["linear", "--json"])
    pretty = runner.invoke(app, ["linear", "--json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == 0
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert '\n  "findings": [\n' in pretty.stdout


def test_linear_positional_target_scopes_audit(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "pc-design", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    danger = [f for f in payload["findings"] if f["severity"] == "DANGER"]
    assert any(f["ticket_ref"] == "PC-228" and f["node_id"] == "pc-design" for f in danger)


def test_linear_from_grades_downstream(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--from", "art-direction#accent", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert any(f["ticket_ref"] == "PC-228" for f in payload["findings"])


def test_linear_exit_code_gates_on_danger(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["linear"]).exit_code == 0
    assert runner.invoke(app, ["linear", "--exit-code"]).exit_code == 1


def test_linear_warn_exit_gates_on_warning(lattice_dir, monkeypatch):
    ticket = _ticket(TicketState(name="In Progress", type="started"))
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["linear", "--exit-code"]).exit_code == 0
    assert runner.invoke(app, ["linear", "--exit-code", "--warn-exit"]).exit_code == 1


def test_linear_blocked_ticket_fails_gate(lattice_dir, monkeypatch):
    # The completed ticket is replaced by a typo: gate must still fail (fail-closed).
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({}))
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
    monkeypatch.setattr(linear_command, "fetch_tickets", _fake_fetch({}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--from", "nonexistent"])
    assert result.exit_code == 2
