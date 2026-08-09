"""Contract tests for release and PyPI publishing automation.

These assert which action each release step calls, not the commit it is pinned to.
`tests/test_workflow_pinning.py` owns the supply-chain rule that every `uses:` in every
workflow resolves to a 40-character commit SHA, so restating individual pins here would
only force a lockstep edit on every routine pin refresh.
"""

from pathlib import Path

from ruamel.yaml import YAML

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_TEXT = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
_WORKFLOW = YAML(typ="safe").load(_WORKFLOW_TEXT)
_CHECKOUT = "actions/checkout"
_UPLOAD_ARTIFACT = "actions/upload-artifact"
_DOWNLOAD_ARTIFACT = "actions/download-artifact"
_PYPI_PUBLISH = "pypa/gh-action-pypi-publish"
_ARTIFACT_NAME = "release-distributions"


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _action(step: dict) -> str:
    """Return a step's action reference with its pin stripped, or "" if it runs a script."""
    return step.get("uses", "").split("@", 1)[0]


def test_release_exposes_publish_coordination_outputs():
    release = _WORKFLOW["jobs"]["release"]
    assert release["permissions"] == {"contents": "write"}
    assert release["outputs"] == {
        "proceed": "${{ steps.gate.outputs.proceed }}",
        "create_tag": "${{ steps.gate.outputs.create_tag }}",
        "version": "${{ steps.target.outputs.version }}",
        "tag": "${{ steps.target.outputs.tag }}",
    }


def test_release_gate_invokes_testable_script_with_runner_environment():
    gate = _named_step(_WORKFLOW["jobs"]["release"], "Tag-health gate")
    assert gate["env"] == {
        "GITHUB_BEFORE": "${{ github.event.before }}",
        "TAG": "${{ steps.target.outputs.tag }}",
        "VERSION": "${{ steps.target.outputs.version }}",
    }
    assert "git fetch --tags --force" in gate["run"]
    assert "scripts/release_gate.py" in gate["run"]


def test_tag_creation_and_github_release_are_idempotent():
    release = _WORKFLOW["jobs"]["release"]
    create_tag = _named_step(release, "Create and push the tag")
    assert create_tag["if"] == "steps.gate.outputs.create_tag == 'true'"
    notes = _named_step(release, "Publish release notes")["run"]
    assert 'gh release view "${TAG}"' in notes
    assert 'gh release create "${TAG}"' in notes


def test_build_job_uses_exact_tag_without_oidc():
    build = _WORKFLOW["jobs"]["build-release"]
    assert build["needs"] == "release"
    assert build["if"] == "needs.release.outputs.proceed == 'true'"
    assert build["permissions"] == {"contents": "read"}
    assert "id-token" not in build["permissions"]
    checkout = build["steps"][0]
    assert _action(checkout) == _CHECKOUT
    assert checkout["with"]["ref"] == "${{ needs.release.outputs.tag }}"


def test_build_job_builds_validates_and_uploads_one_artifact():
    build = _WORKFLOW["jobs"]["build-release"]
    assert "uv build" in _named_step(build, "Build distributions")["run"]
    assert "twine check" in _named_step(build, "Validate distributions")["run"]
    upload = _named_step(build, "Upload distributions")
    assert sum(_action(step) == _UPLOAD_ARTIFACT for step in build["steps"]) == 1
    assert _action(upload) == _UPLOAD_ARTIFACT
    assert upload["with"] == {
        "name": _ARTIFACT_NAME,
        "path": "dist/",
        "if-no-files-found": "error",
    }


def test_publish_job_is_oidc_only_and_waits_for_build():
    publish = _WORKFLOW["jobs"]["publish"]
    assert publish["needs"] == ["release", "build-release"]
    assert publish["if"] == "needs.release.outputs.proceed == 'true'"
    assert publish["environment"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}


def test_publish_job_only_downloads_and_publishes_pinned_artifact():
    publish = _WORKFLOW["jobs"]["publish"]
    assert len(publish["steps"]) == 2
    download, upload = publish["steps"]
    assert download["name"] == "Download distributions"
    assert _action(download) == _DOWNLOAD_ARTIFACT
    assert download["with"] == {"name": _ARTIFACT_NAME, "path": "dist/"}
    assert upload["name"] == "Publish distributions to PyPI"
    assert _action(upload) == _PYPI_PUBLISH
    assert upload["with"]["skip-existing"] is True
    assert all("run" not in step for step in publish["steps"])
