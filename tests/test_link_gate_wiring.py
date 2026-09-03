"""This repository's own link gate runs through the shipped command, in the hook and in CI."""

from pathlib import Path

from cli.helpers import runner
from workflow_helpers import _commands, _invocations, _load_workflow, _matrix_legs_selected

from doc_lattice.cli import app

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _load_workflow(_ROOT / ".github/workflows/ci.yml")
_LINKS_STEP = "the links step's"


def _code_quality_job() -> dict:
    return _WORKFLOW["jobs"]["code-quality"]


def _links_step_indices(job: dict) -> list[int]:
    return [
        index
        for index, step in enumerate(job["steps"])
        if any("links" in argv for argv in _invocations(_commands(step)))
    ]


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
    job = _code_quality_job()
    invocations = [argv for step in job["steps"] for argv in _invocations(_commands(step))]

    assert [argv[-4:] for argv in invocations if "links" in argv] == [
        ["doc-lattice", "links", "--format", "github"]
    ]


def test_the_ci_links_step_annotates_on_exactly_one_matrix_leg():
    # GTX-489. `code-quality` is a 3.13/3.14 matrix, so an ungated links step annotates every
    # finding once per leg, which puts two identical comments on the same diff position. The
    # step is therefore conditioned on a single interpreter, and this is what holds it there: a
    # later matrix edit that widens the condition back to two legs, or a removed conditional,
    # fails here rather than reappearing as duplicate review noise.
    job = _code_quality_job()
    legs = list(job["strategy"]["matrix"]["python"])
    indices = _links_step_indices(job)

    assert len(indices) == 1, f"expected one links step in code-quality, found {len(indices)}"
    condition = job["steps"][indices[0]].get("if")
    assert condition is not None, (
        "the links step runs on every code-quality leg, so each finding is annotated once per "
        "interpreter. Condition the step on a single leg, as the coverage gate in `tests` does."
    )
    selected = _matrix_legs_selected(condition, legs, _LINKS_STEP)
    assert len(selected) == 1, (
        f"the links step's condition {condition!r} selects {selected} of the matrix {legs}; "
        "annotations are per leg, so anything but one leg annotates each finding zero or "
        "several times."
    )


def test_the_ci_links_condition_leaves_the_rest_of_code_quality_on_both_legs():
    # The conditional belongs to the one step whose output is per-leg. Everything else in the
    # job -- lint, formatting, types, the boundary and version-sync guards, the migration rule --
    # is a per-interpreter check, so a conditional that spread to them would quietly halve the
    # job's coverage while the annotation fix still looked correct.
    job = _code_quality_job()
    links = _links_step_indices(job)
    conditioned = [index for index, step in enumerate(job["steps"]) if "if" in step]

    assert conditioned == links, (
        f"code-quality steps {conditioned} carry a conditional, but only the links step "
        f"{links} may: every other check there is per-interpreter and must run on both legs."
    )


def test_the_repository_is_clean_through_the_real_command(monkeypatch):
    # The committed config, its selector, the adapter, the renderer, and the engine composed:
    # exit 0 with nothing on either stream.
    monkeypatch.chdir(_ROOT)

    result = runner.invoke(app, ["links"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
