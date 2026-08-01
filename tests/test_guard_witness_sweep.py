"""Behavior tests for the fail-closed guard witness search tool."""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import multiprocessing
import os
import select
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import IO, TYPE_CHECKING

import pytest

from doc_lattice.github_ci import shell_guards, shell_scanner, shell_taint
from doc_lattice.github_ci.shell_guards import ScanLimits, ScannerLimits

if TYPE_CHECKING:
    from collections.abc import Iterator
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


def _qualnames(reached: set[tuple[str, str]]) -> set[str]:
    return {qualname for _module, qualname in reached}


def _reported_qualnames(out: str) -> set[str]:
    return {line.split(":", 1)[1] for line in out.split()}


def _declared_cap_count() -> int:
    defaults = ScanLimits()
    return sum(
        len(dataclasses.fields(getattr(defaults, field.name)))
        for field in dataclasses.fields(defaults)
    )


def test_limits_grid_shrinks_every_declared_cap() -> None:
    labels = {label for label, _limits in tool.limits_grid((0,))}
    shrunk = labels - {tool.PRODUCTION}

    assert "TaintLimits(max_edges=0)" in shrunk
    assert "ScannerLimits(max_scan_steps=0)" in shrunk
    # Every field of both limits values must appear, or a cap silently goes unsearched.
    assert len(shrunk) == _declared_cap_count()


def test_limits_grid_searches_a_repeated_shrink_value_once() -> None:
    # A duplicate mints an identical configuration under an identical label, which loses the
    # tie-break to the first one, so the only thing it can add is another pass over the whole
    # corpus in a run that already takes several minutes.
    assert tool.limits_grid((0, 3, 0)) == tool.limits_grid((0, 3))


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


def test_sweep_prefers_a_least_shrunk_reach_to_a_shorter_shrunk_one() -> None:
    # The two preferences compete whenever a longer script reaches a guard under looser caps, and
    # only one of them is about the strength of the evidence: authored input reaching the guard
    # under production caps says the guard is reachable, where the same guard under a shrunk cap
    # says only that a resource bound refuses. Resolved by length first, the run pastes the weaker
    # claim into the registry and the stronger one it also found is gone.
    grid = [
        (
            "ScannerLimits(max_source_chars=30)",
            ScanLimits(scanner=ScannerLimits(max_source_chars=30)),
        ),
        (
            "ScannerLimits(max_source_chars=4)",
            ScanLimits(scanner=ScannerLimits(max_source_chars=4)),
        ),
    ]

    found = tool.sweep(["a" * 40, "a" * 8], grid)

    assert found["scanner.source.character-limit"] == (
        "ScannerLimits(max_source_chars=30)",
        "a" * 40,
    )


def test_sweep_keeps_the_better_row_a_reused_accumulator_holds() -> None:
    # The accumulator is documented as caller-owned, so a corpus swept in chunks shares one. Scored
    # from an empty map each call, the first reach of the second chunk replaces a better row from
    # the first with no comparison at all.
    grid = [
        (
            "ScannerLimits(max_source_chars=30)",
            ScanLimits(scanner=ScannerLimits(max_source_chars=30)),
        ),
        (
            "ScannerLimits(max_source_chars=4)",
            ScanLimits(scanner=ScannerLimits(max_source_chars=4)),
        ),
    ]
    found: dict[str, tuple[str, str]] = {}

    tool.sweep(["a" * 40], grid, found=found)
    tool.sweep(["a" * 8], grid, found=found)

    assert found["scanner.source.character-limit"] == (
        "ScannerLimits(max_source_chars=30)",
        "a" * 40,
    )


def test_sweep_keeps_a_row_this_grid_cannot_score() -> None:
    # A label this grid does not mint came from a grid this call cannot rank, and ranking it last
    # is not neutrality: the first entry the grid mints then displaces it, so a production reach an
    # earlier call recorded loses to a shrunk-cap reach here and the registry pins the weaker claim.
    found = {"scanner.source.character-limit": (tool.PRODUCTION, "a" * 40)}
    grid = [
        (
            "ScannerLimits(max_source_chars=4)",
            ScanLimits(scanner=ScannerLimits(max_source_chars=4)),
        )
    ]

    tool.sweep(["a" * 8], grid, found=found)

    assert found["scanner.source.character-limit"] == (tool.PRODUCTION, "a" * 40)


def test_sweep_can_restrict_itself_to_still_unclassified_guards() -> None:
    found = tool.sweep(
        ["echo one; echo two"],
        tool.limits_grid((0,)),
        wanted=frozenset({"scanner.source.character-limit"}),
    )

    assert "scanner.budget.step-limit" not in found


def _entry(rank: int, label: str, origin: str, script: str) -> object:
    return tool.EntryReach(
        rank=rank,
        label=label,
        best={origin: script},
        scanned=frozenset({script}),
        skipped=frozenset(),
    )


_PRODUCTION_ENTRY = ("production", ScanLimits())
_SHRUNK_ENTRY = (
    "ScannerLimits(max_source_chars=4)",
    ScanLimits(scanner=ScannerLimits(max_source_chars=4)),
)
_ORIGIN = "scanner.source.character-limit"


def test_a_parallel_sweep_reports_what_the_serial_one_reports() -> None:
    # The whole closure criterion: splitting the grid across processes changes how long a run
    # takes and nothing about what it prints.
    corpus = ["echo one; echo two", "a" * 40, "eval 'X=${Y=q}'; eval \"$X\"lattice"]
    grid = tool.limits_grid((0, 2))

    assert tool.sweep(corpus, grid, jobs=4) == tool.sweep(corpus, grid)


def test_a_merge_prefers_the_least_shrunk_reach_whatever_order_workers_finish_in() -> None:
    # Workers finish in whatever order the scheduler hands back, so a merge keyed on arrival prints
    # different rows for the same corpus run to run, and half of them pin the weaker claim.
    grid = [_PRODUCTION_ENTRY, _SHRUNK_ENTRY]
    entries = [
        _entry(0, _PRODUCTION_ENTRY[0], _ORIGIN, "a" * 40),
        _entry(1, _SHRUNK_ENTRY[0], _ORIGIN, "a" * 8),
    ]
    rows = []
    for arrival in (entries, list(reversed(entries))):
        found: dict[str, tuple[str, str]] = {}
        tool.merge_reach(arrival, tool.initial_totals(grid, found), found)
        rows.append(found)

    assert rows[0] == rows[1] == {_ORIGIN: (_PRODUCTION_ENTRY[0], "a" * 40)}


def test_a_merge_counts_no_script_another_configuration_scanned(
    capsys: pytest.CaptureFixture,
) -> None:
    # Each worker sees only its own configuration, so a difference taken per worker reports every
    # body one shrunk configuration could not parse, which is the overcount the union avoids.
    grid = [_PRODUCTION_ENTRY, _SHRUNK_ENTRY]
    totals = tool.initial_totals(grid, {})

    tool.merge_reach(
        [
            tool.EntryReach(0, grid[0][0], {}, frozenset({"deep"}), frozenset()),
            tool.EntryReach(1, grid[1][0], {}, frozenset(), frozenset({"deep"})),
        ],
        totals,
        {},
    )
    tool.report_unscanned(totals)

    assert "skipped" not in capsys.readouterr().err


def test_a_merge_counts_a_script_no_configuration_scanned(
    capsys: pytest.CaptureFixture,
) -> None:
    # The other half of the union: a body every configuration refused is coverage the run does not
    # have, and silence about it reads as a candidate that reached nothing.
    grid = [_PRODUCTION_ENTRY, _SHRUNK_ENTRY]
    totals = tool.initial_totals(grid, {})

    tool.merge_reach(
        [
            tool.EntryReach(0, grid[0][0], {}, frozenset(), frozenset({"deep"})),
            tool.EntryReach(1, grid[1][0], {}, frozenset(), frozenset({"deep"})),
        ],
        totals,
        {},
    )
    tool.report_unscanned(totals)

    assert "skipped 1" in capsys.readouterr().err


def test_a_merge_keeps_what_the_configurations_before_a_failure_found() -> None:
    # A pool collects results as its workers finish, so a merge that waited for the whole grid
    # would lose every completed configuration's reach to the one worker that died.
    grid = [_PRODUCTION_ENTRY, _SHRUNK_ENTRY]
    found: dict[str, tuple[str, str]] = {}

    def arriving() -> Iterator[object]:
        yield _entry(0, _PRODUCTION_ENTRY[0], _ORIGIN, "a" * 40)
        raise ValueError("a worker died holding a configuration")

    with pytest.raises(ValueError, match="died holding"):
        tool.merge_reach(arriving(), tool.initial_totals(grid, found), found)

    assert found == {_ORIGIN: (_PRODUCTION_ENTRY[0], "a" * 40)}


def test_a_pooled_sweep_keeps_the_rows_the_worker_that_died_was_not_holding() -> None:
    # The same guarantee through real processes. One worker runs the two configurations in
    # submission order, so the reach of the first is merged before the second one raises what no
    # configuration was expected to raise. `as_completed` hands back whatever is already finished
    # in an order nobody promises, but nothing can be: this frame reaches the waiter in microseconds
    # and the pool has yet to start the interpreter that will scan the first unit, so the two
    # results are yielded as they complete.
    grid = [_SHRUNK_ENTRY, ("no caps at all", object())]
    found: dict[str, tuple[str, str]] = {}
    entries = tool.pooled_entries(["a" * 40], grid, jobs=1)

    with pytest.raises(AttributeError):
        tool.merge_reach(entries, tool.initial_totals(grid, found), found)

    assert found == {_ORIGIN: (_SHRUNK_ENTRY[0], "a" * 40)}


def test_a_pooled_run_resolves_its_filter_and_prints_its_rows_once(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reads that configure a run and the filter it restricts reach with belong to the parent.
    # Repeated per worker they become N copies of one diagnostic, and N copies of one row.
    resolved: list[Path] = []

    def record_debt(root: Path) -> frozenset[str]:
        resolved.append(root)
        return frozenset({_ORIGIN})

    monkeypatch.setattr(tool, "unclassified_ids", record_debt)
    monkeypatch.setattr(tool, "load_corpus", lambda _root, **_kwargs: ["a" * 40])

    assert tool.main(["--shrink", "0", "--jobs", "2"]) == 0

    assert len(resolved) == 1
    assert capsys.readouterr().out.count(_ORIGIN) == 1


def test_a_pooled_run_reports_the_recorded_scripts_it_dropped_once(
    capsys: pytest.CaptureFixture,
) -> None:
    # The corpus is read once by the parent and handed to the workers, so the count of recorded
    # bodies the length filter dropped is written once however many ways the grid is split.
    status = tool.main(
        ["--seeds", "0", "--iterations", "0", "--max-length", "12", "--shrink", "0", "--jobs", "2"]
    )

    assert status == 0
    assert capsys.readouterr().err.count("dropped") == 1


def test_pooled_workers_do_not_inherit_this_process() -> None:
    # A forked worker inherits a scanner a caller has replaced here, so the same run reports reach
    # through one scanner on Linux and another everywhere else. It is also what CPython deprecates
    # once a process has threads, which a pool has by its second worker.
    assert tool.start_method() != "fork"
    assert tool.start_method() in multiprocessing.get_all_start_methods()


_INTERRUPT_DRIVER = '''\
"""Run a pooled sweep long enough to be interrupted, announcing when it is under way.

Driven as its own process because the behavior under test is what a Ctrl-C does to a process
group: this one is signalled along with every worker it started. The work is guarded, since a
worker re-imports this module to reach the sweep it was told to run.
"""

import importlib.util
import sys


def load_tool(path):
    """Return the sweep tool loaded from a path, the way the suite loads it."""
    spec = importlib.util.spec_from_file_location("guard_witness_sweep", path)
    tool = importlib.util.module_from_spec(spec)
    sys.modules["guard_witness_sweep"] = tool
    spec.loader.exec_module(tool)
    return tool


def main():
    """Sweep a corpus that outlasts the interrupt, announcing the first unit of work merged."""
    tool = load_tool(sys.argv[1])
    merge_entry = tool.merge_entry
    merged = []

    def announce(entry, totals, found):
        merge_entry(entry, totals, found)
        if not merged:
            merged.append(entry)
            print("under way", flush=True)

    tool.merge_entry = announce
    corpus = ["echo %d; eval 'X=${Y=q}'" % index for index in range(int(sys.argv[2]))]
    tool.sweep(corpus, tool.limits_grid((0, 1, 2)), jobs=2)
    print("finished", flush=True)


if __name__ == "__main__":
    main()
'''

_INTERRUPT_CORPUS = 900
"""Scripts the interrupted run sweeps, sized so a grid of them outlasts the interrupt by far."""

_INTERRUPT_PATIENCE = 60.0
"""Seconds a stopped run is given to be gone. Measured at 0.6s to 2.5s; a regression never ends."""


def _await_line(stream: IO[str], patience: float) -> str:
    """Return the next line `stream` produces, or the empty string if it produces none in time."""
    ready, _writable, _failed = select.select([stream], [], [], patience)
    return stream.readline() if ready else ""


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are a POSIX signal contract")
def test_an_interrupted_pooled_run_stops_rather_than_having_to_be_killed(tmp_path: Path) -> None:
    # A search tool that cannot be interrupted is worse than a slow one, and this is the one thing a
    # pool takes away by default: torn down as the interpreter finalizes, its stop-work sentinels
    # never reach workers already blocked waiting for them, and the run hangs until it is killed.
    # Signalled twice, because that is what an operator's single Ctrl-C amounts to under the
    # documented `uv run` command, which forwards a copy of the one the terminal sent the group, and
    # because a second interrupt landing inside the teardown is what makes the deadlock reachable.
    driver = tmp_path / "interruptible_sweep.py"
    driver.write_text(_INTERRUPT_DRIVER, encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603 - this interpreter, and a driver written just above
        [sys.executable, str(driver), str(_TOOL), str(_INTERRUPT_CORPUS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    try:
        assert _await_line(process.stdout, _INTERRUPT_PATIENCE).strip() == "under way"
        os.killpg(process.pid, signal.SIGINT)
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=_INTERRUPT_PATIENCE)
    except subprocess.TimeoutExpired:
        pytest.fail(f"the run was still alive {_INTERRUPT_PATIENCE}s after being interrupted")
    finally:
        process.stdout.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_the_default_worker_count_fits_the_work_there_is(monkeypatch: pytest.MonkeyPatch) -> None:
    # A default derived from the machine rather than from the grid starts workers with nothing to
    # scan, and one that can reach zero leaves the sweep with nobody to run it. Bounds rather than
    # the machine's count, which restates the expression: read off a host whose affinity the
    # interpreter cannot see, `os.process_cpu_count()` is None, and the count that has to come back
    # from that is one.
    assert tool.default_jobs(1) == 1
    assert tool.default_jobs(0) == 1
    assert 1 <= tool.default_jobs(1000) <= 1000

    monkeypatch.setattr(tool.os, "process_cpu_count", lambda: None)

    assert tool.default_jobs(1000) == 1


def test_a_sweep_with_no_workers_is_refused_rather_than_run_by_nobody() -> None:
    # Zero workers is not a smaller sweep, and left to argparse it is refused several layers down
    # in a traceback naming the pool rather than the option that sized it.
    with pytest.raises(SystemExit) as raised:
        tool.main(["--jobs", "0"])

    assert raised.value.code == 2


def test_a_trace_refuses_the_worker_count_only_a_sweep_reads() -> None:
    # A trace runs one script in this process. Accepted and ignored, `--jobs` reports on a run the
    # operator believes was split.
    with pytest.raises(SystemExit) as raised:
        tool.main(["--trace", "echo hello", "--jobs", "2"])

    assert raised.value.code == 2


def test_trace_reports_the_guarded_functions_a_script_executes() -> None:
    # This is what locates a reaching *shape* when the sweep finds nothing: it shows which guard
    # machinery a candidate reaches at all, so the next candidate can be aimed one level deeper.
    reached = _qualnames(tool.trace_guard_functions("eval 'X=${Y=q}'; eval \"$X\"lattice"))

    assert "_eval_syntax_record_assignment" in reached
    assert "_eval_syntax_record_decision" in reached


def test_trace_records_the_module_each_function_ran_in() -> None:
    # A bare qualified name cannot say which module ran: `_is_function_positional_parameter` is
    # defined in both guarded modules today. Recorded without the module, reach in one of them is
    # accepted against a guard the other one owns.
    reached = tool.trace_guard_functions("eval 'X=${Y=q}'; eval \"$X\"lattice")

    assert ("shell_taint.py", "_eval_syntax_record_assignment") in reached


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


def test_trace_keeps_the_previously_installed_tracer_running_during_the_scan() -> None:
    # Restoring the ambient hook afterwards only protects what runs later. Replacing it for the
    # duration of the scan is still disabling it, and over exactly the frames a settrace-based
    # coverage run or a debugger session cares about most.
    observed: list[str] = []

    def ambient(frame: object, event: str, _argument: object) -> None:
        if event == "call":
            observed.append(frame.f_code.co_filename)  # ty: ignore[unresolved-attribute]

    restore = sys.gettrace()
    sys.settrace(ambient)
    try:
        tool.trace_guard_functions("echo one; echo two")
    finally:
        sys.settrace(restore)

    assert any(name == shell_scanner.__file__ for name in observed)


def test_trace_keeps_reporting_under_an_ambient_hook_that_reinstalls_itself() -> None:
    # The hook above is a plain Python function, which leaves the global hook where it found it.
    # `coverage.CTracer` does not: invoked through Python dispatch it re-installs itself at the C
    # level, which is how a delegating hook loses the dispatch it delegated from. Under that hook
    # a trace that only delegates records its own entry frame and nothing below it, so the ambient
    # hook has to be handed the event *and* the global hook taken back afterwards.
    restore = sys.gettrace()
    reinstalling: list[object] = []

    def ambient(frame: object, event: str, _argument: object) -> None:
        if event == "call":
            reinstalling.append(frame.f_code.co_filename)  # ty: ignore[unresolved-attribute]
        sys.settrace(ambient)

    sys.settrace(ambient)
    try:
        reached = _qualnames(tool.trace_guard_functions("eval 'X=${Y=q}'; eval \"$X\"lattice"))
    finally:
        sys.settrace(restore)

    assert "_eval_syntax_record_assignment" in reached
    assert any(name == shell_scanner.__file__ for name in reinstalling)


def test_trace_distinguishes_a_shape_that_never_reaches_that_machinery() -> None:
    reached = _qualnames(tool.trace_guard_functions("echo hello"))

    assert "_eval_syntax_record_assignment" not in reached


def test_guard_owning_functions_come_from_the_recorded_inventory() -> None:
    owning = _qualnames(tool.guard_owning_functions(_ROOT))

    assert "_eval_syntax_record_assignment" in owning
    assert "_ascii_lower" not in owning


def test_guard_owning_functions_name_the_module_that_owns_the_guard() -> None:
    # The inventory knows which module each origin lives in, and dropping that is what lets one
    # module's reach be accepted as evidence for another module's guard of the same name.
    owning = tool.guard_owning_functions(_ROOT)

    assert ("shell_taint.py", "_eval_syntax_record_assignment") in owning
    assert ("shell_scanner.py", "_eval_syntax_record_assignment") not in owning


def test_trace_omits_a_function_whose_guard_another_module_owns(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `_ScanBudget.step` runs in the scanner, so a guard of that name owned by the taint module is
    # evidence this candidate does not carry. Intersected on the bare name it would be printed,
    # and the pasted witness then fails asserting an origin the shape never reaches.
    monkeypatch.setattr(
        tool,
        "guard_owning_functions",
        lambda _root: frozenset({("shell_taint.py", "_ScanBudget.step")}),
    )

    assert tool.main(["--trace", "echo one; echo two"]) == 0

    assert capsys.readouterr().out.split() == []


def test_guard_owning_functions_spell_a_nested_guard_the_way_a_frame_does() -> None:
    # The inventory derives its qualified names from the source tree and the tracer reads them off
    # a running frame. A nested guard is where the two spellings can drift apart, and a drift would
    # filter real reach away silently rather than reporting anything wrong.
    owning = _qualnames(tool.guard_owning_functions(_ROOT))

    assert "_contextualize_evidence.charge_edges" in owning
    assert "_contextualize_evidence.charge_edges" in _qualnames(
        tool.trace_guard_functions("echo hello")
    )


def test_trace_output_omits_a_guarded_module_function_owning_no_guard(
    capsys: pytest.CaptureFixture,
) -> None:
    # Every call in a guarded module swamps the reach signal this mode exists to give and presents
    # ordinary helpers as guard machinery.
    assert tool.main(["--trace", "echo hello"]) == 0

    reported = _reported_qualnames(capsys.readouterr().out)
    assert "_ScanBudget.step" in reported
    assert "_ascii_lower" not in reported


def test_trace_all_reports_the_whole_guarded_module_reach(
    capsys: pytest.CaptureFixture,
) -> None:
    # Aiming the next candidate deeper uses the functions between the guards too: most of what
    # separates the worked eval shape from a plain one owns no guard of its own.
    assert tool.main(["--trace", "echo hello", "--trace-all"]) == 0

    assert "_ascii_lower" in _reported_qualnames(capsys.readouterr().out)


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


def test_rendered_rows_quote_a_script_the_formatter_would_requote() -> None:
    # `ruff format` runs from the same pre-commit hook the line limit does, and it rewrites a
    # double-quoted literal that escapes a quote it could have avoided. Emitted with the escape,
    # the row is rewritten the moment it is pasted, so the registry never holds the row the sweep
    # printed and the contributor has to re-stage the file the hook just changed.
    script = 'env FOO="$@" harmless'

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    assert "'env FOO=\"$@\" harmless'" in rendered
    row = ast.parse(f"[{rendered}]", mode="eval").body
    assert isinstance(row, ast.List)
    call = row.elts[0]
    assert isinstance(call, ast.Call)
    assert ast.literal_eval(call.args[1]) == script


def test_rendered_rows_keep_double_quotes_when_switching_removes_no_escape() -> None:
    # The formatter only switches quoting when it removes every escape, so a script carrying both
    # quote characters stays double-quoted. Switched anyway, the row grows an escaped apostrophe
    # the formatter then reverses, which is the same churn the other direction.
    script = "env FOO=\"$@\" 'harmless'"

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    assert '"env FOO=\\"$@\\" \'harmless\'"' in rendered
    row = ast.parse(f"[{rendered}]", mode="eval").body
    assert isinstance(row, ast.List)
    call = row.elts[0]
    assert isinstance(call, ast.Call)
    assert ast.literal_eval(call.args[1]) == script


def test_rendered_rows_quote_a_wrapped_part_on_its_own() -> None:
    # The formatter quotes each piece of an implicit concatenation separately, so a row wide enough
    # to wrap can need one part single-quoted and the next double-quoted. Decided once for the whole
    # script, whichever parts disagree with it are rewritten on paste.
    script = 'echo "' + "a" * 200 + "' " + "b" * 200

    rendered = tool.render_rows({"scanner.budget.step-limit": (tool.PRODUCTION, script)})

    quotes = {line.strip()[0] for line in rendered.splitlines() if line.startswith(tool.ROW_INDENT)}
    assert quotes == {"'", '"'}
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


@pytest.mark.parametrize("digits", [40, 61])
def test_rendered_rows_wrap_a_caps_value_too_wide_for_one_line(digits: int) -> None:
    # The literals either side of it are wrapped for exactly this reason, and `--shrink` takes any
    # non-negative integer, so the caps line can be the widest thing in a row. Emitted flat, the
    # pasted row fails the ruff hook the rest of the renderer exists to satisfy.
    label = f"ScannerLimits(max_scan_steps={10 ** (digits - 1)})"

    rendered = tool.render_rows({"scanner.budget.step-limit": (label, "x")})

    assert max(len(line) for line in rendered.splitlines()) <= 100
    row = ast.parse(f"[{rendered}]", mode="eval").body
    assert isinstance(row, ast.List)
    call = row.elts[0]
    assert isinstance(call, ast.Call)
    assert ast.unparse(call.keywords[0].value).replace(" ", "") == f"ScanLimits(scanner={label})"


def test_rendered_rows_refuse_a_caps_value_no_line_can_carry() -> None:
    # Nothing splits an integer literal, so a value wider than the limit on a line of its own has
    # no rendering; said here rather than emitted as a row nobody can paste.
    label = f"ScannerLimits(max_scan_steps={10**200})"

    with pytest.raises(ValueError, match="line limit"):
        tool.render_rows({"scanner.budget.step-limit": (label, "x")})


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


def test_guarded_filenames_import_a_package_initializer_as_its_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Discovery returns `__init__.py` the moment the package re-exports the guard protocol, and
    # `package.__init__` is a module name CPython has not imported: it builds a second module
    # object and runs the initializer again, so its module-level effects run twice in this process.
    imported: list[str] = []
    import_module = tool.importlib.import_module

    def record(dotted: str) -> ModuleType:
        imported.append(dotted)
        return import_module(dotted)

    monkeypatch.setattr(
        tool, "guarded_modules", lambda _root: (f"{checker.GUARD_MODULE_ROOT}/__init__.py",)
    )
    monkeypatch.setattr(tool.importlib, "import_module", record)

    tool.guarded_filenames(_ROOT)

    assert imported == ["doc_lattice.github_ci"]


def test_guarded_filenames_refuse_a_module_they_cannot_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The gate reads the guard package as inert source text and discovers any module in it that
    # mentions the protocol, one still being written included, which is exactly when a trace is
    # worth running. Left to propagate, the half-written module's own traceback is all the operator
    # gets, and it says nothing about which tool stopped or why.
    root = tmp_path / "candidate"
    module = f"{checker.GUARD_MODULE_ROOT}/shell_budget.py"
    (root / module).parent.mkdir(parents=True)
    (root / module).write_text("raise ImportError('half written')\n", encoding="utf-8")
    monkeypatch.setattr(tool, "guarded_modules", lambda _root: (module,))

    with pytest.raises(ValueError, match="shell_budget"):
        tool.guarded_filenames(root)


def test_guarded_filenames_refuse_a_module_that_fails_to_import_for_another_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A module body runs on import, so an unfinished one fails in more ways than an unfinished
    # import statement: a typo'd constant raises NameError, and a helper that is not written yet
    # raises AttributeError or TypeError from a decorator or a module-level call. Each one aborts
    # the trace with the module's own traceback, which says nothing about the traced surface.
    def explode(_dotted: str) -> ModuleType:
        message = "name 'MAX_EVAL_DEPTH' is not defined"
        raise NameError(message)

    monkeypatch.setattr(
        tool, "guarded_modules", lambda _root: (f"{checker.GUARD_MODULE_ROOT}/shell_scanner.py",)
    )
    monkeypatch.setattr(tool.importlib, "import_module", explode)

    with pytest.raises(ValueError, match="could not be imported"):
        tool.guarded_filenames(_ROOT)


def test_guarded_filenames_skip_a_module_the_tree_no_longer_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The traced surface unions the checker's hand-kept allowlist with what it discovers, so an
    # entry naming a module a contributor has just moved outlives the file until the allowlist is
    # edited. Refused, every trace aborts during a guard move, and the message diagnoses a deleted
    # module as a half-written one; the module contributes no origins to intersect against either.
    monkeypatch.setattr(
        tool,
        "guarded_modules",
        lambda _root: (
            f"{checker.GUARD_MODULE_ROOT}/shell_scanner.py",
            f"{checker.GUARD_MODULE_ROOT}/shell_moved_away.py",
        ),
    )

    assert shell_scanner.__file__ in tool.guarded_filenames(_ROOT)


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


def test_load_corpus_refuses_an_extra_file_it_cannot_find(tmp_path: Path) -> None:
    # Every other read on this path refuses with the file it could not read and what writes it. The
    # one input the operator authored themselves would report a bare FileNotFoundError naming
    # neither the option that read it nor what it is supposed to hold.
    with pytest.raises(ValueError, match="--extra"):
        tool.load_corpus(_ROOT, seeds=0, iterations=0, extra=tmp_path / "candidates.json")


def test_load_corpus_refuses_an_extra_file_it_cannot_parse(tmp_path: Path) -> None:
    # A decoder error reports a line and column of a file the message never names, in a tool whose
    # every other refusal names both the file and what regenerates it.
    extra = tmp_path / "candidates.json"
    extra.write_text('["eval $X",]', encoding="utf-8")

    with pytest.raises(ValueError, match="--extra"):
        tool.load_corpus(_ROOT, seeds=0, iterations=0, extra=extra)


def test_load_corpus_refuses_an_authored_candidate_it_would_drop(tmp_path: Path) -> None:
    # A hand-authored candidate silently length-filtered away prints the same nothing as one that
    # reached no guard, so the operator abandons a shape the sweep never scanned.
    extra = tmp_path / "candidates.json"
    extra.write_text(json.dumps(["echo " + "a" * 800]), encoding="utf-8")

    with pytest.raises(ValueError, match="max-length"):
        tool.load_corpus(_ROOT, seeds=0, iterations=0, extra=extra)


def test_load_corpus_reports_a_recorded_script_the_length_filter_drops(
    capsys: pytest.CaptureFixture,
) -> None:
    # The refusal above says why an authored candidate cannot be dropped in silence, and the same
    # reading applies to a recorded one: the recorded half is the shapes the scanner was built
    # against, so a sweep that never scanned one prints what a sweep that scanned it and reached no
    # guard prints. The filter still has to bound the grammar, so this is counted, not refused.
    tool.load_corpus(_ROOT, seeds=0, iterations=0, max_length=8)

    assert "recorded script" in capsys.readouterr().err


def test_load_corpus_stays_quiet_when_it_drops_no_recorded_script(
    capsys: pytest.CaptureFixture,
) -> None:
    tool.load_corpus(_ROOT, seeds=0, iterations=0, max_length=1_000_000)

    assert "recorded script" not in capsys.readouterr().err


def test_a_trace_that_reaches_nothing_says_so(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty stdout and a zero exit is also what a run that never traced anything prints, and
    # telling the two apart is the whole answer this mode was asked for.
    monkeypatch.setattr(tool, "guard_owning_functions", lambda _root: frozenset())

    assert tool.main(["--trace", "echo hello"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "reached 0 guard-holding functions" in captured.err


def test_a_trace_counts_the_functions_it_reported(capsys: pytest.CaptureFixture) -> None:
    assert tool.main(["--trace", "eval 'X=${Y=q}'; eval \"$X\"lattice"]) == 0

    captured = capsys.readouterr()
    assert f"reached {len(captured.out.split())} guard-holding functions" in captured.err


def test_a_trace_all_run_counts_the_wider_surface_it_reported(
    capsys: pytest.CaptureFixture,
) -> None:
    assert tool.main(["--trace", "echo hello", "--trace-all"]) == 0

    captured = capsys.readouterr()
    assert f"reached {len(captured.out.split())} guarded-module functions" in captured.err


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


def test_sweep_counts_no_script_another_configuration_scanned(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deep recursion is a property of the script and the caps together, so a body only the most
    # shrunk entry cannot parse is still scored by every other one. Counted as unscannable, it
    # sends the operator after coverage the sweep already had.
    scanned = shell_scanner.scan_doc_lattice_invocations

    def refuse_when_shrunk(script: str, *, limits: ScanLimits) -> object:
        if limits.scanner.max_scan_steps == 0:
            raise RecursionError
        return scanned(script, limits=limits)

    monkeypatch.setattr(tool, "scan_doc_lattice_invocations", refuse_when_shrunk)

    tool.sweep(["echo one; echo two"], tool.limits_grid((0, 3)))

    assert "skipped" not in capsys.readouterr().err


def test_trace_reports_a_candidate_it_could_not_finish_scanning(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A truncated trace prints a shorter reach, which is the same output as a candidate that
    # genuinely stops there. Silence about the truncation is what turns it into an invariant
    # justification for a guard the candidate was still walking toward.
    def refuse(_script: str, *, limits: object) -> object:  # noqa: ARG001 - scanner signature
        raise RecursionError

    monkeypatch.setattr(tool, "scan_doc_lattice_invocations", refuse)

    tool.trace_guard_functions("echo one")

    assert "recursion limit" in capsys.readouterr().err


def test_a_sweep_prints_the_rows_it_found_before_a_scan_it_could_not_finish(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every row is buffered until the run ends, so an unexpected scanner failure would otherwise
    # cost a multi-minute sweep every origin it had already reached, and exactly for the shape a
    # witness search exists to find.
    scanned = shell_scanner.scan_doc_lattice_invocations

    def fail_once_a_row_has_been_found(script: str, *, limits: ScanLimits) -> object:
        # Later in the grid than the source-character cap, so the run has already reached a guard.
        if limits.scanner.max_scan_steps == 0:
            raise ValueError("scanner failed on a shape nothing pinned")
        return scanned(script, limits=limits)

    monkeypatch.setattr(tool, "scan_doc_lattice_invocations", fail_once_a_row_has_been_found)
    monkeypatch.setattr(tool, "unclassified_ids", lambda _root: None)
    monkeypatch.setattr(tool, "load_corpus", lambda _root, **_kwargs: ["echo one; echo two"])

    # Serial, because this pins the finest granularity the guarantee has: a worker that dies takes
    # its whole configuration with it, and a scanner replaced here is not the one a worker imports.
    with pytest.raises(ValueError, match="nothing pinned"):
        tool.main(["--shrink", "0", "--jobs", "1"])

    assert "scanner.source.character-limit" in capsys.readouterr().out


@pytest.mark.parametrize("option", ["--seeds", "--iterations", "--max-length"])
def test_a_negative_corpus_size_is_refused_rather_than_emptying_the_corpus(option: str) -> None:
    # `range(-1)` walks no grammar and a negative length filter drops every script, so a negative
    # value sweeps the recorded half alone, or nothing at all, while reporting a script count that
    # reads like the run the operator asked for.
    with pytest.raises(SystemExit) as raised:
        tool.main([option, "-1"])

    assert raised.value.code == 2


def test_a_zero_corpus_size_is_still_accepted() -> None:
    # Zero is how a quick pass over the recorded corpus alone is asked for, and how the tests here
    # skip generation, so the refusal has to be of negatives rather than of anything falsy.
    assert tool.nonnegative_count("0") == 0


def test_a_negative_shrink_is_refused_rather_than_witnessing_under_a_degenerate_cap() -> None:
    # A negative cap is not a smaller bound but a broken one: every count exceeds it before the
    # scan has read anything, so the empty script renders a row for a recursion-depth guard, and
    # the configurations refusing that early mask the guards a zero cap reaches with a real shape.
    with pytest.raises(SystemExit) as raised:
        tool.main(["--shrink", "-1"])

    assert raised.value.code == 2


def test_a_zero_shrink_is_still_accepted() -> None:
    # Zero is the most-shrunk value the default grid searches, so the refusal has to be of
    # negatives rather than of anything falsy.
    assert tool.nonnegative_cap("0") == 0


def test_a_bare_shrink_is_refused_rather_than_searching_production_only() -> None:
    # `--shrink` with no values would otherwise bind an empty list, collapsing the grid to
    # production caps, and report the resource-bound guards as unreached by a run the operator
    # believes searched every shrunk cap.
    with pytest.raises(SystemExit) as raised:
        tool.main(["--shrink"])

    assert raised.value.code == 2


def test_slot_keys_are_the_caps_classes_the_grid_constructs() -> None:
    # The label a sweep mints and the field a row is rendered into are two readings of the same
    # `ScanLimits` shape. Derived apart, a renderer that cannot place a label raises only after
    # the whole sweep has run, discarding every row it found.
    minted = {label.split("(", 1)[0] for label, _limits in tool.limits_grid((0,))}

    assert set(tool.limits_slots()) == minted - {tool.PRODUCTION}


def test_slots_survive_a_scan_limits_field_annotated_as_optional() -> None:
    # The annotation is text under `from __future__ import annotations`, so `TaintLimits | None`
    # or an aliased import silently stops matching the class name the grid mints.
    @dataclasses.dataclass(frozen=True)
    class OptionallyTainted:
        taint: shell_guards.TaintLimits | None = dataclasses.field(
            default_factory=shell_guards.TaintLimits
        )

    assert tool.caps_slots(OptionallyTainted) == {"TaintLimits": "taint"}


def test_slots_refuse_two_fields_carrying_the_same_caps_class() -> None:
    # The caps class name is what the grid mints a label from and what the renderer places one back
    # with. Collapsed onto one entry, the field that loses is never shrunk at all, so the guards its
    # caps govern are reported unreached by a run that never configured them, and a row for the
    # field that survived names caps the sweep did not run under.
    @dataclasses.dataclass(frozen=True)
    class TwiceTainted:
        taint: shell_guards.TaintLimits = dataclasses.field(
            default_factory=shell_guards.TaintLimits
        )
        eval_taint: shell_guards.TaintLimits = dataclasses.field(
            default_factory=shell_guards.TaintLimits
        )

    with pytest.raises(ValueError, match="TaintLimits"):
        tool.caps_slots(TwiceTainted)


def test_load_corpus_refuses_a_replay_inventory_it_cannot_read(tmp_path: Path) -> None:
    # A bare KeyError from a restructured fixture names neither the fixture nor the script that
    # regenerates it, in a tool whose whole point is that a run reports what it was asked.
    root = tmp_path / "candidate"
    inventory = root / tool.REPLAY_INVENTORY
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({"entries": [{"script": "echo one"}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="checkpoint_record_scanner_inputs"):
        tool.load_corpus(root, seeds=0, iterations=0)


def test_load_corpus_refuses_a_replay_inventory_that_records_no_scripts(tmp_path: Path) -> None:
    # An empty entry list satisfies every per-entry check vacuously, so a sweep would search the
    # generated half alone while reporting a script count that reads like the whole corpus. The
    # recorded half is the shapes the scanner was actually built against, and losing it silently
    # turns "reached nothing" into an answer about a corpus nobody asked to run.
    root = tmp_path / "candidate"
    inventory = root / tool.REPLAY_INVENTORY
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({"entries": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="checkpoint_record_scanner_inputs"):
        tool.load_corpus(root, seeds=1, iterations=1)


def test_scanner_checkout_refuses_a_copy_resolving_near_the_filesystem_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Walking back the recorded path's depth from a flattened copy has no ancestor to reach, and
    # an index traceback names nothing the refusal below it was written to explain.
    flattened = SimpleNamespace(__file__="/shell_scanner.py")
    monkeypatch.setattr(tool.importlib, "import_module", lambda _dotted: flattened)

    with pytest.raises(ValueError, match="source checkout"):
        tool.scanner_checkout()


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


def test_unclassified_ids_refuse_a_debt_snapshot_they_cannot_read(tmp_path: Path) -> None:
    # A bare KeyError from a restructured snapshot names neither the file that no longer holds what
    # a sweep reads nor the gate that maintains it, which is what `load_corpus` refuses to leave
    # behind for the replay inventory.
    root = tmp_path / "candidate"
    debt = root / tool.DEBT_PATH
    debt.parent.mkdir(parents=True)
    debt.write_text(json.dumps({"frozen": [{"origin_id": "scanner.budget.step-limit"}]}), "utf-8")

    with pytest.raises(ValueError, match="check_guard_inventory"):
        tool.unclassified_ids(root)


def test_unclassified_ids_refuse_a_snapshot_that_freezes_nothing(tmp_path: Path) -> None:
    # A sweep filters every origin it reaches against this set, so an empty one discards the whole
    # run and prints what a corpus that reached nothing prints. That is also the state the rollout
    # is driving toward, and the reach a run still has is asked for with --all-guards.
    root = tmp_path / "candidate"
    debt = root / tool.DEBT_PATH
    debt.parent.mkdir(parents=True)
    debt.write_text(json.dumps({"schema": 3, "records": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="all-guards"):
        tool.unclassified_ids(root)


def test_a_sweep_that_cannot_write_its_rows_reports_it_and_fails(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rows are written from a `finally` so a scanner failure does not cost the run every origin
    # it had reached. The write is fallible too: piping a several-minute run into `head` closes
    # stdout part way through it, and raised from there that failure replaces the one the sweep was
    # propagating and delivers no rows either.
    class Closed:
        def write(self, _text: str) -> int:
            raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(tool, "unclassified_ids", lambda _root: frozenset())
    monkeypatch.setattr(tool, "load_corpus", lambda _root, **_kwargs: ["echo one"])
    monkeypatch.setattr(sys, "stdout", Closed())

    assert tool.main(["--jobs", "1"]) == 1

    assert "could not write" in capsys.readouterr().err


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

    assert tool.main(["--jobs", "1"]) == 0

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

    monkeypatch.setattr(tool, "guard_owning_functions", record)

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

    assert "_eval_syntax_record_assignment" in _reported_qualnames(capsys.readouterr().out)


def test_trace_output_names_the_module_each_function_ran_in(
    capsys: pytest.CaptureFixture,
) -> None:
    # The reach is intersected on (module, qualified name) precisely because a bare name does not
    # identify a function. Printed without the module, the operator reads the answer the
    # intersection refused to give and aims the next candidate at another module's guard.
    assert tool.main(["--trace", "eval 'X=${Y=q}'; eval \"$X\"lattice"]) == 0

    assert "shell_taint.py:_eval_syntax_record_assignment" in capsys.readouterr().out.split()


def test_trace_output_keeps_one_name_two_guarded_modules_define(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `_is_function_positional_parameter` is defined in both guarded modules today, so a reach into
    # each is two guard-holding functions. Collapsed onto the name, one of them is dropped and the
    # count this mode is read for reports the other as the whole reach.
    both = frozenset({("shell_taint.py", "_shared"), ("shell_scanner.py", "_shared")})
    monkeypatch.setattr(tool, "trace_guard_functions", lambda _script: set(both))
    monkeypatch.setattr(tool, "guard_owning_functions", lambda _root: both)

    assert tool.main(["--trace", "echo hello"]) == 0

    captured = capsys.readouterr()
    assert captured.out.split() == ["shell_scanner.py:_shared", "shell_taint.py:_shared"]
    assert "reached 2 guard-holding functions" in captured.err


def test_a_trace_that_cannot_write_its_reach_reports_it_and_fails(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Piping a `--trace-all` run into `head` closes stdout, and the sweep answers that with a
    # diagnosis and a failing status. Unguarded, the same pipe answers a trace with a traceback out
    # of `main` and a second ignored exception at interpreter shutdown.
    class Closed:
        def write(self, _text: str) -> int:
            raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(sys, "stdout", Closed())

    assert tool.main(["--trace", "eval 'X=${Y=q}'; eval \"$X\"lattice"]) == 1

    assert "could not write" in capsys.readouterr().err


def test_a_sweep_that_cannot_flush_its_rows_reports_it_and_fails(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A write smaller than the stream's buffer, which is every sweep that finds a handful of rows,
    # reaches no syscall at all. Taken as delivery, the run reports the rows written and exits
    # zero, and the failure surfaces at interpreter shutdown as an ignored exception on the final
    # flush, long after the rows it names were lost.
    class Buffered:
        def write(self, _text: str) -> int:
            return 0

        def flush(self) -> None:
            raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(tool, "unclassified_ids", lambda _root: frozenset())
    monkeypatch.setattr(tool, "load_corpus", lambda _root, **_kwargs: ["echo one"])
    monkeypatch.setattr(sys, "stdout", Buffered())

    assert tool.main(["--jobs", "1"]) == 1

    assert "could not write" in capsys.readouterr().err


def test_a_sweep_that_cannot_render_its_rows_reports_it_and_fails(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `render_rows` refuses a caps value no line can carry, and it is called from the block that
    # exists to rescue the rows. Raised from there it delivers nothing and reports nothing.
    def refuse(_found: object) -> str:
        raise ValueError("no line can carry that caps value")

    monkeypatch.setattr(tool, "unclassified_ids", lambda _root: frozenset())
    monkeypatch.setattr(tool, "load_corpus", lambda _root, **_kwargs: ["echo one"])
    monkeypatch.setattr(tool, "render_rows", refuse)

    assert tool.main(["--jobs", "1"]) == 1

    assert "could not render" in capsys.readouterr().err


def test_a_sweep_that_cannot_render_its_rows_keeps_the_failure_it_was_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rescue block exists so an unexpected scanner failure does not cost the run its rows. A
    # rendering failure raised from inside it replaces that failure with one about the rendering,
    # so the run reports neither the rows nor the diagnosis.
    scanned = shell_scanner.scan_doc_lattice_invocations

    def fail_once_a_row_has_been_found(script: str, *, limits: ScanLimits) -> object:
        if limits.scanner.max_scan_steps == 0:
            raise ValueError("scanner failed on a shape nothing pinned")
        return scanned(script, limits=limits)

    def refuse(_found: object) -> str:
        raise ValueError("no line can carry that caps value")

    monkeypatch.setattr(tool, "scan_doc_lattice_invocations", fail_once_a_row_has_been_found)
    monkeypatch.setattr(tool, "unclassified_ids", lambda _root: None)
    monkeypatch.setattr(tool, "load_corpus", lambda _root, **_kwargs: ["echo one; echo two"])
    monkeypatch.setattr(tool, "render_rows", refuse)

    with pytest.raises(ValueError, match="nothing pinned"):
        tool.main(["--shrink", "0", "--jobs", "1"])


def test_a_sweep_reports_a_debt_snapshot_it_cannot_read_as_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty snapshot is the end state AD-20 drives toward, and the refusal already names the
    # file and says the remaining reach is asked for with --all-guards. Left to propagate, that
    # advice arrives under a traceback that reads as a broken tool, out of the documented command.
    def refuse(_root: Path) -> frozenset[str]:
        raise ValueError("holds no records carrying an origin_id for a sweep to look for")

    monkeypatch.setattr(tool, "unclassified_ids", refuse)

    with pytest.raises(SystemExit) as raised:
        tool.main([])

    assert raised.value.code == 2


def test_a_run_reports_a_checkout_it_cannot_locate_as_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The refusal already names what went wrong and tells the operator to run against a source
    # checkout with the package installed editable. Left to propagate, that advice arrives under a
    # traceback out of the documented command, which reads as a broken tool rather than a usage
    # error, and it reaches every mode before any of them has done any work.
    installed = SimpleNamespace(
        __file__="/venv/lib/python3.13/site-packages/doc_lattice/github_ci/shell_scanner.py"
    )
    monkeypatch.setattr(tool.importlib, "import_module", lambda _dotted: installed)

    with pytest.raises(SystemExit) as raised:
        tool.main(["--trace", "echo hello"])

    assert raised.value.code == 2


def test_a_sweep_reports_an_extra_file_it_cannot_read_as_a_usage_error(tmp_path: Path) -> None:
    # The candidates file is the one input on this path the operator wrote themselves, and the one
    # a traceback names worst.
    missing = tmp_path / "candidates.json"

    with pytest.raises(SystemExit) as raised:
        tool.main(["--seeds", "0", "--iterations", "0", "--extra", str(missing)])

    assert raised.value.code == 2
