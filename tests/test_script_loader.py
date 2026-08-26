"""Behavior tests for the `scripts/` load convention the script suites share.

`tests/script_loader.py` exists for one reason: `runpy.run_path` does not do what running a
script by path does, and the difference is exactly the thing `scripts/audit_action_runtimes.py`
depends on to reach `scripts/_ci_report.py` under ``uv run --no-project``. That difference is
invisible until a script grows a sibling import, at which point a suite fails at collection on
code the runner executes happily. It is pinned here so the convention cannot quietly regress to
a bare `run_path`.
"""

import sys
from runpy import run_path

import pytest
from script_loader import load_script, script_path

_SIBLING_IMPORTER = "audit_action_runtimes.py"


def test_script_path_resolves_against_the_repository_not_the_caller():
    resolved = script_path(_SIBLING_IMPORTER)

    assert resolved.is_absolute()
    assert resolved.is_file()
    assert resolved.parent.name == "scripts"


def test_a_script_reaches_its_siblings_the_way_the_runner_lets_it():
    # The property the whole module exists for. A bare `run_path` raises `ModuleNotFoundError`
    # here, because it inserts nothing on `sys.path` for an argument naming a file.
    namespace = load_script(script_path(_SIBLING_IMPORTER))

    assert callable(namespace["main"])


def test_a_bare_run_path_still_cannot_reach_them():
    # The negative half, and the reason the loader is not merely tidier. It has to evict the
    # sibling from the module cache first: a load that already happened leaves `_ci_report` there,
    # after which a bare `run_path` succeeds on the cache rather than on the import path. That is
    # also why the three script suites all go through the loader -- whichever ran first would
    # otherwise be silently holding the others up.
    scripts = str(script_path(_SIBLING_IMPORTER).parent)
    original_path = list(sys.path)
    cached = sys.modules.pop("_ci_report", None)
    try:
        sys.path[:] = [entry for entry in sys.path if entry != scripts]
        with pytest.raises(ModuleNotFoundError, match="_ci_report"):
            run_path(str(script_path(_SIBLING_IMPORTER)))
    finally:
        sys.path[:] = original_path
        if cached is not None:
            sys.modules["_ci_report"] = cached


def test_the_borrowed_path_entry_is_given_back():
    # Scoped to the load rather than left on the session's import path: a suite that follows this
    # one must not be able to import a `scripts/` module by accident and pass on that alone.
    scripts = str(script_path(_SIBLING_IMPORTER).parent)
    before = list(sys.path)

    load_script(script_path(_SIBLING_IMPORTER))

    assert sys.path == before
    assert scripts not in sys.path


def test_an_entry_the_loader_did_not_add_is_left_alone():
    # The loader removes only what it inserted. Taking an entry it found would corrupt the import
    # path of whatever put it there.
    scripts = str(script_path(_SIBLING_IMPORTER).parent)
    sys.path.insert(0, scripts)
    try:
        load_script(script_path(_SIBLING_IMPORTER))

        assert sys.path.count(scripts) == 1
    finally:
        sys.path.remove(scripts)
