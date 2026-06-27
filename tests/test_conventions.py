"""Convention enforcement tests."""

import ast
from pathlib import Path

from game_lattice.constants import VALID_STATUSES

SRC_DIR = Path(__file__).parent.parent / "src" / "game_lattice"


def test_no_bare_datetime_now():
    """datetime.now() and datetime.utcnow() must not appear outside datetime_utils.py."""
    for py_file in SRC_DIR.glob("*.py"):
        if py_file.name == "datetime_utils.py":
            continue
        content = py_file.read_text()
        assert "datetime.now()" not in content, f"{py_file.name} uses datetime.now()"
        assert "datetime.utcnow()" not in content, f"{py_file.name} uses datetime.utcnow()"


def test_no_inner_html():
    """innerHTML must not appear in any source file."""
    for py_file in SRC_DIR.glob("*.py"):
        content = py_file.read_text()
        assert "innerHTML" not in content, f"{py_file.name} contains innerHTML"


def test_no_broad_except():
    """except Exception and except BaseException are not allowed."""
    for py_file in SRC_DIR.glob("*.py"):
        content = py_file.read_text()
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ExceptHandler)
                and node.type is not None
                and isinstance(node.type, ast.Name)
            ):
                assert node.type.id not in ("Exception", "BaseException"), (
                    f"{py_file.name}:{node.lineno} catches {node.type.id}"
                )


def test_no_raw_constant_strings():
    """String literals matching constant values should use constants.py."""
    for py_file in SRC_DIR.glob("*.py"):
        if py_file.name == "constants.py":
            continue
        content = py_file.read_text()
        for status in VALID_STATUSES:
            assert f'"{status}"' not in content, f"{py_file.name} contains raw constant '{status}'"
            assert f"'{status}'" not in content, f"{py_file.name} contains raw constant '{status}'"
