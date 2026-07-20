"""Predeclared evaluation gates for the issue #100 recognizer candidate (spec gates 1-6, 8, 9)."""

import json
from pathlib import Path

import pytest

from doc_lattice.github_ci.direct_marker_scanner import scan_execution_source

CHECKPOINT = Path("tests/fixtures/github_ci_checkpoint")

_LABELS = json.loads((CHECKPOINT / "acceptance_labels.json").read_text())["cases"]


def _acceptance_cases():
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    return ACCEPTANCE_CASES


@pytest.mark.parametrize("index", range(78), ids=[row["description"] for row in _LABELS])
def test_gate1_acceptance_label_conformance(index):
    row = _LABELS[index]
    description, script, _expected = _acceptance_cases()[index]
    assert row["description"] == description
    result = scan_execution_source(script)
    assert result.status == row["expected_status"], (description, result.reason)
    assert [list(i) for i in result.invocations] == row["expected_invocations"], description
    if row["expected_status"] == "uninspectable":
        assert result.reason_category == row["reason_category"], (
            description,
            result.offset,
            result.reason,
        )


def test_gate2_replay_divergences_stay_in_predeclared_categories():
    from github_ci_evaluation_harness import replay_records  # noqa: PLC0415

    records = replay_records()
    assert len(records) == 580 + 13 + 20
    allowed = {"identical", "intentional-exit-2", "outside-direct-marker"}
    unexplained = [r for r in records if r["category"] == "unexplained"]
    assert unexplained == [], unexplained[:5]
    category_d = [r["id"] for r in records if r["category"] == "old-incomplete-new-certified"]
    prelabeled = json.loads((CHECKPOINT / "category_d_exceptions.json").read_text())
    assert category_d == prelabeled == []
    assert {r["category"] for r in records} <= allowed


def test_gate3_tier1_offline_template_certifies(tmp_path):
    from github_ci_evaluation_harness import evaluate_workflow, load_tier3a_cases  # noqa: PLC0415

    from doc_lattice.github_ci.render import render_workflows  # noqa: PLC0415
    from doc_lattice.github_ci.workflow_parser import parse_workflow  # noqa: PLC0415

    offline, _linear = render_workflows("OWNER/REPO", "2.0.0")
    target = tmp_path / "offline.yml"
    target.write_text(offline.text)
    document = parse_workflow(target, target.read_text())
    evaluation = evaluate_workflow(document)

    assert evaluation.diagnostics == ()
    scans = [e for e in evaluation.evaluations if e.source_kind == "run_body"]
    assert len(scans) == 1
    assert scans[0].scan.status == "certified"
    assert scans[0].scan.invocations == (("ci", False), ("check", False), ("lint", False))

    frozen = next(case for case in load_tier3a_cases() if case["id"] == "offline-template-block")
    runs = [step.run for job in document.jobs for step in job.steps if step.run is not None]
    assert runs[0].strip() == frozen["source"].strip()


def test_gate4_tier2_repository_workflow_is_clean():
    from github_ci_evaluation_harness import evaluate_workflow  # noqa: PLC0415

    from doc_lattice.github_ci.workflow_parser import parse_workflow  # noqa: PLC0415

    path = Path(".github/workflows/ci.yml")
    document = parse_workflow(path, path.read_text())
    evaluation = evaluate_workflow(document)

    assert "release" in evaluation.pruned_jobs
    assert evaluation.diagnostics == ()
    assert all(e.scan.status == "not_applicable" for e in evaluation.evaluations)


def _tier3a_ids():
    from github_ci_evaluation_harness import load_tier3a_cases  # noqa: PLC0415

    return [case["id"] for case in load_tier3a_cases()]


@pytest.mark.parametrize("case_id", _tier3a_ids())
def test_gate5_tier3a_documented_conformance(case_id):
    from github_ci_evaluation_harness import load_tier3a_cases  # noqa: PLC0415

    case = next(c for c in load_tier3a_cases() if c["id"] == case_id)
    result = scan_execution_source(case["source"])
    assert result.status == case["expected_status"], (case_id, result.reason)
    assert [list(i) for i in result.invocations] == case["expected_invocations"], case_id
    if case["expected_status"] == "uninspectable":
        assert result.reason_category == case["reason_category"], case_id


TIER3B_BUDGET_TOTAL = 2
TIER3B_BUDGET_NEWLY = 2
TIER3B_VERDICT = "rejected"


def test_gate6_tier3b_conformance_and_recorded_verdict():
    from github_ci_evaluation_harness import (  # noqa: PLC0415
        load_tier3b_provenance,
        old_scan,
        tier3b_run_block,
    )

    fixtures = load_tier3b_provenance()["fixtures"]
    assert len(fixtures) == 20
    indeterminate = []
    newly_indeterminate = []
    false_safe = []
    false_positive = []
    for row in fixtures:
        source = tier3b_run_block(row["id"])
        new = scan_execution_source(source)
        old = old_scan(source)
        assert new.status == row["expected_status"], (row["id"], new.reason)
        assert [list(i) for i in new.invocations] == row["expected_invocations"], row["id"]
        if row["expected_status"] == "uninspectable":
            assert new.reason_category == row["reason_category"], row["id"]
        if new.status == "uninspectable":
            indeterminate.append(row["id"])
            if old.certified:
                newly_indeterminate.append(row["id"])
        if new.status == "certified" and row["expected_status"] == "uninspectable":
            false_safe.append(row["id"])
        if [list(i) for i in new.invocations] != row["expected_invocations"]:
            false_positive.append(row["id"])

    assert sorted(indeterminate) == ["fixture-02", "fixture-05", "fixture-14"]
    assert sorted(newly_indeterminate) == ["fixture-02", "fixture-05", "fixture-14"]
    assert false_safe == []
    assert false_positive == []

    # The owner-adjudicated recorded verdict (ruling 1a): both predeclared budgets breach,
    # so the D3 candidate is rejected by the evaluation. This gate asserts that recorded
    # outcome; a passing budget here would itself be evaluation drift.
    assert len(indeterminate) > TIER3B_BUDGET_TOTAL
    assert len(newly_indeterminate) > TIER3B_BUDGET_NEWLY
    assert TIER3B_VERDICT == "rejected"
