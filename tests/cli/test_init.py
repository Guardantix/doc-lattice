"""CLI integration tests for the init command."""

import os
from pathlib import Path

import pytest

import doc_lattice.cli.commands.init as init_command
from doc_lattice import __version__, persistence
from doc_lattice.cli import app
from doc_lattice.github_ci import filesystem
from doc_lattice.github_ci.render import render_managed_artifacts

from .helpers import runner


def _shared_guidance(version: str) -> str:
    return (
        "# ===== .gitignore (append these lines) =====\n"
        ".doc-lattice-reconcile.json\n"
        ".doc-lattice-reconcile.json.*.tmp\n"
        ".*.doc-lattice-before.*.tmp\n"
        ".*.doc-lattice-after.*.tmp\n"
        "\n"
        "# ===== .pre-commit-config.yaml (add under `repos:`) =====\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: doc-lattice-check\n"
        "        name: doc-lattice check\n"
        f"        entry: uvx --python 3.13 --from doc-lattice=={version} "
        "doc-lattice check\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
        "      - id: doc-lattice-lint\n"
        "        name: doc-lattice lint\n"
        f"        entry: uvx --python 3.13 --from doc-lattice=={version} "
        "doc-lattice lint\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
        "\n"
    )


def _legacy_stdout(version: str) -> str:
    return (
        _shared_guidance(version) + "# ===== .github/workflows/doc-lattice.yml (new file) =====\n"
        "name: doc-lattice\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  check:\n"
        "    name: Traceability check\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: astral-sh/setup-uv@v6\n"
        "      - run: |\n"
        "          set +e\n"
        f"          uvx --python 3.13 --from doc-lattice=={version} doc-lattice check\n"
        "          rc_check=$?\n"
        f"          uvx --python 3.13 --from doc-lattice=={version} doc-lattice lint\n"
        "          rc_lint=$?\n"
        '          [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]\n'
        "\n"
    )


def test_init_delegates_create_only_write_to_shared_persistence(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, bytes, str]] = []

    def capture(path: Path, data: bytes, *, prefix: str) -> None:
        calls.append((path, data, prefix))

    def unexpected_github_prepare(*_args, **_kwargs) -> None:
        raise AssertionError("ordinary init must not prepare GitHub artifacts")

    monkeypatch.setattr(init_command, "atomic_create_bytes", capture)
    monkeypatch.setattr(init_command, "_prepare_github_init", unexpected_github_prepare)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert len(calls) == 1
    target, data, prefix = calls[0]
    assert target == tmp_path / ".doc-lattice.yml"
    assert data == (
        b"# doc-lattice configuration. See https://github.com/Guardantix/doc-lattice\n"
        b"docs_roots:\n"
        b"  - docs\n"
        b"# ignore_globs:\n"
        b'#   - "**/archive/**"\n'
        b"# cache_key: my-project-docs   # opt-in load cache slot under your cache home\n"
        b"# linear_team: ENG\n"
    )
    assert prefix == ".doc-lattice.yml."
    assert result.stdout == _legacy_stdout(__version__)
    assert result.stderr == (
        "wrote .doc-lattice.yml\n"
        "Append the .gitignore block, add the pre-commit block under `repos:`, save the \n"
        "workflow as .github/workflows/doc-lattice.yml, and make sure the exact pinned \n"
        f"version {__version__} is published on PyPI so the snippets resolve.\n"
    )


def test_init_github_requires_repository_before_any_write(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--github"])

    assert result.exit_code == 2
    assert "--repository is required with --github" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_init_repository_requires_github_before_any_write(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--repository", "Guardantix/doc-lattice"])

    assert result.exit_code == 2
    assert "--repository requires --github" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_init_github_creates_managed_artifacts_and_prints_review_guidance(
    tmp_path: Path,
    monkeypatch,
):
    repository = "Guardantix/doc-lattice"
    artifacts = render_managed_artifacts(repository, __version__)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--github", "--repository", repository])

    assert result.exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").exists()
    for artifact in artifacts:
        assert (tmp_path / artifact.relative_path).read_bytes() == artifact.text.encode("utf-8")
        assert artifact.relative_path.as_posix() in result.stderr
    assert result.stdout == _shared_guidance(__version__)
    assert "# ===== .github/workflows/doc-lattice.yml (new file) =====" not in result.stdout
    assert "Review" in result.stderr
    assert f"bash .github/doc-lattice-bootstrap.sh plan {repository}" in result.stderr


def test_init_github_warns_pinned_version_must_be_published(tmp_path: Path, monkeypatch):
    repository = "Guardantix/doc-lattice"
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--github", "--repository", repository])

    assert result.exit_code == 0
    narration = " ".join(result.stderr.split())
    assert f"exact pinned version {__version__} is published on PyPI" in narration


def test_init_github_preflights_conflict_before_config_or_other_artifact_write(
    tmp_path: Path,
    monkeypatch,
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", __version__)
    conflict = tmp_path / artifacts[1].relative_path
    conflict.parent.mkdir(parents=True)
    conflict_bytes = b"user-owned linear workflow\r\n"
    conflict.write_bytes(conflict_bytes)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init", "--github", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 2
    assert "doc-lattice-linear.yml" in result.stderr
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert not (tmp_path / artifacts[0].relative_path).exists()
    assert conflict.read_bytes() == conflict_bytes
    assert not (tmp_path / artifacts[2].relative_path).exists()


def test_init_github_exact_rerun_preserves_all_bytes(tmp_path: Path, monkeypatch):
    repository = "Guardantix/doc-lattice"
    artifacts = render_managed_artifacts(repository, __version__)
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["init", "--github", "--repository", repository])
    assert first.exit_code == 0
    paths = [tmp_path / ".doc-lattice.yml"]
    paths.extend(tmp_path / artifact.relative_path for artifact in artifacts)
    before = [path.read_bytes() for path in paths]

    def unexpected_artifact_create(*_args, **_kwargs) -> None:
        raise AssertionError("exact managed artifacts must not be created again")

    monkeypatch.setattr(filesystem, "atomic_create_bytes", unexpected_artifact_create)
    second = runner.invoke(app, ["init", "--github", "--repository", repository])

    assert second.exit_code == 0
    assert [path.read_bytes() for path in paths] == before


def test_init_github_rerun_creates_only_missing_managed_artifact(
    tmp_path: Path,
    monkeypatch,
):
    repository = "Guardantix/doc-lattice"
    artifacts = render_managed_artifacts(repository, __version__)
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["init", "--github", "--repository", repository])
    assert first.exit_code == 0
    config = tmp_path / ".doc-lattice.yml"
    offline = tmp_path / artifacts[0].relative_path
    missing = tmp_path / artifacts[1].relative_path
    bootstrap = tmp_path / artifacts[2].relative_path
    preserved = [config.read_bytes(), offline.read_bytes(), bootstrap.read_bytes()]
    missing.unlink()
    created: list[Path] = []
    real_create = filesystem.atomic_create_bytes

    def capture_create(path: Path, data: bytes, *, prefix: str) -> None:
        created.append(path)
        real_create(path, data, prefix=prefix)

    monkeypatch.setattr(filesystem, "atomic_create_bytes", capture_create)
    second = runner.invoke(app, ["init", "--github", "--repository", repository])

    assert second.exit_code == 0
    assert created == [missing]
    assert [config.read_bytes(), offline.read_bytes(), bootstrap.read_bytes()] == preserved
    assert missing.read_bytes() == artifacts[1].text.encode("utf-8")


@pytest.mark.parametrize(
    "version",
    ["2.0.0.dev1", "2.1.0rc1", "2.0.0+local", "not-a-final-release"],
)
def test_init_github_rejects_nonfinal_command_version_before_any_write(
    tmp_path: Path,
    monkeypatch,
    version: str,
):
    monkeypatch.setattr(init_command, "__version__", version)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init", "--github", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 2
    assert "must be a final release version" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_init_github_create_race_preserves_winner_and_error_notes(
    tmp_path: Path,
    monkeypatch,
):
    repository = "Guardantix/doc-lattice"
    artifacts = render_managed_artifacts(repository, __version__)
    winner_path = tmp_path / artifacts[0].relative_path
    winner = b"concurrent workflow winner\n"
    real_create = filesystem.atomic_create_bytes

    def collide(path: Path, data: bytes, *, prefix: str) -> None:
        path.write_bytes(winner)
        try:
            real_create(path, data, prefix=prefix)
        except FileExistsError as error:
            error.add_note("concurrent winner must remain untouched")
            raise

    monkeypatch.setattr(filesystem, "atomic_create_bytes", collide)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--github", "--repository", repository])

    assert result.exit_code == 2
    assert "CONFIG_ERROR" in result.stderr
    assert "destination appeared after preflight" in result.stderr
    assert "concurrent winner must remain untouched" in result.stderr
    assert "without rollback" in result.stderr
    assert (tmp_path / ".doc-lattice.yml").exists()
    assert winner_path.read_bytes() == winner
    assert not (tmp_path / artifacts[1].relative_path).exists()
    assert not (tmp_path / artifacts[2].relative_path).exists()


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
