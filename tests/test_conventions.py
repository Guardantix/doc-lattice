"""Convention enforcement tests."""

import ast
import inspect
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from doc_lattice import error_types
from doc_lattice.constants import (
    CHECKOUT_REF,
    CHECKOUT_VERSION,
    SETUP_UV_REF,
    SETUP_UV_VERSION,
    VALID_AUTHORITIES,
)
from doc_lattice.error_types import ProjectError
from doc_lattice.reconcile import Rewrite

SRC_DIR = Path(__file__).parent.parent / "src" / "doc_lattice"


def _source_files() -> list[Path]:
    """Every source module, recursively, excluding bytecode caches."""
    return [p for p in SRC_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    """True when a handler catches Exception/BaseException, in Name, tuple, or bare form."""
    t = handler.type
    if t is None:  # bare `except:` is at least as broad
        return True
    elts = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in elts)


def _broad_except_lines(source: str) -> list[int]:
    """Line numbers of every broad except handler in the given source."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ExceptHandler) and _is_broad_except(node)
    ]


def _current_time_calls(source: str) -> list[int]:
    """Line numbers of any .now()/.utcnow() call (catches the tz-aware form too)."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("now", "utcnow")
    ]


def test_no_current_time_calls_outside_datetime_utils():
    """Any current-time call outside datetime_utils.py is banned (incl. datetime.now(tz=UTC))."""
    assert _current_time_calls("datetime.now(tz=UTC)")  # positive control: arg'd form caught
    assert not _current_time_calls("x = obj.now")  # attribute access, not a call
    for py_file in _source_files():
        # datetime_utils.py is the sanctioned home for a current-time call, and AD-2 records
        # it as the narrow impure time boundary. It exists again because the reconcile journal
        # records when a transaction was prepared; nothing else may read the clock.
        if py_file.name == "datetime_utils.py":
            continue
        assert not _current_time_calls(py_file.read_text(encoding="utf-8")), py_file.name


def test_no_inner_html():
    """innerHTML must not appear in any source file."""
    for py_file in _source_files():
        content = py_file.read_text(encoding="utf-8")
        assert "innerHTML" not in content, f"{py_file.name} contains innerHTML"


def test_broad_except_detector_covers_all_forms():
    """Positive control: the matcher fires on every broad form and spares narrow ones."""
    assert _broad_except_lines("try:\n x=1\nexcept Exception:\n pass\n")
    assert _broad_except_lines("try:\n x=1\nexcept BaseException:\n pass\n")
    assert _broad_except_lines("try:\n x=1\nexcept (ValueError, Exception):\n pass\n")
    assert _broad_except_lines("try:\n x=1\nexcept:\n pass\n")
    assert not _broad_except_lines("try:\n x=1\nexcept (KeyError, ValueError):\n pass\n")


def test_no_broad_except():
    """except Exception/BaseException (Name, tuple, or bare) are not allowed in src."""
    for py_file in _source_files():
        lines = _broad_except_lines(py_file.read_text(encoding="utf-8"))
        assert not lines, f"{py_file.name} has broad except at lines {lines}"


def test_cli_project_error_handlers_stay_centralized():
    """CLI command ProjectError handling must stay centralized in one helper plus main."""
    cli_dir = SRC_DIR / "cli"
    handler_counts = {
        path.relative_to(cli_dir).as_posix(): count
        for path in sorted(cli_dir.rglob("*.py"))
        if (count := path.read_text(encoding="utf-8").count("except ProjectError"))
    }

    assert handler_counts == {"__init__.py": 1, "errors.py": 1}


def test_no_raw_authority_strings():
    """Authority values must be imported from constants.py, not inlined as raw literals."""
    for py_file in _source_files():
        if py_file.name == "constants.py":
            continue
        content = py_file.read_text(encoding="utf-8")
        for value in sorted(VALID_AUTHORITIES):
            assert f'"{value}"' not in content, f"{py_file.name} inlines authority '{value}'"
            assert f"'{value}'" not in content, f"{py_file.name} inlines authority '{value}'"


def test_no_raw_action_pin_values():
    """Action pin values must be imported from constants.py, not inlined as raw literals.

    An identity check on the imported names would not prove this: CPython interns the
    40-character hex SHAs, so ``render.CHECKOUT_REF is constants.CHECKOUT_REF`` holds even for
    an independently declared duplicate literal. Reading every module's source rules out a
    second copy anywhere, including in a renderer that does not exist yet.
    """
    for py_file in _source_files():
        if py_file.name == "constants.py":
            continue
        content = py_file.read_text(encoding="utf-8")
        for value in (CHECKOUT_REF, CHECKOUT_VERSION, SETUP_UV_REF, SETUP_UV_VERSION):
            assert value not in content, f"{py_file.name} inlines the pin value '{value}'"


def test_no_em_dashes_in_source():
    """Em-dash (U+2014) is banned in src docstrings, messages, and comments."""
    em_dash = chr(0x2014)  # build the real char at runtime; keeps this file's own source ASCII
    assert len(em_dash) == 1  # guard: a single real char, not the literal 6-char escape string
    assert ord(em_dash) == 0x2014  # and it is specifically U+2014
    for py_file in _source_files():
        assert em_dash not in py_file.read_text(encoding="utf-8"), f"{py_file.name} has an em-dash"


def test_every_module_has_a_docstring():
    """CLAUDE.md requires a module docstring on every module (ruff has no D rules enabled)."""
    for py_file in _source_files():
        if py_file.name == "__init__.py":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        assert ast.get_docstring(tree) is not None, f"{py_file.name} lacks a module docstring"


def test_all_error_types_extend_project_error_with_code():
    """Every exception defined in error_types.py must extend ProjectError and set a real code."""
    for _name, cls in inspect.getmembers(error_types, inspect.isclass):
        if not (issubclass(cls, BaseException) and cls.__module__ == error_types.__name__):
            continue
        if cls is ProjectError:
            continue
        assert issubclass(cls, ProjectError), f"{cls.__name__} does not extend ProjectError"
        assert cls("msg").code != "UNKNOWN", f"{cls.__name__} left code at the default"


# ---------------------------------------------------------------------------
# Reconcile reparse-gate provenance guard
#
# ``reconcile.py::_verify_reconciled_meta`` is the last point at which a mis-spliced
# rewrite can be refused instead of written durably: the commit transaction stages
# ``Rewrite.after`` and publishes it without ever reparsing those bytes. The chain that
# reaches the gate is sound today, but ``Rewrite`` is an ordinary frozen dataclass and
# the staging and publication helpers are ordinary functions, so nothing stops a future
# write path from routing around the gate. This guard pins that chain by function,
# callee, and value provenance rather than by line number, so unrelated churn does not
# break it while a genuinely new producer, staging site, or publication sink does.
#
# Scoped to transaction *after images* only. Before images, journal bytes, and recovery
# artifacts are deliberately staged and republished without passing through the gate, so
# a generic audit of every stage or publish call would encode the wrong rule.
#
# AD-30 in ARCHITECTURE.md owns this invariant. When these matchers stop fitting, re-derive
# the invariant from that decision rather than loosening them until the suite passes again.
# ---------------------------------------------------------------------------

GATE = "_verify_reconciled_meta"
GATED_CALLEE = "apply_reconcile"
PRODUCER = "plan_rewrites"
ENVELOPE_SPLITTER = "split_frontmatter_parts"
AFTER_IMAGE_INFIX = "RECONCILE_AFTER_IMAGE_INFIX"
STAGE_CALLEE = "stage_bytes"
PUBLISH_CALLEE = "replace_staged"
COPY_CALLEE = "replace"
STAGING_FUNCTION = "_prepare_transaction"
FORWARD_PUBLISHER = "_commit_rewrites_locked"
RECONCILE_MODULE = "reconcile.py"
TRANSACTION_MODULE = "reconcile_transaction.py"
AFTER_FIELD = "after"
AFTER_FIELD_INDEX = list(Rewrite.__dataclass_fields__).index(AFTER_FIELD)
FORWARD_IMAGE_FIELD = "after_path"
ROLLBACK_IMAGE_FIELD = "before_path"
LINE_ENDING_CALLEE = "_line_ending"
# The complete endings a restoration operand or an envelope literal may carry. A character-set
# test would accept any run of CR and LF, so `new_text.replace("\n", "\n\n")` would pass while
# doubling every newline through the gate-verified YAML and body.
LINE_ENDINGS = frozenset({"\n", "\r\n", "\r"})
OPAQUE_BINDING = -1
DESTINATION_FIELD = "destination"
# Calls in the transaction module that may hold a reconcile destination without publishing over
# it. A new sink can always name a write primitive this guard has never heard of, so the audit
# is keyed to the destination rather than to a list of primitives, and every callee that reaches
# one is classified here deliberately. `stage_bytes` writes a sibling temporary, never the
# destination itself; the rest fingerprint, validate, or report it.
# `format_path_for_display` is the purest reader of the set: it returns `repr(str(path))` and
# touches no filesystem at all (AD-34). GTX-209 added it, because applying the display spelling
# to the transaction's own diagnostics hands a destination to it from several functions.
DESTINATION_READERS = frozenset(
    {
        "_authenticate_staged_artifact",
        "_commit_operation_error",
        "_recovery_operation_error",
        "_resolve_journal_path",
        "_validate_artifact_path",
        "file_sha256",
        "format_path_for_display",
        "stage_bytes",
        PUBLISH_CALLEE,
    }
)
# Recording a destination in an in-memory container is not a write, so classification and
# outcome bookkeeping may accumulate one. This is recognized by shape rather than by callee
# name: the receiver must be a local provably bound to a collection built in the same function.
# Admitting the bare names would let any object with an `append` method take a destination.
COLLECTION_ACCUMULATORS = frozenset({"add", "append", "discard", "extend", "insert", "update"})
COLLECTION_BUILDERS = frozenset({"dict", "list", "set"})
PUBLICATION_OWNERS = frozenset({"persistence.py", TRANSACTION_MODULE})
# The composite primitive below stages and publishes one destination in a single call, so naming
# it overwrites a document without ever naming the publication helper and would slip past a scan
# that reads only that helper's name. Its present users write their own artifacts rather than
# documents, so they are pinned per primitive and any new user of that route fails closed.
COMPOSITE_PUBLISH_USERS: dict[str, frozenset[str]] = {
    "atomic_replace_bytes": frozenset({"cache/store.py"}),
}
# The whole document a rewrite may reassemble, as the exact order it reattaches. Requiring only
# that some verified value appears would accept `f"{new_meta}"`, which drops the fences and the
# entire body while every byte it does emit is still gate-verified, so the reassembly is pinned
# as a complete envelope: each piece exactly once, in this order, around the verified metadata.
# `raw_meta` is deliberately absent, since it holds the pre-edit YAML the gate never verified and
# admitting it would let the check pass on a document reintroducing the bytes the gate replaced.
# The literal is part of the order, not a validated aside. Checking each constant on its own and
# then dropping it lets any number of endings be spliced anywhere, and those bytes are published.
GATED_SLOT = "<gate-verified metadata>"
NEWLINE_SLOT = "<line ending>"
ENVELOPE_ORDER = (
    "prefix",
    "open_fence",
    NEWLINE_SLOT,
    GATED_SLOT,
    "close_fence",
    "close_fence_newline",
    "body",
)
ENVELOPE_FIELDS = frozenset(ENVELOPE_ORDER) - {GATED_SLOT, NEWLINE_SLOT}


def _gate_msg(detail: str) -> str:
    """Wrap one guard diagnostic so every failure names the reparse gate explicitly."""
    return f"{detail}; every reconcile after image must flow through {GATE}()"


@dataclass(frozen=True)
class _Binding:
    """One value bound to a local name, optionally as element ``index`` of an unpack."""

    value: ast.expr
    index: int | None = None


@dataclass(frozen=True)
class _Site:
    """One call site, located by module and innermost enclosing function."""

    module: str
    function: str | None
    call: ast.Call

    @property
    def located(self) -> str:
        return f"{self.module}::{self.function}"


@dataclass(frozen=True)
class _TraceContext:
    """What a provenance trace needs to know about one enclosing function."""

    trusted: frozenset[str]
    bindings: dict[str, list[_Binding]]
    envelope_root: str | None = None
    restoration: bool = False


@cache
def _production_sources() -> dict[str, str]:
    """Every production module's source, keyed by path relative to the package root.

    Cached because the parametrized positive controls read the whole package once per case, and
    every consumer builds a new mapping rather than mutating this one.
    """
    return {
        path.relative_to(SRC_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in _source_files()
    }


def _walk_local(node: ast.AST):
    """Walk a subtree without descending into nested function or class scopes."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        yield child
        yield from _walk_local(child)


def _enclosing_functions(tree: ast.Module) -> dict[int, str | None]:
    """Map each node's id to the name of the innermost function that contains it."""
    owner: dict[int, str | None] = {}

    def visit(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else None
            owner[id(child)] = name or current
            visit(child, name or current)

    visit(tree, None)
    return owner


def _is_call_to(node: ast.AST, callee: str) -> bool:
    """True for a call to the bare name ``callee``."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == callee


def _call_sites(trees: dict[str, ast.Module], callee: str | None = None) -> list[_Site]:
    """Every call site across the given trees, optionally filtered to one bare-name callee."""
    sites: list[_Site] = []
    for module, tree in sorted(trees.items()):
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if callee is not None and not _is_call_to(node, callee):
                continue
            sites.append(_Site(module, owner.get(id(node)), node))
    return sites


def _default_bindings(tree: ast.Module) -> list[tuple[str, ast.expr]]:
    """Every parameter carrying a default, paired with the default expression.

    A default binds a name inside the function just as an assignment does, so
    ``def build(..., constructor=Rewrite)`` renames a guarded symbol without ever assigning it.
    """
    pairs: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = node.args
        positional = [*arguments.posonlyargs, *arguments.args]
        defaulted = positional[len(positional) - len(arguments.defaults) :]
        pairs.extend(
            (parameter.arg, default)
            for parameter, default in zip(defaulted, arguments.defaults, strict=True)
        )
        pairs.extend(
            (parameter.arg, default)
            for parameter, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
            if default is not None
        )
    return pairs


def _rebindings(tree: ast.Module) -> list[tuple[list[str], ast.expr]]:
    """Every name a module rebinds, by assignment or by parameter default."""
    pairs: list[tuple[list[str], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if names:
            pairs.append((names, value))
    pairs.extend(([name], default) for name, default in _default_bindings(tree))
    return pairs


def _names_symbol(expr: ast.expr, symbol: str, aliases: set[str]) -> bool:
    """True when an expression refers to the guarded symbol, by alias or by qualification."""
    if isinstance(expr, ast.Name):
        return expr.id in aliases
    return isinstance(expr, ast.Attribute) and expr.attr == symbol


def _local_aliases(tree: ast.Module, symbol: str) -> frozenset[str]:
    """Every local name in a module that refers to ``symbol``, however it was bound.

    Imports are not the only way to rename a guarded symbol. ``R = Rewrite`` followed by
    ``R(...)``, ``publish = persistence.replace_staged``, and a ``constructor=Rewrite``
    parameter default each rebind it under a name an import-only scan never sees, leaving the
    original site as the apparent sole one. Every rebinding is followed to a fixpoint, so a
    chain of renames resolves too.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == symbol
            )
        elif isinstance(node, ast.ClassDef | ast.FunctionDef) and node.name == symbol:
            aliases.add(symbol)
    rebindings = _rebindings(tree)
    subclasses = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.bases]
    growing = True
    while growing:
        growing = False
        for names, value in rebindings:
            if not _names_symbol(value, symbol, aliases):
                continue
            for name in names:
                if name not in aliases:
                    aliases.add(name)
                    growing = True
        # A subclass inherits the frozen dataclass constructor, so `class Rogue(Rewrite)`
        # followed by `Rogue(...)` mints the guarded type without the call site naming it.
        for node in subclasses:
            inherits = any(_names_symbol(base, symbol, aliases) for base in node.bases)
            if inherits and node.name not in aliases:
                aliases.add(node.name)
                growing = True
    return frozenset(aliases)


def _symbol_call_sites(trees: dict[str, ast.Module], symbol: str) -> list[_Site]:
    """Every call to ``symbol``, resolving import aliases and module-qualified references.

    A bare-name scan would miss both ``from .reconcile import Rewrite as R`` followed by
    ``R(...)`` and ``from . import persistence`` followed by ``persistence.replace_staged(...)``,
    each of which is a complete route around the anchors this guard pins.
    """
    sites: list[_Site] = []
    for module, tree in sorted(trees.items()):
        owner = _enclosing_functions(tree)
        aliases = _local_aliases(tree, symbol)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            named = isinstance(node.func, ast.Name) and node.func.id in aliases
            qualified = isinstance(node.func, ast.Attribute) and node.func.attr == symbol
            if named or qualified:
                sites.append(_Site(module, owner.get(id(node)), node))
    return sites


def _mentions_symbol(tree: ast.Module, symbol: str) -> bool:
    """True when a module names ``symbol`` anywhere: import, bare reference, or attribute."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == symbol for a in node.names):
            return True
        if isinstance(node, ast.Name) and node.id == symbol:
            return True
        if isinstance(node, ast.Attribute) and node.attr == symbol:
            return True
    return False


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    """The function definition named ``name``, or None when the module no longer has one."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _record_opaque(bound: dict[str, list[_Binding]], target: ast.expr, value: ast.expr) -> None:
    """Mark every name under a target shape this reader cannot follow element by element."""
    for node in ast.walk(target):
        if isinstance(node, ast.Name):
            bound.setdefault(node.id, []).append(_Binding(value, OPAQUE_BINDING))


def _record_target(bound: dict[str, list[_Binding]], target: ast.expr, value: ast.expr) -> None:
    """Record every name a single assignment target binds, marking shapes we cannot read."""
    if isinstance(target, ast.Name):
        bound.setdefault(target.id, []).append(_Binding(value))
        return
    if isinstance(target, ast.Tuple | ast.List):
        for index, element in enumerate(target.elts):
            if isinstance(element, ast.Name):
                bound.setdefault(element.id, []).append(_Binding(value, index))
            else:
                # A nested or starred element does not sit at ``index`` of ``value``. Recursing
                # would re-key its names to the outer index and let ``(text, _), applied =
                # apply_reconcile(...)`` pass ``text`` off as the element the gate verified.
                _record_opaque(bound, element, value)
        return
    _record_opaque(bound, target, value)


def _bindings(func: ast.FunctionDef) -> dict[str, list[_Binding]]:
    """Map each name assigned in ``func``'s own scope to every value bound to it."""
    bound: dict[str, list[_Binding]] = {}
    for node in _walk_local(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _record_target(bound, target, node.value)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            if node.value is not None:
                _record_target(bound, node.target, node.value)
        elif isinstance(node, ast.For | ast.AsyncFor):
            _record_target(bound, node.target, node.iter)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _record_target(bound, node.optional_vars, node.context_expr)
    return bound


def _sole_binding(bindings: dict[str, list[_Binding]], name: str) -> _Binding | None:
    """The single plain binding of ``name``, or None when it is rebound or unreadable."""
    bound = bindings.get(name, [])
    if len(bound) != 1 or bound[0].index is not None:
        return None
    return bound[0]


def _top_level_index(func: ast.FunctionDef, target: ast.AST) -> int | None:
    """The index in ``func.body`` of the top-level statement containing ``target``."""
    for index, statement in enumerate(func.body):
        if any(node is target for node in ast.walk(statement)):
            return index
    return None


def _argument(call: ast.Call, name: str, index: int) -> ast.expr | None:
    """The keyword argument ``name`` if present, else the positional at ``index``."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return call.args[index] if len(call.args) > index else None


def _is_line_ending_operand(expr: ast.expr, context: _TraceContext) -> bool:
    """True for one complete line-ending literal, or a local holding the source's own ending.

    The literal must be exactly one ending rather than any run of ending characters. A run
    admits ``new_text.replace("\\n", "\\n\\n")``, which inserts a blank line after every line of
    the gate-verified YAML and body instead of restoring the ending the source was written with.
    """
    if isinstance(expr, ast.Constant):
        return expr.value in LINE_ENDINGS
    if isinstance(expr, ast.Name):
        binding = _sole_binding(context.bindings, expr.id)
        return binding is not None and _is_call_to(binding.value, LINE_ENDING_CALLEE)
    return False


def _is_restoration(call: ast.Call, context: _TraceContext) -> bool:
    """True for a line-ending-only ``.replace()`` or a UTF-8 ``.encode()``.

    Both ``replace`` operands are constrained. Checking only the searched text would admit
    ``new_text.replace("\\n", "CORRUPT")``, which rewrites gate-verified content rather than
    restoring the ending the source was written with.
    """
    if not isinstance(call.func, ast.Attribute):
        return False
    if call.func.attr == "encode":
        first = call.args[0] if call.args else None
        return len(call.args) == 1 and isinstance(first, ast.Constant) and first.value == "utf-8"
    if call.func.attr == "replace":
        return len(call.args) == 2 and all(
            _is_line_ending_operand(argument, context) for argument in call.args
        )
    return False


def _is_envelope_part(expr: ast.expr, root: str) -> bool:
    """True for one of the immutable envelope fields split off the original source."""
    if not (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)):
        return False
    return expr.value.id == root and expr.attr in ENVELOPE_FIELDS


def _is_envelope_literal(node: ast.Constant) -> bool:
    """True for the only literal an envelope reassembly may contribute: a line ending.

    Literal text in the f-string is published byte for byte, so skipping constants outright
    would admit ``f"{parts.prefix}{parts.open_fence}\\nINJECTED{new_meta}"`` and write content
    the gate never verified. Only the fence-terminating newline is a legitimate literal, and
    only exactly one of them: a run would open a blank line the gate never saw.
    """
    return node.value in LINE_ENDINGS


def _traces_to(expr: ast.expr, context: _TraceContext, seen: tuple[str, ...] = ()) -> bool:
    """True when ``expr`` provably carries verified bytes through allowed steps only."""
    if isinstance(expr, ast.Name):
        return _traces_name(expr, context, seen)
    if isinstance(expr, ast.IfExp):
        return _traces_to(expr.body, context, seen) and _traces_to(expr.orelse, context, seen)
    if isinstance(expr, ast.Call):
        if not (context.restoration and _is_restoration(expr, context)):
            return False
        return _traces_to(expr.func.value, context, seen)  # ty: ignore[unresolved-attribute]
    if isinstance(expr, ast.JoinedStr):
        return _reassembles_envelope(expr, context, seen)
    return False


def _traces_name(expr: ast.Name, context: _TraceContext, seen: tuple[str, ...]) -> bool:
    """Resolve a name to the verified value, refusing anything rebound or unreadable."""
    if expr.id in context.trusted:
        return True
    if expr.id in seen:
        return False
    binding = _sole_binding(context.bindings, expr.id)
    if binding is None:
        return False
    return _traces_to(binding.value, context, (*seen, expr.id))


def _reassembles_envelope(
    expr: ast.JoinedStr, context: _TraceContext, seen: tuple[str, ...]
) -> bool:
    """True when an f-string rebuilds the whole document envelope around the verified meta.

    Recording only that some verified value appears is not enough: ``f"{new_meta}"`` emits
    nothing but gate-verified bytes and still drops the fences and the entire body. The whole
    sequence must therefore match ``ENVELOPE_ORDER`` exactly, so each piece is reattached once,
    in place, with the verified metadata between the fences.

    Literals take their place in that sequence rather than being validated and dropped. Checking
    each constant on its own admits ``f"\\n{parts.prefix}..."``, which is a legal line ending in
    an illegal position, and every one of those bytes is published without the gate seeing it.
    """
    if context.envelope_root is None:
        return False
    order: list[str] = []
    for value in expr.values:
        if isinstance(value, ast.Constant):
            if not _is_envelope_literal(value):
                return False
            order.append(NEWLINE_SLOT)
            continue
        if not isinstance(value, ast.FormattedValue):
            return False
        if value.conversion != -1 or value.format_spec is not None:
            return False
        part = value.value
        if _traces_to(part, context, seen):
            order.append(GATED_SLOT)
        elif isinstance(part, ast.Attribute) and _is_envelope_part(part, context.envelope_root):
            order.append(part.attr)
        else:
            return False
    return tuple(order) == ENVELOPE_ORDER


def _gated_result_names(bindings: dict[str, list[_Binding]]) -> frozenset[str]:
    """Names bound exactly once to the text element of an ``apply_reconcile`` call."""
    names = {
        name
        for name, bound in bindings.items()
        if len(bound) == 1 and bound[0].index == 0 and _is_call_to(bound[0].value, GATED_CALLEE)
    }
    return frozenset(names)


def _envelope_root(bindings: dict[str, list[_Binding]]) -> str | None:
    """The single name holding the split frontmatter envelope, or None when it is ambiguous."""
    roots = [
        name
        for name, bound in bindings.items()
        if len(bound) == 1
        and bound[0].index is None
        and _is_call_to(bound[0].value, ENVELOPE_SPLITTER)
    ]
    return roots[0] if len(roots) == 1 else None


def _returned_text(node: ast.Return) -> ast.expr | None:
    """The document element of a ``return text, applied`` pair, when the return has that shape."""
    value = node.value
    if isinstance(value, ast.Tuple) and len(value.elts) == 2:
        return value.elts[0]
    return None


def _returns_no_change(node: ast.Return) -> bool:
    """True when a return reports no change by handing back an empty applied set."""
    value = node.value
    if not (isinstance(value, ast.Tuple) and len(value.elts) == 2):
        return False
    applied = value.elts[1]
    return (
        isinstance(applied, ast.Call)
        and isinstance(applied.func, ast.Name)
        and applied.func.id in ("set", "frozenset")
        and not applied.args
        and not applied.keywords
    )


def _field_copy_sites(trees: dict[str, ast.Module]) -> list[_Site]:
    """Every ``replace(..., after=...)`` call, which mints a Rewrite without naming one.

    ``dataclasses.replace(rewrite, after=data)`` returns a frozen ``Rewrite`` carrying bytes the
    gate never saw and is not a ``Rewrite(...)`` call site, so the sole-producer pin has to read
    the copy route too. ``str.replace`` takes no keyword arguments, so the line-ending
    restoration cannot match here.

    An unpacked mapping fails closed. ``replace(rewrite, **{"after": data})`` names no keyword
    at all, since a ``**`` argument carries ``arg`` of None, so which field it replaces is not
    readable from the call and the copy has to be refused rather than assumed harmless.
    """
    sites: list[_Site] = []
    for site in _call_sites(trees):
        func = site.call.func
        named = isinstance(func, ast.Name) and func.id == COPY_CALLEE
        qualified = isinstance(func, ast.Attribute) and func.attr == COPY_CALLEE
        replaces_after = any(keyword.arg in (AFTER_FIELD, None) for keyword in site.call.keywords)
        if (named or qualified) and replaces_after:
            sites.append(site)
    return sites


def _rewrite_producer_violations(trees: dict[str, ast.Module]) -> list[str]:
    """The sole ``Rewrite(...)`` must live in the producer and carry the gated text."""
    expected = f"{RECONCILE_MODULE}::{PRODUCER}"
    copies = sorted(site.located for site in _field_copy_sites(trees))
    if copies:
        return [
            _gate_msg(
                f"{copies} copy a dataclass with a replacement {AFTER_FIELD!r} field, minting a "
                f"{Rewrite.__name__} outside {expected}"
            )
        ]
    sites = _symbol_call_sites(trees, Rewrite.__name__)
    located = sorted(site.located for site in sites)
    if located != [expected]:
        return [_gate_msg(f"production Rewrite(...) sites are {located}, expected only {expected}")]
    producer = _function(trees[RECONCILE_MODULE], PRODUCER)
    if producer is None:
        return [_gate_msg(f"{RECONCILE_MODULE} no longer defines {PRODUCER}")]
    bindings = _bindings(producer)
    trusted = _gated_result_names(bindings)
    if not trusted:
        return [_gate_msg(f"{PRODUCER} does not bind the text {GATED_CALLEE}() returned")]
    after = _argument(sites[0].call, "after", AFTER_FIELD_INDEX)
    context = _TraceContext(trusted, bindings, restoration=True)
    if after is None or not _traces_to(after, context):
        return [
            _gate_msg(
                f"Rewrite.after in {PRODUCER} does not derive from the text {GATED_CALLEE}() "
                "returned through line-ending restoration and UTF-8 encoding alone"
            )
        ]
    return []


def _gated_text_violations(trees: dict[str, ast.Module]) -> list[str]:
    """Every changed return of the gated callee must follow the gate and carry its verified text."""
    func = _function(trees[RECONCILE_MODULE], GATED_CALLEE)
    if func is None:
        return [_gate_msg(f"{RECONCILE_MODULE} no longer defines {GATED_CALLEE}")]
    gates = [node for node in _walk_local(func) if _is_call_to(node, GATE)]
    if len(gates) != 1:
        return [
            _gate_msg(f"{GATED_CALLEE}() calls {GATE} {len(gates)} times, expected exactly one")
        ]
    gate_index = _top_level_index(func, gates[0])
    verified = gates[0].args[0] if gates[0].args else None
    # The gate must be its own unconditional top-level statement. Accepting a gate merely
    # contained in an earlier statement would pass a gate nested in a conditional, which does
    # not run on every path that reaches a later changed return.
    statement = func.body[gate_index] if gate_index is not None else None
    unconditional = isinstance(statement, ast.Expr) and statement.value is gates[0]
    if gate_index is None or not unconditional or not isinstance(verified, ast.Name):
        return [
            _gate_msg(
                f"{GATE} is not an unconditional top-level statement of {GATED_CALLEE}() called "
                "on a plain local, so it does not dominate every later return"
            )
        ]
    bindings = _bindings(func)
    if _sole_binding(bindings, verified.id) is None:
        return [_gate_msg(f"{GATED_CALLEE}() rebinds {verified.id!r} after {GATE} verified it")]
    context = _TraceContext(
        frozenset({verified.id}), bindings, envelope_root=_envelope_root(bindings)
    )
    return _changed_return_violations(func, gate_index, context)


def _changed_return_violations(
    func: ast.FunctionDef, gate_index: int, context: _TraceContext
) -> list[str]:
    """Report every possibly changed return that precedes the gate or leaves its verified text."""
    problems: list[str] = []
    for node in _walk_local(func):
        if not isinstance(node, ast.Return) or _returns_no_change(node):
            continue
        index = _top_level_index(func, node)
        text = _returned_text(node)
        if index is None or index <= gate_index:
            problems.append(
                _gate_msg(
                    f"{func.name}() returns a possibly changed document at line {node.lineno}, "
                    f"ahead of {GATE}"
                )
            )
        elif text is None or not _traces_to(text, context):
            problems.append(
                _gate_msg(
                    f"the document {func.name}() returns at line {node.lineno} is not the text "
                    f"{GATE} verified"
                )
            )
    return problems


def _mentions_after_infix(
    expr: ast.expr, bindings: dict[str, list[_Binding]], seen: tuple[str, ...] = ()
) -> bool:
    """True when an expression names the after-image infix, directly or through a local.

    Resolving locals matters: ``reconcile_transaction.py`` already binds the infix to a name
    before building a prefix from it, so a bare-name scan of the ``prefix`` argument would let a
    second staging site hide behind exactly the shape the module uses elsewhere.
    """
    for node in ast.walk(expr):
        if not isinstance(node, ast.Name):
            continue
        if node.id == AFTER_IMAGE_INFIX:
            return True
        if node.id in seen:
            continue
        binding = _sole_binding(bindings, node.id)
        if binding is not None and _mentions_after_infix(binding.value, bindings, (*seen, node.id)):
            return True
    return False


def _stages_after_bytes(call: ast.Call) -> bool:
    """True for a call whose staged operand is some record's ``after`` field."""
    staged = _argument(call, "data", 1)
    return isinstance(staged, ast.Attribute) and staged.attr == AFTER_FIELD


def _prefix_argument(call: ast.Call) -> ast.expr | None:
    """The ``prefix`` keyword argument of a staging call, when it has one."""
    return next((keyword.value for keyword in call.keywords if keyword.arg == "prefix"), None)


def _is_produced_rewrite_after(expr: ast.expr | None, bindings: dict[str, list[_Binding]]) -> bool:
    """True for ``<name>.after`` where ``<name>`` is bound once to a pending entry's rewrite."""
    if not (isinstance(expr, ast.Attribute) and expr.attr == AFTER_FIELD):
        return False
    if not isinstance(expr.value, ast.Name):
        return False
    binding = _sole_binding(bindings, expr.value.id)
    if binding is None:
        return False
    return isinstance(binding.value, ast.Attribute) and binding.value.attr == "rewrite"


def _enclosing_bindings(
    trees: dict[str, ast.Module],
    site: _Site,
    cache: dict[tuple[str, str | None], dict[str, list[_Binding]]],
) -> dict[str, list[_Binding]]:
    """Every binding in the function enclosing a call site, empty outside any function."""
    key = (site.module, site.function)
    if key not in cache:
        func = _function(trees[site.module], site.function) if site.function else None
        cache[key] = _bindings(func) if func is not None else {}
    return cache[key]


def _after_image_staging_sites(trees: dict[str, ast.Module]) -> list[_Site]:
    """Every call that stages an after image, read by staged operand and by prefix infix.

    Both readings are needed. The prefix reading alone misses a site that stages after bytes
    under a prefix it computed some other way; the operand reading alone misses a site that
    stages after bytes it had already bound to an unrelated name.
    """
    cache: dict[tuple[str, str | None], dict[str, list[_Binding]]] = {}
    sites: list[_Site] = []
    for site in _call_sites(trees):
        if _stages_after_bytes(site.call):
            sites.append(site)
            continue
        prefix = _prefix_argument(site.call)
        if prefix is not None and _mentions_after_infix(
            prefix, _enclosing_bindings(trees, site, cache)
        ):
            sites.append(site)
    return sites


def _staging_violations(trees: dict[str, ast.Module]) -> list[str]:
    """The sole after-image staging site must stage the produced rewrite's own bytes."""
    expected = f"{TRANSACTION_MODULE}::{STAGING_FUNCTION}"
    sites = _after_image_staging_sites(trees)
    located = sorted(site.located for site in sites)
    if located != [expected]:
        return [_gate_msg(f"after-image staging sites are {located}, expected only {expected}")]
    if not _is_call_to(sites[0].call, STAGE_CALLEE):
        return [_gate_msg(f"the after image at {expected} is not staged by {STAGE_CALLEE}()")]
    func = _function(trees[TRANSACTION_MODULE], STAGING_FUNCTION)
    if func is None:
        return [_gate_msg(f"{TRANSACTION_MODULE} no longer defines {STAGING_FUNCTION}")]
    if not _is_produced_rewrite_after(_argument(sites[0].call, "data", 1), _bindings(func)):
        return [
            _gate_msg(
                f"{expected} stages an after image that is not the produced Rewrite's own bytes"
            )
        ]
    return []


def _publication_reach_violations(trees: dict[str, ast.Module]) -> list[str]:
    """Only the reconcile transaction may reach the publication helper it does not define.

    ``persistence.py`` owns ``replace_staged`` and calls it internally as the generic
    atomic-write primitive, so the sink rules below read only the transaction module. That
    scoping is sound exactly while no other module can reach the helper, which is what this
    check pins: a new module naming it at all is a new publication route and fails here instead
    of slipping past a sink audit that never looked at it.

    Reaching it indirectly counts, and so does bypassing it. The composite primitive in
    ``COMPOSITE_PUBLISH_USERS`` stages and publishes one destination in a single call, so a
    module can overwrite a reconcile destination through it without ever naming
    ``replace_staged``. Its current users publish their own artifacts rather than documents and
    are pinned per primitive, so a new user of that route fails here too.

    The check reads every mention of the identifier, not just a ``from ... import`` of it. A
    module can reach the helper as ``from . import persistence`` followed by
    ``persistence.replace_staged(...)``, which no import-name scan would see.

    ``persistence.py`` itself stays exempt from both this rule and the sink audit, so a forward
    sink added inside it is outside the guard. Narrowing that exemption would fire on the
    module's own correct internal use of the helper; AD-30 records the limit.
    """
    reaching = sorted(
        module
        for module, tree in trees.items()
        if module not in PUBLICATION_OWNERS
        and (
            _mentions_symbol(tree, PUBLISH_CALLEE)
            or any(
                module not in users and _mentions_symbol(tree, callee)
                for callee, users in COMPOSITE_PUBLISH_USERS.items()
            )
        )
    )
    if not reaching:
        return []
    pinned = sorted(name for users in COMPOSITE_PUBLISH_USERS.values() for name in users)
    return [
        _gate_msg(
            f"modules publishing through {PUBLISH_CALLEE}() or "
            f"{sorted(COMPOSITE_PUBLISH_USERS)}, outside {sorted(PUBLICATION_OWNERS)} and the "
            f"pinned users {pinned}, are {reaching}, expected none"
        )
    ]


def _same_entry(staged: ast.expr | None, destination: ast.Attribute) -> bool:
    """True when both publication operands read fields of the very same journal entry.

    Matching only the two attribute names lets ``replace_staged(prepared.entries[0].after_path,
    entry.destination)`` pass, which authenticates the entry the commit loop is on and then
    publishes the first document's bytes over every destination it visits.
    """
    if not isinstance(staged, ast.Attribute):
        return False
    return ast.dump(staged.value) == ast.dump(destination.value)


@dataclass(frozen=True)
class _DestinationScope:
    """What one function reveals about which of its names can yield a destination.

    A destination does not stay in the field it was read from. Storing one in a container and
    reading it back, by subscript or by iteration, produces the same path under a name the
    field scan alone cannot see, so containers that ever received one are tracked as tainted
    and the names their elements bind to are tracked with them.
    """

    bindings: dict[str, list[_Binding]]
    containers: frozenset[str]
    elements: frozenset[str]


def _is_destination(expr: ast.expr, scope: _DestinationScope, seen: tuple[str, ...] = ()) -> bool:
    """True for a journal entry's destination, named directly or reached through a local.

    Every binding of a name is read, not only a sole one. Resolving through ``_sole_binding``
    would treat a rebound name as safe rather than as ambiguous, and ``target = entry.destination``
    followed by ``target = target`` still holds the destination at runtime while defeating that
    reading. A name that could have held one stays tainted.
    """
    if isinstance(expr, ast.Attribute) and expr.attr == DESTINATION_FIELD:
        return True
    if isinstance(expr, ast.Subscript):
        return isinstance(expr.value, ast.Name) and expr.value.id in scope.containers
    if not isinstance(expr, ast.Name) or expr.id in seen:
        return False
    if expr.id in scope.elements:
        return True
    return any(
        _is_destination(binding.value, scope, (*seen, expr.id))
        for binding in scope.bindings.get(expr.id, [])
    )


def _display_elements(value: ast.expr) -> list[ast.expr]:
    """Every element a collection display holds, or nothing for anything else."""
    if isinstance(value, ast.List | ast.Set | ast.Tuple):
        return list(value.elts)
    if isinstance(value, ast.Dict):
        return [element for element in value.values if element is not None]
    return []


def _iterates_destinations(expr: ast.expr, scope: _DestinationScope) -> bool:
    """True when iterating an expression yields destinations, through one wrapper at most."""
    if isinstance(expr, ast.Name):
        return expr.id in scope.containers
    if any(_is_destination(element, scope) for element in _display_elements(expr)):
        return True
    unwrapped = expr.args[0] if isinstance(expr, ast.Call) and len(expr.args) == 1 else None
    return unwrapped is not None and _iterates_destinations(unwrapped, scope)


def _taints_container(call: ast.Call, scope: _DestinationScope) -> str | None:
    """The local collection a call records a destination into, when it is one."""
    receiver = call.func
    if not (isinstance(receiver, ast.Attribute) and receiver.attr in COLLECTION_ACCUMULATORS):
        return None
    target = receiver.value
    if not isinstance(target, ast.Name) or not _is_local_collection(target, scope.bindings):
        return None
    if not any(_is_destination(argument, scope) for argument in call.args):
        return None
    return target.id


def _destination_scope(func: ast.FunctionDef | None) -> _DestinationScope:
    """Resolve every name in a function that can yield a destination, containers included.

    Taint and destination provenance are mutually recursive, since a container is tainted by
    receiving a destination and a subscript of a tainted container is itself a destination, so
    both are grown together to a fixpoint.
    """
    if func is None:
        return _DestinationScope({}, frozenset(), frozenset())
    bindings = _bindings(func)
    local = list(_walk_local(func))
    calls = [node for node in local if isinstance(node, ast.Call)]
    loops = [node for node in local if isinstance(node, ast.For | ast.AsyncFor)]
    containers: set[str] = set()
    elements: set[str] = set()
    growing = True
    while growing:
        growing = False
        scope = _DestinationScope(bindings, frozenset(containers), frozenset(elements))
        for name in bindings:
            binding = _sole_binding(bindings, name)
            if name in containers or binding is None:
                continue
            if any(_is_destination(held, scope) for held in _display_elements(binding.value)):
                containers.add(name)
                growing = True
        for call in calls:
            tainted = _taints_container(call, scope)
            if tainted is not None and tainted not in containers:
                containers.add(tainted)
                growing = True
        for loop in loops:
            target = loop.target
            if not isinstance(target, ast.Name) or target.id in elements:
                continue
            if _iterates_destinations(loop.iter, scope):
                elements.add(target.id)
                growing = True
    return _DestinationScope(bindings, frozenset(containers), frozenset(elements))


def _enclosing_scope(
    trees: dict[str, ast.Module],
    site: _Site,
    cache: dict[tuple[str, str | None], _DestinationScope],
) -> _DestinationScope:
    """The destination scope of the function enclosing a call site, resolved once per function."""
    key = (site.module, site.function)
    if key not in cache:
        func = _function(trees[site.module], site.function) if site.function else None
        cache[key] = _destination_scope(func)
    return cache[key]


def _is_local_collection(expr: ast.expr, bindings: dict[str, list[_Binding]]) -> bool:
    """True for a name bound once to a list, set, or dict built in the same function."""
    if not isinstance(expr, ast.Name):
        return False
    binding = _sole_binding(bindings, expr.id)
    if binding is None:
        return False
    value = binding.value
    if isinstance(value, ast.List | ast.Set | ast.Dict | ast.ListComp | ast.SetComp | ast.DictComp):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in COLLECTION_BUILDERS
    )


def _accumulates_destination(call: ast.Call, scope: _DestinationScope) -> bool:
    """True when a call only records its arguments into a local collection.

    Classification and rollback outcome bookkeeping collect destinations to report on them,
    which reaches no filesystem. The receiver has to be a provable local collection rather than
    anything answering to ``append``, so a writer cannot borrow the exemption by method name.
    Storing a destination this way does not launder it: the collection is tainted from here, so
    reading an element back out of it is a destination again.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in COLLECTION_ACCUMULATORS):
        return False
    return _is_local_collection(func.value, scope.bindings)


def _callee_name(call: ast.Call) -> str | None:
    """The bare name of whatever a call invokes, ignoring how it was qualified."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _destination_write_violations(trees: dict[str, ast.Module]) -> list[str]:
    """Nothing in the transaction module may reach a destination but a pinned reader.

    The sink audit resolves ``replace_staged``, so a route that never names it is invisible
    there: ``os.replace(entry.after_path, entry.destination)`` and
    ``entry.destination.write_bytes(data)`` both publish arbitrary bytes over a document while
    the expected call stays in place. Enumerating write primitives cannot close that, since a
    new sink can name one this guard has never heard of, so the audit keys on the destination
    instead and every callee that reaches one is classified in ``DESTINATION_READERS``.

    A destination reached through a container counts. Storing one in a list and reading it back
    by subscript or by iteration produces the same path under a name no field scan would match,
    so ``_DestinationScope`` tracks tainted containers and the names their elements bind to.

    The scan covers ``reconcile_transaction.py`` alone, the only module that owns reconcile
    destinations. Elsewhere a ``.destination`` field belongs to an unrelated domain, and the
    reach rule already pins which modules may publish at all.
    """
    scoped = {TRANSACTION_MODULE: trees[TRANSACTION_MODULE]}
    cache: dict[tuple[str, str | None], _DestinationScope] = {}
    problems: list[str] = []
    for site in _call_sites(scoped):
        scope = _enclosing_scope(scoped, site, cache)
        func = site.call.func
        if isinstance(func, ast.Attribute) and _is_destination(func.value, scope):
            problems.append(
                _gate_msg(
                    f"{site.located} calls {func.attr}() on a reconcile destination, which no "
                    "pinned reader does"
                )
            )
            continue
        # The exemption is a bare name resolved in this module, never a terminal attribute:
        # `writer.file_sha256(entry.destination)` borrows a pinned reader's name for an
        # unrelated method that could publish over the path it is handed.
        pinned = isinstance(func, ast.Name) and func.id in DESTINATION_READERS
        if pinned or _accumulates_destination(site.call, scope):
            continue
        name = _callee_name(site.call)
        arguments = [*site.call.args, *(keyword.value for keyword in site.call.keywords)]
        if any(_is_destination(argument, scope) for argument in arguments):
            problems.append(
                _gate_msg(
                    f"{site.located} hands a reconcile destination to {name}(), which is not a "
                    f"pinned reader in {sorted(DESTINATION_READERS)}"
                )
            )
    return problems


def _publication_violations(trees: dict[str, ast.Module]) -> list[str]:
    """One forward document-publication sink; the before-image rollback sink stays exempt."""
    problems = _publication_reach_violations(trees)
    problems.extend(_destination_write_violations(trees))
    roles: dict[str, list[str]] = {FORWARD_IMAGE_FIELD: [], ROLLBACK_IMAGE_FIELD: []}
    scoped = {TRANSACTION_MODULE: trees[TRANSACTION_MODULE]}
    for site in _symbol_call_sites(scoped, PUBLISH_CALLEE):
        staged = site.call.args[0] if site.call.args else None
        destination = site.call.args[1] if len(site.call.args) > 1 else None
        role = staged.attr if isinstance(staged, ast.Attribute) else None
        if not (isinstance(destination, ast.Attribute) and destination.attr == DESTINATION_FIELD):
            problems.append(
                _gate_msg(
                    f"{PUBLISH_CALLEE}() at {site.located} does not publish over a journal entry "
                    "destination"
                )
            )
        elif not _same_entry(staged, destination):
            problems.append(
                _gate_msg(
                    f"{PUBLISH_CALLEE}() at {site.located} takes its image and its destination "
                    "from different journal entry expressions"
                )
            )
        elif role in roles:
            roles[role].append(site.located)
        else:
            problems.append(
                _gate_msg(
                    f"{PUBLISH_CALLEE}() at {site.located} publishes an image that is neither the "
                    "staged after image nor the rollback before image"
                )
            )
    # Only the forward route is pinned. Before images are deliberately outside the invariant,
    # so a second rollback sink is correct code and must not fail here; it is still required to
    # classify as a rollback above, which is what keeps a forward route from hiding in it.
    expected = [f"{TRANSACTION_MODULE}::{FORWARD_PUBLISHER}"]
    forward = sorted(roles[FORWARD_IMAGE_FIELD])
    if forward != expected:
        problems.append(
            _gate_msg(
                f"document-publication sinks consuming {FORWARD_IMAGE_FIELD} are {forward}, "
                f"expected only {expected}"
            )
        )
    return problems


def _provenance_violations(sources: dict[str, str]) -> list[str]:
    """Every way the given production source lets bytes reach a document without the gate."""
    trees = {module: ast.parse(source) for module, source in sources.items()}
    # Every rule below indexes these two modules directly, so report a move as the guard
    # diagnostic it is instead of letting the suite die on a bare KeyError.
    missing = sorted({RECONCILE_MODULE, TRANSACTION_MODULE} - trees.keys())
    if missing:
        return [_gate_msg(f"the modules this guard reads, {missing}, are no longer where it looks")]
    return [
        *_rewrite_producer_violations(trees),
        *_gated_text_violations(trees),
        *_staging_violations(trees),
        *_publication_violations(trees),
    ]


def test_reconcile_after_images_flow_through_the_reparse_gate():
    """Production source keeps every reconcile after image on the gate-verified text path."""
    violations = _provenance_violations(_production_sources())
    assert not violations, "\n".join(violations)


ROGUE_PRODUCER_MODULE = '''"""A reconcile producer that never consults the reparse gate."""

from pathlib import Path

from .reconcile import Rewrite


def build(path: Path, before: bytes, data: bytes) -> Rewrite:
    """Construct a Rewrite from arbitrary bytes."""
    return Rewrite(path, before, data, frozenset())
'''

EXTRA_STAGING_SITE = '''

def _rogue_stage_after_image(destination: Path, rewrite: Rewrite) -> Path:
    """Stage a second after image outside the prepared transaction."""
    return stage_bytes(
        destination,
        rewrite.after,
        prefix=f".{destination.name}{RECONCILE_AFTER_IMAGE_INFIX}",
    )
'''

EXTRA_PUBLICATION_SINK = '''

def _rogue_publish(entry: JournalEntry) -> None:
    """Publish a document over its destination outside the commit path."""
    replace_staged(entry.after_path, entry.destination)
'''

ROGUE_PUBLISHER_MODULE = '''"""A module that reaches the publication helper on its own."""

from .persistence import replace_staged


def publish(entry: object) -> None:
    """Publish a document over its destination from outside the transaction."""
    replace_staged(entry.after_path, entry.destination)
'''

QUALIFIED_PUBLISHER_MODULE = '''"""A module reaching the publication helper through its module."""

from . import persistence


def publish(entry: object) -> None:
    """Publish a document over its destination through a qualified call."""
    persistence.replace_staged(entry.after_path, entry.destination)
'''

ALIASED_PRODUCER_MODULE = '''"""A reconcile producer hiding behind an import alias."""

from pathlib import Path

from .reconcile import Rewrite as R


def build(path: Path, before: bytes, data: bytes) -> R:
    """Construct a Rewrite under an alias, without consulting the reparse gate."""
    return R(path, before, data, frozenset())
'''

INDIRECT_INFIX_STAGING_SITE = '''

def _rogue_stage_indirect(destination: Path, data: bytes) -> Path:
    """Stage a second after image behind a locally bound infix."""
    infix = RECONCILE_AFTER_IMAGE_INFIX
    return stage_bytes(destination, data, prefix=f".{destination.name}{infix}")
'''

COMPOSITE_PUBLISHER_MODULE = '''"""A module publishing through the composite primitive."""

from pathlib import Path

from .persistence import atomic_replace_bytes


def publish(destination: Path, data: bytes) -> None:
    """Publish arbitrary bytes over a reconcile destination without naming the helper."""
    atomic_replace_bytes(destination, data, prefix=".rogue")
'''

# The forward sink the controls below bend, with the indentation its statement sits at, so an
# added statement lands in the same block rather than reindenting the module into a syntax error.
FORWARD_SINK_CALL = "replace_staged(entry.after_path, entry.destination)"
SINK_INDENT = " " * 12

# The production envelope reassembly, as the two implicitly concatenated pieces it is written in,
# so a control can bend the whole document assembly rather than one interpolation of it.
ENVELOPE_ASSEMBLY = (
    'f"{parts.prefix}{parts.open_fence}\\n{new_meta}"\n'
    '        f"{parts.close_fence}{parts.close_fence_newline}{parts.body}"'
)
REORDERED_ENVELOPE_ASSEMBLY = (
    'f"{parts.prefix}{parts.open_fence}\\n{new_meta}"\n'
    '        f"{parts.body}{parts.close_fence}{parts.close_fence_newline}"'
)

UNPACKED_COPY_PRODUCER_MODULE = '''"""A producer copying a Rewrite through an unpacked mapping."""

import dataclasses

from .reconcile import Rewrite


def corrupt(rewrite: Rewrite, data: bytes) -> Rewrite:
    """Return a Rewrite carrying bytes the reparse gate never saw."""
    return dataclasses.replace(rewrite, **{"after": data})
'''

SUBCLASS_PRODUCER_MODULE = '''"""A producer minting an ungated Rewrite through a subclass."""

from pathlib import Path

from .reconcile import Rewrite


class Rogue(Rewrite):
    """Inherits the frozen dataclass constructor without naming it at the call site."""


def build(path: Path, before: bytes, data: bytes) -> Rewrite:
    """Mint a Rewrite the gate never saw."""
    return Rogue(path, before, data, frozenset())
'''

PARAMETER_DEFAULT_PRODUCER_MODULE = '''"""A producer hidden behind a parameter default."""

from pathlib import Path

from .reconcile import Rewrite


def build(path: Path, before: bytes, data: bytes, constructor=Rewrite) -> Rewrite:
    """Mint a Rewrite the gate never saw, naming the class only as a default."""
    return constructor(path, before, data, frozenset())
'''

ASSIGNED_ALIAS_PRODUCER_MODULE = '''"""A producer hidden behind an assigned alias."""

from pathlib import Path

from .reconcile import Rewrite

R = Rewrite


def build(path: Path, before: bytes, data: bytes) -> Rewrite:
    """Mint a Rewrite the gate never saw, naming the class only in an assignment."""
    return R(path, before, data, frozenset())
'''

FIELD_COPY_PRODUCER_MODULE = '''"""A producer that copies a Rewrite instead of building one."""

import dataclasses

from .reconcile import Rewrite


def corrupt(rewrite: Rewrite, data: bytes) -> Rewrite:
    """Return a Rewrite carrying bytes the reparse gate never saw."""
    return dataclasses.replace(rewrite, after=data)
'''


def _patched(sources: dict[str, str], module: str, old: str, new: str) -> dict[str, str]:
    """Sources with one anchored edit applied, asserting the anchor still exists."""
    source = sources[module]
    assert old in source, f"positive control anchor is stale in {module}: {old!r}"
    return {**sources, module: source.replace(old, new, 1)}


def _extended(sources: dict[str, str], module: str, addition: str) -> dict[str, str]:
    """Sources with ``addition`` appended to one module."""
    return {**sources, module: sources[module] + addition}


@cache
def _positive_controls() -> dict[str, dict[str, str]]:
    """Each named control bends production source into one distinct bypass of the gate.

    The first seven cover the modes the issue enumerated; the second-forward-publication mode
    is exercised both inside the transaction module and from a fresh module. The next five were
    found by adversarial review of this guard and each was reproduced against it before being
    closed: a gate nested in a conditional, a line-ending restoration that corrupts through its
    replacement operand, unverified ``raw_meta`` reattached to the envelope, the publication
    helper reached through its module rather than by name, and a producer hidden behind an
    import alias. The last four came from a second adversarial pass, likewise reproduced first:
    literal text spliced into the envelope f-string, an after image staged behind a locally bound
    infix, a document published through ``atomic_replace_bytes`` without ever naming the
    publication helper, and a Rewrite minted by ``dataclasses.replace`` rather than constructed.
    The last five came from external review of this guard, again each reproduced first: a
    restoration replacing one newline with two, a sink pairing one entry's staged image with
    another entry's destination, a destination republished by a raw ``os.replace``, a destination
    overwritten through its own write method, and a producer behind an alias introduced by
    assignment rather than by import. The last records a destination into something that is not a
    provable local collection, pinning that the bookkeeping exemption cannot be borrowed by
    anything that merely answers to ``append``. The next three came from a further review pass:
    the whole document envelope dropped around the verified metadata, the envelope reattached out
    of order, and a producer behind an alias bound as a parameter default. The next four close the
    routes that launder a guarded value through another shape: a producer subclassing the record
    to inherit its constructor, and a destination republished after a container round trip, by
    subscript of a literal, by subscript of a bookkeeping accumulation, and by iterating one back.
    The final four close what a matcher reads rather than what it matches: a legal line ending in
    an illegal envelope position, a field copy whose keyword is unreadable because it arrives
    unpacked, a destination surviving under a rebound name, and an unrelated method borrowing a
    pinned reader's terminal name.

    Cached alongside the source read so the parametrized run builds this mapping once.
    """
    sources = _production_sources()
    return {
        "extra-after-image-staging-site": _extended(
            sources, TRANSACTION_MODULE, EXTRA_STAGING_SITE
        ),
        "second-forward-publication-sink": _extended(
            sources, TRANSACTION_MODULE, EXTRA_PUBLICATION_SINK
        ),
        "publication-helper-reached-from-a-new-module": {
            **sources,
            "rogue_publisher.py": ROGUE_PUBLISHER_MODULE,
        },
        "ungated-rewrite-producer": {**sources, "rogue_producer.py": ROGUE_PRODUCER_MODULE},
        "rewrite-built-from-unrelated-bytes": _patched(
            sources, RECONCILE_MODULE, 'after.encode("utf-8")', "before"
        ),
        "changed-output-returned-before-the-gate": _patched(
            sources,
            RECONCILE_MODULE,
            "    new_meta = _apply_source_edits(raw_meta, edits)",
            "    if plan.anchored_seen:\n"
            "        return current_file_text, set(plan.applied)\n"
            "    new_meta = _apply_source_edits(raw_meta, edits)",
        ),
        "changed-output-substituted-after-the-gate": _patched(
            sources,
            RECONCILE_MODULE,
            "    return rewritten, set(plan.applied)",
            "    rewritten = current_file_text\n    return rewritten, set(plan.applied)",
        ),
        "gate-nested-in-a-conditional": _patched(
            sources,
            RECONCILE_MODULE,
            "    _verify_reconciled_meta("
            "new_meta, _expected_frontmatter(data, plan.entry_updates), source)",
            "    if plan.entry_updates:\n"
            "        _verify_reconciled_meta(\n"
            "            new_meta, _expected_frontmatter(data, plan.entry_updates), source\n"
            "        )",
        ),
        "line-ending-restoration-that-corrupts": _patched(
            sources,
            RECONCILE_MODULE,
            'after = new_text if ending == "\\n" else new_text.replace("\\n", ending)',
            'after = new_text.replace("\\n", "CORRUPT")',
        ),
        "unverified-raw-meta-reattached-to-the-envelope": _patched(
            sources,
            RECONCILE_MODULE,
            'f"{parts.prefix}{parts.open_fence}\\n{new_meta}"',
            'f"{parts.prefix}{parts.open_fence}\\n{new_meta}{parts.raw_meta}"',
        ),
        "publication-helper-reached-through-its-module": {
            **sources,
            "rogue_qualified_publisher.py": QUALIFIED_PUBLISHER_MODULE,
        },
        "rewrite-producer-hidden-behind-an-alias": {
            **sources,
            "rogue_aliased_producer.py": ALIASED_PRODUCER_MODULE,
        },
        "literal-text-spliced-into-the-envelope": _patched(
            sources,
            RECONCILE_MODULE,
            'f"{parts.prefix}{parts.open_fence}\\n{new_meta}"',
            'f"{parts.prefix}{parts.open_fence}\\nINJECTED{new_meta}"',
        ),
        "after-image-staged-behind-a-bound-infix": _extended(
            sources, TRANSACTION_MODULE, INDIRECT_INFIX_STAGING_SITE
        ),
        "document-published-through-the-composite-primitive": {
            **sources,
            "rogue_composite_publisher.py": COMPOSITE_PUBLISHER_MODULE,
        },
        "rewrite-minted-by-a-dataclass-field-copy": {
            **sources,
            "rogue_field_copy_producer.py": FIELD_COPY_PRODUCER_MODULE,
        },
        "line-ending-restoration-that-doubles-every-newline": _patched(
            sources,
            RECONCILE_MODULE,
            'new_text.replace("\\n", ending)',
            'new_text.replace("\\n", "\\n\\n")',
        ),
        "image-and-destination-taken-from-different-entries": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            "replace_staged(prepared.entries[0].after_path, entry.destination)",
        ),
        "destination-republished-by-a-raw-replace-primitive": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}os.replace(entry.after_path, entry.destination)",
        ),
        "destination-overwritten-through-its-own-write-method": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}entry.destination.write_bytes(b'')",
        ),
        "destination-accumulated-into-something-that-is-not-a-collection": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}prepared.append(entry.destination)",
        ),
        "rewrite-minted-behind-an-assigned-alias": {
            **sources,
            "rogue_assigned_alias_producer.py": ASSIGNED_ALIAS_PRODUCER_MODULE,
        },
        "document-envelope-dropped-around-the-verified-metadata": _patched(
            sources, RECONCILE_MODULE, ENVELOPE_ASSEMBLY, 'f"{new_meta}"'
        ),
        "document-envelope-reattached-out-of-order": _patched(
            sources, RECONCILE_MODULE, ENVELOPE_ASSEMBLY, REORDERED_ENVELOPE_ASSEMBLY
        ),
        "rewrite-minted-behind-a-parameter-default": {
            **sources,
            "rogue_parameter_default_producer.py": PARAMETER_DEFAULT_PRODUCER_MODULE,
        },
        "rewrite-minted-by-a-subclass-of-the-record": {
            **sources,
            "rogue_subclass_producer.py": SUBCLASS_PRODUCER_MODULE,
        },
        "destination-republished-after-a-container-round-trip": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}destinations = [entry.destination]"
            f"\n{SINK_INDENT}os.replace(entry.before_path, destinations[0])",
        ),
        "destination-republished-after-bookkeeping-accumulation": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}seen = []"
            f"\n{SINK_INDENT}seen.append(entry.destination)"
            f"\n{SINK_INDENT}os.replace(entry.before_path, seen[0])",
        ),
        "destination-republished-after-being-iterated-back": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}destinations = [entry.destination]"
            f"\n{SINK_INDENT}for target in destinations:"
            f"\n{SINK_INDENT}    os.replace(entry.before_path, target)",
        ),
        "envelope-line-ending-spliced-into-an-illegal-position": _patched(
            sources,
            RECONCILE_MODULE,
            'f"{parts.prefix}{parts.open_fence}\\n{new_meta}"',
            'f"\\n{parts.prefix}{parts.open_fence}\\n{new_meta}"',
        ),
        "rewrite-minted-by-an-unpacked-field-copy": {
            **sources,
            "rogue_unpacked_copy_producer.py": UNPACKED_COPY_PRODUCER_MODULE,
        },
        "destination-republished-through-a-rebound-name": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}target = entry.destination"
            f"\n{SINK_INDENT}target = target"
            f"\n{SINK_INDENT}os.replace(entry.before_path, target)",
        ),
        "destination-handed-to-a-borrowed-reader-name": _patched(
            sources,
            TRANSACTION_MODULE,
            FORWARD_SINK_CALL,
            f"{FORWARD_SINK_CALL}\n{SINK_INDENT}writer.file_sha256(entry.destination)",
        ),
    }


@pytest.mark.parametrize("control", sorted(_positive_controls()))
def test_provenance_guard_rejects_each_bypass(control):
    """Positive control: the detector fires on every known way around the gate."""
    violations = _provenance_violations(_positive_controls()[control])
    assert violations, f"the provenance guard passed vacuously on {control}"
    assert all(GATE in violation for violation in violations), violations


NON_INVARIANT_ADDITIONS = '''

def _extra_before_image(destination: Path, rewrite: Rewrite) -> Path:
    """Stage a second before image, which the reparse gate deliberately does not cover."""
    return stage_bytes(
        destination,
        rewrite.before,
        prefix=f".{destination.name}{RECONCILE_BEFORE_IMAGE_INFIX}",
    )


def _extra_rollback(entry: JournalEntry) -> None:
    """Restore a destination from its before image, which is rollback, not publication."""
    replace_staged(entry.before_path, entry.destination)
'''


def test_provenance_guard_leaves_before_images_and_rollback_alone():
    """Negative control: before images and rollback stay outside the asserted invariant."""
    sources = _extended(_production_sources(), TRANSACTION_MODULE, NON_INVARIANT_ADDITIONS)
    violations = _provenance_violations(sources)
    assert not violations, "\n".join(violations)


# GTX-125: the bug class is an omitted construction site, not a wrong renderer, so a behavioral
# per-sink list cannot close it -- a sink added tomorrow is not in that list. This guard is the
# static half: inside the modules GTX-125 owns, a path-typed expression may not be interpolated
# into an f-string unless it goes through `format_path_for_display`. See AD-34.
#
# Names treated as path-typed. Deliberately a name heuristic rather than inferred types: the
# suite has no type resolver, and every sink in the owned modules spells its path with one of
# these. A false positive is cheap (wrap it, or add it to the exemptions below with a reason).
# Every component of a dotted expression is tested, not only the last one, so `{path.name}` --
# the exact shape the reconcile adapter's basename sink had before this change -- is caught
# rather than read as an interpolation of something called `name`.
_PATH_BEARING_NAMES = frozenset(
    {"path", "source", "destination", "journal", "link", "cwd", "staged"}
)

# GTX-209: names that are path-typed only inside one module. Scoping them is what lets the
# transaction and recovery sinks be enforced without the global set growing terms that are
# paths there and something else elsewhere -- `current` is an ancestor directory in
# `reconcile_transaction.py` and would read as a cursor or a counter anywhere else, and
# `filename` is a scan failure's path component in the reconcile adapter and an ordinary
# attribute name in general.
#
# `detail` is deliberately absent from every set, even though it names a real sink. In
# `reconcile_transaction.py` it holds text `_exact_journal_status` has *already* displayed, so
# classifying it as a raw path would demand wrapping it a second time, against this issue's own
# rule that a path is displayed once, where it first enters text. Outside these modules it names
# rendered validation and Linear output that carries no path at all.
_MODULE_PATH_BEARING_NAMES: dict[str, frozenset[str]] = {
    "reconcile_transaction.py": frozenset(
        {
            "journal_path",
            "raw_path",
            "artifact",
            "before_path",
            "after_path",
            "operation_path",
            "current",
            "prefix",
            "filename",
        }
    ),
    "cli/commands/reconcile.py": frozenset({"orphan", "filename"}),
}

# Modules outside the guard's ownership, each with the reason it is not scanned. Keyed on the
# module's path under `src/doc_lattice`, not its basename: two packages here already hold a
# `reconcile.py`, and a basename key would exempt a future sink in the wrong one silently.
#
# GTX-209 retired the `reconcile_transaction.py` and `persistence.py` entries GTX-125 parked
# here, and `path_utils.py` with them: the reconcile transaction layer embeds `safe_resolve`'s
# containment ValueError verbatim, so that message is a human-facing sink like any other and is
# display-spelled now rather than exempted. All three are scanned; what remains for them is
# per-expression below.
_DISPLAY_GUARD_EXEMPT_MODULES = {
    # Not document paths: the config file the user pointed at, the tool's own cache location,
    # and a `--docs-root` argument echoed back before any document is read. These carry no
    # repo-controlled document filename, so GTX-125 leaves their spelling alone and GTX-209
    # keeps them out. The cache store's own diagnostic interpolates a cache path separately
    # from the durable-write note GTX-209 did move, and stays GTX-212's boundary.
    "config.py",
    "cache/store.py",
    "cli/commands/init.py",
}

# Individual expressions inside scanned modules that are not paths despite the name. Every entry
# below is machine construction: a filename being built, or a path being written to a machine
# channel. None of them reaches a person, and GTX-209 removed the two reconcile-adapter entries
# that did.
#
# Keyed on (module, qualified function, expression) rather than on a line number. GTX-209 needed
# five entries in one actively edited module, and a line key silently goes stale on the next
# reformat -- exempting nothing while hiding the sink it was written for. A function key survives
# every edit that does not move the expression to another function, and moving it there is
# exactly the change that should be re-judged. Two unwrapped spellings of the same name in one
# function share an entry, so an entry has to be judged against every one of them:
# `_prepare_transaction` interpolates `destination.name` into both stage prefixes, and both are
# the same machine construction. Splitting a function is the natural fix if a future pair is not.
_DISPLAY_GUARD_EXEMPT_EXPRESSIONS = {
    # `source` here is a slice of raw YAML text being re-emitted into a rewritten scalar, not a
    # file path, and it is bytes destined for a document rather than for a human.
    ("reconcile.py", "_anchored_seen_source", "source"),
    # The JSON spelling of an orphan-scan failure. AD-34 excludes machine channels, and the
    # recovery payload's byte identity rests on this line reproducing the pre-GTX-209 rendering
    # exactly, path component included. The human encoder lives in the reconcile adapter.
    ("reconcile_transaction.py", "ScanFailure.legacy_text", "self.filename"),
    # Staged-artifact *filenames*, not text: the expected-name pattern a recorded artifact is
    # matched against, and the two `stage_bytes` prefixes a new stage is created with. These are
    # filesystem input, and quoting them would change the names written to disk.
    ("reconcile_transaction.py", "_validate_artifact_path", "destination.name"),
    ("reconcile_transaction.py", "_prepare_transaction", "destination.name"),
    # The journal serializer: the exact bytes published to the journal file, which recovery
    # reads back and validates. A machine channel in the strictest sense.
    ("reconcile_transaction.py", "_serialize_journal", "journal.model_dump_json"),
}


_DISPLAY_HELPER = "format_path_for_display"
# Rich's markup escaper. Every human-facing sink in these modules passes its interpolated text
# through it, which makes it the second reliable marker of text being built for a person.
_MARKUP_ESCAPE = "escape"
# The separator-join that turns several paths into one string. `_abort_prepared` lists every
# unresolved destination this way, above the message that carries the result.
_TEXT_JOIN = "join"


def _dotted_label(node: ast.AST) -> str | None:
    """Spell a bare name or attribute chain, or None for any other expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_label(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _unwrapped_path_labels(node: ast.AST, names: frozenset[str]) -> set[str]:
    """Path-bearing names an expression still reaches without the display helper.

    Recursion rather than a bare-name test, because a sink almost never interpolates the
    path alone: the pre-GTX-125 reconcile adapter wrote ``{escape(path.name)}``, and a test
    that only reads the top-level expression sees a call and reports nothing. Any subtree
    rooted at a ``format_path_for_display`` call is pruned, so a correctly wrapped path --
    including one wrapped and then handed to ``escape`` -- reaches no name.

    Args:
        node: The expression to scan.
        names: The path-bearing names in force for the module being scanned, which is the
            global set widened by that module's entry in ``_MODULE_PATH_BEARING_NAMES``.
    """
    if isinstance(node, ast.Call) and _dotted_label(node.func) == _DISPLAY_HELPER:
        return set()
    label = _dotted_label(node)
    if label is not None:
        parts = label.split(".")
        return {label} if any(part in names for part in parts) else set()
    found: set[str] = set()
    if isinstance(node, ast.comprehension):
        # A comprehension binds its loop variable; the binding is not a value that reaches
        # text, and the element expression that does is scanned as a sibling of this node.
        # Without this, the correct spelling of a join -- `join(display(destination) for
        # destination in ...)` -- reports its own loop variable as an unwrapped path.
        children: list[ast.AST] = [node.iter, *node.ifs]
    else:
        children = list(ast.iter_child_nodes(node))
    for child in children:
        found |= _unwrapped_path_labels(child, names)
    return found


def _builds_human_text(call: ast.Call) -> bool:
    """Whether a call is one of the two non-f-string shapes that build text from paths."""
    if _dotted_label(call.func) == _MARKUP_ESCAPE:
        return True
    return isinstance(call.func, ast.Attribute) and call.func.attr == _TEXT_JOIN


def _path_interpolations(source: str, module: str = "") -> list[tuple[int, str]]:
    """Return (line, label) for every path-bearing name reaching human-facing text.

    Three construction shapes carry a path into output, and scanning only the first leaves a
    real sink unguarded. An f-string interpolation is the obvious one. The second is a bare
    ``escape(...)`` call: the reconcile adapter formats its basename once above the loop that
    prints it, so the f-string there interpolates an already-formatted local and the raw path
    never appears inside one. A JoinedStr-only scan reports that sink clean no matter how it
    is spelled, which is exactly the omission this guard exists to catch.

    The third is a ``", ".join(...)`` over paths, which GTX-209 added. ``_abort_prepared``
    lists every unresolved destination by joining them into one string above the message that
    carries it, so -- like the ``escape`` shape -- no raw path is ever inside an f-string. The
    join is where the paths first enter text, so it is where they are checked; the local it
    binds to is already-rendered text and is deliberately not a path-bearing name.

    Args:
        source: The module source to scan.
        module: The module's path under ``src/doc_lattice``, which selects its entry in
            ``_MODULE_PATH_BEARING_NAMES``. The default scans with the global names only,
            which is what the detector's own unit tests want.
    """
    names = _PATH_BEARING_NAMES | _MODULE_PATH_BEARING_NAMES.get(module, frozenset())
    found: set[tuple[int, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    found.update(
                        (value.lineno, name) for name in _unwrapped_path_labels(value, names)
                    )
        elif isinstance(node, ast.Call) and _builds_human_text(node):
            found.update((node.lineno, name) for name in _unwrapped_path_labels(node, names))
    return sorted(found)


def test_path_interpolation_detector_sees_a_bare_path_and_ignores_a_wrapped_one():
    bare = 'msg = f"cannot read {path}: boom"'
    wrapped = 'msg = f"cannot read {format_path_for_display(path)}: boom"'

    assert _path_interpolations(bare) == [(1, "path")]
    assert _path_interpolations(wrapped) == []


def test_path_interpolation_detector_sees_an_attribute_path():
    assert _path_interpolations('f"{node.path}"') == [(1, "node.path")]
    assert _path_interpolations('f"{context.source}"') == [(1, "context.source")]


def test_path_interpolation_detector_sees_an_attribute_of_a_path():
    # The regression this guard exists to prevent: the reconcile adapter used to interpolate
    # the basename, which reads as `name` unless every component of the chain is tested.
    assert _path_interpolations('f"{path.name}"') == [(1, "path.name")]


def test_path_interpolation_detector_looks_through_a_wrapper_call():
    # The pre-GTX-125 reconcile adapter spelled its sink exactly this way, and a detector that
    # only reads the top-level expression would see a call and report nothing.
    assert _path_interpolations('f"{escape(path.name)}"') == [(1, "path.name")]
    assert _path_interpolations('f"{escape(str(node.path))}"') == [(1, "node.path")]


def test_path_interpolation_detector_spares_a_wrapped_path_inside_a_wrapper_call():
    assert _path_interpolations('f"{escape(format_path_for_display(path))}"') == []


def test_path_interpolation_detector_sees_a_path_escaped_outside_an_f_string():
    # The reconcile adapter formats its basename once above the loop that prints it, so the
    # raw path never reaches an f-string at all. A JoinedStr-only scan calls that sink clean
    # however it is spelled, which is the omission this guard exists to catch.
    assert _path_interpolations("name = escape(path.name)") == [(1, "path.name")]
    assert _path_interpolations("name = escape(format_path_for_display(path))") == []


def _qualified_names(source: str) -> list[tuple[int, int, str]]:
    """Return (first line, last line, dotted name) for every function in a module.

    Nested definitions come after their parents, so resolving a line against the *last*
    matching span picks the innermost function containing it.
    """
    spans: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                spans.append((child.lineno, child.end_lineno or child.lineno, name))
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return spans


def _qualified_interpolations(source: str, module: str) -> list[tuple[str, str, int]]:
    """Return (function, label, line) for every path-bearing name reaching human text."""
    spans = _qualified_names(source)
    qualified: list[tuple[str, str, int]] = []
    for line, label in _path_interpolations(source, module):
        enclosing = [name for start, end, name in spans if start <= line <= end]
        qualified.append((enclosing[-1] if enclosing else "<module>", label, line))
    return qualified


def test_qualified_interpolations_names_the_innermost_enclosing_function():
    source = "class C:\n    def m(self):\n        def inner():\n            return f'{path}'\n"
    assert _qualified_interpolations(source, "") == [("C.m.inner", "path", 4)]


def test_qualified_interpolations_names_the_module_for_a_top_level_sink():
    assert _qualified_interpolations("msg = f'{path}'", "") == [("<module>", "path", 1)]


def test_path_interpolation_detector_scopes_a_name_to_the_module_that_owns_it():
    # `raw_path` is a journal-recorded path in the transaction module and nothing in
    # particular anywhere else, which is the whole reason the set is scoped rather than global.
    unscoped = 'msg = f"unsafe {raw_path}"'
    assert _path_interpolations(unscoped) == []
    assert _path_interpolations(unscoped, "reconcile_transaction.py") == [(1, "raw_path")]
    assert _path_interpolations(unscoped, "cli/commands/reconcile.py") == []


def test_path_interpolation_detector_sees_the_transaction_entry_image_fields():
    # Neither reads as `path` under component matching: they are single components, and the
    # rollback and commit diagnostics that name them would otherwise go unchecked.
    source = 'msg = f"{entry.before_path} then {entry.after_path}"'
    assert _path_interpolations(source, "reconcile_transaction.py") == [
        (1, "entry.after_path"),
        (1, "entry.before_path"),
    ]


def test_path_interpolation_detector_sees_a_derived_stage_prefix():
    # `prefix` carries `destination.name`, so it stays hostile after the recorded artifact
    # path beside it is wrapped. Both the composition and a bare use of it are reported.
    composition = 'prefix = f".{destination.name}{infix}"'
    assert _path_interpolations(composition, "reconcile_transaction.py") == [
        (1, "destination.name")
    ]
    use = 'msg = f"must match {prefix}<nonempty>{suffix}"'
    assert _path_interpolations(use, "reconcile_transaction.py") == [(1, "prefix")]
    wrapped = 'msg = f"must match {format_path_for_display(prefix + suffix)}"'
    assert _path_interpolations(wrapped, "reconcile_transaction.py") == []


def test_path_interpolation_detector_sees_a_join_over_paths():
    # `_abort_prepared` lists unresolved destinations by joining them above the message that
    # carries them, so no raw path is ever inside an f-string. A scan of f-strings and
    # `escape()` alone calls that sink clean however it is spelled.
    bare = 'listed = ", ".join(str(destination) for destination in outcome.unresolved)'
    assert _path_interpolations(bare) == [(1, "destination")]
    wrapped = (
        'listed = ", ".join(format_path_for_display(destination) '
        "for destination in outcome.unresolved)"
    )
    assert _path_interpolations(wrapped) == []


def test_path_interpolation_detector_ignores_a_comprehension_binding():
    # The loop variable is a binding, not a value reaching text. Reading it as one would
    # report the correct spelling of the join above as its own violation.
    assert _path_interpolations("[str(x) for path in paths]") == []


def test_path_interpolation_detector_leaves_already_rendered_detail_alone():
    # `detail` names a real sink in the transaction module, and is deliberately not a
    # path-bearing name: `_exact_journal_status` has already displayed the journal inside it,
    # so classifying it as a raw path would demand a second wrapping. It also names rendered
    # validation and Linear text elsewhere, which carries no path at all.
    source = 'msg = f"cannot clean journal: {detail}"'
    assert _path_interpolations(source, "reconcile_transaction.py") == []
    assert _path_interpolations(source, "validation_render.py") == []


def test_display_guard_exemptions_are_all_reachable():
    """Every exemption still names a line the scan actually reports, or it is stale.

    An exemption whose line has moved silences nothing and hides the sink it was written for,
    which is the failure mode a line-keyed exemption set has. Checking reachability is what
    keeps the set honest as the modules under it change.
    """
    reported: set[tuple[str, str, str]] = set()
    for file in _source_files():
        module = file.relative_to(SRC_DIR).as_posix()
        if module in _DISPLAY_GUARD_EXEMPT_MODULES:
            continue
        source = file.read_text(encoding="utf-8")
        for function, label, _line in _qualified_interpolations(source, module):
            reported.add((module, function, label))
    stale = sorted(_DISPLAY_GUARD_EXEMPT_EXPRESSIONS - reported)
    assert not stale, f"these display-guard exemptions no longer name a reported sink: {stale}"


def test_no_human_facing_sink_interpolates_a_raw_path():
    offenders: list[str] = []
    for file in _source_files():
        module = file.relative_to(SRC_DIR).as_posix()
        if module in _DISPLAY_GUARD_EXEMPT_MODULES:
            continue
        source = file.read_text(encoding="utf-8")
        for function, label, line in _qualified_interpolations(source, module):
            if (module, function, label) in _DISPLAY_GUARD_EXEMPT_EXPRESSIONS:
                continue
            offenders.append(
                f"{module}:{line} ({function}) interpolates {{{label}}} without the display "
                "spelling"
            )
    assert not offenders, (
        "GTX-125 (AD-34): route each of these through path_utils.format_path_for_display, or "
        "add it to the exemptions above with the reason it is not a human-facing document "
        "path:\n" + "\n".join(offenders)
    )


# GTX-219 (AD-37): the display strategy for a YAML load failure's own message. AD-34's rule that
# a display strategy is only as complete as its sink list applies here for the same reason it
# applies to paths: the failure mode is a handler that reports the exception without the spelling,
# and a per-site behavioral list cannot catch a handler that does not exist yet. The family is
# small and every member of it reaches a user through some handler, so the guard is keyed on the
# `except` clause rather than on an f-string, which also makes it see a handler that builds its
# detail above the message the way `config.py` does.
_YAML_ERROR_FAMILY = "YAML_LOAD_ERRORS"
_YAML_ERROR_HELPER = "format_yaml_error_for_display"

# Handlers that catch the family without reporting it, keyed on (module, qualified function).
# Keyed on the clause rather than on whether it binds a name: an `except YAML_LOAD_ERRORS:` with
# no `as exc` is structurally unable to interpolate the exception, so keying on the binding would
# exempt this handler and every future swallowing one silently, which is the omission the guard
# exists to catch. `_seen_scalar_source` is a round-trip probe rather than a load of the user's
# document, and its own comment records why the answer to a failed probe is the quoted form.
_YAML_ERROR_RENDER_EXEMPT = {("reconcile.py", "_seen_scalar_source")}


def _catches_yaml_load_errors(handler: ast.ExceptHandler) -> bool:
    """True for an ``except`` clause naming the shared load-failure family."""
    return handler.type is not None and any(
        isinstance(node, ast.Name) and node.id == _YAML_ERROR_FAMILY
        for node in ast.walk(handler.type)
    )


def _yaml_handlers(source: str) -> list[tuple[int, bool]]:
    """Return (line, whether it spells the exception) for every load-failure handler.

    The helper is looked for anywhere inside the handler body rather than inside an f-string,
    because a caller may render the detail into a local above the message it builds, which
    ``config.py`` does so its own parser note stays on one line. A scan keyed on interpolation
    would call that handler clean however it is spelled.
    """
    found: list[tuple[int, bool]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler) and _catches_yaml_load_errors(node):
            spelled = any(
                _is_call_to(child, _YAML_ERROR_HELPER)
                for statement in node.body
                for child in ast.walk(statement)
            )
            found.append((node.lineno, spelled))
    return sorted(found)


def test_yaml_handler_detector_sees_a_raw_interpolation():
    source = "try:\n    load()\nexcept YAML_LOAD_ERRORS as exc:\n    raise E(f'bad: {exc}')\n"
    assert _yaml_handlers(source) == [(3, False)]


def test_yaml_handler_detector_accepts_a_spelled_interpolation():
    source = (
        "try:\n    load()\nexcept YAML_LOAD_ERRORS as exc:\n"
        "    raise E(f'bad: {format_yaml_error_for_display(exc)}')\n"
    )
    assert _yaml_handlers(source) == [(3, True)]


def test_yaml_handler_detector_accepts_a_detail_rendered_above_the_message():
    # `config.py`'s shape: the detail is a local, so the f-string interpolates an already
    # rendered name and the raw exception never appears inside one.
    source = (
        "try:\n    load()\nexcept YAML_LOAD_ERRORS as exc:\n"
        "    detail = format_yaml_error_for_display(exc)\n    raise E(f'bad: {detail}')\n"
    )
    assert _yaml_handlers(source) == [(3, True)]


def test_yaml_handler_detector_sees_a_handler_that_binds_no_name():
    # The swallowing shape. A detector keyed on `as exc` would report nothing here, so a future
    # handler that drops the family would be exempt without anyone deciding that it should be.
    assert _yaml_handlers("try:\n    load()\nexcept YAML_LOAD_ERRORS:\n    pass\n") == [(3, False)]


def test_yaml_handler_detector_ignores_a_spelling_outside_the_handler():
    source = (
        "detail = format_yaml_error_for_display(exc)\n"
        "try:\n    load()\nexcept YAML_LOAD_ERRORS as exc:\n    raise E(f'bad: {exc}')\n"
    )
    assert _yaml_handlers(source) == [(4, False)]


def test_yaml_handler_detector_ignores_an_unrelated_family():
    assert _yaml_handlers("try:\n    load()\nexcept OSError as exc:\n    raise E(exc)\n") == []


def _qualified_yaml_handlers(source: str) -> list[tuple[str, int, bool]]:
    """Return (function, line, whether it spells the exception) for each handler in a module."""
    spans = _qualified_names(source)
    qualified: list[tuple[str, int, bool]] = []
    for line, spelled in _yaml_handlers(source):
        enclosing = [name for start, end, name in spans if start <= line <= end]
        qualified.append((enclosing[-1] if enclosing else "<module>", line, spelled))
    return qualified


def test_yaml_error_render_exemptions_are_all_reachable():
    """Every exemption still names a handler the guard would otherwise report, or it is stale.

    Reachability is asserted against the unspelled handlers alone, not against every handler the
    scan sees. An exemption whose handler has since started calling the renderer silences nothing
    and only hides the next handler that moves into that function, which is the failure mode a
    keyed exemption set has.
    """
    reported = {
        (module, function)
        for module, source in _production_sources().items()
        for function, _line, spelled in _qualified_yaml_handlers(source)
        if not spelled
    }
    stale = sorted(_YAML_ERROR_RENDER_EXEMPT - reported)
    assert not stale, f"these YAML display exemptions no longer name a reported handler: {stale}"


def test_every_yaml_load_failure_handler_spells_the_exception_it_reports():
    offenders: list[str] = []
    for module, source in sorted(_production_sources().items()):
        for function, line, spelled in _qualified_yaml_handlers(source):
            if spelled or (module, function) in _YAML_ERROR_RENDER_EXEMPT:
                continue
            offenders.append(f"{module}:{line} ({function}) reports a YAML load failure unspelled")
    assert not offenders, (
        "GTX-219 (AD-37): route each caught exception through "
        "yaml_error_render.format_yaml_error_for_display, or add the handler to the exemptions "
        "above with the reason it reports nothing:\n" + "\n".join(offenders)
    )
