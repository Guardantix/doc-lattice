"""Contract for the recurring checkpoint corpus differential gate in CI."""

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_ROOT = Path(__file__).resolve().parents[1]
_CI = _ROOT / ".github/workflows/ci.yml"
_JOB = "corpus-differential"
_TOOL = "scripts/corpus_differential.py"


def _ci() -> dict[str, Any]:
    return YAML(typ="safe").load(_CI.read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    return _ci()["jobs"][_JOB]


def _job_text() -> str:
    """Return the job's steps as text, including conditions, env bindings and run scripts."""
    return "\n".join(
        f"{step.get('if', '')}\n{step.get('run', '')}\n{step.get('env', {})}"
        for step in _job()["steps"]
    )


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _job()["steps"] if step.get("name") == name)


def test_the_corpus_differential_job_exists() -> None:
    assert _JOB in _ci()["jobs"]


def test_the_differential_runs_on_pull_requests() -> None:
    # The gate compares a candidate against the revision it is proposed on top of, which is what a
    # pull request payload names. A push to main carries no such pair to replay against.
    assert _job()["if"] == "github.event_name == 'pull_request'"


def test_the_differential_does_not_gate_the_release_job() -> None:
    # `needs` on a job that is skipped for push events skips the dependent as well, which would
    # stop every release. The gate protects the merge, not the tag.
    assert _JOB not in _ci()["jobs"]["release"]["needs"]


def test_the_differential_checks_out_enough_history_to_reach_the_base() -> None:
    checkout = next(
        step
        for step in _job()["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )

    assert checkout["with"]["fetch-depth"] == 0


def test_the_differential_is_scoped_to_changes_that_can_move_a_verdict() -> None:
    # An unrelated pull request must not pay for a twenty-thousand script replay, and the scope
    # step is what decides that. Every input the replay reads is named, so a change to the tool or
    # to the frozen corpus is in scope exactly as a change to the guard package is. The list is an
    # env binding because the guard package's own path carries the distribution marker, which the
    # repository's own scanner refuses in an uncertified run body.
    step = _step("Decide whether the differential has anything to compare")
    replayed = step["env"]["REPLAYED_PATHS"].split()

    assert "src/doc_lattice/github_ci/" in replayed
    assert _TOOL in replayed
    assert "scripts/fuzz_shell_taint.py" in replayed
    assert "tests/fixtures/github_ci_checkpoint/replay_inventory.json" in replayed
    assert "$REPLAYED_PATHS" in step["run"]
    assert 'echo "in-scope=' in step["run"]


def test_every_replay_step_is_gated_on_the_scope_decision() -> None:
    replays = [step for step in _job()["steps"] if f"python {_TOOL}" in str(step.get("run", ""))]

    assert len(replays) == 3
    assert all(step["if"] == "steps.scope.outputs.in-scope == 'true'" for step in replays)


def test_the_differential_resolves_the_base_from_the_event_payload() -> None:
    assert "github.event.pull_request.base.sha" in _job_text()


def test_the_differential_fails_rather_than_skips_when_the_base_is_unreadable() -> None:
    # A base whose object is missing is not a base that predates the gate. Skipping there would
    # wave a scanner change through with a green job and nothing replayed.
    script = _step("Decide whether the differential has anything to compare")["run"]

    assert 'git cat-file -e "$BASE_SHA^{commit}"' in script
    assert "refusing to skip the differential" in script


def test_the_base_revision_is_materialized_from_the_clone_rather_than_re_fetched() -> None:
    script = _step("Materialize the protected base revision")["run"]

    assert "git worktree add" in script
    assert "$RUNNER_TEMP/base" in script


def test_both_revisions_are_replayed_by_the_same_tool_over_the_same_corpus() -> None:
    base = _step("Replay the corpus against the protected base")["run"]
    candidate = _step("Replay the corpus against the candidate")["run"]

    assert f"python {_TOOL} record" in base
    assert '--scanner-root "$RUNNER_TEMP/base"' in base
    assert f"python {_TOOL} record" in candidate
    assert "--scanner-root ." in candidate


def test_the_comparison_reads_the_acknowledgements_and_the_base_owned_corpus_floor() -> None:
    # The tool and the corpus are both candidate-owned, so the base's own inventory is what stops
    # a candidate from shrinking the corpus until the divergence disappears.
    compare = _step("Report verdict divergence the pull request has not acknowledged")["run"]

    assert f"python {_TOOL} compare" in compare
    assert "--acknowledged tests/fixtures/corpus_differential_acknowledgements.json" in compare
    assert (
        '--base-inventory "$RUNNER_TEMP/base/tests/fixtures/github_ci_checkpoint/'
        'replay_inventory.json"' in compare
    )


def test_the_differential_runs_on_a_pinned_interpreter_and_a_locked_environment() -> None:
    steps = _job()["steps"]
    uses = [str(step.get("uses", "")) for step in steps]
    script = _job_text()

    assert any(reference.startswith("astral-sh/setup-uv@") for reference in uses)
    assert "uv python install" in script
    assert "uv sync --locked --group dev" in script
