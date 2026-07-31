"""Behavior tests for the fail-closed guard witness search tool."""

from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from doc_lattice.github_ci import shell_guards, shell_scanner, shell_taint
from doc_lattice.github_ci.shell_guards import ScanLimits, ScannerLimits

if TYPE_CHECKING:
    from types import ModuleType

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
checker = tool.check_guard_inventory


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


def test_sweep_prefers_the_least_shrunk_reaching_configuration() -> None:
    # A witness should say as little about the caps as it can get away with, so among shrink
    # values that all reach the guard, the largest one wins.
    found = tool.sweep(["echo one; echo two"], tool.limits_grid((0, 1, 2, 3)))

    label, _script = found["scanner.budget.step-limit"]
    assert label == "ScannerLimits(max_scan_steps=3)"


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


def test_trace_restores_the_previously_installed_tracer() -> None:
    # Coverage collection and debuggers install their own settrace hook; clearing it instead of
    # restoring it would silently disable them for the rest of the process.
    ambient = sys.gettrace()

    def previous(_frame: object, _event: str, _argument: object) -> None:
        return None

    sys.settrace(previous)
    try:
        tool.trace_guard_functions("echo hello")
        assert sys.gettrace() is previous
    finally:
        sys.settrace(ambient)


def test_trace_distinguishes_a_shape_that_never_reaches_that_machinery() -> None:
    reached = tool.trace_guard_functions("echo hello")

    assert "_eval_syntax_record_assignment" not in reached


def test_guard_owning_qualnames_come_from_the_recorded_inventory() -> None:
    owning = tool.guard_owning_qualnames(_ROOT)

    assert "_eval_syntax_record_assignment" in owning
    assert "_ascii_lower" not in owning


def test_guard_owning_qualnames_spell_a_nested_guard_the_way_a_frame_does() -> None:
    # The inventory derives its qualified names from the source tree and the tracer reads them off
    # a running frame. A nested guard is where the two spellings can drift apart, and a drift would
    # filter real reach away silently rather than reporting anything wrong.
    owning = tool.guard_owning_qualnames(_ROOT)

    assert "_contextualize_evidence.charge_edges" in owning
    assert "_contextualize_evidence.charge_edges" in tool.trace_guard_functions("echo hello")


def test_trace_output_omits_a_guarded_module_function_owning_no_guard(
    capsys: pytest.CaptureFixture,
) -> None:
    # Every call in a guarded module swamps the reach signal this mode exists to give and presents
    # ordinary helpers as guard machinery.
    assert tool.main(["--trace", "echo hello"]) == 0

    reported = capsys.readouterr().out.split()
    assert "_ScanBudget.step" in reported
    assert "_ascii_lower" not in reported


def test_trace_all_reports_the_whole_guarded_module_reach(
    capsys: pytest.CaptureFixture,
) -> None:
    # Aiming the next candidate deeper uses the functions between the guards too: most of what
    # separates the worked eval shape from a plain one owns no guard of its own.
    assert tool.main(["--trace", "echo hello", "--trace-all"]) == 0

    assert "_ascii_lower" in capsys.readouterr().out.split()


def test_rendered_rows_are_paste_ready_registry_entries() -> None:
    rendered = tool.render_rows({"scanner.budget.step-limit": ("TaintLimits(max_edges=0)", "x")})

    assert "ReachableWitness(" in rendered
    assert '"scanner.budget.step-limit"' in rendered
    assert "limits=ScanLimits(taint=TaintLimits(max_edges=0))" in rendered


def test_rendered_rows_preserve_a_script_containing_quotes() -> None:
    # Replay-corpus scripts can contain either quote character. The rendered row must stay a
    # valid registry entry whose script literal round-trips exactly, or it is not paste-ready.
    script = "env FOO=\"$@\" 'harmless'"

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    row = ast.parse(f"[{rendered}]", mode="eval").body
    assert isinstance(row, ast.List)
    call = row.elts[0]
    assert isinstance(call, ast.Call)
    assert ast.literal_eval(call.args[1]) == script


def test_rendered_rows_preserve_a_script_containing_astral_characters() -> None:
    # A surrogate-pair escape like 😀 is one character in JSON but two lone surrogates
    # in a Python literal, so the pasted row would scan a different script than the sweep did.
    script = "doc-lattice '😀'"

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    row = ast.parse(f"[{rendered}]", mode="eval").body
    assert isinstance(row, ast.List)
    call = row.elts[0]
    assert isinstance(call, ast.Call)
    assert ast.literal_eval(call.args[1]) == script


def test_rendered_rows_omit_limits_for_a_production_reach() -> None:
    rendered = tool.render_rows({"scanner.source.character-limit": ("production", "x")})

    assert "limits=" not in rendered


def test_corpus_order_does_not_depend_on_set_iteration_order() -> None:
    # Equal-length scripts must sort deterministically, or the same sweep prints different
    # witness rows under different PYTHONHASHSEED values.
    corpus = tool.load_corpus(_ROOT, seeds=1, iterations=50)

    assert corpus == sorted(corpus, key=lambda script: (len(script), script))


def test_guarded_filenames_track_the_imported_modules() -> None:
    # Composing the path from the repository root instead would stop matching whenever the tree is
    # reached through a symlink, and a tracer that matches nothing reports the same empty set as a
    # candidate that reaches no guard machinery.
    names = tool.guarded_filenames(_ROOT)

    assert shell_scanner.__file__ in names
    assert shell_taint.__file__ in names


def test_guarded_filenames_cover_every_module_the_inventory_guards() -> None:
    # A list of modules kept here is a second allowlist, and it goes stale the moment a guard
    # module is added: the inventory would name the new module's origins while the tracer recorded
    # none of its frames, so every one of them would be intersected away and the trace would read
    # exactly like a candidate that reaches no guard machinery at all.
    names = tool.guarded_filenames(_ROOT)

    assert shell_guards.__file__ in names
    for module in checker.GUARDED_MODULES:
        assert str(_ROOT / module) in names


def test_guarded_modules_include_one_the_inventory_discovers(tmp_path: Path) -> None:
    # Discovery, not the allowlist, is what sees a guard module the moment it is written, and it
    # is the surface the gate's own repository rules read. Tracking it is what keeps the tool
    # usable on the tree where a new module is still being classified.
    root = tmp_path / "candidate"
    for module in checker.GUARDED_MODULES:
        target = root / module
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_ROOT / module, target)
    added = root / checker.GUARD_MODULE_ROOT / "shell_taint_eval.py"
    added.write_text(
        "def _bound(depth, limits):\n"
        "    if depth > limits.taint.max_eval_reparse_depth:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.eval.new-bound", "too deep"))\n',
        encoding="utf-8",
    )

    assert added.relative_to(root).as_posix() in tool.guarded_modules(root)


def test_load_corpus_rejects_an_extra_file_that_is_not_a_list_of_scripts(
    tmp_path: Path,
) -> None:
    # `set.update` would otherwise take a bare JSON string apart into its characters and sweep
    # something nobody authored, with no diagnostic.
    extra = tmp_path / "candidates.json"
    extra.write_text('"eval $X"', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON list of scripts"):
        tool.load_corpus(_ROOT, seeds=0, iterations=0, extra=extra)


def test_unclassified_ids_match_the_frozen_debt_snapshot() -> None:
    unclassified = tool.unclassified_ids(_ROOT)

    assert unclassified
    assert "scanner.source.character-limit" not in unclassified


def test_the_tool_accepts_no_alternate_root() -> None:
    # The scanner, the corpus grammar and the inventory checker are all imported from the checkout
    # holding the tool. A root naming another revision would filter a run against guards it never
    # executed, so the flag is refused rather than honored against the wrong tree.
    with pytest.raises(SystemExit) as raised:
        tool.main(["--root", str(_ROOT), "--trace", "echo hello"])

    assert raised.value.code == 2


def test_a_sweep_searches_the_checkout_whose_scanner_it_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The debt snapshot names the guards a sweep is looking for and the scanner decides which
    # guards exist. Read from different revisions, a reachable origin is filtered away silently.
    searched: list[Path] = []

    def record_debt(root: Path) -> frozenset[str]:
        searched.append(root)
        return frozenset()

    def record_corpus(root: Path, **_kwargs: object) -> list[str]:
        searched.append(root)
        return []

    monkeypatch.setattr(tool, "unclassified_ids", record_debt)
    monkeypatch.setattr(tool, "load_corpus", record_corpus)

    assert tool.main([]) == 0

    checkout = Path(shell_scanner.__file__).resolve().parents[3]
    assert searched == [checkout, checkout]


def test_a_trace_filters_against_the_checkout_whose_scanner_it_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same revision coupling on the trace side: the inventory supplies the accepted names and the
    # executed scanner supplies the frames, and a name only one of them carries drops the reach.
    filtered: list[Path] = []

    def record(root: Path) -> frozenset[str]:
        filtered.append(root)
        return frozenset()

    monkeypatch.setattr(tool, "guard_owning_qualnames", record)

    assert tool.main(["--trace", "echo hello"]) == 0

    assert filtered == [Path(shell_scanner.__file__).resolve().parents[3]]


def test_a_trace_records_frames_from_the_checkout_whose_scanner_it_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The traced surface is derived from a tree too, and it has to be the same one the accepted
    # names come from: a module read from another revision contributes frames the inventory never
    # names, or names the tracer never records.
    traced: list[Path] = []

    def record(root: Path) -> frozenset[str]:
        traced.append(root)
        return frozenset()

    monkeypatch.setattr(tool, "guarded_filenames", record)

    assert tool.main(["--trace", "echo hello"]) == 0

    assert traced == [Path(shell_scanner.__file__).resolve().parents[3]]


def test_main_traces_one_script_without_running_a_sweep(capsys: pytest.CaptureFixture) -> None:
    assert tool.main(["--trace", "eval 'X=${Y=q}'; eval \"$X\"lattice"]) == 0

    assert "_eval_syntax_record_assignment" in capsys.readouterr().out
