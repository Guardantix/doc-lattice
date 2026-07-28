"""Tests for fail-closed guard identity shared by the CI shell scanner and taint analysis."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

import pytest
from guard_witnesses import (
    CLASSIFIED_IDS,
    INVARIANT_IDS,
    INVARIANT_WITNESSES,
    REACHABLE_IDS,
    REACHABLE_WITNESSES,
    InvariantWitness,
    ReachableWitness,
)

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.shell_guards import (
    Certified,
    GuardRefusal,
    MarkerDetected,
    ScanLimits,
    ScannerLimits,
)
from doc_lattice.github_ci.shell_scanner import (
    ShellScanResult,
    _ScanBudget,
    _ShellScanIncomplete,
    _ShellScanner,
    direct_doc_lattice_invocations,
    scan_doc_lattice_invocations,
)
from doc_lattice.github_ci.shell_taint import (
    TaintLimits,
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

_ORIGIN_QUALNAMES = {
    record.origin_id: record.qualname for record in checker.repository_origin_records(_ROOT)
}


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
    result = scan_doc_lattice_invocations("a" * (ScannerLimits().max_source_chars + 1))

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
    script = "a" * (ScannerLimits().max_source_chars + 1)

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


def test_an_invariant_witness_cannot_omit_its_boundary_evidence_predicate() -> None:
    # A permissive default would satisfy the boundary-evidence assertion for any script at all,
    # which is the vacuous-classification hole that predicate exists to close.
    with pytest.raises(TypeError):
        InvariantWitness("taint.demo.x", "rationale", "echo hi")  # ty: ignore[missing-argument]


def test_shipped_guard_modules_use_only_canonical_refusal_shapes() -> None:
    assert checker.repository_shape_violations(_ROOT) == ()


def test_scan_limits_reach_the_public_boundary() -> None:
    script = "echo one; echo two; echo three\n"
    shrunk = ScanLimits(scanner=ScannerLimits(max_scan_steps=4))

    assert scan_doc_lattice_invocations(script).verdict == Certified()
    assert scan_doc_lattice_invocations(script, limits=shrunk).guard_id == (
        "scanner.budget.step-limit"
    )


def test_shrunk_source_cap_reaches_the_source_size_guard() -> None:
    shrunk = ScanLimits(scanner=ScannerLimits(max_source_chars=4))

    result = scan_doc_lattice_invocations("echo hello\n", limits=shrunk)

    assert result.guard_id == "scanner.source.character-limit"


def test_shrunk_taint_cap_reaches_a_taint_layer_guard() -> None:
    script = "X=safe; eval 'if true; then X=doc-; fi'; eval \"$X\"lattice"
    shrunk = ScanLimits(taint=TaintLimits(max_eval_reparse_branches=0))

    result = scan_doc_lattice_invocations(script, limits=shrunk)

    assert result.guard_id is not None
    assert result.guard_id.startswith("taint.")


def test_scan_limits_are_not_exposed_by_the_operator_entry_point() -> None:
    signature = inspect.signature(direct_doc_lattice_invocations)

    assert "limits" not in signature.parameters


def test_a_child_scanner_shares_the_parent_scan_limits() -> None:
    shrunk = ScanLimits(scanner=ScannerLimits(max_recursion_depth=1))
    scanner = _ShellScanner("echo hi", budget=_ScanBudget(limits=shrunk))

    child = scanner._child_scanner("echo child")

    assert child.budget is scanner.budget
    assert child.budget.limits is shrunk


@pytest.mark.parametrize(
    "witness",
    REACHABLE_WITNESSES,
    ids=[witness.origin_id for witness in REACHABLE_WITNESSES],
)
def test_reachable_witness_reaches_its_guard_origin(witness: ReachableWitness) -> None:
    result = scan_doc_lattice_invocations(witness.script, limits=witness.limits)

    assert result.guard_id == witness.origin_id


@pytest.mark.parametrize(
    "witness",
    [w for w in REACHABLE_WITNESSES if w.control_script is not None],
    ids=[w.origin_id for w in REACHABLE_WITNESSES if w.control_script is not None],
)
def test_reachable_witness_control_isolates_the_guard(witness: ReachableWitness) -> None:
    assert witness.control_script is not None
    control = scan_doc_lattice_invocations(witness.control_script)

    assert control.guard_id == witness.control_guard_id
    assert control.guard_id != witness.origin_id


@pytest.mark.parametrize(
    "witness",
    INVARIANT_WITNESSES,
    ids=[witness.origin_id for witness in INVARIANT_WITNESSES],
)
def test_invariant_boundary_witness_stops_short_of_its_guard(witness: InvariantWitness) -> None:
    result = scan_doc_lattice_invocations(witness.boundary_script)

    assert result.guard_id == witness.boundary_guard_id
    assert result.guard_id != witness.origin_id


def test_witnessed_ids_equal_the_reachable_classification() -> None:
    assert {witness.origin_id for witness in REACHABLE_WITNESSES} == REACHABLE_IDS


def test_invariant_boundary_rows_equal_the_invariant_classification() -> None:
    assert {witness.origin_id for witness in INVARIANT_WITNESSES} == INVARIANT_IDS


def test_every_witness_classifies_exactly_one_guard_origin() -> None:
    witnessed = [witness.origin_id for witness in REACHABLE_WITNESSES]
    invariant = [witness.origin_id for witness in INVARIANT_WITNESSES]

    assert len(witnessed) == len(set(witnessed))
    assert len(invariant) == len(set(invariant))


def test_an_unrecognized_verdict_projects_to_a_refusal_not_a_certification() -> None:
    # Fail-closed discipline: only Certified may project to "no refusal", so a verdict variant
    # added later cannot silently certify until someone updates this projection.
    unrecognized = "a verdict this projection does not know"
    result = ShellScanResult((), unrecognized)  # ty: ignore[invalid-argument-type]

    assert result.incomplete_reason is not None
    assert result.guard_id is None


def test_a_negative_step_budget_is_rejected_rather_than_treated_as_unset() -> None:
    with pytest.raises(ValueError, match="step budget"):
        _ScanBudget(-2)


def _guard_condition_lines() -> dict[str, tuple[str, int, int]]:
    """Map each guard identifier to its module, its condition line, and its refusal line.

    Line numbers are deliberately absent from the canonical origin record, which must stay stable
    across edits. They are re-derived here, from the current source, purely so a boundary witness
    can be held to executing the guard's own condition.
    """
    located: dict[str, tuple[str, int, int]] = {}
    for module in checker.GUARDED_MODULES:
        path = str(_ROOT / module)
        tree = ast.parse((_ROOT / module).read_text(encoding="utf-8"))
        parents = {
            id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "GuardRefusal" or not isinstance(node.args[0], ast.Constant):
                continue
            origin_id = node.args[0].value
            if not isinstance(origin_id, str):
                continue
            refusal_line = node.lineno
            current: ast.AST | None = node
            condition_line = 0
            while current is not None:
                current = parents.get(id(current))
                if isinstance(current, ast.If):
                    condition_line = current.test.lineno
                    break
                if isinstance(current, ast.FunctionDef):
                    # A fall-through refusal has no branch of its own: reaching it means every
                    # earlier arm declined. Its enclosing function's first executable statement,
                    # skipping the docstring, is the nearest line whose execution shows the walk
                    # ran over real evidence.
                    body = [
                        statement
                        for statement in current.body
                        if not (
                            isinstance(statement, ast.Expr)
                            and isinstance(statement.value, ast.Constant)
                            and isinstance(statement.value.value, str)
                        )
                    ]
                    condition_line = body[0].lineno
                    break
            located[origin_id] = (path, condition_line, refusal_line)
    return located


_GUARD_LINES = _guard_condition_lines()


def _boundary_evidence(script: str) -> Any:
    """Return the frozen taint evidence one boundary script produces."""
    scanner = _ShellScanner(script.replace("\r\n", "\n"))
    with contextlib.suppress(_ShellScanIncomplete):
        scanner.scan()
    assert scanner.taint_builder is not None
    return scanner.taint_builder.freeze()


_VACUOUS_CONTROL_SCRIPT = ""
"""An input that builds the evidence floor: one root scope, no command, pipe or resource.

Every structure a guard could inspect is absent here, so a boundary-evidence predicate that holds
for this control is not reporting anything the boundary script built.
"""


@pytest.mark.parametrize(
    "witness",
    INVARIANT_WITNESSES,
    ids=[witness.origin_id for witness in INVARIANT_WITNESSES],
)
def test_invariant_boundary_evidence_predicate_rejects_the_empty_control(
    witness: InvariantWitness,
) -> None:
    # Requiring the predicate to be non-empty is not enough: `lambda _: True` satisfies the
    # boundary-evidence assertion for any script, and the line trace only shows the condition was
    # evaluated, which holds for an unrelated certifying script because these guards sit in
    # validators every scan runs. A predicate that cannot tell the boundary apart from an input
    # that builds nothing supports no claim about the boundary, so it is rejected here.
    control = _boundary_evidence(_VACUOUS_CONTROL_SCRIPT)

    assert not witness.boundary_evidence(control), (
        f"{witness.origin_id}: boundary evidence predicate also holds for the empty control, so "
        f"it does not witness anything the boundary script built"
    )


def _executed_lines(script: str, path: str) -> set[int]:
    """Return the line numbers executed in one guarded module while scanning this script."""
    executed: set[int] = set()

    def trace(frame: Any, event: str, _argument: Any) -> Any:
        if frame.f_code.co_filename != path:
            return None
        if event == "line":
            executed.add(frame.f_lineno)
        return trace

    # Restore whatever was installed rather than clearing: under Python 3.13 coverage.py measures
    # through `sys.settrace`, so `sys.settrace(None)` would uninstall it for the rest of the
    # session and silently drop every line executed after this test. Python 3.14 measures through
    # `sys.monitoring` and is unaffected, which is why that failure is version-specific.
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        scan_doc_lattice_invocations(script)
    finally:
        sys.settrace(previous)
    return executed


@pytest.mark.parametrize(
    "witness",
    INVARIANT_WITNESSES,
    ids=[witness.origin_id for witness in INVARIANT_WITNESSES],
)
def test_invariant_boundary_witness_evaluates_the_condition_it_claims_is_never_true(
    witness: InvariantWitness,
) -> None:
    # Without this, an invariant row is prose: any certifying script satisfies a bare "the
    # boundary does not reach the guard" assertion, and every one of these guards sits in a
    # function that runs for every scan. The boundary must drive the guard's own condition, so
    # the claim is about a condition that was evaluated over real evidence and found false.
    path, condition_line, refusal_line = _GUARD_LINES[witness.origin_id]
    assert condition_line, f"{witness.origin_id} has no enclosing condition to witness"

    evidence = _boundary_evidence(witness.boundary_script)
    assert witness.boundary_evidence(evidence), (
        f"{witness.origin_id}: boundary script builds no evidence for this guard to inspect"
    )

    executed = _executed_lines(witness.boundary_script, path)

    assert condition_line in executed, (
        f"{witness.origin_id}: boundary script never evaluated the guard condition at "
        f"{path}:{condition_line}"
    )
    assert refusal_line not in executed, (
        f"{witness.origin_id}: boundary script reached the refusal at {path}:{refusal_line}"
    )
