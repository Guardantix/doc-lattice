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
        # No datetime_utils.py exists today: the module was deleted once its last helper
        # went unused. The exemption stays so that recreating that module is the sanctioned
        # way to reintroduce a current-time call.
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
DESTINATION_READERS = frozenset(
    {
        "_authenticate_staged_artifact",
        "_commit_operation_error",
        "_recovery_operation_error",
        "_resolve_journal_path",
        "_validate_artifact_path",
        "file_sha256",
        "stage_bytes",
        PUBLISH_CALLEE,
    }
)
PUBLICATION_OWNERS = frozenset({"persistence.py", TRANSACTION_MODULE})
# Each composite primitive below stages and publishes one destination in a single call, so naming
# one overwrites a document without ever naming the publication helper and would slip past a scan
# that reads only that helper's name. The descriptor-relative variant does not even route through
# it. Each primitive's present users write their own artifacts rather than documents, so they are
# pinned per primitive and any new user of either route fails closed.
COMPOSITE_PUBLISH_USERS: dict[str, frozenset[str]] = {
    "atomic_replace_bytes": frozenset({"cache/store.py"}),
    "atomic_replace_bytes_at": frozenset({"github_ci/filesystem.py"}),
}
# The envelope pieces a rewrite may reattach verbatim. `raw_meta` is deliberately absent: it
# holds the pre-edit YAML the gate never verified, so admitting it would let the mis-splice
# check pass on a document that reintroduces exactly the bytes the gate was meant to replace.
ENVELOPE_FIELDS = frozenset({"prefix", "open_fence", "close_fence", "close_fence_newline", "body"})


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


def _assigned_names(tree: ast.Module) -> list[tuple[list[ast.expr], ast.expr]]:
    """Every assignment in a module, as its target list paired with its assigned value."""
    pairs: list[tuple[list[ast.expr], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            pairs.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs.append(([node.target], node.value))
    return pairs


def _local_aliases(tree: ast.Module, symbol: str) -> frozenset[str]:
    """Every local name in a module that refers to ``symbol``, however it was bound.

    Imports are not the only way to rename a guarded symbol. ``R = Rewrite`` followed by
    ``R(...)``, or ``publish = persistence.replace_staged``, rebinds it under a name an
    import-only scan never sees, leaving the original site as the apparent sole one. Assignments
    are followed to a fixpoint so a chain of renames resolves too.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == symbol
            )
        elif isinstance(node, ast.ClassDef | ast.FunctionDef) and node.name == symbol:
            aliases.add(symbol)
    assignments = _assigned_names(tree)
    growing = True
    while growing:
        growing = False
        for targets, value in assignments:
            renames = (isinstance(value, ast.Name) and value.id in aliases) or (
                isinstance(value, ast.Attribute) and value.attr == symbol
            )
            if not renames:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
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
    """True when an f-string rebuilds the document from verified meta plus envelope parts."""
    if context.envelope_root is None:
        return False
    carried = False
    for value in expr.values:
        if isinstance(value, ast.Constant):
            if not _is_envelope_literal(value):
                return False
            continue
        if not isinstance(value, ast.FormattedValue):
            return False
        if value.conversion != -1 or value.format_spec is not None:
            return False
        if _traces_to(value.value, context, seen):
            carried = True
        elif not _is_envelope_part(value.value, context.envelope_root):
            return False
    return carried


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
    """
    sites: list[_Site] = []
    for site in _call_sites(trees):
        func = site.call.func
        named = isinstance(func, ast.Name) and func.id == COPY_CALLEE
        qualified = isinstance(func, ast.Attribute) and func.attr == COPY_CALLEE
        if (named or qualified) and any(kw.arg == AFTER_FIELD for kw in site.call.keywords):
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

    Reaching it indirectly counts, and so does bypassing it. Each composite primitive in
    ``COMPOSITE_PUBLISH_USERS`` stages and publishes one destination in a single call, so a
    module can overwrite a reconcile destination through one of them without ever naming
    ``replace_staged``; the descriptor-relative variant does not even call it. Each primitive's
    current users publish their own artifacts rather than documents and are pinned per
    primitive, so a new user of either route fails here too.

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


def _is_destination(
    expr: ast.expr, bindings: dict[str, list[_Binding]], seen: tuple[str, ...] = ()
) -> bool:
    """True for a journal entry's destination, named directly or held by a local."""
    if isinstance(expr, ast.Attribute) and expr.attr == DESTINATION_FIELD:
        return True
    if not isinstance(expr, ast.Name) or expr.id in seen:
        return False
    binding = _sole_binding(bindings, expr.id)
    return binding is not None and _is_destination(binding.value, bindings, (*seen, expr.id))


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

    The scan covers ``reconcile_transaction.py`` alone, the only module that owns reconcile
    destinations. Elsewhere a ``.destination`` field belongs to an unrelated domain, and the
    reach rule already pins which modules may publish at all.
    """
    scoped = {TRANSACTION_MODULE: trees[TRANSACTION_MODULE]}
    cache: dict[tuple[str, str | None], dict[str, list[_Binding]]] = {}
    problems: list[str] = []
    for site in _call_sites(scoped):
        bindings = _enclosing_bindings(scoped, site, cache)
        func = site.call.func
        if isinstance(func, ast.Attribute) and _is_destination(func.value, bindings):
            problems.append(
                _gate_msg(
                    f"{site.located} calls {func.attr}() on a reconcile destination, which no "
                    "pinned reader does"
                )
            )
            continue
        name = _callee_name(site.call)
        if name in DESTINATION_READERS:
            continue
        arguments = [*site.call.args, *(keyword.value for keyword in site.call.keywords)]
        if any(_is_destination(argument, bindings) for argument in arguments):
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

DESCRIPTOR_PUBLISHER_MODULE = '''"""A module publishing at a descriptor-relative destination."""

from .persistence import atomic_replace_bytes_at


def publish(directory_fd: int, destination_name: str, data: bytes) -> None:
    """Publish arbitrary bytes over a reconcile destination without reaching the helper."""
    atomic_replace_bytes_at(directory_fd, destination_name, data, prefix=".rogue")
'''

# The forward sink the controls below bend, with the indentation its statement sits at, so an
# added statement lands in the same block rather than reindenting the module into a syntax error.
FORWARD_SINK_CALL = "replace_staged(entry.after_path, entry.destination)"
SINK_INDENT = " " * 12

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
    import alias. The last five came from a second adversarial pass, likewise reproduced first:
    literal text spliced into the envelope f-string, an after image staged behind a locally
    bound infix, a document published through ``atomic_replace_bytes`` without ever naming the
    publication helper, the same through the descriptor-relative ``atomic_replace_bytes_at``,
    which does not even call that helper, and a Rewrite minted by ``dataclasses.replace`` rather
    than constructed. The last five came from external review of this guard, again each
    reproduced first: a restoration replacing one newline with two, a sink pairing one entry's
    staged image with another entry's destination, a destination republished by a raw
    ``os.replace``, a destination overwritten through its own write method, and a producer
    behind an alias introduced by assignment rather than by import.

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
        "document-published-through-the-descriptor-relative-primitive": {
            **sources,
            "rogue_descriptor_publisher.py": DESCRIPTOR_PUBLISHER_MODULE,
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
        "rewrite-minted-behind-an-assigned-alias": {
            **sources,
            "rogue_assigned_alias_producer.py": ASSIGNED_ALIAS_PRODUCER_MODULE,
        },
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
