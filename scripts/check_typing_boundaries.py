#!/usr/bin/env python3
"""Check that typing.Any/typing.cast usage is restricted to boundary modules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# The three modules AD-3 names, spelled as exact source-root-relative paths. An allowlist rather
# than a name pattern: a pattern exempts every future module whose name happens to match it, and
# a stem alone would extend the exemption to a same-named module anywhere in the tree.
BOUNDARY_MODULES = frozenset(
    {
        "doc_lattice/frontmatter_parser.py",
        "doc_lattice/linear_parser.py",
        "doc_lattice/yaml_boundary.py",
    }
)


def is_boundary_module(relpath: Path) -> bool:
    """Return True if the file is one of the boundary modules AD-3 names.

    Args:
        relpath: The module's path relative to the scanned source root, so
            `relpath.parts[0]` is the top-level package.

    Returns:
        True only for an exact path match against `BOUNDARY_MODULES`. A module whose
        name merely reads like a boundary role, such as `doc_lattice/cache/external.py`
        or a second package's `frontmatter_parser.py`, is not exempt.
    """
    return relpath.as_posix() in BOUNDARY_MODULES


TYPING_MODULES = {"typing", "typing_extensions"}
ESCAPE_HATCHES = {"Any", "cast"}


def find_escape_hatch_usage(filepath: Path) -> list[tuple[int, str]]:
    """Return (line, message) pairs for each typing.Any/typing.cast use in the file."""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except SyntaxError:
        return []
    # Local names bound to the typing module itself (`import typing as t`), so a
    # qualified `t.Any` / `typing.cast` is distinguishable from an unrelated
    # `obj.cast()` method call or a `module.Any` attribute on some other object.
    typing_aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in TYPING_MODULES
    }
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in TYPING_MODULES:
            # alias.name is the pre-alias name, so `cast as narrow` is caught here
            # regardless of the local binding.
            violations.extend(
                (node.lineno, f"imports typing.{alias.name}")
                for alias in node.names
                if alias.name in ESCAPE_HATCHES
            )
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in ESCAPE_HATCHES
            and isinstance(node.value, ast.Name)
            and node.value.id in typing_aliases
        ):
            # Qualified `typing.Any` / `typing.cast` (or via an aliased typing module).
            violations.append((node.lineno, f"uses typing.{node.attr}"))
    return violations


def main() -> None:
    """Scan a directory and exit non-zero if typing.Any/cast leaks outside boundaries."""
    search_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path()
    violations: list[str] = []
    for py_file in search_dir.rglob("*.py"):
        if is_boundary_module(py_file.relative_to(search_dir)):
            continue
        violations.extend(
            f"  {py_file}:{line} - {msg}" for line, msg in find_escape_hatch_usage(py_file)
        )
    if violations:
        print("FAIL: typing.Any/typing.cast found outside boundary modules:")
        for v in violations:
            print(v)
        sys.exit(1)
    print("PASS: typing.Any/typing.cast restricted to boundary modules")


if __name__ == "__main__":
    main()
