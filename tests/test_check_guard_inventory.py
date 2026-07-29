"""Behavior tests for the fail-closed guard origin extractor and shape gate."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_ROOT = Path(__file__).resolve().parents[1]
_CHECKER = _ROOT / "scripts/check_guard_inventory.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_guard_inventory", _CHECKER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _fingerprint_for(source: str, path: str, origin_id: str) -> str:
    return next(
        record.fingerprint
        for record in checker.extract_origin_records(source, path)
        if record.origin_id == origin_id
    )


def _fake_root(tmp_path: Path, debt_records: list[dict[str, str]]) -> Path:
    """Build a candidate tree that shares this repository's guarded modules."""
    root = tmp_path / "candidate"
    for module in checker.GUARDED_MODULES:
        target = root / module
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_ROOT / module, target)
    debt = root / checker.DEBT_PATH
    debt.parent.mkdir(parents=True, exist_ok=True)
    debt.write_text(
        json.dumps({"schema": checker.SCHEMA_VERSION, "records": debt_records}),
        encoding="utf-8",
    )
    registry = root / checker.REGISTRY_PATH
    registry.parent.mkdir(parents=True, exist_ok=True)
    if not registry.exists():
        shutil.copy(_ROOT / checker.REGISTRY_PATH, registry)
    return root


_ORIGIN_SOURCE = """
def _guard(value):
    if value > 3:
        raise _TaintLimitExceeded(GuardRefusal("taint.demo.over-three", "too big"))
    return value
"""


def test_extracts_one_record_per_guard_refusal_construction() -> None:
    records = checker.extract_origin_records(_ORIGIN_SOURCE, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.over-three"]
    assert records[0].path == "shell_taint.py"
    assert records[0].qualname == "_guard"
    assert records[0].fingerprint


def test_fingerprint_ignores_line_position() -> None:
    shifted = "\n\n\n# a leading comment\n" + _ORIGIN_SOURCE

    original = checker.extract_origin_records(_ORIGIN_SOURCE, "shell_taint.py")
    moved = checker.extract_origin_records(shifted, "shell_taint.py")

    assert original[0].fingerprint == moved[0].fingerprint


def test_fingerprint_ignores_operator_reason_rewording() -> None:
    reworded = _ORIGIN_SOURCE.replace('"too big"', '"the value is too big"')

    original = checker.extract_origin_records(_ORIGIN_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(reworded, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_fingerprint_changes_when_the_guard_condition_changes() -> None:
    retargeted = _ORIGIN_SOURCE.replace("value > 3", "value > 4")

    original = checker.extract_origin_records(_ORIGIN_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(retargeted, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_changes_when_the_guard_moves_to_another_function() -> None:
    moved = _ORIGIN_SOURCE.replace("def _guard(", "def _other_guard(")

    original = checker.extract_origin_records(_ORIGIN_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(moved, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_raw_text_exception_is_rejected() -> None:
    source = 'def f():\n    raise _TaintLimitExceeded("shell taint edge limit exceeded")\n'

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("raw refusal text" in violation for violation in violations)


def test_interpolated_text_exception_is_rejected() -> None:
    source = "def f(x):\n    raise _ShellScanIncomplete(f'bad {x}')\n"

    violations = checker.find_shape_violations(source, "shell_scanner.py")

    assert any("raw refusal text" in violation for violation in violations)


@pytest.mark.parametrize("verdict", ["Certified()", "MarkerDetected()"])
def test_guard_free_verdict_is_rejected_as_a_refusal_exception_payload(verdict: str) -> None:
    source = f"def f():\n    raise _TaintLimitExceeded({verdict})\n"

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("carries undeclared transport" in violation for violation in violations)


def test_non_literal_guard_id_is_rejected() -> None:
    source = 'def f(name):\n    raise _TaintLimitExceeded(GuardRefusal(name, "reason"))\n'

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("literal" in violation for violation in violations)


def test_executable_guard_reason_is_rejected() -> None:
    source = (
        "def analyze_marker_taint(evidence):\n"
        '    return GuardRefusal("taint.demo.reason", compute_reason())\n'
    )

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("reason must be a string literal" in violation for violation in violations)


def test_executable_guard_reason_is_rejected_during_base_owned_extraction() -> None:
    source = (
        "def analyze_marker_taint(evidence):\n"
        '    return GuardRefusal("taint.demo.reason", compute_reason())\n'
    )

    with pytest.raises(ValueError, match="reason must be a string literal"):
        checker.extract_origin_records(source, "shell_taint.py")


def test_tuple_verdict_return_is_rejected() -> None:
    source = 'def analyze_marker_taint(e):\n    return True, "shell taint edge limit exceeded"\n'

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("analyze_marker_taint" in violation for violation in violations)


@pytest.mark.parametrize(
    "expression",
    ["TAINT_REFUSAL_REASON", '"refusal " + "reason"', "_shared_refusal()"],
)
def test_arbitrary_taint_verdict_return_is_rejected(expression: str) -> None:
    source = f"def analyze_marker_taint(evidence):\n    return {expression}\n"

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("returns an undeclared verdict shape" in violation for violation in violations)


def test_text_only_scan_result_is_rejected() -> None:
    source = (
        "def scan_doc_lattice_invocations(script):\n"
        '    return ShellScanResult((), "source character limit exceeded")\n'
    )

    violations = checker.find_shape_violations(source, "shell_scanner.py")

    assert any("raw refusal text" in violation for violation in violations)


def test_scan_boundary_return_must_construct_a_shell_scan_result() -> None:
    source = (
        "def scan_doc_lattice_invocations(script):\n"
        "    verdict = GuardRefusal('scanner.demo.indirect', 'x')\n"
        "    return verdict\n"
    )

    violations = checker.find_shape_violations(source, "shell_scanner.py")

    assert any("returns an undeclared verdict shape" in violation for violation in violations)


def test_undeclared_transport_is_rejected() -> None:
    source = "def f(other):\n    raise _ShellScanIncomplete(other.refusal)\n"

    violations = checker.find_shape_violations(source, "shell_scanner.py")

    assert any("transport" in violation for violation in violations)


def test_canonical_refusal_shapes_are_accepted() -> None:
    source = (
        "def analyze_marker_taint(e):\n"
        "    try:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.a", "a"))\n'
        "    except _TaintLimitExceeded as error:\n"
        "        return error.refusal\n"
        "    return Certified()\n"
    )

    assert checker.find_shape_violations(source, "shell_taint.py") == ()


def test_guard_free_verdict_constructor_alias_is_accepted() -> None:
    source = (
        "from doc_lattice.github_ci.shell_guards import Certified as Done\n"
        "def analyze_marker_taint(evidence):\n"
        "    return Done()\n"
    )

    assert checker.find_shape_violations(source, "shell_taint.py") == ()


def test_scan_result_constructor_alias_is_accepted() -> None:
    source = (
        "Result = ShellScanResult\n"
        "def scan_doc_lattice_invocations(script):\n"
        "    return Result((), Certified())\n"
    )

    assert checker.find_shape_violations(source, "shell_scanner.py") == ()


def test_the_shipped_shell_modules_have_no_shape_violations() -> None:
    for path in checker.GUARDED_MODULES:
        source = (_ROOT / path).read_text(encoding="utf-8")

        assert checker.find_shape_violations(source, Path(path).name) == ()


def test_every_shipped_origin_id_is_unique() -> None:
    ids = [record.origin_id for record in checker.repository_origin_records(_ROOT)]

    assert len(ids) == len(set(ids))


def test_default_limits_construction_away_from_a_boundary_is_rejected() -> None:
    source = "def _helper(expression):\n    return _evaluate(expression, TaintLimits())\n"

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("constructs default limits" in violation for violation in violations)


@pytest.mark.parametrize(
    ("source", "path", "qualname"),
    [
        (
            "def analyze_marker_taint(evidence):\n"
            "    def _reset_limits():\n"
            "        return TaintLimits()\n"
            "    return _reset_limits()\n",
            "shell_taint.py",
            "analyze_marker_taint._reset_limits",
        ),
        (
            "class _ScanBudget:\n    def reset(self):\n        self.limits = ScanLimits()\n",
            "shell_scanner.py",
            "_ScanBudget.reset",
        ),
    ],
)
def test_limits_boundaries_do_not_exempt_descendant_scopes(
    source: str, path: str, qualname: str
) -> None:
    violations = checker.find_limits_violations(source, path)

    assert any(
        f"{path}:{qualname} constructs default limits" in violation for violation in violations
    )


def test_optional_limits_parameter_away_from_a_boundary_is_rejected() -> None:
    source = (
        "def _helper(expression, limits: TaintLimits = TaintLimits()):\n    return expression\n"
    )

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("must require limits" in violation for violation in violations)


def test_declared_limits_boundaries_are_accepted() -> None:
    source = (
        "def analyze_marker_taint(evidence, *, limits: TaintLimits = TaintLimits()):\n"
        "    return evidence\n"
    )

    assert checker.find_limits_violations(source, "shell_taint.py") == ()


def test_the_shipped_shell_modules_have_no_limits_violations() -> None:
    assert checker.repository_limits_violations(_ROOT) == ()


def test_guard_thresholds_are_limits_fields_or_inventoried_fixed_bounds() -> None:
    assert checker.repository_threshold_violations(_ROOT) == ()


def test_an_uninventoried_guard_threshold_is_rejected() -> None:
    source = (
        "def _guard(value):\n"
        "    if value > _MAX_UNDECLARED_THING:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("_MAX_UNDECLARED_THING" in violation for violation in violations)


def test_compare_against_base_rejects_debt_the_base_did_not_carry(tmp_path: Path) -> None:
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": []})
    record = checker.repository_origin_records(_ROOT)[0]
    root = _fake_root(tmp_path, [record.as_json()])

    failures = checker.compare_against_base(root, base)

    assert any(record.origin_id in failure for failure in failures)


def test_compare_against_base_accepts_debt_that_only_shrank(tmp_path: Path) -> None:
    records = [record.as_json() for record in checker.repository_origin_records(_ROOT)[:2]]
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": records})
    root = _fake_root(tmp_path, records[:1])

    assert checker.compare_against_base(root, base) == ()


def test_compare_against_base_rejects_debt_that_is_not_a_real_candidate_origin(
    tmp_path: Path,
) -> None:
    # A base-owned checker must not take the candidate's own closure run on trust: a debt record
    # naming an origin the candidate source does not contain is laundered debt.
    invented = {
        "origin_id": "taint.invented.guard",
        "path": "shell_taint.py",
        "qualname": "_nowhere",
        "fingerprint": "0000-0000-0000-0000-0000-0000",
    }
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": [invented]})
    root = _fake_root(tmp_path, [invented])

    failures = checker.compare_against_base(root, base)

    assert any("taint.invented.guard" in failure for failure in failures)


def test_compare_against_base_rejects_a_foreign_base_record_schema(tmp_path: Path) -> None:
    # The base snapshot is this checker's own output, so a schema it did not write is corruption.
    base = json.dumps({"schema": checker.SCHEMA_VERSION + 1, "records": []})
    root = _fake_root(tmp_path, [])

    with pytest.raises(ValueError, match="record schema"):
        checker.compare_against_base(root, base)


def test_compare_against_base_reads_a_candidate_snapshot_under_a_newer_schema(
    tmp_path: Path,
) -> None:
    # A candidate that migrates the record schema runs against the base revision's checker, which
    # predates the new schema. Decoding the candidate strictly would make every such migration
    # unmergeable once this gate is on the protected base.
    records = [record.as_json() for record in checker.repository_origin_records(_ROOT)[:2]]
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": records})
    root = _fake_root(tmp_path, [])
    (root / checker.DEBT_PATH).write_text(
        json.dumps(
            {
                "schema": checker.SCHEMA_VERSION + 1,
                "records": [{"origin_id": records[0]["origin_id"], "renamed_digest": "abcd"}],
            }
        ),
        encoding="utf-8",
    )

    assert checker.compare_against_base(root, base) == ()


def test_compare_against_base_rejects_a_candidate_snapshot_without_identifiers(
    tmp_path: Path,
) -> None:
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": []})
    root = _fake_root(tmp_path, [])
    (root / checker.DEBT_PATH).write_text(
        json.dumps({"schema": checker.SCHEMA_VERSION + 1, "records": [{"guard": "x"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=checker.IDENTITY_FIELD):
        checker.compare_against_base(root, base)


def test_compare_against_base_rejects_a_frozen_guard_whose_input_computation_changed(
    tmp_path: Path,
) -> None:
    # The end-to-end form of the fingerprint's scope: a frozen guard is disabled by inverting the
    # accumulation its condition reads, and the debt record must not survive that.
    origin_id = "taint.eval-discovery.work-limit"
    record = next(r for r in checker.repository_origin_records(_ROOT) if r.origin_id == origin_id)
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": [record.as_json()]})
    root = _fake_root(tmp_path, [record.as_json()])
    module = root / checker.GUARDED_MODULES[0]
    source = module.read_text(encoding="utf-8")
    inverted = source.replace("self.work += amount", "self.work -= amount", 1)
    assert inverted != source
    module.write_text(inverted, encoding="utf-8")

    failures = checker.compare_against_base(root, base)

    assert any(origin_id in failure for failure in failures)


_NESTED_SOURCE = """
def _guard(outer, inner):
    if outer:
        if inner > 3:
            raise _TaintLimitExceeded(GuardRefusal("taint.demo.nested", "too big"))
    return outer
"""

_ELIF_SOURCE = """
def _guard(a, b):
    if a:
        raise _TaintLimitExceeded(GuardRefusal("taint.demo.first", "a"))
    elif b > 3:
        raise _TaintLimitExceeded(GuardRefusal("taint.demo.second", "b"))
"""


def test_fingerprint_tracks_the_innermost_guarding_condition() -> None:
    # A guard nested inside another `if` must fingerprint its own test, not the outer one, or an
    # inverted condition keeps a byte-identical debt record.
    retargeted = _NESTED_SOURCE.replace("inner > 3", "inner < 3")

    original = checker.extract_origin_records(_NESTED_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(retargeted, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_an_elif_condition() -> None:
    retargeted = _ELIF_SOURCE.replace("b > 3", "b < 3")

    original = {
        r.origin_id: r.fingerprint for r in checker.extract_origin_records(_ELIF_SOURCE, "m")
    }
    updated = {r.origin_id: r.fingerprint for r in checker.extract_origin_records(retargeted, "m")}

    assert original["taint.demo.second"] != updated["taint.demo.second"]
    assert original["taint.demo.first"] == updated["taint.demo.first"]


_FED_CONDITION_SOURCE = """
class _Budget:
    def charge(self, amount):
        self.work += amount
        self.unrelated = amount
        if self.work > self.limits.max_work:
            raise _TaintLimitExceeded(GuardRefusal("taint.demo.work-limit", "too much work"))
"""


def test_fingerprint_tracks_a_write_that_feeds_the_guard_condition() -> None:
    # Inverting the accumulation disables the guard as completely as inverting its condition, and
    # neither the qualname nor the origin statement moves when it happens.
    inverted = _FED_CONDITION_SOURCE.replace("self.work += amount", "self.work -= amount")

    original = checker.extract_origin_records(_FED_CONDITION_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(inverted, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_ignores_a_same_scope_write_the_condition_does_not_read() -> None:
    # Scoping the digest to what the condition reads is what keeps an unrelated edit in a long
    # function from churning every frozen record inside it.
    edited = _FED_CONDITION_SOURCE.replace("self.unrelated = amount", "self.unrelated = amount + 1")

    original = checker.extract_origin_records(_FED_CONDITION_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_fingerprint_tracks_a_write_through_a_subscript_the_condition_reads() -> None:
    source = (
        "def _guard(state, scope_id):\n"
        "    state[scope_id] = 1\n"
        "    if state.get(scope_id) == 1:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.cycle", "cycle"))\n'
    )
    edited = source.replace("state[scope_id] = 1", "state[scope_id] = 0")

    original = checker.extract_origin_records(source, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


_TRANSITIVE_SOURCE = (
    "def _guard(literal):\n"
    "    option, attached = _resolve(literal)\n"
    "    kind = _KINDS[option]\n"
    "    if kind == 'split':\n"
    '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.split", "unscannable"))\n'
    "    return kind\n"
)


def test_fingerprint_tracks_a_write_two_hops_from_the_guard_condition() -> None:
    # The condition reads `kind` alone, so only `kind = _KINDS[option]` writes what it reads
    # directly. Pinning `option` to a constant makes the refusal unreachable while leaving the
    # condition, the origin statement and the direct writer all byte-identical.
    edited = _TRANSITIVE_SOURCE.replace("_resolve(literal)", "('debug', None)")

    original = checker.extract_origin_records(_TRANSITIVE_SOURCE, "shell_scanner.py")
    updated = checker.extract_origin_records(edited, "shell_scanner.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_the_real_env_option_guards_transitive_writer() -> None:
    # The same defect on the shipped guard the closure was widened for.
    source = (_ROOT / "src/doc_lattice/github_ci/shell_scanner.py").read_text(encoding="utf-8")
    edited = source.replace(
        "    option, attached_value = _resolve_env_long_option(literal)\n",
        '    option, attached_value = ("debug", None)\n',
    )
    assert edited != source

    def fingerprint(text: str) -> str:
        records = checker.extract_origin_records(text, "shell_scanner.py")
        return next(
            record.fingerprint
            for record in records
            if record.origin_id == "scanner.env-option.static-split-string"
        )

    assert fingerprint(source) != fingerprint(edited)


def test_writer_closure_still_ignores_an_unrelated_chain_in_the_same_scope() -> None:
    # Following values transitively must not degrade into hashing the whole function, or every
    # frozen record churns on any edit to a long one and has to be regenerated.
    edited = _TRANSITIVE_SOURCE.replace(
        "    option, attached = _resolve(literal)\n",
        "    option, attached = _resolve(literal)\n    noise = _unrelated(literal)\n",
    )

    original = checker.extract_origin_records(_TRANSITIVE_SOURCE, "shell_scanner.py")
    updated = checker.extract_origin_records(edited, "shell_scanner.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_writer_closure_terminates_on_a_cyclic_dataflow() -> None:
    # A loop that reads and rewrites the same name feeds itself; the fixpoint must still settle.
    source = (
        "def _guard(items):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total = total + item\n"
        "    if total > 3:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.cyclic", "too much"))\n'
    )

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.cyclic"]


def test_fingerprint_ignores_a_write_in_another_function() -> None:
    source = (
        "def _elsewhere(state):\n"
        "    state.work = 0\n"
        "def _guard(state):\n"
        "    if state.work > state.limits.max_work:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.scoped", "too much"))\n'
    )
    edited = source.replace("state.work = 0", "state.work = 1")

    original = checker.extract_origin_records(source, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


_CALLEE_SOURCE = (
    "def _resolve(literal):\n"
    "    option, separator, _value = literal.partition('=')\n"
    "    candidates = tuple(c for c in _KINDS if c.startswith(option))\n"
    "    if len(candidates) != 1:\n"
    '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.ambiguous", "unscannable"))\n'
    "    return candidates[0], bool(separator)\n"
    "def _guard(literal):\n"
    "    option, attached = _resolve(literal)\n"
    "    kind = _KINDS[option]\n"
    "    if kind == 'split':\n"
    '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.split", "unscannable"))\n'
    "    return kind\n"
)


def test_fingerprint_tracks_the_return_of_a_callee_the_guard_reads() -> None:
    # A writer hashes the spelling of its call and nothing the call computes, so pinning the
    # callee's return withdraws the guard with the whole writer closure byte-identical.
    edited = _CALLEE_SOURCE.replace(
        "    return candidates[0], bool(separator)\n",
        "    return 'debug', bool(separator)\n",
    )

    assert _fingerprint_for(_CALLEE_SOURCE, "shell_scanner.py", "scanner.demo.split") != (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_fingerprint_tracks_a_write_feeding_a_callees_return() -> None:
    edited = _CALLEE_SOURCE.replace("c.startswith(option)", "c == option")

    assert _fingerprint_for(_CALLEE_SOURCE, "shell_scanner.py", "scanner.demo.split") != (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_fingerprint_tracks_the_control_flow_deciding_a_callees_return() -> None:
    # Widening the callee's own refusal lets a value reach the return that could not before, which
    # changes what the caller reads without touching any statement the return reads.
    edited = _CALLEE_SOURCE.replace("if len(candidates) != 1:", "if len(candidates) > 99:")

    assert _fingerprint_for(_CALLEE_SOURCE, "shell_scanner.py", "scanner.demo.split") != (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_callee_closure_ignores_a_statement_no_return_of_the_callee_reads() -> None:
    # Following return values must not degrade into hashing the callee's whole body, or one helper
    # couples every guard that calls it to its every unrelated edit and the debt has to be
    # regenerated, which is the laundering path the gate exists to close.
    edited = _CALLEE_SOURCE.replace(
        "    candidates = tuple(c for c in _KINDS if c.startswith(option))\n",
        "    candidates = tuple(c for c in _KINDS if c.startswith(option))\n"
        "    noise = literal.upper()\n",
    )

    assert _fingerprint_for(_CALLEE_SOURCE, "shell_scanner.py", "scanner.demo.split") == (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_callee_closure_leaves_the_callees_own_guard_alone() -> None:
    # `scanner.demo.ambiguous` lives in the callee and does not read its return, so pinning that
    # return moves the caller's record and not its own.
    edited = _CALLEE_SOURCE.replace(
        "    return candidates[0], bool(separator)\n",
        "    return 'debug', bool(separator)\n",
    )

    assert _fingerprint_for(_CALLEE_SOURCE, "shell_scanner.py", "scanner.demo.ambiguous") == (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.ambiguous")
    )


def test_callee_closure_follows_a_callee_two_hops_from_the_guard() -> None:
    source = (
        "def _inner(literal):\n"
        "    return literal.partition('=')[0]\n"
        "def _outer(literal):\n"
        "    return _inner(literal)\n"
        "def _guard(literal):\n"
        "    kind = _KINDS[_outer(literal)]\n"
        "    if kind == 'split':\n"
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.split", "unscannable"))\n'
    )
    edited = source.replace("return literal.partition('=')[0]", "return 'debug'")

    assert _fingerprint_for(source, "shell_scanner.py", "scanner.demo.split") != (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_callee_closure_follows_a_callee_a_reachability_control_reads() -> None:
    # A control deciding the origin is reached withdraws the guard as completely as its condition,
    # so a callee whose value that control reads is covered on the same footing.
    source = (
        "def _is_long(literal):\n"
        "    return literal.startswith('--')\n"
        "def _guard(literal, kind):\n"
        "    if not _is_long(literal):\n"
        "        return 0\n"
        "    if kind == 'split':\n"
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.split", "unscannable"))\n'
    )
    edited = source.replace("return literal.startswith('--')", "return False")

    assert _fingerprint_for(source, "shell_scanner.py", "scanner.demo.split") != (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_callee_closure_follows_a_callee_a_parameter_default_reads() -> None:
    # A default binds a value for every caller that omits the argument, and it is evaluated in the
    # defining scope, so the module function it calls is reached there too.
    source = (
        "def _ceiling():\n"
        "    return 4096\n"
        "def _guard(depth, cap=_ceiling()):\n"
        "    if depth > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.depth", "too deep"))\n'
    )
    edited = source.replace("    return 4096\n", "    return 1 << 40\n")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.depth") != (
        _fingerprint_for(edited, "shell_taint.py", "taint.demo.depth")
    )


def test_callee_closure_ignores_an_attribute_call_sharing_a_module_function_name() -> None:
    # `helper.resolve(...)` names a member of a value this parse cannot resolve. Reading it as the
    # module's `resolve` would hash an unrelated function into the record.
    source = (
        "def resolve(literal):\n"
        "    return 'split'\n"
        "def _guard(helper, literal):\n"
        "    kind = helper.resolve(literal)\n"
        "    if kind == 'split':\n"
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.split", "unscannable"))\n'
    )
    edited = source.replace("    return 'split'\n", "    return 'flag'\n")

    assert _fingerprint_for(source, "shell_scanner.py", "scanner.demo.split") == (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_callee_closure_ignores_a_callee_name_a_local_binding_shadows() -> None:
    # Python lexical binding is the boundary here exactly as it is for a read spelling.
    source = (
        "def _resolve(literal):\n"
        "    return 'split'\n"
        "def _guard(table, literal):\n"
        "    _resolve = table[literal]\n"
        "    kind = _resolve(literal)\n"
        "    if kind == 'split':\n"
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.split", "unscannable"))\n'
    )
    edited = source.replace("    return 'split'\n", "    return 'flag'\n")

    assert _fingerprint_for(source, "shell_scanner.py", "scanner.demo.split") == (
        _fingerprint_for(edited, "shell_scanner.py", "scanner.demo.split")
    )


def test_callee_closure_terminates_on_mutual_recursion() -> None:
    source = (
        "def _left(value):\n"
        "    return _right(value)\n"
        "def _right(value):\n"
        "    return _left(value)\n"
        "def _guard(value):\n"
        "    if _left(value) > 3:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.cyclic-callee", "too big"))\n'
    )

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.cyclic-callee"]


def test_fingerprint_tracks_the_real_env_option_callees_return() -> None:
    # The shipped case the boundary was widened for: `scanner.env-option.static-split-string` tests
    # a `kind` resolved from an `option` that `_resolve_env_long_option` returns, and pinning that
    # return left every fingerprint in the tree unchanged.
    source = (_ROOT / "src/doc_lattice/github_ci/shell_scanner.py").read_text(encoding="utf-8")
    edited = source.replace(
        "    return candidates[0], bool(separator)\n",
        '    return "--debug", bool(separator)\n',
    )
    assert edited != source

    origin_id = "scanner.env-option.static-split-string"
    assert _fingerprint_for(source, "shell_scanner.py", origin_id) != (
        _fingerprint_for(edited, "shell_scanner.py", origin_id)
    )


def test_the_real_env_option_callee_ignores_a_reworded_refusal_inside_it() -> None:
    source = (_ROOT / "src/doc_lattice/github_ci/shell_scanner.py").read_text(encoding="utf-8")
    edited = source.replace(
        '                "scanner.env-option.ambiguous-long-option",\n'
        '                "unsupported env option cannot be scanned safely",\n',
        '                "scanner.env-option.ambiguous-long-option",\n'
        '                "env option is unsupported and cannot be scanned",\n',
    )
    assert edited != source

    origin_id = "scanner.env-option.static-split-string"
    assert _fingerprint_for(source, "shell_scanner.py", origin_id) == (
        _fingerprint_for(edited, "shell_scanner.py", origin_id)
    )


def test_compare_against_base_rejects_a_frozen_guard_whose_callee_return_changed(
    tmp_path: Path,
) -> None:
    # The end-to-end form: the base-owned comparison accepted this withdrawal before the callee's
    # return value entered the record.
    origin_id = "scanner.env-option.static-split-string"
    record = next(r for r in checker.repository_origin_records(_ROOT) if r.origin_id == origin_id)
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": [record.as_json()]})
    root = _fake_root(tmp_path, [record.as_json()])
    module = root / "src/doc_lattice/github_ci/shell_scanner.py"
    source = module.read_text(encoding="utf-8")
    pinned = source.replace(
        "    return candidates[0], bool(separator)\n",
        '    return "--debug", bool(separator)\n',
    )
    assert pinned != source
    module.write_text(pinned, encoding="utf-8")

    failures = checker.compare_against_base(root, base)

    assert any(origin_id in failure for failure in failures)


def test_fingerprint_tracks_the_real_brace_digit_module_bound() -> None:
    source = (_ROOT / "src/doc_lattice/github_ci/shell_taint.py").read_text(encoding="utf-8")
    edited = source.replace(
        "_MAX_BRACE_INTEGER_DIGITS = 256",
        "_MAX_BRACE_INTEGER_DIGITS = 1000",
        1,
    )
    assert edited != source

    assert _fingerprint_for(
        source,
        "shell_taint.py",
        "taint.function-positional.index-digit-limit",
    ) != _fingerprint_for(
        edited,
        "shell_taint.py",
        "taint.function-positional.index-digit-limit",
    )


def test_fingerprint_tracks_a_transitive_module_binding() -> None:
    source = (
        "_BASE = 50\n"
        "_CAP = _BASE * 2\n"
        "def _guard(items):\n"
        "    if len(items) > _CAP:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.module", "nope"))\n'
    )
    edited = source.replace("_BASE = 50", "_BASE = 75")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.module") != _fingerprint_for(
        edited, "shell_taint.py", "taint.demo.module"
    )


def test_fingerprint_tracks_a_relevant_import_binding() -> None:
    source = (
        "from constants import CAP\n"
        "def _guard(items):\n"
        "    if len(items) > CAP:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.import", "nope"))\n'
    )
    edited = source.replace("from constants import CAP", "from revised_constants import CAP")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.import") != _fingerprint_for(
        edited, "shell_taint.py", "taint.demo.import"
    )


def test_module_writer_closure_keeps_lexical_negative_controls() -> None:
    source = (
        "_CAP = 100\n"
        "_UNRELATED = 1\n"
        "def _elsewhere():\n"
        "    other = _CAP\n"
        "    return other\n"
        "def _guard(items):\n"
        "    _CAP = 10\n"
        "    if len(items) > _CAP:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.shadowed", "nope"))\n'
    )
    unrelated = source.replace("_UNRELATED = 1", "_UNRELATED = 2")
    module_shadowed = source.replace("_CAP = 100", "_CAP = 200")
    another_function = source.replace("other = _CAP", "other = _CAP + 1")
    original = _fingerprint_for(source, "shell_taint.py", "taint.demo.shadowed")

    assert _fingerprint_for(unrelated, "shell_taint.py", "taint.demo.shadowed") == original
    assert _fingerprint_for(module_shadowed, "shell_taint.py", "taint.demo.shadowed") == original
    assert _fingerprint_for(another_function, "shell_taint.py", "taint.demo.shadowed") == original


@pytest.mark.parametrize(
    "body",
    [
        (
            "    try:\n"
            "        parse(items)\n"
            "    except Exception as _CAP:\n"
            "        if _CAP:\n"
            '            raise _TaintLimitExceeded(GuardRefusal("taint.demo.except", "nope"))\n'
        ),
        (
            "    match items:\n"
            "        case [*_CAP]:\n"
            "            if _CAP:\n"
            '                raise _TaintLimitExceeded(GuardRefusal("taint.demo.match", "nope"))\n'
        ),
        (
            "    captured = [_CAP for _CAP in items if _CAP]\n"
            "    if captured:\n"
            '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.comprehension", "nope"))\n'
        ),
        (
            "    predicate = lambda _CAP: _CAP > 0\n"
            "    if any(predicate(item) for item in items):\n"
            '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.lambda", "nope"))\n'
        ),
    ],
)
def test_fingerprint_ignores_module_bindings_shadowed_in_expression_scopes(body: str) -> None:
    source = "_CAP = 100\ndef _guard(items):\n" + body
    edited = source.replace("_CAP = 100", "_CAP = 200")

    original = checker.extract_origin_records(source, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_fingerprint_tracks_a_free_module_read_inside_a_comprehension() -> None:
    source = (
        "_CAP = 100\n"
        "def _guard(items):\n"
        "    if any(item > _CAP for item in items):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.free-comprehension", "nope"))\n'
    )
    edited = source.replace("_CAP = 100", "_CAP = 200")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.free-comprehension"
    ) != _fingerprint_for(edited, "shell_taint.py", "taint.demo.free-comprehension")


def test_fingerprint_tracks_a_module_name_bound_only_inside_a_lambda() -> None:
    source = (
        "_CAP = 100\n"
        "def _guard(items):\n"
        "    predicate = lambda: (_CAP := 1)\n"
        "    if len(items) > _CAP:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.lambda-walrus", "nope"))\n'
    )
    edited = source.replace("_CAP = 100", "_CAP = 200")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.lambda-walrus") != (
        _fingerprint_for(edited, "shell_taint.py", "taint.demo.lambda-walrus")
    )


def test_a_comprehension_walrus_still_binds_the_containing_function() -> None:
    source = (
        "_CAP = 100\n"
        "def _guard(items):\n"
        "    captured = [(_CAP := item) for item in items]\n"
        "    if captured and _CAP:\n"
        "        raise _TaintLimitExceeded("
        'GuardRefusal("taint.demo.comprehension-walrus", "nope"))\n'
    )
    edited = source.replace("_CAP = 100", "_CAP = 200")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.comprehension-walrus") == (
        _fingerprint_for(edited, "shell_taint.py", "taint.demo.comprehension-walrus")
    )


@pytest.mark.parametrize(
    ("module_binding", "callback", "condition"),
    [
        ("_STATE = []", "_STATE.append(1)", "_STATE"),
        ("_CONFIG = Config()", "_CONFIG.values.append(1)", "check(_CONFIG)"),
    ],
)
def test_fingerprint_ignores_mutations_deferred_inside_an_unused_lambda(
    module_binding: str,
    callback: str,
    condition: str,
) -> None:
    source = (
        f"{module_binding}\n"
        "def _guard(items):\n"
        f"    callback = lambda: {callback}\n"
        f"    if {condition}:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.deferred-mutation", "nope"))\n'
    )
    edited = source.replace("append(1)", "append(2)")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.deferred-mutation"
    ) == _fingerprint_for(edited, "shell_taint.py", "taint.demo.deferred-mutation")


@pytest.mark.parametrize(
    ("module_binding", "statement", "edited_statement", "condition"),
    [
        (
            "_STATE = []",
            "callback = lambda seeded=_STATE.append(1): None",
            "callback = lambda seeded=_STATE.append(2): None",
            "_STATE",
        ),
        (
            "_CONFIG = Config()",
            "configured = [_CONFIG.values.append(item) for item in items]",
            "configured = [_CONFIG.values.append(item + 1) for item in items]",
            "check(_CONFIG)",
        ),
    ],
)
def test_fingerprint_tracks_mutations_in_lambda_defaults_and_comprehensions(
    module_binding: str,
    statement: str,
    edited_statement: str,
    condition: str,
) -> None:
    source = (
        f"{module_binding}\n"
        "def _guard(items):\n"
        f"    {statement}\n"
        f"    if {condition}:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.executed-mutation", "nope"))\n'
    )
    edited = source.replace(statement, edited_statement)

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.executed-mutation"
    ) != _fingerprint_for(edited, "shell_taint.py", "taint.demo.executed-mutation")


def test_fingerprint_ignores_mutations_deferred_inside_an_unused_generator() -> None:
    source = (
        "_STATE = []\n"
        "def _guard(items):\n"
        "    pending = (_STATE.append(item) for item in items)\n"
        "    if _STATE:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.generator-mutation", "nope"))\n'
    )
    edited = source.replace("append(item)", "append(item + 1)")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.generator-mutation"
    ) == _fingerprint_for(edited, "shell_taint.py", "taint.demo.generator-mutation")


def test_fingerprint_tracks_mutation_in_a_generators_first_iterable() -> None:
    source = (
        "_STATE = []\n"
        "def _guard(items):\n"
        "    pending = (item for item in (_STATE.append(1),))\n"
        "    if _STATE:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.generator-iterable", "nope"))\n'
    )
    edited = source.replace("append(1)", "append(2)")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.generator-iterable"
    ) != _fingerprint_for(edited, "shell_taint.py", "taint.demo.generator-iterable")


@pytest.mark.parametrize(
    "expression",
    [
        "[_STATE.append(item) for item in items]",
        "{_STATE.append(item) for item in items}",
        "{item: _STATE.append(item) for item in items}",
    ],
)
def test_fingerprint_tracks_mutations_in_eager_comprehensions(expression: str) -> None:
    source = (
        "_STATE = []\n"
        "def _guard(items):\n"
        f"    consumed = {expression}\n"
        "    if _STATE:\n"
        "        raise _TaintLimitExceeded("
        'GuardRefusal("taint.demo.eager-comprehension", "nope"))\n'
    )
    edited = source.replace("append(item)", "append(item + 1)")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.eager-comprehension"
    ) != _fingerprint_for(edited, "shell_taint.py", "taint.demo.eager-comprehension")


def test_a_generator_walrus_still_binds_the_containing_function_lexically() -> None:
    source = (
        "_CAP = 100\n"
        "def _guard(items):\n"
        "    pending = ((_CAP := item) for item in items)\n"
        "    if _CAP:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.generator-walrus", "nope"))\n'
    )
    edited = source.replace("_CAP = 100", "_CAP = 200")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.generator-walrus") == (
        _fingerprint_for(edited, "shell_taint.py", "taint.demo.generator-walrus")
    )


def test_threshold_provenance_sees_a_nested_guard_condition() -> None:
    source = (
        "def _guard(a, b):\n"
        "    if a:\n"
        "        if b > _MAX_UNDECLARED_THING:\n"
        '            raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("_MAX_UNDECLARED_THING" in violation for violation in violations)


def test_a_prefixed_import_used_only_as_a_call_target_is_not_a_threshold() -> None:
    source = (
        "from helpers import _MAX_MEASURE\n"
        "def _guard(items, limits):\n"
        "    if _MAX_MEASURE(items) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.prefixed-call", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_a_generically_named_module_threshold_is_rejected() -> None:
    # A resource bound does not have to be spelled `_MAX_...` to be one, so recognizing thresholds
    # by naming convention alone leaves an unregistered cap with no provenance.
    source = (
        "ITEM_CEILING = 100\n"
        "def _guard(items):\n"
        "    if len(items) > ITEM_CEILING:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("ITEM_CEILING" in violation for violation in violations)


def test_a_bare_literal_guard_threshold_is_rejected() -> None:
    source = (
        "def _guard(items):\n"
        "    if len(items) > 100:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("literal 100" in violation for violation in violations)


def test_a_threshold_computed_one_statement_before_the_guard_is_rejected() -> None:
    # Reading the condition alone let a new resource bound ship by taking one hop away from the
    # guard: the condition then holds nothing but a boolean's name, and the magnitude deciding it
    # sits in the writer that produced that boolean.
    source = (
        "def _guard(items):\n"
        "    too_many = len(items) > 100\n"
        "    if too_many:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("literal 100" in violation for violation in violations)


def test_a_named_threshold_reached_through_the_writer_closure_is_rejected() -> None:
    source = (
        "_BUDGET = 512\n"
        "def _guard(items):\n"
        "    cap = _BUDGET\n"
        "    too_many = len(items) > cap\n"
        "    if too_many:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("_BUDGET" in violation for violation in violations)


def test_threshold_provenance_sees_a_preceding_reachability_test() -> None:
    source = (
        "def _guard(items):\n"
        "    if len(items) <= 100:\n"
        "        return\n"
        '    raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("literal 100" in violation for violation in violations)


def test_threshold_provenance_sees_a_preceding_reachability_writer() -> None:
    source = (
        "def _guard(items):\n"
        "    within_bound = len(items) <= 100\n"
        "    if within_bound:\n"
        "        return\n"
        '    raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("literal 100" in violation for violation in violations)


def test_a_limits_field_in_a_preceding_reachability_test_is_accepted() -> None:
    source = (
        "def _guard(items, limits):\n"
        "    if len(items) <= limits.max_items:\n"
        "        return\n"
        '    raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_a_dynamic_offset_in_a_preceding_loop_is_not_a_threshold() -> None:
    source = (
        "def _guard(text, start):\n"
        "    index = start + 2\n"
        "    while index < len(text):\n"
        "        index += 1\n"
        '    raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_a_computed_local_threshold_in_a_preceding_control_is_rejected() -> None:
    source = (
        "def _guard(items):\n"
        "    limit = 50 * 2\n"
        "    if len(items) <= limit:\n"
        "        return\n"
        '    raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold limit" in violation for violation in violations)


def test_a_writer_the_guard_condition_does_not_read_is_not_a_threshold() -> None:
    # The closure is the one the fingerprint records, so an unrelated magnitude in the same
    # function stays out of the rule and cannot fail the gate for a guard it does not decide.
    source = (
        "def _guard(items, other):\n"
        "    unrelated = len(other) > 100\n"
        "    if items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_structural_comparison_literals_are_accepted() -> None:
    # Emptiness and arity are not magnitudes: `remaining < 1` asks whether a limits-seeded counter
    # is exhausted, and `!= 1` asks about arity.
    source = (
        "def _guard(results, remaining):\n"
        "    if len(results) != 1 or remaining < 1:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_a_module_constant_that_is_not_numeric_is_not_a_threshold() -> None:
    source = (
        '_EVAL_REASON = "shell taint eval command substitution cannot be bounded"\n'
        "def _guard(value):\n"
        "    if value:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", _EVAL_REASON))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_an_inventoried_fixed_semantic_bound_is_accepted() -> None:
    source = (
        "_MAX_BRACE_INTEGER_DIGITS = 256\n"
        "def _guard(digits):\n"
        "    if digits > _MAX_BRACE_INTEGER_DIGITS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_a_generically_named_from_import_threshold_is_rejected() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "def _guard(items):\n"
        "    if len(items) > MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.imported", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


@pytest.mark.parametrize(
    ("binding", "setup", "compared", "threshold"),
    [
        ("from constants import MAX_ITEMS as CAP", "", "CAP", "CAP"),
        ("import constants", "", "constants.MAX_ITEMS", "constants.MAX_ITEMS"),
        ("from constants import MAX_ITEMS", "    cap = MAX_ITEMS\n", "cap", "MAX_ITEMS"),
    ],
)
def test_imported_threshold_spellings_are_rejected(
    binding: str,
    setup: str,
    compared: str,
    threshold: str,
) -> None:
    source = (
        f"{binding}\n"
        "def _guard(items):\n"
        f"{setup}"
        f"    if len(items) > {compared}:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.imported", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any(f"guard threshold {threshold}" in violation for violation in violations)


def test_an_imported_inventoried_fixed_semantic_bound_is_accepted() -> None:
    source = (
        "from .shell_taint import _MAX_SHELL_DESCRIPTOR_DIGITS\n"
        "def _guard(digits):\n"
        "    if len(digits) > _MAX_SHELL_DESCRIPTOR_DIGITS:\n"
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.descriptor", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_scanner.py") == ()


def test_unrelated_imports_and_imported_measurement_callables_are_not_thresholds() -> None:
    direct = (
        "from helpers import measure, UNUSED\n"
        "def _guard(items, limits):\n"
        "    if measure(items) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.measured", "nope"))\n'
    )
    forwarded = (
        "from helpers import measure, UNUSED\n"
        "def _guard(items, limits):\n"
        "    metric = measure\n"
        "    if metric(items) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.forwarded", "nope"))\n'
    )

    assert checker.find_threshold_violations(direct, "shell_taint.py") == ()
    assert checker.find_threshold_violations(forwarded, "shell_taint.py") == ()


def test_a_function_local_imported_threshold_is_rejected() -> None:
    source = (
        "def _guard(items):\n"
        "    from constants import MAX_ITEMS\n"
        "    if len(items) > MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.local-import", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_a_function_local_imported_measurement_callable_is_not_a_threshold() -> None:
    source = (
        "def _guard(items, limits):\n"
        "    from helpers import measure\n"
        "    if measure(items) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.local-measure", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_an_imported_callback_in_measured_dataflow_is_not_a_threshold() -> None:
    source = (
        "from helpers import measure\n"
        "def _guard(items, limits):\n"
        "    metric = map(measure, items)\n"
        "    if len(metric) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.callback", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_a_direct_imported_callback_in_measured_dataflow_is_not_a_threshold() -> None:
    source = (
        "from helpers import measure\n"
        "def _guard(items, limits):\n"
        "    if len(list(map(measure, items))) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.direct-callback", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_nested_limits_access_does_not_suppress_an_imported_threshold() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "def _guard(items, limits):\n"
        "    if len(items[:limits.max_items]) > MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.nested-limits", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_a_limits_operand_in_a_comparison_chain_does_not_suppress_another_pair() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "def _guard(items, limits):\n"
        "    if len(items) > MAX_ITEMS > limits.min_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.chained-limits", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_an_imported_callback_is_not_a_threshold_when_paired_with_an_imported_cap() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "from helpers import measure\n"
        "def _guard(items):\n"
        "    if len(list(map(measure, items))) > MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.imported-pair", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)
    assert not any("guard threshold measure" in violation for violation in violations)


def test_a_call_wrapped_forwarded_imported_threshold_is_rejected() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "def _guard(items):\n"
        "    cap = max(MAX_ITEMS, 1)\n"
        "    if len(items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.wrapped-cap", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_a_direct_call_wrapped_imported_threshold_is_rejected() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "def _guard(count):\n"
        "    if count > max(MAX_ITEMS, 1):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.direct-wrapped-cap", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_a_forwarded_measured_callback_loses_to_a_direct_imported_cap() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "from helpers import measure\n"
        "def _guard(items):\n"
        "    count = len(list(map(measure, items)))\n"
        "    if count > MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.forwarded-pair", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)
    assert not any("guard threshold measure" in violation for violation in violations)


def test_a_forwarded_direct_cap_beats_an_unknown_callees_imported_argument() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "from helpers import measure\n"
        "def _guard(items):\n"
        "    cap = MAX_ITEMS\n"
        "    if reduce(measure, items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.forwarded-cap", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_equal_unknown_callee_argument_evidence_remains_conservative() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "from helpers import measure\n"
        "def _guard(items):\n"
        "    if aggregate(measure, items) > max(MAX_ITEMS, 1):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.equal-evidence", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)
    assert any("guard threshold measure" in violation for violation in violations)


def test_transitive_writer_scores_include_the_comparison_operand_path() -> None:
    source = (
        "from constants import MAX_ITEMS\n"
        "from helpers import measure\n"
        "callback = measure\n"
        "cap = max(MAX_ITEMS, 1)\n"
        "def _guard(items):\n"
        "    if aggregate(callback, items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.transitive-score", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_keyword_spelled_text_refusal_is_rejected() -> None:
    source = (
        "def scan_doc_lattice_invocations(script):\n"
        '    return ShellScanResult(invocations=(), verdict="source character limit exceeded")\n'
    )

    violations = checker.find_shape_violations(source, "shell_scanner.py")

    assert any("raw refusal text" in violation for violation in violations)


def test_keyword_spelled_exception_text_is_rejected() -> None:
    source = 'def f():\n    raise _TaintLimitExceeded(refusal="shell taint edge limit exceeded")\n'

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("raw refusal text" in violation for violation in violations)


def test_refusal_exception_alias_still_rejects_raw_text() -> None:
    source = (
        "LimitError = _TaintLimitExceeded\n"
        "def f():\n"
        '    raise LimitError("shell taint edge limit exceeded")\n'
    )

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("raw refusal text" in violation for violation in violations)


def test_configured_limits_construction_away_from_a_boundary_is_rejected() -> None:
    # Rejecting only the zero-argument spelling lets a helper restore production-scale caps under
    # a shrunk scan budget.
    source = "def _helper(e):\n    return _evaluate(e, TaintLimits(max_edges=50_000))\n"

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("constructs" in violation for violation in violations)


def test_repository_gate_reports_every_implemented_property() -> None:
    assert checker.main(["--root", str(_ROOT)]) == 0


def test_repository_gate_fails_on_a_limits_violation(tmp_path: Path, capsys) -> None:
    root = _fake_root(tmp_path, [])
    module = root / checker.GUARDED_MODULES[0]
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\n\ndef _late_helper(e):\n    return _evaluate(e, TaintLimits())\n",
        encoding="utf-8",
    )

    assert checker.main(["--root", str(root)]) == 1
    assert "constructs default limits" in capsys.readouterr().err


def test_classified_ids_are_read_from_the_registry_as_data() -> None:
    classified = checker.classified_origin_ids(_ROOT)

    assert "taint.evidence.stream-scope-kind" in classified
    assert "scanner.source.character-limit" in classified


def _registry_source(*, reachable: str = "()", invariant: str = "()") -> str:
    return f"REACHABLE_WITNESSES = {reachable}\nINVARIANT_WITNESSES = {invariant}\n"


def test_classified_ids_never_execute_the_candidate_registry(tmp_path: Path) -> None:
    # The checker runs from the protected base against a candidate tree, so the candidate's
    # registry must be parsed rather than imported.
    root = _fake_root(tmp_path, [])
    (root / checker.REGISTRY_PATH).write_text(
        'raise SystemExit("importing the candidate registry would run this")\n'
        'REACHABLE_WITNESSES = (ReachableWitness("taint.demo.parsed", "script"),)\n'
        "INVARIANT_WITNESSES = ()\n",
        encoding="utf-8",
    )

    assert checker.classified_origin_ids(root) == frozenset({"taint.demo.parsed"})


@pytest.mark.parametrize(
    ("source", "registry_name"),
    [
        ("REACHABLE_WITNESSES = ()\n", "INVARIANT_WITNESSES"),
        (
            _registry_source() + "REACHABLE_WITNESSES = ()\n",
            "REACHABLE_WITNESSES",
        ),
        (
            _registry_source() + "if True:\n    REACHABLE_WITNESSES = ()\n",
            "REACHABLE_WITNESSES",
        ),
        (
            _registry_source(reachable="[]"),
            "REACHABLE_WITNESSES",
        ),
    ],
)
def test_registry_requires_one_direct_tuple_binding(
    source: str,
    registry_name: str,
) -> None:
    with pytest.raises(ValueError, match=registry_name):
        checker.classified_ids_in_registry(source)


@pytest.mark.parametrize(
    ("reachable", "message"),
    [
        (
            '(InvariantWitness("taint.demo.x", "rationale", "script", lambda evidence: True),)',
            "ReachableWitness",
        ),
        ('(ReachableWitness("taint.demo.x"),)', "script"),
        ('(ReachableWitness("taint.demo.x", "script", unexpected=True),)', "unexpected"),
        (
            '(ReachableWitness("taint.demo.x", "script", script="other"),)',
            "script",
        ),
        ("(ReachableWitness(*values),)", "starred"),
        (
            '(ReachableWitness("taint.demo.x", "script", **options),)',
            "keyword expansion",
        ),
        (
            '(ReachableWitness("taint.demo.x", "script", None, None, None, None),)',
            "positional",
        ),
    ],
)
def test_registry_rejects_malformed_reachable_entries(
    reachable: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        checker.classified_ids_in_registry(_registry_source(reachable=reachable))


def test_registry_requires_invariant_boundary_evidence() -> None:
    source = _registry_source(
        invariant='(InvariantWitness("taint.demo.x", "rationale", "script"),)'
    )

    with pytest.raises(ValueError, match="boundary_evidence"):
        checker.classified_ids_in_registry(source)


def test_registry_accepts_required_evidence_by_keyword() -> None:
    source = _registry_source(
        reachable='(ReachableWitness(origin_id="taint.demo.x", script="script"),)'
    )

    assert checker.classified_ids_in_registry(source) == frozenset({"taint.demo.x"})


def test_closure_rejects_a_classification_outside_the_executable_registry(
    tmp_path: Path,
) -> None:
    origin_id = "scanner.descriptor.unparsable"
    debt = [
        record.as_json()
        for record in checker.load_debt_records(_ROOT)
        if record.origin_id != origin_id
    ]
    root = _fake_root(tmp_path, debt)
    registry = root / checker.REGISTRY_PATH
    registry.write_text(
        registry.read_text(encoding="utf-8") + f'\nUNUSED = ReachableWitness("{origin_id}", "")\n',
        encoding="utf-8",
    )

    violations = checker.repository_closure_violations(root)

    assert any(
        f"{origin_id} is neither classified nor frozen as debt" in violation
        for violation in violations
    )


def test_closure_holds_for_the_shipped_tree() -> None:
    assert checker.repository_closure_violations(_ROOT) == ()


def test_closure_rejects_a_guard_that_is_neither_classified_nor_frozen(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])
    (root / checker.REGISTRY_PATH).write_text(
        'REACHABLE_WITNESSES = (ReachableWitness("scanner.source.character-limit", "x"),)\n'
        "INVARIANT_WITNESSES = ()\n",
        encoding="utf-8",
    )

    violations = checker.repository_closure_violations(root)

    assert any("neither classified nor frozen" in violation for violation in violations)


def test_closure_rejects_a_guard_that_is_both_classified_and_frozen(tmp_path: Path) -> None:
    record = next(
        r
        for r in checker.repository_origin_records(_ROOT)
        if r.origin_id == "scanner.source.character-limit"
    )
    root = _fake_root(tmp_path, [record.as_json()])

    violations = checker.repository_closure_violations(root)

    assert any("both classified and frozen" in violation for violation in violations)


_ATTRIBUTE_SPELLED_SOURCE = """
def _guard(value, limits):
    if value > limits.max_value:
        raise shell_guards._TaintLimitExceeded(
            shell_guards.GuardRefusal("taint.demo.attribute", "too big")
        )
"""


def test_a_refusal_spelled_through_a_module_is_still_a_guard_origin() -> None:
    # Recognizing only a bare-name constructor would make the identical guard, spelled through the
    # module that defines it, invisible to every rule here: no record, no witness, no debt entry.
    records = checker.extract_origin_records(_ATTRIBUTE_SPELLED_SOURCE, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.attribute"]


def test_raw_refusal_text_spelled_through_a_module_is_rejected() -> None:
    source = 'def f():\n    raise shell_guards._ShellScanIncomplete("step limit exceeded")\n'

    violations = checker.find_shape_violations(source, "shell_scanner.py")

    assert any("raw refusal text" in violation for violation in violations)


def test_limits_construction_spelled_through_a_module_is_rejected() -> None:
    source = "def _helper(e):\n    return _evaluate(e, shell_guards.TaintLimits())\n"

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("constructs default limits" in violation for violation in violations)


def test_limits_construction_spelled_through_an_import_alias_is_rejected() -> None:
    source = (
        "from doc_lattice.github_ci.shell_guards import TaintLimits as Limits\n"
        "def _helper(e):\n"
        "    return _evaluate(e, Limits())\n"
    )

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("constructs default limits" in violation for violation in violations)


def test_rebound_limits_constructor_used_as_a_default_factory_is_rejected() -> None:
    source = (
        "Limits = shell_guards.TaintLimits\n"
        "@dataclass\n"
        "class _Helper:\n"
        "    limits: object = field(default_factory=Limits)\n"
    )

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("constructs default limits" in violation for violation in violations)


_ACCUMULATED_SOURCE = """
class _Tracker:
    def visit(self, node):
        self.active.add(node)
        if len(self.active) > self.limits.max_active:
            raise _TaintLimitExceeded(GuardRefusal("taint.demo.accumulated", "too many"))
"""


def test_fingerprint_tracks_an_in_place_mutation_that_feeds_the_guard_condition() -> None:
    # Accumulation through a mutating method binds no name, so a writer closure built from binding
    # statements alone would leave the call that feeds this guard outside its record.
    edited = _ACCUMULATED_SOURCE.replace("self.active.add(node)", "self.active.clear()")

    original = checker.extract_origin_records(_ACCUMULATED_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


_WHILE_SOURCE = """
def _guard(state, limits):
    while state.depth > limits.max_depth:
        raise _TaintLimitExceeded(GuardRefusal("taint.demo.while", "too deep"))
"""

_MATCH_SOURCE = """
def _guard(node, limits):
    match node:
        case int() if node > limits.max_value:
            raise _TaintLimitExceeded(GuardRefusal("taint.demo.match", "too big"))
"""


def test_fingerprint_tracks_a_while_condition() -> None:
    retargeted = _WHILE_SOURCE.replace("state.depth > limits.max_depth", "state.depth < 0")

    original = checker.extract_origin_records(_WHILE_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(retargeted, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_a_match_case_pattern_and_guard() -> None:
    repatterned = _MATCH_SOURCE.replace("case int()", "case str()")
    reguarded = _MATCH_SOURCE.replace("node > limits.max_value", "node < limits.max_value")

    original = checker.extract_origin_records(_MATCH_SOURCE, "shell_taint.py")

    assert original[0].fingerprint != (
        checker.extract_origin_records(repatterned, "shell_taint.py")[0].fingerprint
    )
    assert original[0].fingerprint != (
        checker.extract_origin_records(reguarded, "shell_taint.py")[0].fingerprint
    )


def test_threshold_provenance_sees_a_while_and_a_match_case_guard() -> None:
    for source in (
        _WHILE_SOURCE.replace("limits.max_depth", "100"),
        _MATCH_SOURCE.replace("limits.max_value", "100"),
    ):
        violations = checker.find_threshold_violations(source, "shell_taint.py")

        assert any("literal 100" in violation for violation in violations)


def test_a_magnitude_hidden_in_arithmetic_is_still_a_threshold() -> None:
    # `depth - 4096 > 0` caps the scan exactly as `depth > 4096` does, so a rule that only matched
    # an operand that is itself a literal would fall to a one-line algebraic rewrite.
    source = (
        "def _guard(depth):\n"
        "    if depth - 4096 > 0:\n"
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_scanner.py")

    assert any("literal 4096" in violation for violation in violations)


def test_a_subscript_index_inside_a_comparison_is_not_a_threshold() -> None:
    # A position is not a magnitude: `words[2]` names the third word of a fixed grammar.
    source = (
        "def _guard(words):\n"
        '    if words[2].literal != "in":\n'
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.x", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_scanner.py") == ()


_TRANSPORT_SOURCE = '''
def _validate_acyclic_graph(graph, *, refusal):
    """Reject directed cycles without recursive graph traversal."""
    active = set()
    for node in graph:
        if node in active:
            raise _MalformedTaintEvidence(refusal)


def _validate(graph):
    _validate_acyclic_graph(
        graph,
        refusal=GuardRefusal("taint.demo.cycle", "cannot be structured"),
    )
'''


def test_fingerprint_tracks_the_declared_transport_that_decides_the_refusal() -> None:
    # The parameterized cycle detector is a declared transport, so it mints no identifier and is
    # not an origin. It nonetheless owns the condition that decides its callers' refusals, and
    # inverting that condition would otherwise leave every caller's record byte-identical.
    inverted = _TRANSPORT_SOURCE.replace("if node in active:", "if node not in active:")

    original = checker.extract_origin_records(_TRANSPORT_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(inverted, "shell_taint.py")

    assert original[0].origin_id == "taint.demo.cycle"
    assert original[0].fingerprint != updated[0].fingerprint


def test_closure_rejects_a_second_origin_reusing_a_classified_identifier(tmp_path: Path) -> None:
    # Comparing identifier sets alone lets an unwitnessed guard inherit another guard's evidence:
    # every set-level relation still holds while a brand new fail-closed site ships unclassified.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    module = root / checker.GUARDED_MODULES[1]
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\n\ndef _injected(count, budget):\n"
        + "    if count > budget.limits.scanner.max_invocations:\n"
        + "        raise _ShellScanIncomplete(\n"
        + '            GuardRefusal("scanner.source.character-limit", "x")\n'
        + "        )\n",
        encoding="utf-8",
    )

    violations = checker.repository_closure_violations(root)

    assert any("is constructed at 2 guard origins" in violation for violation in violations)


def _base_inputs(root: Path) -> tuple[str, str]:
    """Return a base snapshot and base registry that match this candidate tree."""
    snapshot = json.dumps(
        {
            "schema": checker.SCHEMA_VERSION,
            "records": [record.as_json() for record in checker.load_debt_records(root)],
        }
    )
    return snapshot, (_ROOT / checker.REGISTRY_PATH).read_text(encoding="utf-8")


def test_compare_against_base_rejects_a_guard_withdrawn_with_its_witness(tmp_path: Path) -> None:
    # Deleting an origin together with its witness row leaves the closure partition exact and
    # leaves the debt comparison nothing to inspect, so removal needs its own gate.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    snapshot, registry = _base_inputs(root)
    module = root / checker.GUARDED_MODULES[1]
    module.write_text(
        module.read_text(encoding="utf-8").replace(
            '"scanner.marker.non-invocation-command"', '"scanner.marker.renamed"', 1
        ),
        encoding="utf-8",
    )

    failures = checker.compare_against_base(root, snapshot, base_registry=registry)

    assert any("scanner.marker.non-invocation-command" in failure for failure in failures)


def test_compare_against_base_accepts_a_withdrawal_the_retirement_ledger_records(
    tmp_path: Path,
) -> None:
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    snapshot, registry = _base_inputs(root)
    module = root / checker.GUARDED_MODULES[1]
    module.write_text(
        module.read_text(encoding="utf-8").replace(
            '"scanner.marker.non-invocation-command"', '"scanner.marker.renamed"', 1
        ),
        encoding="utf-8",
    )
    (root / checker.RETIREMENT_PATH).write_text(
        json.dumps(
            {
                "schema": 1,
                "records": [
                    {
                        "origin_id": "scanner.marker.non-invocation-command",
                        "reason": "renamed with its witness in the same change",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    failures = checker.compare_against_base(root, snapshot, base_registry=registry)

    assert not [f for f in failures if "scanner.marker.non-invocation-command" in f]


def test_a_retirement_without_a_reason_is_rejected(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])
    (root / checker.RETIREMENT_PATH).write_text(
        json.dumps({"schema": 1, "records": [{"origin_id": "taint.demo.x", "reason": "  "}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="why it was retired"):
        checker.load_retired_origin_ids(root)


def test_the_shipped_tree_retires_nothing_unexpectedly() -> None:
    assert checker.load_retired_origin_ids(_ROOT) == frozenset()


def test_the_base_owned_run_does_not_apply_its_own_allowlists_to_the_candidate(
    tmp_path: Path,
) -> None:
    # This checker is the base revision's copy under `--compare-base`, so its transport, limits and
    # threshold allowlists describe the base's source. A candidate that adds a boundary, a declared
    # transport or an inventoried bound edits those allowlists in its own copy, which the base one
    # cannot see; enforcing them here would reject the change with no fix available inside it, and
    # on a push to main it would take the release job down with it.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    snapshot, _registry = _base_inputs(root)
    base = tmp_path / "base.json"
    base.write_text(snapshot, encoding="utf-8")
    module = root / checker.GUARDED_MODULES[0]
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\n\ndef _late_boundary(e):\n    return _evaluate(e, TaintLimits())\n",
        encoding="utf-8",
    )

    assert any("constructs default limits" in v for v in checker.repository_limits_violations(root))
    assert checker.main(["--root", str(root), "--compare-base", str(base)]) == 0
    assert checker.main(["--root", str(root)]) == 1


def test_the_base_owned_run_still_enforces_closure(tmp_path: Path) -> None:
    # Closure names no allowlist: it derives the whole partition from the candidate tree, so it is
    # the one tree-local property the base-owned run must keep.
    root = _fake_root(tmp_path, [])
    snapshot, _registry = _base_inputs(root)
    base = tmp_path / "base.json"
    base.write_text(snapshot, encoding="utf-8")

    assert checker.main(["--root", str(root), "--compare-base", str(base)]) == 1


def test_emit_debt_derives_the_unclassified_records(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])

    payload = json.loads(checker.emit_records(root))
    derived = {record["origin_id"] for record in payload["records"]}

    assert payload["schema"] == checker.SCHEMA_VERSION
    assert derived == {
        record.origin_id for record in checker.repository_origin_records(root)
    } - checker.classified_origin_ids(root)


def test_checked_in_debt_snapshot_matches_current_derivation() -> None:
    assert (_ROOT / checker.DEBT_PATH).read_text(encoding="utf-8") == checker.emit_records(_ROOT)


_FALL_THROUGH_SOURCE = """
def _classify(node):
    if isinstance(node, Sequence):
        return node.parts
    if isinstance(node, Command):
        return ()
    raise _MalformedTaintEvidence(GuardRefusal("taint.demo.unknown-node", "cannot structure"))
"""

_LOOP_EXHAUSTION_SOURCE = """
def _read_literal(text, start):
    index = start
    while index < len(text):
        if text[index] == "'":
            return index + 1
        index += 1
    raise _TaintLimitExceeded(GuardRefusal("taint.demo.unterminated", "unterminated literal"))
"""


_ENCLOSING_LOOP_SOURCE = """
def _walk(lexer, metadata):
    tokens = tuple(lexer)
    index = 0
    for lexeme in tokens:
        if index >= len(metadata):
            raise _TaintLimitExceeded(GuardRefusal("taint.demo.exhausted", "count mismatch"))
        index += 1
    return index
"""


def test_fingerprint_tracks_the_loop_header_enclosing_a_tested_origin() -> None:
    # The guard has a test of its own, so the control-flow closure does not apply, yet the loop
    # header still decides whether the body runs at all. Emptying the iterable withdraws the guard
    # without touching its condition or anything the condition reads.
    withdrawn = _ENCLOSING_LOOP_SOURCE.replace("for lexeme in tokens:", "for lexeme in tokens[:0]:")

    original = checker.extract_origin_records(_ENCLOSING_LOOP_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(withdrawn, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_the_writer_feeding_an_enclosing_loops_iterable() -> None:
    # Recording the header alone is not enough: `for lexeme in tokens` reads identically whether
    # `tokens` can be non-empty or not, so the statement that fills it has to be in the record too.
    withdrawn = _ENCLOSING_LOOP_SOURCE.replace("tokens = tuple(lexer)", "tokens = ()")

    original = checker.extract_origin_records(_ENCLOSING_LOOP_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(withdrawn, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_ignores_an_edit_outside_the_loop_that_governs_nothing() -> None:
    # The negative control for both rules above: widening the closure must not make every edit in
    # the enclosing function churn a frozen record.
    edited = _ENCLOSING_LOOP_SOURCE.replace(
        "    return index", "    unused = len(metadata)\n    return index"
    )

    original = checker.extract_origin_records(_ENCLOSING_LOOP_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_a_function_defined_in_a_loop_body_keeps_its_own_qualified_name() -> None:
    # The loop rule walks the body itself rather than through the generic descent, so a nested
    # function there would keep its parent's qualified name and its parent's guarding condition.
    source = (
        "def _outer(items):\n"
        "    for item in items:\n"
        "        def _inner(value):\n"
        '            raise _TaintLimitExceeded(GuardRefusal("taint.demo.nested", "no"))\n'
        "        _inner(item)\n"
    )

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.qualname for record in records] == ["_outer._inner"]


def test_fingerprint_tracks_the_fall_through_chain_that_reaches_a_test_free_origin() -> None:
    # A guard reached by falling through a chain of returns has no test of its own, so nothing
    # about the code deciding it is reached would otherwise enter the record. Shadowing the last
    # branch withdraws the guard exactly as inverting a condition would.
    shadowed = _FALL_THROUGH_SOURCE.replace("isinstance(node, Command)", "True")

    original = checker.extract_origin_records(_FALL_THROUGH_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(shadowed, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_the_loop_a_test_free_origin_falls_out_of() -> None:
    widened = _LOOP_EXHAUSTION_SOURCE.replace("index < len(text)", "True")

    original = checker.extract_origin_records(_LOOP_EXHAUSTION_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(widened, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_the_early_return_a_test_free_origin_depends_on() -> None:
    # The diverting statement is taken whole rather than reduced to a header, because a `return`
    # decides reachability through the value it returns.
    diverted = _FALL_THROUGH_SOURCE.replace("return node.parts", "return ()")

    original = checker.extract_origin_records(_FALL_THROUGH_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(diverted, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_reachability_closure_ignores_computation_that_diverts_nothing() -> None:
    # Reducing a branch to its test is what keeps an ordinary edit inside a long function from
    # churning a frozen record that only the surrounding control flow describes.
    original_source = _LOOP_EXHAUSTION_SOURCE.replace(
        "index += 1", "unrelated = 1\n        index += 1"
    )
    edited = original_source.replace("unrelated = 1", "unrelated = 2")

    original = checker.extract_origin_records(original_source, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_reachability_closure_ignores_another_function() -> None:
    extended = _FALL_THROUGH_SOURCE + "\n\ndef _elsewhere(x):\n    if x:\n        return 1\n"

    original = checker.extract_origin_records(_FALL_THROUGH_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(extended, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


_PRECEDING_DIVERSION_SOURCE = (
    "def _guard(value, other):\n"
    "    if other:\n"
    "        return 1\n"
    "    if value > 3:\n"
    '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.over-three", "too big"))\n'
    "    return 0\n"
)


def test_fingerprint_tracks_the_diversion_that_precedes_a_guarded_origin() -> None:
    # A guard's test says what it refuses once control arrives, never whether control arrives.
    # Inverting an earlier branch that returns diverts execution around the guard, withdrawing it
    # as completely as inverting its own condition, and leaves test, writers and qualname intact.
    edited = _PRECEDING_DIVERSION_SOURCE.replace(
        "    if other:\n        return 1\n", "    if not other:\n        return 1\n"
    )

    original = checker.extract_origin_records(_PRECEDING_DIVERSION_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_a_writer_feeding_a_preceding_reachability_test() -> None:
    source = (
        "def _guard(run, payload):\n"
        "    dynamic_run = run.dynamic\n"
        "    if payload.index is not None or not dynamic_run:\n"
        "        return None\n"
        "    if payload.active:\n"
        '        raise _ShellScanIncomplete(GuardRefusal("scanner.demo.alternate", "x"))\n'
    )
    bypassed = source.replace("dynamic_run = run.dynamic", "dynamic_run = False")

    original = checker.extract_origin_records(source, "shell_scanner.py")
    updated = checker.extract_origin_records(bypassed, "shell_scanner.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_reachability_closure_ignores_a_diversion_that_can_only_run_later() -> None:
    # The churn boundary. A statement after the origin, with no loop to bring it back around,
    # cannot decide whether the origin was reached, so an edit there must not move the record: a
    # record that churns has to be regenerated, which is the laundering path the gate closes.
    edited = _PRECEDING_DIVERSION_SOURCE.replace(
        "    return 0\n", "    if value < 0:\n        return -1\n    return 0\n"
    )

    original = checker.extract_origin_records(_PRECEDING_DIVERSION_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_fingerprint_tracks_a_later_diversion_a_loop_brings_back_around() -> None:
    # Lexical order settles reachability everywhere except inside a loop, where a statement after
    # the origin runs again before the next iteration reaches it.
    source = (
        "def _guard(values):\n"
        "    for value in values:\n"
        "        if value > 3:\n"
        '            raise _TaintLimitExceeded(GuardRefusal("taint.demo.over-three", "too big"))\n'
        "        if value:\n"
        "            break\n"
        "    return 0\n"
    )
    edited = source.replace("        if value:\n", "        if not value:\n")

    original = checker.extract_origin_records(source, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_reachability_closure_survives_an_origin_at_module_level() -> None:
    source = 'raise _TaintLimitExceeded(GuardRefusal("taint.demo.module", "no scope"))\n'

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.module"]


def test_compare_against_base_rejects_a_frozen_guard_whose_raising_computation_changed(
    tmp_path: Path,
) -> None:
    # The end-to-end form: this guard fires only because `int(digits)` in its own `try` body can
    # raise, so rewriting that computation withdraws the refusal while touching neither the origin
    # statement nor its qualname.
    origin_id = "scanner.descriptor.unparsable"
    record = next(r for r in checker.repository_origin_records(_ROOT) if r.origin_id == origin_id)
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": [record.as_json()]})
    root = _fake_root(tmp_path, [record.as_json()])
    module = root / checker.GUARDED_MODULES[1]
    source = module.read_text(encoding="utf-8")
    unreachable = source.replace("        return int(digits)\n", "        return 0\n", 1)
    assert unreachable != source
    module.write_text(unreachable, encoding="utf-8")

    failures = checker.compare_against_base(root, base)

    assert any(origin_id in failure for failure in failures)


def test_compare_against_base_rejects_a_frozen_guard_whose_reachability_input_changed(
    tmp_path: Path,
) -> None:
    origin_id = "scanner.uv-tool.alternate-run-argv-expansion"
    record = next(r for r in checker.repository_origin_records(_ROOT) if r.origin_id == origin_id)
    base = json.dumps({"schema": checker.SCHEMA_VERSION, "records": [record.as_json()]})
    root = _fake_root(tmp_path, [record.as_json()])
    module = root / checker.GUARDED_MODULES[1]
    source = module.read_text(encoding="utf-8")
    bypassed = source.replace("    dynamic_run = run.dynamic\n", "    dynamic_run = False\n", 1)
    assert bypassed != source
    module.write_text(bypassed, encoding="utf-8")

    failures = checker.compare_against_base(root, base)

    assert any(origin_id in failure for failure in failures)


def test_closure_rejects_a_retirement_for_a_guard_that_is_still_live(tmp_path: Path) -> None:
    # A premature row has no effect on the withdrawal comparison, so nothing would reject it when
    # it is written; a later change could then delete that guard and have its removal absorbed by a
    # row it did not add, with no ledger diff in the change that actually withdraws the guard.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    (root / checker.RETIREMENT_PATH).write_text(
        json.dumps(
            {
                "schema": 1,
                "records": [
                    {
                        "origin_id": "scanner.source.character-limit",
                        "reason": "planted ahead of the removal",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    violations = checker.repository_closure_violations(root)

    assert any("still a guard origin" in violation for violation in violations)


def test_closure_keeps_a_retirement_for_a_guard_that_is_gone(tmp_path: Path) -> None:
    # The ledger is a permanent record: its rows stay valid after the withdrawal merges, which is
    # what keeps the diff that recorded the removal in the tree.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    (root / checker.RETIREMENT_PATH).write_text(
        json.dumps(
            {
                "schema": 1,
                "records": [{"origin_id": "taint.demo.long-gone", "reason": "withdrawn in #179"}],
            }
        ),
        encoding="utf-8",
    )

    assert checker.repository_closure_violations(root) == ()


def test_a_refusal_spelled_through_an_import_alias_is_still_a_guard_origin() -> None:
    # Every rule here recognizes a construction by name, and a verdict return is the one position
    # where an aliased construction is a well-formed verdict no carrier rule rejects. An origin
    # invisible to the extractor leaves the closure partition exact while an unwitnessed
    # fail-closed guard ships.
    source = (
        "from doc_lattice.github_ci.shell_guards import GuardRefusal as GR\n"
        "def analyze_marker_taint(evidence, *, limits):\n"
        "    if evidence.edges:\n"
        '        return GR("taint.demo.aliased", "nope")\n'
        "    return Certified()\n"
    )

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.aliased"]


def test_a_refusal_spelled_through_a_module_level_rebinding_is_still_a_guard_origin() -> None:
    source = (
        "from doc_lattice.github_ci.shell_guards import GuardRefusal\n"
        "_REFUSE = GuardRefusal\n"
        "def analyze_marker_taint(evidence, *, limits):\n"
        "    if evidence.edges:\n"
        '        return _REFUSE("taint.demo.rebound", "nope")\n'
        "    return Certified()\n"
    )

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.rebound"]


def test_a_refusal_spelled_through_an_attribute_rebinding_is_still_a_guard_origin() -> None:
    # A call is recognized by its final name component, so `shell_guards.GuardRefusal(...)` is an
    # origin; reading only bare-name bindings let the same spelling bound to a name escape.
    source = (
        "from doc_lattice.github_ci import shell_guards\n"
        "_REFUSE = shell_guards.GuardRefusal\n"
        "def analyze_marker_taint(evidence, *, limits):\n"
        "    if evidence.edges:\n"
        '        return _REFUSE("taint.demo.attribute-rebound", "nope")\n'
        "    return Certified()\n"
    )

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.attribute-rebound"]


def test_a_refusal_spelled_through_an_annotated_rebinding_is_still_a_guard_origin() -> None:
    source = (
        "from doc_lattice.github_ci import shell_guards\n"
        "_REFUSE: Final = shell_guards.GuardRefusal\n"
        "def analyze_marker_taint(evidence, *, limits):\n"
        "    if evidence.edges:\n"
        '        return _REFUSE("taint.demo.annotated-rebound", "nope")\n'
        "    return Certified()\n"
    )

    records = checker.extract_origin_records(source, "shell_taint.py")

    assert [record.origin_id for record in records] == ["taint.demo.annotated-rebound"]


def test_an_unrelated_attribute_binding_is_not_read_as_the_refusal_constructor() -> None:
    source = (
        "from doc_lattice.github_ci import shell_guards\n"
        "_BUILD = shell_guards.Certified\n"
        "def analyze_marker_taint(evidence, *, limits):\n"
        "    return _BUILD()\n"
    )

    assert checker.extract_origin_records(source, "shell_taint.py") == ()


def test_a_refusal_carrier_module_with_an_indirect_payload_is_discovered(
    tmp_path: Path,
) -> None:
    root = _fake_root(tmp_path, [])
    module = root / checker.GUARD_MODULE_ROOT / "indirect_refusal.py"
    module.write_text(
        "def _guard():\n"
        '    raise _ShellScanIncomplete(_REFUSALS[0]("scanner.demo.indirect", "nope"))\n',
        encoding="utf-8",
    )

    coverage = checker.repository_coverage_violations(root)
    shapes = checker.repository_shape_violations(root)

    assert any("indirect_refusal.py" in violation for violation in coverage)
    assert any("indirect_refusal.py" in violation for violation in shapes)
    assert any("undeclared transport" in violation for violation in shapes)


def test_a_discovered_refusal_carrier_alias_is_shape_checked(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])
    module = root / checker.GUARD_MODULE_ROOT / "raw_refusal.py"
    module.write_text(
        "from somewhere import _ShellScanIncomplete as StopScan\n"
        "def _guard():\n"
        '    raise StopScan("raw refusal")\n',
        encoding="utf-8",
    )

    violations = checker.repository_shape_violations(root)

    assert any("raw_refusal.py" in violation for violation in violations)
    assert any("raw refusal text" in violation for violation in violations)


def test_a_verdict_producer_module_is_discovered_and_shape_checked(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])
    module = root / checker.GUARD_MODULE_ROOT / "verdict_boundary.py"
    module.write_text(
        'def analyze_marker_taint(evidence):\n    return "raw refusal"\n',
        encoding="utf-8",
    )

    coverage = checker.repository_coverage_violations(root)
    shapes = checker.repository_shape_violations(root)

    assert any("verdict_boundary.py" in violation for violation in coverage)
    assert any("verdict_boundary.py" in violation for violation in shapes)
    assert any("returns raw refusal text" in violation for violation in shapes)


def test_a_refusing_module_outside_the_guarded_tuple_is_rejected(tmp_path: Path) -> None:
    # GUARDED_MODULES is hand-maintained and drives limits and threshold checks, so discovery must
    # reject an omitted module even though origin derivation and shape validation already see it.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    (root / checker.GUARD_MODULE_ROOT / "shell_taint_eval.py").write_text(
        "def _bound(depth, limits):\n"
        "    if depth > limits.taint.max_eval_reparse_depth:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.eval.new-bound", "too deep"))\n',
        encoding="utf-8",
    )

    violations = checker.repository_coverage_violations(root)

    assert any("shell_taint_eval.py" in violation for violation in violations)
    assert any("not in GUARDED_MODULES" in violation for violation in violations)


def test_a_refusing_module_in_a_guard_subpackage_is_rejected(tmp_path: Path) -> None:
    # A module one directory down is exactly as invisible to GUARDED_MODULES as one beside it, so
    # a top-level-only walk would be satisfied by moving a new guard into a subpackage.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    nested = root / checker.GUARD_MODULE_ROOT / "guards" / "eval_bounds.py"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text(
        "def _bound(depth, limits):\n"
        "    if depth > limits.taint.max_eval_reparse_depth:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.eval.new-bound", "too deep"))\n',
        encoding="utf-8",
    )

    violations = checker.repository_coverage_violations(root)

    assert any("guards/eval_bounds.py" in violation for violation in violations)


def test_base_owned_closure_discovers_a_classified_guard_in_a_new_candidate_module(
    tmp_path: Path,
) -> None:
    origin_id = "taint.eval.new-bound"
    root = _fake_root(
        tmp_path,
        [record.as_json() for record in checker.load_debt_records(_ROOT)],
    )
    nested = root / checker.GUARD_MODULE_ROOT / "guards" / "eval_bounds.py"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text(
        "def _bound(depth, limits):\n"
        "    if depth > limits.taint.max_eval_reparse_depth:\n"
        f'        raise _TaintLimitExceeded(GuardRefusal("{origin_id}", "too deep"))\n',
        encoding="utf-8",
    )
    registry = root / checker.REGISTRY_PATH
    declaration = "REACHABLE_WITNESSES: tuple[ReachableWitness, ...] = (\n"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            declaration,
            declaration + f'    ReachableWitness("{origin_id}", "x"),\n',
            1,
        ),
        encoding="utf-8",
    )

    violations = checker.repository_closure_violations(root)

    assert not [violation for violation in violations if origin_id in violation]


def test_the_shipped_tree_inventories_every_refusing_module() -> None:
    assert checker.repository_coverage_violations(_ROOT) == ()


def test_the_base_owned_run_reads_a_candidate_snapshot_under_a_newer_schema(
    tmp_path: Path,
) -> None:
    # Closure runs from the base revision's copy against the candidate tree, so decoding the
    # candidate's snapshot against this copy's record schema would make a SCHEMA_VERSION bump fail
    # the base-owned job with no fix available inside the change that makes it.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    debt = root / checker.DEBT_PATH
    payload = json.loads(debt.read_text(encoding="utf-8"))
    payload["schema"] = checker.SCHEMA_VERSION + 1
    debt.write_text(json.dumps(payload), encoding="utf-8")

    assert checker.repository_closure_violations(root) == ()
    assert checker.repository_artifact_violations(root) != ()


def test_the_base_owned_run_reads_a_witness_strengthened_with_a_new_field(tmp_path: Path) -> None:
    # Same contract for the registry: validating the candidate's entries against this copy's field
    # lists would reject a witness the candidate strengthened, in the base's copy where the
    # candidate cannot fix it.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    registry = root / checker.REGISTRY_PATH
    registry.write_text(
        'REACHABLE_WITNESSES = (ReachableWitness("scanner.demo.only", "x", severity="high"),)\n'
        "INVARIANT_WITNESSES = ()\n",
        encoding="utf-8",
    )

    assert checker.registry_origin_ids(registry.read_text(encoding="utf-8")) == frozenset(
        {"scanner.demo.only"}
    )
    assert any(
        "unexpected field 'severity'" in violation
        for violation in checker.repository_artifact_violations(root)
    )


def test_the_base_owned_run_still_requires_a_literal_identifier(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])
    (root / checker.REGISTRY_PATH).write_text(
        "REACHABLE_WITNESSES = (ReachableWitness(_COMPUTED, 'x'),)\nINVARIANT_WITNESSES = ()\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="literal origin_id"):
        checker.repository_closure_violations(root)


def test_an_earlier_gate_still_reports_when_a_later_one_cannot_derive_its_answer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A non-literal identifier is both a clean shape violation and a condition the extractor
    # raises on. Building the failure list eagerly replaced the operator's report with a traceback
    # naming no gate and discarding every violation already computed.
    root = _fake_root(tmp_path, [record.as_json() for record in checker.load_debt_records(_ROOT)])
    module = root / checker.GUARDED_MODULES[1]
    module.write_text(
        module.read_text(encoding="utf-8").replace(
            'GuardRefusal("scanner.budget.step-limit", "step limit exceeded")',
            'GuardRefusal(_STEP_ID, "step limit exceeded")',
        ),
        encoding="utf-8",
    )

    assert checker.main(["--root", str(root)]) == 1

    reported = capsys.readouterr().err
    assert "identifier must be a string literal" in reported
    assert "closure: " in reported


_NESTED_CLOSURE_SOURCE = """
def _contextualize(evidence, limits):
    edges = evidence.edges

    def _unrelated(state):
        edges = state.rebuild()
        return edges

    if len(edges) > limits.taint.max_edges:
        raise _TaintLimitExceeded(GuardRefusal("taint.demo.edge-limit", "too many"))
"""


def test_writer_closure_ignores_a_write_inside_a_nested_function() -> None:
    # A nested body does not run where it is written, so a write inside it decides nothing about
    # the guard around it. Folding one in lets an edit to an unrelated closure churn a frozen
    # record, which is the regeneration path this gate exists to close.
    edited = _NESTED_CLOSURE_SOURCE.replace("state.rebuild()", "state.rebuild(limits)")

    original = checker.extract_origin_records(_NESTED_CLOSURE_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].origin_id == "taint.demo.edge-limit"
    assert original[0].fingerprint == updated[0].fingerprint


_HANDLER_SOURCE = """
def _tokenize(program):
    lexer = shlex.shlex(program, posix=True)
    lexer.commenters = ""
    lexer.whitespace_split = True
    try:
        tokens = tuple(lexer)
    except ValueError as error:
        raise _TaintLimitExceeded(
            GuardRefusal("taint.demo.lex-error", "payload cannot be tokenized")
        ) from error
    return tokens
"""


def test_fingerprint_tracks_the_object_state_that_configures_a_guarded_operation() -> None:
    # A guard reached through an `except` handler has no condition of its own, and the control-flow
    # closure records a `try` only through its handled exception types. Reconfiguring the lexer so
    # the operation stops raising withdraws the guard while leaving the record byte-identical.
    reconfigured = _HANDLER_SOURCE.replace('lexer.commenters = ""', 'lexer.quotes = ""').replace(
        "lexer.whitespace_split = True", 'lexer.escape = ""'
    )

    original = checker.extract_origin_records(_HANDLER_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(reconfigured, "shell_taint.py")

    assert original[0].origin_id == "taint.demo.lex-error"
    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_the_guarded_operation_itself() -> None:
    # Rewriting the raising call to one that cannot raise withdraws the guard just as completely.
    neutered = _HANDLER_SOURCE.replace("tokens = tuple(lexer)", "tokens = ()")

    original = checker.extract_origin_records(_HANDLER_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(neutered, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_fingerprint_tracks_the_guarded_operation_for_a_tested_handler_origin() -> None:
    source = (
        "def _tokenize(lexer, report):\n"
        "    try:\n"
        "        tokens = tuple(lexer)\n"
        "    except ValueError as error:\n"
        "        if report:\n"
        "            raise _TaintLimitExceeded(\n"
        '                GuardRefusal("taint.demo.tested-lex-error", "cannot tokenize")\n'
        "            ) from error\n"
        "    return tokens\n"
    )
    neutered = source.replace("tokens = tuple(lexer)", "tokens = ()")

    original = checker.extract_origin_records(source, "shell_taint.py")
    updated = checker.extract_origin_records(neutered, "shell_taint.py")

    assert original[0].fingerprint != updated[0].fingerprint


def test_a_handler_guard_record_ignores_an_unrelated_edit_in_the_same_scope() -> None:
    # The negative control for the two above: the closure is bounded by what the guarded body
    # reads, so an unrelated statement in the same function leaves the record alone.
    unrelated = _HANDLER_SOURCE.replace(
        "    return tokens\n", "    unrelated = _describe(program)\n    return tokens\n"
    )

    original = checker.extract_origin_records(_HANDLER_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(unrelated, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_a_computed_module_threshold_is_rejected() -> None:
    # Requiring a bare literal let a computed spelling escape both halves of the rule: the name is
    # absent from the numeric set, and being module-bound also exempts it from the prefix check.
    source = (
        "_MAX_ITEMS = 50 * 2\n"
        "def _guard(items):\n"
        "    if len(items) > _MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.computed", "x"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold _MAX_ITEMS" in violation for violation in violations)


def test_a_local_threshold_binding_is_rejected() -> None:
    source = (
        "def _guard(items):\n"
        "    limit = 100\n"
        "    if len(items) > limit:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.local", "x"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold limit" in violation for violation in violations)


@pytest.mark.parametrize(
    "signature",
    ["items, limit=100", "items, /, limit=100", "items, *, limit=100"],
)
def test_a_numeric_parameter_default_used_as_a_guard_threshold_is_rejected(
    signature: str,
) -> None:
    source = (
        f"def _guard({signature}):\n"
        "    if len(items) > limit:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.default", "x"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold limit" in violation for violation in violations)


def test_a_local_counter_and_a_local_limits_field_are_not_thresholds() -> None:
    # The negative controls for the rule above: a counter seeded at a structural literal is not a
    # magnitude, and a local bound to a limits field already has its provenance.
    counter = (
        "def _guard(limits):\n"
        "    depth = 0\n"
        "    if depth > limits.taint.max_edges:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.counter", "x"))\n'
    )
    threaded = (
        "def _guard(items, limits):\n"
        "    cap = limits.taint.max_edges\n"
        "    if len(items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.threaded", "x"))\n'
    )

    assert checker.find_threshold_violations(counter, "shell_taint.py") == ()
    assert checker.find_threshold_violations(threaded, "shell_taint.py") == ()


def test_fingerprint_tracks_a_mutation_in_a_generator_the_statement_consumes() -> None:
    # A generator handed to a call is exhausted by that call, so the accumulation in its body runs
    # and is what the guard's condition reads. Treating it as deferred let it be edited away with
    # the frozen record unchanged.
    source = (
        "_STATE = []\n"
        "def _guard(items):\n"
        "    any(_STATE.append(item) for item in items)\n"
        "    if _STATE:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.consumed-generator", "nope"))\n'
    )
    edited = source.replace("_STATE.append(item)", "_STATE.append(item + 1)")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.consumed-generator"
    ) != _fingerprint_for(edited, "shell_taint.py", "taint.demo.consumed-generator")


def test_fingerprint_tracks_a_writer_read_through_a_shadowed_attribute_subscript() -> None:
    # `word` is a comprehension target, but `index` still resolves in the enclosing scope, so the
    # statement writing it is part of the closure.
    source = (
        "def _guard(words, marker):\n"
        "    index = _first_slot()\n"
        "    if any(word.parts[index].text == marker for word in words):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.slot", "nope"))\n'
    )
    edited = source.replace("_first_slot()", "_always_safe_slot()")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.slot") != _fingerprint_for(
        edited, "shell_taint.py", "taint.demo.slot"
    )


def test_an_unrelated_alias_on_a_read_import_does_not_move_a_fingerprint() -> None:
    # The guard reads one name off a shared import line. Hashing the whole line would churn the
    # record of every guard reading any other name on it, forcing the mass regeneration this gate
    # exists to prevent.
    source = (
        "from constants import MARKER, OTHER\n"
        "def _guard(word):\n"
        "    if word == MARKER:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.import-shape", "nope"))\n'
    )
    widened = source.replace("MARKER, OTHER", "MARKER, OTHER, EXTRA")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.import-shape"
    ) == _fingerprint_for(widened, "shell_taint.py", "taint.demo.import-shape")


def test_the_source_of_a_read_import_moves_a_fingerprint() -> None:
    # The control for the rule above: what the guard actually reads is still recorded, so
    # resolving the same spelling from another module moves the record.
    source = (
        "from constants import MARKER, OTHER\n"
        "def _guard(word):\n"
        "    if word == MARKER:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.import-source", "nope"))\n'
    )
    rerouted = source.replace("from constants import", "from decoys import")
    renamed = source.replace("MARKER, OTHER", "MARKER as OTHER, OTHER as MARKER")

    original = _fingerprint_for(source, "shell_taint.py", "taint.demo.import-source")
    assert original != _fingerprint_for(rerouted, "shell_taint.py", "taint.demo.import-source")
    assert original != _fingerprint_for(renamed, "shell_taint.py", "taint.demo.import-source")


def test_a_local_cap_scaled_by_a_runtime_value_is_a_threshold() -> None:
    # `512 * factor` fixes a 512-fold resource bound however `factor` is derived. Requiring the
    # whole binding to be constant let that cap ship with no recorded provenance.
    source = (
        "def _guard(items, factor):\n"
        "    cap = 512 * factor\n"
        "    if len(items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.scaled", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold cap" in violation for violation in violations)


def test_a_threshold_reached_through_an_accessor_is_still_a_threshold() -> None:
    # A callee is machinery, but a name already resolved to a magnitude stays one wherever it is
    # spelled: excluding the whole `call.func` subtree let a bound ship behind an accessor.
    source = (
        "def _guard(items, cap=4096):\n"
        "    if len(items) > cap.__index__():\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.accessor", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold cap" in violation for violation in violations)


def test_a_limits_attribute_on_an_unrelated_object_does_not_suppress_a_threshold() -> None:
    # `parsed` is any object that happens to carry a `limits` attribute, not the scan's limits, so
    # it is no evidence that the opposite operand is measured data.
    source = (
        "from constants import MAX_ITEMS\n"
        "def _guard(items, parsed):\n"
        "    if parsed.limits.max_depth > MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.fake-limits", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold MAX_ITEMS" in violation for violation in violations)


def test_limits_carried_under_another_spelling_is_still_a_limits_field() -> None:
    # The other direction: the scan's limits reached through a differently named binding is
    # approved evidence, so a legitimate guard is not rejected until its field is renamed.
    source = (
        "from constants import MAX_ITEMS\n"
        "def _guard(items, limits):\n"
        "    budget = limits\n"
        "    if budget.max_depth > MAX_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.aliased-limits", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_an_imported_value_compared_for_membership_is_not_a_threshold() -> None:
    # Equality and membership ask which value something is, not how much of it there is. An
    # imported frozenset or sentinel is not a magnitude, and inventorying one as a fixed semantic
    # bound would be a fiction.
    membership = (
        "from constants import ALLOWED_KINDS\n"
        "def _guard(node):\n"
        "    if node.kind not in ALLOWED_KINDS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.membership", "nope"))\n'
    )
    equality = (
        "from constants import SENTINEL\n"
        "def _guard(node):\n"
        "    if node.kind == SENTINEL:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.equality", "nope"))\n'
    )

    assert checker.find_threshold_violations(membership, "shell_taint.py") == ()
    assert checker.find_threshold_violations(equality, "shell_taint.py") == ()


def test_an_imported_value_compared_for_ordering_is_still_a_threshold() -> None:
    # The control for the rule above: an ordering comparison can bound a resource, so the same
    # imported value is still caught there.
    source = (
        "from constants import CEILING\n"
        "def _guard(items):\n"
        "    if len(items) > CEILING:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.ordering", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold CEILING" in violation for violation in violations)


def test_a_comparison_a_writer_does_not_forward_is_not_an_imported_threshold() -> None:
    # A writer is in the closure because the guard reads what it binds, not because every
    # comparison it spells decides the guard. Scoring those reported an import that never reaches
    # the condition.
    source = (
        "from constants import CEILING, FLOOR\n"
        "def _guard(items, limits):\n"
        "    state = {}\n"
        "    state[CEILING > FLOOR] = _measure(items)\n"
        "    if state and _measure(items) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.unforwarded", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_fingerprint_tracks_the_default_bound_to_a_parameter_the_guard_reads() -> None:
    # The signature is not in the body the local fixpoint walks, so a default that feeds the
    # guard's accumulator was invisible. Charging zero per call withdraws the guard from every
    # caller that omits the argument, and the record did not move.
    source = (
        "class _Budget:\n"
        "    def charge(self, amount: int = 1) -> None:\n"
        "        self.work += amount\n"
        "        if self.work > self.limits.max_work:\n"
        '            raise _TaintLimitExceeded(GuardRefusal("taint.demo.charge", "nope"))\n'
    )
    withdrawn = source.replace("amount: int = 1", "amount: int = 0")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.charge") != _fingerprint_for(
        withdrawn, "shell_taint.py", "taint.demo.charge"
    )


def test_fingerprint_tracks_a_keyword_only_default_the_guard_condition_reads() -> None:
    # The same hole is reachable through a keyword-only parameter, where flipping the default
    # switches the guard off for every caller that does not pass it.
    source = (
        "def _guard(depth, limits, *, enabled: bool = True):\n"
        "    if enabled and depth > limits.max_depth:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.enabled", "nope"))\n'
    )
    withdrawn = source.replace("enabled: bool = True", "enabled: bool = False")

    assert _fingerprint_for(source, "shell_taint.py", "taint.demo.enabled") != _fingerprint_for(
        withdrawn, "shell_taint.py", "taint.demo.enabled"
    )


def test_fingerprint_tracks_the_module_binding_a_read_parameter_default_resolves() -> None:
    # A default is evaluated in the defining scope, so the module constant behind it is part of
    # the closure and editing that constant moves the record.
    source = (
        "_CHARGE = 1\n"
        "def _guard(limits, amount: int = _CHARGE):\n"
        "    work = amount\n"
        "    if work > limits.max_work:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.module-default", "nope"))\n'
    )
    withdrawn = source.replace("_CHARGE = 1", "_CHARGE = 0")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.module-default"
    ) != _fingerprint_for(withdrawn, "shell_taint.py", "taint.demo.module-default")


def test_a_parameter_default_the_guard_does_not_read_leaves_the_fingerprint_alone() -> None:
    # The control for the rule above. Recording every default in the signature would churn a
    # frozen record on an edit that decides nothing about the guard.
    source = (
        "def _guard(depth, limits, unrelated: int = 1):\n"
        "    if depth > limits.max_depth:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.unread-default", "nope"))\n'
    )
    edited = source.replace("unrelated: int = 1", "unrelated: int = 99")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.unread-default"
    ) == _fingerprint_for(edited, "shell_taint.py", "taint.demo.unread-default")


def test_a_local_binding_does_not_shadow_the_module_name_a_default_resolves() -> None:
    # A default is evaluated where the function is defined, so a same-named local inside the body
    # does not shadow it and the module binding stays in the closure.
    source = (
        "_CHARGE = 1\n"
        "def _guard(limits, amount: int = _CHARGE):\n"
        "    _CHARGE = _unrelated()\n"
        "    work = amount + _CHARGE\n"
        "    if work > limits.max_work:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.shadowed-default", "nope"))\n'
    )
    withdrawn = source.replace("_CHARGE = 1", "_CHARGE = 0")

    assert _fingerprint_for(
        source, "shell_taint.py", "taint.demo.shadowed-default"
    ) != _fingerprint_for(withdrawn, "shell_taint.py", "taint.demo.shadowed-default")


def test_compare_against_base_rejects_a_frozen_guard_whose_parameter_default_changed(
    tmp_path: Path,
) -> None:
    # The end-to-end form of the reported hole: the base-owned comparison must reject a candidate
    # that weakens a frozen guard's default while claiming the base's record.
    root = _fake_root(tmp_path, [{"origin_id": "taint.eval-discovery.work-limit"}])
    module = root / "src/doc_lattice/github_ci/shell_taint.py"
    source = module.read_text(encoding="utf-8")
    assert source.count("def charge_work(self, amount: int = 1) -> None:") == 1
    base = json.dumps(
        {
            "schema": checker.SCHEMA_VERSION,
            "records": [
                record.as_json()
                for record in checker.repository_origin_records(root)
                if record.origin_id == "taint.eval-discovery.work-limit"
            ],
        }
    )
    module.write_text(
        source.replace(
            "def charge_work(self, amount: int = 1) -> None:",
            "def charge_work(self, amount: int = 0) -> None:",
        ),
        encoding="utf-8",
    )

    failures = checker.compare_against_base(root, base)

    assert any("taint.eval-discovery.work-limit" in failure for failure in failures)


def test_a_threshold_literal_compared_through_a_call_is_rejected() -> None:
    # `operator.gt(count, 100)` bounds a resource exactly as `count > 100` does. Reading only
    # comparison syntax left the literal reachable from no `ast.Compare` node at all.
    source = (
        "import operator\n"
        "def _guard(items):\n"
        "    if operator.gt(_measure(items), 100):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.call-literal", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold literal 100" in violation for violation in violations)


def test_a_threshold_literal_compared_through_a_dunder_call_is_rejected() -> None:
    # The receiver form of the same spelling, where the left operand is the attribute's own base.
    source = (
        "def _guard(items):\n"
        "    if _measure(items).__gt__(100):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.dunder-literal", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold literal 100" in violation for violation in violations)


def test_an_imported_threshold_compared_through_a_call_is_rejected() -> None:
    # The imported-bound rule reads the same comparisons, so a call spelling must not hide the
    # provenance of an imported cap either.
    source = (
        "from operator import gt\n"
        "from constants import CEILING\n"
        "def _guard(items):\n"
        "    if gt(_measure(items), CEILING):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.call-import", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold CEILING" in violation for violation in violations)


def test_a_limits_field_compared_through_a_call_is_still_approved_provenance() -> None:
    # The control: recognizing the call form must not turn a properly sourced bound into a
    # violation, whichever operand the limits field is.
    source = (
        "import operator\n"
        "def _guard(items, limits):\n"
        "    if operator.gt(_measure(items), limits.max_items):\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.call-limits", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_a_call_that_is_not_a_comparison_contributes_no_threshold() -> None:
    # Arity and keyword arguments decide whether a call spells a comparison. A same-named call
    # that does not must not manufacture operands out of unrelated arguments.
    source = (
        "def _guard(items, limits):\n"
        "    if gt(_measure(items), 100, strict=True) and _measure(items) > limits.max_items:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.not-a-comparison", "nope"))\n'
    )

    assert checker.find_threshold_violations(source, "shell_taint.py") == ()


def test_an_imported_threshold_forwarded_by_a_parameter_default_is_rejected() -> None:
    # A default forwards a value into the guard exactly as an assignment writer does, and no
    # statement in the scope writes it, so the import was invisible to the provenance rule.
    source = (
        "from constants import CEILING\n"
        "def _guard(items, cap=CEILING):\n"
        "    if _measure(items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.default-import", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("guard threshold CEILING" in violation for violation in violations)
