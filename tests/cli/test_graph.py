"""CLI integration tests for the graph command."""

import json
from pathlib import Path

from doc_lattice.cli import app

from .helpers import runner


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
