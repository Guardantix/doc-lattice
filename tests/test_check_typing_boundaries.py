"""Tests for the typing-escape-hatch boundary guard script."""

from pathlib import Path
from runpy import run_path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = run_path(str(_ROOT / "scripts" / "check_typing_boundaries.py"))
is_boundary_module = _SCRIPT["is_boundary_module"]
find_escape_hatch_usage = _SCRIPT["find_escape_hatch_usage"]
BOUNDARY_MODULES = _SCRIPT["BOUNDARY_MODULES"]

# AD-3 names exactly these three: document frontmatter YAML, Linear JSON, and the shared ruamel
# safe-load mechanics the first of those and `config` both read through.
_AD3_MODULES = (
    "doc_lattice/frontmatter_parser.py",
    "doc_lattice/linear_parser.py",
    "doc_lattice/yaml_boundary.py",
)


@pytest.mark.parametrize("relpath", _AD3_MODULES)
def test_each_ad3_module_is_a_boundary(relpath):
    assert is_boundary_module(Path(relpath))


def test_the_allowlist_is_exactly_the_ad3_modules():
    assert frozenset(_AD3_MODULES) == BOUNDARY_MODULES


def test_every_allowlisted_module_exists_in_the_source_tree():
    """An allowlist entry naming a module that no longer exists is a silent open hatch."""
    missing = [relpath for relpath in BOUNDARY_MODULES if not (_ROOT / "src" / relpath).is_file()]
    assert missing == [], f"BOUNDARY_MODULES names modules that do not exist: {sorted(missing)}"


@pytest.mark.parametrize(
    "relpath",
    [
        # Keyword-shaped non-members: each of these was exempted by the retired keyword matcher
        # as an inner directory, an exact stem, or a `_<keyword>` suffix.
        "doc_lattice/cache/external.py",
        "doc_lattice/external/store.py",
        "doc_lattice/cache_store_external.py",
        "doc_lattice/boundary.py",
        "doc_lattice/adapter.py",
        "doc_lattice/validator.py",
        "doc_lattice/inbound.py",
        "doc_lattice/parser/loader.py",
        "doc_lattice/yaml_parser.py",
    ],
)
def test_keyword_shaped_non_members_are_rejected(relpath):
    assert not is_boundary_module(Path(relpath))


@pytest.mark.parametrize(
    "relpath",
    [
        # Same basename as an allowlisted module, but not the allowlisted path. A stem-based
        # allowlist would exempt both of these.
        "doc_lattice/nested/frontmatter_parser.py",
        "other_package/frontmatter_parser.py",
        "doc_lattice/yaml_boundary/impl.py",
    ],
)
def test_same_basename_non_members_are_rejected(relpath):
    assert not is_boundary_module(Path(relpath))


def test_ordinary_module_is_not_a_boundary():
    assert not is_boundary_module(Path("doc_lattice/loader.py"))


def test_from_import_of_an_escape_hatch_is_reported(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text("from typing import Any\n\nx: Any = 1\n", encoding="utf-8")
    assert find_escape_hatch_usage(module) == [(1, "imports typing.Any")]


def test_aliased_from_import_is_reported_under_its_pre_alias_name(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text("from typing import cast as narrow\n", encoding="utf-8")
    assert find_escape_hatch_usage(module) == [(1, "imports typing.cast")]


def test_qualified_attribute_use_is_reported(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text("import typing as t\n\nv = t.cast(int, '1')\n", encoding="utf-8")
    assert find_escape_hatch_usage(module) == [(3, "uses typing.cast")]


def test_unrelated_cast_attribute_is_not_reported(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text("import obj\n\nv = obj.cast(1)\n", encoding="utf-8")
    assert find_escape_hatch_usage(module) == []


def test_unparsable_module_reports_nothing_rather_than_raising(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text("def broken(:\n", encoding="utf-8")
    assert find_escape_hatch_usage(module) == []
