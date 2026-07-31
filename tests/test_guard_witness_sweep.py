"""Behavior tests for the fail-closed guard witness search tool."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
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


def test_rendered_rows_survive_a_script_carrying_a_lone_surrogate() -> None:
    # `--extra` candidates are read with `json.loads`, which accepts an escaped lone surrogate that
    # UTF-8 cannot encode. Left raw in a row, it fails the single write that emits the whole sweep,
    # so one such candidate costs every row the run found rather than only its own.
    script = "echo x\ud800"

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    rendered.encode("utf-8")
    row = ast.parse(f"[{rendered}]", mode="eval").body
    assert isinstance(row, ast.List)
    call = row.elts[0]
    assert isinstance(call, ast.Call)
    assert ast.literal_eval(call.args[1]) == script


def test_rendered_rows_refuse_a_label_they_cannot_place() -> None:
    # `sweep` accepts any labelled grid, and a label no limits class minted has no field to sit in.
    # Guessing one renders `ScanLimits(scanner=tiny-source)`, which parses as a subtraction rather
    # than failing, so the pasted row raises NameError at collection and takes the suite with it.
    with pytest.raises(ValueError, match="tiny-source"):
        tool.render_rows({"scanner.source.character-limit": ("tiny-source", "x")})


def test_rendered_rows_stay_within_the_repository_line_limit() -> None:
    # A row wider than the 100-character limit fails the ruff hook the moment it is pasted, so it
    # is not paste-ready however faithful the literal is.
    script = "echo " + "a" * 400

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    assert max(len(line) for line in rendered.splitlines()) <= 100
    row = ast.parse(f"[{rendered}]", mode="eval").body
    assert isinstance(row, ast.List)
    call = row.elts[0]
    assert isinstance(call, ast.Call)
    assert ast.literal_eval(call.args[1]) == script


def test_rendered_rows_carry_no_character_a_narrow_stdout_cannot_write() -> None:
    # Every row is emitted in one write, so a single non-ASCII candidate would cost a multi-minute
    # sweep every row it found rather than only its own.
    script = "doc-lattice '😀 café'"

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    assert rendered.isascii()
    rendered.encode("ascii")
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


def test_load_corpus_refuses_an_authored_candidate_it_would_drop(tmp_path: Path) -> None:
    # A hand-authored candidate silently length-filtered away prints the same nothing as one that
    # reached no guard, so the operator abandons a shape the sweep never scanned.
    extra = tmp_path / "candidates.json"
    extra.write_text(json.dumps(["echo " + "a" * 800]), encoding="utf-8")

    with pytest.raises(ValueError, match="max-length"):
        tool.load_corpus(_ROOT, seeds=0, iterations=0, extra=extra)


def test_sweep_reports_the_scripts_it_could_not_scan(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A candidate that crashes the scanner is dropped for every configuration. Counted nowhere, it
    # reads exactly like a candidate that reached nothing.
    def refuse(_script: str, *, limits: object) -> object:  # noqa: ARG001 - scanner signature
        raise RecursionError

    monkeypatch.setattr(tool, "scan_doc_lattice_invocations", refuse)

    assert tool.sweep(["echo one"], tool.limits_grid((0,))) == {}

    assert "skipped 1" in capsys.readouterr().err


def test_scanner_checkout_refuses_a_tree_that_holds_no_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An installed copy of the package derives a root holding none of the debt snapshot, replay
    # corpus or inventory a run reads, and the tool no longer takes a root to correct it with.
    installed = SimpleNamespace(
        __file__="/venv/lib/python3.13/site-packages/doc_lattice/github_ci/shell_scanner.py"
    )
    monkeypatch.setattr(tool.importlib, "import_module", lambda _dotted: installed)

    with pytest.raises(ValueError, match="source checkout"):
        tool.scanner_checkout()


def test_a_trace_refuses_an_option_only_the_sweep_reads() -> None:
    # Accepted and ignored, `--shrink` traces production limits while reporting on a run the
    # operator believes was shrunk, which is the wrong answer with nothing to say so.
    with pytest.raises(SystemExit) as raised:
        tool.main(["--trace", "echo hello", "--shrink", "0"])

    assert raised.value.code == 2


def test_a_sweep_refuses_an_option_only_a_trace_reads() -> None:
    # `--trace-all` alone otherwise falls through to a multi-minute sweep the operator did not ask
    # for, with no message that the flag was inert.
    with pytest.raises(SystemExit) as raised:
        tool.main(["--trace-all"])

    assert raised.value.code == 2


def test_help_describes_the_shrink_grid_the_sweep_is_built_around(
    capsys: pytest.CaptureFixture,
) -> None:
    # `--shrink` is what the whole sweep is organized around and is named in no other document, so
    # an operator reading `--shrink [SHRINK ...]` alone has nowhere to learn what a value means.
    with pytest.raises(SystemExit):
        tool.main(["--help"])

    documented = capsys.readouterr().out
    assert "seed" in documented
    assert "bodies" in documented
    assert "characters" in documented
    assert "cap" in documented


def test_trace_can_be_run_under_a_shrunk_cap() -> None:
    # The reach under production caps is not the reach under the caps a sweep searched, and the
    # deeper machinery a candidate is being aimed at is exactly what a shrunk cap cuts off.
    shrunk = tool.trace_guard_functions(
        "echo one; echo two", ScanLimits(scanner=ScannerLimits(max_scan_steps=0))
    )

    assert shrunk != tool.trace_guard_functions("echo one; echo two")
    assert shrunk - tool.trace_guard_functions("echo one; echo two")


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
