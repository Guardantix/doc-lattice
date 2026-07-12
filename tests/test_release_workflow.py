"""Contract tests for release and PyPI publishing automation."""

from pathlib import Path

from ruamel.yaml import YAML

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_TEXT = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
_WORKFLOW = YAML(typ="safe").load(_WORKFLOW_TEXT)


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_release_exposes_publish_coordination_outputs():
    release = _WORKFLOW["jobs"]["release"]
    assert release["outputs"] == {
        "proceed": "${{ steps.gate.outputs.proceed }}",
        "create_tag": "${{ steps.gate.outputs.create_tag }}",
        "version": "${{ steps.target.outputs.version }}",
        "tag": "${{ steps.target.outputs.tag }}",
    }


def test_release_gate_distinguishes_retry_from_ordinary_merge():
    gate = _named_step(_WORKFLOW["jobs"]["release"], "Tag-health gate")["run"]
    assert 'tagged_sha="$(git rev-list -n 1 "${TAG}")"' in gate
    assert '[ "${tagged_sha}" = "${GITHUB_SHA}" ]' in gate
    assert 'echo "proceed=true" >> "$GITHUB_OUTPUT"' in gate
    assert 'echo "create_tag=false" >> "$GITHUB_OUTPUT"' in gate


def test_tag_creation_and_github_release_are_idempotent():
    release = _WORKFLOW["jobs"]["release"]
    create_tag = _named_step(release, "Create and push the tag")
    assert create_tag["if"] == "steps.gate.outputs.create_tag == 'true'"
    notes = _named_step(release, "Publish release notes")["run"]
    assert 'gh release view "${TAG}"' in notes
    assert 'gh release create "${TAG}"' in notes


def test_publish_job_uses_exact_tag_and_least_privilege_oidc():
    publish = _WORKFLOW["jobs"]["publish"]
    assert publish["needs"] == "release"
    assert publish["if"] == "needs.release.outputs.proceed == 'true'"
    assert publish["environment"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    checkout = publish["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["ref"] == "${{ needs.release.outputs.tag }}"


def test_publish_job_builds_checks_and_uploads_idempotently():
    publish = _WORKFLOW["jobs"]["publish"]
    build = _named_step(publish, "Build distributions")["run"]
    check = _named_step(publish, "Validate distributions")["run"]
    upload = _named_step(publish, "Publish distributions to PyPI")
    assert build == "uv build"
    assert check == "uvx --from twine twine check dist/*"
    assert upload["uses"] == "pypa/gh-action-pypi-publish@release/v1"
    assert upload["with"]["skip-existing"] is True
