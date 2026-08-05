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


def test_the_job_keeps_the_display_name_branch_protection_requires() -> None:
    # The gate is enforced by required status checks rather than by a `needs` edge, and a required
    # check is matched by display name. Renaming the job would leave every test here green while
    # the required check never reported again, which is the differential switched off in silence.
    assert _job()["name"] == "Corpus differential"


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
    # to the frozen corpus is in scope exactly as a change to the guard package is, and
    # `error_types.py` is named because it is the one module outside the guard package the scan
    # path imports. The list is an env binding because the guard package's own path carries the
    # distribution marker, which the repository's own scanner refuses in an uncertified run body.
    step = _step("Decide whether the differential has anything to compare")
    replayed = step["env"]["REPLAYED_PATHS"].split()

    assert "src/doc_lattice/github_ci/" in replayed
    assert "src/doc_lattice/error_types.py" in replayed
    assert _TOOL in replayed
    assert "scripts/fuzz_shell_taint.py" in replayed
    assert "tests/fixtures/github_ci_checkpoint/replay_inventory.json" in replayed
    assert "$REPLAYED_PATHS" in step["run"]
    assert 'echo "in-scope=' in step["run"]


def test_a_pull_request_that_only_weakens_the_differential_job_still_replays() -> None:
    # The scale, the relaxation flags and this very scope list all live in the workflow file. Left
    # out of scope, a pull request that only edits the job would skip the differential and report
    # green having replayed nothing, and the weakened job would then be what every later scanner
    # change runs under.
    step = _step("Decide whether the differential has anything to compare")

    assert ".github/workflows/ci.yml" in step["env"]["REPLAYED_PATHS"].split()


def test_a_pull_request_that_only_edits_an_acknowledgement_still_replays() -> None:
    # An acknowledgement pre-authorizes one verdict transition. Leaving the file out of scope
    # would let a pull request land the excuse with the gate skipped and nothing replayed, ready
    # for a later diff to make exactly that move through a green comparison.
    step = _step("Decide whether the differential has anything to compare")

    assert (
        "tests/fixtures/corpus_differential_acknowledgements.json"
        in step["env"]["REPLAYED_PATHS"].split()
    )


def test_every_replay_step_is_gated_on_the_scope_decision() -> None:
    replays = [step for step in _job()["steps"] if f"python {_TOOL}" in str(step.get("run", ""))]

    assert len(replays) == 2
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
    replay = _step("Replay the corpus against both revisions")["run"]

    assert replay.count(f"python {_TOOL} record") == 2
    assert '--scanner-root "$RUNNER_TEMP/base"' in replay
    assert "--scanner-root ." in replay


def test_a_refusal_from_either_revision_fails_the_replay_step() -> None:
    # The two recordings run concurrently, so neither one's exit status reaches the step on its
    # own. Dropping either wait would let a revision that refused to record be read later as the
    # comparison's missing input, or worse be paired with a stale record from an earlier attempt.
    replay = _step("Replay the corpus against both revisions")["run"]

    assert "set -euo pipefail" in replay
    assert "base=$!" in replay
    assert "candidate=$!" in replay
    assert 'wait "$base"' in replay
    assert 'wait "$candidate"' in replay
    assert 'exit "$base_status"' in replay
    assert 'exit "$candidate_status"' in replay


def test_neither_recording_is_abandoned_by_the_other_one_failing() -> None:
    # `wait` returns the status it waited for, so under `set -e` an unguarded first wait ends the
    # step with the second recording still running: it holds half the runner until the job is
    # cleaned up, and whatever it was refusing over never reaches the log. Both statuses are
    # therefore collected before either is acted on, which is what the guards on the waits are for.
    replay = _step("Replay the corpus against both revisions")["run"]
    waits = replay.index('wait "$base"'), replay.index('wait "$candidate"')

    assert 'wait "$base" || base_status=$?' in replay
    assert 'wait "$candidate" || candidate_status=$?' in replay
    # Nothing may exit between the two waits, or the second one is unreachable again.
    assert "exit" not in replay[waits[0] : waits[1]]


def test_the_concurrent_recordings_split_the_runner_rather_than_each_claiming_it() -> None:
    # Each `record` replays across a pool sized to the cores it is given, so two runs that each
    # default to the whole machine oversubscribe it. The split is what makes running them together
    # faster than running them in sequence rather than merely more parallel on paper.
    replay = _step("Replay the corpus against both revisions")["run"]

    assert "$(nproc) / 2" in replay
    assert replay.count('--jobs "$jobs"') == 2


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


def test_neither_replay_step_shrinks_the_corpus_on_the_command_line() -> None:
    # The pinned scale is the tool's default and `check_corpus_scale` reads what the record names,
    # so a scale spelled here overrides both. Pinning the module constants does not reach it, which
    # is why the absence of the flags is what this holds rather than their value. `--workers` is
    # not one of them: it splits the same corpus across processes rather than drawing less of it.
    script = _step("Replay the corpus against both revisions")["run"]

    assert "--seeds" not in script
    assert "--iterations" not in script


def test_the_comparison_takes_neither_relaxation_the_tool_offers() -> None:
    # Each relaxation is a named flag so that a diff taking one is visible. That only gates
    # anything if the workflow is held to taking neither: `--allow-shrunk-corpus` disables the
    # scale check and the stale-acknowledgement failure together, and `--no-corpus-floor` drops the
    # base-owned floor the comparison is otherwise refused without.
    compare = _step("Report verdict divergence the pull request has not acknowledged")["run"]

    assert "--allow-shrunk-corpus" not in compare
    assert "--no-corpus-floor" not in compare


def test_the_differential_skips_a_base_that_carries_none_of_its_inputs() -> None:
    # A base predating the gate carries no frozen inventory for the comparison's floor, so the
    # comparison would refuse and leave the required check red until the branch is rebased. The
    # probe runs only after the base is proved readable, so a force-push cannot reach the skip.
    step = _step("Decide whether the differential has anything to compare")
    required = step["env"]["BASE_GATE_INPUTS"].split()

    assert "tests/fixtures/github_ci_checkpoint/replay_inventory.json" in required
    assert 'git cat-file -e "$BASE_SHA:$required"' in step["run"]
    assert "predates the corpus differential gate" in step["run"]
    assert step["run"].index("refusing to skip the differential") < step["run"].index(
        "predates the corpus"
    )


def test_the_differential_runs_on_a_pinned_interpreter_and_a_locked_environment() -> None:
    steps = _job()["steps"]
    uses = [str(step.get("uses", "")) for step in steps]
    script = _job_text()

    assert any(reference.startswith("astral-sh/setup-uv@") for reference in uses)
    assert "uv python install" in script
    assert "uv sync --locked --group dev" in script


def test_the_installed_interpreter_is_the_one_the_replay_actually_runs_on() -> None:
    # Installing an interpreter does not select it: `uv sync` and `uv run --no-sync` resolve from
    # `.python-version` unless `UV_PYTHON` says otherwise, so without the binding the corpus is
    # scored on whatever that file happens to pin and the install is dead weight.
    assert _job()["env"]["UV_PYTHON"] == "3.13"
    assert (
        'uv python install "$UV_PYTHON"'
        in _step("Install a pinned interpreter and the locked environment")["run"]
    )
