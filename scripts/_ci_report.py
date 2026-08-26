"""Guarded reporting mechanics shared by the two CI audit scripts.

`audit_action_runtimes.py` and `check_action_pin_correspondence.py` both answer a question in
their exit code and both write what they found to the same three places: the job summary on
stdout, one annotation line per item on stderr, and an append to ``GITHUB_STEP_SUMMARY``. Both
therefore need the same guard, because both reserve exit 1 for a *finding* and the interpreter
hands a traceback that same code. An unguarded write that failed for a reason having nothing to
do with the audit -- a full runner disk, a console whose encoding cannot carry upstream text --
would report a clean run as the thing the script exists to detect. Holding one implementation
here is what keeps the guarded exception set and the per-write granularity from drifting apart
between the two, which they twice did while each script spelled its own copy.

What is deliberately *not* here is exit-code precedence. Each script's ``main`` reads its own
domain state and owns its own ladder, and each one's suite pins it; the two ladders agree today,
and a shared helper over two different state shapes would buy nothing for that.

`emit` takes rendered lines rather than either script's domain objects, so this module owns I/O
and neither script's vocabulary. The module is stdlib-only and imports nothing from
``doc_lattice``, because the auditing workflow runs its caller under ``uv run --no-project``; a
sibling module in ``scripts/`` is importable there, since running a script by path prepends the
script's own directory to ``sys.path``.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# The one failure that is about the report rather than about what was audited. Worded once, so
# the two audits read the same way in a log when either one's report is what failed.
REPORT_FAILED = "the report could not be written"
# How a reporting write fails for reasons that are not the audit's answer. `OSError` is the disk
# or the path. `UnicodeEncodeError` is a stream whose encoding cannot carry the text: both callers
# render upstream text -- workflow, branch, job and annotation text on one side, transport error
# detail on the other -- and a console under an ASCII encoding raises rather than writing it. It
# is a `ValueError`, so guarding `OSError` alone would let exactly the inversion this guard exists
# to prevent back in.
REPORT_FAILURES = (OSError, UnicodeEncodeError)


def _append(path: str, text: str) -> None:
    """Append text to a file, creating it when it does not exist."""
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(text)


def guarded_write(write: Callable[..., object], *args: object, **kwargs: object) -> bool:
    """Attempt one reporting write, and report whether it took.

    A write here can fail for the ordinary reasons any write can, and letting that escape would
    end the run on a traceback carrying the interpreter's exit 1 -- which is the *finding* code in
    both callers. So the write is guarded and its failure is answered in the exit code instead, by
    the caller.

    This is one write rather than the whole report because callers report different things. `emit`
    composes it per write so that a write that fails costs only itself, while a caller with no
    report to compose -- because the audit never returned one -- uses it directly for its single
    error line.

    Args:
        write: The write to attempt.
        *args: Positional arguments for ``write``.
        **kwargs: Keyword arguments for ``write``.

    Returns:
        True when the write took, False when it failed for one of `REPORT_FAILURES`.
    """
    try:
        write(*args, **kwargs)
    except REPORT_FAILURES as error:
        # Suppressed rather than raised: this is the report of a failed report, so the channel it
        # would travel on may be the one already known to be unreliable. The exit code carries it.
        with contextlib.suppress(*REPORT_FAILURES):
            print(f"::error::infrastructure failure: {REPORT_FAILED}: {error}", file=sys.stderr)
        return False
    return True


def emit(summary: str, log_lines: Sequence[str]) -> bool:
    """Write one report to every channel, and report whether all of them took it.

    Every write is guarded on its own -- the summary, each log line separately, and the file --
    and every one is attempted whatever the ones before it did. The granularity is per write and
    not per channel: one failing annotation line costs that line alone, not the lines after it and
    not the file. The results are collected before they are combined, which is what keeps a
    boolean accumulator from short-circuiting a later write away.

    The order is therefore presentation order, not failure containment: with each write guarded
    separately, nothing a later write does can reach an earlier one. The summary goes first and is
    flushed there, because a piped stdout is block-buffered and an unflushed summary would
    otherwise land in the workflow log after the annotation lines it introduces. The
    ``GITHUB_STEP_SUMMARY`` append stays last so the rendered report reaches a reader in one
    stable order, which is all its position buys now.

    Args:
        summary: The rendered job summary.
        log_lines: One rendered line per item worth annotating, written to stderr in order. Each
            caller renders its own, so this module owns neither's vocabulary.

    Returns:
        True when every write took, False when one of them failed.
    """
    attempts = [guarded_write(print, summary, end="", flush=True)]
    attempts.extend(guarded_write(print, line, file=sys.stderr) for line in log_lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        attempts.append(guarded_write(_append, summary_path, summary))
    return all(attempts)
