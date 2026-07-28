"""Behavior tests for the fail-closed guard witness search tool."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from doc_lattice.github_ci.shell_guards import ScanLimits, ScannerLimits

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "scripts/guard_witness_sweep.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_witness_sweep", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def test_limits_grid_shrinks_every_declared_cap() -> None:
    labels = {label for label, _limits in tool.limits_grid((0,))}
    shrunk = labels - {tool.PRODUCTION}

    assert "TaintLimits(max_edges=0)" in shrunk
    assert "ScannerLimits(max_scan_steps=0)" in shrunk
    # Every field of both limits values must appear, or a cap silently goes unsearched.
    assert len(shrunk) == tool.limits_field_count()


def test_limits_grid_includes_the_unshrunk_configuration() -> None:
    assert ("production", ScanLimits()) in tool.limits_grid(())


def test_sweep_finds_a_guard_only_a_shrunk_cap_reaches() -> None:
    found = tool.sweep(["echo one; echo two"], tool.limits_grid((0,)))

    assert "scanner.budget.step-limit" in found
    label, script = found["scanner.budget.step-limit"]
    assert "max_scan_steps=0" in label
    assert script == "echo one; echo two"


def test_sweep_prefers_the_shortest_reaching_script() -> None:
    corpus = ["a" * 40, "a" * 8]
    grid = [("tiny-source", ScanLimits(scanner=ScannerLimits(max_source_chars=4)))]

    found = tool.sweep(corpus, grid)

    assert found["scanner.source.character-limit"][1] == "a" * 8


def test_sweep_can_restrict_itself_to_still_unclassified_guards() -> None:
    found = tool.sweep(
        ["echo one; echo two"],
        tool.limits_grid((0,)),
        wanted=frozenset({"scanner.source.character-limit"}),
    )

    assert "scanner.budget.step-limit" not in found


def test_trace_reports_the_guarded_functions_a_script_executes() -> None:
    # This is what locates a reaching *shape* when the sweep finds nothing: it shows which guard
    # machinery a candidate reaches at all, so the next candidate can be aimed one level deeper.
    reached = tool.trace_guard_functions("eval 'X=${Y=q}'; eval \"$X\"lattice")

    assert "_eval_syntax_record_assignment" in reached
    assert "_eval_syntax_record_decision" in reached


def test_trace_distinguishes_a_shape_that_never_reaches_that_machinery() -> None:
    reached = tool.trace_guard_functions("echo hello")

    assert "_eval_syntax_record_assignment" not in reached


def test_rendered_rows_are_paste_ready_registry_entries() -> None:
    rendered = tool.render_rows({"scanner.budget.step-limit": ("TaintLimits(max_edges=0)", "x")})

    assert "ReachableWitness(" in rendered
    assert '"scanner.budget.step-limit"' in rendered
    assert "limits=ScanLimits(taint=TaintLimits(max_edges=0))" in rendered


def test_rendered_rows_omit_limits_for_a_production_reach() -> None:
    rendered = tool.render_rows({"scanner.source.character-limit": ("production", "x")})

    assert "limits=" not in rendered


def test_unclassified_ids_match_the_frozen_debt_snapshot() -> None:
    unclassified = tool.unclassified_ids(_ROOT)

    assert unclassified
    assert "scanner.source.character-limit" not in unclassified


def test_main_traces_one_script_without_running_a_sweep(capsys: pytest.CaptureFixture) -> None:
    assert tool.main(["--trace", "eval 'X=${Y=q}'; eval \"$X\"lattice"]) == 0

    assert "_eval_syntax_record_assignment" in capsys.readouterr().out
