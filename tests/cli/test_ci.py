"""CLI integration tests for managed GitHub CI audit and refresh."""

from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from typing import TYPE_CHECKING, cast

import pytest
from rich.console import Console
from rich.text import Text

from doc_lattice import __version__
from doc_lattice.cli import app
from doc_lattice.cli.commands import ci as ci_module
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.filesystem import apply_changes, preflight_create
from doc_lattice.github_ci.render import (
    CANONICAL_ARTIFACT_TARGETS,
    CHECKOUT_REF,
    SETUP_UV_REF,
    render_managed_artifacts,
)

from .helpers import runner

_OFFLINE_WORKFLOW = ".github/workflows/doc-lattice.yml"
_LINEAR_WORKFLOW = ".github/workflows/doc-lattice-linear.yml"
_UNRELATED_WORKFLOW = ".github/workflows/unrelated.yml"
_BOOTSTRAP_SCRIPT = ".github/doc-lattice-bootstrap.sh"
_GIT_ATTRIBUTES = ".github/.gitattributes"

if TYPE_CHECKING:
    from pathlib import Path

    from doc_lattice.config import ProjectConfig
    from doc_lattice.github_ci.model import ArtifactChange, ManagedArtifact
    from doc_lattice.model import Lattice


class _TtyStringIO(StringIO):
    """In-memory text stream that reports an attached terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _git_repository(tmp_path: Path) -> None:
    """Run every managed-CI command test inside a real Git working tree."""
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - tests require the local git executable
        cwd=tmp_path,
        check=True,
    )


def _install(root: Path, repository: str = "Guardantix/doc-lattice") -> None:
    artifacts = render_managed_artifacts(repository, __version__)
    apply_changes(preflight_create(root, artifacts))


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _audit_finding_keys(stdout: str) -> frozenset[tuple[str, str]]:
    findings: set[tuple[str, str]] = set()
    for line in stdout.splitlines():
        path, code, _message = line.split(": ", 2)
        findings.add((path, code))
    return frozenset(findings)


def _write_non_utf8_origin(root: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - test requires the local git executable
        cwd=root,
        check=True,
    )
    config = root / ".git/config"
    config.write_bytes(
        config.read_bytes() + b"\n"
        b'[remote "origin"]\n'
        b"\turl = https://github.com/Guardantix/SENSITIVE_ORIGIN_\xff.git\n"
    )


def _runtime(tmp_path: Path) -> tuple[CliRuntime, StringIO]:
    stderr = StringIO()

    def unexpected_config(_config: Path | None, _cwd: Path) -> ProjectConfig:
        raise AssertionError("project loading is not allowed")

    def unexpected_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project, require_verified, persist_cache
        raise AssertionError("lattice loading is not allowed")

    return (
        CliRuntime(
            stdout=Console(file=StringIO(), no_color=True),
            stderr=Console(file=stderr, stderr=True, no_color=True),
            cwd=tmp_path,
            load_config=unexpected_config,
            load_lattice=unexpected_lattice,
        ),
        stderr,
    )


def test_ci_audit_exact_installation_exits_zero(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"])
    assert result.exit_code == 0
    assert result.stdout == "doc-lattice ci audit: ok\n"


def test_ci_audit_from_subdirectory_ignores_nested_managed_decoy(
    tmp_path: Path,
    monkeypatch,
):
    nested = tmp_path / "nested"
    nested.mkdir()
    _install(tmp_path)
    _install(nested)
    unsafe = tmp_path / ".github/workflows/unsafe.yml"
    unsafe.write_text("on: pull_request_target\njobs: {}\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app,
        ["ci", "audit", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 1
    assert result.stdout == (
        ".github/workflows/unsafe.yml: PULL_REQUEST_TARGET: "
        "pull_request_target is prohibited for repository workflows\n"
    )


@pytest.mark.parametrize(
    "running_version",
    ["2.1.0.dev1", "2.1.0rc1", "2.0.0+local"],
)
def test_ci_audit_nonfinal_running_version_reports_stale_without_generation(
    tmp_path: Path,
    monkeypatch,
    running_version: str,
):
    _install(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_module, "__version__", running_version)

    def fail_generation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ci audit must not generate artifacts from the running version")

    monkeypatch.setattr(ci_module, "render_managed_artifacts", fail_generation)

    result = runner.invoke(
        app,
        ["ci", "audit", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 1
    assert result.stderr == ""
    assert _audit_finding_keys(result.stdout) == frozenset(
        {
            (_OFFLINE_WORKFLOW, "STALE_GENERATOR"),
            (_LINEAR_WORKFLOW, "STALE_GENERATOR"),
            (_BOOTSTRAP_SCRIPT, "STALE_GENERATOR"),
            (_GIT_ATTRIBUTES, "STALE_GENERATOR"),
        }
    )
    assert "MANAGED_" not in result.stdout


def test_ci_audit_policy_finding_exits_one(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    unsafe = tmp_path / ".github/workflows/unsafe.yml"
    unsafe.write_text("on: pull_request_target\njobs: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"])
    assert result.exit_code == 1
    assert result.stdout == (
        ".github/workflows/unsafe.yml: PULL_REQUEST_TARGET: "
        "pull_request_target is prohibited for repository workflows\n"
    )


def test_ci_audit_escapes_control_characters_in_finding_paths(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    relative_path = ".github/workflows/evil\nFAKE: OK\x1b.yml"
    unsafe = tmp_path / relative_path
    unsafe.write_text("on: pull_request_target\njobs: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"])

    display_path = json.dumps(relative_path, ensure_ascii=True)[1:-1]
    assert result.exit_code == 1
    assert result.stdout == (
        f"{display_path}: PULL_REQUEST_TARGET: "
        "pull_request_target is prohibited for repository workflows\n"
    )
    assert result.stdout.count("\n") == 1
    assert "\x1b" not in result.stdout


def test_ci_audit_malformed_present_yaml_exits_two(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    broken = tmp_path / ".github/workflows/broken.yml"
    broken.write_text("on: [push\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert ".github/workflows/broken.yml" in result.stderr
    assert "CONFIG_ERROR" in result.stderr


def test_ci_audit_absent_workflows_before_adoption_exits_one(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"])
    assert result.exit_code == 1
    assert ".github/workflows: MISSING_WORKFLOW_DIRECTORY:" in result.stdout
    assert ".github/workflows/doc-lattice.yml: MISSING_MANAGED_ARTIFACT:" in result.stdout


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:Guardantix/doc-lattice.git\n",
        "ssh://git@github.com/Guardantix/doc-lattice.git\n",
        "https://github.com/Guardantix/doc-lattice.git\n",
    ],
)
def test_ci_audit_omitted_repository_resolves_supported_origin(
    tmp_path: Path, monkeypatch, origin: str
):
    _install(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[list[str], Path, int]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["capture_output"] is True
        assert "text" not in kwargs
        assert kwargs["check"] is False
        calls.append(
            (
                argv,
                cast("Path", kwargs["cwd"]),
                cast("int", kwargs["timeout"]),
            )
        )
        return subprocess.CompletedProcess(argv, 0, origin.encode(), b"")

    monkeypatch.setattr(ci_module.subprocess, "run", fake_run)
    result = runner.invoke(app, ["ci", "audit"])

    assert result.exit_code == 0
    assert result.stdout == "doc-lattice ci audit: ok\n"
    assert calls == [
        (
            ["git", "config", "--local", "--get-all", "remote.origin.url"],
            tmp_path,
            5,
        )
    ]


def test_ci_audit_omitted_repository_rejects_multiple_local_origin_urls(
    tmp_path: Path,
    monkeypatch,
):
    _install(tmp_path)
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - test requires the local git executable
        cwd=tmp_path,
        check=True,
    )
    for origin in (
        "https://github.com/unrelated/first-origin.git",
        "https://github.com/Guardantix/doc-lattice.git",
    ):
        subprocess.run(  # noqa: S603 - origins are fixed test fixtures
            [  # noqa: S607 - test requires the local git executable
                "git",
                "config",
                "--local",
                "--add",
                "remote.origin.url",
                origin,
            ],
            cwd=tmp_path,
            check=True,
        )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ci", "audit"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot resolve repository from git origin" in result.stderr
    assert "CONFIG_ERROR" in result.stderr


def test_ci_audit_omitted_repository_ignores_global_origin(tmp_path: Path, monkeypatch):
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - test requires the local git executable
        cwd=tmp_path,
        check=True,
    )
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        '[remote "origin"]\n\turl = https://github.com/unrelated/global-origin.git\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ci", "audit"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot resolve repository from git origin" in result.stderr
    assert "CONFIG_ERROR" in result.stderr


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.CompletedProcess([], 1, b"", b"ignored"), "cannot resolve"),
        (subprocess.CompletedProcess([], 0, b"", b""), "cannot resolve"),
        (subprocess.CompletedProcess([], 0, b" \t\n", b""), "cannot resolve"),
        (
            subprocess.CompletedProcess(
                [], 0, b"https://github.com/a/b\nhttps://github.com/c/d\n", b""
            ),
            "cannot resolve",
        ),
        (
            subprocess.CompletedProcess([], 0, b"https://example.com/a/b\n", b""),
            "origin URL",
        ),
    ],
)
def test_ci_audit_missing_ambiguous_or_unsupported_origin_exits_two(
    tmp_path: Path,
    monkeypatch,
    failure: subprocess.CompletedProcess[bytes],
    message: str,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_module.subprocess, "run", lambda *_args, **_kwargs: failure)
    result = runner.invoke(app, ["ci", "audit"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert message in result.stderr
    assert "CONFIG_ERROR" in result.stderr


def test_ci_audit_non_utf8_real_git_origin_exits_two_without_leaking(tmp_path: Path, monkeypatch):
    _write_non_utf8_origin(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ci", "audit"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == (
        "error: cannot decode repository from git origin as UTF-8 (CONFIG_ERROR)\n"
    )
    assert "Traceback" not in result.stderr
    assert "internal error" not in result.stderr
    assert "SENSITIVE_ORIGIN" not in result.stderr
    assert "b'" not in result.stderr


def test_ci_audit_non_utf8_real_git_origin_has_stable_entry_error(tmp_path: Path):
    _write_non_utf8_origin(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from doc_lattice.cli import main; main()",
            "ci",
            "audit",
        ],
        cwd=tmp_path,
        env={"NO_COLOR": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "error: cannot decode repository from git origin as UTF-8 (CONFIG_ERROR)\n"
    )
    assert "Traceback" not in completed.stderr
    assert "internal error" not in completed.stderr
    assert "SENSITIVE_ORIGIN" not in completed.stderr
    assert "b'" not in completed.stderr


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["git"], 5),
        OSError("cannot execute git"),
    ],
)
def test_ci_audit_git_execution_failure_exits_two(
    tmp_path: Path, monkeypatch, error: BaseException
):
    monkeypatch.chdir(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(ci_module.subprocess, "run", fail)
    result = runner.invoke(app, ["ci", "audit"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot resolve repository from git origin" in result.stderr
    assert "CONFIG_ERROR" in result.stderr


def test_ci_audit_missing_git_exits_two_with_actionable_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(ci_module.subprocess, "run", fail)
    result = runner.invoke(app, ["ci", "audit"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == (
        "error: git executable not found; install Git or pass --repository OWNER/REPO "
        "(CONFIG_ERROR)\n"
    )


def test_ci_audit_explicit_repository_never_reads_origin(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("origin lookup must not run for an explicit repository")

    monkeypatch.setattr(ci_module.subprocess, "run", fail)
    result = runner.invoke(app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"])

    assert result.exit_code == 0


def test_ci_audit_does_not_load_project_or_lattice(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("project state must not be loaded")

    monkeypatch.setattr("doc_lattice.cli.runtime.load_config", fail)
    monkeypatch.setattr("doc_lattice.cli.runtime.load_lattice", fail)
    result = runner.invoke(app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"])

    assert result.exit_code == 0


def test_init_github_then_ci_audit_round_trips_without_loading_lattice(
    tmp_path: Path,
    monkeypatch,
):
    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("project and lattice loading are not allowed")

    monkeypatch.setattr("doc_lattice.cli.runtime.load_config", fail)
    monkeypatch.setattr("doc_lattice.cli.runtime.load_lattice", fail)
    monkeypatch.chdir(tmp_path)

    initialized = runner.invoke(
        app,
        ["init", "--github", "--repository", "Guardantix/doc-lattice"],
    )
    audited = runner.invoke(
        app,
        ["ci", "audit", "--repository", "guardantix/DOC-LATTICE"],
    )

    assert initialized.exit_code == 0
    assert audited.exit_code == 0
    assert audited.stdout == "doc-lattice ci audit: ok\n"


@pytest.mark.parametrize(
    ("target", "old", "new", "expected_findings"),
    [
        pytest.param(
            "offline",
            "on:\n  push:",
            "on:\n  pull_request_target:\n  push:",
            frozenset(
                {
                    (_OFFLINE_WORKFLOW, "MANAGED_TRIGGERS"),
                    (_OFFLINE_WORKFLOW, "PULL_REQUEST_TARGET"),
                }
            ),
            id="pull-request-target",
        ),
        pytest.param(
            "linear",
            "  workflow_dispatch:",
            "  workflow_dispatch:\n  pull_request:",
            frozenset(
                {
                    (_LINEAR_WORKFLOW, "MANAGED_TRIGGERS"),
                    (_LINEAR_WORKFLOW, "PR_LINEAR_INVOCATION"),
                }
            ),
            id="linear-pr-trigger",
        ),
        pytest.param(
            "linear",
            "      github.repository == 'Guardantix/doc-lattice' &&\n",
            "",
            frozenset({(_LINEAR_WORKFLOW, "MANAGED_COMMAND")}),
            id="repository-condition-removed",
        ),
        pytest.param(
            "linear",
            "      github.ref == 'refs/heads/main' &&\n",
            "      startsWith(github.ref, 'refs/heads/') &&\n",
            frozenset({(_LINEAR_WORKFLOW, "MANAGED_COMMAND")}),
            id="ref-condition-broadened",
        ),
        pytest.param(
            "linear",
            "      (github.event_name == 'push' || github.event_name == 'workflow_dispatch')",
            "      (github.event_name == 'push' || "
            "github.event_name == 'workflow_dispatch' || "
            "github.event_name == 'pull_request')",
            frozenset({(_LINEAR_WORKFLOW, "MANAGED_COMMAND")}),
            id="event-condition-broadened",
        ),
        pytest.param(
            "linear",
            "jobs:\n  linear:",
            "jobs:\n  trusted:",
            frozenset(
                {
                    (_LINEAR_WORKFLOW, "LINEAR_SECRET_REFERENCE"),
                    (_LINEAR_WORKFLOW, "MANAGED_JOB"),
                }
            ),
            id="linear-job-renamed",
        ),
        pytest.param(
            "linear",
            "    environment: doc-lattice-linear\n",
            "",
            frozenset({(_LINEAR_WORKFLOW, "MANAGED_JOB")}),
            id="environment-removed",
        ),
        pytest.param(
            "secret-job-env",
            "",
            "",
            frozenset(
                {
                    (_LINEAR_WORKFLOW, "LINEAR_SECRET_REFERENCE"),
                    (_LINEAR_WORKFLOW, "MANAGED_COMMAND"),
                    (_LINEAR_WORKFLOW, "MANAGED_SECRET"),
                }
            ),
            id="secret-moved-to-job-env",
        ),
        pytest.param(
            "secret-earlier-step",
            "",
            "",
            frozenset(
                {
                    (_LINEAR_WORKFLOW, "LINEAR_SECRET_REFERENCE"),
                    (_LINEAR_WORKFLOW, "MANAGED_COMMAND"),
                    (_LINEAR_WORKFLOW, "MANAGED_SECRET"),
                }
            ),
            id="secret-moved-to-earlier-step",
        ),
        pytest.param(
            "linear",
            "secrets.DOC_LATTICE_LINEAR_API_KEY",
            "secrets.LINEAR_API_KEY",
            frozenset(
                {
                    (_LINEAR_WORKFLOW, "LINEAR_SECRET_REFERENCE"),
                    (_LINEAR_WORKFLOW, "MANAGED_SECRET"),
                }
            ),
            id="legacy-repository-secret",
        ),
        pytest.param(
            "unrelated",
            "",
            """\
on: push
jobs:
  unrelated:
    runs-on: ubuntu-latest
    steps:
      - env:
          TOKEN: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: true
""",
            frozenset({(_UNRELATED_WORKFLOW, "LINEAR_SECRET_REFERENCE")}),
            id="unrelated-secret-reference",
        ),
        pytest.param(
            "offline",
            "        with:\n          persist-credentials: false\n",
            "",
            frozenset({(_OFFLINE_WORKFLOW, "MANAGED_CHECKOUT")}),
            id="checkout-credentials-setting-removed",
        ),
        pytest.param(
            "offline",
            f"actions/checkout@{CHECKOUT_REF}",
            "actions/checkout@v4",
            frozenset({(_OFFLINE_WORKFLOW, "MANAGED_ACTION")}),
            id="checkout-tag",
        ),
        pytest.param(
            "linear",
            f"astral-sh/setup-uv@{SETUP_UV_REF}",
            "astral-sh/setup-uv@v6",
            frozenset({(_LINEAR_WORKFLOW, "MANAGED_ACTION")}),
            id="setup-uv-tag",
        ),
        pytest.param(
            "linear",
            "          enable-cache: false",
            "          enable-cache: true",
            frozenset({(_LINEAR_WORKFLOW, "MANAGED_CACHE")}),
            id="setup-uv-cache-enabled",
        ),
        pytest.param(
            "offline",
            "      - name: Audit, check, and lint\n",
            "      - uses: actions/cache@v4\n"
            "        with:\n"
            "          path: .cache\n"
            "      - name: Audit, check, and lint\n",
            frozenset({(_OFFLINE_WORKFLOW, "MANAGED_CACHE")}),
            id="actions-cache-added",
        ),
        pytest.param(
            "unrelated",
            "",
            """\
on: pull_request
jobs:
  reconcile:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice reconcile --all
""",
            frozenset({(_UNRELATED_WORKFLOW, "PR_MUTATING_RECONCILE")}),
            id="mutating-reconcile-on-pr",
        ),
        pytest.param(
            "delete-bootstrap",
            "",
            "",
            frozenset({(_BOOTSTRAP_SCRIPT, "MISSING_MANAGED_ARTIFACT")}),
            id="bootstrap-deleted",
        ),
    ],
)
def test_ci_audit_reports_each_load_bearing_security_control_mutation(  # noqa: PLR0913
    tmp_path: Path,
    monkeypatch,
    target: str,
    old: str,
    new: str,
    expected_findings: frozenset[tuple[str, str]],
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", __version__)
    _install(tmp_path)
    paths: dict[str, Path] = {
        artifact.role: tmp_path / artifact.relative_path for artifact in artifacts
    }
    if target == "unrelated":
        unrelated = tmp_path / ".github/workflows/unrelated.yml"
        unrelated.write_text(new, encoding="utf-8")
    elif target == "delete-bootstrap":
        paths["bootstrap"].unlink()
    elif target in {"secret-job-env", "secret-earlier-step"}:
        linear = paths["linear"]
        _replace_once(
            linear,
            "        env:\n          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n",
            "",
        )
        if target == "secret-job-env":
            _replace_once(
                linear,
                "    runs-on: ubuntu-latest\n",
                "    env:\n"
                "      LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
                "    runs-on: ubuntu-latest\n",
            )
        else:
            _replace_once(
                linear,
                "      - name: Install pinned doc-lattice without the Linear secret\n        run:",
                "      - name: Install pinned doc-lattice without the Linear secret\n"
                "        env:\n"
                "          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
                "        run:",
            )
    else:
        _replace_once(paths[target], old, new)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["ci", "audit", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 1
    assert _audit_finding_keys(result.stdout) == expected_findings


def test_ci_audit_allows_unrelated_release_workflow_controls(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    release = tmp_path / ".github/workflows/release.yml"
    release.write_text(
        """\
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: true
      - uses: actions/cache@v4
        with:
          path: .cache
      - run: uv publish
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["ci", "audit", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 0
    assert result.stdout == "doc-lattice ci audit: ok\n"


def test_ci_refresh_current_installation_exits_zero(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ci", "refresh", "--repository", "Guardantix/doc-lattice"])
    assert result.exit_code == 0
    assert result.stdout == "doc-lattice ci refresh: current\n"


def test_ci_refresh_from_subdirectory_previews_git_root_not_nested_decoy(
    tmp_path: Path,
    monkeypatch,
):
    nested = tmp_path / "nested"
    nested.mkdir()
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    _install(nested)
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 1
    assert "--- a/.github/workflows/doc-lattice.yml" in result.stdout


@pytest.mark.parametrize(
    "running_version",
    ["2.1.0.dev1", "2.1.0rc1", "2.0.0+local"],
)
def test_ci_refresh_still_rejects_nonfinal_running_version(
    tmp_path: Path,
    monkeypatch,
    running_version: str,
):
    _install(tmp_path)
    before = {
        target.relative_path: (tmp_path / target.relative_path).read_bytes()
        for target in CANONICAL_ARTIFACT_TARGETS
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_module, "__version__", running_version)

    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "must be a final release version" in result.stderr
    assert all(
        (tmp_path / relative_path).read_bytes() == data for relative_path, data in before.items()
    )


def test_ci_refresh_previews_stale_managed_artifacts_without_writing(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    before = {item.relative_path: (tmp_path / item.relative_path).read_bytes() for item in old}
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ci", "refresh", "--repository", "Guardantix/doc-lattice"])

    assert result.exit_code == 1
    assert "--- a/.github/workflows/doc-lattice.yml" in result.stdout
    assert all((tmp_path / path).read_bytes() == data for path, data in before.items())


def test_ci_refresh_unsafe_unmarked_state_exits_two_without_writing(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    target = tmp_path / ".github/workflows/doc-lattice.yml"
    original = target.read_bytes()
    target.write_text("# unmanaged\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ci", "refresh", "--repository", "Guardantix/doc-lattice"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "ownership marker" in result.stderr
    assert target.read_text(encoding="utf-8") == "# unmanaged\n"
    assert target.read_bytes() != original


def test_ci_refresh_apply_non_tty_exits_two_without_writing(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
        input="Guardantix/doc-lattice\n",
    )
    assert result.exit_code == 2
    assert "interactive TTY" in result.stderr
    assert (tmp_path / old[0].relative_path).read_text(encoding="utf-8") == old[0].text


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("", "ended before"),
        ("guardantix/doc-lattice\n", "did not match"),
        ("Guardantix/doc-lattice \n", "did not match"),
        ("Guardantix/doc-lattice\r\n", "did not match"),
    ],
)
def test_require_repository_confirmation_fails_closed(tmp_path: Path, answer: str, message: str):
    runtime, stderr = _runtime(tmp_path)
    with pytest.raises(ConfigError, match=message):
        ci_module.require_repository_confirmation(
            _TtyStringIO(answer), runtime, "Guardantix/doc-lattice"
        )
    assert stderr.getvalue() == "Type Guardantix/doc-lattice to apply managed refresh: "


def test_require_repository_confirmation_accepts_exact_text(tmp_path: Path):
    runtime, stderr = _runtime(tmp_path)
    ci_module.require_repository_confirmation(
        _TtyStringIO("Guardantix/doc-lattice\n"),
        runtime,
        "Guardantix/doc-lattice",
    )
    assert stderr.getvalue() == "Type Guardantix/doc-lattice to apply managed refresh: "


def test_ci_refresh_apply_repeats_preflight_and_applies_exact_plan(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    monkeypatch.chdir(tmp_path)
    confirmations: list[str] = []
    real_preflight = ci_module.preflight_refresh
    preflight_calls = 0

    def confirm(_stream: object, _runtime: CliRuntime, repository: str) -> None:
        confirmations.append(repository)

    def tracked_preflight(
        root: Path, artifacts: tuple[ManagedArtifact, ...]
    ) -> tuple[ArtifactChange, ...]:
        nonlocal preflight_calls
        preflight_calls += 1
        return real_preflight(root, artifacts)

    monkeypatch.setattr(ci_module, "require_repository_confirmation", confirm)
    monkeypatch.setattr(ci_module, "preflight_refresh", tracked_preflight)
    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )

    assert result.exit_code == 0
    assert confirmations == ["Guardantix/doc-lattice"]
    assert preflight_calls == 3
    expected = render_managed_artifacts("Guardantix/doc-lattice", __version__)
    assert all(
        (tmp_path / artifact.relative_path).read_text(encoding="utf-8") == artifact.text
        for artifact in expected
    )


def test_ci_refresh_apply_refuses_change_after_confirmation(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    target = tmp_path / old[0].relative_path
    monkeypatch.chdir(tmp_path)

    def change_after_preview(_stream: object, _runtime: CliRuntime, _repository: str) -> None:
        target.write_text(target.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

    monkeypatch.setattr(ci_module, "require_repository_confirmation", change_after_preview)
    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )

    assert result.exit_code == 2
    assert "fresh preview" in result.stderr
    assert target.read_text(encoding="utf-8").endswith("# changed\n")


def test_ci_refresh_apply_detects_current_artifact_race_during_apply(tmp_path: Path, monkeypatch):
    current = render_managed_artifacts("Guardantix/doc-lattice", __version__)
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, (current[0], old[1], old[2], old[3])))
    raced_target = tmp_path / current[0].relative_path
    real_apply = ci_module.apply_changes
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        ci_module,
        "require_repository_confirmation",
        lambda *_args, **_kwargs: None,
    )

    def race_current_artifact(changes: tuple[ArtifactChange, ...]) -> None:
        raced_target.write_text(old[0].text, encoding="utf-8")
        real_apply(changes)

    monkeypatch.setattr(ci_module, "apply_changes", race_current_artifact)
    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )

    assert result.exit_code == 2
    assert "changed after preflight" in result.stderr
    assert raced_target.read_text(encoding="utf-8") == old[0].text
    assert (tmp_path / current[1].relative_path).read_text(encoding="utf-8") == old[1].text
    assert (tmp_path / current[2].relative_path).read_text(encoding="utf-8") == old[2].text


def test_ci_refresh_apply_preserves_repeat_preflight_diagnostic(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    monkeypatch.chdir(tmp_path)
    real_preflight = ci_module.preflight_refresh
    calls = 0

    def failing_repeat(
        root: Path, artifacts: tuple[ManagedArtifact, ...]
    ) -> tuple[ArtifactChange, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            error = ConfigError("cannot read managed artifact: Permission denied; path X")
            error.add_note("distinctive repeat remediation note")
            raise error
        return real_preflight(root, artifacts)

    monkeypatch.setattr(ci_module, "preflight_refresh", failing_repeat)
    monkeypatch.setattr(ci_module, "require_repository_confirmation", lambda *_a, **_k: None)

    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )

    assert result.exit_code == 2
    assert "changed after confirmation" in result.stderr
    assert "Permission denied" in result.stderr
    assert "distinctive repeat remediation note" in result.stderr


def test_ci_refresh_apply_preserves_converge_diagnostic(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    monkeypatch.chdir(tmp_path)
    real_preflight = ci_module.preflight_refresh
    calls = 0

    def failing_converge(
        root: Path, artifacts: tuple[ManagedArtifact, ...]
    ) -> tuple[ArtifactChange, ...]:
        nonlocal calls
        calls += 1
        if calls == 3:
            error = ConfigError("cannot recheck managed artifact: Input/output error; path X")
            error.add_note("distinctive converge remediation note")
            raise error
        return real_preflight(root, artifacts)

    monkeypatch.setattr(ci_module, "preflight_refresh", failing_converge)
    monkeypatch.setattr(ci_module, "require_repository_confirmation", lambda *_a, **_k: None)

    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )

    assert result.exit_code == 2
    assert "did not converge" in result.stderr
    assert "Input/output error" in result.stderr
    assert "distinctive converge remediation note" in result.stderr


def test_ci_refresh_recreates_missing_bootstrap(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", __version__)
    bootstrap = tmp_path / artifacts[2].relative_path
    bootstrap.unlink()
    monkeypatch.chdir(tmp_path)

    preview = runner.invoke(app, ["ci", "refresh", "--repository", "Guardantix/doc-lattice"])
    assert preview.exit_code == 1
    assert "--- /dev/null" in preview.stdout
    assert "+++ b/.github/doc-lattice-bootstrap.sh" in preview.stdout
    assert not bootstrap.exists()

    monkeypatch.setattr(
        ci_module,
        "require_repository_confirmation",
        lambda *_args, **_kwargs: None,
    )
    applied = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )
    assert applied.exit_code == 0
    assert bootstrap.read_text(encoding="utf-8") == artifacts[2].text


def test_ci_refresh_recreates_missing_attributes_policy(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    attributes = tmp_path / ".github/.gitattributes"
    attributes.unlink(missing_ok=True)
    monkeypatch.chdir(tmp_path)

    preview = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice"],
    )

    assert preview.exit_code == 1
    assert "+++ b/.github/.gitattributes" in preview.stdout
    assert not attributes.exists()

    monkeypatch.setattr(
        ci_module,
        "require_repository_confirmation",
        lambda *_args, **_kwargs: None,
    )
    applied = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )

    assert applied.exit_code == 0
    assert attributes.read_text(encoding="utf-8").splitlines()[-1] == (
        "doc-lattice-bootstrap.sh text eol=lf"
    )


def test_ci_refresh_converges_mixed_version_rerun(tmp_path: Path, monkeypatch):
    current = render_managed_artifacts("Guardantix/doc-lattice", __version__)
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    mixed = (current[0], old[1], old[2], old[3])
    apply_changes(preflight_create(tmp_path, mixed))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        ci_module,
        "require_repository_confirmation",
        lambda *_args, **_kwargs: None,
    )

    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
    )

    assert result.exit_code == 0
    assert all(
        (tmp_path / artifact.relative_path).read_text(encoding="utf-8") == artifact.text
        for artifact in current
    )


def test_ci_refresh_updates_repository_identity(tmp_path: Path, monkeypatch):
    _install(tmp_path, repository="OldOwner/old-repo")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        ci_module,
        "require_repository_confirmation",
        lambda *_args, **_kwargs: None,
    )

    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "NewOwner/new-repo", "--apply"],
    )

    assert result.exit_code == 0
    expected = render_managed_artifacts("NewOwner/new-repo", __version__)
    assert all(
        (tmp_path / artifact.relative_path).read_text(encoding="utf-8") == artifact.text
        for artifact in expected
    )


@pytest.mark.parametrize("option", ["--yes", "--force"])
def test_ci_refresh_has_no_confirmation_bypass(tmp_path: Path, monkeypatch, option: str):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "ci",
            "refresh",
            "--repository",
            "Guardantix/doc-lattice",
            "--apply",
            option,
        ],
    )

    assert result.exit_code == 2
    stderr = Text.from_ansi(result.stderr).plain
    assert f"No such option: {option}" in stderr


def test_ci_refresh_requires_repository(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ci", "refresh"])
    assert result.exit_code == 2
    stderr = Text.from_ansi(result.stderr).plain
    assert "Missing option '--repository'" in stderr
