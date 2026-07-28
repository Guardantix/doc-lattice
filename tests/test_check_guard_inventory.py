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


def test_non_literal_guard_id_is_rejected() -> None:
    source = 'def f(name):\n    raise _TaintLimitExceeded(GuardRefusal(name, "reason"))\n'

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("literal" in violation for violation in violations)


def test_tuple_verdict_return_is_rejected() -> None:
    source = 'def analyze_marker_taint(e):\n    return True, "shell taint edge limit exceeded"\n'

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("analyze_marker_taint" in violation for violation in violations)


def test_text_only_scan_result_is_rejected() -> None:
    source = (
        "def scan_doc_lattice_invocations(script):\n"
        '    return ShellScanResult((), "source character limit exceeded")\n'
    )

    violations = checker.find_shape_violations(source, "shell_scanner.py")

    assert any("raw refusal text" in violation for violation in violations)


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


def test_compare_against_base_rejects_a_foreign_record_schema(tmp_path: Path) -> None:
    base = json.dumps({"schema": checker.SCHEMA_VERSION + 1, "records": []})
    root = _fake_root(tmp_path, [])

    with pytest.raises(ValueError, match="record schema"):
        checker.compare_against_base(root, base)


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


def test_threshold_provenance_sees_a_nested_guard_condition() -> None:
    source = (
        "def _guard(a, b):\n"
        "    if a:\n"
        "        if b > _MAX_UNDECLARED_THING:\n"
        '            raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("_MAX_UNDECLARED_THING" in violation for violation in violations)


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


def test_classified_ids_never_execute_the_candidate_registry(tmp_path: Path) -> None:
    # The checker runs from the protected base against a candidate tree, so the candidate's
    # registry must be parsed rather than imported.
    root = _fake_root(tmp_path, [])
    (root / checker.REGISTRY_PATH).write_text(
        'raise SystemExit("importing the candidate registry would run this")\n'
        'X = (ReachableWitness("taint.demo.parsed", "script"),)\n',
        encoding="utf-8",
    )

    assert checker.classified_origin_ids(root) == frozenset({"taint.demo.parsed"})


def test_closure_holds_for_the_shipped_tree() -> None:
    assert checker.repository_closure_violations(_ROOT) == ()


def test_closure_rejects_a_guard_that_is_neither_classified_nor_frozen(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])
    (root / checker.REGISTRY_PATH).write_text(
        'W = (ReachableWitness("scanner.source.character-limit", "x"),)\n',
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


def test_emit_debt_derives_the_unclassified_records(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, [])

    payload = json.loads(checker.emit_records(root))
    derived = {record["origin_id"] for record in payload["records"]}

    assert payload["schema"] == checker.SCHEMA_VERSION
    assert derived == {
        record.origin_id for record in checker.repository_origin_records(root)
    } - checker.classified_origin_ids(root)
