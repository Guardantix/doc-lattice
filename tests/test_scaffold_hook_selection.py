"""Real pre-commit selection over the generated hook block, for metadata-only commits (GTX-909).

``test_scaffold.py`` pins what the generated block says. These tests pin what pre-commit does
with it, because which hooks run for a given commit is pre-commit's decision, not the block's:
a filter that parses cleanly can still skip the one commit that changed the lattice.

The engine is this checkout's rather than a PyPI release. Each generated entry's pinned ``uvx``
prefix is replaced by the environment's own ``doc-lattice`` console script, and nothing else in
the block is touched, so selection and argument policy stay exactly as generated.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from doc_lattice.scaffold import render_precommit

_HOOKS = ("doc-lattice check", "doc-lattice lint", "doc-lattice links")
_STATUS_LINE = re.compile(
    r"^(?P<hook>doc-lattice \w+)\.+(?:\(no files to check\))?(?P<status>Passed|Failed|Skipped)$"
)

# Two registered documents, one deriving from the other, both inside the coverage selection.
# Neither Markdown file carries frontmatter, so the manifest is the only thing enrolling them.
_MANIFEST = (
    "nodes:\n"
    "  - path: skills/rule.md\n"
    "    meta:\n"
    "      id: rule\n"
    "      authority: binding\n"
    "  - path: skills/usage.md\n"
    "    meta:\n"
    "      id: usage\n"
    "      authority: binding\n"
    "      derives_from:\n"
    "        - ref: rule\n"
)
_CONFIG = (
    "lattice_format: 2\n"
    "docs_roots: [skills]\n"
    "link_sources: ['skills/*.md']\n"
    "sidecar_manifests: [nodes.yml]\n"
    "sidecar_coverage:\n"
    "  select: ['skills/*.md']\n"
)
_IDENTITY = ("-c", "user.name=doc-lattice tests", "-c", "user.email=tests@example.invalid")


@cache
def _local_engine() -> str:
    script = shutil.which("doc-lattice", path=str(Path(sys.executable).parent))
    assert script is not None, "the doc-lattice console script is not installed beside pytest"
    return script


def _environment(root: Path) -> dict[str, str]:
    # Ambient GIT_* variables (a hook's GIT_INDEX_FILE, say) and the caller's Git configuration
    # (a hooksPath, commit signing) would reach into these commands, and forced color would
    # break the status-line parse.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"FORCE_COLOR", "VIRTUAL_ENV"}
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        PRE_COMMIT_HOME=str(root / "pre-commit-home"),
        XDG_CACHE_HOME=str(root / "xdg-cache"),
        NO_COLOR="1",
    )
    return env


def _run(argv: list[str], repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - arguments are test-local literals and paths
        argv, cwd=repo, env=env, capture_output=True, text=True, check=False
    )


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    result = _run(["git", *args], repo, env)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _local_block() -> str:
    """The generated block with each pinned entry prefix swapped for this checkout's engine."""
    block = render_precommit("0.0.0")
    hooks = YAML(typ="safe").load(block)[0]["hooks"]
    pinned = {hook["entry"].rsplit(" ", 1)[0] for hook in hooks}
    assert len(pinned) == 1
    (prefix,) = pinned
    assert block.count(prefix) == len(_HOOKS)
    return block.replace(prefix, shlex.quote(_local_engine()))


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A committed sidecar project whose generated gates all pass, built once per module."""
    repo = tmp_path_factory.mktemp("baseline") / "repo"
    (repo / "skills").mkdir(parents=True)
    env = _environment(repo.parent)
    (repo / "skills/rule.md").write_text("# Rule\nThe rule a skill follows.\n", encoding="utf-8")
    (repo / "skills/usage.md").write_text("# Usage\nHow a skill applies it.\n", encoding="utf-8")
    (repo / "nodes.yml").write_text(_MANIFEST, encoding="utf-8")
    (repo / ".doc-lattice.yml").write_text(_CONFIG, encoding="utf-8")
    (repo / ".pre-commit-config.yaml").write_text("repos:\n" + _local_block(), encoding="utf-8")
    # The baseline acknowledges the one edge, so every later failure is the mutation's own.
    reconciled = _run([_local_engine(), "reconcile", "--all"], repo, env)
    assert reconciled.returncode == 0, (reconciled.stdout, reconciled.stderr)
    _git(repo, env, "init", "--quiet")
    _git(repo, env, "add", "--all")
    _git(repo, env, *_IDENTITY, "commit", "--quiet", "--no-verify", "-m", "baseline")
    return repo


@pytest.fixture
def gated_repo(baseline: Path, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A private copy of the baseline, so each test mutates and stages only its own."""
    repo = tmp_path / "repo"
    shutil.copytree(baseline, repo, symlinks=True)
    return repo, _environment(tmp_path)


def _run_hooks(repo: Path, env: dict[str, str]) -> tuple[int, dict[str, tuple[str, str]]]:
    """Run pre-commit over the staged files and split its output into one report per hook."""
    result = _run([sys.executable, "-m", "pre_commit", "run", "--color", "never"], repo, env)
    reports: dict[str, tuple[str, str]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        match = _STATUS_LINE.match(line)
        if match:
            current = match["hook"]
            reports[current] = (match["status"], "")
        elif current is not None:
            status, body = reports[current]
            reports[current] = (status, body + line + "\n")
    assert tuple(reports) == _HOOKS, result.stdout + result.stderr
    return result.returncode, reports


def _stage(repo: Path, env: dict[str, str], path: str, text: str) -> None:
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, env, "add", "--", path)
    # Selection is over the staged set, so assert it is exactly the one metadata file.
    assert _git(repo, env, "diff", "--cached", "--name-only").splitlines() == [path]


def _markdown(repo: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in sorted(repo.glob("skills/*.md"))}


def test_every_generated_gate_runs_with_nothing_staged(gated_repo):
    # With no staged file at all, only always_run can select a hook, so this is the case that
    # proves the flag itself rather than a filter that happens to match.
    repo, env = gated_repo
    assert _git(repo, env, "diff", "--cached", "--name-only") == ""

    returncode, reports = _run_hooks(repo, env)

    assert returncode == 0
    assert {hook: status for hook, (status, _) in reports.items()} == dict.fromkeys(
        _HOOKS, "Passed"
    )


@pytest.mark.parametrize(
    ("path", "edit"),
    [
        (
            "nodes.yml",
            lambda text: text.replace(
                "id: usage\n      authority: binding", "id: usage\n      authority: derived"
            ),
        ),
        (".doc-lattice.yml", lambda text: text + "# reviewed for GTX-909\n"),
    ],
    ids=["manifest", "config"],
)
def test_a_valid_metadata_only_commit_runs_and_passes_every_gate(gated_repo, path, edit):
    repo, env = gated_repo
    before = _markdown(repo)
    _stage(repo, env, path, edit((repo / path).read_text(encoding="utf-8")))

    returncode, reports = _run_hooks(repo, env)

    assert returncode == 0
    assert {hook: status for hook, (status, _) in reports.items()} == dict.fromkeys(
        _HOOKS, "Passed"
    )
    assert _markdown(repo) == before


def test_a_manifest_only_commit_dropping_a_registration_fails_check_on_coverage(gated_repo):
    repo, env = gated_repo
    before = _markdown(repo)
    manifest = (repo / "nodes.yml").read_text(encoding="utf-8")
    usage_at = manifest.index("  - path: skills/usage.md\n")
    _stage(repo, env, "nodes.yml", manifest[:usage_at])

    returncode, reports = _run_hooks(repo, env)

    assert returncode == 1
    status, body = reports["doc-lattice check"]
    assert status == "Failed"
    assert "- exit code: 2\n" in body
    assert "error (COVERAGE_ERROR)" in body
    assert "'skills/usage.md': selected by 'skills/*.md'; not enrolled" in body
    assert _markdown(repo) == before


def test_a_manifest_only_authority_inversion_fails_lint_and_leaves_check_green(gated_repo):
    repo, env = gated_repo
    before = _markdown(repo)
    manifest = (repo / "nodes.yml").read_text(encoding="utf-8")
    inverted = manifest.replace(
        "id: rule\n      authority: binding", "id: rule\n      authority: exploratory"
    )
    assert inverted != manifest
    _stage(repo, env, "nodes.yml", inverted)

    returncode, reports = _run_hooks(repo, env)

    assert returncode == 1
    assert reports["doc-lattice check"][0] == "Passed"
    assert reports["doc-lattice links"][0] == "Passed"
    status, body = reports["doc-lattice lint"]
    assert status == "Failed"
    assert "- exit code: 1\n" in body
    assert "usage" in body
    assert "rule" in body
    assert _markdown(repo) == before
