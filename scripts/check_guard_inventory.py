#!/usr/bin/env python3
"""Extract and gate the CI shell scanner's fail-closed guard origins.

Every fail-closed guard below the CI scanner's guard package has one *origin*: the site that
detects the condition and constructs a `GuardRefusal`. This module reads that package as inert
source text, never importing it, and produces one canonical record per origin.

It enforces six separable properties:

1. **Canonical refusal shapes.** A refusal may only reach an exception, a `ShellScanResult`, or a
   verdict return as a `GuardRefusal` construction with a literal identifier and literal reason,
   or as one of the explicitly declared transports. Raw text, executable reason expressions and
   arbitrary verdict expressions are rejected, so a future change cannot bypass construction of
   the discriminated value.
2. **Tree-local closure.** Source origin identifiers must partition exactly into the classified
   inventory and the frozen rollout debt set, with the two disjoint.
3. **Guard reachability.** Every origin must sit in a function some public entry point of its own
   module can reach. Orphaning the function holding a guard withdraws it as completely as
   inverting its condition, and leaves every shape in its record untouched.
4. **Statement reachability.** No statement in a guarded module may sit after a statement that
   leaves its block, because dead code above a guard withdraws it while every record stays frozen.
   The rule is syntactic; a condition that is constantly false is out of scope.
5. **Invariant evidence relevance.** A guard classified as unreachable by an invariant witness must
   carry a boundary-evidence predicate that reads something the guard's own condition reads. Every
   other assertion about such a row holds for a predicate about unrelated data.
6. **Debt monotonicity.** Compared against a base revision, every guard the candidate freezes must
   re-derive to a record the base already carried. Freezing *records* rather than bare identifiers
   means an unclassified guard cannot be moved or semantically edited while keeping its debt entry.

Because the monotonicity check runs from the base revision's copy of this script against the
candidate's source, the candidate is only ever parsed as data. `--compare-base` therefore runs the
closure, reachability and monotonicity checks alone: those derive everything from the candidate
tree, while the refusal-shape, limits and threshold rules read allowlists that describe the source
they shipped with. The candidate's own copy enforces those three, without `--compare-base`, in the
test suite.
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
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SCHEMA_VERSION = 12
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
    # The module that defines the protocol mentions its own constructor in the verdict alias, so
    # mention-based discovery sweeps it in and coverage requires it here. It constructs no refusal,
    # so it contributes no origin record; what it does need is the `ScanLimits` boundary
    # declaration below, since the limits classes are defined here too.
    "src/doc_lattice/github_ci/shell_guards.py",
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

GUARD_PROTOCOL_NAMES = frozenset({REFUSAL_CONSTRUCTOR, RESULT_CONSTRUCTOR}) | REFUSAL_EXCEPTIONS
"""The canonical names whose mention makes a module part of the guarded surface.

These are the constructor families `_shape_constructors` resolves and the shape gate tracks, named
canonically. Module discovery reads them as bare spellings rather than resolving per-module aliases:
an alias is introduced either by an import statement, which names the canonical target, or by a
binding whose value mentions the canonical name, so a mention scan sees both without following
either. The guard-free verdicts are deliberately absent, since a module producing only `Certified`
or `MarkerDetected` transports no guard identity."""

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

_Function = ast.FunctionDef | ast.AsyncFunctionDef

_CONSTRUCTION_HOOKS = frozenset({"__init__", "__post_init__"})
"""Methods a construction of the class runs without any call naming them.

`taint.eval-syntax.cleared-projection-without-widening` lives in a dataclass `__post_init__`, which
no call in either module spells. Reading a construction as an edge to these is what keeps the
reachability rule from reporting it as orphaned."""
"""A function definition, whichever way it is spelled."""

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


def _paired_bindings(target: ast.expr, value: ast.expr) -> list[tuple[str, ast.expr]]:
    """Return the spellings this one binding form binds, paired with the targets they bind to.

    A binding names a constructor by its final component, the same way a call does:
    `GR = shell_guards.GuardRefusal` names it exactly as `GR = GuardRefusal` does. Destructuring
    binds several at once, and `GR, E = GuardRefusal, _TaintLimitExceeded` is one statement that
    names both the refusal constructor and its transport. Reading only the single-value form let
    that spelling escape every rule at once: no origin record, no shape violation, and no discovery
    of the module holding it.

    A starred target collects a list rather than one of the values, so it names no constructor and
    is paired with nothing.

    Args:
        target: The binding's target expression.
        value: The expression bound to it.

    Returns:
        Spelling and target pairs, empty when the form binds no name this module can follow.
    """
    match target, value:
        case (ast.Tuple() | ast.List(), ast.Tuple() | ast.List()):
            if len(target.elts) != len(value.elts):
                return []
            if any(isinstance(element, ast.Starred) for element in target.elts):
                return []
            return [
                pair
                for element, bound in zip(target.elts, value.elts, strict=True)
                for pair in _paired_bindings(element, bound)
            ]
        case (_, ast.Name(id=spelling) | ast.Attribute(attr=spelling)):
            return [(spelling, target)]
        case _:
            return []


def _rebinding_targets(node: ast.AST) -> list[tuple[str, ast.expr]]:
    """Return every constructor spelling a binding statement reads, paired with its target.

    Args:
        node: Any AST node.

    Returns:
        Spelling and target pairs, empty when the node is not a value binding.
    """
    match node:
        case ast.Assign(value=value, targets=targets):
            pass
        case ast.AnnAssign(value=ast.expr() as value, target=target):
            targets = [target]
        case _:
            return []
    return [pair for target in targets for pair in _paired_bindings(target, value)]


def _constructor_names(tree: ast.AST, constructors: frozenset[str]) -> frozenset[str]:
    """Return every local name this module binds to one of these constructors.

    Recognition is by name everywhere in this module, so import aliases, rebindings and parameter
    defaults must participate in every constructor-specific rule. Otherwise an aliased refusal
    can disappear from the inventory, an aliased verdict can be rejected despite being canonical,
    or an aliased limits factory can silently mint a fresh production budget.

    A parameter default is a rebinding the caller may leave in place: `def helper(factory=
    TaintLimits)` binds the constructor to `factory` for every call that omits the argument, so
    `factory()` below the public boundary constructs production-scale limits exactly as a direct
    call would.

    Args:
        tree: Parsed module.
        constructors: Canonical constructor names to follow through imports, rebindings and
            parameter defaults.

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
            rebound = {
                target.id
                for spelling, target in _rebinding_targets(node)
                if spelling in names
                if isinstance(target, ast.Name)
            }
            rebound |= {
                argument.arg
                for argument, default in _defaulted_arguments(node)
                if _referenced_name(default) in names
            }
            if rebound - names:
                names |= rebound
                grew = True
    return frozenset(names)


def _refusal_constructor_names(tree: ast.AST) -> frozenset[str]:
    """Return every local name this module binds to the refusal constructor."""
    return _constructor_names(tree, frozenset({REFUSAL_CONSTRUCTOR}))


_CLASSINFO_PREDICATES = frozenset({"isinstance", "issubclass"})
"""Builtins whose second argument names a class rather than constructing one."""

_CLASSINFO_ARITY = 2
"""The argument count those predicates are spelled with, so the class argument is the second."""


def _bound_value_references(target: ast.expr, value: ast.expr) -> list[ast.expr]:
    """Return the value-side references one binding form registers as constructor aliases.

    `_paired_bindings` owns which forms bind a spelling at all. This mirrors the pairing it
    performs to recover the value nodes it read, so a destructuring statement registers only the
    elements it paired and not everything else the right-hand side happens to hold. Only a bare
    name target registers an alias, exactly as `_constructor_names` records one, so a binding into
    a subscript or an attribute is not a followable context either.

    Args:
        target: The binding's target expression.
        value: The expression bound to it.

    Returns:
        The value-side reference nodes that register an alias, empty when none do.
    """
    if not _paired_bindings(target, value):
        return []
    match target, value:
        case (ast.Tuple() | ast.List(), ast.Tuple() | ast.List()):
            return [
                node
                for element, bound in zip(target.elts, value.elts, strict=True)
                for node in _bound_value_references(element, bound)
            ]
        case (ast.Name(), _):
            return [value]
        case _:
            return []


def _call_reference_contexts(call: ast.Call) -> Iterator[ast.AST]:
    """Yield every node one call names a constructor in without hiding it.

    A callee is the direct construction every constructor rule already reads, but only while
    `_called_name` resolves it: a bare name or an attribute-qualified one. Any other callee
    expression hides the constructor in call position exactly as a binding would,
    so `(TaintLimits if use_default else injected)()` constructs production limits that no
    construction rule reports, and its references are left for this module's rule to reject.

    A `field` default factory is the indirect construction the limits rule reads, and the class
    argument of a membership predicate names a type without constructing anything.

    Args:
        call: The call to read.

    Yields:
        The nodes of that call's followable contexts.
    """
    if isinstance(call.func, ast.Name | ast.Attribute):
        yield from ast.walk(call.func)
    if _called_name(call) == "field":
        for keyword in call.keywords:
            if keyword.arg == "default_factory":
                yield keyword.value
    if (
        isinstance(call.func, ast.Name)
        and call.func.id in _CLASSINFO_PREDICATES
        and len(call.args) == _CLASSINFO_ARITY
    ):
        yield from ast.walk(call.args[1])


_CONSTRUCTOR_HIDING_NODES = (
    ast.Call,
    ast.Dict,
    ast.Set,
    ast.List,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
"""Expression forms that hide a constructor wherever they are spelled, type position included.

A container subscripted for its element, `{"g": GuardRefusal}["g"]`, names the constructor where no
rule reads it and hands it back on demand, and a call or lambda computes the reference instead of
naming it. A `type` statement's value is exempt from the reference rule only while its subtree holds
none of these, so the exemption covers a chain of type references and not everything the grammar
allows in that position."""


def _hides_constructor(declared: ast.expr) -> bool:
    """Return whether this type expression spells a form that hides a constructor reference.

    Args:
        declared: The declared type expression.

    Returns:
        Whether any node in it is a container, call, lambda or comprehension.
    """
    return any(isinstance(node, _CONSTRUCTOR_HIDING_NODES) for node in ast.walk(declared))


def _declaration_contexts(declared: ast.expr) -> Iterator[ast.AST]:
    """Yield every node of one type declaration except the references an inline assignment binds.

    A declaration names a constructor as a type, which binds nothing a later call can reach. An
    assignment expression inside one does bind it, and the alias follower never reads a walrus, so
    `def _helper(x: (factory := TaintLimits) = None)` would register `factory` where no rule looks
    and `factory()` would then mint production limits invisibly.

    Args:
        declared: The declared type expression.

    Yields:
        The nodes of that declaration that hide no constructor.
    """
    bound = {
        id(inner)
        for node in ast.walk(declared)
        if isinstance(node, ast.NamedExpr)
        for part in (node.target, node.value)
        for inner in ast.walk(part)
    }
    yield from (node for node in ast.walk(declared) if id(node) not in bound)


def _declared_reference_contexts(node: ast.AST) -> Iterator[ast.AST]:
    """Yield every node one statement or declaration names a constructor in without hiding it.

    An exception type, an annotation and a match pattern's class name a constructor as a type
    rather than binding it to a name that could later be called.

    A `type` statement is a type position too, and the only one that is a statement rather than an
    annotation. The interpreter evaluates its value lazily inside its own scope and binds a
    `TypeAliasType`, which is not callable, so a constructor named there is reachable as a
    constructor only through the same dunder indirection an ordinary registered alias already
    permits (`Alias.__call__(...)` reads no tracked spelling either). Only a value spelling no
    constructor-hiding form is exempt, per `_hides_constructor`: `type X = {"g": GuardRefusal}["g"]`
    hides the constructor in a type position exactly as it does outside one, and `X.__value__` hands
    the element straight back. A `TypeAlias`-annotated assignment is deliberately *not* a type
    position at all: the annotation is unenforced, so
    `factory: TypeAlias = GuardRefusal if use_default else injected` really does bind a callable.

    A raise is not a type position, so only the bare name or attribute it raises is allowed here.
    Everything else inside `exc` and `cause` is left to the ordinary per-node rules, which is what
    makes an unresolvable callee unfollowable in a raise exactly as it is in an expression:
    `raise (GuardRefusal if use_default else injected)("taint.demo.hidden", "nope")` constructs the
    refusal through a spelling no rule reads, and walking the raise subtree certified it.

    Args:
        node: Any AST node.

    Yields:
        The nodes of that node's followable declaration contexts.
    """
    if isinstance(node, ast.ExceptHandler) and node.type is not None:
        yield from _declaration_contexts(node.type)
    if isinstance(node, ast.TypeAlias) and not _hides_constructor(node.value):
        yield from _declaration_contexts(node.value)
    if isinstance(node, ast.Raise):
        for part in (node.exc, node.cause):
            if isinstance(part, ast.Name | ast.Attribute):
                yield part
    if isinstance(node, ast.MatchClass):
        yield from _declaration_contexts(node.cls)
    for attribute in ("annotation", "returns"):
        declared = getattr(node, attribute, None)
        if isinstance(declared, ast.expr):
            yield from _declaration_contexts(declared)


def _binding_reference_contexts(node: ast.AST) -> Iterator[ast.AST]:
    """Yield every node one binding registers as a constructor alias.

    These are the references the alias follower already reads, so they are followed rather than
    rejected: `_constructor_names` grows the bound name and every constructor rule then applies to
    it. A binding form it does not read registers nothing and yields nothing.

    Args:
        node: Any AST node.

    Yields:
        The value-side reference nodes this binding registers.
    """
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _bound_value_references(target, node.value)
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        yield from _bound_value_references(node.target, node.value)
    for _argument, default in _defaulted_arguments(node):
        if _referenced_name(default) is not None:
            yield default


def _followable_reference_contexts(tree: ast.AST) -> set[int]:
    """Return the node identities a constructor reference may occupy without hiding a constructor.

    Every context is pinned by a spelling the guarded modules already carry, and each is enumerated
    by one of `_call_reference_contexts`, `_declared_reference_contexts` and
    `_binding_reference_contexts`.

    Args:
        tree: Parsed module.

    Returns:
        The `id` of every node inside a followable context.
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            allowed.update(id(inner) for inner in _call_reference_contexts(node))
        allowed.update(id(inner) for inner in _declared_reference_contexts(node))
        allowed.update(id(inner) for inner in _binding_reference_contexts(node))
    return allowed


def _unanalyzable_constructor_references(
    tree: ast.Module, constructors: frozenset[str], path: str
) -> tuple[str, ...]:
    """Return a violation for every tracked constructor reference the inventory cannot follow.

    The rule is deny-by-default. `_followable_reference_contexts` enumerates the contexts a
    constructor may be named in, and a reference outside them is rejected rather than ignored, so a
    spelling nobody enumerated fails loudly instead of hiding a constructor. Teaching the alias
    follower one new spelling at a time let every other one through:
    `factory = TaintLimits if use_default else injected` binds the constructor where no rule looks,
    and `factory()` then mints production limits invisibly.

    Constructors are tracked by the transitively aliased name set, so a reference to an already
    registered alias in an unfollowable position is caught the same way the canonical name is. Only
    a load reads the constructor; the store side of a binding is the registration itself.

    A rejection is resolved by binding the constructor plainly, calling it directly, or spelling it
    in one of the enumerated contexts, never by widening those contexts for a spelling being
    introduced.

    Args:
        tree: Parsed module.
        constructors: Constructor spellings to track, including the aliases already followed.
        path: Module file name, used in messages.

    Returns:
        Human-readable violations, empty when every reference sits in a followable context.
    """
    allowed = _followable_reference_contexts(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name | ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        name = _referenced_name(node)
        if name not in constructors or id(node) in allowed:
            continue
        violations.append(
            f"{path}:{node.lineno}: {name} is referenced in a form the inventory cannot follow; "
            f"bind it plainly, call it directly, or use a declared context"
        )
    return tuple(violations)


_WriterDerivation = tuple[tuple[ast.stmt, ...], tuple[str, ...], frozenset[str], frozenset[str]]
"""One writer closure: the statements it selected, the shapes of the parameter defaults it reads,
and the read and whole-value spellings the fixpoint grew to."""


@dataclass(frozen=True, slots=True)
class _DerivationCache:
    """Per-parse memo for the derivations repeated across every origin in one module.

    The writer closure re-derives the same statement's written spellings and normalized shape once
    per fixpoint pass and once per origin sharing the scope, which dominates the gate's runtime.
    Keying by node identity is sound because one cache lives no longer than the parse it was built
    for, so an identity cannot be reused by a later node.

    The threshold gate runs the whole writer fixpoint once per comparison operand, so the closure
    itself and the two scope-wide traversals it depends on are memoized as well. Every one is a
    pure function of nodes this parse owns.
    """

    shapes: dict[int, str] = field(default_factory=dict)
    written: dict[int, frozenset[str]] = field(default_factory=dict)
    configured: dict[int, frozenset[str]] = field(default_factory=dict)
    statements: dict[int, list[ast.stmt]] = field(default_factory=dict)
    parents: dict[int, dict[int, ast.AST]] = field(default_factory=dict)
    bindings: dict[int, frozenset[str]] = field(default_factory=dict)
    paths: dict[tuple[int, bool], tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]] = (
        field(default_factory=dict)
    )
    closures: dict[tuple[int, int, frozenset[str], frozenset[str]], _WriterDerivation] = field(
        default_factory=dict
    )
    functions: dict[int, dict[str, _Function]] = field(default_factory=dict)
    scope_definitions: dict[int, dict[str, _Function]] = field(default_factory=dict)
    qualnames: dict[int, str] = field(default_factory=dict)
    callee_shapes: dict[int, tuple[str, ...]] = field(default_factory=dict)
    callee_callees: dict[int, tuple[_Function, ...]] = field(default_factory=dict)
    module_parents: dict[int, dict[int, ast.AST]] = field(default_factory=dict)
    definitions: dict[int, dict[str, tuple[_Function, ...]]] = field(default_factory=dict)
    calls: dict[int, dict[str, tuple[tuple[ast.stmt, ast.Call], ...]]] = field(default_factory=dict)
    reachability: dict[tuple[int, int], tuple[str, ...]] = field(default_factory=dict)
    reads: dict[int, frozenset[str]] = field(default_factory=dict)
    value_reads: dict[int, frozenset[str]] = field(default_factory=dict)
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
            iterable read, the defaults bound to the parameters that dataflow reads, the headers of
            those enclosing loops, the shape of any declared transport the origin statement hands
            its refusal to, the control flow that decides whether it is reached and the writers
            feeding that flow, any `try` body whose handler contains the origin, and the
            return-deciding statements of every module-level callee whose value that dataflow
            reads, and the controls at every resolvable call site of the guard's own function.
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


def _cached_local_binding_names(scope: ast.AST | None, cache: _DerivationCache) -> frozenset[str]:
    """Return the scope's lexically bound names, walking the scope at most once per parse."""
    if scope is None:
        return frozenset()
    names = cache.bindings.get(id(scope))
    if names is None:
        names = _local_binding_names(scope)
        cache.bindings[id(scope)] = names
    return names


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


def _consumed_generators(statement: ast.stmt) -> set[int]:
    """Return the identities of generator expressions this statement iterates as it builds them.

    A generator handed straight to a call, to a `for` header or to a comprehension is driven to
    exhaustion by that same statement, so its body runs there. One merely bound to a name is not.
    """
    consumed: set[int] = set()
    for node in (statement, *_own_expressions(statement)):
        match node:
            case ast.Call(args=iterables):
                pass
            case (
                ast.For(iter=iterable)
                | ast.AsyncFor(iter=iterable)
                | ast.comprehension(iter=iterable)
            ):
                iterables = [iterable]
            case _:
                continue
        consumed.update(id(item) for item in iterables if isinstance(item, ast.GeneratorExp))
    return consumed


def _executed_expressions(statement: ast.stmt) -> list[ast.AST]:
    """Return expressions executed by this statement in its surrounding scope.

    Constructing a lambda executes its defaults but defers its body to a different execution
    scope. List, set and dictionary comprehensions execute immediately. Constructing a generator
    expression evaluates only its first iterable, unless the same statement consumes it: the body
    of `any(seen.add(word) for word in words)` runs there, and skipping it let the accumulation
    feeding a guard be deleted with every fingerprint unchanged. A generator that is only bound
    stays deferred, with its body, filters and later iterables outside this traversal. This
    traversal follows runtime side effects only. Compile-time bindings such as a walrus in a
    deferred generator body use a separate lexical traversal.
    """
    executed: list[ast.AST] = []
    consumed = _consumed_generators(statement)
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
        if isinstance(node, ast.GeneratorExp) and id(node) not in consumed:
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

    def visit(  # noqa: PLR0912 - lexical AST scopes require distinct traversal rules
        node: ast.AST, shadowed: frozenset[str], *, initial: bool = False
    ) -> None:
        if skip_nested_statements and isinstance(node, ast.stmt) and not initial:
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id not in shadowed:
                found.append(node)
            return
        if isinstance(node, ast.Attribute):
            # A shadowed lexical root disqualifies the path itself, not what a subscript on that
            # path reads: `word.parts[index].text` resolves `index` in the enclosing scope even
            # when `word` is a comprehension target, and dropping it would let the statement that
            # writes `index` fall out of the guard's closure.
            if _reference_root_name(node) not in shadowed and isinstance(node.ctx, ast.Load):
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


def _cached_statement_reads(statement: ast.stmt, cache: _DerivationCache) -> frozenset[str]:
    """Return the statement's loaded spellings, walking the statement at most once per parse."""
    memo = cache.reads.get(id(statement))
    if memo is None:
        memo = _statement_reads(statement)
        cache.reads[id(statement)] = memo
    return memo


def _cached_statement_value_reads(statement: ast.stmt, cache: _DerivationCache) -> frozenset[str]:
    """Return the statement's whole-value reads, walking the statement at most once per parse."""
    memo = cache.value_reads.get(id(statement))
    if memo is None:
        memo = _statement_value_reads(statement)
        cache.value_reads[id(statement)] = memo
    return memo


def _mutated_spellings(statement: ast.stmt) -> set[str]:
    """Return the receivers this statement mutates in place, ignoring any nested statement's.

    `active.add(node)` and `effects.append(edge)` write what a guard's condition later reads, and
    neither binds a name, so a rule that recognized only binding statements would leave the
    accumulation that feeds a guard outside its record.

    A mutation the statement drives counts wherever it is spelled. `any(seen.add(word) for word
    in words)` runs the accumulation as the statement exhausts the generator, so
    `_executed_expressions` follows a consumed generator's body.
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
    cache: _DerivationCache,
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

    The executable scope of this fixpoint is deliberately the enclosing function, plus only the
    module bindings its dataflow actually reads. A closure that absorbed unrelated module
    statements, or a callee's whole body, would churn frozen records on unrelated edits, and a debt
    record that churns constantly has to be regenerated, which is the laundering path this gate
    exists to close. What a selected writer's call *computes* is covered separately and narrowly by
    `_callee_shapes`, which follows a module-level callee's return value into its own returns. A
    caller passing a different value down into this scope is outside the boundary and is not
    covered.

    A nested function or class body is outside the scope for the same reason and by the same rule
    `_reachability_shapes` uses: it does not run where it is written, so a write inside it decides
    nothing about the guard around it, and folding one in would let an edit to an unrelated closure
    churn the guard's record.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        guards: The expressions governing the origin: the tests it sits under, and the iterable of
            every enclosing loop.
        cache: Per-parse derivation memo. Required, because the module it carries is what makes
            the referenced module bindings part of the closure: a cache without one derives a
            different fingerprint from the one the snapshot froze.

    Returns:
        Shapes in source order, empty when the guard is unconditional or scope-free.
    """
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
        Module shapes, then function-local shapes, then parameter-default shapes; empty when
        nothing is read.
    """
    statements, defaults, grown_read, grown_values = _writer_derivation(
        origin, scope, read, values, cache
    )
    read |= grown_read
    values |= grown_values
    return (*(_writer_shape(statement, read, cache) for statement in statements), *defaults)


def _writer_shape(statement: ast.stmt, read: set[str], cache: _DerivationCache) -> str:
    """Return the shape recording what one selected writer contributes to this closure.

    An import statement binds every alias it names, but a guard reads only some of them. Hashing
    the whole statement would make a guard's record depend on the shape of a shared
    `from ... import (...)` line, so adding one unrelated alias to it churns the frozen record of
    every guard reading any other name on that line. Mass regeneration is the laundering path this
    gate exists to close, so an import contributes only its source module and the aliases this
    closure actually reads.

    Args:
        statement: A statement the writer fixpoint selected.
        read: The spellings the closure reads, after the fixpoint has grown them.
        cache: Per-parse derivation memo.

    Returns:
        The normalized shape to hash for this writer.
    """
    if not isinstance(statement, ast.Import | ast.ImportFrom):
        return _cached_shape(statement, cache)
    bound = _imported_spellings(statement) & read
    plain = isinstance(statement, ast.Import)
    source = "import" if plain else f"from {'.' * statement.level}{statement.module or ''} import"
    entries = sorted(
        f"{alias.name} as {alias.asname}" if alias.asname else alias.name
        for alias in statement.names
        if (alias.asname or (alias.name.split(".", 1)[0] if plain else alias.name)) in bound
    )
    return f"{source} {entries}"


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
    assignments, mutations and imports. Function and class bodies remain boundaries of this
    fixpoint, so following a referenced binding never hashes a callee's implementation here;
    `_callee_shapes` covers what a selected call returns.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        read: Seed spellings, extended in place as the fixpoint grows.
        values: The subset read as whole values, extended in place alongside `read`.
        cache: Per-parse derivation memo.

    Returns:
        Referenced module statements followed by local statements, each in source order.
    """
    statements, _defaults, grown_read, grown_values = _writer_derivation(
        origin, scope, read, values, cache
    )
    read |= grown_read
    values |= grown_values
    return statements


def _writer_derivation(
    origin: ast.stmt,
    scope: ast.AST | None,
    read: set[str],
    values: set[str],
    cache: _DerivationCache,
) -> _WriterDerivation:
    """Return the writer closure and the spellings it grew to, memoized per seed.

    The threshold gate asks for this closure once per comparison operand and once per writer
    reached from one, so the same seed reaches the same scope repeatedly. The result depends on
    nothing but the origin, the scope, the module the cache carries and the seed spellings, all of
    which this parse owns.

    A statement is not the only thing that binds a name the guard reads. A parameter default binds
    one too, for every call that omits the argument, and no statement in the scope records it: the
    signature sits outside the body the local fixpoint walks. `charge_work(self, amount: int = 1)`
    is the concrete case, where `amount: int = 0` stops the zero-argument callers charging anything
    and withdraws `taint.eval-discovery.work-limit` with the closure unchanged. The defaults the
    dataflow reads are therefore recorded alongside the writers, and what they read seeds the
    module fixpoint: a default is evaluated in the defining scope, so it resolves module bindings
    rather than the function locals that shadow them inside the body.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        read: Seed spellings, left unmodified.
        values: The subset read as whole values, left unmodified.
        cache: Per-parse derivation memo.

    Returns:
        The selected statements, the parameter-default shapes, the grown read spellings and the
        grown whole-value spellings.
    """
    key = (id(origin), id(scope), frozenset(read), frozenset(values))
    memo = cache.closures.get(key)
    if memo is not None:
        return memo
    if not read:
        result: _WriterDerivation = ((), (), frozenset(read), frozenset(values))
        cache.closures[key] = result
        return result

    grown_read = set(read)
    grown_values = set(values)
    local = (
        _select_writer_statements(
            [
                statement
                for statement in _cached_scope_statements(scope, cache)
                if statement is not origin
            ],
            grown_read,
            grown_values,
            cache,
        )
        if scope is not None
        else ()
    )
    defaults = _read_parameter_defaults(scope, grown_read)
    default_shapes = tuple(
        f"default {name}={ast.dump(default, include_attributes=False)}"
        for name, default in defaults
    )
    default_expressions = tuple(default for _name, default in defaults)
    shadowed = _cached_local_binding_names(scope, cache)
    module_read = {
        spelling for spelling in grown_read if _spelling_root(spelling) not in shadowed
    } | set(_read_spellings(default_expressions))
    module_values = {
        spelling for spelling in grown_values if _spelling_root(spelling) not in shadowed
    } | set(_read_value_spellings(default_expressions))
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
    grown_read |= module_read
    grown_values |= module_values
    result = (
        (*module_writers, *local),
        default_shapes,
        frozenset(grown_read),
        frozenset(grown_values),
    )
    cache.closures[key] = result
    return result


def _read_parameter_defaults(
    scope: ast.AST | None, read: set[str]
) -> tuple[tuple[str, ast.expr], ...]:
    """Return the scope's defaulted parameters this dataflow reads, in signature order.

    A parameter is matched by the lexical root of a read spelling, so a guard reaching
    `context.frames` records the default bound to `context`.

    Args:
        scope: Enclosing function, or `None` at class or module level.
        read: The spellings the dataflow reads.

    Returns:
        Parameter name and default expression pairs, empty when none is read.
    """
    if scope is None:
        return ()
    roots = {_spelling_root(spelling) for spelling in read}
    return tuple(
        (argument.arg, default)
        for argument, default in _defaulted_arguments(scope)
        if argument.arg in roots
    )


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
            read |= _cached_statement_reads(statement, cache)
            values |= _cached_statement_value_reads(statement, cache)
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
) -> tuple[tuple[ast.stmt, ...], tuple[ast.stmt, ...], set[str]]:
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
        closure, both in source order, and the spellings that closure reads.
    """
    if scope is None:
        return (), (), set()
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
        read |= _cached_statement_reads(statement, cache)
        values |= _cached_statement_value_reads(statement, cache)
    writers = _writer_statements(origin, scope, read, values, cache)
    return controls, writers, read


def _reachability_shapes(
    origin: ast.stmt, scope: ast.AST | None, cache: _DerivationCache
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
        cache: Per-parse derivation memo. Required for the reason
            `_condition_writer_shapes` gives: the module it carries is part of the closure.

    Returns:
        Shapes in source order, empty when the origin has no enclosing function.
    """
    memo = cache.reachability.get((id(origin), id(scope)))
    if memo is not None:
        return memo
    controls, writers, read = _reachability_inputs(origin, scope, cache)
    shapes = (
        *(
            shape
            for statement in controls
            if (shape := _diverting_shape(statement, cache)) is not None
        ),
        *(_writer_shape(statement, read, cache) for statement in writers),
    )
    cache.reachability[(id(origin), id(scope))] = shapes
    return shapes


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
    body = _guarded_body_statements(origin, guarded)
    if not body:
        return ()
    read, values = _statement_dataflow(body, cache)
    return (
        *(_cached_shape(statement, cache) for statement in body),
        *_writer_closure(origin, scope, read, values, cache),
    )


def _guarded_body_statements(
    origin: ast.stmt, guarded: tuple[ast.stmt, ...]
) -> tuple[ast.stmt, ...]:
    """Return every statement of the `try` body an origin's handler guards, in source order.

    Args:
        origin: The statement that constructs the refusal, excluded from its own guarded body.
        guarded: Top-level statements of that body.

    Returns:
        The body's statements, empty when the origin is not reached through a handler.
    """
    if not guarded:
        return ()
    body: list[ast.stmt] = []
    for statement in guarded:
        body.append(statement)
        body.extend(_scope_statements(statement))
    return tuple(
        sorted(
            (statement for statement in body if statement is not origin),
            key=lambda statement: (statement.lineno, statement.col_offset),
        )
    )


def _statement_dataflow(
    statements: tuple[ast.stmt, ...], cache: _DerivationCache
) -> tuple[set[str], set[str]]:
    """Return the spellings these statements read, and the subset they read as whole values."""
    read: set[str] = set()
    values: set[str] = set()
    for statement in statements:
        read |= _cached_statement_reads(statement, cache)
        values |= _cached_statement_value_reads(statement, cache)
    return read, values


def _cached_module_functions(cache: _DerivationCache) -> dict[str, _Function]:
    """Return the module's top-level functions by name, memoized for this parse.

    A later definition rebinds an earlier one exactly as any other assignment would, so the last
    `def` of a name is the one a call reaches. Nested definitions are absent because they are not
    module bindings; `_resolve_called_definition` reaches them through the lexical chain of the
    scope a call is spelled in, and consults this map only when no enclosing function binds the
    name.

    Args:
        cache: Per-parse derivation memo carrying the module.

    Returns:
        Module-level functions by bound name, empty when the cache carries no module.
    """
    module = cache.module
    if module is None:
        return {}
    memo = cache.functions.get(id(module))
    if memo is None:
        memo = {
            statement.name: statement
            for statement in module.body
            if isinstance(statement, _SCOPES) and not isinstance(statement, ast.ClassDef)
        }
        cache.functions[id(module)] = memo
    return memo


def _cached_scope_definitions(scope: ast.AST, cache: _DerivationCache) -> dict[str, _Function]:
    """Return the functions this scope binds by a `def`, keyed by the name each binds.

    A nested definition binds a name in the scope holding it, at whatever statement depth it is
    written, and a later `def` of the same name rebinds it exactly as an assignment would.

    Args:
        scope: The function or module whose own bindings to collect.
        cache: Per-parse derivation memo.

    Returns:
        Definitions by bound name, empty when the scope defines no function.
    """
    memo = cache.scope_definitions.get(id(scope))
    if memo is not None:
        return memo
    found: dict[str, _Function] = {}
    pending: list[ast.AST] = [scope]
    while pending:
        node = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    previous = found.get(child.name)
                    if previous is None or previous.lineno < child.lineno:
                        found[child.name] = child
                continue
            pending.append(child)
    cache.scope_definitions[id(scope)] = found
    return found


def _resolve_called_definition(
    name: str, scope: ast.AST | None, cache: _DerivationCache
) -> _Function | None:
    """Return the definition a bare-name call spelled in this scope reaches, if this parse can tell.

    Resolution follows Python's lexical chain. The nearest enclosing scope that binds the name
    decides: a `def` of it resolves, and any other binding, such as a parameter, assignment, import
    alias, handler name or match capture, shadows the name with a value this parse cannot follow.
    Only when no enclosing function binds it at all does the module-level function apply.

    A class body binds nothing for a method written inside it, so the chain skips one.

    Args:
        name: The callee name spelled bare.
        scope: The function the call is spelled in, or `None` for a module-scope expression.
        cache: Per-parse derivation memo carrying the module.

    Returns:
        The definition reached, or `None` when the name resolves to a value instead or to nothing.
    """
    parents = _cached_module_parents(cache) if scope is not None else {}
    current = scope
    while isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
        definition = _cached_scope_definitions(current, cache).get(name)
        if definition is not None:
            return definition
        if name in _cached_local_binding_names(current, cache):
            return None
        current = _enclosing_node(current, (ast.FunctionDef, ast.AsyncFunctionDef), parents)
    return _cached_module_functions(cache).get(name)


def _called_definitions(
    scoped: tuple[ast.AST, ...],
    free: tuple[ast.AST, ...],
    scope: ast.AST | None,
    cache: _DerivationCache,
) -> tuple[_Function, ...]:
    """Return the function definitions this dataflow calls by a name this parse can resolve.

    Only a bare `Name` callee is followed. An attribute call names a member of a value this parse
    cannot resolve, and reading `obj.run()` as the module's `run` would hash an unrelated function
    into the record.

    A nested definition is followed on the same footing as a module-level one.
    `_contextualize_evidence` reaches its guards through fifty lexically nested helpers, and frozen
    origins read what `positional_call_arguments` returns; excluding nested definitions let that
    return be pinned to an empty tuple with every fingerprint in the module byte-identical.

    Args:
        scoped: Nodes spelled inside the enclosing function, whose lexical chain resolves the names
            they spell.
        free: Nodes evaluated in module scope, which no local binding shadows.
        scope: Enclosing function, or `None` at class or module level.
        cache: Per-parse derivation memo.

    Returns:
        Definitions in source order, empty when this dataflow reaches none.
    """
    reached: dict[int, _Function] = {}
    for roots, spelled_in in ((scoped, scope), (free, None)):
        for root in roots:
            for node in ast.walk(root):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                definition = _resolve_called_definition(node.func.id, spelled_in, cache)
                if definition is not None:
                    reached[id(definition)] = definition
    return tuple(
        sorted(reached.values(), key=lambda function: (function.lineno, function.col_offset))
    )


def _callee_return_statements(
    function: _Function, cache: _DerivationCache
) -> tuple[ast.Return, ...]:
    """Return the value-bearing `return` statements this function executes itself, in source order.

    A bare `return` yields nothing the caller can read. It still decides which of the other returns
    runs, and it reaches the record that way, as a diverting statement in their reachability
    closure.

    Args:
        function: The callee to inspect, excluding any function or class body nested in it.
        cache: Per-parse derivation memo.

    Returns:
        Returns in source order, empty when the function has none carrying a value.
    """
    return tuple(
        sorted(
            (
                statement
                for statement in _cached_scope_statements(function, cache)
                if isinstance(statement, ast.Return) and statement.value is not None
            ),
            key=lambda statement: (statement.lineno, statement.col_offset),
        )
    )


def _cached_callee_shapes(function: _Function, cache: _DerivationCache) -> tuple[str, ...]:
    """Return the shapes recording what decides this callee's return value, memoized per parse.

    Each of the callee's returns is recorded the way a guard origin is: the return statement's own
    shape, the transitive closure of the function-local and referenced-module statements that write
    what it reads, the defaults bound to the parameters that dataflow reads, and the control flow
    deciding that this return is the one reached. Everything else in the callee is machinery. A
    branch whose body writes nothing a return reads changes no caller's value, and folding the
    whole body in would couple a frozen record to edits that cannot withdraw its guard.

    Args:
        function: The callee whose return value the guard's dataflow reads.
        cache: Per-parse derivation memo.

    Returns:
        The callee's qualified name followed by its return-deciding shapes.
    """
    memo = cache.callee_shapes.get(id(function))
    if memo is not None:
        return memo
    shapes: list[str] = [f"callee {_definition_qualname(function, cache)}"]
    for statement in _callee_return_statements(function, cache):
        read, values = _statement_dataflow((statement,), cache)
        shapes.append(_cached_shape(statement, cache))
        shapes.extend(_writer_closure(statement, function, read, values, cache))
        shapes.extend(_reachability_shapes(statement, function, cache))
    result = tuple(shapes)
    cache.callee_shapes[id(function)] = result
    return result


def _definition_qualname(function: _Function, cache: _DerivationCache) -> str:
    """Return the dotted name of the scopes this definition is written in, ending in its own.

    Callee blocks are ordered by this name, and two nested helpers can share a bare name, so the
    bare name would make the order depend on which the walk happened to reach first. It is derived
    from the enclosing definitions rather than from a line number, so moving a helper within its
    scope moves no record.

    Args:
        function: The definition to name.
        cache: Per-parse derivation memo carrying the module.

    Returns:
        The qualified name, which is the bare name for a module-level definition.
    """
    memo = cache.qualnames.get(id(function))
    if memo is not None:
        return memo
    parents = _cached_module_parents(cache)
    parts = [function.name]
    current = _enclosing_node(function, _SCOPES, parents)
    while isinstance(current, _SCOPES):
        parts.append(current.name)
        current = _enclosing_node(current, _SCOPES, parents)
    qualname = ".".join(reversed(parts))
    cache.qualnames[id(function)] = qualname
    return qualname


def _cached_callee_callees(function: _Function, cache: _DerivationCache) -> tuple[_Function, ...]:
    """Return the functions this callee's own return dataflow reads the value of.

    The nodes searched are exactly the ones `_cached_callee_shapes` hashes, so the closure follows
    a call only where an edit to what it returns would move a shape already in the record.

    Args:
        function: The callee to expand.
        cache: Per-parse derivation memo.

    Returns:
        Definitions, empty when this callee's returns read no resolvable function's value.
    """
    memo = cache.callee_callees.get(id(function))
    if memo is not None:
        return memo
    scoped: list[ast.AST] = []
    free: list[ast.AST] = []
    for statement in _callee_return_statements(function, cache):
        scoped.append(statement)
        read, values = _statement_dataflow((statement,), cache)
        _split_by_scope(
            _writer_statements(statement, function, read, values, cache),
            function,
            cache,
            scoped,
            free,
        )
        free.extend(default for _name, default in _read_parameter_defaults(function, read))
        controls, writers, _flow_read = _reachability_inputs(statement, function, cache)
        scoped.extend(controls)
        _split_by_scope(writers, function, cache, scoped, free)
    result = _called_definitions(tuple(scoped), tuple(free), function, cache)
    cache.callee_callees[id(function)] = result
    return result


def _split_by_scope(
    statements: tuple[ast.stmt, ...],
    scope: ast.AST | None,
    cache: _DerivationCache,
    scoped: list[ast.AST],
    free: list[ast.AST],
) -> None:
    """Sort selected writers into the ones spelled inside the scope and the module-scope ones.

    The writer closure returns both in one sequence, and which lexical scope a statement was
    spelled in is what decides whether the scope's bindings shadow a callee name it spells.

    Args:
        statements: Statements the writer closure selected.
        scope: Enclosing function, or `None` at class or module level.
        cache: Per-parse derivation memo.
        scoped: Collector for the statements spelled inside the scope, extended in place.
        free: Collector for the module-scope statements, extended in place.
    """
    inside = (
        {id(statement) for statement in _cached_scope_statements(scope, cache)}
        if scope is not None
        else set()
    )
    for statement in statements:
        (scoped if id(statement) in inside else free).append(statement)


def _callee_shapes(
    origin: ast.stmt,
    scope: ast.AST | None,
    guarded: tuple[ast.stmt, ...],
    governing: tuple[ast.expr, ...],
    cache: _DerivationCache,
) -> tuple[str, ...]:
    """Return the shapes of the module-level callees whose return values decide this guard.

    A record covers the statements that write what a guard reads, but a writer such as
    `option, attached_value = _resolve_env_long_option(literal)` hashes the call spelling and
    nothing the call computes. Rewriting that callee's return to a constant withdraws
    `scanner.env-option.static-split-string` while leaving every fingerprint in the tree unchanged,
    and the base-owned comparison accepts it. A callee whose value the guard's condition, its
    reachability controls, its guarded operation or the parameter defaults feeding any of them read
    is therefore part of the record.

    The closure follows return values only, transitively, and takes each callee's return-deciding
    statements rather than its body. A helper is coupled to a guard because the guard reads what
    the helper computes, not because the guard's function happens to call it, and what is hashed of
    that helper is only what can change the value the guard reads. The bound is on which edits
    churn a record rather than on how many records a helper is tied to: fifty origins read what
    `choice` returns, so an edit to what it returns moves seventeen frozen records and should, while
    a statement no return reads moves none. A record that churns on an unrelated edit has to be
    regenerated, which is the laundering path this gate exists to close.

    Two boundaries remain, both for want of a resolvable target rather than by preference: a call
    through an attribute, and a value the caller passes down rather than reads back.

    Args:
        origin: The statement that constructs the refusal, hashed separately.
        scope: Enclosing function, or `None` at class or module level.
        guarded: Top-level statements of the `try` body the origin's handler guards, if any.
        governing: The expressions governing the origin: the tests it sits under, and the iterable
            of every enclosing loop.
        cache: Per-parse derivation memo.

    Returns:
        One block per reached callee, ordered by name, empty when the guard reads no callee's
        return value.
    """
    scoped: list[ast.AST] = list(governing)
    free: list[ast.AST] = []

    read = set(_read_spellings(governing))
    values = set(_read_value_spellings(governing))
    _split_by_scope(
        _writer_statements(origin, scope, read, values, cache), scope, cache, scoped, free
    )
    free.extend(default for _name, default in _read_parameter_defaults(scope, read))

    controls, writers, flow_read = _reachability_inputs(origin, scope, cache)
    scoped.extend(controls)
    _split_by_scope(writers, scope, cache, scoped, free)
    free.extend(default for _name, default in _read_parameter_defaults(scope, flow_read))

    body = _guarded_body_statements(origin, guarded)
    if body:
        scoped.extend(body)
        raised_read, raised_values = _statement_dataflow(body, cache)
        _split_by_scope(
            _writer_statements(origin, scope, raised_read, raised_values, cache),
            scope,
            cache,
            scoped,
            free,
        )
        free.extend(default for _name, default in _read_parameter_defaults(scope, raised_read))

    reached: dict[int, _Function] = {}
    pending = list(_called_definitions(tuple(scoped), tuple(free), scope, cache))
    while pending:
        function = pending.pop()
        if id(function) in reached:
            continue
        reached[id(function)] = function
        pending.extend(_cached_callee_callees(function, cache))
    return tuple(
        shape
        for function in sorted(reached.values(), key=lambda node: _definition_qualname(node, cache))
        for shape in _cached_callee_shapes(function, cache)
    )


def _cached_module_parents(cache: _DerivationCache) -> dict[int, ast.AST]:
    """Return every node in the module mapped to the node holding it, memoized for this parse.

    `_cached_scope_parents` deliberately stops at a nested scope, which is what the writer and
    reachability closures need. Resolving a call site needs the opposite: the class or function a
    definition is written in, across the whole module.

    Args:
        cache: Per-parse derivation memo carrying the module.

    Returns:
        Holding nodes keyed by node identity, empty when the cache carries no module.
    """
    module = cache.module
    if module is None:
        return {}
    memo = cache.module_parents.get(id(module))
    if memo is None:
        memo = {
            id(child): node for node in ast.walk(module) for child in ast.iter_child_nodes(node)
        }
        cache.module_parents[id(module)] = memo
    return memo


def _enclosing_node(
    node: ast.AST, kinds: tuple[type[ast.AST], ...], parents: dict[int, ast.AST]
) -> ast.AST | None:
    """Return the nearest node of one of these kinds holding this node, or `None` if there is none.

    Args:
        node: Node to walk outwards from.
        kinds: Node types to stop at.
        parents: Module-wide parent map.

    Returns:
        The nearest holding node of one of these kinds, or `None`.
    """
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, kinds):
            return current
        current = parents.get(id(current))
    return None


def _cached_definitions_by_name(cache: _DerivationCache) -> dict[str, tuple[_Function, ...]]:
    """Return every function the module defines at any depth, keyed by the name it binds.

    `_cached_module_functions` answers a different question: which module-level binding a bare-name
    call reaches. Call-site resolution and the reachability graph both need every definition,
    including methods and nested functions, and both need to know when a name is ambiguous.

    Args:
        cache: Per-parse derivation memo carrying the module.

    Returns:
        Definitions in source order keyed by name, empty when the cache carries no module.
    """
    module = cache.module
    if module is None:
        return {}
    memo = cache.definitions.get(id(module))
    if memo is None:
        collected: dict[str, list[_Function]] = {}
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                collected.setdefault(node.name, []).append(node)
        memo = {
            name: tuple(sorted(defs, key=lambda node: (node.lineno, node.col_offset)))
            for name, defs in collected.items()
        }
        cache.definitions[id(module)] = memo
    return memo


def _cached_calls_by_name(
    cache: _DerivationCache,
) -> dict[str, tuple[tuple[ast.stmt, ast.Call], ...]]:
    """Return every call in the module keyed by the callee name it spells, memoized for this parse.

    Resolving one function's call sites is a question about the whole module, and there are as many
    origins as there are guards. Grouping the module's calls once keeps that from being a walk per
    origin.

    Args:
        cache: Per-parse derivation memo carrying the module.

    Returns:
        Statement and call pairs keyed by spelled callee name, empty when the cache carries no
        module.
    """
    module = cache.module
    if module is None:
        return {}
    memo = cache.calls.get(id(module))
    if memo is None:
        collected: dict[str, list[tuple[ast.stmt, ast.Call]]] = {}
        for statement in ast.walk(module):
            if not isinstance(statement, ast.stmt):
                continue
            for node in _own_expressions(statement):
                if not isinstance(node, ast.Call):
                    continue
                name = _resolved_callee_name(node)
                if name is not None:
                    collected.setdefault(name, []).append((statement, node))
        memo = {name: tuple(pairs) for name, pairs in collected.items()}
        cache.calls[id(module)] = memo
    return memo


def _resolved_callee_name(call: ast.Call) -> str | None:
    """Return the name a call spells for its callee, whether bare or through an attribute."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_call_site_of(call: ast.Call, function: _Function, cache: _DerivationCache) -> bool:
    """Return whether this call resolves, unambiguously, to that function definition.

    Three spellings resolve, and nothing else does. A module-level function is reached by a bare
    name. A function nested in another is reached by a bare name too, and only from inside the
    scope that binds it. A method is reached by `self.name` from inside its own class, and by any
    receiver at all when exactly one function in the module carries that name, because there is
    then no other definition the attribute could denote.

    A receiver this parse cannot resolve, spelling a name two definitions share, stays unresolved.
    Guessing there would hash an unrelated call into a guard's record, and the reachability graph
    covers those spellings separately by name alone, where over-approximating is the safe
    direction.

    Args:
        call: The call to classify.
        function: The definition the call may reach.
        cache: Per-parse derivation memo carrying the module.

    Returns:
        Whether the call reaches that definition.
    """
    name = _resolved_callee_name(call)
    if name != function.name:
        return False
    parents = _cached_module_parents(cache)
    owner = _enclosing_node(
        function, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef), parents
    )
    if isinstance(call.func, ast.Name):
        return _bare_name_reaches(call, function, owner, parents)
    if not isinstance(owner, ast.ClassDef):
        return False
    # With exactly one definition of the name in the module, there is no other one the attribute
    # could denote whatever the receiver is. Otherwise only `self` inside the class resolves.
    if len(_cached_definitions_by_name(cache).get(name, ())) == 1:
        return True
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    return (
        isinstance(receiver, ast.Name)
        and receiver.id == "self"
        and _enclosing_node(call, (ast.ClassDef,), parents) is owner
    )


def _is_unresolved_call_site_of(
    call: ast.Call, function: _Function, cache: _DerivationCache
) -> bool:
    """Return whether this call may reach that definition without the parse being able to tell.

    An attribute call whose name two or more definitions in the module share is the one spelling
    `_is_call_site_of` has to decline: the receiver decides which definition runs, and resolving it
    would need type information this parse does not have. Recording nothing about those calls
    certified a withdrawal. With a frozen guard `A.check`, a benign `B.check` and an entry point
    calling both, deleting only `a.check()` left the guard's record byte-identical, while the
    remaining `b.check()` kept the reachability graph, which resolves by name alone, reporting
    `A.check` as reached.

    So a call that might reach the definition is recorded as one that might, in its own block. The
    cost is real and bounded: an edit to a same-named call that does not reach this guard moves its
    record. It applies only where the module gives one name to more than one definition, and
    renaming either definition removes the coupling entirely, which is why over-approximating here
    is preferred to certifying a withdrawal.

    Args:
        call: The call to classify.
        function: The definition the call may reach.
        cache: Per-parse derivation memo carrying the module.

    Returns:
        Whether the call is an unresolvable candidate for that definition.
    """
    if not isinstance(call.func, ast.Attribute) or call.func.attr != function.name:
        return False
    # The name always resolves to at least this definition, so one carrier means no ambiguity and
    # `_is_call_site_of` has already resolved the call through any receiver.
    if len(_cached_definitions_by_name(cache).get(function.name, ())) == 1:
        return False
    return not _is_call_site_of(call, function, cache)


def _bare_name_reaches(
    call: ast.Call, function: _Function, owner: ast.AST | None, parents: dict[int, ast.AST]
) -> bool:
    """Return whether a bare-name call reaches this definition.

    A bare name reaches a method only through a binding this parse does not model, and a nested
    definition only from inside the scope that binds it or from the definition itself.

    Args:
        call: The bare-name call to classify.
        function: The definition the call may reach.
        owner: The class or function the definition is written in, or `None` at module level.
        parents: Module-wide parent map.

    Returns:
        Whether the call reaches that definition.
    """
    if isinstance(owner, ast.ClassDef):
        return False
    if owner is None:
        return True
    spelled_in = _enclosing_node(call, (ast.FunctionDef, ast.AsyncFunctionDef), parents)
    return spelled_in is owner or spelled_in is function


def _call_site_shapes(
    scope: ast.AST | None,
    annotations: _Annotations,
    cache: _DerivationCache,
) -> tuple[str, ...]:
    """Return the shapes of the controls deciding that the guard's own function is called.

    A record covers what a guard refuses once control arrives in its function, and the flow inside
    that function deciding it is reached. Neither says anything about whether the function itself
    is called. `_finish_case` holds `scanner.control-flow.unfinished-case` and has exactly one call
    site; replacing that call with `return` withdraws the guard completely while every shape in the
    record stays byte-identical.

    Each resolvable call site contributes the caller's qualified name, the condition the call sits
    under, the call statement's own shape and the flow diverting around it, which is the same
    treatment the origin statement gets one level down. Inverting `if frame is not None` at the call
    site, deleting the call, or returning ahead of it therefore all move the record.

    The closure is one level deep by construction. Following callers transitively would pull the
    reachability closure of the public entry point into every record in both modules, since all
    paths converge there, and a record that churns on an unrelated edit has to be regenerated,
    which is the laundering path this gate exists to close. Withdrawal further up is covered
    instead by `find_reachability_violations`, which needs no digest.

    A function with no resolvable call site records that fact rather than nothing, so acquiring one
    moves the record too.

    An attribute call to a name more than one definition in the module carries resolves to none of
    them, and `_is_unresolved_call_site_of` gives the reason those are recorded as candidates rather
    than dropped.

    Args:
        scope: The origin's enclosing function, or `None` at class or module level.
        annotations: The module's one annotation pass.
        cache: Per-parse derivation memo carrying the module.

    Returns:
        The resolvable call sites' blocks, in source order, followed by the unresolvable
        candidates', each headed by its resolution outcome.
    """
    module = cache.module
    if module is None or not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        return ("call sites: no enclosing function",)
    sites: list[ast.stmt] = []
    candidates: list[ast.stmt] = []
    for statement, call in _cached_calls_by_name(cache).get(scope.name, ()):
        if _is_call_site_of(call, scope, cache):
            sites.append(statement)
        elif _is_unresolved_call_site_of(call, scope, cache):
            candidates.append(statement)

    def controls(statements: list[ast.stmt], heading: str) -> list[str]:
        ordered = sorted(dict.fromkeys(statements), key=lambda node: (node.lineno, node.col_offset))
        shapes = [heading.format(count=len(ordered))]
        for statement in ordered:
            caller = annotations.scopes.get(id(statement))
            shapes.append(annotations.names.get(id(statement), ""))
            shapes.append(annotations.conditions.get(id(statement), ""))
            shapes.append(_cached_shape(statement, cache))
            shapes.extend(_reachability_shapes(statement, caller, cache))
        return shapes

    resolved = (
        controls(sites, f"call sites: {{count}} for {scope.name}")
        if sites
        else [f"call sites: none resolvable for {scope.name}"]
    )
    # A function with no ambiguous candidate records nothing about them, so the many records with
    # no name collision at all are unaffected, and acquiring a collision moves the record.
    unresolved = (
        controls(candidates, f"unresolved call sites: {{count}} for {scope.name}")
        if candidates
        else []
    )
    return (*resolved, *unresolved)


def _module_entry_points(module: ast.Module) -> tuple[_Function, ...]:
    """Return the module-level functions a caller outside the module can name.

    This is derived from the candidate tree rather than read from an allowlist, which is what lets
    the reachability gate run from the base revision's copy of this checker: an allowlist there
    would describe the base's source and reject a legitimate rename with no fix available inside
    the same change.

    Making a withdrawn guard's function public would satisfy this rule, and is not a way through:
    the rename moves the record's qualified name, which the base-relative comparison reports as new
    debt.

    Args:
        module: Parsed module.

    Returns:
        Public module-level functions, in source order.
    """
    return tuple(
        statement
        for statement in module.body
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
        if not statement.name.startswith("_")
    )


def _reachable_functions(module: ast.Module, cache: _DerivationCache) -> set[int]:
    """Return the identities of the functions reachable from the module's entry points.

    Edges are resolved by callee name alone, across every definition carrying that name, and a
    construction of a class is an edge to the hooks that construction runs. Over-approximating is
    the safe direction here: this rule reports a guard that nothing can reach, so counting an edge
    that a more precise analysis would drop only ever withholds a report. The precise resolution
    `_is_call_site_of` performs is the one that feeds a fingerprint, where a wrong edge would
    instead couple a record to unrelated code.

    A function that is only ever referenced, never called, is not an edge. That direction is
    deliberate too: it can raise a report for a callback-dispatched guard, which is visible and
    answerable, rather than certifying one silently.

    Args:
        module: Parsed module.
        cache: Per-parse derivation memo carrying the module.

    Returns:
        Identities of reachable function definitions.
    """
    definitions = _cached_definitions_by_name(cache)
    classes = {node.name: node for node in ast.walk(module) if isinstance(node, ast.ClassDef)}
    owners: dict[int, ast.AST | None] = {}

    def assign(node: ast.AST, current: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            owners[id(child)] = current
            assign(
                child,
                child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current,
            )

    assign(module, None)
    edges: dict[int, set[int]] = {}
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        name = _resolved_callee_name(node)
        if name is None:
            continue
        owner = owners.get(id(node))
        targets = edges.setdefault(id(owner) if owner is not None else 0, set())
        targets.update(id(definition) for definition in definitions.get(name, ()))
        constructed = classes.get(name)
        if constructed is not None:
            targets.update(
                id(hook)
                for hook in constructed.body
                if isinstance(hook, ast.FunctionDef | ast.AsyncFunctionDef)
                if hook.name in _CONSTRUCTION_HOOKS
            )
    # The module body runs on import, so a function it calls is entered exactly as one an entry
    # point calls is.
    pending = [0, *(id(function) for function in _module_entry_points(module))]
    reached: set[int] = set()
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(edges.get(current, ()))
    return reached


# Each of these is a pure function of the module source it is handed, and the repository-level
# rules derive the same unchanged sources many times over in one process. Memoizing them keeps a
# gate run, and a test suite that exercises these rules repeatedly, from re-parsing and
# re-fingerprinting both guarded modules for every call.
@cache
def find_reachability_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every guard origin whose own function nothing in the module can reach.

    A guard is withdrawn as completely by orphaning the function holding it as by inverting its
    condition, and orphaning it leaves every shape in its record untouched. `_call_site_shapes`
    covers the immediate call site; this rule covers the rest of the chain, at any depth, and needs
    no digest to do it, so it costs a frozen record nothing in churn.

    Args:
        source: Module source text, parsed but never executed.
        path: Name reported with each violation.

    Returns:
        Human-readable violations, empty when every origin's function is reachable.
    """
    tree = ast.parse(source)
    annotations = _annotate(tree)
    cache = _DerivationCache(module=tree)
    reachable = _reachable_functions(tree, cache)
    violations: list[str] = []
    for statement, call in _origin_calls_by_statement(tree):
        if not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        origin_id = call.args[0].value
        if not isinstance(origin_id, str):
            continue
        scope = annotations.scopes.get(id(statement))
        if scope is None or id(scope) in reachable:
            continue
        violations.append(
            f"{path}: {origin_id} ({annotations.names.get(id(statement), '')}) sits in a function "
            f"no public entry point of this module reaches, so the guard cannot fire; restore the "
            f"call that reaches it or retire the guard"
        )
    return tuple(violations)


_TERMINAL_STATEMENTS = (ast.Return, ast.Raise, ast.Continue, ast.Break)
"""Statements that leave their own block unconditionally, so nothing after them in it can run."""


def _statement_blocks(tree: ast.AST) -> Iterator[tuple[ast.stmt, ...]]:
    """Yield every statement list in the module, whichever construct owns it.

    Reading the lists structurally rather than naming the constructs that carry them is what keeps
    the dead-statement rule from having a blind spot per construct: a module body, a function body,
    each arm of an `if` chain, a loop body and its `else`, a `try` body, its handlers, its `else`
    and its `finally`, a `with` body and a `match` case body are all just a field holding
    statements. A field holding anything else, such as a call's arguments or a module's type
    ignores, is not a block and is skipped.

    Args:
        tree: Parsed module.

    Returns:
        Every block of statements in the module, in walk order.
    """
    for node in ast.walk(tree):
        for _, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            statements = tuple(item for item in value if isinstance(item, ast.stmt))
            if statements and len(statements) == len(value):
                yield statements


def find_dead_statement_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every statement a guarded module cannot reach, block by block.

    Dead code above a guard withdraws it as completely as inverting its condition, and it moves no
    fingerprint: an unconditional `return None` at the top of the scan method makes every scanner
    guard dead while all of the origins persist, every record stays byte-identical and every other
    gate passes. The crude form of that edit is visible in the syntax alone, as a terminal statement
    with more statements after it in the same block, and this rule rejects it there.

    The rule is syntactic and evaluates no condition. `if True: return` above the rest of a function
    makes that rest dead without any statement following a terminal one in the same block, and
    deciding it needs constant folding this parse deliberately does not do. That conditional form is
    the AD-20 residual: it is bounded by the frozen-debt window, because a classified guard's
    witness executes the guard and turns the same edit into a failing test, and the dynamic control
    for the window is the recurring checkpoint-corpus differential of issue #182.

    Polarity kin: like the magnitude, constructor-reference and relevance rules, this one names what
    it can decide and reports what it finds rather than enumerating known bypasses. Its scope is the
    crude form of withdrawal by dead code; the targeted conditional form stays with classification
    and the corpus differential.

    Args:
        source: Module source text, parsed but never executed.
        path: Name reported with each violation.

    Returns:
        Human-readable violations, empty when every statement in the module is reachable.
    """
    violations: list[str] = []
    for block in _statement_blocks(ast.parse(source)):
        for index, statement in enumerate(block):
            if not isinstance(statement, _TERMINAL_STATEMENTS):
                continue
            violations.extend(
                f"{path}:{dead.lineno}: this statement is unreachable, because line "
                f"{statement.lineno} leaves the block first; every statement in a guarded module "
                f"must be reachable, since dead code above a guard withdraws it while its record "
                f"stays frozen"
                for dead in block[index + 1 :]
            )
            break
    return tuple(violations)


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


# Each of these is a pure function of the module source it is handed, and the repository-level
# rules derive the same unchanged sources many times over in one process. Memoizing them keeps a
# gate run, and a test suite that exercises these rules repeatedly, from re-parsing and
# re-fingerprinting both guarded modules for every call.
@cache
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
        # A writer hashes the spelling of a call, never what the call computes, so a callee whose
        # return value this guard reads is followed into its own returns.
        callees = _callee_shapes(
            statement, scope, guarded_bodies.get(id(statement), ()), governing, cache
        )
        # Everything above describes the guard's own function. None of it moves when the call that
        # reaches that function is withdrawn, so the controls at the immediate call site are part
        # of the record too.
        callers = _call_site_shapes(scope, annotations, cache)
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
                    *callees,
                    *callers,
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


# Each of these is a pure function of the module source it is handed, and the repository-level
# rules derive the same unchanged sources many times over in one process. Memoizing them keeps a
# gate run, and a test suite that exercises these rules repeatedly, from re-parsing and
# re-fingerprinting both guarded modules for every call.
@cache
def find_shape_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every non-canonical refusal shape in this module source.

    A refusal, transport or result constructor named in a form the alias follower cannot read is
    reported here too: see `_unanalyzable_constructor_references`.

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
        *_unanalyzable_constructor_references(
            tree,
            constructors.refusals | constructors.exceptions | constructors.results,
            path,
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
        # `ScanLimits` is the scan-level value itself, and its two fields are where the per-layer
        # defaults are declared. They are constructed only when `ScanLimits` is, which the entries
        # above confine to the boundaries; a shrunk cap reaches every layer by passing the value
        # those boundaries built rather than by editing this declaration. Like every entry here the
        # pin is name-shaped rather than node-shaped, so it exempts anything else in that module
        # whose qualified name is `ScanLimits` as well: a second definition shadowing this one is
        # caught by review of the declaration, not by the gate.
        ("shell_guards.py", "ScanLimits"),
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
    """Return one callable's positional and keyword-only arguments that carry defaults.

    A lambda binds a default exactly as a `def` does, so a constructor alias handed to one is
    followed the same way. Only a `def` is a scope for the rules that ask which names a function
    binds, and those hand this one a `def` or nothing.
    """
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
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


# Each of these is a pure function of the module source it is handed, and the repository-level
# rules derive the same unchanged sources many times over in one process. Memoizing them keeps a
# gate run, and a test suite that exercises these rules repeatedly, from re-parsing and
# re-fingerprinting both guarded modules for every call.
@cache
def find_limits_violations(source: str, path: str) -> tuple[str, ...]:
    """Return every limits construction or optional limits parameter away from a boundary.

    Direct calls, constructor import aliases and rebindings, and dataclass `default_factory`
    references all construct limits for this purpose. A limits constructor named in a form the
    alias follower cannot read is reported here too: see `_unanalyzable_constructor_references`.

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

    violations.extend(_unanalyzable_constructor_references(tree, limit_names, path))

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

_LITERAL_BENIGN_CALLEES: dict[str, frozenset[int]] = {
    "int": frozenset({1}),
    "_read_ansi_c_digits": frozenset({4}),
    "_read_ansi_c_prefixed_escape": frozenset({3, 4}),
}
"""Positional argument indexes that state a numeric base or digit width, not a resource budget.

`int(text, 16)` names the radix the escape is spelled in, and the ANSI-C escape readers take that
radix and the digit count the shell grammar fixes. Each index is pinned by a guarded-module call
site, so `int` is benign only at the base and never at the value it converts: `int(100)` is the
same cap as `100`. Every other callee is excluded deliberately, because `max(100, floor)` floors a
value at 100 and `range(1000)` bounds an iteration.
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
        if node.value is not None and _binding_magnitudes(node.value):
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


def _expression_parents(expression: ast.expr) -> dict[int, ast.AST]:
    """Return a child-to-parent map over one expression tree.

    Args:
        expression: The bound expression to map. It stays referenced for the map's lifetime, so
            the node identities the keys record cannot be reused by another object.

    Returns:
        Parent node keyed by `id()` of each descendant, empty for a leaf expression.
    """
    return {
        id(child): node for node in ast.walk(expression) for child in ast.iter_child_nodes(node)
    }


def _is_arity_membership(container: ast.Set | ast.Tuple, parents: dict[int, ast.AST]) -> bool:
    """Return whether a container of literals alone is the right side of an `in` test.

    Args:
        container: The set or tuple holding the literal being classified.
        parents: Child-to-parent map over the enclosing binding.

    Returns:
        Whether the container is an arity or grammar membership test. A container that also holds
        a runtime value is not one, so `count in {500, budget}` keeps 500 a magnitude.
    """
    if not all(_is_numeric_constant_expression(element) for element in container.elts):
        return False
    compare = parents.get(id(container))
    if not isinstance(compare, ast.Compare):
        return False
    return any(
        comparator is container and isinstance(operator, ast.In | ast.NotIn)
        for operator, comparator in zip(compare.ops, compare.comparators, strict=True)
    )


def _is_subscripted_table_value(
    entry: ast.AST, table: ast.Dict, parents: dict[int, ast.AST]
) -> bool:
    """Return whether a literal is a value in a mapping a subscript reads directly.

    Args:
        entry: The literal, or the sign wrapper standing in for it.
        table: The mapping the literal belongs to.
        parents: Child-to-parent map over the enclosing binding.

    Returns:
        Whether the literal is a width the mapping yields. A key is not: `{100: 'x'}[escape]`
        selects by a magnitude nothing records, so the pinned spelling holds its literals in value
        position only.
    """
    if not any(value is entry for value in table.values):
        return False
    subscript = parents.get(id(table))
    return isinstance(subscript, ast.Subscript) and subscript.value is table


def _is_numeric_base_argument(argument: ast.AST, call: ast.Call) -> bool:
    """Return whether a literal argument states a numeric base or digit width.

    Args:
        argument: The literal argument, or the sign wrapper standing in for it.
        call: The call the argument belongs to.

    Returns:
        Whether the argument sits in one of the positions `_LITERAL_BENIGN_CALLEES` pins for this
        callee. A star argument makes every position unknowable, so such a call is never benign.
    """
    positions = _LITERAL_BENIGN_CALLEES.get(_called_name(call) or "")
    if positions is None or any(isinstance(item, ast.Starred) for item in call.args):
        return False
    return any(index in positions and item is argument for index, item in enumerate(call.args))


_PARITY_MODULUS = 2
"""The only modulo divisor pinned as a benign role, matching `shell_scanner.py`'s `% 2`. A wider
divisor such as `% 500` bounds a magnitude and is not exempted."""


def _is_displacing_arithmetic(operand: ast.AST, arithmetic: ast.BinOp) -> bool:
    """Return whether a literal operand moves a position instead of fixing a bound.

    Args:
        operand: The literal operand, or the sign or slice wrapper standing in for it.
        arithmetic: The arithmetic node the operand belongs to.

    Returns:
        Whether the arithmetic displaces rather than bounds.
    """
    if isinstance(arithmetic.op, ast.Mod):
        # Parity: `... % 2` selects a residue rather than budgeting anything. Pinned to divisor 2
        # exactly, the only spelling the guarded modules carry; `n % 500` bounds a magnitude.
        return (
            operand is arithmetic.right
            and isinstance(operand, ast.Constant)
            and operand.value == _PARITY_MODULUS
        )
    if not isinstance(arithmetic.op, ast.Add | ast.Sub):
        # Scaling by a literal fixes a bound however the other operand is derived.
        return False
    # Displacement: `index + 2` moves a cursor, while `50 + 50` is arithmetic on a bound.
    other = arithmetic.left if operand is arithmetic.right else arithmetic.right
    return not _is_numeric_constant_expression(other)


def _is_benign_literal_role(literal: ast.expr, parents: dict[int, ast.AST]) -> bool:
    """Return whether one numeric literal occurrence sits in a role that fixes no magnitude.

    Each role below is pinned by a spelling the guarded modules already carry. Anything else,
    including a role that reads as harmless, is a magnitude: see `_is_magnitude_binding`.

    Args:
        literal: The numeric literal occurrence to classify.
        parents: Child-to-parent map over the enclosing binding, from `_expression_parents`.

    Returns:
        Whether the occurrence is benign.
    """
    child: ast.AST = literal
    parent = parents.get(id(child))
    # A sign or a slice bound carries no role of its own, so the role is the one its own parent
    # gives: `basename[:-4]` is a slice position however the negative bound is spelled.
    while isinstance(parent, ast.UnaryOp | ast.Slice):
        child, parent = parent, parents.get(id(parent))
    if isinstance(parent, ast.BinOp):
        return _is_displacing_arithmetic(child, parent)
    if isinstance(parent, ast.Subscript):
        # A position in a fixed grammar: `range_parts[:2]`, `words[3:]`.
        return child is parent.slice
    if isinstance(parent, ast.Call):
        return _is_numeric_base_argument(child, parent)
    if isinstance(parent, ast.Set | ast.Tuple):
        return _is_arity_membership(parent, parents)
    if isinstance(parent, ast.Dict):
        return _is_subscripted_table_value(child, parent, parents)
    return False


def _is_magnitude_binding(expression: ast.expr) -> bool:
    """Return whether a binding fixes a resource magnitude rather than a dynamic position.

    The rule is deny-by-default. Every numeric literal occurrence in the binding that is not
    structural fixes a magnitude unless it sits in one of the benign roles
    `_is_benign_literal_role` enumerates, and one such occurrence makes the whole binding a
    magnitude. Enumerating the magnitude shapes instead let any spelling nobody had thought of
    through: `cap = (strict and 100) or 200` is a cap in either branch, and a rule that listed
    conditional expressions but not boolean ones certified it silently.

    So `limit = 50 * 2`, `cap = 512 * factor`, `cap = 100 if strict else budget` and
    `caps = (1, 100)` are all magnitudes, and so is any other value-position literal, while
    `index = start + 2` stays a cursor offset because displacement is a pinned benign role.

    A false positive here is resolved by spelling the binding plainly or by inventorying the bound
    name, never by widening the benign roles. A role is added only when a spelling already in the
    guarded modules needs it, and never to accommodate a spelling being introduced.

    Args:
        expression: The bound expression to classify.

    Returns:
        Whether the binding fixes a magnitude.
    """
    parents = _expression_parents(expression)
    return any(
        not _is_benign_literal_role(node, parents)
        for node in ast.walk(expression)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        and abs(node.value) not in STRUCTURAL_GUARD_LITERALS
    )


def _local_numeric_names(scope: ast.AST | None) -> frozenset[str]:
    """Return the scope's own names bound to a magnitude that is not structural.

    A cap spelled as a local assignment or parameter default has the provenance problem a
    module-level one has: `limit = 100` and `def scan(limit=100)` both create resource bounds with
    nothing recording where they came from, and both are invisible to the module-constant set and
    naming convention. `_is_magnitude_binding` decides which bindings fix a magnitude, so
    `index = start + 2` stays a dynamic cursor offset while `cap = 512 * factor` is a cap. Zero and
    one are excluded for the reason `STRUCTURAL_GUARD_LITERALS` gives, so a counter seeded at zero
    is not mistaken for a threshold.

    Args:
        scope: Enclosing function, or `None` at class or module level.

    Returns:
        Local names bound to a magnitude, empty when the origin is scope-free.
    """
    if scope is None:
        return frozenset()
    names: set[str] = set()
    for argument, default in _defaulted_arguments(scope):
        if _is_magnitude_binding(default):
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
        if _is_magnitude_binding(statement.value):
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(names)


def _threshold_names(
    nodes: tuple[ast.AST, ...], bound: frozenset[str], numeric: frozenset[str]
) -> set[str]:
    """Return every named threshold the guard's condition or origin statement references.

    A module-level numeric constant is a threshold whatever it is called. A conventionally named
    one the module does not define cannot be resolved here, so it is treated as a threshold too;
    a name the module binds to something other than a number is not one.

    A callee is calculation machinery rather than a compared value, but a name already resolved to
    a magnitude stays one wherever it is spelled: `cap.__index__()` reads the same bound `cap`
    does, and excluding the whole `call.func` subtree let a threshold ship uninventoried by being
    reached through an accessor.
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
        if id(node) not in call_targets or node.id in numeric
        if node.id in numeric or (node.id.startswith(_THRESHOLD_PREFIXES) and node.id not in bound)
    }


def _cached_reference_path_scores(
    root: ast.AST,
    cache: _DerivationCache,
    *,
    loads_only: bool = False,
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """Return the scored spellings of one root, traversing it at most once per parse.

    Callers extend the returned mappings while following writers, so each hit hands back its own
    copy rather than the memo.
    """
    key = (id(root), loads_only)
    scores = cache.paths.get(key)
    if scores is None:
        scores = _reference_path_scores(root, loads_only=loads_only)
        cache.paths[key] = scores
    read, values = scores
    return dict(read), dict(values)


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
    cache: _DerivationCache,
    prefix: tuple[int, int] = (0, 0),
) -> dict[str, tuple[int, int, int]]:
    """Return imported value references and their provenance distance from one operand.

    The score is call nesting, then writer hops, then total dependency distance. A direct-value
    reference reached through one assignment is stronger threshold evidence than an imported
    argument buried in a call, while equally shallow evidence remains conservative. True
    ``call.func`` nodes and attribute bases are machinery or incomplete spellings rather than
    compared values.
    """
    _, values = _cached_reference_path_scores(root, cache)
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


def _annotation_names(annotation: ast.expr | None) -> frozenset[str]:
    """Return the type names one annotation spells, including a stringized one."""
    if annotation is None:
        return frozenset()
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return frozenset()
    names: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return frozenset(names)


def _limits_parameter_names(tree: ast.Module, constructors: frozenset[str]) -> frozenset[str]:
    """Return the parameter names this module resolves to the scan's limits.

    A parameter annotated with a limits type holds one. So does one spelled `limits`, because
    `find_limits_violations` enforces that spelling: a scope taking limits must take it as a
    required parameter under that name, so the name is a contract this gate already checks rather
    than a convention it is guessing at. No other spelling qualifies, which is what keeps an
    arbitrary object's `.limits` attribute from approving itself.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = node.args
        names.update(
            argument.arg
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
            if argument.arg == "limits" or _annotation_names(argument.annotation) & constructors
        )
    return frozenset(names)


def _limits_valued_spellings(tree: ast.Module) -> frozenset[str]:
    """Return the spellings this module resolves to a scan-limits value.

    Resolution follows the binding: the limits parameters `_limits_parameter_names` recognizes
    hold one, a binding of a limits construction holds one, and a binding of a spelling already
    resolved to one holds one too. Matching any attribute path with a component spelled `limits`
    instead would approve `parsed.limits.max_depth` on a config object that is not the scan's
    limits, and reject the same value carried as `self._scan_limits` until someone renamed it.

    Args:
        tree: Parsed module.

    Returns:
        Bare and dotted spellings holding a limits value, empty when the module carries none.
    """
    constructors = _constructor_names(tree, LIMITS_CONSTRUCTORS)
    spellings = set(_limits_parameter_names(tree, constructors))

    def holds_limits(annotation: ast.expr | None, value: ast.expr | None) -> bool:
        if _annotation_names(annotation) & constructors:
            return True
        if value is None:
            return False
        if isinstance(value, ast.Call) and _called_name(value) in constructors:
            return True
        return ast.unparse(value) in spellings

    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            match node:
                case ast.AnnAssign(target=target, annotation=annotation, value=value):
                    targets: list[ast.expr] = [target]
                case ast.Assign(targets=assigned, value=value):
                    targets, annotation = list(assigned), None
                case _:
                    continue
            if not holds_limits(annotation, value):
                continue
            for target in targets:
                if not isinstance(target, ast.Name | ast.Attribute):
                    continue
                spelling = ast.unparse(target)
                if spelling not in spellings:
                    spellings.add(spelling)
                    grew = True
    return frozenset(spellings)


def _is_limits_field_operand(operand: ast.expr, limits_values: frozenset[str]) -> bool:
    """Return whether this operand reads a field of a resolved scan-limits value."""
    if not isinstance(operand, ast.Attribute):
        return False
    path = _attribute_path(operand)
    return any(".".join(path[:index]) in limits_values for index in range(1, len(path)))


def _operand_import_evidence(
    origin: ast.stmt,
    scope: ast.AST | None,
    operand: ast.expr,
    imported: frozenset[str],
    cache: _DerivationCache,
) -> dict[str, tuple[int, int, int]]:
    """Return imported evidence feeding one operand, scored by its shortest dependency path.

    A parameter default forwards a value into the guard exactly as an assignment writer does, and
    is one hop away for the same reason, so `def scan(items, cap=MAX_ITEMS)` sources the compared
    `cap` from the import even though no statement in the scope writes it.
    """
    evidence = _import_reference_evidence(operand, imported, cache)
    read_paths, value_paths = _cached_reference_path_scores(operand, cache)
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
                    root, imported, cache, writer_path
                ).items():
                    previous = evidence.get(spelling)
                    if previous is None or score < previous:
                        evidence[spelling] = score
            statement_reads, statement_values = _cached_reference_path_scores(
                statement, cache, loads_only=True
            )
            for paths, additions in (
                (read_paths, statement_reads),
                (value_paths, statement_values),
            ):
                for spelling, local_score in additions.items():
                    score = _combined_path_score(writer_path, local_score)
                    previous = paths.get(spelling)
                    if previous is None or score < previous:
                        paths[spelling] = score
    for spelling, score in _default_import_evidence(scope, read_paths, imported, cache).items():
        previous = evidence.get(spelling)
        if previous is None or score < previous:
            evidence[spelling] = score
    return evidence


def _default_import_evidence(
    scope: ast.AST | None,
    read_paths: dict[str, tuple[int, int]],
    imported: frozenset[str],
    cache: _DerivationCache,
) -> dict[str, tuple[int, int, int]]:
    """Return imported evidence the read parameters' defaults forward, one hop past the parameter.

    Args:
        scope: Enclosing function, or `None` at class or module level.
        read_paths: Scored spellings the operand's dependency graph reaches.
        imported: Spellings bound by an import the origin's scope can see.
        cache: Per-parse derivation memo.

    Returns:
        Scored imported spellings, empty when no read parameter carries a default.
    """
    evidence: dict[str, tuple[int, int, int]] = {}
    for name, default in _read_parameter_defaults(scope, set(read_paths)):
        reaching = [
            path for spelling, path in read_paths.items() if _spelling_root(spelling) == name
        ]
        if not reaching:
            continue
        shortest = min(reaching)
        for spelling, score in _import_reference_evidence(
            default, imported, cache, (shortest[0], shortest[1] + 1)
        ).items():
            previous = evidence.get(spelling)
            if previous is None or score < previous:
                evidence[spelling] = score
    return evidence


@dataclass(frozen=True, slots=True)
class _ImportedScope:
    """The spellings one origin's scope resolves to an import or to a scan-limits value.

    Attributes:
        imported: Spellings bound by an import the origin's scope can see.
        limits_values: Spellings the module resolves to a scan-limits value.
    """

    imported: frozenset[str]
    limits_values: frozenset[str]


def _pair_imported_thresholds(
    origin: ast.stmt,
    scope: ast.AST | None,
    operands: tuple[ast.expr, ast.expr],
    names: _ImportedScope,
    cache: _DerivationCache,
) -> set[str]:
    """Return the strongest imported threshold evidence for one adjacent operand pair.

    An explicit limits field is approved threshold evidence, making the opposite operand measured
    data. Otherwise all imported value paths compete by dependency distance. Equal strongest paths
    remain conservative and are all returned.
    """
    left, right = operands
    limits = names.limits_values
    if _is_limits_field_operand(left, limits) or _is_limits_field_operand(right, limits):
        return set()
    imported = names.imported
    evidence = _operand_import_evidence(origin, scope, left, imported, cache)
    for spelling, score in _operand_import_evidence(origin, scope, right, imported, cache).items():
        previous = evidence.get(spelling)
        if previous is None or score < previous:
            evidence[spelling] = score
    if not evidence:
        return set()
    strongest = min(evidence.values())
    return {spelling for spelling, score in evidence.items() if score == strongest}


_ORDERING_OPERATORS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
"""The comparisons that can bound a resource. Equality and membership ask which value something
is, not how much of it there is, so an imported sentinel, enum member or frozenset compared that
way is not a threshold and inventorying it as a fixed semantic bound would be a fiction."""

_COMPARISON_CALLS: dict[str, type[ast.cmpop]] = {
    "lt": ast.Lt,
    "le": ast.LtE,
    "gt": ast.Gt,
    "ge": ast.GtE,
    "eq": ast.Eq,
    "ne": ast.NotEq,
    "__lt__": ast.Lt,
    "__le__": ast.LtE,
    "__gt__": ast.Gt,
    "__ge__": ast.GtE,
    "__eq__": ast.Eq,
    "__ne__": ast.NotEq,
}
"""Callable spellings of a comparison, keyed by the callee name an operand would spell.

`operator.gt(len(items), 100)` and `len(items).__gt__(100)` bound a resource exactly as
`len(items) > 100` does. A rule that recognizes only comparison *syntax* therefore lets a new
resource cap ship with no provenance, since neither the literal nor an imported bound is reachable
from any `ast.Compare` node. The name is matched however it is imported, because the module it came
from is not what makes the call a comparison."""


def _call_comparison(node: ast.Call) -> ast.Compare | None:
    """Return the comparison a call spells, or `None` when it is not one.

    The synthesized node carries the call's own operand expressions, so the identity-keyed scoring
    and memoization downstream see exactly the nodes the source compares. Only the comparison
    wrapper is new. A starred or keyword argument leaves the operands unresolvable, so such a call
    is not read as a comparison.

    Args:
        node: A call the guard's closure contains.

    Returns:
        The equivalent comparison, or `None` when the call does not spell one.
    """
    operator = _COMPARISON_CALLS.get(_called_name(node) or "")
    if operator is None or node.keywords:
        return None
    match node.args:
        case [ast.Starred(), *_] | [_, ast.Starred(), *_]:
            return None
        case [left, right]:
            pass
        case [right] if isinstance(node.func, ast.Attribute):
            left = node.func.value
        case _:
            return None
    return ast.Compare(left=left, ops=[operator()], comparators=[right])


def _comparison_nodes(roots: tuple[ast.AST, ...]) -> tuple[ast.Compare, ...]:
    """Return every comparison these roots contain, in comparison and call spelling alike.

    Args:
        roots: Expression or statement roots to search.

    Returns:
        One comparison per comparing node, deduplicated by that node's identity.
    """
    found: dict[int, ast.Compare] = {}
    for root in roots:
        for node in ast.walk(root):
            if id(node) in found:
                continue
            if isinstance(node, ast.Compare):
                found[id(node)] = node
            elif isinstance(node, ast.Call) and (spelled := _call_comparison(node)) is not None:
                found[id(node)] = spelled
    return tuple(found.values())


def _imported_thresholds(
    origin: ast.stmt,
    scope: ast.AST | None,
    comparisons: tuple[ast.Compare, ...],
    names: _ImportedScope,
    cache: _DerivationCache,
) -> set[str]:
    """Return strongest imported threshold paths, classifying each ordering pair independently."""
    thresholds: set[str] = set()
    for comparison in comparisons:
        operands = (comparison.left, *comparison.comparators)
        for operator, pair in zip(comparison.ops, pairwise(operands), strict=True):
            if not isinstance(operator, _ORDERING_OPERATORS):
                continue
            thresholds.update(_pair_imported_thresholds(origin, scope, pair, names, cache))
    return thresholds


def _operand_magnitudes(operand: ast.expr, *, containers: bool = False) -> set[int | float]:
    """Return the numeric magnitudes one comparison operand or binding carries.

    Descent follows the forms that leave a magnitude compared while spelling it somewhere other
    than the operand itself. Arithmetic, because `depth - 4096 > 0` caps the scan at the same
    magnitude as `depth > 4096`. Both arms of a conditional expression, because each is a cap the
    guard can be compared against. The value of a subscript but never its slice, because
    `(100, 200)[flag]` compares a magnitude while `words[2]` names a position in a fixed grammar.
    A keyword argument nested in an operand is a position rather than a magnitude, and is not
    descended into.

    Args:
        operand: The expression to read.
        containers: Whether to read the elements of a literal container as well. A binding is
            followed that way, because `CAPS = (100,)` fixes a bound that `CAPS[0]` then compares,
            while a container spelled directly in a comparison is a membership test over a semantic
            set rather than a resource budget.

    Returns:
        The magnitudes the expression carries, empty when it carries none.
    """
    found: set[int | float] = set()
    pending = [(operand, containers)]
    while pending:
        current, inside = pending.pop()
        if isinstance(current, ast.Constant):
            if isinstance(current.value, int | float) and not isinstance(current.value, bool):
                found.add(current.value)
        elif isinstance(current, ast.BinOp):
            pending.extend(((current.left, inside), (current.right, inside)))
        elif isinstance(current, ast.UnaryOp):
            pending.append((current.operand, inside))
        elif isinstance(current, ast.IfExp):
            pending.extend(((current.body, inside), (current.orelse, inside)))
        elif isinstance(current, ast.Subscript):
            # Selecting one element of a container compares whatever magnitude it holds, so the
            # container is read from here down even when the operand itself is not a binding.
            pending.append((current.value, True))
        elif inside and isinstance(current, ast.Tuple | ast.List | ast.Set):
            pending.extend((element, inside) for element in current.elts)
        elif inside and isinstance(current, ast.Dict):
            pending.extend((value, inside) for value in current.values if value is not None)
    return found


def _binding_magnitudes(expression: ast.expr) -> set[int | float]:
    """Return the numeric magnitudes a binding fixes, including those held in a container."""
    return _operand_magnitudes(expression, containers=True)


def _threshold_literals(nodes: tuple[ast.AST, ...]) -> set[int | float]:
    """Return every bare numeric magnitude the guard compares against."""
    return {
        literal
        for node in _comparison_nodes(nodes)
        for operand in (node.left, *node.comparators)
        for literal in _operand_magnitudes(operand)
        if literal not in STRUCTURAL_GUARD_LITERALS
    }


# Each of these is a pure function of the module source it is handed, and the repository-level
# rules derive the same unchanged sources many times over in one process. Memoizing them keeps a
# gate run, and a test suite that exercises these rules repeatedly, from re-parsing and
# re-fingerprinting both guarded modules for every call.
@cache
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

    The imported-value rule reads a narrower set: the condition, the controls, and the values those
    writers forward into them. A writer is in the closure because the guard reads what it binds,
    not because every comparison it happens to spell decides the guard, and scoring those too
    reported an import that never reaches the condition while multiplying the work by the size of
    the closure.

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
    limits_values = _limits_valued_spellings(tree)
    violations: list[str] = []
    for statement, _call in _origin_calls_by_statement(tree):
        scope = annotations.scopes.get(id(statement))
        local_bindings = _cached_local_binding_names(scope, cache)
        local_imported = _scope_import_names(scope, cache)
        imported = local_imported | (module_imported - local_bindings)
        tests = annotations.tests.get(id(statement), ())
        condition_writers = _writer_statements(
            statement, scope, set(_read_spellings(tests)), set(_read_value_spellings(tests)), cache
        )
        controls, reachability_writers, _reachability_reads = _reachability_inputs(
            statement, scope, cache
        )
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
        compared: tuple[ast.AST, ...] = (
            statement,
            *tests,
            *control_expressions,
            *_forwarded_value_roots((*condition_writers, *reachability_writers)),
        )
        comparisons = _comparison_nodes(compared)
        imported_thresholds = _imported_thresholds(
            statement,
            scope,
            comparisons,
            _ImportedScope(imported, limits_values),
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


def _attribute_nodes(nodes: tuple[ast.AST, ...]) -> tuple[ast.Attribute, ...]:
    """Return the attribute accesses these nodes read off a value, method names excluded.

    A call target names an operation rather than data, exactly as it does for a threshold: reading
    `.get` or `.startswith` as inspected state would let any predicate spelling a common method
    claim relevance to any guard.

    Args:
        nodes: Roots to search.

    Returns:
        The attribute accesses, in walk order and with a node repeated when the roots overlap.
    """
    targets = {
        id(target)
        for root in nodes
        for call in ast.walk(root)
        if isinstance(call, ast.Call)
        for target in ast.walk(call.func)
    }
    return tuple(
        node
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Attribute)
        if id(node) not in targets
    )


def _attribute_reads(nodes: tuple[ast.AST, ...]) -> frozenset[str]:
    """Return the attribute names these nodes read off a value, method names excluded.

    Args:
        nodes: Roots to search.

    Returns:
        Attribute names read, empty when the nodes read none.
    """
    return frozenset(node.attr for node in _attribute_nodes(nodes))


def _leaf_reads(
    nodes: tuple[ast.AST, ...],
    iterations: tuple[tuple[ast.expr, ast.expr], ...] = (),
) -> frozenset[str]:
    """Return this derivation's attribute reads without the containers it only iterates through.

    A guard that walks `for scope in evidence.scopes` and refuses on `scope.parent_scope_id` reads
    both names, but only the second is what it decides on. Leaving the container in the relevant
    set is what let a predicate claim relevance to that guard by reading `evidence.scopes` and
    inspecting no parent edge at all, which is exactly the borrowing this rule exists to stop.

    An attribute is intermediate when it is the iteration source of a `for` statement or a
    comprehension generator in this derivation and the iteration variable itself has attribute
    reads here. Requiring the second condition keeps a container the derivation reads for its own
    sake, such as a membership test over an iterated set of identifiers, in the relevant set.

    Args:
        nodes: The derivation's roots, whose attribute reads form the layer.
        iterations: Target and iterable pairs from loops enclosing the derivation, whose headers
            are not themselves among the roots.

    Returns:
        The layer without its intermediate containers, or the whole layer when removing them
        would leave nothing to intersect against.
    """
    accesses = _attribute_nodes(nodes)
    names = frozenset(node.attr for node in accesses)
    pairs = [
        *iterations,
        *(
            (node.target, node.iter)
            for root in nodes
            for node in ast.walk(root)
            if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension)
        ),
    ]
    read_variables = {node.value.id for node in accesses if isinstance(node.value, ast.Name)}
    intermediate = {
        name
        for target, source in pairs
        if _target_names(target) & read_variables
        for name in _attribute_reads((source,))
    }
    return frozenset(names - intermediate) or names


def _transport_parameters(tree: ast.AST, path: str) -> dict[str, tuple[_Function, str]]:
    """Return each declared transport this module defines as a receiving function, by its name.

    A declared transport is a site that propagates a refusal it did not mint. Most of them raise
    one they were handed by a caller further out, but `_validate_acyclic_graph` is handed one as an
    argument and owns the condition that decides it, which is what makes its callers the origins.
    Only that form is resolvable from the origin's side, so a transport qualifies here when it
    takes a parameter of the declared argument name.

    Args:
        tree: Parsed guarded module.
        path: Its file name, used to select the declarations that describe it.

    Returns:
        The transport definition and its refusal parameter name, keyed by the callee name an origin
        statement spells.
    """
    declared = {
        qualname.rsplit(".", 1)[-1]: argument
        for module, qualname, argument in DECLARED_TRANSPORTS
        if module == path
    }
    found: dict[str, tuple[_Function, str]] = {}
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


def _receiving_transport_call(
    call: ast.Call,
    parents: dict[int, ast.AST],
    transports: dict[str, tuple[_Function, str]],
) -> tuple[_Function, str, ast.Call] | None:
    """Return the declared transport this refusal is constructed as an argument to.

    Args:
        call: The refusal construction.
        parents: Module-wide parent map.
        transports: Receiving transports the module defines, by callee name.

    Returns:
        The transport definition, its refusal parameter name and the call handing the refusal to
        it, or `None` when this refusal is not transported that way.
    """
    current: ast.AST | None = call
    while current is not None:
        holder = parents.get(id(current))
        if isinstance(holder, ast.Call):
            name = holder.func.id if isinstance(holder.func, ast.Name) else None
            resolved = transports.get(name) if name is not None else None
            return None if resolved is None else (*resolved, holder)
        if isinstance(holder, ast.stmt) or holder is None:
            return None
        current = holder
    return None


def _transport_argument_reads(
    statement: ast.stmt,
    call: ast.Call,
    transports: dict[str, tuple[_Function, str]],
    annotations: _Annotations,
    cache: _DerivationCache,
) -> frozenset[str]:
    """Return the leaf reads of the layer that decides a transported refusal.

    The origin statement of a transported refusal spells the identifier and the value it hands
    down, and nothing else. What decides the refusal is split in two: the tests governing the
    transport's `raise` of that parameter, and the structure the caller built into the other
    arguments it passes. `_validate_acyclic_graph` is the whole of the second case, where the
    caller's `parent_graph` is what carries the parent edges the cycle detector walks.

    The caller-side closure is seeded from those other arguments rather than from the governing
    tests, and each selected writer contributes only its own expressions. A loop selected because
    it binds the name a writer reads would otherwise contribute every attribute its body touches,
    which is how an unrelated sibling assignment in the same loop reaches the relevant set.

    Args:
        statement: The statement that constructs the refusal.
        call: The refusal construction itself.
        transports: Receiving transports the module defines, by callee name.
        annotations: The module's one annotation pass, which also carries the origin's scope.
        cache: Per-parse derivation memo.

    Returns:
        Leaf attribute names, empty when the refusal is not handed to a resolvable transport or
        when neither side reads an attribute.
    """
    resolved = _receiving_transport_call(call, _cached_module_parents(cache), transports)
    if resolved is None:
        return frozenset()
    scope = annotations.scopes.get(id(statement))
    transport, argument, transport_call = resolved
    tests: tuple[ast.expr, ...] = ()
    for raised in ast.walk(transport):
        if not isinstance(raised, ast.Raise) or raised.exc is None:
            continue
        if argument not in {
            reference.id for reference in ast.walk(raised.exc) if isinstance(reference, ast.Name)
        }:
            continue
        tests = annotations.tests.get(id(raised), ())
        break
    carried = tuple(
        expression
        for expression in (
            *transport_call.args,
            *(keyword.value for keyword in transport_call.keywords),
        )
        if all(node is not call for node in ast.walk(expression))
    )
    read = set(_read_spellings(carried))
    values = set(_read_value_spellings(carried))
    writers = _writer_statements(statement, scope, read, values, cache)
    nodes = (*tests, *(node for writer in writers for node in _own_expressions(writer)))
    iterations = tuple(
        (writer.target, writer.iter)
        for writer in writers
        if isinstance(writer, ast.For | ast.AsyncFor)
    )
    return _leaf_reads(nodes, iterations)


# Each of these is a pure function of the module source it is handed, and the repository-level
# rules derive the same unchanged sources many times over in one process. Memoizing them keeps a
# gate run, and a test suite that exercises these rules repeatedly, from re-parsing and
# re-fingerprinting both guarded modules for every call.
@cache
def guard_condition_reads(source: str, path: str) -> dict[str, tuple[frozenset[str], ...]]:
    """Return the layers of attribute names each guard's condition inspects, by origin identifier.

    An invariant classification claims a guard's condition is false for every constructible input,
    and its evidence is a boundary script that drives that condition over the structure the guard
    inspects. Which structure that is has to be derived from the guard, not asserted by the witness:
    a predicate reading `evidence.commands` satisfies every assertion a hand-written row can be held
    to while saying nothing about a guard that inspects `scope.parent_scope_id`.

    Up to three layers are derived, ordered by how directly each decides the refusal, and only the
    non-empty ones are kept:

    - The transported layer, for a refusal handed to a declared transport, which is the transport's
      own governing tests plus the caller-side closure of the structure it was handed.
    - The condition layer: the origin statement, the tests and enclosing loop iterables governing
      it, and the `try` body whose failure is a handler guard's only condition.
    - The closure layer, which adds the transitive writer closure of what those read, recovering
      the structure behind a value the condition reads under a local name.

    Only when all three are empty, which is the case for a guard reached purely by falling through
    earlier arms, do the preceding controls stand in for them. They are a much wider set, including
    everything every earlier guard in the same function inspected, so using them first would let a
    predicate borrow relevance from an unrelated neighbour. Every layer is reported with the
    containers it only iterates through already removed, so a caller intersecting against the first
    one is held to what the guard decides on rather than to what holds it.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used to resolve declared transports and to keep two modules
            defining the same identifier out of one memo entry.

    Returns:
        Ordered non-empty layers by origin identifier, empty for a guard whose condition reads no
        attribute at all.
    """
    tree = ast.parse(source)
    annotations = _annotate(tree)
    guarded_bodies = _guarded_bodies(tree)
    cache = _DerivationCache(module=tree)
    transports = _transport_parameters(tree, path)
    reads: dict[str, tuple[frozenset[str], ...]] = {}
    for statement, call in _origin_calls_by_statement(tree):
        if not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        origin_id = call.args[0].value
        if not isinstance(origin_id, str):
            continue
        scope = annotations.scopes.get(id(statement))
        loops = annotations.loops.get(id(statement), ())
        governing = (*annotations.tests.get(id(statement), ()), *(loop.iter for loop in loops))
        iterations = tuple((loop.target, loop.iter) for loop in loops)
        body = guarded_bodies.get(id(statement), ())
        transported = _transport_argument_reads(statement, call, transports, annotations, cache)
        condition = _leaf_reads((statement, *governing, *body), iterations)
        read = set(_read_spellings(governing)) | set(_cached_statement_reads(statement, cache))
        values = set(_read_value_spellings(governing)) | set(
            _cached_statement_value_reads(statement, cache)
        )
        writers = _writer_statements(statement, scope, read, values, cache)
        closure = _leaf_reads((statement, *governing, *writers, *body), iterations)
        layers = tuple(layer for layer in (transported, condition, closure) if layer)
        if layers:
            reads[origin_id] = layers
            continue
        controls, flow_writers, _flow_read = _reachability_inputs(statement, scope, cache)
        fallback = _leaf_reads((*controls, *flow_writers))
        reads[origin_id] = (fallback,) if fallback else ()
    return reads


@cache
def guard_relevant_reads(source: str, path: str) -> dict[str, frozenset[str]]:
    """Return the leaf reads of the one layer that decides each guard, by origin identifier.

    This is the set a boundary-evidence predicate is held to: the first non-empty layer
    `guard_condition_reads` derives, with the containers it only iterates through already removed.
    It is exported because the relevance rule is enforced twice over the same set. The gate
    intersects it against what the registry's predicate mentions in inert source, and the test
    suite intersects it against what that predicate actually reads while executing against its
    boundary evidence, which is the half no source derivation can supply. Both halves have to name
    the same set for the second to be a real strengthening of the first rather than a parallel rule
    with its own notion of relevance.

    Args:
        source: Module source text, parsed but never executed.
        path: Module file name, used to resolve declared transports and to keep two modules
            defining the same identifier out of one memo entry.

    Returns:
        Leaf attribute names by origin identifier, empty for a guard whose condition reads no
        attribute at all.
    """
    return {
        origin_id: layers[0] if layers else frozenset()
        for origin_id, layers in guard_condition_reads(source, path).items()
    }


@cache
def invariant_predicate_reads(source: str) -> dict[str, frozenset[str]]:
    """Return the attribute names each invariant row's boundary predicate reads, by identifier.

    The registry is parsed, never imported, like every other read of it here. A predicate spelled as
    a call to a helper the registry defines is followed into that helper, transitively, since
    `_scope_output_nodes` is where the shipped output-walk rows do their reading.

    Args:
        source: Registry source text.

    Returns:
        Attribute names by origin identifier, for every invariant row carrying a predicate.

    Raises:
        ValueError: If the invariant registry is not a single canonical tuple binding.
    """
    tree = ast.parse(source)
    helpers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    constructor, fields, _required = CLASSIFICATION_REGISTRIES["INVARIANT_WITNESSES"]
    predicate_field = "boundary_evidence"
    reads: dict[str, frozenset[str]] = {}
    for entry in _classification_registry_tuples(tree)["INVARIANT_WITNESSES"].elts:
        if not isinstance(entry, ast.Call) or _called_name(entry) != constructor:
            continue
        supplied = dict(zip(fields, entry.args, strict=False))
        supplied.update(
            {keyword.arg: keyword.value for keyword in entry.keywords if keyword.arg is not None}
        )
        identifier = supplied.get(IDENTITY_FIELD)
        predicate = supplied.get(predicate_field)
        if not isinstance(identifier, ast.Constant) or not isinstance(identifier.value, str):
            continue
        if predicate is None:
            continue
        found: set[str] = set()
        followed: set[str] = set()
        pending: list[ast.AST] = [predicate]
        while pending:
            node = pending.pop()
            found |= _attribute_reads((node,))
            for called in ast.walk(node):
                if not isinstance(called, ast.Call) or not isinstance(called.func, ast.Name):
                    continue
                name = called.func.id
                helper = helpers.get(name)
                if helper is not None and name not in followed:
                    followed.add(name)
                    pending.append(helper)
        reads[identifier.value] = frozenset(found)
    return reads


def repository_invariant_relevance_violations(root: Path) -> tuple[str, ...]:
    """Return every invariant row whose boundary predicate inspects nothing its guard does.

    The rule is deny-by-default. The derivation enumerates the layers the gate can follow and
    holds the predicate to the first non-empty one, so an unreadable predicate or an empty
    derivation rejects rather than passes.

    The row's other assertions live in the test suite, where the boundary script can be executed:
    that it stops short of the guard, that its predicate rejects the evidence an empty script
    builds, and that it drives the guard's own condition line without reaching the refusal. None of
    them constrains what the predicate *means*. A row for a guard that inspects
    `scope.parent_scope_id` can pass all three with `boundary_script="echo hi"` and
    `boundary_evidence=lambda evidence: bool(evidence.commands)`, classifying the guard out of
    frozen debt with no executable support for the invariant it claims.

    Requiring the predicate to read something the guard's condition reads ties the two together
    without executing either. It is a floor rather than a proof: a predicate can still be weak about
    the right data. What it rules out is a predicate about the wrong data. The residual is concrete:
    a predicate reading `scope_id` for the parent-cycle guard reads a leaf of the deciding layer and
    still says nothing about whether the parent edges form a cycle. Nothing derivable from the
    source closes that gap, so it stays with human review of the row's rationale under AD-20.

    Being a rule over inert source, this half also counts a read the predicate never performs, since
    a mention in a branch that never runs is still a mention. The suite closes that by executing
    every predicate against its boundary evidence under a recording wrapper and intersecting the
    reads it observes against the same `guard_relevant_reads` set, so an unexecuted read cannot
    carry a row however it is spelled.

    The predicate is held to the layer that actually decides the guard, and to the leaf attributes
    of it. Intersecting a flat union of every layer would let a row for the parent-cycle detector
    pass on `bool(evidence.commands) and bool(evidence.scopes)`, which reads no parent edge and
    borrows its relevance from the container the guard walks through.

    Args:
        root: Repository root holding the guarded modules and the witness registry.

    Returns:
        Human-readable violations, empty when every invariant row inspects its guard's own data.
    """
    predicates = invariant_predicate_reads((root / REGISTRY_PATH).read_text(encoding="utf-8"))
    conditions: dict[str, frozenset[str]] = {}
    for module in _repository_guard_modules(root):
        source = (root / module).read_text(encoding="utf-8")
        conditions.update(guard_relevant_reads(source, Path(module).name))
    violations: list[str] = []
    for origin_id, names in sorted(predicates.items()):
        inspected = conditions.get(origin_id)
        if inspected is None:
            continue
        if not inspected:
            violations.append(
                f"{origin_id}: this guard's condition reads no attribute of the evidence, so a "
                f"boundary-evidence predicate cannot witness it; classify it with a reachable "
                f"witness instead, under shrunk limits if it is a resource bound"
            )
            continue
        if not names & inspected:
            violations.append(
                f"{origin_id}: boundary evidence reads {sorted(names) or 'nothing'} while the "
                f"guard's condition reads {sorted(inspected)}; a predicate over data this guard "
                f"does not inspect supports no claim about it"
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
    """Return whether a module mentions the refusal or verdict protocol at all.

    Discovery over-approximates. Recognizing participation by *call* made discovery the last
    accept-by-default recognizer in the pipeline, and every deny-by-default gate sits behind it: a
    module whose only construction is spelled through a form no rule can follow, such as
    `FACTORIES = {"guard": GuardRefusal}` called as `FACTORIES["guard"](...)`, was never discovered,
    so the shape gate that rejects exactly that binding never ran over it. A protocol name mentioned
    anywhere, in any position, resolvable or not, is therefore enough, together with a definition of
    a verdict-producing function.

    A mention includes the name as *text*, since `getattr(sg, "GuardRefusal")(...)` is the same
    construction one obfuscation deeper and spells no `Name` or `Attribute` node at all. Matching a
    whole string literal against the name keeps that cheap: a docstring or message mentioning the
    protocol in prose is not equal to a bare protocol name, so only a string that could be handed to
    an attribute lookup matches.

    Over-approximating here is safe in the direction it errs. It decides only which modules the
    strict gates run over, and a module swept in by an incidental mention then has to satisfy those
    gates: shape validation and reachability have nothing to say about a module that constructs no
    refusal, while coverage and the limits rules can require candidate-owned allowlist entries, as
    the protocol-defining module's `GUARDED_MODULES` and `LIMITS_BOUNDARIES` entries record.

    Args:
        tree: Parsed module.

    Returns:
        Whether the module belongs to the guarded surface.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in GUARD_PROTOCOL_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in GUARD_PROTOCOL_NAMES:
            return True
        if isinstance(node, ast.Constant) and node.value in GUARD_PROTOCOL_NAMES:
            return True
        if isinstance(node, ast.Import | ast.ImportFrom) and any(
            alias.name.rsplit(".", 1)[-1] in GUARD_PROTOCOL_NAMES for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in VERDICT_FUNCTIONS
        ):
            return True
    return False


def _repository_guard_modules(root: Path) -> tuple[str, ...]:
    """Return every module below the guard package that participates in the guard protocol.

    This derivation is used by the base-owned closure and comparison. Reading the base checker's
    `GUARDED_MODULES` there would make a candidate module invisible even after the candidate adds it
    to its own tuple, because the protected base copy necessarily predates that edit. Discovery
    over-approximates by mention, so a malformed refusal shape reaches the candidate-owned shape
    gate before it can be added to the allowlist however it is spelled.
    """
    modules: list[str] = []
    for path in sorted((root / GUARD_MODULE_ROOT).rglob("*.py")):
        if _source_uses_guard_protocol(path.read_text(encoding="utf-8")):
            modules.append(path.relative_to(root).as_posix())
    return tuple(modules)


@cache
def _source_uses_guard_protocol(source: str) -> bool:
    """Return whether this module source mentions the guard protocol.

    Discovery re-reads every module below the guard package on each repository-level rule, and the
    answer is a pure function of the source. Keying on the text rather than the path is what keeps
    a candidate tree written during a test from ever reading a stale answer.

    The answer over-approximates, as `_uses_guard_protocol` describes: a mention sweeps the module
    in and the strict gates decide inside it.

    Args:
        source: Module source text, parsed but never executed.

    Returns:
        Whether the module belongs to the guarded surface.
    """
    return _uses_guard_protocol(ast.parse(source))


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


def repository_reachability_violations(root: Path) -> tuple[str, ...]:
    """Return every guard origin that no public entry point of its own module can reach.

    The guarded surface is discovered from the candidate tree, and the entry points within each
    module are derived from it as well, so this rule names no allowlist and runs from the base
    revision's copy of the checker for the same reason closure does.

    Args:
        root: Repository root to read the guarded modules from.

    Returns:
        Human-readable violations, empty when every origin's function is reachable.
    """
    violations: list[str] = []
    for module in _repository_guard_modules(root):
        source = (root / module).read_text(encoding="utf-8")
        violations.extend(find_reachability_violations(source, Path(module).name))
    return tuple(violations)


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


def repository_dead_statement_violations(root: Path) -> tuple[str, ...]:
    """Return every unreachable statement across the repository's guarded modules.

    The surface is the one shape validation uses, the discovered modules together with the
    allowlist, because a module that stops being discovered is exactly where dead code would be
    cheapest to hide.

    Args:
        root: Repository root to read the guarded modules from.

    Returns:
        Human-readable violations, empty when every statement in every guarded module is reachable.
    """
    violations: list[str] = []
    for module in sorted(set(GUARDED_MODULES) | set(_repository_guard_modules(root))):
        source = (root / module).read_text(encoding="utf-8")
        violations.extend(find_dead_statement_violations(source, Path(module).name))
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
        # base is what makes it unweakenable. Reachability derives its entry points from the
        # candidate tree for that same reason, and so belongs here too.
        base_snapshot = arguments.compare_base.read_text(encoding="utf-8")
        base_registry = (
            None
            if arguments.base_registry is None
            else arguments.base_registry.read_text(encoding="utf-8")
        )
        failures = _run_gates(
            (
                ("closure", lambda: repository_closure_violations(arguments.root)),
                ("guard reachability", lambda: repository_reachability_violations(arguments.root)),
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
                ("guard reachability", lambda: repository_reachability_violations(arguments.root)),
                ("dead statements", lambda: repository_dead_statement_violations(arguments.root)),
                (
                    "invariant evidence relevance",
                    lambda: repository_invariant_relevance_violations(arguments.root),
                ),
                ("closure", lambda: repository_closure_violations(arguments.root)),
            )
        )
    for failure in failures:
        sys.stderr.write(f"{failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
