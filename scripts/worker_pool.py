#!/usr/bin/env python3
"""Start-method and worker-count policy the pooled contributor tools share.

`scripts/guard_witness_sweep.py` and `scripts/corpus_differential.py` both split a fixed body of
work across worker processes, and both had to answer the same three questions to do it: which start
method leaves a worker an interpreter that inherited nothing, how many workers a run nobody sized
starts, and what a `--jobs` value on a command line means. Answered once here rather than twice,
because two copies of an answer drift: the copies this module replaces already disagreed about a
non-numeric `--jobs`, one refusing it by name and the other leaving it to argparse's generic message
for `int`, and a later fix to the start method or the CPU count would have had to be found twice.

Nothing here imports a scanner or a corpus, so a worker re-importing this module to reach a helper
pays for the policy alone.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os

FRESH_START_METHODS = ("forkserver", "spawn")
"""Start methods that give a worker its own interpreter, cheapest first.

`fork` is not among them, though it is the cheapest and was the Linux default until 3.14. A forked
worker inherits the parent's memory, so what a pooled run reports would depend on the platform it
ran on: a scanner replaced in a tool's own namespace would be searched by the workers where fork is
the default and by none of the workers anywhere else, and a corpus scored under two process models
differs between runs for a reason that has nothing to do with the scanner. It is also the start
method CPython deprecates once a process has threads, which a pool has by the time it starts its
second worker. Both fresh methods re-import the tool in the worker instead, once per worker.
"""


def start_method() -> str:
    """Return the process start method a pooled run drives its workers under.

    Returns:
        The cheapest start method this platform offers that gives a worker a fresh interpreter.
    """
    available = multiprocessing.get_all_start_methods()
    return next(
        (method for method in FRESH_START_METHODS if method in available),
        FRESH_START_METHODS[-1],
    )


def default_jobs(units: int) -> int:
    """Return how many units of work a run takes at a time when nobody says.

    Args:
        units: How many units of work the run is split into.

    Returns:
        Worker count, at least one and never more than there is work for. Derived from the CPUs
        this process may actually run on rather than from the machine's count, so a run under a
        CPU affinity mask or a container quota does not oversubscribe what it was given.
    """
    return max(1, min(units, os.process_cpu_count() or 1))


def resolve_jobs(units: int, jobs: int | None) -> int:
    """Return the worker count a run of `units` units starts, whether or not one was asked for.

    The cap belongs here rather than at each caller because a pool sized past the work there is
    starts interpreters that pay for their own copy of the corpus and score nothing at all, and a
    run that announces how many processes it started has to name the count it went on to use. Every
    caller resolving it through here is what keeps those two numbers the same one.

    Args:
        units: How many units of work the run is split into.
        jobs: Workers asked for on the command line, or None for the default.

    Returns:
        Worker count, at least one and never more than there is work for.
    """
    return max(1, min(units, default_jobs(units) if jobs is None else jobs))


def positive_jobs(text: str) -> int:
    """Return `text` as a count of worker processes, refusing a non-positive one.

    Kept apart from the converters that size a corpus because zero means something here that it does
    not mean there: an empty corpus is a run over nothing, which each tool answers on its own terms,
    while zero workers is not a smaller run but one with nobody to score a script. Left to
    argparse's `int`, a pool refuses it several layers down, out of a traceback that names the pool
    rather than the option that sized it.

    Args:
        text: The value as spelled on the command line.

    Returns:
        The value as a worker count.

    Raises:
        ArgumentTypeError: If the value is not a whole number, or is below one, since a run is
            driven by at least one process.
    """
    try:
        value = int(text)
    except ValueError as error:
        message = f"{text!r} is not a whole number of worker processes"
        raise argparse.ArgumentTypeError(message) from error
    if value < 1:
        message = f"{text!r} is not a worker count; a run is driven by at least one process"
        raise argparse.ArgumentTypeError(message)
    return value
