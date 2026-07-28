"""Tests for fail-closed guard identity shared by the CI shell scanner and taint analysis."""

from __future__ import annotations

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.shell_guards import (
    Certified,
    GuardRefusal,
    MarkerDetected,
)
from doc_lattice.github_ci.shell_scanner import (
    _MAX_SHELL_SOURCE_CHARS,
    _ShellScanIncomplete,
    direct_doc_lattice_invocations,
    scan_doc_lattice_invocations,
)
from doc_lattice.github_ci.shell_taint import (
    _MalformedTaintEvidence,
    _TaintLimitExceeded,
)


def test_guard_refusal_carries_a_stable_origin_id_and_reason() -> None:
    refusal = GuardRefusal("scanner.source.character-limit", "source character limit exceeded")

    assert refusal.origin_id == "scanner.source.character-limit"
    assert refusal.reason == "source character limit exceeded"


def test_guard_refusal_is_immutable() -> None:
    refusal = GuardRefusal("scanner.source.character-limit", "source character limit exceeded")

    with pytest.raises(AttributeError):
        refusal.origin_id = "scanner.other"  # ty: ignore[invalid-assignment]


def test_certified_and_marker_detected_carry_no_guard_id() -> None:
    assert not hasattr(Certified(), "origin_id")
    assert not hasattr(MarkerDetected(), "origin_id")


def test_certified_and_marker_detected_are_singleton_equal_values() -> None:
    assert Certified() == Certified()
    assert MarkerDetected() == MarkerDetected()
    assert Certified() != MarkerDetected()


def test_fail_closed_exceptions_reject_raw_refusal_text() -> None:
    for exception_type in (_TaintLimitExceeded, _MalformedTaintEvidence, _ShellScanIncomplete):
        with pytest.raises(TypeError):
            exception_type("shell taint edge limit exceeded")  # ty: ignore[invalid-argument-type]


def test_fail_closed_exceptions_retain_the_origin_refusal() -> None:
    refusal = GuardRefusal("taint.edges.limit", "shell taint edge limit exceeded")

    error = _TaintLimitExceeded(refusal)

    assert error.refusal is refusal
    assert str(error) == "shell taint edge limit exceeded"


def test_certified_script_reports_a_certified_verdict() -> None:
    result = scan_doc_lattice_invocations("echo hello\n")

    assert result.verdict == Certified()
    assert result.incomplete_reason is None
    assert result.guard_id is None


def test_source_size_guard_reports_its_origin_id_through_the_public_path() -> None:
    result = scan_doc_lattice_invocations("a" * (_MAX_SHELL_SOURCE_CHARS + 1))

    assert result.verdict == GuardRefusal(
        "scanner.source.character-limit", "source character limit exceeded"
    )
    assert result.guard_id == "scanner.source.character-limit"
    assert result.incomplete_reason == "source character limit exceeded"


def test_marker_flow_reports_a_guard_free_marker_verdict() -> None:
    result = scan_doc_lattice_invocations(
        "X=safe; eval 'if true; then X=doc-; fi'; eval \"$X\"lattice"
    )

    assert result.verdict == MarkerDetected()
    assert result.guard_id is None
    assert result.incomplete_reason == "authored marker flow reaches an execution sink"


def test_scanner_control_flow_guard_preserves_its_origin_id() -> None:
    result = scan_doc_lattice_invocations("if true; fi\n")

    assert result.guard_id == "scanner.control-flow.unfinished-if"
    assert result.incomplete_reason == "unfinished if control flow"


def test_incomplete_reason_projection_keeps_the_config_error_wording() -> None:
    script = "a" * (_MAX_SHELL_SOURCE_CHARS + 1)

    with pytest.raises(ConfigError, match=r"^shell scan incomplete: source character limit"):
        direct_doc_lattice_invocations(script)
    with pytest.raises(ConfigError, match=r"^wf\.yml: shell scan incomplete: source character"):
        direct_doc_lattice_invocations(script, context="wf.yml")
