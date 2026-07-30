#!/usr/bin/env python3
"""Search for authored inputs that reach a still-unclassified fail-closed guard origin.

AD-20 requires every guard origin in the CI shell scanner to carry executable evidence: either a
`ReachableWitness` naming an input that reaches it through the public scan path, or an
`InvariantWitness` explaining why authored input cannot. This tool finds the first kind.

It works in two modes, and the second is what unsticks the first.

**Sweep.** Drive `scan_doc_lattice_invocations` over a corpus, once per shrunk cap, and record
which guard origin each combination reports. Shrinking one cap at a time keeps the attribution
unambiguous: the reported origin is the guard that cap governs. The corpus is the recorded replay
inventory plus bodies from the fuzzer's compositional grammar, which between them cover the
shapes the scanner was built against.

**Trace.** For one candidate script, report which guard *functions* it executes at all. A sweep
that finds nothing says only that the corpus never reached the machinery; the trace says how far
it got, so the next candidate can be aimed one level deeper. Locating
`eval 'X=${Y=q}'` as the shape that reaches the eval-syntax assignment and decision recorders,
where a plain `eval 'X=${Y}'` does not, came from this mode rather than from any sweep.

Neither mode classifies anything on its own. A row it prints is a candidate: paste it into
`tests/guard_witnesses.py`, where the suite then holds it to returning that exact identifier.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
from pathlib import Path
from typing import TYPE_CHECKING

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

import fuzz_shell_taint  # noqa: E402

from doc_lattice.github_ci.shell_guards import (  # noqa: E402
    GuardRefusal,
    ScanLimits,
    ScannerLimits,
    TaintLimits,
)
from doc_lattice.github_ci.shell_scanner import scan_doc_lattice_invocations  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

REPLAY_INVENTORY = "tests/fixtures/github_ci_checkpoint/replay_inventory.json"
DEBT_PATH = "tests/fixtures/shell_guard_debt.json"
GUARDED_MODULES = (
    "src/doc_lattice/github_ci/shell_taint.py",
    "src/doc_lattice/github_ci/shell_scanner.py",
)
PRODUCTION = "production"

Reach = dict[str, tuple[str, str]]
"""Guard origin identifier -> (limits label, the shortest script that reached it)."""


def limits_field_count() -> int:
    """Return how many distinct caps a single-cap sweep shrinks.

    Returns:
        The combined field count of both limits values.
    """
    return len(dataclasses.fields(TaintLimits)) + len(dataclasses.fields(ScannerLimits))


def limits_grid(values: Sequence[int] = (0, 1, 2, 3)) -> list[tuple[str, ScanLimits]]:
    """Return one configuration per cap per shrink value, plus the unshrunk one.

    Only one cap is shrunk at a time, so whichever guard refuses is the guard that cap governs.

    Args:
        values: Shrink values to try for each cap.

    Returns:
        Labelled scan-limits configurations.
    """
    grid: list[tuple[str, ScanLimits]] = [(PRODUCTION, ScanLimits())]
    for field in dataclasses.fields(TaintLimits):
        for value in values:
            grid.append(
                (
                    f"TaintLimits({field.name}={value})",
                    ScanLimits(taint=TaintLimits(**{field.name: value})),
                )
            )
    for field in dataclasses.fields(ScannerLimits):
        for value in values:
            grid.append(
                (
                    f"ScannerLimits({field.name}={value})",
                    ScanLimits(scanner=ScannerLimits(**{field.name: value})),
                )
            )
    return grid


def unclassified_ids(root: Path) -> frozenset[str]:
    """Return the guard identifiers still frozen as rollout debt.

    Args:
        root: Repository root holding the debt snapshot.

    Returns:
        Identifiers with no witness yet.
    """
    payload = json.loads((root / DEBT_PATH).read_text(encoding="utf-8"))
    return frozenset(record["origin_id"] for record in payload["records"])


def load_corpus(
    root: Path,
    *,
    seeds: int = 4,
    iterations: int = 400,
    max_length: int = 600,
    extra: Path | None = None,
) -> list[str]:
    """Return the scripts to sweep, shortest first.

    Args:
        root: Repository root holding the replay inventory.
        seeds: How many fuzzer seeds to generate bodies from.
        iterations: Bodies to request per seed.
        max_length: Drop scripts longer than this; a witness has to stay reviewable.
        extra: Optional JSON file holding a list of hand-authored candidates.

    Returns:
        Deduplicated scripts ordered by length.
    """
    corpus: set[str] = set()
    inventory = json.loads((root / REPLAY_INVENTORY).read_text(encoding="utf-8"))
    corpus.update(entry["source"] for entry in inventory["entries"])
    for seed in range(seeds):
        for case in fuzz_shell_taint.generate(random.Random(seed), iterations):
            corpus.add(case.script)
    if extra is not None:
        corpus.update(json.loads(extra.read_text(encoding="utf-8")))
    return sorted((script for script in corpus if len(script) <= max_length), key=len)


def sweep(
    corpus: Iterable[str],
    grid: Sequence[tuple[str, ScanLimits]],
    *,
    wanted: frozenset[str] | None = None,
) -> Reach:
    """Return the shortest script that reaches each guard origin the corpus can reach.

    Args:
        corpus: Scripts to drive through the public scan path.
        grid: Labelled scan-limits configurations to try.
        wanted: Restrict the result to these identifiers, or None for every guard reached.

    Returns:
        Guard identifier mapped to the labelled configuration and script that reached it.
    """
    scripts = list(corpus)
    found: Reach = {}
    for label, limits in grid:
        for script in scripts:
            try:
                result = scan_doc_lattice_invocations(script, limits=limits)
            except RecursionError:
                continue
            verdict = result.verdict
            if not isinstance(verdict, GuardRefusal):
                continue
            if wanted is not None and verdict.origin_id not in wanted:
                continue
            previous = found.get(verdict.origin_id)
            # Prefer the shortest script, then the least-shrunk configuration, so a witness stays
            # readable and says as little about the caps as it can get away with.
            if previous is None or (len(script), len(label)) < (
                len(previous[1]),
                len(previous[0]),
            ):
                found[verdict.origin_id] = (label, script)
    return found


def trace_guard_functions(script: str, limits: ScanLimits | None = None) -> set[str]:
    """Return the guarded-module functions one script executes.

    Args:
        script: Candidate Bash source.
        limits: Optional shrunk caps.

    Returns:
        Qualified names, with nested-function `<locals>` markers removed.
    """
    guarded = {str(_ROOT / module) for module in GUARDED_MODULES}
    reached: set[str] = set()

    def trace(frame, event, _argument):  # noqa: ANN001, ANN202 - CPython tracer signature
        if event == "call" and frame.f_code.co_filename in guarded:
            reached.add(frame.f_code.co_qualname.replace(".<locals>", ""))

    # Restore whatever hook was active, not None: coverage collection and debuggers also use
    # settrace, and clearing it would silently disable them for the rest of the process.
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        scan_doc_lattice_invocations(script, limits=limits)
    finally:
        sys.settrace(previous)
    return reached


def render_rows(found: Reach) -> str:
    """Return paste-ready `ReachableWitness` rows for a sweep result.

    Args:
        found: Guard identifier mapped to its labelled configuration and script.

    Returns:
        Registry source text. Every row still has to earn its place: the suite holds it to
        returning that exact identifier through the public scan path.
    """
    lines: list[str] = []
    for origin_id in sorted(found):
        label, script = found[origin_id]
        # json.dumps escapes exactly what a double-quoted Python literal needs escaped, so the
        # row stays valid for a script that itself contains quotes.
        lines.append("    ReachableWitness(")
        lines.append(f"        {json.dumps(origin_id)},")
        lines.append(f"        {json.dumps(script)},")
        if label != PRODUCTION:
            kind = label.split("(", 1)[0]
            field = "taint" if kind == "TaintLimits" else "scanner"
            lines.append(f"        limits=ScanLimits({field}={label}),")
        lines.append("    ),")
    return "\n".join(lines) + "\n" if lines else ""


def main(argv: list[str] | None = None) -> int:
    """Search for witnesses, or trace one candidate script.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_ROOT)
    parser.add_argument("--trace", metavar="SCRIPT", help="report the guards one script reaches")
    parser.add_argument("--seeds", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=400)
    parser.add_argument("--max-length", type=int, default=600)
    parser.add_argument("--extra", type=Path, help="JSON list of hand-authored candidates")
    parser.add_argument("--shrink", type=int, nargs="*", default=[0, 1, 2, 3])
    parser.add_argument(
        "--all-guards",
        action="store_true",
        help="report every guard reached, not only the still-unclassified ones",
    )
    arguments = parser.parse_args(argv)

    if arguments.trace is not None:
        for name in sorted(trace_guard_functions(arguments.trace)):
            sys.stdout.write(f"{name}\n")
        return 0

    wanted = None if arguments.all_guards else unclassified_ids(arguments.root)
    corpus = load_corpus(
        arguments.root,
        seeds=arguments.seeds,
        iterations=arguments.iterations,
        max_length=arguments.max_length,
        extra=arguments.extra,
    )
    grid = limits_grid(tuple(arguments.shrink))
    sys.stderr.write(f"sweeping {len(corpus)} scripts over {len(grid)} configurations\n")
    found = sweep(corpus, grid, wanted=wanted)
    sys.stderr.write(f"reached {len(found)} guard origins\n")
    sys.stdout.write(render_rows(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
