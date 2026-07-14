#!/usr/bin/env python3
"""Benchmark section derivation on a representative large Markdown document."""

import argparse
import statistics
import time
from dataclasses import dataclass

from doc_lattice.loader import derive_file_sections

_DEFAULT_HEADINGS = 10_000
_DEFAULT_RUNS = 7
_DEFAULT_WARMUPS = 2
_DEFAULT_MAX_REGRESSION_PERCENT = 20.0
_FENCE_INTERVAL = 37
_UNICODE_INTERVAL = 53
_EMPTY_INTERVAL = 211


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Measured section-derivation samples and corpus structure."""

    byte_count: int
    line_count: int
    heading_count: int
    samples_ms: tuple[float, ...]
    median_ms: float


def build_document(heading_count: int) -> str:
    """Build a deterministic Markdown corpus for section derivation.

    Args:
        heading_count: Number of addressable headings to generate.

    Returns:
        Markdown containing prose, markers, Unicode, empty headings, and fences.

    Raises:
        ValueError: If ``heading_count`` is negative.
    """
    if heading_count < 0:
        msg = "heading_count must be non-negative"
        raise ValueError(msg)
    blocks: list[str] = []
    for index in range(heading_count):
        if index % _FENCE_INTERVAL == 0:
            blocks.append(
                "```python\n"
                f"## Hidden fenced heading {index} {{#hidden-{index}}}\n"
                "print('not a section')\n"
                "```"
            )
        if index % _EMPTY_INTERVAL == 0:
            heading = "##"
        elif index % _UNICODE_INTERVAL == 0:
            heading = f"## Привет 你好 {index} {{#section-{index}}}"
        elif index % 3 == 0:
            heading = f"## Section {index} {{#section-{index}}}"
        else:
            heading = f"## Section {index}"
        blocks.append(f"{heading}\nordinary prose for section {index}\nsecond body line")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def regression_percent(baseline_ms: float, candidate_ms: float) -> float:
    """Return candidate median change relative to a positive baseline.

    Args:
        baseline_ms: Baseline median milliseconds.
        candidate_ms: Candidate median milliseconds.

    Returns:
        Percentage change, where a positive value is slower.

    Raises:
        ValueError: If ``baseline_ms`` is not positive.
    """
    if baseline_ms <= 0:
        msg = "baseline_ms must be positive"
        raise ValueError(msg)
    return (candidate_ms / baseline_ms - 1) * 100


def exceeds_regression(
    baseline_ms: float, candidate_ms: float, max_regression_percent: float
) -> bool:
    """Return whether a candidate exceeds the permitted regression threshold."""
    return regression_percent(baseline_ms, candidate_ms) > max_regression_percent


def benchmark(body: str, *, runs: int, warmups: int) -> BenchmarkResult:
    """Measure warmed section derivation over one Markdown document.

    Args:
        body: Markdown corpus to derive.
        runs: Positive measured run count.
        warmups: Non-negative unmeasured warmup count.

    Returns:
        Corpus structure and millisecond samples.

    Raises:
        ValueError: If run counts are outside their accepted ranges.
    """
    if runs < 1:
        msg = "runs must be at least 1"
        raise ValueError(msg)
    if warmups < 0:
        msg = "warmups must be non-negative"
        raise ValueError(msg)
    for _ in range(warmups):
        derive_file_sections(body)

    samples: list[float] = []
    heading_count = 0
    for _ in range(runs):
        start = time.perf_counter()
        derived = derive_file_sections(body)
        samples.append((time.perf_counter() - start) * 1000)
        heading_count = len(derived.sections)
    sample_tuple = tuple(samples)
    return BenchmarkResult(
        byte_count=len(body.encode()),
        line_count=len(body.split("\n")) - (1 if body.endswith("\n") else 0),
        heading_count=heading_count,
        samples_ms=sample_tuple,
        median_ms=statistics.median(sample_tuple),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headings", type=int, default=_DEFAULT_HEADINGS)
    parser.add_argument("--runs", type=int, default=_DEFAULT_RUNS)
    parser.add_argument("--warmups", type=int, default=_DEFAULT_WARMUPS)
    parser.add_argument("--baseline-ms", type=float)
    parser.add_argument(
        "--max-regression-percent", type=float, default=_DEFAULT_MAX_REGRESSION_PERCENT
    )
    return parser.parse_args()


def main() -> int:
    """Run the benchmark and enforce an optional baseline threshold."""
    args = _parse_args()
    result = benchmark(build_document(args.headings), runs=args.runs, warmups=args.warmups)
    print(f"bytes={result.byte_count}")
    print(f"lines={result.line_count}")
    print(f"headings={result.heading_count}")
    print("samples_ms=" + ",".join(f"{sample:.3f}" for sample in result.samples_ms))
    print(f"median_ms={result.median_ms:.3f}")
    if args.baseline_ms is None:
        return 0
    regression = regression_percent(args.baseline_ms, result.median_ms)
    print(f"baseline_ms={args.baseline_ms:.3f}")
    print(f"regression_percent={regression:.3f}")
    if exceeds_regression(args.baseline_ms, result.median_ms, args.max_regression_percent):
        print(f"maximum_regression_percent={args.max_regression_percent:.3f}: exceeded")
        return 1
    print(f"maximum_regression_percent={args.max_regression_percent:.3f}: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
