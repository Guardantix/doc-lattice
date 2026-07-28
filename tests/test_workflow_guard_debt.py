"""Contract for the base-relative fail-closed guard debt gate in CI."""

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_ROOT = Path(__file__).resolve().parents[1]
_CI = _ROOT / ".github/workflows/ci.yml"


def _ci() -> dict[str, Any]:
    return YAML(typ="safe").load(_CI.read_text(encoding="utf-8"))


def _guard_debt_job() -> dict[str, Any]:
    return _ci()["jobs"]["guard-debt"]


def _job_text() -> str:
    """Return the job's steps as text, including env bindings and run scripts."""
    return "\n".join(
        f"{step.get('run', '')}\n{step.get('env', {})}" for step in _guard_debt_job()["steps"]
    )


def test_guard_debt_job_exists_and_gates_release() -> None:
    workflow = _ci()

    assert "guard-debt" in workflow["jobs"]
    assert "guard-debt" in workflow["jobs"]["release"]["needs"]


def test_guard_debt_job_runs_on_pushes_as_well_as_pull_requests() -> None:
    # Monotonicity only on pull_request would leave a direct push free to add a source origin
    # and a matching debt record together.
    assert "if" not in _guard_debt_job()


def test_guard_debt_checkout_fetches_enough_history_to_reach_the_base() -> None:
    checkout = next(
        step
        for step in _guard_debt_job()["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )

    assert checkout["with"]["fetch-depth"] == 0


def test_guard_debt_resolves_the_base_from_the_event_payload() -> None:
    script = _job_text()

    assert "github.event.pull_request.base.sha" in script
    assert "github.event.before" in script


def test_guard_debt_reads_the_base_revision_as_data_not_as_a_checkout() -> None:
    script = _job_text()

    assert "git show" in script
    assert "scripts/check_guard_inventory.py" in script
    assert "tests/fixtures/shell_guard_debt.json" in script


def test_guard_debt_runs_the_base_owned_checker_against_the_candidate_tree() -> None:
    # A head change must not be able to weaken the extractor while making debt appear to shrink,
    # so the checker that runs is the base revision's copy and the candidate is only its input.
    script = _job_text()

    assert "$RUNNER_TEMP/base/check_guard_inventory.py" in script
    assert "--compare-base" in script
    assert "--root ." in script


def test_guard_debt_tolerates_a_base_that_predates_the_gate() -> None:
    # The commit that introduces the gate has a base with no checker and no debt snapshot, and so
    # does the first push to main after it merges. `git show` would fail there under
    # `set -euo pipefail` and block the release job.
    script = _job_text()

    assert "git cat-file -e" in script


def test_guard_debt_runs_the_checker_on_a_pinned_interpreter() -> None:
    # Every other job pins its interpreter; the base checker parses candidate source, so a
    # runner default older than the candidate's syntax would surface as an opaque SyntaxError.
    steps = _guard_debt_job()["steps"]
    uses = [str(step.get("uses", "")) for step in steps]

    assert any(reference.startswith("astral-sh/setup-uv@") for reference in uses)
    assert "uv python install" in _job_text()
    assert "uv run" in _job_text()
