"""Contract tests for the action-pin correspondence workflow.

The correspondence this workflow establishes is only observable with network access, so it cannot
live in the offline suite that gates every pull request. That makes it the third signal AD-42
records and the second one nothing else in the suite would notice going quiet: a scheduled
workflow that stopped being triggered, lost its permissions, or stopped invoking the script would
keep reporting nothing, which reads exactly like a repository whose pins are all correct. These
tests are that notice. `tests/test_workflow_pinning.py` still owns the rule that every ``uses:``
here resolves to a commit SHA, so no pin is restated in this module.
"""

from pathlib import Path

from workflow_helpers import (
    _invocations,
    _invokes,
    _load_workflow,
    _named_step,
    _triggers,
    _uses_fragments,
)

from doc_lattice.constants import CHECKOUT_USES, SETUP_UV_USES

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_PATH = _ROOT / ".github/workflows/action-pin-correspondence.yml"
_RELEASING = _ROOT / "RELEASING.md"
_SCRIPT = "scripts/check_action_pin_correspondence.py"
_JOB = "correspondence"
_STEP = "Check each pin against the release its comment names"


def _job() -> dict:
    return _load_workflow(_WORKFLOW_PATH)["jobs"][_JOB]


def test_the_check_runs_on_a_schedule_and_on_demand_and_never_on_a_pull_request():
    """A pull-request trigger here would make an offline gate depend on the network.

    Both triggers are load-bearing and neither substitutes for the other. The schedule is the
    only thing that notices an upstream retag or tag deletion, which is the case that survives
    AD-42's narrowing; the manual dispatch is what confirms a fix without waiting a month.
    Asserting the trigger set exactly is what keeps `pull_request` from being added later, which
    would put a live API read in front of every merge and go red on any upstream outage.
    """
    triggers = _triggers(_load_workflow(_WORKFLOW_PATH))

    assert set(triggers) == {"workflow_dispatch", "schedule"}


def test_the_schedule_is_a_bounded_monthly_cadence():
    """A daily or hourly cadence spends the rate limit on a question that changes rarely.

    Two lookups per pin against a pair that moves a few times a year does not need watching more
    often than Dependabot watches for the releases themselves.
    """
    schedule = _triggers(_load_workflow(_WORKFLOW_PATH))["schedule"]

    assert len(schedule) == 1
    minute, hour, day_of_month, month, day_of_week = schedule[0]["cron"].split()

    assert minute.isdigit()
    assert hour.isdigit()
    assert day_of_month.isdigit()
    assert (month, day_of_week) == ("*", "*")


def test_the_job_reads_only_what_it_needs():
    """Widened permissions would hand a default-branch job write access it never uses.

    A scheduled workflow runs on the default branch with the repository's own token, which is the
    one context where an over-permissioned job matters, so the grant is asserted exactly rather
    than as a subset. The two lookups are public reads; the token buys a rate limit, not access.
    """
    assert _job()["permissions"] == {"contents": "read"}


def test_the_step_runs_the_checker_against_a_synced_project():
    """A step that only names the script checks nothing, and an unsynced one cannot import.

    The checker reads the pins from `doc_lattice.constants` rather than restating them, which is
    what keeps a bump from having to remember this file. That import is also why this workflow
    syncs the project instead of running under ``--no-project`` the way the runtime auditor does:
    drop the sync and the monthly run fails on an import, having established nothing.
    """
    job = _job()
    commands = [argv for step in job["steps"] for argv in _invocations(step.get("run", ""))]
    invocations = [argv for argv in commands if _invokes(argv, _SCRIPT)]

    assert invocations
    assert any(argv[:2] == ["uv", "sync"] for argv in commands)
    assert "--no-project" not in invocations[0]


def test_the_step_supplies_a_token_for_the_rate_limit():
    """Unauthenticated API reads share one low limit per runner IP.

    The lookups work without a token and then start failing as infrastructure on a busy runner,
    which is a red monthly run that says nothing about the pins.
    """
    assert _named_step(_job(), _STEP)["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}


def test_the_workflow_pins_the_same_action_fragments_this_repository_ships():
    """A local copy of a shipped pin is how the two halves drift apart.

    This workflow checking a pin it does not itself use would be the sharpest version of that:
    it would report on the shipped pair while running a different one.
    """
    fragments = set(_uses_fragments(_WORKFLOW_PATH))

    assert {CHECKOUT_USES, SETUP_UV_USES} <= fragments


def test_the_check_is_not_one_of_the_protected_contexts_releasing_records():
    """Making this merge-blocking is a branch-protection change, not a workflow edit.

    RELEASING.md records the required contexts and treats that list as the settings contract.
    Joining it by naming a job after one of them would leave the document describing a protection
    rule the repository does not have.

    The assertion reads the row rather than counting it, deliberately: the list grows -- GTX-119
    is adding `Runtime floor compatibility` to it now -- and a test that pinned the count would
    fail on someone else's rollout while saying nothing about this workflow.
    """
    row = next(
        line
        for line in _RELEASING.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `checks` and `strict` |")
    )

    assert _load_workflow(_WORKFLOW_PATH)["jobs"][_JOB]["name"] not in row
