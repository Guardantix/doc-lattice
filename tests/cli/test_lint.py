"""CLI integration tests for the lint command."""

import json
from pathlib import Path

from doc_lattice.cli import app
from doc_lattice.cli.github import escape_github_property

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
    compact = runner.invoke(app, ["lint", "--format", "json"])
    pretty = runner.invoke(app, ["lint", "--format", "json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert "\n  " in pretty.stdout


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
    # The carriage return and newline sit in the directory name alone: AD-35 refuses a control
    # character in an id or a ref outright, so a document carrying one never reaches an
    # annotation at all, while a filename still can and is what exercises %0D%0A end to end.
    weird = tmp_path / "docs" / "sub%:,\r\nline"
    weird.mkdir(parents=True)
    (weird / "up.md").write_text(
        '---\nid: "up%:,line"\nauthority: derived\n---\n# Up\nbody\n',
        encoding="utf-8",
    )
    (weird / "down.md").write_text(
        '---\nid: "down%:,line"\nauthority: binding\nderives_from:\n'
        '  - ref: "up%:,line"\n---\n# Down\nbody\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--format", "github"])

    assert result.exit_code == 1
    expected_path = escape_github_property("docs/sub%:,\r\nline/down.md")
    assert "%0D%0A" in expected_path
    assert result.stdout == (
        f"::error file={expected_path},"
        "title=doc-lattice ladder violation::"
        "down%25:,line (binding) -> up%25:,line (derived)\n"
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


def test_lint_json_lists_violations(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint", "--format", "json"])
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
    result = runner.invoke(app, ["lint", "--format", "json"])
    payload = json.loads(result.stdout)
    assert payload["violations"] == []
    assert payload["skipped"][0]["reason"] == "target-unannotated"


def test_lint_exits_2_on_bad_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots: ['../x']\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 2
    assert "resolves outside the project root" in result.stderr


def _nested_lint_project(tmp_path: Path) -> Path:
    """Write a ladder violation at the checkout root and return a nested cwd inside it."""
    _write_lint_docs(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots:\n  - docs\n", encoding="utf-8"
    )
    nested = tmp_path / "tools" / "scripts"
    nested.mkdir(parents=True)
    return nested


def test_lint_github_annotation_is_workspace_relative_from_a_nested_cwd(
    tmp_path: Path, monkeypatch
):
    nested = _nested_lint_project(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app, ["lint", "--config", "../../.doc-lattice.yml", "--format", "github"]
    )

    assert result.exit_code == 1
    assert result.stdout == (
        "::error file=docs/down.md,title=doc-lattice ladder violation::"
        "down (binding) -> up (derived)\n"
    )


def test_lint_github_annotation_uses_cwd_when_no_workspace_is_set(tmp_path: Path, monkeypatch):
    nested = _nested_lint_project(tmp_path)
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app, ["lint", "--config", "../../.doc-lattice.yml", "--format", "github"]
    )

    assert result.exit_code == 1
    document = tmp_path / "docs" / "down.md"
    assert result.stdout == (
        f"::error file={escape_github_property(str(document))},"
        "title=doc-lattice ladder violation::down (binding) -> up (derived)\n"
    )


def test_lint_github_annotation_ignores_a_workspace_that_excludes_the_document(
    tmp_path: Path, monkeypatch
):
    _nested_lint_project(tmp_path)
    # Inside tmp_path, since the workspace only has to exclude docs/down.md, not the whole tree.
    elsewhere = tmp_path / "tools"
    monkeypatch.setenv("GITHUB_WORKSPACE", str(elsewhere))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--format", "github"])

    assert result.exit_code == 1
    assert result.stdout == (
        "::error file=docs/down.md,title=doc-lattice ladder violation::"
        "down (binding) -> up (derived)\n"
    )


def test_lint_names_an_ambiguous_target_in_human_and_json(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Notes\n\n# Notes\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#notes\n---\n# Down\n", encoding="utf-8"
    )
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots:\n  - docs\n", encoding="utf-8"
    )

    human = runner.invoke(app, ["lint", "--config", str(tmp_path / ".doc-lattice.yml")])
    payload = runner.invoke(
        app, ["lint", "--config", str(tmp_path / ".doc-lattice.yml"), "--format", "json"]
    )

    assert 'ambiguous with "Notes" (line 1), "Notes" (line 3)' in human.stdout
    assert json.loads(payload.stdout)["ambiguous"][0]["target_ref"] == "up#notes"
