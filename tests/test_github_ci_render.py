"""Tests for deterministic managed GitHub Actions workflow rendering."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import get_args

import pytest
from ruamel.yaml import YAML

from doc_lattice.constants import (
    CHECKOUT_REF,
    CHECKOUT_USES,
    CHECKOUT_VERSION,
    SETUP_UV_REF,
    SETUP_UV_USES,
    SETUP_UV_VERSION,
)
from doc_lattice.github_ci import render as render_module
from doc_lattice.github_ci.model import (
    ARTIFACT_MARKER_PREFIX,
    MANAGED_SCHEMA_LINE,
    REPOSITORY_MARKER_PREFIX,
    VALID_ARTIFACT_ROLES,
    VERSION_MARKER_PREFIX,
    ArtifactRole,
)
from doc_lattice.github_ci.render import (
    BOOTSTRAP_PATH,
    LINEAR_WORKFLOW_PATH,
    OFFLINE_WORKFLOW_PATH,
    ownership_header,
    render_managed_artifacts,
    render_workflows,
)
from doc_lattice.scaffold import PYTHON_PIN

_SECRETS_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])")

# Inputs the committed golden fixtures were captured with. Fixed on purpose, and deliberately
# not the live __version__, so a release cannot silently invalidate the byte comparison.
GOLDEN_REPOSITORY = "Guardantix/doc-lattice"
GOLDEN_VERSION = "2.1.0"
_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "managed-workflows"


def _load_workflow(text):
    """Parse one rendered workflow as safe YAML."""
    return YAML(typ="safe").load(text)


def _iter_unquoted_expression_segments(value):
    """Yield unquoted segments from complete GitHub expression spans."""
    cursor = 0
    while True:
        start = value.find("${{", cursor)
        if start < 0:
            return

        index = start + len("${{")
        segments = []
        segment = []
        in_string = False
        while index < len(value):
            character = value[index]
            if in_string:
                if character == "'":
                    if index + 1 < len(value) and value[index + 1] == "'":
                        index += 2
                        continue
                    in_string = False
                index += 1
                continue

            if value.startswith("}}", index):
                segments.append("".join(segment))
                yield segments
                cursor = index + len("}}")
                break
            if character == "'":
                segments.append("".join(segment))
                segment = []
                in_string = True
            else:
                segment.append(character)
            index += 1
        else:
            return


def _collect_secret_context_scalars(value, path=()):
    """Yield paths and string values that dereference the GitHub secrets context."""
    if isinstance(value, str):
        expressions = _iter_unquoted_expression_segments(value)
        if any(
            _SECRETS_TOKEN_RE.search(segment) is not None
            for expression in expressions
            for segment in expression
        ):
            yield path, value
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            yield from _collect_secret_context_scalars(nested, (*path, str(key)))
    elif isinstance(value, Sequence):
        for index, nested in enumerate(value):
            yield from _collect_secret_context_scalars(nested, (*path, str(index)))


def test_collect_secret_context_scalars_detects_dereference_variants():
    document = {
        "dot": "${{ secrets . DOT_NAME }}",
        "items": [
            "${{ secrets['BRACKET_NAME'] }}",
            {"spaced": "${{ secrets [ 'SPACED_NAME' ] }}"},
        ],
        "function": "${{ toJSON(secrets) }}",
        "dynamic": "${{ secrets[env.SECRET_NAME] }}",
        "multiline": "${{\n  secrets\n}}",
        "format": "${{ format('{{x}}', secrets.TOKEN) }}",
        "escaped": "${{ format('it''s {{x}}', secrets.TOKEN) }}",
        "quoted": "${{ 'secrets' }}",
        "unterminated": "${{ secrets.TOKEN",
        "ordinary": ["secrets.NAME is prose", "${{ notsecrets.NAME }}"],
    }

    assert list(_collect_secret_context_scalars(document)) == [
        (("dot",), "${{ secrets . DOT_NAME }}"),
        (("items", "0"), "${{ secrets['BRACKET_NAME'] }}"),
        (("items", "1", "spaced"), "${{ secrets [ 'SPACED_NAME' ] }}"),
        (("function",), "${{ toJSON(secrets) }}"),
        (("dynamic",), "${{ secrets[env.SECRET_NAME] }}"),
        (("multiline",), "${{\n  secrets\n}}"),
        (("format",), "${{ format('{{x}}', secrets.TOKEN) }}"),
        (("escaped",), "${{ format('it''s {{x}}', secrets.TOKEN) }}"),
    ]


def test_render_workflows_returns_only_canonical_workflow_artifacts():
    artifacts = render_workflows("Guardantix/doc-lattice", "2.1.0")

    assert [(artifact.role, artifact.relative_path) for artifact in artifacts] == [
        ("offline", OFFLINE_WORKFLOW_PATH),
        ("linear", LINEAR_WORKFLOW_PATH),
    ]
    assert BOOTSTRAP_PATH not in {artifact.relative_path for artifact in artifacts}


def test_render_managed_artifacts_include_scoped_bootstrap_lf_policy():
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")

    assert [(artifact.role, artifact.relative_path) for artifact in artifacts] == [
        ("offline", OFFLINE_WORKFLOW_PATH),
        ("linear", LINEAR_WORKFLOW_PATH),
        ("bootstrap", BOOTSTRAP_PATH),
        ("attributes", PurePosixPath(".github/.gitattributes")),
    ]
    assert artifacts[3].text.splitlines() == [
        "# doc-lattice-managed: github-ci-v1",
        "# doc-lattice-artifact: attributes",
        "# doc-lattice-version: 2.1.0",
        "# doc-lattice-repository: Guardantix/doc-lattice",
        "doc-lattice-bootstrap.sh text eol=lf",
    ]


def test_render_offline_workflow_runs_all_gates_without_secrets():
    offline = render_workflows("Guardantix/doc-lattice", "2.1.0")[0]
    workflow = _load_workflow(offline.text)

    assert workflow["on"] == {
        "push": {"branches": ["main"]},
        "pull_request": {"branches": ["main"]},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert list(_collect_secret_context_scalars(workflow)) == []
    assert "LINEAR_API_KEY" not in offline.text
    assert "${{ secrets." not in offline.text
    assert "pull_request_target" not in offline.text
    assert "reconcile" not in offline.text

    run = workflow["jobs"]["check"]["steps"][-1]["run"]
    assert run.splitlines()[0] == "set +e"
    assert "doc-lattice ci audit" in run
    assert "doc-lattice check" in run
    assert "doc-lattice lint" in run
    assert "rc_audit=$?" in run
    assert "rc_check=$?" in run
    assert "rc_lint=$?" in run
    assert run.splitlines()[-1] == (
        '[ "$rc_audit" -eq 0 ] && [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]'
    )


def test_render_linear_workflow_installs_before_mapping_secret():
    linear = render_workflows("Guardantix/doc-lattice", "2.1.0")[1]
    workflow = _load_workflow(linear.text)

    assert workflow["on"] == {
        "push": {"branches": ["main"]},
        "workflow_dispatch": None,
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert "env" not in workflow

    job = workflow["jobs"]["linear"]
    assert job["environment"] == "doc-lattice-linear"
    assert "env" not in job
    assert job["if"] == (
        "github.repository == 'Guardantix/doc-lattice' && github.ref == 'refs/heads/main' "
        "&& (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )

    steps = job["steps"]
    assert list(_collect_secret_context_scalars(workflow)) == [
        (
            ("jobs", "linear", "steps", "3", "env", "LINEAR_API_KEY"),
            "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}",
        )
    ]
    assert all("env" not in step for step in steps[:-1])
    for step in steps[:-1]:
        for field in ("env", "with", "run"):
            assert "${{ secrets." not in str(step.get(field, ""))
    assert linear.text.count("${{ secrets.") == 1
    assert steps[-1]["env"] == {"LINEAR_API_KEY": "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}"}
    assert steps[-1]["run"] == (
        '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code'
    )

    install = steps[-2]["run"]
    assert "uv python install 3.13" in install
    assert 'uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"' in install
    assert "uv pip install" in install
    assert "doc-lattice==2.1.0" in install


def test_rendered_workflows_pin_actions_and_mark_ownership():
    artifacts = render_workflows("Guardantix/doc-lattice", "2.1.0")

    for artifact in artifacts:
        workflow = _load_workflow(artifact.text)
        steps = next(iter(workflow["jobs"].values()))["steps"]

        assert steps[0]["uses"] == f"actions/checkout@{CHECKOUT_REF}"
        assert steps[0]["with"] == {"persist-credentials": False}
        assert steps[1]["uses"] == f"astral-sh/setup-uv@{SETUP_UV_REF}"
        assert steps[1]["with"] == {"enable-cache": False}
        assert f"      - uses: {CHECKOUT_USES}\n" in artifact.text
        assert f"      - uses: {SETUP_UV_USES}\n" in artifact.text
        assert "actions/cache" not in artifact.text
        assert "doc-lattice==2.1.0" in artifact.text
        assert artifact.text.splitlines()[:4] == [
            "# doc-lattice-managed: github-ci-v1",
            f"# doc-lattice-artifact: {artifact.role}",
            "# doc-lattice-version: 2.1.0",
            "# doc-lattice-repository: Guardantix/doc-lattice",
        ]


def test_artifact_role_domain_matches_literal():
    # The shared frozenset must stay in lockstep with the ArtifactRole Literal so the
    # marker parser accepts exactly the declared roles.
    assert frozenset(get_args(ArtifactRole)) == VALID_ARTIFACT_ROLES


def test_ownership_header_is_built_from_shared_grammar_constants():
    header = ownership_header("offline", "Guardantix/doc-lattice", "2.1.0")

    assert header.splitlines() == [
        MANAGED_SCHEMA_LINE,
        f"{ARTIFACT_MARKER_PREFIX} offline",
        f"{VERSION_MARKER_PREFIX} 2.1.0",
        f"{REPOSITORY_MARKER_PREFIX} Guardantix/doc-lattice",
    ]


def test_rendered_python_pin_is_single_sourced_from_scaffold():
    offline, linear = render_workflows("Guardantix/doc-lattice", "2.1.0")

    assert f"--python {PYTHON_PIN} --from doc-lattice==2.1.0" in offline.text
    assert f"uv python install {PYTHON_PIN}" in linear.text
    assert f'uv venv --python {PYTHON_PIN} "$RUNNER_TEMP/doc-lattice-venv"' in linear.text


def test_render_workflows_is_byte_deterministic():
    first = render_workflows("Guardantix/doc-lattice", "2.1.0")
    second = render_workflows("Guardantix/doc-lattice", "2.1.0")

    assert first == second


def test_rendered_workflows_match_golden_bytes_exactly():
    # The guard the substring and self-comparison tests above cannot provide: both would accept
    # an unintended but deterministic byte change. These fixtures were captured from the
    # renderer and are committed as auditable evidence, so any drift in the managed output has
    # to be an explicit, reviewed update to the fixture rather than a silent edit.
    for artifact in render_workflows(GOLDEN_REPOSITORY, GOLDEN_VERSION):
        golden = _GOLDEN_DIR / f"{artifact.role}.yml.golden"

        assert artifact.text.encode("utf-8") == golden.read_bytes(), (
            f"managed {artifact.role} workflow no longer matches {golden.name}; "
            "review the diff and update the fixture only if the change is intended"
        )


def test_managed_templates_carry_no_inline_pin_literals():
    # The whole `uses:` fragment is interpolated, so neither the SHA, the version label, nor
    # the composed fragment may appear as a literal in the template source that renders them.
    for template in (render_module._OFFLINE_TEMPLATE, render_module._LINEAR_TEMPLATE):
        assert CHECKOUT_REF not in template
        assert SETUP_UV_REF not in template
        assert CHECKOUT_VERSION not in template
        assert SETUP_UV_VERSION not in template
        assert "- uses: __CHECKOUT_USES__\n" in template
        assert "- uses: __SETUP_UV_USES__\n" in template


@pytest.mark.parametrize(
    "repository",
    [
        "a/__VERSION__",
        "a/__CHECKOUT_USES__",
        "a/__SETUP_UV_USES__",
        "a/__PYTHON_PIN__",
    ],
)
def test_render_workflows_preserves_token_like_repository_names(repository):
    offline, linear = render_workflows(repository, "2.1.0")
    offline_run = _load_workflow(offline.text)["jobs"]["check"]["steps"][-1]["run"]
    linear_condition = _load_workflow(linear.text)["jobs"]["linear"]["if"]

    assert (
        "uvx --python 3.13 --from doc-lattice==2.1.0 doc-lattice ci audit "
        f"--repository {repository}"
    ) in offline_run.splitlines()
    assert linear_condition == (
        f"github.repository == '{repository}' && github.ref == 'refs/heads/main' "
        "&& (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    assert f"--repository {repository}\n" in offline.text
    assert f"github.repository == '{repository}' &&" in linear.text
