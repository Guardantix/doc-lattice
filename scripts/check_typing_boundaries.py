#!/usr/bin/env python3
"""Check that typing.Any/typing.cast usage is restricted to boundary modules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BOUNDARY_PATTERNS = {"boundary", "adapter", "parser", "validator", "external", "inbound"}


def is_boundary_module(relpath: Path) -> bool:
    """Return True if the file is an approved boundary module.

    `relpath` is relative to the scanned source root, so `relpath.parts[0]` is the
    top-level package. Only directories *below* that package count toward a
    directory match, so a package merely named like a boundary role (e.g.
    `src/parser/`) does not exempt its own ordinary modules.
    """
    inner_dirs = set(relpath.parts[1:-1])
    if inner_dirs & BOUNDARY_PATTERNS or relpath.stem in BOUNDARY_PATTERNS:
        return True
    return any(relpath.stem.endswith(f"_{p}") for p in BOUNDARY_PATTERNS)


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
