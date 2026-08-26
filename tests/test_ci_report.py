"""Behavior tests for the guarded reporting mechanics the two CI audit scripts share.

`scripts/_ci_report.py` owns the properties asserted here, and both scripts reserve exit 1 for a
finding, so every one of them is the difference between a clean run and a run that reports the
thing the script exists to detect. They were pinned twice before, once per script, and drifted
apart both times; this module is where they are pinned once.

Each script's own suite keeps the integration half -- which lines it renders and how its exit
ladder reads a failed report -- because that is the part this module deliberately knows nothing
about.
"""

import ast
import sys

import pytest
from failing_streams import _AsciiOnly, _FailsOnceThenWrites
from script_loader import load_script, script_path

_MODULE = load_script(script_path("_ci_report.py"))

REPORT_FAILED = _MODULE["REPORT_FAILED"]
REPORT_FAILURES = _MODULE["REPORT_FAILURES"]
emit = _MODULE["emit"]
guarded_write = _MODULE["guarded_write"]

_SUMMARY = "## Report\n"


def test_the_guarded_set_is_exactly_the_two_failures_a_report_has():
    # Spelled out rather than asserted through behavior, because the set is the whole contract and
    # both members were once absent from one of the two copies. `OSError` is the disk or the path.
    # `UnicodeEncodeError` is a `ValueError`, so a guard on `OSError` alone lets a console that
    # cannot encode upstream text escape as a traceback carrying the interpreter's exit 1 -- which
    # is the *finding* code in both callers. The expected pair leads because the name it is
    # compared against is a constant, which is the order the linter reads as the natural one.
    assert (OSError, UnicodeEncodeError) == REPORT_FAILURES


def test_a_write_that_takes_is_reported_as_taken(capsys):
    assert guarded_write(print, "hello") is True
    assert capsys.readouterr().out == "hello\n"


@pytest.mark.parametrize(
    "error",
    [
        OSError("No space left on device"),
        UnicodeEncodeError("ascii", "café", 3, 4, "ordinal not in range(128)"),
    ],
)
def test_a_guarded_failure_is_answered_in_the_return_value(error, capsys):
    def refuse() -> None:
        raise error

    assert guarded_write(refuse) is False
    assert REPORT_FAILED in capsys.readouterr().err


def test_an_unguarded_failure_still_escapes(capsys):
    # The guard is narrow on purpose. A `ValueError` that is not an encoding failure is a bug in
    # the caller's rendering, not an infrastructure failure, and swallowing it would report a
    # broken report as a written one.
    def refuse() -> None:
        raise ValueError("not an encoding failure")

    with pytest.raises(ValueError, match="not an encoding failure"):
        guarded_write(refuse)
    capsys.readouterr()


def test_a_self_report_that_cannot_be_written_is_suppressed(monkeypatch, capsys):
    # The diagnostic travels on the channel that just failed, so writing it may fail the same way.
    # Raising there would defeat the whole guard: the caller would get the traceback it was being
    # protected from, only one frame later.
    monkeypatch.setattr(sys, "stderr", _AsciiOnly(sys.stderr))

    def refuse() -> None:
        raise OSError("café is not encodable on this console")

    assert guarded_write(refuse) is False
    capsys.readouterr()


def test_emit_writes_the_summary_the_lines_and_the_file(tmp_path, monkeypatch, capsys):
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    assert emit(_SUMMARY, ["first", "second"]) is True

    captured = capsys.readouterr()
    assert captured.out == _SUMMARY
    assert captured.err == "first\nsecond\n"
    assert summary_path.read_text(encoding="utf-8") == _SUMMARY


def test_emit_skips_the_file_when_the_runner_names_none(monkeypatch, capsys):
    # Outside Actions there is no step summary to append to, and treating its absence as a failed
    # write would make every local invocation report as one.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert emit(_SUMMARY, []) is True
    assert capsys.readouterr().out == _SUMMARY


def test_emit_flushes_the_summary_before_the_lines_it_introduces(monkeypatch):
    # A piped stdout is block-buffered, so an unflushed summary lands in the workflow log after
    # the annotation lines it introduces. The order is presentation order and nothing else
    # enforces it.
    events = []

    class _Recorder:
        def write(self, text: str) -> int:
            events.append(("write", text))
            return len(text)

        def flush(self) -> None:
            events.append(("flush", None))

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "stdout", _Recorder())
    monkeypatch.setattr(sys, "stderr", _Recorder())

    emit(_SUMMARY, ["annotation"])

    assert events.index(("flush", None)) < events.index(("write", "annotation"))


def test_a_failed_summary_leaves_every_later_write_attempted(tmp_path, monkeypatch, capsys):
    # The summary is the first write, so a guard that gave up on the first `OSError` would cost
    # the annotation lines and the file too. Only the write that failed is allowed to be lost.
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setattr(sys, "stdout", _FailsOnceThenWrites(sys.stdout))

    assert emit(_SUMMARY, ["first", "second"]) is False

    captured = capsys.readouterr()
    assert "first" in captured.err
    assert "second" in captured.err
    assert summary_path.read_text(encoding="utf-8") == _SUMMARY


def test_a_failed_line_leaves_the_later_lines_and_the_file_attempted(tmp_path, monkeypatch, capsys):
    # The granularity is per write, not per channel: the second annotation line shares stderr with
    # the first, and grouping the two inside one guarded call would drop it. Two lines is the
    # smallest case that can tell the difference.
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setattr(sys, "stderr", _FailsOnceThenWrites(sys.stderr))

    assert emit(_SUMMARY, ["first", "second"]) is False

    assert "second" in capsys.readouterr().err
    assert summary_path.read_text(encoding="utf-8") == _SUMMARY


def test_emit_never_short_circuits_the_writes_it_has_left(monkeypatch, capsys):
    # `all` over a generator stops at the first False, which would silently restore the
    # skipped-later-write bug the two tests above forbid. The results are collected before they
    # are combined, and this is the assertion that says so about every write rather than about
    # the ones a stream double happens to reach.
    attempted = []

    class _RefusesEverything:
        def write(self, text: str) -> int:
            attempted.append(text)
            raise OSError("No space left on device")

        def flush(self) -> None:
            pass

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "stdout", _RefusesEverything())
    monkeypatch.setattr(sys, "stderr", _RefusesEverything())

    assert emit(_SUMMARY, ["first", "second", "third"]) is False

    # `print` writes its argument and its newline separately, and the first write raises, so
    # each attempt is recorded as the bare text. All four reporting writes are here even though
    # the first of them failed.
    assert attempted.count(_SUMMARY) == 1
    for line in ("first", "second", "third"):
        assert line in attempted
    capsys.readouterr()


def test_a_step_summary_path_that_cannot_be_opened_is_a_guarded_failure(
    tmp_path, monkeypatch, capsys
):
    # The file write is the one channel that is not a stream, and its failure arrives from `open`
    # rather than from a write. It has to reach the same guard.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "absent" / "step-summary.md"))

    assert emit(_SUMMARY, []) is False

    captured = capsys.readouterr()
    assert captured.out == _SUMMARY
    assert REPORT_FAILED in captured.err


def test_the_file_is_appended_as_utf_8_whatever_the_console_can_carry(
    tmp_path, monkeypatch, capsys
):
    # The three writes fail independently, so a console that cannot encode upstream text costs
    # the console alone. The step-summary file is opened as UTF-8 and still receives the report.
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setattr(sys, "stdout", _AsciiOnly(sys.stdout))
    monkeypatch.setattr(sys, "stderr", _AsciiOnly(sys.stderr))

    assert emit("## café\n", ["café"]) is False

    assert summary_path.read_text(encoding="utf-8") == "## café\n"
    # `UnicodeEncodeError` renders the offending character escaped, so the diagnostic is pure
    # ASCII and still reaches a stream that has just refused the summary.
    assert REPORT_FAILED in capsys.readouterr().err


def test_the_module_imports_nothing_the_no_project_workflow_cannot_reach():
    # `scripts/audit_action_runtimes.py` runs under `uv run --no-project`, where nothing outside
    # the standard library and the script's own directory is importable. A dependency added here
    # would break that workflow, and nothing else in the offline suite would notice: the rest of
    # the repository runs against a synced project where the import would resolve.
    tree = ast.parse(script_path("_ci_report.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.partition(".")[0])

    assert roots
    assert roots <= sys.stdlib_module_names
