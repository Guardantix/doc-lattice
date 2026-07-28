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


def test_threshold_provenance_sees_a_nested_guard_condition() -> None:
    source = (
        "def _guard(a, b):\n"
        "    if a:\n"
        "        if b > _MAX_UNDECLARED_THING:\n"
        '            raise _TaintLimitExceeded(GuardRefusal("taint.demo.x", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("_MAX_UNDECLARED_THING" in violation for violation in violations)


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


def test_control_flow_closure_ignores_computation_that_diverts_nothing() -> None:
    # Reducing a branch to its test is what keeps an ordinary edit inside a long function from
    # churning a frozen record that only the surrounding control flow describes.
    edited = _LOOP_EXHAUSTION_SOURCE.replace("index += 1", "index += 2")

    original = checker.extract_origin_records(_LOOP_EXHAUSTION_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_control_flow_closure_ignores_another_function() -> None:
    extended = _FALL_THROUGH_SOURCE + "\n\ndef _elsewhere(x):\n    if x:\n        return 1\n"

    original = checker.extract_origin_records(_FALL_THROUGH_SOURCE, "shell_taint.py")
    updated = checker.extract_origin_records(extended, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


def test_control_flow_closure_leaves_a_guarded_origin_alone() -> None:
    # A guard with a test already records that test and the writes feeding it. Widening the closure
    # to every guard would make each one churn on any control-flow edit in its function, and a debt
    # record that churns has to be regenerated, which is the laundering path the gate closes.
    source = (
        "def _guard(value, other):\n"
        "    if other:\n"
        "        return 1\n"
        "    if value > 3:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.over-three", "too big"))\n'
    )
    edited = source.replace(
        "    if other:\n        return 1\n", "    if not other:\n        return 2\n"
    )

    original = checker.extract_origin_records(source, "shell_taint.py")
    updated = checker.extract_origin_records(edited, "shell_taint.py")

    assert original[0].fingerprint == updated[0].fingerprint


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
