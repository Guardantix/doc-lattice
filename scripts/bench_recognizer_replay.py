"""Fleetyard wall-clock benchmark for the issue #100 recognizer candidate (gate 9).

Implements the checkpoint benchmark protocol: interleaved candidate and current-scanner
runs, each timing one full replay-inventory scan through the harness entry point, with 3
discarded warm-up rounds, 30 measured repetitions, the median statistic, and a 250 ms
ceiling per version median. Trusted fleetyard-only decision gate; never CI-enforced and
never run on a self-hosted runner.
"""

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

from doc_lattice.github_ci.direct_marker_scanner import scan_execution_source
from doc_lattice.github_ci.shell_scanner import scan_doc_lattice_invocations

INVENTORY = Path("tests/fixtures/github_ci_checkpoint/replay_inventory.json")
WARMUPS = 3
REPETITIONS = 30
CEILING_MS = 250.0


def _sources() -> list[str]:
    """Load every frozen replay-inventory source."""
    entries = json.loads(INVENTORY.read_text())["entries"]
    return [entry["source"] for entry in entries]


def _timed_pass(runner: Callable[[str], object], sources: list[str]) -> float:
    """Run one full inventory scan and return elapsed milliseconds."""
    start = time.perf_counter()
    for source in sources:
        runner(source)
    return (time.perf_counter() - start) * 1000.0


def main() -> int:
    """Run the pinned protocol and report per-version medians and the baseline ratio."""
    parser = argparse.ArgumentParser(description="issue #100 recognizer benchmark")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()
    sources = _sources()
    candidate_ms: list[float] = []
    baseline_ms: list[float] = []
    for _ in range(WARMUPS):
        _timed_pass(scan_execution_source, sources)
        _timed_pass(scan_doc_lattice_invocations, sources)
    for _ in range(REPETITIONS):
        candidate_ms.append(_timed_pass(scan_execution_source, sources))
        baseline_ms.append(_timed_pass(scan_doc_lattice_invocations, sources))
    candidate_median = statistics.median(candidate_ms)
    baseline_median = statistics.median(baseline_ms)
    result = {
        "python": sys.version.split()[0],
        "inventory_count": len(sources),
        "warmups": WARMUPS,
        "repetitions": REPETITIONS,
        "candidate_median_ms": round(candidate_median, 3),
        "baseline_median_ms": round(baseline_median, 3),
        "candidate_to_baseline_ratio": round(candidate_median / baseline_median, 3),
        "ceiling_ms": CEILING_MS,
        "within_ceiling": candidate_median <= CEILING_MS,
    }
    print(json.dumps(result, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["within_ceiling"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
