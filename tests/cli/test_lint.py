"""CLI integration tests for the lint command."""

import json
from pathlib import Path

from doc_lattice.cli import app
from doc_lattice.cli.output import escape_github_property

from .helpers import runner


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


def test_lint_format_json_accepts_indent(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    via_flag = runner.invoke(app, ["lint", "--json", "--indent", "2"])
    via_format = runner.invoke(app, ["lint", "--format", "json", "--indent", "2"])
    assert via_flag.exit_code == via_format.exit_code
    assert via_format.stdout == via_flag.stdout
    assert json.loads(via_format.stdout) == json.loads(via_flag.stdout)


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
    expected_path = escape_github_property("docs/sub%:,\nline/down.md")
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
