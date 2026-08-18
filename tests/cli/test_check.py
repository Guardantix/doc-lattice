"""CLI integration tests for the check command."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

from doc_lattice.cli import app
from doc_lattice.cli.output import escape_github_property
from doc_lattice.constants import EdgeState

from .helpers import _clean_docs, runner

_SRC = Path(__file__).resolve().parents[2] / "src"


def _mixed_docs(tmp_path: Path) -> None:
    """Write a graph carrying one OK edge and two problem edges in a pinned order.

    The shared ``lattice_dir`` fixture is load bearing across suites and has no OK edge, so
    the default-filter cases build their own graph here instead of widening it. Node ids sort
    ``mid`` before ``zdown``, and ``check`` classifies in node-id then frontmatter-edge order,
    so the classified sequence is OK, BROKEN, UNRECONCILED. Callers reconcile ``mid`` to turn
    its edge OK; ``zdown``'s two edges stay problems either way.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text(
        "---\nid: up\n---\n# Up {#sec}\nsec body\n\n## Other {#other}\nother body\n",
        encoding="utf-8",
    )
    (docs / "mid.md").write_text(
        "---\nid: mid\nderives_from:\n  - ref: up#sec\n---\n# Mid\nbody\n",
        encoding="utf-8",
    )
    (docs / "zdown.md").write_text(
        "---\nid: zdown\nderives_from:\n  - ref: ghost\n  - ref: up#other\n---\n# Z Down\nbody\n",
        encoding="utf-8",
    )


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


def test_check_human_output_omits_ok_rows_by_default(tmp_path: Path, monkeypatch):
    _mixed_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "mid"]).exit_code == 0

    result = runner.invoke(app, ["check"])

    assert result.exit_code == 1
    # The OK row that would have led this listing is gone; the two problem rows keep their
    # classification order, and the verdict still counts all three edges.
    assert result.stdout == (
        "BROKEN        zdown -> ghost\n"
        "UNRECONCILED  zdown -> up#other\n"
        "3 edges: 1 OK, 0 STALE, 1 UNRECONCILED, 1 BROKEN\n"
    )


def test_check_only_ok_still_shows_ok_rows(tmp_path: Path, monkeypatch):
    # The new default is a branch on whether --only was supplied, not a change to what the
    # flag does, so an explicit --only OK against a graph that has an OK edge still lists it.
    _mixed_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "mid"]).exit_code == 0

    result = runner.invoke(app, ["check", "--only", "OK"])

    assert result.exit_code == 1
    assert result.stdout == (
        "OK            mid -> up#sec\n3 edges: 1 OK, 0 STALE, 1 UNRECONCILED, 1 BROKEN\n"
    )


def test_check_json_still_carries_ok_records_by_default(tmp_path: Path, monkeypatch):
    _mixed_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "mid"]).exit_code == 0

    result = runner.invoke(app, ["check", "--format", "json"])

    payload = json.loads(result.stdout)
    assert [(e["source_id"], e["target_ref"], e["state"]) for e in payload["edges"]] == [
        ("mid", "up#sec", "OK"),
        ("zdown", "ghost", "BROKEN"),
        ("zdown", "up#other", "UNRECONCILED"),
    ]


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
    # A problem-free graph is the verdict line alone: the default listing is problem-only, and
    # the verdict is what keeps a clean run explicit rather than silent.
    assert result.stdout == "1 edge: 1 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN\n"


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


def _frontmatter_tiers(tmp_path: Path) -> dict[str, Path]:
    """Write one file per frontmatter tier: a node, unfenced prose, and id-less metadata."""
    docs = tmp_path / "docs"
    docs.mkdir()
    paths = {
        "node": docs / "up.md",
        "prose": docs / "prose.md",
        "skillish": docs / "skillish.md",
    }
    paths["node"].write_text("---\nid: up\n---\n# Up\n", encoding="utf-8")
    paths["prose"].write_text("# just prose\n", encoding="utf-8")
    paths["skillish"].write_text(
        "---\nname: some-skill\ndescription: non-lattice frontmatter\n---\n# Skill\n",
        encoding="utf-8",
    )
    return paths


def _check_in(cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``check`` in a real subprocess, so warnings render to a real stderr.

    pytest replaces ``showwarning`` for the duration of a test, so an in-process run records
    the warning instead of writing it. Only a separate interpreter exercises the stderr a user
    actually sees. ``PYTHONPATH`` carries the source tree because pytest's ``pythonpath``
    setting only reaches the interpreter running the suite, not one this test spawns.
    """
    return subprocess.run(
        [sys.executable, "-c", "from doc_lattice.cli import main; main()", "check"],
        cwd=cwd,
        env={**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(_SRC), **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_reports_id_less_frontmatter_on_stderr_without_changing_its_exit(tmp_path: Path):
    paths = _frontmatter_tiers(tmp_path)

    completed = _check_in(tmp_path)

    assert completed.returncode == 0  # a skip is a warning, not a gate failure
    assert f"skipping {paths['skillish']}" in completed.stderr
    assert "declares no 'id', so it is not a lattice node" in completed.stderr
    assert str(paths["prose"]) not in completed.stderr  # no opening fence stays silent
    assert str(paths["node"]) not in completed.stderr


def test_check_id_less_stderr_is_byte_identical_uncached_cold_and_warm(tmp_path: Path):
    # The point of storing the disposition in the cache: what a user sees must not depend on
    # whether the load was accelerated.
    _frontmatter_tiers(tmp_path)
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg")}

    uncached = _check_in(tmp_path, env)
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: idless\n", encoding="utf-8")
    cold = _check_in(tmp_path, env)  # writes the cache
    warm = _check_in(tmp_path, env)  # every file served from it

    assert "declares no 'id'" in uncached.stderr
    assert (cold.stderr, cold.returncode) == (uncached.stderr, uncached.returncode)
    assert (warm.stderr, warm.returncode) == (uncached.stderr, uncached.returncode)


def test_check_exits_2_naming_the_file_when_an_id_less_block_declares_lattice_intent(
    tmp_path: Path,
):
    # The reported typo: `idd` swallows both the node and the live edge it declares.
    _frontmatter_tiers(tmp_path)
    typo = tmp_path / "docs" / "down.md"
    typo.write_text("---\nidd: down\nderives_from:\n  - ref: up\n---\n# Down\n", encoding="utf-8")

    completed = _check_in(tmp_path)

    assert completed.returncode == 2
    assert f"frontmatter in {typo} declares 'derives_from' but has no 'id' key" in completed.stderr
    # The typo is a frontmatter defect, so it must not send the user to the config file.
    assert "FRONTMATTER_ERROR" in completed.stderr
    assert "CONFIG_ERROR" not in completed.stderr


def _nested_annotation_project(tmp_path: Path) -> tuple[Path, Path]:
    """Write a lattice at the checkout root and return its nested cwd and broken document."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: ghost\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots:\n  - docs\n", encoding="utf-8")
    nested = tmp_path / "tools" / "scripts"
    nested.mkdir(parents=True)
    return nested, docs / "down.md"


def test_check_github_annotation_is_workspace_relative_from_a_nested_cwd(
    tmp_path: Path, monkeypatch
):
    # Invoking from a subdirectory used to emit an absolute path, which GitHub cannot attach
    # to a diff, so inline annotations silently vanished for anyone not running from the root.
    nested, _ = _nested_annotation_project(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app, ["check", "--config", "../../.doc-lattice.yml", "--format", "github"]
    )

    assert result.exit_code == 1
    assert result.stdout == (
        "::error file=docs/down.md,title=doc-lattice BROKEN::down -> ghost is BROKEN\n"
    )


def test_check_github_annotation_uses_cwd_when_no_workspace_is_set(tmp_path: Path, monkeypatch):
    # Outside Actions the base is the invocation cwd, which keeps the absolute fallback for a
    # document that cwd does not contain.
    nested, document = _nested_annotation_project(tmp_path)
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app, ["check", "--config", "../../.doc-lattice.yml", "--format", "github"]
    )

    assert result.exit_code == 1
    assert result.stdout == (
        f"::error file={escape_github_property(str(document))},"
        "title=doc-lattice BROKEN::down -> ghost is BROKEN\n"
    )


def test_check_github_annotation_ignores_a_workspace_that_excludes_the_document(
    tmp_path: Path, monkeypatch
):
    # A workspace pointing somewhere else must not reach the renderer: it would emit the
    # absolute path instead of the cwd-relative one the fallback is there to produce.
    nested, _ = _nested_annotation_project(tmp_path)
    elsewhere = tmp_path.parent / f"{tmp_path.name}-other"
    elsewhere.mkdir()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(elsewhere))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["check", "--format", "github"])

    assert result.exit_code == 1
    assert result.stdout == (
        "::error file=docs/down.md,title=doc-lattice BROKEN::down -> ghost is BROKEN\n"
    )
    assert nested.exists()
