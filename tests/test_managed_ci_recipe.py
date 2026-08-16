"""Contract tests for the trusted Linear workflow published in MANAGED_CI.md.

The recipe is documentation, but this particular block is security-sensitive project output:
a reader installs it verbatim, and after 5.0 removes the managed commands it is the only
description of the protected setup this project ships. Nothing else checks it.
``scripts/check_version_sync.py`` reads only ``doc-lattice==`` refs, and
``tests/test_workflow_pinning.py`` inspects this repository's own ``.github/workflows`` files
rather than fenced YAML inside a Markdown document. Without this module the block could become
invalid YAML, lose its event allowlist, promote the secret above the final step, or drift from
the shipped action pins while every other gate stayed green.

Two independent checks run here on purpose. The equality check pins the block to the managed
renderer and enforces MANAGED_CI.md's own conversion claim, that deleting the four ownership
marker lines from an installed managed artifact leaves exactly the documented workflow. The
structural checks re-derive the security properties from the parsed document, so they keep
working when GTX-163 removes the renderer and the equality check goes with it.
"""

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from doc_lattice import __version__
from doc_lattice.constants import CHECKOUT_USES, SETUP_UV_USES
from doc_lattice.github_ci.model import MARKER_PREFIXES
from doc_lattice.github_ci.render import (
    LINEAR_JOB_ID,
    LINEAR_SECRET_ENV_NAME,
    LINEAR_SECRET_ENV_VALUE,
    render_workflows,
)
from doc_lattice.scaffold import PYTHON_PIN

_ROOT = Path(__file__).resolve().parents[1]
_MANAGED_CI = _ROOT / "MANAGED_CI.md"

# The placeholder identity the published block carries. A reader substitutes their own canonical
# OWNER/REPO; the renderer accepts this literal as a valid identity, which is what lets the
# equality check below compare the two texts directly.
_PLACEHOLDER_REPOSITORY = "OWNER/REPO"
_ENVIRONMENT = "doc-lattice-linear"
_FENCED_YAML = re.compile(r"^```yaml\n(?P<body>.*?)^```$", re.MULTILINE | re.DOTALL)

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


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the ``on:`` mapping under either YAML spelling.

    A 1.1 resolver reads the bare key ``on`` as the boolean ``True``, so reading only the
    string key would silently find nothing and pass every trigger assertion vacuously.
    """
    triggers = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(triggers, dict)
    return triggers


def _linear_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert list(jobs) == [LINEAR_JOB_ID], (
        f"the published workflow must define exactly the {LINEAR_JOB_ID!r} job"
    )
    job = jobs[LINEAR_JOB_ID]
    assert isinstance(job, dict)
    return job


def test_published_workflow_matches_the_managed_renderer_without_its_markers():
    """The block is the managed Linear artifact minus its four ownership marker lines.

    MANAGED_CI.md tells a converting adopter to delete exactly those lines and keep the rest,
    so that claim has to hold byte for byte rather than approximately.
    """
    _, linear = render_workflows(_PLACEHOLDER_REPOSITORY, __version__)
    rendered_lines = linear.text.splitlines(keepends=True)
    marker_lines = rendered_lines[: len(MARKER_PREFIXES)]

    assert [line.split(":", 1)[0] + ":" for line in marker_lines] == list(MARKER_PREFIXES)
    assert _published_workflow_text() == "".join(rendered_lines[len(MARKER_PREFIXES) :])


def test_published_workflow_triggers_are_exactly_trusted_main_and_dispatch():
    triggers = _triggers(_parsed_workflow())

    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    for forbidden in _FORBIDDEN_TRIGGERS:
        assert forbidden not in triggers, f"{forbidden} must never trigger the Linear gate"


def test_published_workflow_guard_carries_repository_ref_and_event_conditions():
    """All three conditions, not two.

    Repository and ref alone would accept a `pull_request_target` run, whose GITHUB_REF is the
    default branch, so the event allowlist is what refuses it inside the job itself. That
    matters more in the recipe than it did under the managed product, because no repository-wide
    audit runs anymore.
    """
    guard = _linear_job(_parsed_workflow())["if"]

    assert f"github.repository == '{_PLACEHOLDER_REPOSITORY}'" in guard
    assert "github.ref == 'refs/heads/main'" in guard
    assert "github.event_name == 'push'" in guard
    assert "github.event_name == 'workflow_dispatch'" in guard


def test_published_workflow_binds_the_protected_environment_and_least_privilege_token():
    workflow = _parsed_workflow()

    assert workflow["permissions"] == {"contents": "read"}
    assert _linear_job(workflow)["environment"] == _ENVIRONMENT


def test_published_workflow_maps_the_secret_only_on_its_final_step():
    workflow = _parsed_workflow()
    job = _linear_job(workflow)
    steps = job["steps"]

    assert "env" not in workflow, "no workflow-level env may carry the Linear secret"
    assert "env" not in job, "no job-level env may carry the Linear secret"
    for step in steps[:-1]:
        assert "env" not in step, "only the final step may receive the Linear secret"
    assert steps[-1]["env"] == {LINEAR_SECRET_ENV_NAME: LINEAR_SECRET_ENV_VALUE}
    assert steps[-1]["run"].endswith('doc-lattice" linear --exit-code')
    assert _published_workflow_text().count(LINEAR_SECRET_ENV_VALUE) == 1


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


def test_recipe_names_the_removal_release_for_every_deprecated_command():
    document = _document()

    for command in ("init --github", "ci audit", "ci refresh"):
        assert f"`{command}`" in document
    assert "removed in 5.0" in document
