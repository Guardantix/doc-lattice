"""Contract tests for the trusted Linear workflow published in MANAGED_CI.md.

The recipe is documentation, but this particular block is security-sensitive project output:
a reader installs it verbatim, and now that the managed commands are gone it is the only
description of the protected setup this project ships. Nothing else checks it.
``scripts/check_version_sync.py`` reads only ``doc-lattice==`` refs, and
``tests/test_workflow_pinning.py`` inspects this repository's own ``.github/workflows`` files
rather than fenced YAML inside a Markdown document. Without this module the block could become
invalid YAML, lose its event allowlist, promote the secret above the final step, or drift from
the shipped action pins while every other gate stayed green.

Every check re-derives its security property from the parsed document against literals declared
in this module, so nothing here depends on a renderer the project no longer ships.

The ``gh`` procedure is covered too. It is copy-paste shell that establishes the boundary the
workflow depends on, so an edit that retargets the environment, renames the secret, widens the
branch policy, or drops host pinning is as damaging as an edit to the YAML.
"""

import re
import textwrap
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from workflow_helpers import _triggers

from doc_lattice import __version__
from doc_lattice.constants import CHECKOUT_USES, SETUP_UV_USES
from doc_lattice.markdown_compat import anchor_ids, extract_headings
from doc_lattice.scaffold import PYTHON_PIN

_ROOT = Path(__file__).resolve().parents[1]
_MANAGED_CI = _ROOT / "MANAGED_CI.md"

# The placeholder identity the published block carries. A reader substitutes their own canonical
# OWNER/REPO; the renderer accepts this literal as a valid identity, which is what lets the
# equality check below compare the two texts directly.
_PLACEHOLDER_REPOSITORY = "OWNER/REPO"

# Declared as literals rather than imported from the renderer. These are the security properties
# themselves, so deriving them from the code under description would let a rename pass unnoticed,
# and importing them would tie this module's import to a package 5.0 deletes.
_ENVIRONMENT = "doc-lattice-linear"
_JOB_ID = "linear"
_DEFAULT_BRANCH = "main"
_SECRET_NAME = "DOC_LATTICE_LINEAR_API_KEY"  # noqa: S105  # pragma: allowlist secret
_SECRET_ENV_VAR = "LINEAR_API_KEY"  # noqa: S105  # pragma: allowlist secret
_SECRET_REFERENCE = "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}"  # noqa: S105  # pragma: allowlist secret
_EXPECTED_STEP_COUNT = 4

# Compared as one normalized string, not as substrings. Every individual condition is still
# present when the operators between them are rewritten, so `&&` swapped for `||`, or a `true ||`
# prefix, satisfies a containment check while making the guard accept anything.
_EXPECTED_GUARD = (
    f"github.repository == '{_PLACEHOLDER_REPOSITORY}' && "
    "github.ref == 'refs/heads/main' && "
    "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
)

_FENCED_YAML = re.compile(r"^```yaml\n(?P<body>.*?)^```$", re.MULTILINE | re.DOTALL)
_FENCED_BASH = re.compile(
    r"^[ \t]*```bash\n(?P<body>.*?)^[ \t]*```[ \t]*$", re.MULTILINE | re.DOTALL
)
_ANCHOR_LINK = re.compile(r"\]\(#(?P<anchor>[^)]+)\)")

# Every pull-request-family event, plus the merge-queue event the managed triggers deliberately
# omit. `pull_request_target` is the load-bearing one: it resolves GITHUB_REF to the default
# branch, so the environment's main-only policy would authorize it while it handles untrusted
# input. The others are listed so a future edit cannot quietly widen the trigger set at all.
_FORBIDDEN_TRIGGERS = (
    "pull_request",
    "pull_request_target",
    "pull_request_review",
    "pull_request_review_comment",
    "merge_group",
)


def _document() -> str:
    return _MANAGED_CI.read_text(encoding="utf-8")


def _published_workflow_text() -> str:
    """Return the one fenced YAML block that publishes the trusted Linear workflow."""
    blocks = [
        match.group("body")
        for match in _FENCED_YAML.finditer(_document())
        if match.group("body").startswith("name: doc-lattice Linear\n")
    ]
    assert len(blocks) == 1, (
        f"MANAGED_CI.md must publish exactly one trusted Linear workflow block, found {len(blocks)}"
    )
    return blocks[0]


def _parsed_workflow() -> dict[Any, Any]:
    """Return the parsed block, keyed loosely because ``on`` can resolve to a boolean."""
    loader = YAML(typ="safe")
    document = loader.load(_published_workflow_text())
    assert isinstance(document, dict)
    return document


def _linear_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert list(jobs) == [_JOB_ID], (
        f"the published workflow must define exactly the {_JOB_ID!r} job"
    )
    job = jobs[_JOB_ID]
    assert isinstance(job, dict)
    return job


def _shell_commands() -> list[str]:
    """Return every command in the document's ``bash`` blocks, continuations joined.

    Backslash continuations are folded first so a single logical invocation is one entry, then
    whitespace is collapsed so an assertion can match a flag without depending on wrapping.
    Comment lines are dropped, and a jq expression that spans lines inside its quotes leaves
    trailing fragments that no assertion here looks at, because every check filters on a
    ``gh`` prefix.
    """
    commands: list[str] = []
    for match in _FENCED_BASH.finditer(_document()):
        body = textwrap.dedent(match.group("body")).replace("\\\n", " ")
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                commands.append(" ".join(stripped.split()))
    return commands


def test_published_workflow_triggers_are_exactly_trusted_main_and_dispatch():
    triggers = _triggers(_parsed_workflow())

    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    for forbidden in _FORBIDDEN_TRIGGERS:
        assert forbidden not in triggers, f"{forbidden} must never trigger the Linear gate"


def test_published_workflow_guard_is_exactly_the_three_part_condition():
    """All three conditions, combined exactly as published.

    Repository and ref alone would accept a `pull_request_target` run, whose GITHUB_REF is the
    default branch, so the event allowlist is what refuses it inside the job itself. That
    matters more in the recipe than it did under the managed product, because no repository-wide
    audit runs anymore. The whole expression is compared because the conditions surviving a
    rewrite of the operators between them is precisely the failure worth catching.
    """
    guard = _linear_job(_parsed_workflow())["if"]

    assert " ".join(guard.split()) == _EXPECTED_GUARD


def test_published_workflow_binds_the_protected_environment_and_least_privilege_token():
    workflow = _parsed_workflow()
    job = _linear_job(workflow)

    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in job, (
        "a job-level permissions block overrides the workflow-level one wholesale"
    )
    assert job["environment"] == _ENVIRONMENT


def test_published_workflow_never_continues_on_error():
    """A tolerated failure is the one edit that silently disarms the gate.

    ``linear --exit-code`` reports DANGER or BLOCKED by exiting non-zero, so
    ``continue-on-error`` on the final step, the job, or the workflow turns every finding the
    gate exists to raise into a green run. Checked against the raw text so no placement escapes.
    """
    assert "continue-on-error" not in _published_workflow_text()


def test_published_workflow_maps_the_secret_only_on_its_final_step():
    workflow = _parsed_workflow()
    job = _linear_job(workflow)
    steps = job["steps"]

    assert "env" not in workflow, "no workflow-level env may carry the Linear secret"
    assert "env" not in job, "no job-level env may carry the Linear secret"
    assert len(steps) == _EXPECTED_STEP_COUNT, (
        "the step count is pinned so a step cannot be inserted into the trusted job unnoticed"
    )
    for step in steps[:-1]:
        assert "env" not in step, "only the final step may receive the Linear secret"
    assert steps[-1]["env"] == {_SECRET_ENV_VAR: _SECRET_REFERENCE}
    assert steps[-1]["run"].endswith('doc-lattice" linear --exit-code')
    assert _published_workflow_text().count(_SECRET_REFERENCE) == 1


def test_published_workflow_pins_actions_to_the_shipped_fragments():
    """Compare the written `uses:` lines, not the parsed values.

    The release tag rides along as a trailing `# vX.Y.Z` comment, which the safe loader
    discards, so comparing parsed refs would let a bumped SHA keep a stale tag in the one
    place users copy it from.
    """
    fragments = [
        line.strip().removeprefix("- ").removeprefix("uses:").strip()
        for line in _published_workflow_text().splitlines()
        if line.strip().startswith(("uses:", "- uses:"))
    ]

    assert fragments == [CHECKOUT_USES, SETUP_UV_USES]


def test_published_workflow_hardens_checkout_and_disables_persistent_caching():
    steps = _linear_job(_parsed_workflow())["steps"]

    assert steps[0]["with"] == {"persist-credentials": False}
    assert steps[1]["with"] == {"enable-cache": False}


def test_published_workflow_installs_the_current_pins():
    install = _linear_job(_parsed_workflow())["steps"][2]["run"]

    assert f"doc-lattice=={__version__}" in install
    assert f"uv python install {PYTHON_PIN}" in install


def test_documented_gh_calls_are_pinned_to_github_com():
    """Every documented call names its host, by the only mechanism each subcommand offers.

    ``gh api`` takes ``--hostname``; ``gh secret``, ``gh run``, and ``gh workflow`` do not, and
    accept the host only as a ``--repo`` prefix. Against a different active host the secret
    deletions return the same not-found result as a secret that was already absent, which is the
    outcome the reader is told to expect, so the repository-scoped key survives and the
    verification step confirms its absence against a repository it never inspected.
    """
    checked = 0
    for command in _shell_commands():
        if command.startswith("gh api "):
            assert "--hostname github.com" in command, command
            checked += 1
        elif command.startswith(("gh secret ", "gh run ", "gh workflow ")):
            assert "--repo github.com/" in command, command
            checked += 1

    assert checked, "MANAGED_CI.md must document the gh calls this test pins"


def test_gh_procedure_targets_the_environment_and_secret_the_workflow_uses():
    """The shell and the YAML have to name the same environment and the same secret.

    Nothing else ties them together: the workflow binds an environment by name and reads a
    secret by name, and the procedure creates both, so a rename on either side leaves a job that
    is skipped or a secret that is never mapped, with no gate anywhere reporting it.
    """
    job = _linear_job(_parsed_workflow())
    commands = _shell_commands()

    creates = [command for command in commands if "--method PUT" in command]
    assert len(creates) == 1, "the recipe must create the environment exactly once"
    assert f"environments/{_ENVIRONMENT}" in creates[0]
    assert job["environment"] == _ENVIRONMENT

    sets = [command for command in commands if command.startswith("gh secret set ")]
    assert sets, "the procedure must set the environment secret"
    for command in sets:
        assert _SECRET_NAME in command, command
        assert f"--env {_ENVIRONMENT}" in command, command
    assert job["steps"][-1]["env"] == {_SECRET_ENV_VAR: _SECRET_REFERENCE}


def test_gh_procedure_allows_exactly_the_main_branch():
    """The deployment allow list is the boundary; a pattern here would widen it silently."""
    policies = [
        command
        for command in _shell_commands()
        if "deployment-branch-policies" in command and "--method POST" in command
    ]

    assert len(policies) == 1, "the recipe must add exactly one deployment branch policy"
    assert "--field 'name=main'" in policies[0]
    assert "--field 'type=branch'" in policies[0]


def test_gh_procedure_reads_state_before_it_creates_the_environment():
    """The existence precondition has to precede the call that rewrites the policy.

    The create call is a PUT, so on an environment that already exists it overwrites the
    deployment branch policy, and the readback that follows reports only what that call just
    wrote. Ordered the other way the reader gets a clean readback and no signal that a different
    protection was replaced.
    """
    commands = _shell_commands()
    # The quoted path ends at `environments`, which distinguishes the listing call from every
    # call addressing `environments/doc-lattice-linear` underneath it.
    listing = f'"repos/{_PLACEHOLDER_REPOSITORY}/environments"'
    environments_reads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("gh api ") and listing in command
    ]
    creates = [index for index, command in enumerate(commands) if "--method PUT" in command]

    assert environments_reads, "step 3 must list the repository's environments before creating one"
    assert creates, "step 3 must create the environment with a PUT"
    environments_read, create = environments_reads[0], creates[0]

    assert environments_read < create, (
        "step 3 must check for an existing environment before the PUT rewrites its policy"
    )


def test_gh_procedure_matches_environment_names_case_insensitively():
    """The precondition has to recognize the environment GitHub would actually resolve.

    GitHub environment names are not case sensitive, so the PUT in step 3 updates a
    ``Doc-Lattice-Linear`` that already exists. An exact-match precondition therefore prints
    nothing and clears the reader to take that environment over silently. ``render.py`` already
    compares this name with ``contains_ascii_case_line`` for the managed path, and the hand
    procedure that replaces it cannot be weaker.
    """
    commands = _shell_commands()
    listing = f'"repos/{_PLACEHOLDER_REPOSITORY}/environments"'
    reads = [
        command
        for command in commands
        if command.startswith("gh api ") and listing in command and "--jq" in command
    ]

    assert reads, "step 3 must list the repository's environments"
    for command in reads:
        assert "ascii_downcase" in command, (
            "step 3's existence check must case-fold the environment name, or an existing "
            "environment in different casing reads as absent"
        )
        assert f'== "{_ENVIRONMENT}"' in command, command


def test_documented_init_invocations_pin_the_default_branch():
    """Every scaffold and upgrade call names the branch instead of letting ``init`` probe.

    Without the flag the printed offline workflow triggers on whatever ``origin/HEAD`` the
    reader's clone has cached, and a stale target that still exists locally is indistinguishable
    from a current one without the network. The recipe is pinned to ``main`` end to end, so a
    probed branch would install a `check` and `lint` gate that never runs on the branch every
    other step in this document assumes, and no readback here would report it: they all inspect
    the Linear gate.
    """
    invocations = [command for command in _shell_commands() if " doc-lattice init" in command]

    assert invocations, "MANAGED_CI.md must document the init invocations this test pins"
    for command in invocations:
        assert f"--default-branch {_DEFAULT_BRANCH}" in command, command


def test_gh_procedure_triggers_a_run_before_it_verifies_one():
    """Verification has to dispatch a run, not read whichever run is newest.

    The failures step 6 exists to catch, a rename that breaks the ``if:`` guard above all, stop
    the gate from producing runs at all rather than producing failing ones. So the last run from
    before the break stays at the top of the listing, and reading it certifies a repository whose
    gate is now dead. Ordered as documented, the reader compares a baseline listing against a
    listing taken after an explicit ``workflow_dispatch`` and verifies only the run that appeared.
    """
    commands = _shell_commands()
    dispatches = [
        index for index, command in enumerate(commands) if command.startswith("gh workflow run ")
    ]
    listings = [
        index for index, command in enumerate(commands) if command.startswith("gh run list ")
    ]
    views = [index for index, command in enumerate(commands) if command.startswith("gh run view ")]

    assert len(dispatches) == 1, (
        "step 6 must dispatch exactly one run; a listing alone reports only that the gate used "
        "to work"
    )
    assert views, "step 6 must read the dispatched run's jobs back"
    dispatch, view = dispatches[0], views[0]

    assert commands[dispatch].endswith(f"--ref {_DEFAULT_BRANCH}"), commands[dispatch]
    assert any(index < dispatch for index in listings), (
        "step 6 must record the newest run before it dispatches, or the run it verifies is "
        "indistinguishable from the one that was already there"
    )
    assert any(index > dispatch for index in listings), (
        "step 6 must list runs again after the dispatch to identify the new one"
    )
    assert view > dispatch, "step 6 must verify the dispatched run, not a preexisting one"

    for index in listings:
        assert "--event workflow_dispatch" in commands[index], (
            "step 6's listings must exclude push runs of this same workflow, or a push landing "
            "mid-check supplies the new databaseId the comparison accepts"
        )


def test_every_intra_document_anchor_resolves():
    """The document cross-links its own sections, so a stale anchor strands the reader.

    Anchors are resolved with the engine's own pinned slugger rather than a second
    implementation, which also means fenced code blocks cannot contribute phantom headings.
    """
    document = _document()
    known = set(anchor_ids(extract_headings(document)))
    targets = {match.group("anchor") for match in _ANCHOR_LINK.finditer(document)}

    assert targets, "MANAGED_CI.md must carry intra-document links"
    assert not sorted(targets - known), (
        f"MANAGED_CI.md links to headings that do not exist: {sorted(targets - known)}"
    )
