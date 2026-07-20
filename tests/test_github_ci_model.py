"""Tests for GitHub CI model dataclasses."""

import pytest

from doc_lattice.github_ci.model import (
    AuditDiagnostic,
    AuditResult,
    BlockScan,
    diagnostic_sort_key,
)


def test_block_scan_invariants():
    BlockScan("not_applicable", (), None, None, None, 12)
    BlockScan("certified", (("check", False),), None, None, None, 30)
    BlockScan("uninspectable", (), "unsupported-operator", "unquoted pipe", 4, 9)
    BlockScan("uninspectable", (("check", False),), "unsupported-operator", "unquoted pipe", 20, 40)
    with pytest.raises(ValueError, match="not_applicable blocks carry no invocations"):
        BlockScan("not_applicable", (("check", False),), None, None, None, 1)
    with pytest.raises(ValueError, match="not_applicable blocks carry no invocations"):
        BlockScan("not_applicable", (), None, "reason", None, 1)
    with pytest.raises(ValueError, match="certified blocks carry no reason"):
        BlockScan("certified", (), None, "reason", None, 1)
    with pytest.raises(ValueError, match="certified blocks carry no reason"):
        BlockScan("certified", (), "unsupported-operator", None, None, 1)
    with pytest.raises(ValueError, match="uninspectable blocks require reason"):
        BlockScan("uninspectable", (), None, None, None, 1)
    with pytest.raises(ValueError, match="uninspectable blocks require reason"):
        BlockScan("uninspectable", (), "unsupported-operator", "reason", None, 1)
    with pytest.raises(ValueError, match="uninspectable blocks require reason"):
        BlockScan("uninspectable", (), None, "reason", 3, 1)


def test_diagnostic_sort_key_orders_missing_offsets_first():
    with_offset = AuditDiagnostic(
        "a.yml", "job", 0, "run_body", "UNINSPECTABLE_SOURCE", "reason", 7
    )
    without_offset = AuditDiagnostic(
        "a.yml", "job", 0, "run_body", "UNINSPECTABLE_SOURCE", "reason", None
    )
    ordered = sorted([with_offset, without_offset], key=diagnostic_sort_key)
    assert ordered == [without_offset, with_offset]


def test_audit_result_holds_both_lists():
    result = AuditResult(findings=(), diagnostics=())
    assert result.findings == ()
    assert result.diagnostics == ()
