"""Tests for pure run-local cache state transitions."""

from doc_lattice import __version__
from doc_lattice.cache.schema import CacheFile, Entry, StatRecord
from doc_lattice.cache.state import RunState
from doc_lattice.constants import CACHE_VERSION, MAX_STAT_ROOTS

ROOT = "/abs/current-root"


def _entry(*roots: str) -> Entry:
    return Entry(
        file_sha256="a" * 64,
        stats={root: StatRecord(size=1, mtime_ns=1) for root in roots},
        node=None,
    )


def _snapshot(entries: dict[str, Entry], roots: list[str]) -> CacheFile:
    return CacheFile(
        version=CACHE_VERSION,
        tool_version=__version__,
        roots=roots,
        entries=entries,
    )


def test_begin_without_snapshot_starts_empty() -> None:
    state = RunState.begin(None, ROOT)

    assert state.entry("missing") is None
    assert state.complete() == _snapshot({}, [ROOT])


def test_begin_copies_entry_mapping_and_root_ledger() -> None:
    other_root = "/abs/other-root"
    snapshot = _snapshot({"a.md": _entry(ROOT)}, [ROOT, other_root])
    state = RunState.begin(snapshot, ROOT)
    replacement = _entry(ROOT)

    assert state.entry("a.md") is snapshot.entries["a.md"]
    state.replace("b.md", replacement)

    assert "b.md" not in snapshot.entries
    completed = state.complete()
    assert snapshot.roots == [ROOT, other_root]
    assert completed.roots == [other_root, ROOT]
    assert completed.entries["b.md"] is replacement


def test_claim_without_stat_preserves_stats_and_retains_entry() -> None:
    cached_entry = _entry(ROOT, "/abs/other-root")
    original_stats = dict(cached_entry.stats)
    state = RunState.begin(_snapshot({"a.md": cached_entry}, [ROOT]), ROOT)

    state.claim("a.md")
    completed = state.complete()

    assert completed.entries["a.md"].stats == original_stats


def test_claim_with_refreshed_stat_updates_current_root_only() -> None:
    other_root = "/abs/other-root"
    cached_entry = _entry(other_root)
    state = RunState.begin(_snapshot({"a.md": cached_entry}, [other_root]), ROOT)
    refreshed = StatRecord(size=7, mtime_ns=9)

    state.claim("a.md", refreshed)
    completed = state.complete()

    assert completed.entries["a.md"].stats == {
        other_root: StatRecord(size=1, mtime_ns=1),
        ROOT: refreshed,
    }


def test_replace_swaps_entry_and_retains_it() -> None:
    original = _entry(ROOT)
    replacement = _entry(ROOT, "/abs/other-root")
    state = RunState.begin(_snapshot({"a.md": original}, [ROOT]), ROOT)

    state.replace("a.md", replacement)
    completed = state.complete()

    assert completed.entries["a.md"] is replacement


def test_complete_moves_existing_current_root_to_ledger_tail() -> None:
    other_root = "/abs/other-root"
    state = RunState.begin(_snapshot({}, [ROOT, other_root]), ROOT)

    completed = state.complete()

    assert completed.roots == [other_root, ROOT]


def test_complete_without_discoveries_withdraws_current_root_and_drops_entry() -> None:
    state = RunState.begin(_snapshot({"old.md": _entry(ROOT)}, [ROOT]), ROOT)

    completed = state.complete()

    assert completed.entries == {}


def test_undiscovered_shared_entry_loses_only_current_root_claim() -> None:
    other_root = "/abs/other-root"
    state = RunState.begin(
        _snapshot({"shared.md": _entry(ROOT, other_root)}, [ROOT, other_root]), ROOT
    )

    completed = state.complete()

    assert completed.entries["shared.md"].stats == {other_root: StatRecord(size=1, mtime_ns=1)}


def test_complete_evicts_oldest_root_and_scrubs_entry_claim() -> None:
    old_roots = [f"/root/{index}" for index in range(MAX_STAT_ROOTS)]
    cached_entry = _entry(*old_roots, ROOT)
    state = RunState.begin(_snapshot({"x.md": cached_entry}, old_roots), ROOT)

    state.claim("x.md")
    completed = state.complete()

    assert len(completed.roots) == MAX_STAT_ROOTS
    assert completed.roots[-1] == ROOT
    assert old_roots[0] not in completed.roots
    assert old_roots[0] not in completed.entries["x.md"].stats


def test_complete_sets_current_cache_and_tool_versions() -> None:
    snapshot = CacheFile(version=0, tool_version="old", roots=[], entries={})
    state = RunState.begin(snapshot, ROOT)

    completed = state.complete()

    assert completed.version == CACHE_VERSION
    assert completed.tool_version == __version__
