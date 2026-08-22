"""Behavior tests for the deprecation-annotation auditor.

The script is loaded with `run_path` rather than imported, for the reason
`tests/test_extract_release_notes.py` does the same: `scripts/` is not a package and the
auditing workflow runs the file by path, so this exercises exactly what the runner executes.
"""

import subprocess
import sys
import types
from pathlib import Path
from runpy import run_path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _ROOT / "scripts" / "audit_action_runtimes.py"
_SCRIPT = run_path(str(_SCRIPT_PATH))

Annotation = _SCRIPT["Annotation"]
Finding = _SCRIPT["Finding"]
Job = _SCRIPT["Job"]
Run = _SCRIPT["Run"]
collect_findings = _SCRIPT["collect_findings"]
fetch_json = _SCRIPT["fetch_json"]
is_deprecation = _SCRIPT["is_deprecation"]
main = _SCRIPT["main"]
paginate = _SCRIPT["paginate"]
render_summary = _SCRIPT["render_summary"]

_REPOSITORY = "Guardantix/doc-lattice"
_RUN_ID = 32292367505
# The exact texts the 5.0.0 release run carried, kept verbatim: the matcher exists to separate
# these two, and paraphrasing either would let it drift away from what the runner writes.
_NODE_20_MESSAGE = (
    "Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to "
    "run on Node.js 24: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02. For "
    "more information see: "
    "https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/"
)
_CACHE_MESSAGE = (
    "Failed to save: Unable to reserve cache with key setup-uv-3-x86_64-unknown-linux-gnu, "
    "another job may be creating this cache."
)

_RUN_PAYLOAD = {
    "id": _RUN_ID,
    "name": "CI",
    "html_url": f"https://github.com/{_REPOSITORY}/actions/runs/{_RUN_ID}",
    "event": "push",
    "head_branch": "main",
    "run_attempt": 1,
}


def _job_payload(job_id: int, name: str, status: str = "completed") -> dict:
    return {
        "id": job_id,
        "name": name,
        "html_url": f"https://github.com/{_REPOSITORY}/actions/runs/{_RUN_ID}/job/{job_id}",
        "status": status,
        "conclusion": "success" if status == "completed" else None,
    }


def _annotation_payload(level: str, message: str) -> dict:
    return {
        "annotation_level": level,
        "message": message,
        "path": ".github",
        "start_line": 2,
        "end_line": 2,
    }


def _job(job_id: int, name: str, status: str = "completed"):
    payload = _job_payload(job_id, name, status)
    return Job(
        id=payload["id"],
        name=payload["name"],
        html_url=payload["html_url"],
        status=payload["status"],
        conclusion=payload["conclusion"],
    )


def _annotation(level: str, message: str):
    return Annotation(level=level, message=message, path=".github", start_line=2)


def _run_fixture():
    return Run(
        name="CI",
        run_id=_RUN_ID,
        html_url=_RUN_PAYLOAD["html_url"],
        event="push",
        head_branch="main",
        run_attempt=1,
    )


def _fake_api(jobs: list[dict], annotations: dict[int, list[dict]], run: dict | None = None):
    """Return a transport over canned payloads and the list recording every path it was asked."""
    calls: list[str] = []

    def fetch(path: str) -> object:
        calls.append(path)
        base = path.partition("?")[0]
        if base.endswith("/jobs"):
            return {"jobs": jobs}
        if "/check-runs/" in base:
            job_id = int(base.split("/check-runs/", 1)[1].split("/", 1)[0])
            return annotations.get(job_id, [])
        if base.endswith(f"/actions/runs/{_RUN_ID}"):
            return run if run is not None else _RUN_PAYLOAD
        raise AssertionError(f"unexpected path {path}")

    return fetch, calls


@pytest.mark.parametrize(
    ("level", "message", "expected"),
    [
        ("warning", "This action is deprecated and will be removed.", True),
        ("failure", "Deprecation: the runner image is going away.", True),
        ("notice", "This action is deprecated and will be removed.", False),
        ("warning", "Unable to reach the cache service.", False),
        ("warning", _NODE_20_MESSAGE, True),
        ("warning", _CACHE_MESSAGE, False),
    ],
)
def test_is_deprecation_separates_runtime_notices_from_ordinary_noise(level, message, expected):
    # If this stops holding the audit is either silent on a real runtime deprecation or red on
    # every run that lost a cache race, and a gate that is always red gets ignored.
    assert is_deprecation(_annotation(level, message)) is expected


def test_collect_findings_pairs_each_matching_annotation_with_its_job():
    # The report names an action *and* the job that ran it, which is what points a maintainer at
    # the workflow to edit. Losing the pairing would leave a list of messages with no location.
    build = _job(96196396251, "Build release distributions")
    publish = _job(96196460418, "Publish to PyPI")
    tests = _job(96195879591, "Tests (3.13)")

    findings = collect_findings(
        [
            (tests, [_annotation("warning", _CACHE_MESSAGE)]),
            (build, [_annotation("warning", _NODE_20_MESSAGE)]),
            (
                publish,
                [
                    _annotation("notice", "Generating and uploading digital attestations"),
                    _annotation("warning", _NODE_20_MESSAGE),
                ],
            ),
        ]
    )

    assert [finding.job.name for finding in findings] == [
        "Build release distributions",
        "Publish to PyPI",
    ]
    assert all(finding.annotation.message == _NODE_20_MESSAGE for finding in findings)


def test_collect_findings_returns_nothing_for_a_clean_run():
    tests = _job(96195879591, "Tests (3.13)")

    assert collect_findings([(tests, [_annotation("warning", _CACHE_MESSAGE)])]) == []


def test_render_summary_reports_a_clean_run_without_a_table():
    # A clean run still writes a summary. Without it there is no way to tell an audit that found
    # nothing from an audit that never ran.
    summary = render_summary(_run_fixture(), [], 12)

    assert summary.startswith(f"## Action runtime audit: CI run {_RUN_ID}\n")
    assert f"[View the source run]({_RUN_PAYLOAD['html_url']})" in summary
    assert "event `push`, branch `main`, attempt 1." in summary
    assert "Jobs audited: 12." in summary
    assert "No deprecation annotations." in summary
    assert "| Job | Level | Annotation |" not in summary
    assert "RELEASING.md" not in summary
    assert summary.endswith("\n")


def test_render_summary_tables_every_finding_and_points_at_the_bump_procedure():
    # The table is the whole report: each row has to link the job so the log is one click away,
    # and the pointer paragraph is what tells a reader whether the fix is a workflow edit or the
    # coupled multi-file edit the shipped pins require.
    build = _job(96196396251, "Build release distributions")
    publish = _job(96196460418, "Publish to PyPI")
    findings = [
        Finding(job=build, annotation=_annotation("warning", _NODE_20_MESSAGE)),
        Finding(job=publish, annotation=_annotation("warning", _NODE_20_MESSAGE)),
    ]

    summary = render_summary(_run_fixture(), findings, 12)
    lines = summary.splitlines()

    assert "| Job | Level | Annotation |" in lines
    assert "| --- | --- | --- |" in lines
    rows = [line for line in lines if line.startswith("| [")]
    assert len(rows) == 2
    assert rows[0] == (
        f"| [Build release distributions]({build.html_url}) | warning | {_NODE_20_MESSAGE} |"
    )
    assert rows[1] == f"| [Publish to PyPI]({publish.html_url}) | warning | {_NODE_20_MESSAGE} |"
    assert "actions/checkout" in summary
    assert "astral-sh/setup-uv" in summary
    assert "Keeping the action pins current" in summary
    assert "RELEASING.md" in summary
    assert "No deprecation annotations." not in summary


def test_render_summary_escapes_pipes_and_newlines_in_annotation_text():
    # Annotation text is upstream prose. An unescaped pipe would end the cell early and quietly
    # shift every later column, so a report could misattribute a level to the wrong job.
    build = _job(96196396251, "Build release distributions")
    finding = Finding(
        job=build, annotation=_annotation("warning", "deprecated: a | b\nsecond line")
    )

    summary = render_summary(_run_fixture(), [finding], 1)

    assert r"deprecated: a \| b second line |" in summary


def test_paginate_follows_pages_until_one_comes_back_short():
    # A run with more than a hundred jobs is not hypothetical for a matrixed workflow, and a
    # single-page read would audit the first hundred and report the rest as clean.
    calls: list[str] = []
    pages = {1: [{"id": index} for index in range(100)], 2: [{"id": index} for index in range(37)]}

    def fetch(path: str) -> object:
        calls.append(path)
        page = int(path.rsplit("page=", 1)[1])
        return {"jobs": pages[page]}

    items = paginate(fetch, "/repos/o/r/actions/runs/1/jobs", "jobs")

    assert calls == [
        "/repos/o/r/actions/runs/1/jobs?per_page=100&page=1",
        "/repos/o/r/actions/runs/1/jobs?per_page=100&page=2",
    ]
    assert len(items) == 137


def test_paginate_reads_a_bare_array_endpoint_in_one_request():
    # The annotations endpoint returns the array itself rather than wrapping it, so asking for a
    # key there would fail on every job.
    calls: list[str] = []

    def fetch(path: str) -> object:
        calls.append(path)
        return [{"annotation_level": "warning"}]

    assert paginate(fetch, "/repos/o/r/check-runs/1/annotations") == [
        {"annotation_level": "warning"}
    ]
    assert calls == ["/repos/o/r/check-runs/1/annotations?per_page=100&page=1"]


def test_main_exits_one_and_writes_the_summary_when_a_deprecation_is_found(
    tmp_path, monkeypatch, capsys
):
    # Exit 1 is the notice. A run that found a deprecated runtime and still exited 0 would leave
    # the workflow green and the finding unread.
    summary_path = tmp_path / "step-summary.md"
    summary_path.touch()
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    fetch, _ = _fake_api(
        jobs=[_job_payload(1, "Build release distributions")],
        annotations={1: [_annotation_payload("warning", _NODE_20_MESSAGE)]},
    )

    code = main(["--repository", _REPOSITORY, "--run-id", str(_RUN_ID)], fetch)

    assert code == 1
    captured = capsys.readouterr()
    assert "| [Build release distributions]" in captured.out
    assert summary_path.read_text(encoding="utf-8") == captured.out
    # The plain stderr line is what makes the run readable from the log alone, without opening
    # the summary tab.
    assert "Build release distributions: warning: .github:2:" in captured.err


def test_main_exits_zero_on_a_clean_run(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fetch, _ = _fake_api(
        jobs=[_job_payload(1, "Tests (3.13)")],
        annotations={1: [_annotation_payload("warning", _CACHE_MESSAGE)]},
    )

    code = main(["--repository", _REPOSITORY, "--run-id", str(_RUN_ID)], fetch)

    assert code == 0
    captured = capsys.readouterr()
    assert "No deprecation annotations." in captured.out
    assert captured.err == ""


def test_main_skips_jobs_that_have_not_completed(monkeypatch, capsys):
    # An unfinished job's check run has no settled annotations. Reading one anyway costs a
    # request per job and can report a run as clean before the runner has written its warnings.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fetch, calls = _fake_api(
        jobs=[
            _job_payload(1, "Tests (3.13)"),
            _job_payload(2, "Action runtime audit", status="in_progress"),
        ],
        annotations={1: [], 2: [_annotation_payload("warning", _NODE_20_MESSAGE)]},
    )

    code = main(["--repository", _REPOSITORY, "--run-id", str(_RUN_ID)], fetch)

    assert code == 0
    assert any("/check-runs/1/annotations" in call for call in calls)
    assert not any("/check-runs/2/annotations" in call for call in calls)
    assert "Jobs audited: 1." in capsys.readouterr().out


def test_main_reports_an_uninterpretable_payload_as_a_failed_audit(monkeypatch, capsys):
    # Exit 2 keeps "the audit could not run" distinct from "the audit found something", so a
    # broken transport never reads as a deprecation and never reads as a clean run either.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fetch, _ = _fake_api(jobs=[], annotations={}, run={"id": _RUN_ID, "name": "CI"})

    code = main(["--repository", _REPOSITORY, "--run-id", str(_RUN_ID)], fetch)

    assert code == 2
    assert "::error::" in capsys.readouterr().err


def test_main_requires_a_repository_when_the_environment_names_none(monkeypatch):
    # The runner always sets GITHUB_REPOSITORY, so this is the local-invocation path. Falling
    # back to a guess would audit some other repository's run and report it as this one's.
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    fetch, _ = _fake_api(jobs=[], annotations={})

    with pytest.raises(SystemExit) as error:
        main(["--run-id", str(_RUN_ID)], fetch)

    assert error.value.code == 2


def test_fetch_json_exits_two_and_surfaces_gh_stderr_on_failure(monkeypatch, capsys):
    # A 404, a missing token, or a rate limit must not look like a clean audit. Exit 2 with gh's
    # own message is the only thing separating "no deprecations" from "never asked".
    def fake_run(*_args, **_kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as error:
        fetch_json("/repos/o/r/actions/runs/1")

    assert error.value.code == 2
    assert "gh: Not Found (HTTP 404)" in capsys.readouterr().err


def test_fetch_json_decodes_the_body_gh_prints(monkeypatch):
    def fake_run(*_args, **_kwargs):
        return types.SimpleNamespace(returncode=0, stdout='{"id": 7}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert fetch_json("/repos/o/r/actions/runs/7") == {"id": 7}


def test_script_rejects_a_missing_run_id_at_the_command_line():
    # The workflow invokes the file by path, so the argument contract has to hold for a real
    # process rather than only for an in-process call: an empty RUN_ID must fail the step.
    result = subprocess.run(  # noqa: S603 - controlled test interpreter arguments
        (sys.executable, str(_SCRIPT_PATH), "--repository", _REPOSITORY),
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--run-id" in result.stderr
