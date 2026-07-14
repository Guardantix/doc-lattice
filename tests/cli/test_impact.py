"""CLI integration tests for the impact command."""

import json
from pathlib import Path

from doc_lattice.cli import app

from .helpers import runner


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


def test_impact_lists_dependents(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "art-direction#accent", "--json"])
    payload = json.loads(result.stdout)
    assert "pc-design" in {n["id"] for n in payload["affected"]}


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


def test_impact_unknown_token_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "nonexistent"])
    assert result.exit_code == 2
