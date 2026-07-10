"""Convention enforcement tests."""

import ast
import inspect
from pathlib import Path

from game_lattice import error_types
from game_lattice.constants import VALID_AUTHORITIES, VALID_STATUSES
from game_lattice.error_types import ProjectError

SRC_DIR = Path(__file__).parent.parent / "src" / "game_lattice"


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
    cli_source = (SRC_DIR / "cli.py").read_text(encoding="utf-8")

    assert cli_source.count("except ProjectError") == 2


def test_no_raw_authority_strings():
    """Authority values must be imported from constants.py, not inlined as raw literals."""
    for py_file in _source_files():
        if py_file.name == "constants.py":
            continue
        content = py_file.read_text(encoding="utf-8")
        for value in sorted(VALID_AUTHORITIES):
            assert f'"{value}"' not in content, f"{py_file.name} inlines authority '{value}'"
            assert f"'{value}'" not in content, f"{py_file.name} inlines authority '{value}'"


def test_no_raw_status_strings():
    """Status values must be imported from constants.py, not inlined as raw literals."""
    for py_file in _source_files():
        if py_file.name == "constants.py":
            continue
        content = py_file.read_text(encoding="utf-8")
        for value in sorted(VALID_STATUSES):
            assert f'"{value}"' not in content, f"{py_file.name} inlines status '{value}'"
            assert f"'{value}'" not in content, f"{py_file.name} inlines status '{value}'"


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
