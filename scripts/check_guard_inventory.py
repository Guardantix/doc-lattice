#!/usr/bin/env python3
"""Extract and gate the CI shell scanner's fail-closed guard origins.

Every fail-closed guard below the CI scanner's guard package has one *origin*: the site that
detects the condition and constructs a `GuardRefusal`. This module reads that package as inert
source text, never importing it, and produces one canonical record per origin.

It enforces three separable properties:

1. **Canonical refusal shapes.** A refusal may only reach an exception, a `ShellScanResult`, or a
   verdict return as a `GuardRefusal` construction with a literal identifier and literal reason,
   or as one of the explicitly declared transports. Raw text, executable reason expressions and
   arbitrary verdict expressions are rejected, so a future change cannot bypass construction of
   the discriminated value.
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
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

SCHEMA_VERSION = 8
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

GUARD_MODULE_ROOT = "src/doc_lattice/github_ci"
"""The package every guarded module lives in.

The base-owned closure and comparison discover guard-protocol modules recursively from the
candidate tree, so a candidate can add a guarded module that the protected base tuple could not
have named. Shape validation reads the discovered surface directly. `GUARDED_MODULES` remains the
candidate-owned allowlist for limits and threshold checks; `repository_coverage_violations`
rejects any discovered module omitted from that tuple."""

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
REASON_ARGUMENT_INDEX = 1
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
        # The taint boundary projects the refusal caught from an internal evidence or limits error.
        ("shell_taint.py", "analyze_marker_taint", "error.refusal"),
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


def _referenced_name(node: ast.AST) -> str | None:
    """Return the final component of a bare or attribute-qualified reference."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_string_literal(node: ast.AST) -> bool:
    """Return whether this node is a direct string literal."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _rebinding_targets(node: ast.AST) -> tuple[str | None, list[ast.expr]]:
    """Return the constructor spelling a binding reads and the names it binds it to.

    A call is recognized by its final name component everywhere here, so a binding must be read the
    same way: `GR = shell_guards.GuardRefusal` names the constructor exactly as `GR = GuardRefusal`
    does, and reading only the bare-name form is what let the first spelling escape every rule.

    Args:
        node: Any AST node.

    Returns:
        The name the right-hand side spells, or `None` when the node is not a value binding, paired
        with the binding's targets.
    """
    match node:
        case ast.Assign(value=value, targets=targets):
            pass
        case ast.AnnAssign(value=ast.expr() as value, target=target):
            targets = [target]
        case _:
            return None, []
    match value:
        case ast.Name(id=spelling) | ast.Attribute(attr=spelling):
            return spelling, targets
        case _:
            return None, []


def _constructor_names(tree: ast.AST, constructors: frozenset[str]) -> frozenset[str]:
    """Return every local name this module binds to one of these constructors.

    Recognition is by name everywhere in this module, so import aliases and module-level
    rebindings must participate in every constructor-specific rule. Otherwise an aliased refusal
    can disappear from the inventory, an aliased verdict can be rejected despite being canonical,
    or an aliased limits factory can silently mint a fresh production budget.

    Args:
        tree: Parsed module.
        constructors: Canonical constructor names to follow through imports and rebindings.

    Returns:
        The canonical constructor names together with every alias bound to one of them.
    """
    names = set(constructors)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.asname is not None and alias.name.rsplit(".", 1)[-1] in constructors:
                names.add(alias.asname)
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            spelling, targets = _rebinding_targets(node)
            if spelling is None or spelling not in names:
                continue
            rebound = {target.id for target in targets if isinstance(target, ast.Name)} - names
            if rebound:
                names |= rebound
                grew = True
    return frozenset(names)


def _refusal_constructor_names(tree: ast.AST) -> frozenset[str]:
    """Return every local name this module binds to the refusal constructor."""
    return _constructor_names(tree, frozenset({REFUSAL_CONSTRUCTOR}))


@dataclass(frozen=True, slots=True)
class _DerivationCache:
    """Per-parse memo for the derivations repeated across every origin in one module.

    The writer closure re-derives the same statement's written spellings and normalized shape once
    per fixpoint pass and once per origin sharing the scope, which dominates the gate's runtime.
    Keying by node identity is sound because one cache lives no longer than the parse it was built
    for, so an identity cannot be reused by a later node.
    """

    shapes: dict[int, str] = field(default_factory=dict)
    written: dict[int, frozenset[str]] = field(default_factory=dict)
    configured: dict[int, frozenset[str]] = field(default_factory=dict)
    statements: dict[int, list[ast.stmt]] = field(default_factory=dict)
    parents: dict[int, dict[int, ast.AST]] = field(default_factory=dict)
    module: ast.Module | None = None


@dataclass(frozen=True, slots=True)
class OriginRecord:
    """One canonical, line-number-free identity for a fail-closed guard origin.

    Attributes:
        origin_id: The literal identifier the origin constructs.
        path: Repository-relative module the origin lives in.
        qualname: Enclosing qualified name, so moving a guard changes its record.
        fingerprint: Digest over the qualname, the guarding condition, the origin statement shape
            with operator-facing reason text normalized away, the shapes of the function-local and
            referenced module statements that write what the condition and any enclosing loop's
            iterable read, the headers of those enclosing loops, the shape of any declared
            transport the origin statement hands its refusal to, the control flow that decides
            whether it is reached and the writers feeding that flow, and any `try` body whose
            handler contains the origin.
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
        loops: The enclosing `for` statements, outermost first, keyed by node identity. A `while`
            is absent because its test is already a guarding test; a `for` exposes none.
        scopes: Nearest enclosing function, keyed by node identity, or `None` at class or module
            level.
    """

    names: dict[int, str]
    conditions: dict[int, str]
    tests: dict[int, tuple[ast.expr, ...]]
    loops: dict[int, tuple[ast.For | ast.AsyncFor, ...]]
    scopes: dict[int, ast.AST | None]


@dataclass(frozen=True, slots=True)
class _Context:
    """The annotation state one node inherits from the code enclosing it.

    Attributes:
        prefix: Qualified name of the enclosing scope.
        condition: Guarding condition as text, conjoined outermost first.
        guards: The guarding tests themselves, outermost first.
        loops: The enclosing `for` statements, outermost first.
        scope: Nearest enclosing function, or `None` at class or module level.
    """

    prefix: str = ""
    condition: str = ""
    guards: tuple[ast.expr, ...] = ()
    loops: tuple[ast.For | ast.AsyncFor, ...] = ()
    scope: ast.AST | None = None

    def under(self, condition: str, test: ast.expr | None = None) -> _Context:
        """Return this context with one more guarding condition, and optionally its test."""
        joined = condition if not self.condition else f"{self.condition} and {condition}"
        guards = self.guards if test is None else (*self.guards, test)
        return replace(self, condition=joined, guards=guards)

    def inside(self, loop: ast.For | ast.AsyncFor) -> _Context:
        """Return this context with one more enclosing loop."""
        return replace(self, loops=(*self.loops, loop))

    def entering(self, scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> _Context:
        """Return the context a nested scope's body runs under.

        Nothing enclosing the definition governs the body, which does not run where it is written,
        so every guarding condition and enclosing loop is dropped and only the name accumulates.
        """
        return _Context(
            prefix=f"{self.prefix}.{scope.name}" if self.prefix else scope.name,
            scope=None if isinstance(scope, ast.ClassDef) else scope,
        )


def _annotate(tree: ast.AST) -> _Annotations:
    """Map every node to its enclosing scope, qualified name and nearest guarding condition."""
    names: dict[int, str] = {}
    conditions: dict[int, str] = {}
    tests: dict[int, tuple[ast.expr, ...]] = {}
    loops: dict[int, tuple[ast.For | ast.AsyncFor, ...]] = {}
    scopes: dict[int, ast.AST | None] = {}

    def descend(node: ast.AST, context: _Context) -> None:
        for child in ast.iter_child_nodes(node):
            record(child, context)

    def record(node: ast.AST, context: _Context) -> None:
        # Entering a nested scope is handled here rather than in `descend`, because a body this
        # function walks itself, such as an `if` or a `for` body, never passes through `descend`.
        # A function defined in one of those bodies would otherwise keep its parent's qualified
        # name, and carry a guarding condition that its own body does not actually run under.
        if isinstance(node, _SCOPES):
            context = context.entering(node)
        names[id(node)] = context.prefix
        conditions[id(node)] = context.condition
        tests[id(node)] = context.guards
        loops[id(node)] = context.loops
        scopes[id(node)] = context.scope
        # An `if` nested directly inside another `if` body, and every `elif`, arrives here rather
        # than through `descend`'s child loop. Handling it here is what keeps the innermost test
        # in the recorded condition instead of the enclosing one. A `while` test decides a refusal
        # exactly as an `if` test does, so it is treated the same way.
        if isinstance(node, ast.If | ast.While):
            test = " ".join(ast.unparse(node.test).split())
            record(node.test, context)
            for statement in node.body:
                record(statement, context.under(test, node.test))
            for statement in node.orelse:
                record(statement, context.under(f"not ({test})", node.test))
            return
        # A `for` header decides whether its body runs at all, yet it exposes no test for the
        # condition rules to pick up, so a guard nested in the body recorded nothing about it.
        # Carrying the enclosing loops is what puts the header, and the writers feeding its
        # iterable, inside that guard's record.
        if isinstance(node, ast.For | ast.AsyncFor):
            record(node.target, context)
            record(node.iter, context)
            for statement in (*node.body, *node.orelse):
                record(statement, context.inside(node))
            return
        if isinstance(node, ast.Match):
            subject = " ".join(ast.unparse(node.subject).split())
            record(node.subject, context)
            for case in node.cases:
                pattern = " ".join(ast.unparse(case.pattern).split())
                arm = context.under(f"match {subject} case {pattern}")
                if case.guard is not None:
                    arm = arm.under(" ".join(ast.unparse(case.guard).split()), case.guard)
                    record(case.guard, context)
                for statement in case.body:
                    record(statement, arm)
            return
        if isinstance(node, ast.ExceptHandler):
            handled = ast.unparse(node.type) if node.type is not None else "BaseException"
            descend(node, context.under(f"except {handled}"))
            return
        descend(node, context)

    descend(tree, _Context())
    return _Annotations(names, conditions, tests, loops, scopes)


def _normalized_shape(statement: ast.stmt) -> str:
    """Return the statement's structure with operator-facing reason text removed.

    Rewording a refusal's message is not a semantic change to the guard, so the reason argument is
    replaced with an empty literal before hashing. Only literal reason text is normalized;
    executable expressions and every other part of the origin statement are retained.
    """
    clone = copy.deepcopy(statement)
    for node in ast.walk(clone):
        if (
            _called_name(node) == REFUSAL_CONSTRUCTOR
            and isinstance(node, ast.Call)
            and len(node.args) > REASON_ARGUMENT_INDEX
            and _is_string_literal(node.args[REASON_ARGUMENT_INDEX])
        ):
            node.args[REASON_ARGUMENT_INDEX] = ast.Constant(value="")
    return ast.dump(clone, include_attributes=False)


def _cached_shape(statement: ast.stmt, cache: _DerivationCache) -> str:
    """Return the statement's normalized shape, deriving it at most once per parse."""
    shape = cache.shapes.get(id(statement))
    if shape is None:
        shape = _normalized_shape(statement)
        cache.shapes[id(statement)] = shape
    return shape


def _cached_written_spellings(statement: ast.stmt, cache: _DerivationCache) -> frozenset[str]:
    """Return the statement's written spellings, deriving them at most once per parse."""
    spellings = cache.written.get(id(statement))
    if spellings is None:
        spellings = _written_spellings(statement)
        cache.written[id(statement)] = spellings
    return spellings


def _cached_configured_receivers(statement: ast.stmt, cache: _DerivationCache) -> frozenset[str]:
    """Return the statement's configured receivers, deriving them at most once per parse."""
    receivers = cache.configured.get(id(statement))
    if receivers is None:
        receivers = _configured_receivers(statement)
        cache.configured[id(statement)] = receivers
    return receivers


def _cached_scope_statements(scope: ast.AST, cache: _DerivationCache) -> list[ast.stmt]:
    """Return the scope's own statements, walking the scope at most once per parse."""
    statements = cache.statements.get(id(scope))
    if statements is None:
        statements = _scope_statements(scope)
        cache.statements[id(scope)] = statements
    return statements


def _cached_scope_parents(scope: ast.AST, cache: _DerivationCache) -> dict[int, ast.AST]:
    """Return each of the scope's own statements mapped to the node holding it.

    Descent follows `_scope_statements`, so an `except` clause and a `match` arm are linked even
    though neither is a statement, and a nested function or class body is not entered.

    Args:
        scope: Enclosing function to link the statements of.
        cache: Per-parse derivation memo.

    Returns:
        Holding nodes keyed by node identity.
    """
    parents = cache.parents.get(id(scope))
    if parents is None:
        parents = {}
        pending: list[ast.AST] = [scope]
        while pending:
            node = pending.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.excepthandler | ast.match_case) or (
                    isinstance(child, ast.stmt) and not isinstance(child, _SCOPES)
                ):
                    parents[id(child)] = node
                    pending.append(child)
        cache.parents[id(scope)] = parents
    return parents


def _repeated_statements(
    origin: ast.stmt, scope: ast.AST, cache: _DerivationCache
) -> frozenset[int]:
    """Return the statements a loop around the origin can run before the origin runs again.

    Lexical order decides what can execute before a statement everywhere except inside a loop,
    where the statements after the origin run again ahead of the next iteration's origin. Taking
    the outermost enclosing loop covers every inner one, since an inner loop's body is part of it.

    Args:
        origin: The statement that constructs the refusal.
        scope: Enclosing function.
        cache: Per-parse derivation memo.

    Returns:
        Node identities of the statements inside the outermost enclosing loop, empty when no loop
        encloses the origin.
    """
    parents = _cached_scope_parents(scope, cache)
    outermost: ast.stmt | None = None
    node: ast.AST | None = origin
    while (node := parents.get(id(node))) is not None:
        if isinstance(node, ast.For | ast.AsyncFor | ast.While):
            outermost = node
    if outermost is None:
        return frozenset()
    return frozenset(id(statement) for statement in _scope_statements(outermost))


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


def _executed_expressions(statement: ast.stmt) -> list[ast.AST]:
    """Return expressions executed by this statement in its surrounding scope.

    Constructing a lambda executes its defaults but defers its body to a different execution
    scope. List, set and dictionary comprehensions execute immediately. Constructing a generator
    expression evaluates only its first iterable; its body, filters and later iterables are
    deferred until iteration. This traversal follows runtime side effects only. Compile-time
    bindings such as a walrus in a deferred generator body use a separate lexical traversal.
    """
    executed: list[ast.AST] = []
    pending: list[ast.AST] = [
        child for child in ast.iter_child_nodes(statement) if not isinstance(child, ast.stmt)
    ]
    while pending:
        node = pending.pop()
        executed.append(node)
        if isinstance(node, ast.Lambda):
            pending.extend(node.args.defaults)
            pending.extend(default for default in node.args.kw_defaults if default is not None)
            continue
        if isinstance(node, ast.GeneratorExp):
            pending.append(node.generators[0].iter)
            continue
        pending.extend(
            child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.stmt)
        )
    return executed


def _target_names(target: ast.AST) -> frozenset[str]:
    """Return names one assignment, pattern or expression-scope target binds."""
    names: set[str] = set()
    match target:
        case ast.Name(id=name):
            names.add(name)
        case ast.MatchAs(pattern=pattern, name=name):
            if pattern is not None:
                names.update(_target_names(pattern))
            if name is not None:
                names.add(name)
        case ast.MatchStar(name=str() as name):
            names.add(name)
        case ast.MatchMapping(patterns=patterns, rest=rest):
            names.update(name for pattern in patterns for name in _target_names(pattern))
            if rest is not None:
                names.add(rest)
        case ast.MatchClass(patterns=patterns, kwd_patterns=keyword_patterns):
            names.update(
                name
                for pattern in (*patterns, *keyword_patterns)
                for name in _target_names(pattern)
            )
        case ast.MatchOr(patterns=patterns) | ast.MatchSequence(patterns=patterns):
            names.update(name for pattern in patterns for name in _target_names(pattern))
        case ast.Tuple(elts=elements) | ast.List(elts=elements):
            names.update(name for element in elements for name in _target_names(element))
        case ast.Starred(value=value):
            names.update(_target_names(value))
    return frozenset(names)


def _argument_names(arguments: ast.arguments) -> frozenset[str]:
    """Return every spelling local to a function or lambda through its parameters."""
    names = {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return frozenset(names)


def _reference_root_name(node: ast.AST) -> str | None:
    """Return the lexical root name of an attribute or subscript expression."""
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _free_reference_nodes(
    root: ast.AST,
    *,
    skip_nested_statements: bool = False,
) -> tuple[ast.Name | ast.Attribute, ...]:
    """Return load references that resolve outside nested expression-local scopes.

    Lambda parameters and comprehension targets are local only to their expression scope. Raw
    `ast.walk` cannot distinguish those loads from a free module read of the same spelling. The
    first comprehension iterable remains in the enclosing scope; each target shadows only the
    filters, later iterables and result expressions evaluated after it.
    """
    found: list[ast.Name | ast.Attribute] = []

    def visit(  # noqa: PLR0911, PLR0912 - lexical AST scopes require distinct traversal rules
        node: ast.AST, shadowed: frozenset[str], *, initial: bool = False
    ) -> None:
        if skip_nested_statements and isinstance(node, ast.stmt) and not initial:
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id not in shadowed:
                found.append(node)
            return
        if isinstance(node, ast.Attribute):
            if _reference_root_name(node) in shadowed:
                return
            if isinstance(node.ctx, ast.Load):
                found.append(node)
            visit(node.value, shadowed)
            return
        if isinstance(node, ast.Lambda):
            defaults = (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None))
            for default in defaults:
                visit(default, shadowed)
            visit(node.body, shadowed | _argument_names(node.args))
            return
        if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp):
            comprehension_shadowed = shadowed
            for generator in node.generators:
                visit(generator.iter, comprehension_shadowed)
                comprehension_shadowed |= _target_names(generator.target)
                for condition in generator.ifs:
                    visit(condition, comprehension_shadowed)
            if isinstance(node, ast.DictComp):
                visit(node.key, comprehension_shadowed)
                visit(node.value, comprehension_shadowed)
            else:
                visit(node.elt, comprehension_shadowed)
            return
        if isinstance(node, ast.match_case):
            visit(node.pattern, shadowed)
            if node.guard is not None:
                visit(node.guard, shadowed | _target_names(node.pattern))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, shadowed)

    visit(root, frozenset(), initial=True)
    return tuple(found)


def _read_spellings(nodes: tuple[ast.expr, ...]) -> frozenset[str]:
    """Return every free name and dotted attribute spelling these expressions read."""
    return frozenset(ast.unparse(node) for root in nodes for node in _free_reference_nodes(root))


def _value_spellings(nodes: list[ast.AST], *, loads_only: bool) -> frozenset[str]:
    """Return the spellings these nodes read as a value in their own right.

    A name reached only as the base of a longer path is excluded: `self.work > cap` reads
    `self.work`, and reaches `self` solely to get there, while `tuple(lexer)` reads `lexer` itself.
    Only the latter is a read that an attribute write on the receiver can feed.

    Args:
        nodes: Expression roots to inspect.
        loads_only: Whether to keep only `Load` occurrences, as a statement's reads require.

    Returns:
        Whole-value spellings, empty when nothing is read that way.
    """
    bases: set[int] = set()
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Attribute | ast.Subscript):
                bases.add(id(node.value))
    found: set[str] = set()
    for root in nodes:
        for node in _free_reference_nodes(root, skip_nested_statements=loads_only):
            if id(node) in bases:
                continue
            found.add(ast.unparse(node))
    return frozenset(found)


def _read_value_spellings(nodes: tuple[ast.expr, ...]) -> frozenset[str]:
    """Return the spellings these expressions read as whole values."""
    return _value_spellings(list(nodes), loads_only=False)


def _statement_value_reads(statement: ast.stmt) -> frozenset[str]:
    """Return the spellings this statement loads as whole values, ignoring nested statements."""
    return _value_spellings([statement], loads_only=True)


def _statement_reads(statement: ast.stmt) -> frozenset[str]:
    """Return the spellings this statement loads, ignoring any nested statement's.

    Only `Load` occurrences count, so a binding target does not read itself and the closure that
    follows a value back to its source cannot be seeded by the name it is being stored into. The
    receiver of `state[key] = value` does load `state`, which is correct: that statement both reads
    and writes it.
    """
    return frozenset(
        ast.unparse(node) for node in _free_reference_nodes(statement, skip_nested_statements=True)
    )


def _mutated_spellings(statement: ast.stmt) -> set[str]:
    """Return the receivers this statement mutates in place, ignoring any nested statement's.

    `active.add(node)` and `effects.append(edge)` write what a guard's condition later reads, and
    neither binds a name, so a rule that recognized only binding statements would leave the
    accumulation that feeds a guard outside its record.
    """
    spellings: set[str] = set()
    for node in _executed_expressions(statement):
        if _called_name(node) not in _MUTATING_METHODS or not isinstance(node, ast.Call):
            continue
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if isinstance(receiver, ast.Name | ast.Attribute | ast.Subscript):
            spellings.add(ast.unparse(receiver))
            if isinstance(receiver, ast.Subscript):
                spellings.add(ast.unparse(receiver.value))
    return spellings


def _write_targets(statement: ast.stmt) -> list[ast.expr]:
    """Return the leaf assignment targets this statement binds, ignoring any nested statement's."""
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
    targets.extend(_scope_named_expression_targets(statement))

    leaves: list[ast.expr] = []
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Tuple | ast.List):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        elif isinstance(target, ast.Name | ast.Attribute | ast.Subscript):
            leaves.append(target)
    return leaves


def _scope_named_expression_targets(statement: ast.stmt) -> list[ast.expr]:
    """Return walrus targets that bind in the statement's surrounding lexical scope.

    A walrus in any comprehension, including a deferred generator body, makes the name local to the
    containing function under PEP 572. A lambda introduces a function scope of its own, so targets
    in its body must not leak into the statement's scope; lambda defaults remain in the surrounding
    lexical scope.
    """
    targets: list[ast.expr] = []

    def visit(node: ast.AST, *, initial: bool = False) -> None:
        if isinstance(node, ast.stmt) and not initial:
            return
        if isinstance(node, ast.Lambda):
            for default in (
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default is not None),
            ):
                visit(default)
            return
        if isinstance(node, ast.NamedExpr):
            targets.append(node.target)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(statement, initial=True)
    return targets


def _imported_spellings(statement: ast.stmt) -> frozenset[str]:
    """Return the names an import statement binds in its surrounding scope."""
    if isinstance(statement, ast.Import):
        return frozenset(alias.asname or alias.name.split(".", 1)[0] for alias in statement.names)
    if isinstance(statement, ast.ImportFrom):
        return frozenset(
            alias.asname or alias.name for alias in statement.names if alias.name != "*"
        )
    return frozenset()


def _written_spellings(statement: ast.stmt) -> frozenset[str]:
    """Return the spellings this statement binds or mutates, ignoring any nested statement's."""
    spellings: set[str] = set(_imported_spellings(statement))
    spellings |= _mutated_spellings(statement)
    for target in _write_targets(statement):
        spellings.add(ast.unparse(target))
        if isinstance(target, ast.Subscript):
            # `state[key] = ...` writes through `state`, which is how the condition reads it.
            spellings.add(ast.unparse(target.value))
    return frozenset(spellings)


def _configured_receivers(statement: ast.stmt) -> frozenset[str]:
    """Return the receivers this statement configures without binding them.

    `lexer.commenters = ""` and `lexer.push_token(...)` both change what `lexer` does while
    binding only `lexer.commenters` or nothing at all, so a statement that reads `lexer` itself
    reads what they wrote. Recording object configuration nowhere is what let the `shlex` lexer be
    reconfigured so an unterminated quote tokenizes cleanly, withdrawing
    `taint.eval-payload.lex-error` with every fingerprint in the tree unchanged.

    These are matched against the spellings a closure reads *as whole values* rather than against
    every spelling it reads. A guard testing `self.work` reaches `self` only as the base of that
    path, and treating an unrelated `self.other = ...` as a write it reads would churn the record
    of every guard in a method on any edit to any attribute of the same object.

    Args:
        statement: The statement to inspect, ignoring any nested statement's expressions.

    Returns:
        Receiver spellings, empty when the statement configures nothing.
    """
    receivers: set[str] = set()
    for target in _write_targets(statement):
        if isinstance(target, ast.Attribute):
            receivers.add(ast.unparse(target.value))
    for node in _executed_expressions(statement):
        if _called_name(node) not in _MUTATING_METHODS or not isinstance(node, ast.Call):
            continue
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if isinstance(receiver, ast.Attribute):
            receivers.add(ast.unparse(receiver.value))
    return frozenset(receivers)


def _condition_writer_shapes(
    origin: ast.stmt,
    scope: ast.AST | None,
    guards: tuple[ast.expr, ...],
    cache: _DerivationCache | None = None,
) -> tuple[str, ...]:
    """Return normalized local and referenced module shapes feeding what governs the guard.

    A guard is disabled as effectively by editing what its condition reads as by editing the
    condition, and neither the qualified name nor the origin statement moves when that happens.
    An enclosing loop's iterable governs the guard the same way, so it seeds the closure alongside
    the tests: emptying `tokens` in `for lexeme in tokens` withdraws every guard in that body.

    The selection is a transitive closure, not one hop. A guard reading a single name is usually
    two or more assignments away from the input that decides it, and stopping at the direct writer
    leaves every statement behind it outside the record.
    `_skip_static_env_option` is the concrete case: `scanner.env-option.static-split-string` tests
    `kind`, written by `kind = _ENV_LONG_OPTION_KINDS[option]`, whose `option` comes from
    `option, attached_value = _resolve_env_long_option(literal)`. Rewriting that call to a constant
    withdraws the guard, and with a one-hop rule the fingerprint does not move.

    The executable scope is deliberately the enclosing function, plus only the module bindings its
    dataflow actually reads. A closure that crossed call boundaries or absorbed unrelated module
    statements would churn frozen records on unrelated edits, and a debt record that churns
    constantly has to be regenerated, which is the laundering path this gate exists to close. A
    caller passing a different value into this scope is outside that boundary and is not covered.

    A nested function or class body is outside the scope for the same reason and by the same rule
    `_reachability_shapes` uses: it does not run where it is written, so a write inside it decides
    nothing about the guard around it, and folding one in would let an edit to an unrelated closure
    churn the guard's record.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        guards: The expressions governing the origin: the tests it sits under, and the iterable of
            every enclosing loop.
        cache: Per-parse derivation memo, defaulting to one used for this call alone.

    Returns:
        Shapes in source order, empty when the guard is unconditional or scope-free.
    """
    cache = cache if cache is not None else _DerivationCache()
    return _writer_closure(
        origin, scope, set(_read_spellings(guards)), set(_read_value_spellings(guards)), cache
    )


def _writer_closure(
    origin: ast.stmt,
    scope: ast.AST | None,
    read: set[str],
    values: set[str],
    cache: _DerivationCache,
) -> tuple[str, ...]:
    """Return shapes of local and referenced module statements that write these spellings.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        read: Seed spellings, extended in place as the fixpoint grows.
        values: The subset read as whole values, extended in place alongside `read`.
        cache: Per-parse derivation memo.

    Returns:
        Module shapes followed by function-local shapes, empty when nothing is read.
    """
    return tuple(
        _cached_shape(statement, cache)
        for statement in _writer_statements(origin, scope, read, values, cache)
    )


def _writer_statements(
    origin: ast.stmt,
    scope: ast.AST | None,
    read: set[str],
    values: set[str],
    cache: _DerivationCache,
) -> tuple[ast.stmt, ...]:
    """Return local and referenced module statements that transitively write these spellings.

    A statement qualifies by binding a spelling the closure reads, or by configuring a receiver the
    closure reads as a whole value. The second is what brings `lexer.commenters = ...` in for an
    operation that reads `lexer`, while keeping every `self.other = ...` out of a closure that
    reaches `self` only through `self.work`.

    Function-local writers are selected first. Names that Python makes local anywhere in the
    function, including parameters, import aliases, handler names and pattern captures, shadow
    module bindings throughout that function and cannot select an unrelated module statement with
    the same spelling. Lambda and comprehension locals are removed from reads only inside their
    expression scope. The remaining free reads seed a second fixpoint over module-scope
    assignments, mutations and imports. Function and class bodies remain boundaries, so following
    a referenced binding never hashes a callee's implementation.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        read: Seed spellings, extended in place as the fixpoint grows.
        values: The subset read as whole values, extended in place alongside `read`.
        cache: Per-parse derivation memo.

    Returns:
        Referenced module statements followed by local statements, each in source order.
    """
    if not read:
        return ()

    local = (
        _select_writer_statements(
            [
                statement
                for statement in _cached_scope_statements(scope, cache)
                if statement is not origin
            ],
            read,
            values,
            cache,
        )
        if scope is not None
        else ()
    )
    shadowed = _local_binding_names(scope)
    module_read = {spelling for spelling in read if _spelling_root(spelling) not in shadowed}
    module_values = {spelling for spelling in values if _spelling_root(spelling) not in shadowed}
    module = cache.module
    module_writers = (
        _select_writer_statements(
            [
                statement
                for statement in _cached_scope_statements(module, cache)
                if statement is not origin
            ],
            module_read,
            module_values,
            cache,
        )
        if module is not None
        else ()
    )
    return (*module_writers, *local)


def _select_writer_statements(
    candidates: list[ast.stmt],
    read: set[str],
    values: set[str],
    cache: _DerivationCache,
) -> tuple[ast.stmt, ...]:
    """Run one transitive writer fixpoint over the supplied lexical scope."""
    selected: dict[int, ast.stmt] = {}
    grew = True
    while grew:
        grew = False
        for statement in candidates:
            if id(statement) in selected:
                continue
            if not (_cached_written_spellings(statement, cache) & read) and not (
                _cached_configured_receivers(statement, cache) & values
            ):
                continue
            selected[id(statement)] = statement
            # What this writer reads becomes part of the dataflow, so the statements feeding *it*
            # are selected on the next pass. The scope bounds the fixpoint.
            read |= _statement_reads(statement)
            values |= _statement_value_reads(statement)
            grew = True
    return tuple(
        sorted(selected.values(), key=lambda statement: (statement.lineno, statement.col_offset))
    )


def _spelling_root(spelling: str) -> str:
    """Return the lexically bound root of a bare or qualified spelling."""
    return spelling.split(".", 1)[0].split("[", 1)[0]


def _local_binding_names(scope: ast.AST | None) -> frozenset[str]:
    """Return names whose lexical binding prevents module lookup in this function."""
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        return frozenset()
    names = set(_argument_names(scope.args))

    global_names: set[str] = set()
    pending: list[ast.AST] = list(scope.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Global):
            global_names.update(node.names)
            continue
        if isinstance(node, ast.Nonlocal):
            names.update(node.names)
            continue
        if isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
        if isinstance(node, ast.match_case):
            names.update(_target_names(node.pattern))
        if isinstance(node, _SCOPES):
            names.add(node.name)
            continue
        if isinstance(node, ast.stmt):
            names.update(_imported_spellings(node))
            names.update(
                target.id for target in _write_targets(node) if isinstance(target, ast.Name)
            )
        pending.extend(ast.iter_child_nodes(node))
    return frozenset(names - global_names)


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


def _diverting_shape(statement: ast.stmt, cache: _DerivationCache) -> str | None:
    """Return the shape of the control this statement exerts, or `None` when it exerts none.

    A branch is reduced to its test and a loop to its target and iterable, so an edit inside one
    branch's body does not churn the record of a guard outside it. Statements that transfer control
    are taken whole, because a `return` decides reachability through the value it returns:
    `return int(digits)` can raise where `return 0` cannot.
    """
    match statement:
        case ast.Return() | ast.Raise() | ast.Break() | ast.Continue():
            return _cached_shape(statement, cache)
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


def _reachability_inputs(
    origin: ast.stmt,
    scope: ast.AST | None,
    cache: _DerivationCache,
) -> tuple[tuple[ast.stmt, ...], tuple[ast.stmt, ...]]:
    """Return the controls deciding whether an origin is reached and their writer closure.

    Keeping this derivation as AST nodes lets both the fingerprint and threshold-provenance gate
    consume the same reachability boundary. A fixed magnitude in an earlier branch decides the
    guard exactly as it would in a lexically enclosing test, and a writer feeding that branch is
    equally part of the decision.

    Args:
        origin: The statement that constructs the refusal.
        scope: Enclosing function, or `None` at class or module level.
        cache: Per-parse derivation memo.

    Returns:
        Diverting statements followed separately by the statements in their transitive writer
        closure, both in source order.
    """
    if scope is None:
        return (), ()
    repeated = _repeated_statements(origin, scope, cache)
    position = (origin.lineno, origin.col_offset)
    statements = [
        statement
        for statement in _cached_scope_statements(scope, cache)
        if statement is not origin
        if (statement.lineno, statement.col_offset) < position or id(statement) in repeated
    ]
    statements.sort(key=lambda statement: (statement.lineno, statement.col_offset))
    controls = tuple(
        statement for statement in statements if _diverting_shape(statement, cache) is not None
    )
    read: set[str] = set()
    values: set[str] = set()
    for statement in controls:
        read |= _statement_reads(statement)
        values |= _statement_value_reads(statement)
    writers = _writer_statements(origin, scope, read, values, cache)
    return controls, writers


def _reachability_shapes(
    origin: ast.stmt, scope: ast.AST | None, cache: _DerivationCache | None = None
) -> tuple[str, ...]:
    """Return the shapes of the control flow that decides whether the origin is reached at all.

    A guard's tests and the writers feeding them describe what it refuses once control arrives.
    They say nothing about whether control arrives, and a guard is withdrawn as completely by
    diverting execution around it as by inverting its condition. Two concrete cases, one for a
    guard with a test and one for a guard without:

    `scanner.env-option.static-split-string` sits under `if kind == "split"`, behind an earlier
    `if not literal.startswith("--")` that returns. Dropping the `not` sends every long option down
    the short-option path, so the guard can no longer fire, while its test, its writers and its
    qualified name are all untouched.

    `scanner.descriptor.unparsable` has no test at all. It fires only because `return int(digits)`
    in its own `try` body can raise `ValueError`, and rewriting that to `return 0` withdraws it.

    Only the statements that can execute before the origin are taken. Lexical order settles that
    everywhere except inside a loop, where a statement after the origin runs again ahead of the
    next iteration, so `_repeated_statements` adds an enclosing loop's whole body back. A statement
    that can only run after the origin cannot decide whether the origin was reached, and excluding
    it is what keeps a later edit in the same function from churning the record. Churn is what
    forces the regeneration this gate exists to prevent.

    The scope is the enclosing function plus the referenced module bindings, the same boundary
    `_condition_writer_shapes` uses and for the same reason. The returned controls include the
    transitive writer closure of every value they read, so replacing a value that a preceding
    branch or loop tests also moves the fingerprint.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        cache: Per-parse derivation memo, defaulting to one used for this call alone.

    Returns:
        Shapes in source order, empty when the origin has no enclosing function.
    """
    cache = cache if cache is not None else _DerivationCache()
    controls, writers = _reachability_inputs(origin, scope, cache)
    return (
        *(
            shape
            for statement in controls
            if (shape := _diverting_shape(statement, cache)) is not None
        ),
        *(_cached_shape(statement, cache) for statement in writers),
    )


def _guarded_bodies(tree: ast.AST) -> dict[int, tuple[ast.stmt, ...]]:
    """Map every node inside an `except` handler to the `try` body that handler guards.

    Descent stops at a nested function or class body, which does not run inside the `try`. Trees
    are visited outermost first, so an inner `try` overwrites its own handlers' entries and the
    nearest guarded body wins.

    Args:
        tree: Parsed module.

    Returns:
        Guarded bodies keyed by node identity, empty for a module with no handler.
    """
    bodies: dict[int, tuple[ast.stmt, ...]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try | ast.TryStar):
            continue
        body = tuple(node.body)
        for handler in node.handlers:
            pending: list[ast.AST] = [handler]
            while pending:
                current = pending.pop()
                bodies[id(current)] = body
                pending.extend(
                    child
                    for child in ast.iter_child_nodes(current)
                    if not isinstance(child, _SCOPES)
                )
    return bodies


def _raising_operation_shapes(
    origin: ast.stmt,
    scope: ast.AST | None,
    guarded: tuple[ast.stmt, ...],
    cache: _DerivationCache,
) -> tuple[str, ...]:
    """Return the shapes of the operation an exception handler guards, and what configures it.

    Whether or not the guard has a nested condition of its own, what decides whether its handler is
    entered is the operation in the `try` body and the object state that operation reads. The
    control-flow closure records a `try` only through its handled exception types, so neither would
    otherwise be in the record. `taint.eval-payload.lex-error` is the concrete case: reconfiguring
    the `shlex` lexer so an unterminated quote tokenizes cleanly withdraws the guard, and rewriting
    the tokenizing call itself withdraws it too.

    The writer closure is the same one a guarded origin gets, seeded from what the guarded body
    reads rather than from what a condition reads, and bounded by the same function-local and
    referenced-module dataflow.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        guarded: Top-level statements of the `try` body the origin's handler guards.
        cache: Per-parse derivation memo.

    Returns:
        The guarded body's shapes followed by its writer closure, empty when the origin is not
        reached through a handler.
    """
    if not guarded:
        return ()
    body: list[ast.stmt] = []
    for statement in guarded:
        body.append(statement)
        body.extend(_scope_statements(statement))
    body = [statement for statement in body if statement is not origin]
    body.sort(key=lambda statement: (statement.lineno, statement.col_offset))
    read: set[str] = set()
    values: set[str] = set()
    for statement in body:
        read |= _statement_reads(statement)
        values |= _statement_value_reads(statement)
    return (
        *(_cached_shape(statement, cache) for statement in body),
        *_writer_closure(origin, scope, read, values, cache),
    )


def _is_guard_refusal_call(node: ast.AST, names: frozenset[str]) -> bool:
    return _called_name(node) in names


def _origin_calls_by_statement(
    tree: ast.AST, names: frozenset[str] | None = None
) -> list[tuple[ast.stmt, ast.Call]]:
    """Pair every guard-origin construction with the statement that owns it."""
    names = names if names is not None else _refusal_constructor_names(tree)
    pairs: list[tuple[ast.stmt, ast.Call]] = []
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt):
            continue
        pairs.extend(
            (statement, node)
            for node in _own_expressions(statement)
            if _is_guard_refusal_call(node, names)
            if isinstance(node, ast.Call)
        )
    return pairs


def _guard_refusal_calls(tree: ast.AST, names: frozenset[str] | None = None) -> list[ast.Call]:
    names = names if names is not None else _refusal_constructor_names(tree)
    return [node for node in ast.walk(tree) if _is_guard_refusal_call(node, names)]  # ty: ignore[invalid-return-type]


def _declared_transport_shapes(
    tree: ast.AST, path: str, qualnames: dict[int, str]
) -> dict[str, str]:
    """Return the normalized shape of every declared transport this module defines.

    A transport does not mint an identifier, so it is never an origin. One of them nonetheless owns
    the condition that decides its callers' refusals: the parameterized cycle detector is handed a
    caller's `GuardRefusal` and tests `node in active` itself. Folding a called transport's shape
    into the caller's fingerprint is what keeps that deciding code inside a record.

    Args:
        tree: Parsed module.
        path: Module file name, used to resolve declared transports.
        qualnames: Enclosing qualified names keyed by node identity, from the module's one
            annotation pass.

    Returns:
        Normalized shapes keyed by the callee name an origin statement would spell.
    """
    declared = {qualname for module, qualname, _ in DECLARED_TRANSPORTS if module == path}
    shapes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if qualnames.get(id(node), node.name) in declared:
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
    transports = _declared_transport_shapes(tree, path, annotations.names)
    guarded_bodies = _guarded_bodies(tree)
    cache = _DerivationCache(module=tree)
    records: list[OriginRecord] = []
    for statement, call in _origin_calls_by_statement(tree):
        if not call.args or not isinstance(call.args[0], ast.Constant):
            raise ValueError(f"{path}: guard identifier must be a string literal")
        origin_id = call.args[0].value
        if not isinstance(origin_id, str):
            raise ValueError(f"{path}: guard identifier must be a string literal")
        if len(call.args) <= REASON_ARGUMENT_INDEX or not _is_string_literal(
            call.args[REASON_ARGUMENT_INDEX]
        ):
            raise ValueError(f"{path}: guard reason must be a string literal")
        qualname = annotations.names.get(id(statement), "")
        condition = annotations.conditions.get(id(statement), "")
        scope = annotations.scopes.get(id(statement))
        tests = annotations.tests.get(id(statement), ())
        loops = annotations.loops.get(id(statement), ())
        # An enclosing loop governs the origin whether or not the origin has a test of its own, so
        # its header and the writers feeding its iterable are recorded either way. For a test-free
        # origin the control-flow closure names the same header a second time, which costs one
        # repeated digest line and nothing else.
        enclosing = tuple(
            shape for loop in loops if (shape := _diverting_shape(loop, cache)) is not None
        )
        governing = (*tests, *(loop.iter for loop in loops))
        writers = _condition_writer_shapes(statement, scope, governing, cache)
        # Every origin records the control flow that decides it is reached at all, since a test
        # describes only what the guard refuses once control arrives. One reached through a handler
        # records in addition the operation whose failure is the only condition it has.
        flow = _reachability_shapes(statement, scope, cache)
        raising = _raising_operation_shapes(
            statement,
            scope,
            guarded_bodies.get(id(statement), ()),
            cache,
        )
        called = {name for node in _own_expressions(statement) if (name := _called_name(node))}
        carried = tuple(shape for name, shape in sorted(transports.items()) if name in called)
        digest = hashlib.sha256(
            "\n".join(
                (
                    qualname,
                    condition,
                    _cached_shape(statement, cache),
                    *writers,
                    *enclosing,
                    *carried,
                    *flow,
                    *raising,
                )
            ).encode("utf-8")
        ).hexdigest()
        records.append(OriginRecord(origin_id, path, qualname, _group_digest(digest)))
    return tuple(sorted(records, key=lambda record: (record.origin_id, record.fingerprint)))


def _is_guard_refusal_shape(node: ast.expr, names: frozenset[str]) -> bool:
    """Return whether this expression directly constructs a guard refusal."""
    return _called_name(node) in names


def _is_verdict_shape(
    node: ast.expr,
    names: frozenset[str],
    guard_free_names: frozenset[str],
) -> bool:
    """Return whether this expression directly constructs a discriminated scan verdict."""
    return _called_name(node) in names | guard_free_names


def _is_raw_text(node: ast.expr) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _literal_id_violations(tree: ast.AST, path: str, names: frozenset[str]) -> list[str]:
    """Return a violation for every origin whose identifier is not a string literal."""
    return [
        f"{path}: {REFUSAL_CONSTRUCTOR} identifier must be a string literal"
        for call in _guard_refusal_calls(tree, names)
        if not call.args
        or not isinstance(call.args[0], ast.Constant)
        or not isinstance(call.args[0].value, str)
    ]


def _literal_reason_violations(tree: ast.AST, path: str, names: frozenset[str]) -> list[str]:
    """Return a violation for every origin whose operator reason is not literal text."""
    return [
        f"{path}: {REFUSAL_CONSTRUCTOR} reason must be a string literal"
        for call in _guard_refusal_calls(tree, names)
        if len(call.args) <= REASON_ARGUMENT_INDEX
        or not _is_string_literal(call.args[REASON_ARGUMENT_INDEX])
    ]


@dataclass(frozen=True, slots=True)
class _ShapeConstructors:
    """Constructor spellings recognized while validating refusal and verdict shapes."""

    refusals: frozenset[str]
    exceptions: frozenset[str]
    results: frozenset[str]
    guard_free_verdicts: frozenset[str]


def _shape_constructors(tree: ast.AST) -> _ShapeConstructors:
    """Resolve every constructor family used by refusal-shape rules and module discovery."""
    return _ShapeConstructors(
        refusals=_refusal_constructor_names(tree),
        exceptions=_constructor_names(tree, REFUSAL_EXCEPTIONS),
        results=_constructor_names(tree, frozenset({RESULT_CONSTRUCTOR})),
        guard_free_verdicts=_constructor_names(tree, GUARD_FREE_VERDICTS),
    )


def _refusal_carrier_violations(
    tree: ast.AST,
    qualnames: dict[int, str],
    path: str,
    constructors: _ShapeConstructors,
) -> list[str]:
    """Return a violation for every refusal carried as text or as an undeclared transport."""
    violations: list[str] = []
    for node in ast.walk(tree):
        called = _called_name(node)
        if called is None or not isinstance(node, ast.Call):
            continue
        if called in constructors.exceptions:
            candidates = [*node.args, *(keyword.value for keyword in node.keywords)]
            refusal_only = True
        elif called in constructors.results:
            candidates = [
                *node.args[1:],
                *(keyword.value for keyword in node.keywords if keyword.arg != "invocations"),
            ]
            refusal_only = False
        else:
            continue
        qualname = qualnames.get(id(node), "")
        for argument in candidates:
            if (
                _is_guard_refusal_shape(argument, constructors.refusals)
                if refusal_only
                else _is_verdict_shape(
                    argument,
                    constructors.refusals,
                    constructors.guard_free_verdicts,
                )
            ):
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


def _verdict_return_violations(
    tree: ast.AST,
    qualnames: dict[int, str],
    path: str,
    constructors: _ShapeConstructors,
) -> list[str]:
    """Return a violation for every non-discriminated verdict return."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, _SCOPES) or node.name not in VERDICT_FUNCTIONS:
            continue
        for statement in _scope_statements(node):
            if not isinstance(statement, ast.Return) or statement.value is None:
                continue
            value = statement.value
            qualname = qualnames.get(id(statement), node.name)
            if node.name == "analyze_marker_taint":
                called = _called_name(value)
                allowed = (
                    called in constructors.refusals | constructors.guard_free_verdicts
                    or (
                        path,
                        qualname,
                        ast.unparse(value),
                    )
                    in DECLARED_TRANSPORTS
                )
            else:
                allowed = _called_name(value) in constructors.results
            if allowed:
                continue
            if isinstance(value, ast.Tuple):
                violations.append(
                    f"{path}:{node.name} returns a tuple verdict; return a discriminated "
                    f"verdict so guard identity survives"
                )
            elif _is_raw_text(value):
                violations.append(
                    f"{path}:{node.name} returns raw refusal text; return a discriminated verdict"
                )
            else:
                violations.append(
                    f"{path}:{node.name} returns an undeclared verdict shape "
                    f"{ast.unparse(value)!r}; construct the discriminated verdict directly"
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
    qualnames = _annotate(tree).names
    constructors = _shape_constructors(tree)
    return (
        *_literal_id_violations(tree, path, constructors.refusals),
        *_literal_reason_violations(tree, path, constructors.refusals),
        *_refusal_carrier_violations(
            tree,
            qualnames,
            path,
            constructors,
        ),
        *_verdict_return_violations(
            tree,
            qualnames,
            path,
            constructors,
        ),
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
"""The exact sites allowed to construct default limits. Descendant scopes are not boundaries;
everywhere else must be handed the scan's limits, so no layer can silently fall back to production
caps under a shrunk budget."""

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
    "_CASE_HEADER_SUBJECT_WORDS": (
        "The number of words needed to identify the subject in a `case` header: the reserved word, "
        "the subject and `in`. A grammatical arity, not a resource budget."
    ),
    "_CASE_HEADER_PATTERN_WORDS": (
        "The number of words a complete `case` header spells before its first pattern: the "
        "reserved word, the subject, `in`, and the pattern itself. A grammatical arity, not a "
        "budget, and reached by authoring a shorter header."
    ),
    "_RANGE_PARTS_WITH_STEP": (
        "The number of parts a brace range spells when it carries a step: start, stop and step. "
        "A grammatical arity, not a budget, and reached by authoring the step."
    ),
    "_DFA_START": (
        "The index of the marker automaton's start state. A position in the state vector rather "
        "than a magnitude, and the same kind of quantity a subscript literal is exempt as."
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


def _defaulted_arguments(scope: ast.AST) -> tuple[tuple[ast.arg, ast.expr], ...]:
    """Return one function's positional and keyword-only arguments that carry defaults."""
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        return ()
    arguments = scope.args
    positional = arguments.posonlyargs + arguments.args
    offset = len(positional) - len(arguments.defaults)
    defaulted = list(zip(positional[offset:], arguments.defaults, strict=True))
    defaulted.extend(
        (argument, default)
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
        if default is not None
    )
    return tuple(defaulted)


def find_limits_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every limits construction or optional limits parameter away from a boundary.

    Direct calls, constructor import aliases and rebindings, and dataclass `default_factory`
    references all construct limits for this purpose.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used to resolve boundaries and in messages.

    Returns:
        Human-readable violations, empty when limits flow from one scan-level value.
    """
    tree = ast.parse(source)
    names = _annotate(tree).names
    limit_names = _constructor_names(tree, LIMITS_CONSTRUCTORS)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct = _called_name(node) in limit_names
        default_factory = next(
            (
                keyword.value
                for keyword in node.keywords
                if _called_name(node) == "field" and keyword.arg == "default_factory"
            ),
            None,
        )
        indirect = default_factory is not None and _referenced_name(default_factory) in limit_names
        if not direct and not indirect:
            continue
        qualname = names.get(id(node), "")
        if (path, qualname) in LIMITS_BOUNDARIES:
            continue
        # A configured construction is as dangerous as a default one: it restores production-scale
        # caps under a shrunk scan budget, which is the failure this rule exists to prevent.
        spelling = "default limits" if indirect or not (node.args or node.keywords) else "limits"
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
        for argument, default in _defaulted_arguments(node):
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
    """Return the module-level bound names, and the subset bound to a numeric magnitude.

    A magnitude is recognized through arithmetic, not only as a bare literal: `_MAX_ITEMS = 50 * 2`
    caps a guard exactly as `_MAX_ITEMS = 100` does, and requiring a bare literal let the computed
    spelling escape both halves of the rule at once, since a module-bound name is also exempt from
    the naming-convention check.
    """
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
        if node.value is not None and _operand_magnitudes(node.value):
            numeric |= names
    return frozenset(bound), frozenset(numeric)


def _scope_import_names(scope: ast.AST | None, cache: _DerivationCache) -> frozenset[str]:
    """Return import-bound spellings in one module or function execution scope."""
    if scope is None:
        return frozenset()
    return frozenset(
        spelling
        for statement in _cached_scope_statements(scope, cache)
        if isinstance(statement, ast.Import | ast.ImportFrom)
        for spelling in _imported_spellings(statement)
    )


def _is_numeric_constant_expression(expression: ast.expr) -> bool:
    """Return whether an expression is composed entirely of numeric literals and arithmetic."""
    if isinstance(expression, ast.Constant):
        return isinstance(expression.value, int | float) and not isinstance(expression.value, bool)
    if isinstance(expression, ast.UnaryOp):
        return _is_numeric_constant_expression(expression.operand)
    if isinstance(expression, ast.BinOp):
        return _is_numeric_constant_expression(expression.left) and _is_numeric_constant_expression(
            expression.right
        )
    return False


def _local_numeric_names(scope: ast.AST | None) -> frozenset[str]:
    """Return the scope's own names bound to a magnitude that is not structural.

    A cap spelled as a local assignment or parameter default has the provenance problem a
    module-level one has: `limit = 100` and `def scan(limit=100)` both create resource bounds with
    nothing recording where they came from, and both are invisible to the module-constant set and
    naming convention. The binding must consist entirely of numeric arithmetic: `index = start + 2`
    is a dynamic cursor offset, not a fixed cap. Zero and one are excluded for the reason
    `STRUCTURAL_GUARD_LITERALS` gives, so a counter seeded at zero is not mistaken for a threshold.

    Args:
        scope: Enclosing function, or `None` at class or module level.

    Returns:
        Local names bound to a magnitude, empty when the origin is scope-free.
    """
    if scope is None:
        return frozenset()
    names: set[str] = set()
    for argument, default in _defaulted_arguments(scope):
        if (
            _is_numeric_constant_expression(default)
            and _operand_magnitudes(default) - STRUCTURAL_GUARD_LITERALS
        ):
            names.add(argument.arg)
    for statement in _scope_statements(scope):
        if isinstance(statement, ast.Assign):
            targets: list[ast.expr] = list(statement.targets)
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        if statement.value is None:
            continue
        if (
            _is_numeric_constant_expression(statement.value)
            and _operand_magnitudes(statement.value) - STRUCTURAL_GUARD_LITERALS
        ):
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(names)


def _threshold_names(
    nodes: tuple[ast.AST, ...], bound: frozenset[str], numeric: frozenset[str]
) -> set[str]:
    """Return every named threshold the guard's condition or origin statement references.

    A module-level numeric constant is a threshold whatever it is called. A conventionally named
    one the module does not define cannot be resolved here, so it is treated as a threshold too;
    a name the module binds to something other than a number is not one.
    """
    call_targets = {
        id(target)
        for root in nodes
        for call in ast.walk(root)
        if isinstance(call, ast.Call)
        for target in ast.walk(call.func)
    }
    return {
        node.id
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Name)
        if id(node) not in call_targets
        if node.id in numeric or (node.id.startswith(_THRESHOLD_PREFIXES) and node.id not in bound)
    }


def _reference_path_scores(
    root: ast.AST,
    *,
    loads_only: bool = False,
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """Return read and whole-value spellings scored from this root.

    Scores carry ``(call nesting, writer hops)``. A root contributes no writer hops; callers add
    those while following assignments. References inside a true ``call.func`` subtree are
    calculation machinery and are excluded.
    """
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    call_targets = {
        id(target)
        for call in ast.walk(root)
        if isinstance(call, ast.Call)
        for target in ast.walk(call.func)
    }
    attribute_bases = {
        id(node.value) for node in ast.walk(root) if isinstance(node, ast.Attribute | ast.Subscript)
    }
    read: dict[str, tuple[int, int]] = {}
    values: dict[str, tuple[int, int]] = {}
    for node in _free_reference_nodes(root, skip_nested_statements=loads_only):
        if id(node) in call_targets:
            continue
        call_nesting = 0
        current: ast.AST = node
        while (parent := parents.get(id(current))) is not None:
            if isinstance(parent, ast.Call):
                call_nesting += 1
            current = parent
        spelling = ast.unparse(node)
        score = (call_nesting, 0)
        previous = read.get(spelling)
        if previous is None or score < previous:
            read[spelling] = score
        if id(node) not in attribute_bases:
            previous = values.get(spelling)
            if previous is None or score < previous:
                values[spelling] = score
    return read, values


def _combined_path_score(
    prefix: tuple[int, int],
    suffix: tuple[int, int],
) -> tuple[int, int]:
    """Return one dependency path followed by another."""
    return (prefix[0] + suffix[0], prefix[1] + suffix[1])


def _import_reference_evidence(
    root: ast.expr,
    imported: frozenset[str],
    prefix: tuple[int, int] = (0, 0),
) -> dict[str, tuple[int, int, int]]:
    """Return imported value references and their provenance distance from one operand.

    The score is call nesting, then writer hops, then total dependency distance. A direct-value
    reference reached through one assignment is stronger threshold evidence than an imported
    argument buried in a call, while equally shallow evidence remains conservative. True
    ``call.func`` nodes and attribute bases are machinery or incomplete spellings rather than
    compared values.
    """
    _, values = _reference_path_scores(root)
    evidence: dict[str, tuple[int, int, int]] = {}
    for spelling, local_score in values.items():
        if _spelling_root(spelling) not in imported:
            continue
        call_nesting, writer_hops = _combined_path_score(prefix, local_score)
        score = (call_nesting, writer_hops, call_nesting + writer_hops)
        previous = evidence.get(spelling)
        if previous is None or score < previous:
            evidence[spelling] = score
    return evidence


def _expression_value_reads(
    expressions: tuple[ast.expr, ...],
) -> tuple[set[str], set[str]]:
    """Return expression data reads, excluding callees used only to measure a value."""
    call_targets = {
        id(target)
        for expression in expressions
        for call in ast.walk(expression)
        if isinstance(call, ast.Call)
        for target in ast.walk(call.func)
    }
    attribute_bases = {
        id(node.value)
        for expression in expressions
        for node in ast.walk(expression)
        if isinstance(node, ast.Attribute | ast.Subscript)
    }
    read: set[str] = set()
    values: set[str] = set()
    for expression in expressions:
        for node in _free_reference_nodes(expression):
            if id(node) not in call_targets:
                spelling = ast.unparse(node)
                read.add(spelling)
                if id(node) not in attribute_bases:
                    values.add(spelling)
    return read, values


def _forwarded_value_roots(statements: tuple[ast.stmt, ...]) -> tuple[ast.expr, ...]:
    """Return assignment values that can forward a compared value through the writer closure."""
    roots: list[ast.expr] = []
    for statement in statements:
        match statement:
            case ast.Assign(value=value) | ast.AugAssign(value=value):
                roots.append(value)
            case ast.AnnAssign(value=ast.expr() as value):
                roots.append(value)
        roots.extend(
            node.value
            for node in _executed_expressions(statement)
            if isinstance(node, ast.NamedExpr)
        )
    return tuple(roots)


def _attribute_path(node: ast.Attribute) -> tuple[str, ...]:
    """Return the dotted components of a statically spelled attribute path."""
    parts = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return tuple(reversed(parts))


def _is_limits_field_operand(operand: ast.expr) -> bool:
    """Return whether this operand itself is an explicit scan-limits field."""
    return isinstance(operand, ast.Attribute) and "limits" in _attribute_path(operand)[:-1]


def _operand_import_evidence(
    origin: ast.stmt,
    scope: ast.AST | None,
    operand: ast.expr,
    imported: frozenset[str],
    cache: _DerivationCache,
) -> dict[str, tuple[int, int, int]]:
    """Return imported evidence feeding one operand, scored by its shortest dependency path."""
    evidence = _import_reference_evidence(operand, imported)
    read_paths, value_paths = _reference_path_scores(operand)
    writers = _writer_statements(origin, scope, set(read_paths), set(value_paths), cache)
    writer_paths: dict[int, tuple[int, int]] = {}
    grew = True
    while grew:
        grew = False
        for statement in writers:
            incoming = [
                read_paths[spelling]
                for spelling in _cached_written_spellings(statement, cache) & read_paths.keys()
            ]
            incoming.extend(
                value_paths[spelling]
                for spelling in _cached_configured_receivers(statement, cache) & value_paths.keys()
            )
            if not incoming:
                continue
            path = min(incoming)
            previous_path = writer_paths.get(id(statement))
            if previous_path is not None and previous_path <= path:
                continue
            writer_paths[id(statement)] = path
            grew = True
            writer_path = (path[0], path[1] + 1)
            for root in _forwarded_value_roots((statement,)):
                for spelling, score in _import_reference_evidence(
                    root, imported, writer_path
                ).items():
                    previous = evidence.get(spelling)
                    if previous is None or score < previous:
                        evidence[spelling] = score
            statement_reads, statement_values = _reference_path_scores(statement, loads_only=True)
            for paths, additions in (
                (read_paths, statement_reads),
                (value_paths, statement_values),
            ):
                for spelling, local_score in additions.items():
                    score = _combined_path_score(writer_path, local_score)
                    previous = paths.get(spelling)
                    if previous is None or score < previous:
                        paths[spelling] = score
    return evidence


def _pair_imported_thresholds(
    origin: ast.stmt,
    scope: ast.AST | None,
    operands: tuple[ast.expr, ast.expr],
    imported: frozenset[str],
    cache: _DerivationCache,
) -> set[str]:
    """Return the strongest imported threshold evidence for one adjacent operand pair.

    An explicit limits field is approved threshold evidence, making the opposite operand measured
    data. Otherwise all imported value paths compete by dependency distance. Equal strongest paths
    remain conservative and are all returned.
    """
    left, right = operands
    if _is_limits_field_operand(left) or _is_limits_field_operand(right):
        return set()
    evidence = _operand_import_evidence(origin, scope, left, imported, cache)
    for spelling, score in _operand_import_evidence(origin, scope, right, imported, cache).items():
        previous = evidence.get(spelling)
        if previous is None or score < previous:
            evidence[spelling] = score
    if not evidence:
        return set()
    strongest = min(evidence.values())
    return {spelling for spelling, score in evidence.items() if score == strongest}


def _imported_thresholds(
    origin: ast.stmt,
    scope: ast.AST | None,
    comparisons: tuple[ast.Compare, ...],
    imported: frozenset[str],
    cache: _DerivationCache,
) -> set[str]:
    """Return strongest imported threshold paths, classifying each adjacent pair independently."""
    thresholds: set[str] = set()
    for comparison in comparisons:
        operands = (comparison.left, *comparison.comparators)
        for left, right in pairwise(operands):
            thresholds.update(
                _pair_imported_thresholds(origin, scope, (left, right), imported, cache)
            )
    return thresholds


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

    A threshold is recognized structurally rather than by naming convention: any module-level or
    function-local numeric constant the guard references, computed as well as literal, any imported
    value used by a relevant comparison directly or through assignment writers, and any bare
    numeric magnitude it compares against. Recognizing only conventionally named module literals
    would let a generically named bound, a computed one, a local one, an imported one, and a raw
    literal each introduce a resource cap with no provenance.

    The search covers the writers feeding the condition as well as the condition itself, plus the
    preceding controls that decide whether the origin is reached and their writers. These are the
    same closures the fingerprint records. A comparison computed one statement earlier caps the
    scan exactly as an inline one does: `too_many = len(items) > 100` followed by `if too_many`
    leaves the magnitude nowhere the condition can see it, so reading the condition alone let any
    new resource bound ship by taking one hop away from the guard.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used in messages.

    Returns:
        Human-readable violations, empty when every guard threshold has declared provenance.
    """
    tree = ast.parse(source)
    annotations = _annotate(tree)
    bound, numeric = _module_constants(tree)
    cache = _DerivationCache(module=tree)
    module_imported = _scope_import_names(tree, cache)
    violations: list[str] = []
    for statement, _call in _origin_calls_by_statement(tree):
        scope = annotations.scopes.get(id(statement))
        local_bindings = _local_binding_names(scope)
        local_imported = _scope_import_names(scope, cache)
        imported = local_imported | (module_imported - local_bindings)
        tests = annotations.tests.get(id(statement), ())
        condition_writers = _writer_statements(
            statement, scope, set(_read_spellings(tests)), set(_read_value_spellings(tests)), cache
        )
        controls, reachability_writers = _reachability_inputs(statement, scope, cache)
        control_expressions = tuple(
            expression for control in controls for expression in _own_expressions(control)
        )
        nodes: tuple[ast.AST, ...] = (
            statement,
            *tests,
            *condition_writers,
            *control_expressions,
            *reachability_writers,
        )
        local = _local_numeric_names(annotations.scopes.get(id(statement)))
        comparisons = tuple(
            {
                id(node): node
                for root in nodes
                for node in ast.walk(root)
                if isinstance(node, ast.Compare)
            }.values()
        )
        imported_thresholds = _imported_thresholds(
            statement,
            scope,
            comparisons,
            imported,
            cache,
        )
        for name in sorted(_threshold_names(nodes, bound, numeric | local) | imported_thresholds):
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


def _uses_guard_protocol(tree: ast.AST) -> bool:
    """Return whether a module constructs or transports refusal/verdict values.

    Discovery cannot depend only on recognizing a canonical refusal construction: malformed
    payloads are exactly what the shape gate must see. Calls to refusal carriers and result
    constructors, including their aliases and rebindings, and definitions of verdict-producing
    functions therefore mark a module as part of the guarded surface too.
    """
    constructors = _shape_constructors(tree)
    carriers = constructors.exceptions | constructors.results
    return bool(
        _guard_refusal_calls(tree, constructors.refusals)
        or any(
            isinstance(node, ast.Call) and _called_name(node) in carriers for node in ast.walk(tree)
        )
        or any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in VERDICT_FUNCTIONS
            for node in ast.walk(tree)
        )
    )


def _repository_guard_modules(root: Path) -> tuple[str, ...]:
    """Return every module below the guard package that participates in the guard protocol.

    This derivation is used by the base-owned closure and comparison. Reading the base checker's
    `GUARDED_MODULES` there would make a candidate module invisible even after the candidate adds it
    to its own tuple, because the protected base copy necessarily predates that edit. Discovering
    carriers and verdict boundaries as well as canonical origins ensures malformed refusal shapes
    reach the candidate-owned shape gate before they can be added to the allowlist.
    """
    modules: list[str] = []
    for path in sorted((root / GUARD_MODULE_ROOT).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _uses_guard_protocol(tree):
            modules.append(path.relative_to(root).as_posix())
    return tuple(modules)


def repository_origin_records(root: Path) -> tuple[OriginRecord, ...]:
    """Return every guard origin record discovered recursively in the guard package.

    Args:
        root: Repository root to read the guarded modules from.

    Returns:
        Records ordered by identifier across all guarded modules.
    """
    records: list[OriginRecord] = []
    for module in _repository_guard_modules(root):
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
    modules = sorted(set(GUARDED_MODULES) | set(_repository_guard_modules(root)))
    for module in modules:
        source = (root / module).read_text(encoding="utf-8")
        violations.extend(find_shape_violations(source, Path(module).name))
    return tuple(violations)


def repository_coverage_violations(root: Path) -> tuple[str, ...]:
    """Return every guard-protocol module omitted from the guarded-module allowlist.

    `GUARDED_MODULES` is the surface the limits and threshold rules read. A module using a refusal
    carrier or producing verdicts belongs to that surface even when its refusal construction is
    malformed and cannot be inventoried. This rule derives the real surface from the package
    instead of trusting the tuple; shape validation consumes that discovered surface directly.

    Args:
        root: Repository root to read the guard package from.

    Returns:
        Human-readable violations, empty when the tuple covers every refusing module.
    """
    guarded = set(GUARDED_MODULES)
    violations: list[str] = []
    for module in _repository_guard_modules(root):
        if module in guarded:
            continue
        violations.append(
            f"{module} uses the guard refusal or verdict protocol but is not in GUARDED_MODULES; "
            "add it so its guards are limits-checked, inventoried and held to the closure "
            "partition"
        )
    return tuple(violations)


def repository_artifact_violations(root: Path) -> tuple[str, ...]:
    """Return a violation when this tree's own inventory artifacts are not canonical.

    The base-owned closure run reads the candidate's debt snapshot and witness registry by identity
    alone, so that a candidate can migrate the record schema or strengthen a witness in one change
    without the base revision's copy failing to decode what it is handed. The full canonical shape
    of both artifacts is therefore this tree's own copy to enforce, which is what this rule does.

    Args:
        root: Repository root holding the artifacts.

    Returns:
        Human-readable violations, empty when both artifacts are canonical.
    """
    violations: list[str] = []
    try:
        load_debt_records(root)
    except ValueError as error:
        violations.append(str(error))
    try:
        classified_origin_ids(root)
    except ValueError as error:
        violations.append(str(error))
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


def registry_origin_ids(source: str) -> frozenset[str]:
    """Return the guard identifiers a registry claims, tolerating a foreign witness shape.

    This is the registry's counterpart to `_decode_identifiers`, and it exists for the same reason:
    the base revision's copy of this checker reads the candidate's registry, so validating each
    entry against this copy's field lists would reject a witness the candidate strengthened with a
    new evidence field, hard-failing the base-owned job with no fix available inside that change.

    Identity is still taken structurally: an entry must name its identifier with the `origin_id`
    keyword or supply it first, and it must be a string literal either way. Everything else about
    the entry is the candidate's own copy to check, in `classified_ids_in_registry`.

    Args:
        source: Registry source text.

    Returns:
        Identifiers carried by a reachable or invariant witness.

    Raises:
        ValueError: If either executable registry is missing, or an entry carries no literal
            identifier.
    """
    tree = ast.parse(source)
    identifiers: set[str] = set()
    for registry_name, registry in _classification_registry_tuples(tree).items():
        for entry in registry.elts:
            if not isinstance(entry, ast.Call):
                raise ValueError(f"{REGISTRY_PATH}: {registry_name} entries must be constructions")
            by_keyword = next(
                (keyword.value for keyword in entry.keywords if keyword.arg == IDENTITY_FIELD),
                None,
            )
            supplied = by_keyword if by_keyword is not None else next(iter(entry.args), None)
            if not isinstance(supplied, ast.Constant) or not isinstance(supplied.value, str):
                raise ValueError(
                    f"{REGISTRY_PATH}: {registry_name} entries must carry a literal "
                    f"{IDENTITY_FIELD}"
                )
            identifiers.add(supplied.value)
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
    # Closure is the one gate the base revision's copy runs against a candidate tree, so both
    # artifacts are read by identity alone. Decoding the candidate's snapshot strictly would make a
    # `SCHEMA_VERSION` bump, and validating its registry entries against this copy's field lists
    # would make a strengthened witness, fail here with no fix available inside the change that
    # introduces it. The candidate's own copy holds both to their full canonical shape in
    # `repository_artifact_violations`.
    classified = registry_origin_ids((root / REGISTRY_PATH).read_text(encoding="utf-8"))
    debt = set(
        _decode_identifiers(json.loads((root / DEBT_PATH).read_text(encoding="utf-8")), DEBT_PATH)
    )
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


def _run_gates(gates: tuple[tuple[str, Callable[[], tuple[str, ...]]], ...]) -> list[str]:
    """Run each gate independently and return every violation the run produced.

    A gate that cannot derive its answer raises, and several of them raise for the same conditions
    another gate reports cleanly. Building the failure list eagerly therefore threw away every
    violation already computed and replaced the operator's report with a traceback naming no gate.
    Each gate's own failure is reported as one more violation instead.

    Args:
        gates: Named gates to run, in report order.

    Returns:
        Human-readable violations across every gate.
    """
    failures: list[str] = []
    for name, gate in gates:
        try:
            failures.extend(gate())
        except ValueError as error:
            failures.append(f"{name}: {error}")
    return failures


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
        # take the release job down with it. The same holds for the canonical shape of the debt
        # snapshot and the witness registry, which this copy reads by identity alone. Every one of
        # those gates runs from the candidate's own copy in the test suite. Closure names no
        # allowlist: it derives the whole partition from the candidate tree, so running it from the
        # base is what makes it unweakenable.
        base_snapshot = arguments.compare_base.read_text(encoding="utf-8")
        base_registry = (
            None
            if arguments.base_registry is None
            else arguments.base_registry.read_text(encoding="utf-8")
        )
        failures = _run_gates(
            (
                ("closure", lambda: repository_closure_violations(arguments.root)),
                (
                    "base comparison",
                    lambda: compare_against_base(
                        arguments.root, base_snapshot, base_registry=base_registry
                    ),
                ),
            )
        )
    else:
        failures = _run_gates(
            (
                ("guarded module coverage", lambda: repository_coverage_violations(arguments.root)),
                ("inventory artifacts", lambda: repository_artifact_violations(arguments.root)),
                ("refusal shapes", lambda: repository_shape_violations(arguments.root)),
                ("limits provenance", lambda: repository_limits_violations(arguments.root)),
                ("guard thresholds", lambda: repository_threshold_violations(arguments.root)),
                ("closure", lambda: repository_closure_violations(arguments.root)),
            )
        )
    for failure in failures:
        sys.stderr.write(f"{failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
