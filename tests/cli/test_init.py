"""CLI integration tests for the init command."""

import errno
import os
import subprocess
from pathlib import Path

import pytest

import doc_lattice.cli.commands.init as init_command
from doc_lattice import __version__, persistence
from doc_lattice.cli import app
from doc_lattice.path_utils import format_path_for_display

from .helpers import runner


@pytest.fixture(autouse=True)
def _git_repository(tmp_path: Path) -> None:
    """Run init command tests inside a real Git working tree."""
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - tests require the local git executable
        cwd=tmp_path,
        check=True,
    )


# Spelled out here rather than imported from the command module, so a wording change fails the
# test instead of being followed silently.
_EXPECTED_BASELINE_GUIDANCE = (
    "For an initial adoption with no established baseline, run `doc-lattice reconcile --all` "
    "once after annotating documents and before enabling the gates. It acknowledges the "
    "current state so the gates start from a known baseline; BROKEN edges are skipped and "
    "remain findings, so this does not by itself make CI green."
)


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
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  check:\n"
        "    name: Traceability check\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        # Spelled out rather than interpolated from constants, so this stays an independent
        # assertion of the exact bytes init prints.
        "      - uses: actions/checkout@"
        "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"  # pragma: allowlist secret
        "        with:\n"
        "          persist-credentials: false\n"
        "      - uses: astral-sh/setup-uv@"
        "20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1\n"  # pragma: allowlist secret
        "        with:\n"
        "          enable-cache: false\n"
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

    monkeypatch.setattr(init_command, "atomic_create_bytes", capture)
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
        # The fixture repository has no remote, so the probe finds nothing and the fixed
        # fallback is used. Naming the source is what makes a silent fallback visible.
        "workflow triggers on branch main (fallback)\n"
        "Append the .gitignore block, add the pre-commit block under `repos:`, save the \n"
        "workflow as .github/workflows/doc-lattice.yml, and make sure the exact pinned \n"
        f"version {__version__} is published on PyPI so the snippets resolve.\n"
        # One unwrapped line: soft_wrap keeps Rich from splitting `doc-lattice reconcile --all`
        # across a hard newline, which would break the command on copy and in redirection.
        f"{_EXPECTED_BASELINE_GUIDANCE}\n"
    )


def _origin_head(cwd: Path, branch: str) -> None:
    """Point the fixture repository's cached origin/HEAD at a real remote-tracking branch."""
    for arguments in (
        (
            "-c",
            "user.name=doc-lattice tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "seed",
        ),
        ("update-ref", f"refs/remotes/origin/{branch}", "HEAD"),
        ("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"),
    ):
        subprocess.run(  # noqa: S603 - arguments are test-local literals
            ["git", *arguments],  # noqa: S607 - tests require the local git executable
            cwd=cwd,
            check=True,
            capture_output=True,
        )


def test_init_uses_the_probed_default_branch_and_names_its_source(tmp_path: Path, monkeypatch):
    _origin_head(tmp_path, "trunk")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "    branches: [trunk]\n" in result.stdout
    assert "[main]" not in result.stdout
    assert "workflow triggers on branch trunk (origin/HEAD)\n" in result.stderr


def test_init_default_branch_flag_beats_the_probe(tmp_path: Path, monkeypatch):
    # Precedence is flag, then probe, then fallback. The probe is a local hint that cannot see
    # an upstream rename, so the explicit flag has to win outright.
    _origin_head(tmp_path, "trunk")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--default-branch", "develop"])

    assert result.exit_code == 0
    assert "    branches: [develop]\n" in result.stdout
    assert "[trunk]" not in result.stdout
    assert "workflow triggers on branch develop (--default-branch)\n" in result.stderr


def test_init_falls_back_to_main_without_a_probe_result(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "    branches: [main]\n" in result.stdout
    assert "workflow triggers on branch main (fallback)\n" in result.stderr


def test_init_reports_the_branch_before_the_copy_paste_guidance(tmp_path: Path, monkeypatch):
    # Stdout owns the artifacts; the branch and its source belong on stderr, ahead of the
    # instructions that tell an adopter where to paste them.
    _origin_head(tmp_path, "trunk")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.stderr.index("workflow triggers on branch trunk") < result.stderr.index(
        "Append the .gitignore block"
    )
    assert "workflow triggers on branch" not in result.stdout


@pytest.mark.parametrize("branch", ["release/*", "main branch", "!main", "..", "réf", "main."])
def test_init_rejects_hostile_default_branch_before_any_write(
    tmp_path: Path,
    monkeypatch,
    branch: str,
):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--default-branch", branch])

    assert result.exit_code == 2
    assert "must be an ASCII Git branch name" in result.stderr
    assert {path.name for path in tmp_path.iterdir()} == {".git"}


def test_init_rejects_a_probed_branch_outside_the_supported_domain(tmp_path: Path, monkeypatch):
    # Discovery failure falls back silently; a name that was actually discovered and then fails
    # policy is reported instead, because rendering it would produce a filter that is wrong.
    monkeypatch.setattr(init_command, "probe_default_branch", lambda _cwd: "release/*")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert "must be an ASCII Git branch name" in result.stderr
    assert {path.name for path in tmp_path.iterdir()} == {".git"}


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


def test_init_prints_first_adoption_baseline_guidance(tmp_path: Path, monkeypatch):
    # Without a baseline, a fresh adoption turns CI red on its first run and reads as a
    # misconfiguration, so init has to say so.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    narration = " ".join(result.stderr.split())
    assert _EXPECTED_BASELINE_GUIDANCE in narration


def test_baseline_guidance_has_one_owner():
    # init prints the module-level constant verbatim, so the narration cannot drift from it.
    assert " ".join(init_command._BASELINE_GUIDANCE.split()) == _EXPECTED_BASELINE_GUIDANCE


def test_baseline_guidance_is_scoped_and_does_not_promise_green_ci():
    # init is rerunnable against an existing config, and reconcile --all acknowledges every
    # STALE and UNRECONCILED edge, so unqualified guidance would tell an established adopter to
    # erase legitimate drift. BROKEN edges are skipped, so the baseline is not a green-CI promise.
    guidance = " ".join(init_command._BASELINE_GUIDANCE.split())

    assert "initial adoption with no established baseline" in guidance
    assert "doc-lattice reconcile --all" in guidance
    assert "does not by itself make CI green" in guidance


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
    # GTX-209 applied AD-34's display spelling to this shared note. The helper is reached from
    # `init`, the load cache, and reconcile alike, and a reconcile stage inherits a document
    # filename, so the spelling is settled at the one sink rather than per caller. `init`'s own
    # path is not a document path, and this assertion records that it moved with it.
    expected_note = (
        f"durable cleanup failed for helper-owned stage {format_path_for_display(orphan)}: "
        "cleanup blocked; it is not governed by a recovery journal, so inspect and remove it "
        "manually when safe"
    )
    assert expected_note in result.stderr
    # The failure is the stage cleanup, not the config file: it names the write boundary.
    assert "INIT_PERSISTENCE" in result.stderr
    assert "CONFIG_ERROR" not in result.stderr


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
    assert "INIT_PERSISTENCE" in result.stderr
    assert "CONFIG_ERROR" not in result.stderr


def test_init_read_only_filesystem_reports_the_write_boundary_not_the_config(
    tmp_path: Path, monkeypatch
):
    # A read-only or permission-denied working directory is a problem in the directory being
    # scaffolded, so sending the user to .doc-lattice.yml with CONFIG_ERROR was the defect.
    # Injected at the atomic_create_bytes seam rather than by chmod, which root ignores and
    # which cannot represent EROFS at all.
    def fail_create(*_args, **_kwargs) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(init_command, "atomic_create_bytes", fail_create)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot write .doc-lattice.yml" in result.stderr
    assert "Read-only file system" in result.stderr
    assert "INIT_PERSISTENCE" in result.stderr
    assert "CONFIG_ERROR" not in result.stderr


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
