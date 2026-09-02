"""This repository's own link gate runs through the shipped command, in the hook and in CI."""

from pathlib import Path

from cli.helpers import runner
from workflow_helpers import _commands, _invocations, _load_workflow

from doc_lattice.cli import app

_ROOT = Path(__file__).resolve().parents[1]


def _hook_invocations(hook_id: str) -> list[list[str]]:
    config = _load_workflow(_ROOT / ".pre-commit-config.yaml")
    hooks = [
        hook for repo in config["repos"] for hook in repo["hooks"] if hook.get("id") == hook_id
    ]
    assert len(hooks) == 1, f"expected one {hook_id} hook, found {len(hooks)}"
    hook = hooks[0]
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False
    return _invocations(hook["entry"])


def test_the_pre_commit_hook_runs_the_shipped_links_command():
    invocations = _hook_invocations("doc-lattice-links")

    assert [argv[-2:] for argv in invocations] == [["doc-lattice", "links"]]


def test_the_ci_code_quality_job_runs_the_shipped_links_command_with_annotations():
    # CI enumerates its checks directly and never invokes pre-commit, so the hook alone would
    # leave a renamed heading green on a pull request; annotations are the surface a reviewer
    # sees, so CI alone runs the github format.
    job = _load_workflow(_ROOT / ".github/workflows/ci.yml")["jobs"]["code-quality"]
    invocations = [argv for step in job["steps"] for argv in _invocations(_commands(step))]

    assert [argv[-4:] for argv in invocations if "links" in argv] == [
        ["doc-lattice", "links", "--format", "github"]
    ]


def test_the_repository_is_clean_through_the_real_command(monkeypatch):
    # The committed config, its selector, the adapter, the renderer, and the engine composed:
    # exit 0 with nothing on either stream.
    monkeypatch.chdir(_ROOT)

    result = runner.invoke(app, ["links"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
