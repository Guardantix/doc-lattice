#!/usr/bin/env python3
"""Dev-only benchmark for the opt-in load cache. Not shipped.

Generates a synthetic corpus at 1k and 5k docs and reports the median of 5 runs of
load_lattice wall time in four states: uncached, cold cache (including the write), warm
verify tier, and warm stat tier. Also reports the cache file size. Acceptance: warm verify
tier is at least 3x faster than uncached at 5k docs.
"""

import os
import statistics
import tempfile
import time
from pathlib import Path

from game_lattice.config import load_config
from game_lattice.orchestrate import load_lattice

_HEADINGS_PER_DOC = 6
_EDGES_PER_DOC = 3


def _write_corpus(root: Path, count: int) -> None:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        edges = "".join(
            f"  - ref: doc{(i - j) % count}\n"
            for j in range(1, _EDGES_PER_DOC + 1)
            if (i - j) % count != i
        )
        sections = "".join(
            f"## Section {s} {{#s{i}-{s}}}\nbody {s}\n\n" for s in range(_HEADINGS_PER_DOC)
        )
        derives = f"derives_from:\n{edges}" if edges else ""
        (docs / f"doc{i}.md").write_text(
            f"---\nid: doc{i}\nlayer: design\n{derives}---\n# Doc {i}\n\n{sections}",
            encoding="utf-8",
        )


def _config(root: Path, *, cache_key: str | None, trust_stat: bool) -> Path:
    lines: list[str] = []
    if cache_key is not None:
        lines.append(f"cache_key: {cache_key}")
    if trust_stat:
        lines.append("cache_trust_stat: true")
    (root / ".game-lattice.yml").write_text(
        "\n".join(lines) + "\n" if lines else "", encoding="utf-8"
    )
    return root


def _median_seconds(root: Path, runs: int = 5) -> float:
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        load_lattice(load_config(None, root))
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def _bench_size(count: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "proj"
        _write_corpus(root, count)

        os.environ["XDG_CACHE_HOME"] = str(base / "xdg")

        _config(root, cache_key=None, trust_stat=False)
        uncached = _median_seconds(root)

        _config(root, cache_key="bench", trust_stat=False)
        # Cold: remove any cache, single timed run including the write.
        cache_dir = base / "xdg" / "game-lattice" / "bench"
        if cache_dir.exists():
            for entry in cache_dir.iterdir():
                entry.unlink()
        start = time.perf_counter()
        load_lattice(load_config(None, root))
        cold = time.perf_counter() - start

        warm_verify = _median_seconds(root)

        _config(root, cache_key="bench", trust_stat=True)
        warm_stat = _median_seconds(root)

        cache_file = cache_dir / "load-cache.json"
        size_kb = cache_file.stat().st_size / 1024 if cache_file.exists() else 0.0
        speedup = uncached / warm_verify if warm_verify else float("inf")
        print(f"== {count} docs ==")
        print(f"  uncached       : {uncached * 1000:8.1f} ms")
        print(f"  cold (w/ write): {cold * 1000:8.1f} ms")
        print(f"  warm verify    : {warm_verify * 1000:8.1f} ms  ({speedup:.1f}x vs uncached)")
        print(f"  warm stat      : {warm_stat * 1000:8.1f} ms")
        print(f"  cache size     : {size_kb:8.1f} KB")


def main() -> None:
    """Run the benchmark at 1k and 5k docs and print the table."""
    for count in (1000, 5000):
        _bench_size(count)


if __name__ == "__main__":
    main()
