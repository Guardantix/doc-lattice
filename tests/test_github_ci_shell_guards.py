"""Tests for fail-closed guard identity shared by the CI shell scanner and taint analysis."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
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
from doc_lattice.github_ci import shell_scanner
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
    CommandOutput,
    SequenceOutput,
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


def test_an_unrecognized_verdict_does_not_certify_at_the_public_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `ShellScanResult` fails closed on a verdict it does not recognize, but the scan boundary
    # decides what verdict the result carries: matching `MarkerDetected` positively and returning
    # normally otherwise lets a `ScanVerdict` member added later reach a boundary that mints
    # `Certified()`, certifying a run body the taint analysis did not certify.

    class _Unrecognized:
        pass

    monkeypatch.setattr(shell_scanner, "analyze_marker_taint", lambda *_, **__: _Unrecognized())

    result = scan_doc_lattice_invocations('X=doc-; eval "$X"lattice')

    assert not isinstance(result.verdict, Certified)
    assert result.incomplete_reason is not None
    with pytest.raises(ConfigError, match="shell scan incomplete"):
        direct_doc_lattice_invocations('X=doc-; eval "$X"lattice')


def _guard_condition_lines() -> dict[str, tuple[str, int, int]]:
    """Map each guard identifier to its module, its condition line, and its refusal line.

    Line numbers are deliberately absent from the canonical origin record, which must stay stable
    across edits. They are re-derived here, from the current source, purely so a boundary witness
    can be held to executing the guard's own condition.

    Every construct the checker treats as deciding a refusal is a stopping point here, for the same
    reason the checker gives: a `while` test governs the guard exactly as an `if` test does, a
    `for` header decides whether the body runs at all, a `match` arm selects it, and an `except`
    clause is reached only by the `try` body raising. Walking past any of them to the enclosing
    function's first line would attribute a line that runs for any script entering the function, so
    a boundary script could satisfy the trace assertion without the guard's own condition ever
    being evaluated. Three frozen origins sit in `except` handlers today, so this is the difference
    between a real witness and a vacuous one the moment one of them is classified.
    """
    located: dict[str, tuple[str, int, int]] = {}
    for module in checker.GUARDED_MODULES:
        path = str(_ROOT / module)
        tree = ast.parse((_ROOT / module).read_text(encoding="utf-8"))
        parents = {
            id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        transports = _declared_transport_parameters(tree, Path(module).name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "GuardRefusal" or not node.args:
                continue
            if not isinstance(node.args[0], ast.Constant):
                continue
            origin_id = node.args[0].value
            if not isinstance(origin_id, str):
                continue
            transported = _transported_refusal_lines(node, parents, transports)
            lines = transported or (_governing_line(node, parents), node.lineno)
            located[origin_id] = (path, *lines)
    return located


def _declared_transport_parameters(
    tree: ast.Module, module: str
) -> dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Return each declared transport this module defines as a receiving function, by its name.

    A declared transport is a site that propagates a refusal it did not mint. Most of them raise one
    they were handed by a caller further out, but `_validate_acyclic_graph` is handed one as an
    argument and owns the condition that decides it, which is what makes its callers the origins.
    Only that form is resolvable from the origin's side, so a transport qualifies here when it takes
    a parameter of the declared argument name.

    Args:
        tree: Parsed guarded module.
        module: Its file name, used to select the declarations that describe it.

    Returns:
        The transport definition and its refusal parameter name, keyed by the callee name an origin
        statement spells.
    """
    declared = {
        qualname.rsplit(".", 1)[-1]: argument
        for declared_module, qualname, argument in checker.DECLARED_TRANSPORTS
        if declared_module == module
    }
    found: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        argument = declared.get(node.name)
        if argument is None:
            continue
        parameters = {
            parameter.arg
            for group in (node.args.posonlyargs, node.args.args, node.args.kwonlyargs)
            for parameter in group
        }
        if argument in parameters:
            found[node.name] = (node, argument)
    return found


def _transported_refusal_lines(
    node: ast.Call,
    parents: dict[int, ast.AST],
    transports: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]],
) -> tuple[int, int] | None:
    """Return the condition and refusal lines inside the transport this refusal is handed to.

    A refusal constructed as an argument to a transport is built unconditionally: reaching the call
    runs the construction whether or not the transport goes on to raise it. Taking that line as the
    refusal line makes the assertion that a boundary script does not reach it unsatisfiable, and the
    condition line walked from it names the caller's first statement rather than anything about the
    guard. Both lines belong to the transport, which is where this guard's condition is evaluated
    and where its refusal is raised.

    Args:
        node: The refusal construction.
        parents: Parent map over the module holding it.
        transports: Receiving transports the module defines, by callee name.

    Returns:
        The transport's condition and refusal lines, or `None` when this refusal is not transported.
    """
    current: ast.AST | None = node
    while current is not None:
        holder = parents.get(id(current))
        if isinstance(holder, ast.Call):
            name = holder.func.id if isinstance(holder.func, ast.Name) else None
            resolved = transports.get(name) if name is not None else None
            if resolved is not None:
                transport, argument = resolved
                inner = {
                    id(child): parent
                    for parent in ast.walk(transport)
                    for child in ast.iter_child_nodes(parent)
                }
                raised = next(
                    (
                        statement
                        for statement in ast.walk(transport)
                        if isinstance(statement, ast.Raise)
                        if statement.exc is not None
                        if argument
                        in {
                            reference.id
                            for reference in ast.walk(statement.exc)
                            if isinstance(reference, ast.Name)
                        }
                    ),
                    None,
                )
                if raised is not None:
                    return _governing_line(raised, inner), raised.lineno
            return None
        if isinstance(holder, ast.stmt) or holder is None:
            return None
        current = holder
    return None


def _first_executable_line(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return the line of the function's first statement that is not its docstring."""
    body = [
        statement
        for statement in function.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    return body[0].lineno


def _handled_operation_line(handler: ast.ExceptHandler, parents: dict[int, ast.AST]) -> int:
    """Return the line of the operation whose failure is the only condition a handler guard has.

    A handler has no condition of its own: it is reached only by the `try` body raising, which is
    the same thing the checker records as this guard's raising operation. The boundary runs that
    operation and it does not raise, so the guarded body is the line to witness. Taking the
    handler's own line instead would be unsatisfiable, because entering the handler is what runs
    the refusal.

    Args:
        handler: The `except` clause holding the origin.
        parents: Parent map over the module holding it.

    Returns:
        The first line of the guarded `try` body.
    """
    guarded = parents.get(id(handler))
    if isinstance(guarded, ast.Try | ast.TryStar):
        return guarded.body[0].lineno
    return handler.lineno


def _governing_line(node: ast.AST, parents: dict[int, ast.AST]) -> int:
    """Return the line whose execution shows this guard's own condition was evaluated.

    Args:
        node: The refusal construction to walk outwards from.
        parents: Parent map over the module holding it.

    Returns:
        The governing line, or 0 when the walk leaves the module without finding one.
    """
    current: ast.AST | None = node
    while current is not None:
        holder = parents.get(id(current))
        if isinstance(holder, ast.If | ast.While):
            return holder.test.lineno
        if isinstance(holder, ast.For | ast.AsyncFor):
            # The header decides whether the body runs at all, and an empty iterable is exactly how
            # a boundary script enters the function without reaching a guard in the body.
            return holder.iter.lineno
        if isinstance(holder, ast.match_case):
            return (holder.guard or holder.pattern).lineno
        if isinstance(holder, ast.ExceptHandler):
            return _handled_operation_line(holder, parents)
        if isinstance(holder, ast.FunctionDef | ast.AsyncFunctionDef):
            # A fall-through refusal has no branch of its own: reaching it means every earlier arm
            # declined. Its enclosing function's first executable statement, skipping the
            # docstring, is the nearest line whose execution shows the walk ran over real evidence.
            return _first_executable_line(holder)
        current = holder
    return 0


def _governing_line_of(source: str) -> int:
    """Return the governing line of the one guard origin in this source."""
    tree = ast.parse(source)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    origin = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if isinstance(node.func, ast.Name) and node.func.id == "GuardRefusal"
    )
    return _governing_line(origin, parents)


def test_governing_line_of_a_guard_under_an_if_is_its_test() -> None:
    source = 'def _guard(value):\n    if value > 3:\n        raise E(GuardRefusal("a", "b"))\n'

    assert _governing_line_of(source) == 2


def test_governing_line_of_a_guard_under_a_while_is_the_loop_test() -> None:
    # Walking past the loop to the function head would name a line that runs for any script
    # entering the function, so a boundary could satisfy the trace without the loop test ever
    # being evaluated.
    source = (
        "def _guard(state):\n"
        "    setup(state)\n"
        "    while state.depth > 3:\n"
        '        raise E(GuardRefusal("a", "b"))\n'
    )

    assert _governing_line_of(source) == 3


def test_governing_line_of_a_guard_in_a_loop_body_is_the_header() -> None:
    source = (
        "def _guard(state):\n"
        "    setup(state)\n"
        "    for item in state.items:\n"
        '        raise E(GuardRefusal("a", "b"))\n'
    )

    assert _governing_line_of(source) == 3


def test_governing_line_of_a_guard_in_a_handler_is_the_guarded_operation() -> None:
    # Three frozen origins sit in `except` handlers. The handler's own line is unsatisfiable as a
    # witness, because entering the handler is what runs the refusal.
    source = (
        "def _guard(digits):\n"
        "    try:\n"
        "        return int(digits)\n"
        "    except ValueError:\n"
        '        raise E(GuardRefusal("a", "b")) from None\n'
    )

    assert _governing_line_of(source) == 3


def test_governing_line_of_a_guard_in_a_match_arm_is_the_arm() -> None:
    source = (
        "def _guard(node):\n"
        "    match node.kind:\n"
        '        case "unknown":\n'
        '            raise E(GuardRefusal("a", "b"))\n'
    )

    assert _governing_line_of(source) == 3


def test_governing_line_of_a_fall_through_guard_skips_the_docstring() -> None:
    source = (
        "def _guard(node):\n"
        '    """Doc."""\n'
        '    if node.kind == "a":\n'
        "        return 1\n"
        '    raise E(GuardRefusal("a", "b"))\n'
    )

    assert _governing_line_of(source) == 3


@pytest.mark.parametrize(
    "origin_id",
    [
        "taint.eval-descriptor.unparsable",
        "taint.eval-payload.lex-error",
        "scanner.descriptor.unparsable",
    ],
)
def test_a_shipped_handler_guard_witnesses_its_guarded_operation(origin_id: str) -> None:
    # Re-derived from the other direction: find the handler holding this origin and take the `try`
    # it belongs to. These three are frozen debt today, so the line they would resolve to is what
    # decides whether classifying one of them later produces a real witness or a vacuous one.
    path, condition_line, _refusal_line = _GUARD_LINES[origin_id]
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    origin = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if isinstance(node.func, ast.Name) and node.func.id == "GuardRefusal"
        if node.args and getattr(node.args[0], "value", None) == origin_id
    )
    handler = _enclosing_of(origin, ast.ExceptHandler, parents)
    guarded = parents[id(handler)]
    assert isinstance(guarded, ast.Try)

    assert condition_line == guarded.body[0].lineno


@pytest.mark.parametrize(
    "origin_id",
    ["taint.evidence.scope-parent-cycle", "taint.evidence.output-graph-cycle"],
)
def test_a_transported_refusal_witnesses_the_transports_own_condition(origin_id: str) -> None:
    # Both of these hand their refusal to `_validate_acyclic_graph`, which owns the condition that
    # decides it. Read from the caller, the refusal line is the construction, which runs for every
    # script that reaches the call, so the assertion that a boundary does not reach it could never
    # hold and neither guard could be classified as an invariant at all.
    path, condition_line, refusal_line = _GUARD_LINES[origin_id]
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    transport = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        if node.name == "_validate_acyclic_graph"
    )
    raised = next(node for node in ast.walk(transport) if isinstance(node, ast.Raise))
    executed = _executed_lines("echo hi", path)

    assert refusal_line == raised.lineno
    assert condition_line < refusal_line
    assert condition_line in executed
    assert refusal_line not in executed


def test_an_untransported_refusal_keeps_its_own_lines() -> None:
    path, condition_line, refusal_line = _GUARD_LINES["taint.evidence.unknown-parent-scope"]
    source = Path(path).read_text(encoding="utf-8").splitlines()

    assert "if scope.parent_scope_id is not None" in source[condition_line - 1]
    assert "GuardRefusal(" in source[refusal_line - 1]


def _enclosing_of(node: ast.AST, kind: type[ast.AST], parents: dict[int, ast.AST]) -> ast.AST:
    """Return the nearest node of this kind holding the given node."""
    current = parents.get(id(node))
    while current is not None and not isinstance(current, kind):
        current = parents.get(id(current))
    assert current is not None
    return current


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


def test_the_executable_assertions_alone_accept_a_misdirected_predicate() -> None:
    # Why the gate derives relevance from the guarded source instead of trusting the row. This is
    # the predicate a vacuous row for `taint.evidence.unknown-output-node` would carry, and every
    # assertion above holds for it: the boundary certifies, the predicate rejects the empty control,
    # the guard's condition line runs and its refusal line does not. It says nothing about an
    # unhandled output node. `checker.repository_invariant_relevance_violations` is what rejects it,
    # because `commands` is not among the attributes that walk inspects.
    misdirected = InvariantWitness(
        "taint.evidence.unknown-output-node",
        "a rationale nothing supports",
        "echo hi",
        boundary_evidence=lambda evidence: bool(evidence.commands),
    )
    path, condition_line, refusal_line = _GUARD_LINES[misdirected.origin_id]
    executed = _executed_lines(misdirected.boundary_script, path)

    assert scan_doc_lattice_invocations(misdirected.boundary_script).guard_id is None
    assert not misdirected.boundary_evidence(_boundary_evidence(_VACUOUS_CONTROL_SCRIPT))
    assert misdirected.boundary_evidence(_boundary_evidence(misdirected.boundary_script))
    assert condition_line in executed
    assert refusal_line not in executed


class _RecordingEvidence:
    """Delegating wrapper that records every attribute a predicate reads off one evidence tree.

    The gate's relevance rule derives a predicate's reads from inert source, so a read spelled in a
    branch that never runs counts exactly as one that does. Pruning such branches statically is a
    recognizer treadmill, and unnecessary here: the harness already executes every predicate against
    real boundary evidence, so the reads of the accepting run can be measured instead. Dead code
    contributes nothing at runtime however it is spelled.

    Reads go into a set shared by every wrapper in the tree. Dataclass instances are wrapped
    recursively, and list and tuple values have their elements wrapped, so a read a helper the
    registry defines performs on the way down is recorded too. Anything else is returned as it is,
    including a container this wrapper does not descend: under-recording can only shrink the
    observed set, which makes the assertion stricter rather than letting a row through it.

    `__class__` reports the wrapped object's type, the mechanism `mock.Mock(spec=...)` uses, because
    the two output-walk predicates dispatch on `isinstance` over the output union and would
    otherwise see nothing they recognize.
    """

    __slots__ = ("_recorded", "_wrapped")

    def __init__(self, wrapped: Any, recorded: set[str]) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_recorded", recorded)

    @property
    def __class__(self) -> Any:
        return type(object.__getattribute__(self, "_wrapped"))

    def __getattr__(self, name: str) -> Any:
        recorded: set[str] = object.__getattribute__(self, "_recorded")
        recorded.add(name)
        wrapped = object.__getattribute__(self, "_wrapped")
        return _recorded_value(getattr(wrapped, name), recorded)


def _recorded_value(value: Any, recorded: set[str]) -> Any:
    """Return this value with any attribute read through it recorded into the shared set.

    Args:
        value: A value a predicate just read off the evidence tree.
        recorded: The set every wrapper in this tree records into.

    Returns:
        A wrapper for a dataclass instance, a fresh list of wrapped elements for a list or tuple,
        and the value itself for anything else. Predicates only read, so rebuilding the sequence is
        safe.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _RecordingEvidence(value, recorded)
    if isinstance(value, list | tuple):
        return [_recorded_value(item, recorded) for item in value]
    return value


def _recorded_reads(predicate: Callable[[Any], bool], script: str) -> tuple[bool, frozenset[str]]:
    """Run a boundary predicate over one script's evidence and report what it read.

    Args:
        predicate: The row's boundary-evidence predicate.
        script: The boundary script whose evidence it is evaluated against.

    Returns:
        Whether the predicate accepted the evidence, and the attribute names it read doing so.
    """
    recorded: set[str] = set()
    accepted = bool(predicate(_RecordingEvidence(_boundary_evidence(script), recorded)))
    return accepted, frozenset(recorded)


def _relevant_reads() -> dict[str, frozenset[str]]:
    """Map each guard origin to the leaf reads of the layer that decides its refusal.

    Derived from the real guarded modules through the same checker function the gate's source-side
    relevance rule consumes, so the executed reads are held to the same set the gate holds the
    predicate's spelling to.
    """
    relevant: dict[str, frozenset[str]] = {}
    for module in checker.GUARDED_MODULES:
        source = (_ROOT / module).read_text(encoding="utf-8")
        relevant.update(checker.guard_relevant_reads(source, Path(module).name))
    return relevant


_RELEVANT_READS = _relevant_reads()

_DEAD_BRANCH_PREDICATE = (
    "lambda e: bool(e.commands) if True else any(s.parent_scope_id for s in e.scopes)"
)
"""The Codex round-7 predicate: its whole relevance to the parent-scope guard is dead code.

The executed half is `bool(e.commands)`, which is false for the empty control and true for
`echo hi`, so every executable assertion an invariant row can be held to passes. The `else` branch
never runs and donates `parent_scope_id` to the gate's source derivation regardless.
"""


@pytest.mark.parametrize(
    "witness",
    INVARIANT_WITNESSES,
    ids=[witness.origin_id for witness in INVARIANT_WITNESSES],
)
def test_invariant_boundary_predicate_reads_a_guard_leaf_while_it_runs(
    witness: InvariantWitness,
) -> None:
    # The gate can only see what the predicate mentions. This is the half that cannot be derived
    # from source: the accepting run itself is measured, and the reads it performed are held to the
    # same deciding layer, so a row whose relevance lives in an unexecuted branch fails here.
    relevant = _RELEVANT_READS[witness.origin_id]
    assert relevant, f"{witness.origin_id}: no deciding layer for a predicate to read"

    accepted, accessed = _recorded_reads(witness.boundary_evidence, witness.boundary_script)

    assert accepted, (
        f"{witness.origin_id}: boundary evidence rejected while recording, so there is no "
        f"accepting run to measure"
    )
    assert accessed & relevant, (
        f"{witness.origin_id}: the accepting run read {sorted(accessed) or 'nothing'} while this "
        f"guard decides on {sorted(relevant)}; a read reached only in unexecuted code supports no "
        f"claim about it"
    )


def test_a_dead_branch_read_satisfies_the_source_rule_and_fails_the_runtime_one() -> None:
    # Codex round-7 P1, pinned on the shipped origin it would have carried. The gate's derivation
    # walks the whole predicate, so the dead `else` is enough to pass it; the recorded run never
    # touches `parent_scope_id`, so the runtime rule rejects the same row.
    origin_id = "taint.evidence.unknown-parent-scope"
    registry = (
        "REACHABLE_WITNESSES = ()\n"
        "INVARIANT_WITNESSES = (\n"
        "    InvariantWitness(\n"
        f'        "{origin_id}",\n'
        '        "a rationale nothing supports",\n'
        '        "echo hi",\n'
        f"        boundary_evidence={_DEAD_BRANCH_PREDICATE},\n"
        "    ),\n"
        ")\n"
    )
    # Both halves have to read the one spelling Codex reported, so the predicate is compiled from
    # the same text the registry under test carries rather than restated as a second literal.
    predicate = eval(_DEAD_BRANCH_PREDICATE)  # noqa: S307
    relevant = _RELEVANT_READS[origin_id]

    mentioned = checker.invariant_predicate_reads(registry)[origin_id]
    accepted, accessed = _recorded_reads(predicate, "echo hi")

    assert mentioned & relevant, "the source rule already rejects this row, so it pins nothing"
    assert accepted
    assert "parent_scope_id" not in accessed
    assert not accessed & relevant


def test_a_genuine_parent_edge_predicate_records_the_leaf_it_reads() -> None:
    # The control for the pin above: the same claim spelled so that it runs reads the leaf, which
    # is what keeps the runtime rule from being unsatisfiable rather than strict.
    accepted, accessed = _recorded_reads(
        lambda evidence: any(scope.parent_scope_id is not None for scope in evidence.scopes),
        "if true; then (:); fi",
    )

    assert accepted
    assert "parent_scope_id" in accessed
    assert accessed & _RELEVANT_READS["taint.evidence.unknown-parent-scope"]


def test_the_recording_wrapper_keeps_isinstance_working_over_the_output_union() -> None:
    # The two output-walk predicates dispatch on `isinstance` over that union. A wrapper reporting
    # its own type would make every arm decline, and both rows would then report nothing while the
    # recorded set stayed silently small. A recursing member and a leaf one are both checked,
    # because the walk descends through the first to reach the second.
    recorded: set[str] = set()
    evidence = _RecordingEvidence(_boundary_evidence("if true; then echo a; fi"), recorded)

    roots = [scope.output for scope in evidence.scopes if scope.output is not None]
    leaves = [part for root in roots for part in root.parts]

    assert roots
    assert leaves
    assert all(type(node) is _RecordingEvidence for node in (*roots, *leaves)), (
        "the walk was handed unwrapped values, so this proves nothing about the wrapper"
    )
    assert all(isinstance(root, SequenceOutput) for root in roots)
    assert any(isinstance(leaf, CommandOutput) for leaf in leaves)
