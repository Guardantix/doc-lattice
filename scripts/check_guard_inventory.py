#!/usr/bin/env python3
"""Extract and gate the CI shell scanner's fail-closed guard origins.

Every fail-closed guard in `shell_taint.py` and `shell_scanner.py` has one *origin*: the site
that detects the condition and constructs a `GuardRefusal`. This module reads those two modules
as inert source text, never importing them, and produces one canonical record per origin.

It enforces three separable properties:

1. **Canonical refusal shapes.** A refusal may only reach an exception, a `ShellScanResult`, or a
   verdict return as a `GuardRefusal` construction with a literal identifier, or as one of the
   explicitly declared transports. Raw text, interpolated text, and `(refused, reason)` tuple
   returns are rejected, so a future change cannot reintroduce a text-only refusal.
2. **Tree-local closure.** Source origin identifiers must partition exactly into the classified
   inventory and the frozen rollout debt set, with the two disjoint.
3. **Debt monotonicity.** Compared against a base revision, the candidate's debt records must be
   a subset of the base's. Freezing *records* rather than bare identifiers means an unclassified
   guard cannot be moved or semantically edited while keeping its debt entry.

Because the monotonicity check runs from the base revision's copy of this script against the
candidate's source, the candidate is only ever parsed as data.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
"""Version of the canonical origin-record shape. Bump when the record fields or the fingerprint
derivation change, so a base-relative comparison never silently compares incompatible records."""

GUARDED_MODULES = (
    "src/doc_lattice/github_ci/shell_taint.py",
    "src/doc_lattice/github_ci/shell_scanner.py",
)

DEBT_PATH = "tests/fixtures/shell_guard_debt.json"

REFUSAL_CONSTRUCTOR = "GuardRefusal"
REFUSAL_EXCEPTIONS = frozenset(
    {"_TaintLimitExceeded", "_MalformedTaintEvidence", "_ShellScanIncomplete"}
)
GUARD_FREE_VERDICTS = frozenset({"Certified", "MarkerDetected"})
RESULT_CONSTRUCTOR = "ShellScanResult"
VERDICT_FUNCTIONS = frozenset({"analyze_marker_taint", "scan_doc_lattice_invocations"})

DECLARED_TRANSPORTS = frozenset(
    {
        # The parameterized cycle detector refuses on behalf of whichever caller supplied the
        # origin refusal, so the callers are the origins and this site only propagates.
        ("shell_taint.py", "_validate_acyclic_graph", "refusal"),
        # The taint verdict crosses into the scanner already discriminated.
        ("shell_scanner.py", "_ShellScanner.scan", "verdict"),
        # Brace-limit refusals are deferred on the word and re-raised once the word's role is
        # known; the origin is the brace expander inside ContentBuilder.build.
        ("shell_scanner.py", "_ShellScanner._flush_command", "word.brace_expansion_error"),
        # Word assembly re-wraps a taint-layer bound as a scanner-layer stop.
        ("shell_scanner.py", "_ShellScanner._parse_word.finish", "error.refusal"),
        # The public boundary projects an already-constructed refusal onto the result.
        ("shell_scanner.py", "scan_doc_lattice_invocations", "error.refusal"),
    }
)
"""Sites that only propagate an existing refusal. Transports never mint identifiers, so they are
not guard origins and never appear in the inventory."""

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True, slots=True)
class OriginRecord:
    """One canonical, line-number-free identity for a fail-closed guard origin.

    Attributes:
        origin_id: The literal identifier the origin constructs.
        path: Repository-relative module the origin lives in.
        qualname: Enclosing qualified name, so moving a guard changes its record.
        fingerprint: Digest over the qualname, the guarding condition, and the origin statement
            shape with operator-facing reason text normalized away.
    """

    origin_id: str
    path: str
    qualname: str
    fingerprint: str

    def as_json(self) -> dict[str, str]:
        """Return the record as an order-stable JSON object."""
        return {
            "origin_id": self.origin_id,
            "path": self.path,
            "qualname": self.qualname,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_json(cls, payload: dict[str, str]) -> OriginRecord:
        """Rebuild a record from its JSON object."""
        return cls(
            payload["origin_id"],
            payload["path"],
            payload["qualname"],
            payload["fingerprint"],
        )


def _annotate(tree: ast.AST) -> tuple[dict[int, str], dict[int, str]]:
    """Map every node to its enclosing qualified name and nearest guarding condition."""
    names: dict[int, str] = {}
    conditions: dict[int, str] = {}

    def descend(node: ast.AST, prefix: str, condition: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner, inner_condition = prefix, condition
            if isinstance(child, _SCOPES):
                inner = f"{prefix}.{child.name}" if prefix else child.name
                inner_condition = ""
            names[id(child)] = inner
            conditions[id(child)] = inner_condition
            if isinstance(child, ast.If):
                test = " ".join(ast.unparse(child.test).split())
                for statement in child.body:
                    record(statement, inner, test)
                for statement in child.orelse:
                    record(statement, inner, f"not ({test})")
                continue
            if isinstance(child, ast.ExceptHandler):
                handled = ast.unparse(child.type) if child.type is not None else "BaseException"
                descend(child, inner, f"except {handled}")
                continue
            descend(child, inner, inner_condition)

    def record(node: ast.AST, prefix: str, condition: str) -> None:
        names[id(node)] = prefix
        conditions[id(node)] = condition
        descend(node, prefix, condition)

    descend(tree, "", "")
    return names, conditions


def _normalized_shape(statement: ast.stmt) -> str:
    """Return the statement's structure with operator-facing reason text removed.

    Rewording a refusal's message is not a semantic change to the guard, so the reason argument is
    dropped before hashing. Everything else about the origin statement is retained.
    """
    clone = copy.deepcopy(statement)
    for node in ast.walk(clone):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == REFUSAL_CONSTRUCTOR
        ):
            node.args = node.args[:1]
            node.keywords = []
    return ast.dump(clone, include_attributes=False)


def _group_digest(digest: str) -> str:
    """Return a truncated digest in hyphen-separated groups.

    Grouping keeps the fingerprint readable in review and keeps the snapshot free of long
    undifferentiated hex runs, which repository secret scanning reports as high-entropy strings.
    """
    return "-".join(digest[index : index + 4] for index in range(0, 24, 4))


def _own_expressions(statement: ast.stmt) -> list[ast.AST]:
    """Return the statement's own expression nodes, excluding any nested statement's."""
    owned: list[ast.AST] = []
    pending: list[ast.AST] = [
        child for child in ast.iter_child_nodes(statement) if not isinstance(child, ast.stmt)
    ]
    while pending:
        node = pending.pop()
        owned.append(node)
        pending.extend(
            child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.stmt)
        )
    return owned


def _is_guard_refusal_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == REFUSAL_CONSTRUCTOR
    )


def _origin_calls_by_statement(tree: ast.AST) -> list[tuple[ast.stmt, ast.Call]]:
    """Pair every guard-origin construction with the statement that owns it."""
    pairs: list[tuple[ast.stmt, ast.Call]] = []
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt):
            continue
        pairs.extend(
            (statement, node)
            for node in _own_expressions(statement)
            if _is_guard_refusal_call(node)
            if isinstance(node, ast.Call)
        )
    return pairs


def _guard_refusal_calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if _is_guard_refusal_call(node)]  # ty: ignore[invalid-return-type]


def extract_origin_records(source: str, path: str) -> tuple[OriginRecord, ...]:
    """Return one canonical record per guard origin in this module source.

    Args:
        source: Module source text, parsed but never executed.
        path: Name recorded on each produced record.

    Returns:
        Records ordered by identifier.

    Raises:
        ValueError: If an origin constructs a non-literal identifier.
    """
    tree = ast.parse(source)
    names, conditions = _annotate(tree)
    records: list[OriginRecord] = []
    for statement, call in _origin_calls_by_statement(tree):
        if not call.args or not isinstance(call.args[0], ast.Constant):
            raise ValueError(f"{path}: guard identifier must be a string literal")
        origin_id = call.args[0].value
        if not isinstance(origin_id, str):
            raise ValueError(f"{path}: guard identifier must be a string literal")
        qualname = names.get(id(statement), "")
        condition = conditions.get(id(statement), "")
        digest = hashlib.sha256(
            "\n".join((qualname, condition, _normalized_shape(statement))).encode("utf-8")
        ).hexdigest()
        records.append(OriginRecord(origin_id, path, qualname, _group_digest(digest)))
    return tuple(sorted(records, key=lambda record: (record.origin_id, record.fingerprint)))


def _is_refusal_shape(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {REFUSAL_CONSTRUCTOR, *GUARD_FREE_VERDICTS}
    )


def _is_raw_text(node: ast.expr) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _literal_id_violations(tree: ast.AST, path: str) -> list[str]:
    """Return a violation for every origin whose identifier is not a string literal."""
    return [
        f"{path}: {REFUSAL_CONSTRUCTOR} identifier must be a string literal"
        for call in _guard_refusal_calls(tree)
        if not call.args
        or not isinstance(call.args[0], ast.Constant)
        or not isinstance(call.args[0].value, str)
    ]


def _refusal_carrier_violations(tree: ast.AST, names: dict[int, str], path: str) -> list[str]:
    """Return a violation for every refusal carried as text or as an undeclared transport."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id in REFUSAL_EXCEPTIONS:
            candidates = list(node.args)
        elif node.func.id == RESULT_CONSTRUCTOR:
            candidates = list(node.args[1:])
        else:
            continue
        qualname = names.get(id(node), "")
        for argument in candidates:
            if _is_refusal_shape(argument):
                continue
            if _is_raw_text(argument):
                violations.append(
                    f"{path}:{qualname}: {node.func.id} carries raw refusal text; "
                    f"construct a {REFUSAL_CONSTRUCTOR} at the guard origin"
                )
                continue
            expression = ast.unparse(argument)
            if (path, qualname, expression) not in DECLARED_TRANSPORTS:
                violations.append(
                    f"{path}:{qualname}: {node.func.id} carries undeclared transport "
                    f"{expression!r}; declare it or construct a {REFUSAL_CONSTRUCTOR} here"
                )
    return violations


def _verdict_return_violations(tree: ast.AST, path: str) -> list[str]:
    """Return a violation for every verdict function that still returns a text-only refusal."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, _SCOPES) or node.name not in VERDICT_FUNCTIONS:
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Return) or statement.value is None:
                continue
            if isinstance(statement.value, ast.Tuple):
                violations.append(
                    f"{path}:{node.name} returns a tuple verdict; return a discriminated "
                    f"verdict so guard identity survives"
                )
            elif _is_raw_text(statement.value):
                violations.append(
                    f"{path}:{node.name} returns raw refusal text; return a discriminated verdict"
                )
    return violations


def find_shape_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every non-canonical refusal shape in this module source.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used to resolve declared transports and in messages.

    Returns:
        Human-readable violations, empty when every refusal shape is canonical.
    """
    tree = ast.parse(source)
    names, _ = _annotate(tree)
    return (
        *_literal_id_violations(tree, path),
        *_refusal_carrier_violations(tree, names, path),
        *_verdict_return_violations(tree, path),
    )


LIMITS_CONSTRUCTORS = frozenset({"TaintLimits", "ScannerLimits", "ScanLimits"})

LIMITS_BOUNDARIES = frozenset(
    {
        # The taint entry point keeps a default so a direct evidence-level analysis stays usable.
        ("shell_taint.py", "analyze_marker_taint"),
        # The public scan boundary constructs the one scan-level limits value.
        ("shell_scanner.py", "scan_doc_lattice_invocations"),
        # The budget derives its counters from the limits it owns.
        ("shell_scanner.py", "_ScanBudget"),
    }
)
"""The only places allowed to construct default limits. Everywhere else must be handed the scan's
limits, so no layer can silently fall back to production caps under a shrunk budget."""

FIXED_SEMANTIC_BOUNDS = {
    "_MAX_BRACE_INTEGER_DIGITS": (
        "A brace-range integer wider than this is directly authorable, so the guard needs no "
        "shrink witness; the bound is a spelling limit on the literal, not a resource budget."
    ),
    "_MAX_SHELL_DESCRIPTOR_DIGITS": (
        "A file descriptor number is a semantic quantity. A descriptor spelled with more digits "
        "than this is directly authorable and already far beyond any real descriptor."
    ),
    "_MAX_TRACKED_LITERAL_ALTERNATIVES": (
        "The number of IFS field alternatives a single read projection tracks is a modeling "
        "arity, reachable by authoring that many separators in one literal."
    ),
    "_UNICODE_MAX": "The last Unicode code point. Not a budget; authorable as an escape.",
    "_SURROGATE_MIN": "The first surrogate code point. Not a budget; authorable as an escape.",
    "_SURROGATE_MAX": "The last surrogate code point. Not a budget; authorable as an escape.",
    "_EVAL_UNICODE_MAX": "The last Unicode code point, as the eval reparser spells it.",
    "_EVAL_SURROGATE_MIN": "The first surrogate code point, as the eval reparser spells it.",
    "_EVAL_SURROGATE_MAX": "The last surrogate code point, as the eval reparser spells it.",
}
"""Guard thresholds that are fixed semantic bounds rather than resource budgets. Each is directly
authorable, so its guard is witnessed without shrinking anything."""


def find_limits_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every limits construction or optional limits parameter away from a boundary.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used to resolve boundaries and in messages.

    Returns:
        Human-readable violations, empty when limits flow from one scan-level value.
    """
    tree = ast.parse(source)
    names, _ = _annotate(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in LIMITS_CONSTRUCTORS or node.args or node.keywords:
            continue
        qualname = names.get(id(node), "")
        if (path, qualname.split(".")[0] if qualname else "") in LIMITS_BOUNDARIES:
            continue
        if (path, qualname) in LIMITS_BOUNDARIES:
            continue
        violations.append(
            f"{path}:{qualname or '<module>'} constructs default limits; accept the scan's "
            f"limits instead so a shrunk cap reaches this guard"
        )

    for node in ast.walk(tree):
        if not isinstance(node, _SCOPES) or isinstance(node, ast.ClassDef):
            continue
        qualname = names.get(id(node), node.name)
        if (path, qualname) in LIMITS_BOUNDARIES:
            continue
        arguments = node.args
        positional = arguments.posonlyargs + arguments.args
        offset = len(positional) - len(arguments.defaults)
        defaulted = list(zip(positional[offset:], arguments.defaults, strict=True))
        defaulted += [
            (argument, default)
            for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
            if default is not None
        ]
        for argument, default in defaulted:
            if argument.arg == "limits" and default is not None:
                violations.append(
                    f"{path}:{qualname} must require limits; an optional limits parameter lets a "
                    f"caller silently restore production caps"
                )

    return tuple(violations)


def _threshold_names(statement: ast.stmt, condition: str) -> set[str]:
    referenced = {
        node.id
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and node.id.startswith("_MAX_")
    }
    referenced |= {
        token
        for token in condition.replace("(", " ").replace(")", " ").split()
        if token.startswith("_MAX_") or token.startswith("_EVAL_") or token.startswith("_SURROGATE")
    }
    return {name.strip(".,:") for name in referenced}


def find_threshold_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every guard threshold that is neither a limits field nor an inventoried bound.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used in messages.

    Returns:
        Human-readable violations, empty when every guard threshold has declared provenance.
    """
    tree = ast.parse(source)
    _, conditions = _annotate(tree)
    violations: list[str] = []
    for statement, _call in _origin_calls_by_statement(tree):
        condition = conditions.get(id(statement), "")
        for name in sorted(_threshold_names(statement, condition)):
            if name in FIXED_SEMANTIC_BOUNDS:
                continue
            violations.append(
                f"{path}: guard threshold {name} is neither a scan-limits field nor an "
                f"inventoried fixed semantic bound"
            )
    return tuple(violations)


def repository_limits_violations(root: Path) -> tuple[str, ...]:
    """Return every limits-provenance violation across the repository's guarded modules.

    Args:
        root: Repository root to read the guarded modules from.

    Returns:
        Human-readable violations, empty when limits flow from one scan-level value.
    """
    violations: list[str] = []
    for module in GUARDED_MODULES:
        source = (root / module).read_text(encoding="utf-8")
        violations.extend(find_limits_violations(source, Path(module).name))
    return tuple(violations)


def repository_threshold_violations(root: Path) -> tuple[str, ...]:
    """Return every guard threshold without declared provenance across the guarded modules.

    Args:
        root: Repository root to read the guarded modules from.

    Returns:
        Human-readable violations, empty when every guard threshold has declared provenance.
    """
    violations: list[str] = []
    for module in GUARDED_MODULES:
        source = (root / module).read_text(encoding="utf-8")
        violations.extend(find_threshold_violations(source, Path(module).name))
    return tuple(violations)


def repository_origin_records(root: Path) -> tuple[OriginRecord, ...]:
    """Return every guard origin record in the repository's guarded modules.

    Args:
        root: Repository root to read the guarded modules from.

    Returns:
        Records ordered by identifier across all guarded modules.
    """
    records: list[OriginRecord] = []
    for module in GUARDED_MODULES:
        source = (root / module).read_text(encoding="utf-8")
        records.extend(extract_origin_records(source, Path(module).name))
    return tuple(sorted(records, key=lambda record: (record.origin_id, record.fingerprint)))


def repository_shape_violations(root: Path) -> tuple[str, ...]:
    """Return every non-canonical refusal shape across the repository's guarded modules.

    Args:
        root: Repository root to read the guarded modules from.

    Returns:
        Human-readable violations, empty when every refusal shape is canonical.
    """
    violations: list[str] = []
    for module in GUARDED_MODULES:
        source = (root / module).read_text(encoding="utf-8")
        violations.extend(find_shape_violations(source, Path(module).name))
    return tuple(violations)


def load_debt_records(root: Path) -> tuple[OriginRecord, ...]:
    """Return the frozen rollout debt records recorded in this tree.

    Args:
        root: Repository root holding the debt snapshot.

    Returns:
        Debt records, empty when the rollout has closed.

    Raises:
        ValueError: If the snapshot was written under a different record schema.
    """
    return _decode_records(json.loads((root / DEBT_PATH).read_text(encoding="utf-8")), DEBT_PATH)


def _decode_records(payload: dict[str, object], origin: str) -> tuple[OriginRecord, ...]:
    schema = payload.get("schema")
    if schema != SCHEMA_VERSION:
        raise ValueError(f"{origin}: record schema {schema!r} is not {SCHEMA_VERSION}")
    rows = payload.get("records", [])
    if not isinstance(rows, list):
        raise ValueError(f"{origin}: records must be a list")
    decoded: list[OriginRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{origin}: each record must be an object")
        decoded.append(OriginRecord.from_json({str(k): str(v) for k, v in row.items()}))
    return tuple(decoded)


def emit_records(root: Path) -> str:
    """Return the candidate tree's debt snapshot as schema-versioned JSON.

    Args:
        root: Repository root to read from.

    Returns:
        JSON text with a trailing newline.
    """
    records = load_debt_records(root)
    payload = {"schema": SCHEMA_VERSION, "records": [record.as_json() for record in records]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def compare_against_base(root: Path, base_snapshot: str) -> tuple[str, ...]:
    """Return every way the candidate's debt grew relative to a base snapshot.

    Args:
        root: Repository root holding the candidate tree.
        base_snapshot: JSON text emitted by the base revision's snapshot.

    Returns:
        Human-readable failures, empty when debt only shrank or held steady.
    """
    base = set(_decode_records(json.loads(base_snapshot), "base snapshot"))
    head = set(load_debt_records(root))
    source = set(repository_origin_records(root))
    failures = [
        f"{record.origin_id} ({record.path}:{record.qualname}) is new fail-closed guard debt; "
        f"classify it as reachable or invariant instead of freezing it"
        for record in sorted(head - base, key=lambda record: record.origin_id)
    ]
    # The candidate's own closure run is not trusted here: this checker is the base revision's
    # copy, so it re-derives the candidate's origins and rejects any debt record that does not
    # describe one of them.
    failures.extend(
        f"{record.origin_id} ({record.path}:{record.qualname}) is frozen as debt but is not a "
        f"guard origin in the candidate source; the record was laundered onto a guard that "
        f"moved or no longer exists"
        for record in sorted(head - source, key=lambda record: record.origin_id)
    )
    return tuple(failures)


def main(argv: list[str] | None = None) -> int:
    """Run the requested guard-inventory gate.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--emit-debt", action="store_true", help="write the debt snapshot JSON")
    parser.add_argument("--compare-base", type=Path, help="base debt snapshot to compare against")
    arguments = parser.parse_args(argv)

    if arguments.emit_debt:
        sys.stdout.write(emit_records(arguments.root))
        return 0

    failures = list(repository_shape_violations(arguments.root))
    if arguments.compare_base is not None:
        failures.extend(
            compare_against_base(
                arguments.root,
                arguments.compare_base.read_text(encoding="utf-8"),
            )
        )
    for failure in failures:
        sys.stderr.write(f"{failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
