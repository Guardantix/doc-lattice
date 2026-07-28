"""Tests for fail-closed guard identity shared by the CI shell scanner and taint analysis."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

import pytest
from guard_witnesses import (
    CLASSIFIED_IDS,
    INVARIANT_IDS,
    INVARIANT_WITNESSES,
    REACHABLE_IDS,
)

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

_ROOT = Path(__file__).resolve().parents[1]


def _load_checker() -> ModuleType:
    path = _ROOT / "scripts/check_guard_inventory.py"
    spec = importlib.util.spec_from_file_location("check_guard_inventory", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


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


def test_guard_origins_partition_into_the_registry_and_frozen_debt() -> None:
    source_ids = {record.origin_id for record in checker.repository_origin_records(_ROOT)}
    debt_ids = {record.origin_id for record in checker.load_debt_records(_ROOT)}

    assert not CLASSIFIED_IDS & debt_ids
    assert source_ids == CLASSIFIED_IDS | debt_ids


def test_reachable_and_invariant_classifications_are_disjoint() -> None:
    assert not REACHABLE_IDS & INVARIANT_IDS


def test_frozen_debt_records_still_describe_real_guard_origins() -> None:
    source_records = set(checker.repository_origin_records(_ROOT))

    assert set(checker.load_debt_records(_ROOT)) <= source_records


def test_every_invariant_witness_carries_a_rationale() -> None:
    for witness in INVARIANT_WITNESSES:
        assert witness.rationale.strip(), witness.origin_id
        assert witness.boundary_script.strip(), witness.origin_id


def test_shipped_guard_modules_use_only_canonical_refusal_shapes() -> None:
    assert checker.repository_shape_violations(_ROOT) == ()
