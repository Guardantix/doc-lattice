"""CLI integration tests for the check command."""

import json
from pathlib import Path
from typing import get_args

from doc_lattice.cli import app
from doc_lattice.cli.output import escape_github_property
from doc_lattice.constants import EdgeState

from .helpers import _clean_docs, runner


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
        "3 edges: 0 OK, 1 STALE, 1 UNRECONCILED, 1 BROKEN\n"
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
    expected_path = escape_github_property("docs/sub%:,\nline/down.md")
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


def test_check_json_reports_states(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "json"])
    payload = json.loads(result.stdout)
    states = {(e["source_id"], e["target_ref"]): e["state"] for e in payload["edges"]}
    assert states[("gdd", "ghost")] == "BROKEN"


def test_check_json_reports_all_states(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "json"])
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
    compact = runner.invoke(app, ["check", "--format", "json"])
    pretty = runner.invoke(app, ["check", "--format", "json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == 1
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert '\n  "edges": [\n' in pretty.stdout


def test_check_json_zero_indent_round_trips_to_compact_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, ["check", "--format", "json"])
    zero_indent = runner.invoke(app, ["check", "--format", "json", "--indent", "0"])
    assert compact.exit_code == zero_indent.exit_code == 1
    assert json.loads(zero_indent.stdout) == json.loads(compact.stdout)
    assert '\n"edges": [\n' in zero_indent.stdout


def test_check_indent_without_format_json_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--indent", "2"])
    assert result.exit_code == 2
    assert "--indent requires --format json" in result.stderr


def test_check_indent_validation_precedes_project_loading(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", "--config", "missing.yml", "--indent", "0"])
    assert result.exit_code == 2
    assert "--indent requires --format json" in result.stderr
    assert "config file not found" not in result.stderr


def test_check_negative_indent_is_rejected(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "json", "--indent", "-1"])
    assert result.exit_code == 2


def test_check_only_filters_human_output(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--only", "STALE"])
    *rows, summary = [line for line in result.stdout.splitlines() if line.strip()]
    assert rows
    assert all("STALE" in line for line in rows)
    # --only narrows the rows; the summary keeps counting every classified edge, so it
    # deliberately does not sum to the number of rows above it.
    assert summary == "3 edges: 0 OK, 1 STALE, 1 UNRECONCILED, 1 BROKEN"


def test_check_only_filters_json_output(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "json", "--only", "STALE"])
    payload = json.loads(result.stdout)
    assert payload["edges"]
    assert all(edge["state"] == "STALE" for edge in payload["edges"])


def test_check_only_is_case_insensitive(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--only", "stale"])
    *rows, summary = [line for line in result.stdout.splitlines() if line.strip()]
    assert rows
    assert all("STALE" in line for line in rows)
    assert summary == "3 edges: 0 OK, 1 STALE, 1 UNRECONCILED, 1 BROKEN"


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
    # No OK edge to list, yet the run still states the verdict rather than printing nothing.
    assert result.stdout == "3 edges: 0 OK, 1 STALE, 1 UNRECONCILED, 1 BROKEN\n"


def test_check_only_repeated_flags_combine(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(
        app, ["check", "--format", "json", "--only", "STALE", "--only", "BROKEN"]
    )
    payload = json.loads(result.stdout)
    states = {edge["state"] for edge in payload["edges"]}
    assert states == {"STALE", "BROKEN"}


def test_check_without_only_shows_all_states(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "json"])
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


def test_check_reports_unreconciled_for_a_file_docs_root(tmp_path: Path, monkeypatch):
    # GTX-1: a docs_roots entry naming a single .md file (ARCHITECTURE.md, not a directory)
    # was silently dropped by discovery, so its edges vanished and check exited 0. It must
    # now be discovered like any other root and surface its UNRECONCILED edge with exit 1.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text(
        "---\nid: spec\nauthority: binding\n---\n# Spec\nbody\n", encoding="utf-8"
    )
    (tmp_path / "ARCHITECTURE.md").write_text(
        "---\nid: arch\nauthority: derived\nderives_from: [{ref: spec}]\n---\n"
        "# Architecture\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / ".doc-lattice.yml").write_text(
        "docs_roots: [docs, ARCHITECTURE.md]\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["check", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    states = {(e["source_id"], e["target_ref"]): e["state"] for e in payload["edges"]}
    assert states[("arch", "spec")] == "UNRECONCILED"


def test_check_human_summary_is_present_on_a_clean_tree(tmp_path: Path, monkeypatch):
    _clean_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "down"]).exit_code == 0

    result = runner.invoke(app, ["check"])

    assert result.exit_code == 0
    assert result.stdout == (
        "OK            down -> up#sec\n1 edge: 1 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN\n"
    )


def test_check_human_summary_is_present_when_there_are_no_edges(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["check"])

    assert result.exit_code == 0
    assert result.stdout == "0 edges: 0 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN\n"


def test_check_json_summary_covers_every_state_including_zero_counts(
    lattice_dir: Path, monkeypatch
):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "json"])
    payload = json.loads(result.stdout)
    assert payload["summary"] == {"OK": 0, "STALE": 1, "UNRECONCILED": 1, "BROKEN": 1}
    assert set(payload["summary"]) == set(get_args(EdgeState))
    assert sum(payload["summary"].values()) == len(payload["edges"])


def test_check_json_summary_is_not_distorted_by_only(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    unfiltered = json.loads(runner.invoke(app, ["check", "--format", "json"]).stdout)
    filtered = json.loads(
        runner.invoke(app, ["check", "--format", "json", "--only", "STALE"]).stdout
    )

    assert filtered["summary"] == unfiltered["summary"]
    assert len(filtered["edges"]) == 1
    assert sum(filtered["summary"].values()) == 3


def test_check_json_edge_records_are_unchanged_by_the_summary(lattice_dir: Path, monkeypatch):
    # The summary is purely additive: existing consumers reading `edges` keep working, in
    # the same order and with the same per-record keys.
    monkeypatch.chdir(lattice_dir)
    payload = json.loads(runner.invoke(app, ["check", "--format", "json"]).stdout)

    assert list(payload) == ["edges", "summary"]
    assert [(e["source_id"], e["target_ref"]) for e in payload["edges"]] == [
        ("gdd", "ghost"),
        ("pc-design", "art-direction#accent"),
        ("pc-design", "art-direction#motion"),
    ]
    assert all(
        set(edge) == {"source_id", "target_ref", "target_id", "state", "expected", "actual"}
        for edge in payload["edges"]
    )


def test_check_github_output_carries_no_summary(lattice_dir: Path, monkeypatch):
    # The GitHub format is annotations-only and silent on a clean tree; a summary line would
    # be emitted as stray non-annotation output into the workflow log.
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "github"])
    assert all(line.startswith("::error ") for line in result.stdout.splitlines() if line)
    assert "edges:" not in result.stdout


def test_check_exits_0_when_fully_reconciled(tmp_path: Path, monkeypatch):
    _clean_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "down"]).exit_code == 0
    # No broken refs and every edge reconciled, so check reports clean.
    assert runner.invoke(app, ["check"]).exit_code == 0
