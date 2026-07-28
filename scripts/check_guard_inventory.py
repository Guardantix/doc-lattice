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
3. **Debt monotonicity.** Compared against a base revision, every guard the candidate freezes must
   re-derive to a record the base already carried. Freezing *records* rather than bare identifiers
   means an unclassified guard cannot be moved or semantically edited while keeping its debt entry.

Because the monotonicity check runs from the base revision's copy of this script against the
candidate's source, the candidate is only ever parsed as data. `--compare-base` therefore runs the
closure and monotonicity checks alone: those derive everything from the candidate tree, while the
refusal-shape, limits and threshold rules read allowlists that describe the source they shipped
with. The candidate's own copy enforces those three, without `--compare-base`, in the test suite.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3
"""Version of the canonical origin-record shape. Bump when the record fields or the fingerprint
derivation change, so a base-relative comparison never silently compares incompatible records."""

IDENTITY_FIELD = "origin_id"
"""The one record field that is stable across record schemas, and the migration contract.

A base-relative comparison reads only this field out of the candidate's snapshot and re-derives
every other field from candidate source with its own extractor. A candidate may therefore migrate
`SCHEMA_VERSION`, the record fields, and the fingerprint derivation in one change without the base
revision's checker failing to decode the snapshot it is handed. Renaming this field is the one
migration the gate cannot absorb.
"""

GUARDED_MODULES = (
    "src/doc_lattice/github_ci/shell_taint.py",
    "src/doc_lattice/github_ci/shell_scanner.py",
)

DEBT_PATH = "tests/fixtures/shell_guard_debt.json"
RETIREMENT_PATH = "tests/fixtures/shell_guard_retirements.json"
REGISTRY_PATH = "tests/guard_witnesses.py"
CLASSIFICATION_REGISTRIES = {
    "REACHABLE_WITNESSES": (
        "ReachableWitness",
        ("origin_id", "script", "limits", "control_script", "control_guard_id"),
        2,
    ),
    "INVARIANT_WITNESSES": (
        "InvariantWitness",
        ("origin_id", "rationale", "boundary_script", "boundary_evidence", "boundary_guard_id"),
        4,
    ),
}

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

_MUTATING_METHODS = frozenset(
    {
        "add",
        "append",
        "appendleft",
        "clear",
        "difference_update",
        "discard",
        "extend",
        "extendleft",
        "insert",
        "intersection_update",
        "pop",
        "popitem",
        "popleft",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "symmetric_difference_update",
        "update",
    }
)
"""Method names that mutate their receiver in place. A guard that reads an accumulator is disabled
as effectively by removing the call that feeds it as by editing the condition, so such a call is a
write of its receiver for the purposes of the fingerprint's writer closure."""


def _called_name(node: ast.AST) -> str | None:
    """Return a call's final callee name, whether it is spelled bare or through a module.

    Every rule in this module recognizes a construction by name. Matching only `Name` callees would
    let the identical construction spelled `shell_guards.GuardRefusal(...)` escape all of them.

    Args:
        node: Any AST node.

    Returns:
        The callee's last name component, or `None` when the node is not a call or the callee is
        not a name or an attribute path.
    """
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


@dataclass(frozen=True, slots=True)
class OriginRecord:
    """One canonical, line-number-free identity for a fail-closed guard origin.

    Attributes:
        origin_id: The literal identifier the origin constructs.
        path: Repository-relative module the origin lives in.
        qualname: Enclosing qualified name, so moving a guard changes its record.
        fingerprint: Digest over the qualname, the guarding condition, the origin statement shape
            with operator-facing reason text normalized away, the shapes of the same-scope
            statements that write what the condition reads, the shape of any declared transport
            the origin statement hands its refusal to, and, for an origin with no guarding test,
            the same-scope control flow that decides whether it is reached.
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


@dataclass(frozen=True, slots=True)
class _Annotations:
    """Per-node context derived from one parse of a guarded module.

    Attributes:
        names: Enclosing qualified name, keyed by node identity.
        conditions: Nearest guarding condition as text, keyed by node identity.
        tests: The guarding `if`, `while` and `match`-case tests themselves, outermost first, keyed
            by node identity. Text conditions carry `except` clauses and `match` patterns too and so
            are not always parseable; these are.
        scopes: Nearest enclosing function, keyed by node identity, or `None` at class or module
            level.
    """

    names: dict[int, str]
    conditions: dict[int, str]
    tests: dict[int, tuple[ast.expr, ...]]
    scopes: dict[int, ast.AST | None]


def _annotate(tree: ast.AST) -> _Annotations:
    """Map every node to its enclosing scope, qualified name and nearest guarding condition."""
    names: dict[int, str] = {}
    conditions: dict[int, str] = {}
    tests: dict[int, tuple[ast.expr, ...]] = {}
    scopes: dict[int, ast.AST | None] = {}

    def conjoin(outer: str, test: str) -> str:
        return test if not outer else f"{outer} and {test}"

    def descend(
        node: ast.AST,
        prefix: str,
        condition: str,
        guards: tuple[ast.expr, ...],
        scope: ast.AST | None,
    ) -> None:
        for child in ast.iter_child_nodes(node):
            inner, inner_condition, inner_guards, inner_scope = prefix, condition, guards, scope
            if isinstance(child, _SCOPES):
                inner = f"{prefix}.{child.name}" if prefix else child.name
                inner_condition = ""
                inner_guards = ()
                inner_scope = None if isinstance(child, ast.ClassDef) else child
            record(child, inner, inner_condition, inner_guards, inner_scope)

    def record(
        node: ast.AST,
        prefix: str,
        condition: str,
        guards: tuple[ast.expr, ...],
        scope: ast.AST | None,
    ) -> None:
        names[id(node)] = prefix
        conditions[id(node)] = condition
        tests[id(node)] = guards
        scopes[id(node)] = scope
        # An `if` nested directly inside another `if` body, and every `elif`, arrives here rather
        # than through `descend`'s child loop. Handling it here is what keeps the innermost test
        # in the recorded condition instead of the enclosing one. A `while` test decides a refusal
        # exactly as an `if` test does, so it is treated the same way.
        if isinstance(node, ast.If | ast.While):
            test = " ".join(ast.unparse(node.test).split())
            record(node.test, prefix, condition, guards, scope)
            for statement in node.body:
                record(statement, prefix, conjoin(condition, test), (*guards, node.test), scope)
            for statement in node.orelse:
                negated = conjoin(condition, f"not ({test})")
                record(statement, prefix, negated, (*guards, node.test), scope)
            return
        if isinstance(node, ast.Match):
            subject = " ".join(ast.unparse(node.subject).split())
            record(node.subject, prefix, condition, guards, scope)
            for case in node.cases:
                pattern = " ".join(ast.unparse(case.pattern).split())
                arm = conjoin(condition, f"match {subject} case {pattern}")
                arm_guards = guards
                if case.guard is not None:
                    arm = conjoin(arm, " ".join(ast.unparse(case.guard).split()))
                    arm_guards = (*guards, case.guard)
                    record(case.guard, prefix, condition, guards, scope)
                for statement in case.body:
                    record(statement, prefix, arm, arm_guards, scope)
            return
        if isinstance(node, ast.ExceptHandler):
            handled = ast.unparse(node.type) if node.type is not None else "BaseException"
            descend(node, prefix, conjoin(condition, f"except {handled}"), guards, scope)
            return
        descend(node, prefix, condition, guards, scope)

    descend(tree, "", "", (), None)
    return _Annotations(names, conditions, tests, scopes)


def _normalized_shape(statement: ast.stmt) -> str:
    """Return the statement's structure with operator-facing reason text removed.

    Rewording a refusal's message is not a semantic change to the guard, so the reason argument is
    dropped before hashing. Everything else about the origin statement is retained.
    """
    clone = copy.deepcopy(statement)
    for node in ast.walk(clone):
        if _called_name(node) == REFUSAL_CONSTRUCTOR and isinstance(node, ast.Call):
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


def _read_spellings(nodes: tuple[ast.expr, ...]) -> frozenset[str]:
    """Return every name and dotted attribute spelling these expressions read."""
    return frozenset(
        ast.unparse(node)
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Name | ast.Attribute)
    )


def _statement_reads(statement: ast.stmt) -> frozenset[str]:
    """Return the spellings this statement loads, ignoring any nested statement's.

    Only `Load` occurrences count, so a binding target does not read itself and the closure that
    follows a value back to its source cannot be seeded by the name it is being stored into. The
    receiver of `state[key] = value` does load `state`, which is correct: that statement both reads
    and writes it.
    """
    return frozenset(
        ast.unparse(node)
        for node in _own_expressions(statement)
        if isinstance(node, ast.Name | ast.Attribute)
        if isinstance(node.ctx, ast.Load)
    )


def _mutated_spellings(statement: ast.stmt) -> set[str]:
    """Return the receivers this statement mutates in place, ignoring any nested statement's.

    `active.add(node)` and `effects.append(edge)` write what a guard's condition later reads, and
    neither binds a name, so a rule that recognized only binding statements would leave the
    accumulation that feeds a guard outside its record.
    """
    spellings: set[str] = set()
    for node in _own_expressions(statement):
        if _called_name(node) not in _MUTATING_METHODS or not isinstance(node, ast.Call):
            continue
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if isinstance(receiver, ast.Name | ast.Attribute | ast.Subscript):
            spellings.add(ast.unparse(receiver))
            if isinstance(receiver, ast.Subscript):
                spellings.add(ast.unparse(receiver.value))
    return spellings


def _written_spellings(statement: ast.stmt) -> frozenset[str]:
    """Return the spellings this statement binds or mutates, ignoring any nested statement's."""
    targets: list[ast.expr] = []
    match statement:
        case ast.Assign() | ast.Delete():
            targets.extend(statement.targets)
        case ast.AugAssign() | ast.AnnAssign() | ast.For() | ast.AsyncFor():
            targets.append(statement.target)
        case ast.With() | ast.AsyncWith():
            targets.extend(
                item.optional_vars for item in statement.items if item.optional_vars is not None
            )
        case _:
            pass
    targets.extend(
        node.target for node in _own_expressions(statement) if isinstance(node, ast.NamedExpr)
    )

    spellings: set[str] = _mutated_spellings(statement)
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Tuple | ast.List):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        elif isinstance(target, ast.Subscript):
            # `state[key] = ...` writes through `state`, which is how the condition reads it.
            spellings.add(ast.unparse(target))
            spellings.add(ast.unparse(target.value))
        elif isinstance(target, ast.Name | ast.Attribute):
            spellings.add(ast.unparse(target))
    return frozenset(spellings)


def _condition_writer_shapes(
    origin: ast.stmt, scope: ast.AST | None, guards: tuple[ast.expr, ...]
) -> tuple[str, ...]:
    """Return the normalized shapes of same-scope statements that feed the guard's condition.

    A guard is disabled as effectively by editing what its condition reads as by editing the
    condition, and neither the qualified name nor the origin statement moves when that happens.

    The selection is a transitive closure, not one hop. A guard reading a single name is usually
    two or more assignments away from the input that decides it, and stopping at the direct writer
    leaves every statement behind it outside the record.
    `_skip_static_env_option` is the concrete case: `scanner.env-option.static-split-string` tests
    `kind`, written by `kind = _ENV_LONG_OPTION_KINDS[option]`, whose `option` comes from
    `option, attached_value = _resolve_env_long_option(literal)`. Rewriting that call to a constant
    withdraws the guard, and with a one-hop rule the fingerprint does not move.

    The scope is deliberately the enclosing function rather than the whole module: a closure that
    crossed call boundaries would churn every frozen record on any edit to either module, and a
    debt record that churns constantly has to be regenerated, which is the laundering path this
    gate exists to close. A caller passing a different value into this scope is outside that
    boundary and is not covered.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        guards: The `if` tests governing the origin.

    Returns:
        Shapes in source order, empty when the guard is unconditional or scope-free.
    """
    read = set(_read_spellings(guards))
    if scope is None or not read:
        return ()
    candidates = [
        statement
        for statement in ast.walk(scope)
        if isinstance(statement, ast.stmt)
        if statement is not origin
    ]
    selected: dict[int, ast.stmt] = {}
    grew = True
    while grew:
        grew = False
        for statement in candidates:
            if id(statement) in selected or not (_written_spellings(statement) & read):
                continue
            selected[id(statement)] = statement
            # What this writer reads becomes part of the condition's dataflow, so the statements
            # feeding *it* are selected on the next pass. The scope bounds the fixpoint.
            read |= _statement_reads(statement)
            grew = True
    writers = sorted(
        selected.values(), key=lambda statement: (statement.lineno, statement.col_offset)
    )
    return tuple(_normalized_shape(statement) for statement in writers)


def _scope_statements(scope: ast.AST) -> list[ast.stmt]:
    """Return every statement the scope executes itself, excluding a nested scope's body.

    A nested function or class body does not run where it is written, so its statements decide
    nothing about whether the statements around it are reached.
    """
    found: list[ast.stmt] = []
    pending: list[ast.AST] = [scope]
    while pending:
        node = pending.pop()
        for child in ast.iter_child_nodes(node):
            # An `except` clause and a `match` arm are not statements, so descending only through
            # statements would drop the handler and arm bodies that hold the origins this rule is
            # for.
            if isinstance(child, ast.excepthandler | ast.match_case):
                pending.append(child)
            elif isinstance(child, ast.stmt) and not isinstance(child, _SCOPES):
                found.append(child)
                pending.append(child)
    return found


def _diverting_shape(statement: ast.stmt) -> str | None:
    """Return the shape of the control this statement exerts, or `None` when it exerts none.

    A branch is reduced to its test and a loop to its target and iterable, so an edit inside one
    branch's body does not churn the record of a guard outside it. Statements that transfer control
    are taken whole, because a `return` decides reachability through the value it returns:
    `return int(digits)` can raise where `return 0` cannot.
    """
    match statement:
        case ast.Return() | ast.Raise() | ast.Break() | ast.Continue():
            return _normalized_shape(statement)
        case ast.If() | ast.While():
            return f"test {ast.dump(statement.test)}"
        case ast.For() | ast.AsyncFor():
            return f"for {ast.dump(statement.target)} in {ast.dump(statement.iter)}"
        case ast.Match():
            arms = [
                part
                for case in statement.cases
                for part in (
                    ast.dump(case.pattern),
                    "" if case.guard is None else ast.dump(case.guard),
                )
            ]
            return " ".join(("match", ast.dump(statement.subject), *arms))
        case ast.Try() | ast.TryStar():
            handled = (
                "bare" if handler.type is None else ast.dump(handler.type)
                for handler in statement.handlers
            )
            return " ".join(("try", *handled))
        case _:
            return None


def _control_flow_shapes(origin: ast.stmt, scope: ast.AST | None) -> tuple[str, ...]:
    """Return the shapes of the control flow that decides whether a test-free origin is reached.

    A guard governed by an `if`, a `while` or a `match` arm records that test and the same-scope
    writes feeding it. A guard reached by falling through a chain of returns, by exhausting a loop,
    or through an `except` handler has no such test, so nothing about the code deciding it reaches
    the refusal would otherwise enter the record. `scanner.descriptor.unparsable` is the concrete
    case: it fires only because `return int(digits)` in its own `try` body can raise `ValueError`,
    and rewriting that to `return 0` withdraws the guard while leaving a byte-identical record.

    The scope is the enclosing function, the same boundary `_condition_writer_shapes` uses and for
    the same reason. Within it every diverting statement is taken, not only the ones lexically
    before the origin, because a loop lets a later statement run first. The cost is that any
    control-flow edit in that function churns such a record; the remedy is the outcome this
    inventory wants anyway, which is to classify the guard so it leaves the debt snapshot.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.

    Returns:
        Shapes in source order, empty when the origin has no enclosing function.
    """
    if scope is None:
        return ()
    statements = [statement for statement in _scope_statements(scope) if statement is not origin]
    statements.sort(key=lambda statement: (statement.lineno, statement.col_offset))
    return tuple(
        shape for statement in statements if (shape := _diverting_shape(statement)) is not None
    )


def _is_guard_refusal_call(node: ast.AST) -> bool:
    return _called_name(node) == REFUSAL_CONSTRUCTOR


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


def _declared_transport_shapes(tree: ast.AST, path: str) -> dict[str, str]:
    """Return the normalized shape of every declared transport this module defines.

    A transport does not mint an identifier, so it is never an origin. One of them nonetheless owns
    the condition that decides its callers' refusals: the parameterized cycle detector is handed a
    caller's `GuardRefusal` and tests `node in active` itself. Folding a called transport's shape
    into the caller's fingerprint is what keeps that deciding code inside a record.

    Args:
        tree: Parsed module.
        path: Module file name, used to resolve declared transports.

    Returns:
        Normalized shapes keyed by the callee name an origin statement would spell.
    """
    declared = {qualname for module, qualname, _ in DECLARED_TRANSPORTS if module == path}
    names = _annotate(tree).names
    shapes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if names.get(id(node), node.name) in declared:
            shapes[node.name] = _normalized_shape(node)
    return shapes


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
    annotations = _annotate(tree)
    transports = _declared_transport_shapes(tree, path)
    records: list[OriginRecord] = []
    for statement, call in _origin_calls_by_statement(tree):
        if not call.args or not isinstance(call.args[0], ast.Constant):
            raise ValueError(f"{path}: guard identifier must be a string literal")
        origin_id = call.args[0].value
        if not isinstance(origin_id, str):
            raise ValueError(f"{path}: guard identifier must be a string literal")
        qualname = annotations.names.get(id(statement), "")
        condition = annotations.conditions.get(id(statement), "")
        scope = annotations.scopes.get(id(statement))
        tests = annotations.tests.get(id(statement), ())
        writers = _condition_writer_shapes(statement, scope, tests)
        # A guard with a test records that test and what writes it; one without records the control
        # flow that decides it is reached at all, which is the only description that guard has.
        flow = () if tests else _control_flow_shapes(statement, scope)
        called = {name for node in _own_expressions(statement) if (name := _called_name(node))}
        carried = tuple(shape for name, shape in sorted(transports.items()) if name in called)
        digest = hashlib.sha256(
            "\n".join(
                (qualname, condition, _normalized_shape(statement), *writers, *carried, *flow)
            ).encode("utf-8")
        ).hexdigest()
        records.append(OriginRecord(origin_id, path, qualname, _group_digest(digest)))
    return tuple(sorted(records, key=lambda record: (record.origin_id, record.fingerprint)))


def _is_refusal_shape(node: ast.expr) -> bool:
    return _called_name(node) in {REFUSAL_CONSTRUCTOR, *GUARD_FREE_VERDICTS}


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
        called = _called_name(node)
        if called is None or not isinstance(node, ast.Call):
            continue
        if called in REFUSAL_EXCEPTIONS:
            candidates = [*node.args, *(keyword.value for keyword in node.keywords)]
        elif called == RESULT_CONSTRUCTOR:
            candidates = [
                *node.args[1:],
                *(keyword.value for keyword in node.keywords if keyword.arg != "invocations"),
            ]
        else:
            continue
        qualname = names.get(id(node), "")
        for argument in candidates:
            if _is_refusal_shape(argument):
                continue
            if _is_raw_text(argument):
                violations.append(
                    f"{path}:{qualname}: {called} carries raw refusal text; "
                    f"construct a {REFUSAL_CONSTRUCTOR} at the guard origin"
                )
                continue
            expression = ast.unparse(argument)
            if (path, qualname, expression) not in DECLARED_TRANSPORTS:
                violations.append(
                    f"{path}:{qualname}: {called} carries undeclared transport "
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
    names = _annotate(tree).names
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
    "_CASE_HEADER_PATTERN_WORDS": (
        "The number of words a complete `case` header spells before its first pattern: the "
        "reserved word, the subject, `in`, and the pattern itself. A grammatical arity, not a "
        "budget, and reached by authoring a shorter header."
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
    names = _annotate(tree).names
    violations: list[str] = []

    for node in ast.walk(tree):
        if _called_name(node) not in LIMITS_CONSTRUCTORS or not isinstance(node, ast.Call):
            continue
        qualname = names.get(id(node), "")
        if (path, qualname.split(".")[0] if qualname else "") in LIMITS_BOUNDARIES:
            continue
        if (path, qualname) in LIMITS_BOUNDARIES:
            continue
        # A configured construction is as dangerous as a default one: it restores production-scale
        # caps under a shrunk scan budget, which is the failure this rule exists to prevent.
        spelling = "default limits" if not (node.args or node.keywords) else "limits"
        violations.append(
            f"{path}:{qualname or '<module>'} constructs {spelling}; accept the scan's limits "
            f"instead so a shrunk cap reaches this guard"
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


_THRESHOLD_PREFIXES = ("_MAX_", "_EVAL_", "_SURROGATE")
"""Naming conventions that mark a threshold even when the module does not define it, so a guard
referencing an imported or not-yet-written bound is still caught."""

STRUCTURAL_GUARD_LITERALS = frozenset({0, 1})
"""Integer literals a guard may compare against without naming them.

Zero and one are the emptiness and singleton cases: `remaining < 1` asks whether a counter that a
limits field seeded is exhausted, and `len(results) != 1` asks about arity. Neither is a resource
budget. Any other bare literal in a guard comparison is a magnitude with no recorded provenance,
so it must be named and inventoried instead.
"""


def _module_constants(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Return the module-level bound names, and the subset bound to a numeric literal."""
    bound: set[str] = set()
    numeric: set[str] = set()
    for node in getattr(tree, "body", ()):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        names = {target.id for target in targets if isinstance(target, ast.Name)}
        bound |= names
        value = node.value
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, int | float)
            and not isinstance(value.value, bool)
        ):
            numeric |= names
    return frozenset(bound), frozenset(numeric)


def _threshold_names(
    nodes: tuple[ast.AST, ...], bound: frozenset[str], numeric: frozenset[str]
) -> set[str]:
    """Return every named threshold the guard's condition or origin statement references.

    A module-level numeric constant is a threshold whatever it is called. A conventionally named
    one the module does not define cannot be resolved here, so it is treated as a threshold too;
    a name the module binds to something other than a number is not one.
    """
    return {
        node.id
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Name)
        if node.id in numeric or (node.id.startswith(_THRESHOLD_PREFIXES) and node.id not in bound)
    }


def _operand_magnitudes(operand: ast.expr) -> set[int | float]:
    """Return the numeric magnitudes one comparison operand carries.

    Descent follows arithmetic only. `depth - 4096 > 0` caps the scan at the same magnitude as
    `depth > 4096`, so an operand that merely wraps its literal in arithmetic must not escape the
    rule. A subscript index or a keyword argument nested in an operand is a position rather than a
    magnitude, and is not descended into.
    """
    found: set[int | float] = set()
    pending = [operand]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Constant):
            if isinstance(current.value, int | float) and not isinstance(current.value, bool):
                found.add(current.value)
        elif isinstance(current, ast.BinOp):
            pending.extend((current.left, current.right))
        elif isinstance(current, ast.UnaryOp):
            pending.append(current.operand)
    return found


def _threshold_literals(nodes: tuple[ast.AST, ...]) -> set[int | float]:
    """Return every bare numeric magnitude the guard compares against."""
    return {
        literal
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Compare)
        for operand in (node.left, *node.comparators)
        for literal in _operand_magnitudes(operand)
        if literal not in STRUCTURAL_GUARD_LITERALS
    }


def find_threshold_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every guard threshold that is neither a limits field nor an inventoried bound.

    A threshold is recognized structurally rather than by naming convention: any module-level
    numeric constant the guard references, and any bare numeric magnitude it compares against.
    Recognizing only conventionally named constants would let both a generically named bound and a
    raw literal introduce a resource cap with no provenance.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used in messages.

    Returns:
        Human-readable violations, empty when every guard threshold has declared provenance.
    """
    tree = ast.parse(source)
    annotations = _annotate(tree)
    bound, numeric = _module_constants(tree)
    violations: list[str] = []
    for statement, _call in _origin_calls_by_statement(tree):
        nodes: tuple[ast.AST, ...] = (statement, *annotations.tests.get(id(statement), ()))
        for name in sorted(_threshold_names(nodes, bound, numeric)):
            if name in FIXED_SEMANTIC_BOUNDS:
                continue
            violations.append(
                f"{path}: guard threshold {name} is neither a scan-limits field nor an "
                f"inventoried fixed semantic bound"
            )
        violations.extend(
            f"{path}: guard threshold literal {literal} has no provenance; take it from the "
            f"scan's limits, or name it and inventory it as a fixed semantic bound"
            for literal in sorted(_threshold_literals(nodes))
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


def classified_origin_ids(root: Path) -> frozenset[str]:
    """Return every guard identifier the witness registry classifies.

    Args:
        root: Repository root holding the registry.

    Returns:
        Identifiers carried by a reachable or invariant witness.

    Raises:
        ValueError: If a classification carries a non-literal identifier.
    """
    return classified_ids_in_registry((root / REGISTRY_PATH).read_text(encoding="utf-8"))


def _classification_registry_tuples(tree: ast.Module) -> dict[str, ast.Tuple]:
    bindings = Counter(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and node.id in CLASSIFICATION_REGISTRIES
    )
    assignments: dict[str, list[ast.expr | None]] = {
        registry_name: [] for registry_name in CLASSIFICATION_REGISTRIES
    }
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                continue
            registry_name = statement.targets[0].id
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            registry_name = statement.target.id
        else:
            continue
        if registry_name in assignments:
            assignments[registry_name].append(statement.value)

    registries: dict[str, ast.Tuple] = {}
    for registry_name, values in assignments.items():
        if bindings[registry_name] != 1 or len(values) != 1:
            raise ValueError(
                f"{REGISTRY_PATH}: {registry_name} must have exactly one direct module-level "
                "binding"
            )
        value = values[0]
        if not isinstance(value, ast.Tuple):
            raise ValueError(f"{REGISTRY_PATH}: {registry_name} must be a tuple literal")
        registries[registry_name] = value
    return registries


def _classification_identifier(
    entry: ast.expr,
    *,
    registry_name: str,
    constructor: str,
    fields: tuple[str, ...],
    required_count: int,
) -> str:
    if not isinstance(entry, ast.Call) or _called_name(entry) != constructor:
        raise ValueError(
            f"{REGISTRY_PATH}: {registry_name} entries must directly call {constructor}"
        )
    if any(isinstance(argument, ast.Starred) for argument in entry.args):
        raise ValueError(f"{REGISTRY_PATH}: {constructor} does not accept starred arguments")
    if len(entry.args) > len(fields):
        raise ValueError(
            f"{REGISTRY_PATH}: {constructor} has too many positional arguments; "
            f"expected at most {len(fields)}"
        )

    arguments = dict(zip(fields, entry.args, strict=False))
    for keyword in entry.keywords:
        if keyword.arg is None:
            raise ValueError(f"{REGISTRY_PATH}: {constructor} does not accept keyword expansion")
        if keyword.arg not in fields:
            raise ValueError(f"{REGISTRY_PATH}: {constructor} has unexpected field {keyword.arg!r}")
        if keyword.arg in arguments:
            raise ValueError(
                f"{REGISTRY_PATH}: {constructor} supplies {keyword.arg!r} more than once"
            )
        arguments[keyword.arg] = keyword.value

    missing = [field for field in fields[:required_count] if field not in arguments]
    if missing:
        raise ValueError(
            f"{REGISTRY_PATH}: {constructor} is missing required evidence field(s): "
            f"{', '.join(missing)}"
        )
    identifier = arguments["origin_id"]
    if not isinstance(identifier, ast.Constant) or not isinstance(identifier.value, str):
        raise ValueError(f"{REGISTRY_PATH}: classification identifiers must be literals")
    return identifier.value


def classified_ids_in_registry(source: str) -> frozenset[str]:
    """Return every guard identifier one witness registry's source classifies.

    The registry is parsed, never imported: this checker runs from the protected base against a
    candidate tree, so candidate code is only ever read as data.

    Args:
        source: Registry source text.

    Returns:
        Identifiers carried by a reachable or invariant witness.

    Raises:
        ValueError: If either executable registry or one of its entries is non-canonical.
    """
    tree = ast.parse(source)
    identifiers: set[str] = set()
    registries = _classification_registry_tuples(tree)
    for registry_name, (constructor, fields, required_count) in CLASSIFICATION_REGISTRIES.items():
        identifiers.update(
            _classification_identifier(
                entry,
                registry_name=registry_name,
                constructor=constructor,
                fields=fields,
                required_count=required_count,
            )
            for entry in registries[registry_name].elts
        )
    return frozenset(identifiers)


def unclassified_origin_records(root: Path) -> tuple[OriginRecord, ...]:
    """Return the canonical records of every guard origin the registry does not classify.

    Args:
        root: Repository root to derive from.

    Returns:
        Records ordered by identifier.
    """
    classified = classified_origin_ids(root)
    return tuple(
        record for record in repository_origin_records(root) if record.origin_id not in classified
    )


def repository_closure_violations(root: Path) -> tuple[str, ...]:
    """Return every guard origin that is not classified exactly once.

    Source origins must partition exactly into the witness registry and the frozen debt snapshot,
    with the two disjoint, and one identifier must name one origin. Comparing identifier sets alone
    would let a second, unwitnessed guard reuse an already-classified identifier and inherit its
    evidence, since every set-level relation still holds.

    The retirement ledger is held disjoint from source origins for the same reason. A row naming a
    guard that is still live has no effect on the withdrawal comparison, so nothing would reject it
    when it is written; a later change could then delete that guard and have its removal absorbed
    by a row it did not add, which is the ledger diff that was supposed to be the review signal.

    Args:
        root: Repository root to check.

    Returns:
        Human-readable violations, empty when the partition holds.
    """
    records = repository_origin_records(root)
    source = {record.origin_id for record in records}
    classified = classified_origin_ids(root)
    debt = {record.origin_id for record in load_debt_records(root)}
    retired = load_retired_origin_ids(root)
    sites = Counter(record.origin_id for record in records)
    return (
        *(
            f"{origin_id} is recorded as retired in {RETIREMENT_PATH} but is still a guard origin "
            f"in this tree; retire a guard in the change that withdraws it, not before"
            for origin_id in sorted(retired & source)
        ),
        *(
            f"{origin_id} is constructed at {count} guard origins; one identifier classifies one "
            f"origin, so give each site its own identifier"
            for origin_id, count in sorted(sites.items())
            if count > 1
        ),
        *(
            f"{origin_id} is neither classified nor frozen as debt; add a witness to "
            f"{REGISTRY_PATH} or freeze it"
            for origin_id in sorted(source - classified - debt)
        ),
        *(
            f"{origin_id} is both classified and frozen as debt; a classified guard carries "
            f"evidence and must leave the debt snapshot"
            for origin_id in sorted(classified & debt)
        ),
        *(
            f"{origin_id} is classified or frozen but is not a guard origin in this tree"
            for origin_id in sorted((classified | debt) - source)
        ),
    )


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


def load_retired_origin_ids(root: Path) -> frozenset[str]:
    """Return the guard identifiers this tree records as deliberately retired.

    Removing a guard from source together with its witness row leaves every partition intact, so
    closure cannot see it and the base-relative comparison has nothing left to inspect. A
    retirement is therefore a declared artifact: the identifier and a reason, in a file whose diff
    is the review signal that a fail-closed guard was withdrawn.

    Args:
        root: Repository root holding the retirement ledger.

    Returns:
        Retired identifiers, empty when the ledger is absent.

    Raises:
        ValueError: If an entry carries no identifier or no reason.
    """
    path = root / RETIREMENT_PATH
    if not path.exists():
        return frozenset()
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("records", [])
    if not isinstance(rows, list):
        raise ValueError(f"{RETIREMENT_PATH}: records must be a list")
    retired: set[str] = set()
    for row in rows:
        identifier = row.get(IDENTITY_FIELD) if isinstance(row, dict) else None
        reason = row.get("reason") if isinstance(row, dict) else None
        if not isinstance(identifier, str):
            raise ValueError(
                f"{RETIREMENT_PATH}: every record must carry a string {IDENTITY_FIELD}"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{RETIREMENT_PATH}: {identifier} must record why it was retired")
        retired.add(identifier)
    return frozenset(retired)


def _decode_identifiers(payload: dict[str, object], origin: str) -> tuple[str, ...]:
    """Return the origin identifiers a snapshot claims, tolerating a foreign record schema.

    Args:
        payload: Decoded snapshot JSON.
        origin: Snapshot name, used in messages.

    Returns:
        The claimed identifiers, in snapshot order.

    Raises:
        ValueError: If the records are not objects carrying a string identifier.
    """
    rows = payload.get("records", [])
    if not isinstance(rows, list):
        raise ValueError(f"{origin}: records must be a list")
    identifiers: list[str] = []
    for row in rows:
        identifier = row.get(IDENTITY_FIELD) if isinstance(row, dict) else None
        if not isinstance(identifier, str):
            raise ValueError(f"{origin}: every record must carry a string {IDENTITY_FIELD}")
        identifiers.append(identifier)
    return tuple(identifiers)


def emit_records(root: Path) -> str:
    """Return the tree's derived debt snapshot as schema-versioned JSON.

    The snapshot is derived from source rather than copied: it is exactly the guard origins the
    witness registry does not classify, so regenerating it after a guard legitimately moves cannot
    quietly retain a stale entry.

    Args:
        root: Repository root to derive from.

    Returns:
        JSON text with a trailing newline.
    """
    records = unclassified_origin_records(root)
    payload = {"schema": SCHEMA_VERSION, "records": [record.as_json() for record in records]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def compare_against_base(
    root: Path,
    base_snapshot: str,
    base_registry: str | None = None,
) -> tuple[str, ...]:
    """Return every way the candidate's guard inventory weakened relative to a base revision.

    Nothing the candidate asserts about a guard is taken on trust. This checker is the base
    revision's copy, so it reads only the identifiers the candidate freezes and re-derives their
    records from candidate source with its own extractor. A record that the base did not carry is
    new debt whether the identifier is new or the guard behind it was edited while frozen.

    Reading the candidate snapshot by identifier alone is also what lets the record schema and the
    fingerprint derivation migrate: see `IDENTITY_FIELD`. The base snapshot is the base revision's
    own output and is still decoded strictly.

    Given the base's witness registry as well, the comparison also covers guard *existence*: an
    origin the base classified or froze that the candidate's source no longer constructs is a
    withdrawn guard, and closure cannot see it because deleting the origin and its witness row
    together leaves the partition exact. Such a removal is accepted only when the candidate's
    retirement ledger records it.

    Args:
        root: Repository root holding the candidate tree.
        base_snapshot: JSON text emitted by the base revision's snapshot.
        base_registry: The base revision's witness registry source, when available.

    Returns:
        Human-readable failures, empty when the inventory only shrank in debt or held steady.

    Raises:
        ValueError: If the base snapshot was written under a different record schema, or the
            candidate snapshot does not carry identifiers.
    """
    base = set(_decode_records(json.loads(base_snapshot), "base snapshot"))
    claimed = _decode_identifiers(
        json.loads((root / DEBT_PATH).read_text(encoding="utf-8")), DEBT_PATH
    )
    derived: dict[str, list[OriginRecord]] = {}
    for record in repository_origin_records(root):
        derived.setdefault(record.origin_id, []).append(record)

    grew: list[str] = []
    laundered: list[str] = []
    for origin_id in sorted(set(claimed)):
        records = derived.get(origin_id)
        if not records:
            laundered.append(
                f"{origin_id} is frozen as debt but is not a guard origin in the candidate "
                f"source; the record was laundered onto a guard that moved or no longer exists"
            )
            continue
        grew.extend(
            f"{record.origin_id} ({record.path}:{record.qualname}) is new fail-closed guard debt; "
            f"classify it as reachable or invariant instead of freezing it"
            for record in records
            if record not in base
        )

    withdrawn: list[str] = []
    if base_registry is not None:
        retired = load_retired_origin_ids(root)
        base_known = {record.origin_id for record in base} | classified_ids_in_registry(
            base_registry
        )
        withdrawn.extend(
            f"{origin_id} was a fail-closed guard origin at the base and is not one in the "
            f"candidate source; record it in {RETIREMENT_PATH} with the reason it was withdrawn"
            for origin_id in sorted(base_known - set(derived) - retired)
        )
    return (*grew, *laundered, *withdrawn)


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
    parser.add_argument(
        "--compare-base",
        type=Path,
        help="base debt snapshot to compare against; runs the base-owned checks only",
    )
    parser.add_argument(
        "--base-registry",
        type=Path,
        help="the base revision's witness registry, so guard withdrawal is covered too",
    )
    arguments = parser.parse_args(argv)

    if arguments.emit_debt:
        sys.stdout.write(emit_records(arguments.root))
        return 0

    if arguments.compare_base is not None:
        # This copy of the checker is the base revision's, so its shape, limits and threshold
        # allowlists describe the base's source rather than the candidate's. Running them here
        # would reject a legitimate rename, a newly declared transport or a newly inventoried
        # fixed bound with no fix available inside the same change, and on a push to main it would
        # take the release job down with it. Those three gates run from the candidate's own copy in
        # the test suite. Closure names no allowlist: it derives the whole partition from the
        # candidate tree, so running it from the base is what makes it unweakenable.
        failures = [
            *repository_closure_violations(arguments.root),
            *compare_against_base(
                arguments.root,
                arguments.compare_base.read_text(encoding="utf-8"),
                base_registry=(
                    None
                    if arguments.base_registry is None
                    else arguments.base_registry.read_text(encoding="utf-8")
                ),
            ),
        ]
    else:
        failures = [
            *repository_shape_violations(arguments.root),
            *repository_limits_violations(arguments.root),
            *repository_threshold_violations(arguments.root),
            *repository_closure_violations(arguments.root),
        ]
    for failure in failures:
        sys.stderr.write(f"{failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
