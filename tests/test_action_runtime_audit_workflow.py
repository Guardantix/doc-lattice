"""Contract tests for the action-runtime audit workflow and the Dependabot configuration.

AD-42 splits "a pin has fallen behind its releases" from "an executed action runs on a runtime
that is going away" and answers each with one stock GitHub mechanism. Neither answer is a
required check, so nothing else in the suite notices when one of them stops being wired up.
These tests are that notice. `tests/test_workflow_pinning.py` still owns the supply-chain rule
that every `uses:` here resolves to a commit SHA, so no pin is restated in this module.
"""

from pathlib import Path

from workflow_helpers import (
    _action_references,
    _invocations,
    _invokes,
    _load_workflow,
    _named_step,
    _triggers,
    _uses_fragments,
    _workflows,
)

from doc_lattice.constants import CHECKOUT_USES, SETUP_UV_USES

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIR = _ROOT / ".github/workflows"
_AUDIT_PATH = _WORKFLOW_DIR / "action-runtime-audit.yml"
_DEPENDABOT_PATH = _ROOT / ".github/dependabot.yml"
_SCRIPT = "scripts/audit_action_runtimes.py"
_SOURCE_WORKFLOWS = ("ci.yml", "claude.yml")
_UPSTREAM_ACTIONS = frozenset(
    reference.partition("@")[0] for reference in (CHECKOUT_USES, SETUP_UV_USES)
)
# Copied verbatim from the workflow rather than reassembled from its parts. The condition is the
# only thing keeping the audit off the roughly-every-comment stream of skipped `Claude Code`
# runs, and a rewrite that looks equivalent is exactly how that protection gets lost.
_AUDIT_IF = (
    "github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion != 'skipped'"
)
_AUDIT_ENV = {
    "GH_TOKEN": "${{ github.token }}",
    "RUN_ID": "${{ github.event.workflow_run.id || inputs.run_id }}",
}


def _referenced_actions() -> set[str]:
    """Return every distinct action name any workflow in this repository references.

    Read from the parsed documents rather than the raw ``uses:`` lines: the version comment
    those carry is discarded here anyway, and an undercount is invisible to the assertion
    below, which compares a limit against the size of whatever this happens to find.
    """
    return {
        reference.partition("@")[0]
        for workflow in _workflows().values()
        for reference in _action_references(workflow)
    }


def test_audit_watches_exactly_the_workflows_that_execute_actions():
    """A source workflow missing here executes actions no one is auditing.

    `workflow_run` matches on the source workflow's `name:`, not on its filename, so renaming
    `CI` or `Claude Code` silently detaches the audit and leaves it reporting green forever.
    Deriving the expected list from those files is what turns that rename into a failure here.
    """
    expected = sorted(_load_workflow(_WORKFLOW_DIR / name)["name"] for name in _SOURCE_WORKFLOWS)
    workflow_run = _triggers(_load_workflow(_AUDIT_PATH))["workflow_run"]

    assert sorted(workflow_run["workflows"]) == expected
    # Only a completed run has settled annotations; auditing `requested` would read a run that
    # has not written its warnings yet and report it clean.
    assert workflow_run["types"] == ["completed"]


def test_audit_can_be_replayed_against_a_named_run():
    """Without the manual trigger a missed or superseded audit cannot be re-run.

    The run id has to be required: defaulting it would let a dispatch audit nothing and still
    report success, which is indistinguishable from a run with no findings.
    """
    dispatch = _triggers(_load_workflow(_AUDIT_PATH))["workflow_dispatch"]

    assert dispatch["inputs"]["run_id"]["required"] is True


def test_audit_job_reads_only_what_it_needs_and_skips_runs_that_executed_nothing():
    """Widened permissions here would hand a default-branch job write access it never uses.

    `workflow_run` runs on the default branch with the repository's own token, which is the one
    context where an over-permissioned job matters, so the grant is asserted exactly rather than
    as a subset. The condition is asserted alongside it because a skipped source run executed no
    action at all, and `claude.yml` skips on nearly every comment in the repository.
    """
    job = _load_workflow(_AUDIT_PATH)["jobs"]["audit"]

    assert job["permissions"] == {"contents": "read", "actions": "read", "checks": "read"}
    assert job["if"] == _AUDIT_IF


def test_audit_step_runs_the_auditor_against_the_triggering_run():
    """A step that only names the script, or reads the wrong run, audits nothing.

    Both spellings of the id have to survive: `workflow_run` supplies `github.event.workflow_run.id`
    and a manual dispatch supplies `inputs.run_id`, and dropping either half leaves that trigger
    invoking the auditor with an empty argument.
    """
    job = _load_workflow(_AUDIT_PATH)["jobs"]["audit"]
    step = _named_step(job, "Audit the completed run")

    assert step["env"] == _AUDIT_ENV
    invocations = [argv for argv in _invocations(step["run"]) if _invokes(argv, _SCRIPT)]
    assert invocations
    assert "${RUN_ID}" in invocations[0]


def test_audit_workflow_pins_the_same_action_fragments_this_repository_ships():
    """A local copy of a shipped pin is how the two halves drift apart.

    `tests/test_workflow_pinning.py` compares whole fragments across every workflow, so this is
    the same rule read from the other side: it fails loudly here first if the new workflow ever
    picks up its own `actions/checkout` or `astral-sh/setup-uv` pin.
    """
    fragments = set(_uses_fragments(_AUDIT_PATH))

    assert {CHECKOUT_USES, SETUP_UV_USES} <= fragments


def test_dependabot_watches_the_actions_ecosystem_on_a_bounded_cadence():
    """Without this file nothing notices a pin that has fallen behind its upstream releases.

    A frozen SHA cannot drift on its own, which is the point of pinning and also the reason it
    goes stale silently. The commit prefix keeps those pull requests inside the repository's
    own conventional-commit history rather than arriving as untyped subjects.
    """
    config = _load_workflow(_DEPENDABOT_PATH)

    assert config["version"] == 2
    assert len(config["updates"]) == 1
    update = config["updates"][0]
    assert update["package-ecosystem"] == "github-actions"
    assert update["directory"] == "/"
    assert update["schedule"]["interval"] == "monthly"
    assert update["commit-message"]["prefix"] == "ci"


def test_dependabot_groups_exactly_the_pins_this_project_ships_to_adopters():
    """Splitting these two would open a pull request that cannot go green on its own.

    Each is also published from `constants.py`, so a bump is a coupled multi-file edit and the
    two have to arrive on one branch a maintainer can finish. Grouping any *other* action in
    with them would be the opposite failure: an unrelated fix held hostage by a red pin test.
    The expected set is derived from the shipped constants so adding a third shipped pin fails
    here instead of quietly updating on its own.
    """
    groups = _load_workflow(_DEPENDABOT_PATH)["updates"][0]["groups"]

    assert list(groups) == ["shipped-pins"]
    assert set(groups["shipped-pins"]["patterns"]) == _UPSTREAM_ACTIONS


def test_dependabot_can_open_a_pull_request_for_every_action_at_once():
    """A limit below the action count silently drops updates for whichever actions come last.

    Dependabot stops opening pull requests once the limit is reached and reports nothing, so a
    workflow that grows past the limit loses coverage for the actions it just added.
    """
    referenced = _referenced_actions()
    limit = _load_workflow(_DEPENDABOT_PATH)["updates"][0]["open-pull-requests-limit"]

    assert referenced
    assert limit >= len(referenced), (
        f"open-pull-requests-limit is {limit}, but the workflows reference "
        f"{len(referenced)} distinct actions: {sorted(referenced)}"
    )
