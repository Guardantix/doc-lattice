"""Behavior tests for the start-method and worker-count policy the pooled tools share."""

from __future__ import annotations

import argparse
import importlib.util
import multiprocessing
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "scripts/worker_pool.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("worker_pool", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def test_pooled_workers_do_not_inherit_the_process_that_started_them() -> None:
    # A forked worker inherits a scanner a caller has replaced in the tool's namespace, so the same
    # corpus is scored through one revision on Linux and another everywhere else, which is the one
    # thing a differential cannot afford. It is also what CPython deprecates once a process has
    # threads, which a pool has by its second worker.
    assert tool.start_method() != "fork"
    assert tool.start_method() in multiprocessing.get_all_start_methods()


def test_the_default_worker_count_fits_the_work_there_is(monkeypatch: pytest.MonkeyPatch) -> None:
    # A default derived from the machine rather than from the work starts workers with nothing to
    # do, and one that can reach zero leaves the run with nobody to drive it. Bounds rather than
    # the machine's count, which restates the expression: read off a host whose affinity the
    # interpreter cannot see, `os.process_cpu_count()` is None, and the count that has to come back
    # from that is one.
    assert tool.default_jobs(1) == 1
    assert tool.default_jobs(0) == 1
    assert 1 <= tool.default_jobs(1000) <= 1000

    monkeypatch.setattr(tool.os, "process_cpu_count", lambda: None)

    assert tool.default_jobs(1000) == 1


def test_a_resolved_count_never_exceeds_the_work_or_falls_below_one() -> None:
    # The cap every caller used to write out for itself. A pool sized past the work there is starts
    # interpreters that pay for their own copy of the corpus and score nothing, and a run with no
    # units of work is still driven by this process rather than by nobody.
    assert tool.resolve_jobs(3, 8) == 3
    assert tool.resolve_jobs(8, 3) == 3
    assert tool.resolve_jobs(0, 8) == 1
    assert tool.resolve_jobs(0, None) == 1
    assert tool.resolve_jobs(1, None) == 1
    assert tool.resolve_jobs(1000, None) == tool.default_jobs(1000)


def test_a_run_with_no_workers_is_refused_rather_than_driven_by_nobody() -> None:
    # Zero workers is not a smaller run, and left to argparse's `int` it is refused several layers
    # down in a traceback naming the pool rather than the option that sized it. A value that is not
    # a number at all is refused here too, so both tools name the option rather than one of them
    # falling back to argparse's generic message.
    with pytest.raises(argparse.ArgumentTypeError):
        tool.positive_jobs("0")
    with pytest.raises(argparse.ArgumentTypeError):
        tool.positive_jobs("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        tool.positive_jobs("half")

    assert tool.positive_jobs("3") == 3
