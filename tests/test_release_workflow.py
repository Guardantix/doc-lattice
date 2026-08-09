"""Contract tests for release and PyPI publishing automation.

These assert which action each release step calls, not the commit it is pinned to.
`tests/test_workflow_pinning.py` owns the supply-chain rule that every `uses:` in every
workflow resolves to a 40-character commit SHA, so restating individual pins here would
only force a lockstep edit on every routine pin refresh.
"""

import shlex
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


def _commands(step: dict) -> str:
    """Return a step's executable command lines, dropping blanks and commented-out text."""
    return "\n".join(
        stripped
        for line in step.get("run", "").splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )


def _out_dir(command: str) -> str | None:
    """Return a command's ``--out-dir`` value, or None when the flag is absent."""
    words = shlex.split(command)
    for index, word in enumerate(words):
        if word.startswith("--out-dir="):
            return word.split("=", 1)[1]
        if word == "--out-dir":
            return words[index + 1] if index + 1 < len(words) else ""
    return None


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
    steps = _WORKFLOW["jobs"]["release"]["steps"]
    gate_index = next(i for i, step in enumerate(steps) if step.get("name") == "Tag-health gate")
    assert steps[gate_index]["env"] == {
        "GITHUB_BEFORE": "${{ github.event.before }}",
        "TAG": "${{ steps.target.outputs.tag }}",
        "VERSION": "${{ steps.target.outputs.version }}",
    }
    # release_gate.py resolves refs/tags/<tag> from local state, so a tag created remotely
    # since checkout is invisible unless the fetch runs first. Steps in a job run in order,
    # so the fetch may live in the gate step or in any step before it. Anchor both matches to
    # whole command lines: text quoted inside an echo would satisfy a bare substring search
    # while neither command ever runs, leaving the gate step to skip publishing silently.
    lines = [line for step in steps[: gate_index + 1] for line in _commands(step).splitlines()]
    fetches = [i for i, line in enumerate(lines) if line.startswith("git fetch --tags --force")]
    gates = [i for i, line in enumerate(lines) if line.endswith("scripts/release_gate.py")]
    assert fetches
    assert gates
    assert fetches[0] < gates[0]


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
    # RELEASING.md requires publishing a wheel and a source distribution and validating both.
    # Both commands cover both formats when given no format argument, so naming one format is
    # only acceptable when the other is named too, as RELEASING.md's own invocations do.
    build_run = _commands(_named_step(build, "Build distributions"))
    assert any(line.startswith("uv build") for line in build_run.splitlines())
    assert ("--wheel" in build_run) == ("--sdist" in build_run)
    # The upload step below publishes `dist/` with `if-no-files-found: error`, so the build has
    # to write there. Compare the whole argument: a prefix check accepts `--out-dir dist-old`
    # and strands every distribution outside the uploaded directory.
    assert _out_dir(build_run) in (None, "dist")
    validate = _named_step(build, "Validate distributions")
    validate_run = _commands(validate)
    assert "twine check" in validate_run
    assert (".whl" in validate_run) == (".tar.gz" in validate_run)
    # twine is neither a project dependency nor preinstalled, so a bare `twine check` fails on a
    # clean runner. Any provisioning route satisfies this; running the unprovisioned binary does
    # not. Adding twine to the dev group means switching the call to `uv run` here too.
    twine_line = next(line for line in validate_run.splitlines() if "twine check" in line)
    earlier = build["steps"][: build["steps"].index(validate)]
    installs = "\n".join(_commands(step) for step in earlier)
    assert twine_line.startswith(("uvx --from twine", "uv run", "uv tool run")) or (
        "install twine" in installs
    )
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
