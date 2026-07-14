"""CLI integration tests for the init command."""

import os
from pathlib import Path

import pytest

import doc_lattice.cli.commands.init as init_command
from doc_lattice import __version__, persistence
from doc_lattice.cli import app

from .helpers import runner


def test_init_delegates_create_only_write_to_shared_persistence(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, bytes, str]] = []

    def capture(path: Path, data: bytes, *, prefix: str) -> None:
        calls.append((path, data, prefix))

    monkeypatch.setattr(init_command, "atomic_create_bytes", capture)
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

    monkeypatch.setattr(init_command, "atomic_create_bytes", fail_create)
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
