# Cache Package Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `src/doc_lattice/cache.py` (one 407-line `LoadCache` class) into a phase-separated `doc_lattice/cache/` package with bit-identical runtime behavior and an unchanged v1 JSON cache format.

**Architecture:** Convert the module to a package in one mechanical commit, then peel off `schema.py` (pure models + codec), `state.py` (pure `RunState`), `lookup.py` (impure, non-mutating tier selection), and `store.py` (impure disk I/O) one module at a time, keeping `LoadCache` delegating and the suite green after every task. Finally rewire `orchestrate._load_cached` to the new lifecycle, delete `LoadCache`, and redistribute the tests to mirror the new modules. Binding spec: `docs/superpowers/specs/2026-07-13-cache-package-refactor-design.md`.

**Tech Stack:** Python 3.13+, uv, pydantic v2, pytest + hypothesis, ruff, ty.

## Global Constraints

- Work on the existing branch `refactor/loadcache` in this worktree; never commit to `main`.
- All commands run through uv: `uv run --group dev pytest`, `uv run --group dev ruff check src tests`, `uv run --group dev ruff format src tests`, `uv run --group dev ty check src`, `uv run --group dev python scripts/check_typing_boundaries.py src`.
- Run the full suite as `env -u FORCE_COLOR uv run --group dev pytest` (this shell exports `FORCE_COLOR=3`, which breaks rich human-output substring asserts; conftest scrubs it, but unsetting matches CI exactly).
- ruff line length 100. Module docstring on every module. Google-style docstrings on public functions. No em-dashes anywhere in drafted content (docstrings, comments, messages).
- No `typing.Any`, no `typing.cast` in any cache module (none of the new file stems is a boundary name, so `scripts/check_typing_boundaries.py` enforces this).
- All custom exceptions extend `ProjectError`; this refactor introduces NO new exception types and must not change any error message.
- Behavior contract (spec section 5): CLI stdout, stderr, exit codes, file mutations, and the cache file format are bit-identical to `main`. The cache file parity check in Task 1/Task 7 proves the format claim.
- Coverage gate is 80% (suite currently sits at 99.40%; do not regress below the current level).
- The pre-commit hook runs ruff (with `--fix`), ruff-format, ty, the typing-boundary check, the version-sync check, and detect-secrets. If a hook auto-fixes a file, re-stage and re-commit.
- No Claude attribution of any kind in commit messages.
- Constants come from `doc_lattice.constants`: `CACHE_VERSION`, `CACHE_FILE_NAME`, `MAX_STAT_ROOTS`. Never re-declare their values.

---

### Task 1: Convert cache.py to a package and capture the format-parity baseline

**Files:**
- Create: `src/doc_lattice/cache/__init__.py` (via `git mv` from `src/doc_lattice/cache.py`)
- Delete: `src/doc_lattice/cache.py` (same `git mv`)

**Interfaces:**
- Consumes: nothing new.
- Produces: the package `doc_lattice.cache` whose `__init__.py` is today's `cache.py` verbatim. Every existing import (`from doc_lattice.cache import LoadCache, CacheHit, ...`) keeps working. Also produces the parity baseline at `/tmp/dl-cache-parity/` used by Task 7.

- [ ] **Step 1: Capture the cache-file parity baseline while sources still equal main**

The branch has only docs commits so far, so `src/` is identical to `main`. Generate a cache file with the pre-refactor code; Task 7 regenerates it with the refactored code and diffs.

```bash
rm -rf /tmp/dl-cache-parity
mkdir -p /tmp/dl-cache-parity/proj/docs
printf -- '---\nid: a\n---\n# A {#a}\nbody a\n' > /tmp/dl-cache-parity/proj/docs/a.md
printf -- '---\nid: b\nderives_from:\n  - ref: a#a\n---\n# B\nbody b\n' > /tmp/dl-cache-parity/proj/docs/b.md
printf -- '# plain, not a lattice node\n' > /tmp/dl-cache-parity/proj/docs/plain.md
printf 'cache_key: parity\n' > /tmp/dl-cache-parity/proj/.doc-lattice.yml
env -u FORCE_COLOR XDG_CACHE_HOME=/tmp/dl-cache-parity/xdg-before \
  uv run doc-lattice check /tmp/dl-cache-parity/proj > /tmp/dl-cache-parity/check-before.txt; echo "exit=$?"
test -f /tmp/dl-cache-parity/xdg-before/doc-lattice/parity/load-cache.json && echo BASELINE-OK
```

Expected: an exit line (nonzero is fine if edges are unreconciled; the exit code just needs to match Task 7's rerun), then `BASELINE-OK`.

- [ ] **Step 2: Convert the module to a package**

```bash
mkdir src/doc_lattice/cache
git mv src/doc_lattice/cache.py src/doc_lattice/cache/__init__.py
```

Then fix the relative-import depth, the ONLY content edit in this task. The module now lives one package level deeper, so every intra-project relative import gains a dot. In `src/doc_lattice/cache/__init__.py` change exactly these four lines:

```python
from .. import __version__
from ..constants import CACHE_FILE_NAME, CACHE_VERSION, MAX_STAT_ROOTS
from ..discovery import read_doc_bytes_and_stat
from ..model import FileSections, NodeMeta, ParsedDoc, SectionRecord
```

(previously `from . import __version__`, `from .constants ...`, `from .discovery ...`, `from .model ...`; without this, `.constants` resolves to the nonexistent `doc_lattice.cache.constants`). The in-method lazy import in `_stat_tier` likewise becomes `from ..discovery import _unreadable  # noqa: PLC0415` (it is deleted entirely in Task 4).

- [ ] **Step 3: Run the full gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
```

Expected: all pass (723 tests green). Python treats `cache/__init__.py` exactly like `cache.py` for every import in the codebase.

- [ ] **Step 4: Commit**

```bash
git add -A src/doc_lattice
git commit -m "refactor: convert cache module to a package, imports depth-adjusted only"
```

---

### Task 2: Extract schema.py (models + codec), delegate LoadCache to the codec

**Files:**
- Create: `src/doc_lattice/cache/schema.py`
- Create: `tests/test_cache_schema.py`
- Modify: `src/doc_lattice/cache/__init__.py` (remove the models, import from `.schema`, delegate `_reconstruct`/`_refresh_stat`/`record_miss` bodies to the codec)
- Modify: `tests/test_cache.py` (move `_sample_cache_file` + `test_cache_file_round_trips_through_json` out)
- Modify: `tests/test_loader.py` (move
  `test_file_sections_survive_serialization_round_trip` out; keep `_markdown_body` because
  `test_derive_file_sections_matches_inline_derivation` and others still use it; check with grep
  first, and if nothing else uses it, move it too)

**Interfaces:**
- Consumes: `doc_lattice.model.FileSections`, `SectionRecord`, `NodeMeta`, `ParsedDoc`.
- Produces (used by Tasks 3-6):
  - `schema.StatRecord`, `schema.SectionRecordModel`, `schema.NodePayload`, `schema.Entry`, `schema.CacheFile` (the five pydantic models, moved verbatim)
  - `schema.stat_record(st: os.stat_result) -> StatRecord`
  - `schema.reconstruct_doc(entry: Entry, path: Path) -> ParsedDoc | None`
  - `schema.make_entry(data: bytes, meta: NodeMeta | None, body: str, sections: FileSections | None, st: os.stat_result, current_root: str) -> Entry`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cache_schema.py`:

```python
"""Tests for the cache persistence models and codec."""

import hashlib
import types
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from doc_lattice import __version__
from doc_lattice.cache.schema import (
    CacheFile,
    Entry,
    NodePayload,
    SectionRecordModel,
    StatRecord,
    make_entry,
    reconstruct_doc,
    stat_record,
)
from doc_lattice.constants import CACHE_VERSION
from doc_lattice.loader import derive_file_sections
from doc_lattice.model import FileSections, NodeMeta, ParsedDoc, SectionRecord

ROOT = "/abs/current-root"


def _fake_stat(size: int = 10, mtime_ns: int = 123):
    return types.SimpleNamespace(st_size=size, st_mtime_ns=mtime_ns)


def _sample_cache_file() -> CacheFile:
    return CacheFile(
        version=CACHE_VERSION,
        tool_version=__version__,
        roots=["/abs/root"],
        entries={
            "docs/a.md": Entry(
                file_sha256="a" * 64,
                stats={"/abs/root": StatRecord(size=10, mtime_ns=123)},
                node=NodePayload(
                    meta=NodeMeta.model_validate({"id": "a"}),
                    body="# A\n",
                    total_lines=1,
                    sections=[SectionRecordModel(anchor="a-top", start=1, end=1)],
                ),
            ),
            "docs/plain.md": Entry(
                file_sha256="b" * 64,
                stats={"/abs/root": StatRecord(size=3, mtime_ns=456)},
                node=None,
            ),
        },
    )


def test_cache_file_round_trips_through_json():
    original = _sample_cache_file()
    dumped = original.model_dump_json()
    reloaded = CacheFile.model_validate_json(dumped)
    assert reloaded == original
    # The nested NodeMeta reloads as a validated NodeMeta, not a raw dict.
    reloaded_node = reloaded.entries["docs/a.md"].node
    assert reloaded_node is not None
    assert isinstance(reloaded_node.meta, NodeMeta)


def test_stat_record_captures_size_and_mtime_ns():
    assert stat_record(_fake_stat(7, 99)) == StatRecord(size=7, mtime_ns=99)


def test_make_entry_hashes_bytes_and_resets_stats_to_current_root():
    data = b"---\nid: a\n---\n# A\n"
    sections = FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),))
    entry = make_entry(
        data, NodeMeta.model_validate({"id": "a"}), "# A\n", sections, _fake_stat(), ROOT
    )
    assert entry.file_sha256 == hashlib.sha256(data).hexdigest()
    assert set(entry.stats) == {ROOT}
    assert entry.stats[ROOT] == StatRecord(size=10, mtime_ns=123)
    assert entry.node is not None
    assert entry.node.meta.id == "a"
    assert entry.node.sections == [SectionRecordModel(anchor="a", start=1, end=1)]


def test_make_entry_non_node_stores_null_node():
    entry = make_entry(b"# plain\n", None, "# plain\n", None, _fake_stat(), ROOT)
    assert entry.node is None


def test_reconstruct_doc_rebuilds_parsed_doc():
    entry = _sample_cache_file().entries["docs/a.md"]
    doc = reconstruct_doc(entry, Path("/proj/docs/a.md"))
    assert isinstance(doc, ParsedDoc)
    assert doc.path == Path("/proj/docs/a.md")
    assert doc.meta.id == "a"
    assert doc.body == "# A\n"
    assert doc.sections == FileSections(total_lines=1, sections=(SectionRecord("a-top", 1, 1),))


def test_reconstruct_doc_non_node_returns_none():
    entry = _sample_cache_file().entries["docs/plain.md"]
    assert reconstruct_doc(entry, Path("/proj/docs/plain.md")) is None


@st.composite
def _markdown_body(draw) -> str:
    lines = draw(
        st.lists(
            st.one_of(
                st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40),
                st.builds(
                    lambda n, t: "#" * n + " " + t, st.integers(1, 6), st.text("abc ", max_size=10)
                ),
            ),
            max_size=25,
        )
    )
    return "\n".join(lines)


@settings(max_examples=200)
@given(_markdown_body())
def test_file_sections_survive_codec_round_trip(body: str):
    # derive -> make_entry -> reconstruct_doc must hand build_lattice the exact same sections
    # a fresh parse would, which is the codec's whole correctness obligation.
    original = derive_file_sections(body)
    entry = make_entry(
        body.encode("utf-8"),
        NodeMeta.model_validate({"id": "x"}),
        body,
        original,
        _fake_stat(),
        ROOT,
    )
    doc = reconstruct_doc(entry, Path("/proj/docs/x.md"))
    assert doc is not None
    assert doc.sections == original
    assert doc.body == body
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_schema.py -v
```

Expected: FAIL at import with `ModuleNotFoundError: No module named 'doc_lattice.cache.schema'`.

- [ ] **Step 3: Create schema.py**

Create `src/doc_lattice/cache/schema.py`. The five model classes move VERBATIM from `cache/__init__.py` (same field order, same docstrings, same `ConfigDict(extra="forbid")`); the three codec functions are new:

```python
"""Cache persistence models and the codec between cached payloads and domain objects.

The models define the version 1 single-file JSON schema; the codec converts between a cached
``Entry`` and the domain objects the loader consumes. Everything here is pure: no filesystem,
no environment, no stderr.
"""

import hashlib
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..model import FileSections, NodeMeta, ParsedDoc, SectionRecord


class StatRecord(BaseModel):
    """One checkout's stat hint for a file: byte size and nanosecond mtime."""

    model_config = ConfigDict(extra="forbid")

    size: int
    mtime_ns: int


class SectionRecordModel(BaseModel):
    """The serialized form of one anchored section span."""

    model_config = ConfigDict(extra="forbid")

    anchor: str
    start: int
    end: int


class NodePayload(BaseModel):
    """The cached derivation of a lattice node: validated meta, body, and section spans."""

    model_config = ConfigDict(extra="forbid")

    meta: NodeMeta
    body: str
    total_lines: int
    sections: list[SectionRecordModel]


class Entry(BaseModel):
    """One cached file: its content hash, per-root stat hints, and node payload (or null)."""

    model_config = ConfigDict(extra="forbid")

    file_sha256: str
    stats: dict[str, StatRecord]
    node: NodePayload | None


class CacheFile(BaseModel):
    """The whole cache document, version 1."""

    model_config = ConfigDict(extra="forbid")

    version: int
    tool_version: str
    roots: list[str]
    entries: dict[str, Entry]


def stat_record(st: os.stat_result) -> StatRecord:
    """Record a stat result as the (size, mtime_ns) pair the stat tier compares.

    Args:
        st: The stat to record, captured alongside the bytes it corresponds to.

    Returns:
        The StatRecord holding ``st_size`` and ``st_mtime_ns``.
    """
    return StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)


def reconstruct_doc(entry: Entry, path: Path) -> ParsedDoc | None:
    """Rebuild a ParsedDoc from a cached node payload, or None for a cached non-node.

    Args:
        entry: The cache entry whose payload to reconstruct.
        path: The absolute path of the file on disk, carried onto the ParsedDoc.

    Returns:
        The reconstructed ParsedDoc, or None when the entry caches a non-node verdict.
    """
    node = entry.node
    if node is None:
        return None
    sections = FileSections(
        total_lines=node.total_lines,
        sections=tuple(SectionRecord(r.anchor, r.start, r.end) for r in node.sections),
    )
    return ParsedDoc(path=path, meta=node.meta, body=node.body, sections=sections)


def make_entry(  # noqa: PLR0913
    data: bytes,
    meta: NodeMeta | None,
    body: str,
    sections: FileSections | None,
    st: os.stat_result,
    current_root: str,
) -> Entry:
    """Build a replacement entry from a fresh parse: new hash, stats reset to one root.

    Args:
        data: The raw file bytes hashed for ``file_sha256``.
        meta: The validated NodeMeta, or None for a discovered non-node file.
        body: The verbatim body (unused when ``meta`` is None).
        sections: The pre-derived sections (present when ``meta`` is not None).
        st: The stat captured alongside ``data`` (see ``read_doc_bytes_and_stat``), stored
            as the fresh stat hint for the current root.
        current_root: The project root whose claim the fresh stats map holds.

    Returns:
        The entry to store for the file, claiming only ``current_root``.
    """
    has_node_payload = meta is not None and sections is not None
    node: NodePayload | None = None
    if has_node_payload:
        node = NodePayload(
            meta=meta,
            body=body,
            total_lines=sections.total_lines,
            sections=[
                SectionRecordModel(anchor=r.anchor, start=r.start, end=r.end)
                for r in sections.sections
            ],
        )
    return Entry(
        file_sha256=hashlib.sha256(data).hexdigest(),
        stats={current_root: stat_record(st)},
        node=node,
    )
```

- [ ] **Step 4: Delegate LoadCache to the codec**

In `src/doc_lattice/cache/__init__.py`:

1. Delete the five model class definitions and the now-unused `BaseModel, ConfigDict` import (keep `ValidationError`; `_read` still uses it).
2. Add the import (one line, with the noqa):

```python
from .schema import (  # noqa: F401 (models re-exported for doc_lattice.cache importers)
    CacheFile,
    Entry,
    NodePayload,
    SectionRecordModel,
    StatRecord,
    make_entry,
    reconstruct_doc,
    stat_record,
)
```

All five models MUST stay importable from `doc_lattice.cache`: `tests/test_cache.py` still imports them until Task 6, and the facade contract keeps them permanently. The `noqa` is temporary; Task 6 replaces it with `__all__`.
3. Delete the `_reconstruct` staticmethod; replace its two call sites: in `lookup` use `return CacheHit(doc=reconstruct_doc(entry, path))` and in `_stat_tier` use `return CacheHit(doc=reconstruct_doc(entry, path))`.
4. Replace the `_refresh_stat` body with:

```python
    def _refresh_stat(self, entry: Entry, st: os.stat_result) -> None:
        """Insert or refresh the current root's stat hint on a verify-tier hit.

        ``st`` is the stat already captured alongside the bytes that produced the verify hit
        (see ``read_doc_bytes_and_stat``), so no separate stat call is needed or made here.
        """
        entry.stats[self._current_root] = stat_record(st)
```

5. Replace the `record_miss` body (keep the signature, its `# noqa: PLR0913`, and its docstring verbatim) with:

```python
        self._entries[rel_key] = make_entry(data, meta, body, sections, st, self._current_root)
```

6. Prune the `..model` import (Task 1 already depth-adjusted it): `ParsedDoc` and `SectionRecord` were only used by the deleted `_reconstruct`, while `record_miss`'s signature still needs `NodeMeta` and `FileSections`, so it becomes `from ..model import FileSections, NodeMeta`. Check every remaining reference before pruning; ruff will flag leftovers.

- [ ] **Step 5: Run the gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_schema.py tests/test_cache.py -v
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests && uv run --group dev ty check src
```

Expected: all pass.

- [ ] **Step 6: Move the two migrating tests**

1. In `tests/test_cache.py`: delete `_sample_cache_file` and `test_cache_file_round_trips_through_json` (now in `tests/test_cache_schema.py`). Several later tests in the file still use `_sample_cache_file`; re-check with `grep -n "_sample_cache_file" tests/test_cache.py`. The corruption tests (`test_open_wrong_version_is_empty`, `test_open_wrong_tool_version_is_empty`, `test_open_invalid_meta_is_empty`, `test_open_valid_file_loads_entries`) use it, so instead of deleting it, KEEP the helper in `tests/test_cache.py` for now (it migrates in Task 5) and delete only the round-trip test.
2. In `tests/test_loader.py`: delete `test_file_sections_survive_serialization_round_trip` (replaced by `test_file_sections_survive_codec_round_trip`). Run `grep -n "_markdown_body" tests/test_loader.py`; if the strategy is now unused, delete it too, otherwise leave it.

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_loader.py tests/test_cache.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc_lattice/cache tests/test_cache_schema.py tests/test_cache.py tests/test_loader.py
git commit -m "refactor: extract cache schema and codec into cache/schema.py"
```

---

### Task 3: Add state.py (RunState) with pure unit tests

**Files:**
- Create: `src/doc_lattice/cache/state.py`
- Create: `tests/test_cache_state.py`

**Interfaces:**
- Consumes: `schema.CacheFile`, `schema.Entry`, `schema.StatRecord`; `constants.CACHE_VERSION`, `constants.MAX_STAT_ROOTS`; `doc_lattice.__version__`.
- Produces (used by Task 6):
  - `RunState.begin(cache: CacheFile | None, current_root: str) -> RunState`
  - `RunState.entry(rel_key: str) -> Entry | None`
  - `RunState.claim(rel_key: str, refreshed_stat: StatRecord | None = None) -> None`
  - `RunState.replace(rel_key: str, entry: Entry) -> None`
  - `RunState.complete() -> CacheFile`
- NOTE: `LoadCache.finalize` keeps its own copies of the four reclamation helpers until Task 6 deletes the class. This short-lived duplication inside one PR is deliberate; do not try to make `LoadCache` delegate to `RunState`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cache_state.py`:

```python
"""Tests for the run-local cache state (RunState)."""

from doc_lattice import __version__
from doc_lattice.cache.schema import CacheFile, Entry, StatRecord
from doc_lattice.cache.state import RunState
from doc_lattice.constants import CACHE_VERSION, MAX_STAT_ROOTS

ROOT = "/abs/current-root"


def _entry(*roots: str) -> Entry:
    return Entry(
        file_sha256="a" * 64,
        stats={r: StatRecord(size=1, mtime_ns=1) for r in roots},
        node=None,
    )


def _snapshot(entries: dict[str, Entry], roots: list[str]) -> CacheFile:
    return CacheFile(
        version=CACHE_VERSION, tool_version=__version__, roots=roots, entries=entries
    )


def test_begin_from_none_starts_empty():
    state = RunState.begin(None, ROOT)
    assert state.entry("docs/a.md") is None
    final = state.complete()
    assert final.entries == {}
    assert final.roots == [ROOT]


def test_begin_reads_snapshot_entries_without_mutating_it():
    snapshot = _snapshot({"docs/a.md": _entry(ROOT)}, [ROOT])
    state = RunState.begin(snapshot, ROOT)
    assert state.entry("docs/a.md") is snapshot.entries["docs/a.md"]
    state.replace("docs/b.md", _entry(ROOT))
    assert "docs/b.md" not in snapshot.entries  # begin copied the dict


def test_claim_without_stat_keeps_entry_unchanged_and_marks_discovered():
    entry = _entry(ROOT)
    state = RunState.begin(_snapshot({"docs/a.md": entry}, [ROOT]), ROOT)
    state.claim("docs/a.md")
    assert entry.stats == {ROOT: StatRecord(size=1, mtime_ns=1)}
    final = state.complete()
    assert "docs/a.md" in final.entries  # discovered, so the claim survives


def test_claim_with_refreshed_stat_inserts_the_current_root_record():
    entry = _entry("/abs/other")
    state = RunState.begin(_snapshot({"docs/a.md": entry}, ["/abs/other"]), ROOT)
    state.claim("docs/a.md", StatRecord(size=7, mtime_ns=9))
    assert entry.stats[ROOT] == StatRecord(size=7, mtime_ns=9)
    assert entry.stats["/abs/other"] == StatRecord(size=1, mtime_ns=1)


def test_replace_swaps_the_entry_and_marks_discovered():
    state = RunState.begin(_snapshot({"docs/a.md": _entry(ROOT)}, [ROOT]), ROOT)
    fresh = _entry(ROOT)
    state.replace("docs/a.md", fresh)
    assert state.entry("docs/a.md") is fresh
    assert "docs/a.md" in state.complete().entries


def test_complete_moves_current_root_to_ledger_tail():
    final = RunState.begin(_snapshot({}, [ROOT, "/abs/other"]), ROOT).complete()
    assert final.roots == ["/abs/other", ROOT]


def test_complete_withdraws_claims_on_undiscovered_paths_and_drops_unclaimed():
    state = RunState.begin(_snapshot({"docs/old.md": _entry(ROOT)}, [ROOT]), ROOT)
    final = state.complete()  # nothing discovered this run
    assert "docs/old.md" not in final.entries


def test_complete_keeps_undiscovered_entry_a_second_root_claims():
    other = "/abs/other"
    state = RunState.begin(
        _snapshot({"docs/shared.md": _entry(ROOT, other)}, [other, ROOT]), ROOT
    )
    final = state.complete()
    assert ROOT not in final.entries["docs/shared.md"].stats
    assert other in final.entries["docs/shared.md"].stats


def test_complete_evicts_over_cap_head_roots_and_scrubs_their_stats():
    old_roots = [f"/root/{i}" for i in range(MAX_STAT_ROOTS)]
    entry = _entry(*old_roots, ROOT)
    state = RunState.begin(_snapshot({"docs/x.md": entry}, old_roots), ROOT)
    state.claim("docs/x.md")
    final = state.complete()
    assert len(final.roots) == MAX_STAT_ROOTS
    assert final.roots[-1] == ROOT
    assert old_roots[0] not in final.roots
    assert old_roots[0] not in final.entries["docs/x.md"].stats


def test_complete_sets_version_and_tool_version():
    final = RunState.begin(None, ROOT).complete()
    assert final.version == CACHE_VERSION
    assert final.tool_version == __version__
```

- [ ] **Step 2: Run to verify failure**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_state.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'doc_lattice.cache.state'`.

- [ ] **Step 3: Create state.py**

```python
"""Run-local cache state: entries, root claims, the LRU roots ledger, and eviction.

Pure and filesystem-free. RunState knows nothing about paths, JSON, stderr, or hashing; it is
handed a loaded snapshot, absorbs each lookup outcome as an explicit fact, and produces the
final cache document for the store to persist.
"""

from .. import __version__
from ..constants import CACHE_VERSION, MAX_STAT_ROOTS
from .schema import CacheFile, Entry, StatRecord


class RunState:
    """The cache document being updated during one successful load attempt.

    Every lookup outcome is reported through ``claim`` (a hit) or ``replace`` (a miss), which
    also records the file as discovered this run. ``complete`` reclaims, bounds, and returns
    the final document.
    """

    def __init__(self, *, entries: dict[str, Entry], roots: list[str], current_root: str) -> None:
        self._entries = entries
        self._roots = roots
        self._current_root = current_root
        self._discovered: set[str] = set()

    @classmethod
    def begin(cls, cache: CacheFile | None, current_root: str) -> "RunState":
        """Start run state from a loaded snapshot, or empty when the load produced none.

        Args:
            cache: The validated cache document, or None for a missing or discarded cache.
            current_root: This run's project-root realpath, the per-root stat key.

        Returns:
            A RunState holding copies of the snapshot's entry map and roots ledger.
        """
        if cache is None:
            return cls(entries={}, roots=[], current_root=current_root)
        return cls(
            entries=dict(cache.entries), roots=list(cache.roots), current_root=current_root
        )

    def entry(self, rel_key: str) -> Entry | None:
        """Return the entry for a discovered file's key, or None when absent.

        Args:
            rel_key: The file's POSIX path relative to the project root (the entry key).

        Returns:
            The current entry, or None.
        """
        return self._entries.get(rel_key)

    def claim(self, rel_key: str, refreshed_stat: StatRecord | None = None) -> None:
        """Record a cache hit: mark the key discovered and apply a verify-tier stat refresh.

        Args:
            rel_key: The entry key of the file that hit.
            refreshed_stat: The current root's refreshed stat claim from a verify-tier hit,
                or None for a stat-tier hit whose stored hint already matches.
        """
        self._discovered.add(rel_key)
        if refreshed_stat is not None:
            self._entries[rel_key].stats[self._current_root] = refreshed_stat

    def replace(self, rel_key: str, entry: Entry) -> None:
        """Record a miss: mark the key discovered and replace its entry wholesale.

        Args:
            rel_key: The entry key of the file that missed.
            entry: The freshly built entry (see ``schema.make_entry``).
        """
        self._discovered.add(rel_key)
        self._entries[rel_key] = entry

    def complete(self) -> CacheFile:
        """Reclaim, bound, and return the final cache document after a successful load.

        Moves the current root to the ledger tail, withdraws its claim on files it did not
        discover this run, evicts over-cap head roots and scrubs their stats, and drops
        entries no live root claims.

        Returns:
            The final CacheFile for the store to persist.
        """
        self._touch_current_root()
        self._withdraw_undiscovered_claims()
        self._evict_over_cap_roots()
        self._drop_unclaimed_entries()
        return CacheFile(
            version=CACHE_VERSION,
            tool_version=__version__,
            roots=self._roots,
            entries=self._entries,
        )

    def _touch_current_root(self) -> None:
        """Move the current root to the ledger tail (most recently used)."""
        if self._current_root in self._roots:
            self._roots.remove(self._current_root)
        self._roots.append(self._current_root)

    def _withdraw_undiscovered_claims(self) -> None:
        """Remove the current root's stat key from every entry it did not discover this run."""
        for rel_key, entry in self._entries.items():
            if rel_key not in self._discovered:
                entry.stats.pop(self._current_root, None)

    def _evict_over_cap_roots(self) -> None:
        """Evict head roots beyond MAX_STAT_ROOTS and scrub their keys from every entry."""
        if len(self._roots) <= MAX_STAT_ROOTS:
            return
        evicted = set(self._roots[:-MAX_STAT_ROOTS])
        self._roots = self._roots[-MAX_STAT_ROOTS:]
        for entry in self._entries.values():
            for root in evicted:
                entry.stats.pop(root, None)

    def _drop_unclaimed_entries(self) -> None:
        """Drop any entry whose stats map is empty (no live root claims it)."""
        self._entries = {key: entry for key, entry in self._entries.items() if entry.stats}
```

- [ ] **Step 4: Run the gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_state.py -v
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests && uv run --group dev ty check src
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/cache/state.py tests/test_cache_state.py
git commit -m "refactor: add RunState, the pure run-local cache state"
```

---

### Task 4: Add lookup.py (fact-carrying tier selection); move CacheHit/CacheMiss

**Files:**
- Create: `src/doc_lattice/cache/lookup.py`
- Create: `tests/test_cache_lookup.py`
- Modify: `src/doc_lattice/cache/__init__.py` (delete the `CacheHit`/`CacheMiss` dataclass definitions; import them from `.lookup` instead)

**Interfaces:**
- Consumes: `schema.Entry`, `schema.StatRecord`, `schema.reconstruct_doc`, `schema.stat_record`; `discovery.read_doc_bytes_and_stat`, `discovery._unreadable`.
- Produces (used by Task 6):
  - `LookupPolicy(current_root: str, trust_stat: bool)` frozen dataclass. `trust_stat` is the ALREADY-EFFECTIVE value (config AND not require_verified); `resolve` applies no further gating.
  - `CacheHit(doc: ParsedDoc | None, refreshed_stat: StatRecord | None = None)` frozen dataclass. `refreshed_stat` is set only by a verify-tier hit.
  - `CacheMiss(data: bytes, stat: os.stat_result)` frozen dataclass (unchanged shape).
  - `resolve(entry: Entry | None, path: Path, policy: LookupPolicy) -> CacheHit | CacheMiss`
- NOTE: `LoadCache.lookup` keeps its own implementation until Task 6 (its TOCTOU tests monkeypatch `doc_lattice.cache.read_doc_bytes_and_stat`, the `__init__` module global). Do not make `LoadCache` delegate to `resolve`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cache_lookup.py`:

```python
"""Tests for per-file cache tier selection (lookup.resolve)."""

import hashlib
import types
from pathlib import Path

import pytest

import doc_lattice.cache.lookup as lookup_module
from doc_lattice.cache.lookup import CacheHit, CacheMiss, LookupPolicy, resolve
from doc_lattice.cache.schema import Entry, NodePayload, SectionRecordModel, StatRecord
from doc_lattice.error_types import UnreadableDocError
from doc_lattice.model import FileSections, NodeMeta, ParsedDoc, SectionRecord

ROOT = "/abs/current-root"
VERIFY = LookupPolicy(current_root=ROOT, trust_stat=False)
TRUSTING = LookupPolicy(current_root=ROOT, trust_stat=True)


def _node() -> NodePayload:
    return NodePayload(
        meta=NodeMeta.model_validate({"id": "a"}),
        body="# A {#a-top}\nbody\n",
        total_lines=2,
        sections=[SectionRecordModel(anchor="a-top", start=1, end=2)],
    )


def _entry_for(
    text: str, *, node: NodePayload | None, stats: dict[str, StatRecord] | None = None
) -> Entry:
    data = text.encode("utf-8")
    return Entry(
        file_sha256=hashlib.sha256(data).hexdigest(),
        stats=stats if stats is not None else {ROOT: StatRecord(size=len(data), mtime_ns=0)},
        node=node,
    )


def _write(tmp_path: Path, text: str) -> Path:
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text, encoding="utf-8")
    return doc


def test_absent_entry_is_a_miss(tmp_path: Path):
    doc = _write(tmp_path, "new\n")
    result = resolve(None, doc, VERIFY)
    assert isinstance(result, CacheMiss)
    assert result.data == b"new\n"


def test_verify_tier_hit_reconstructs_doc_and_carries_refreshed_stat(tmp_path: Path):
    text = "---\nid: a\n---\n# A {#a-top}\nbody\n"
    doc = _write(tmp_path, text)
    result = resolve(_entry_for(text, node=_node()), doc, VERIFY)
    assert isinstance(result, CacheHit)
    assert isinstance(result.doc, ParsedDoc)
    assert result.doc.meta.id == "a"
    assert result.doc.sections == FileSections(
        total_lines=2, sections=(SectionRecord("a-top", 1, 2),)
    )
    st = doc.stat()
    assert result.refreshed_stat == StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)


def test_verify_tier_non_node_hit_returns_none_doc(tmp_path: Path):
    doc = _write(tmp_path, "# plain\n")
    result = resolve(_entry_for("# plain\n", node=None), doc, VERIFY)
    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_content_change_is_a_miss_carrying_current_bytes(tmp_path: Path):
    doc = _write(tmp_path, "changed\n")
    result = resolve(_entry_for("original\n", node=None), doc, VERIFY)
    assert isinstance(result, CacheMiss)
    assert result.data == b"changed\n"


def test_stat_tier_hit_skips_reading_the_file_and_carries_no_stat(tmp_path: Path):
    doc = _write(tmp_path, "# A\n")
    st = doc.stat()
    entry = Entry(
        file_sha256="deadbeef" * 8,  # deliberately wrong; the stat tier must not hash
        stats={ROOT: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    result = resolve(entry, doc, TRUSTING)
    assert isinstance(result, CacheHit)
    assert result.doc is None
    assert result.refreshed_stat is None


def test_stat_tier_disabled_by_policy_falls_to_verify(tmp_path: Path):
    doc = _write(tmp_path, "# A\n")
    st = doc.stat()
    entry = Entry(
        file_sha256="deadbeef" * 8,  # wrong hash; the verify tier will miss
        stats={ROOT: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    assert isinstance(resolve(entry, doc, VERIFY), CacheMiss)


@pytest.mark.parametrize("delta", [("size", 1), ("mtime", 1)])
def test_stat_tier_mismatch_falls_through_to_verify_hit(tmp_path: Path, delta):
    text = "# A\n"
    doc = _write(tmp_path, text)
    st = doc.stat()
    size = st.st_size + (delta[1] if delta[0] == "size" else 0)
    mtime = st.st_mtime_ns + (delta[1] if delta[0] == "mtime" else 0)
    entry = _entry_for(text, node=None, stats={ROOT: StatRecord(size=size, mtime_ns=mtime)})
    result = resolve(entry, doc, TRUSTING)
    assert isinstance(result, CacheHit)  # correct hash: the verify tier rescues it
    assert result.refreshed_stat is not None  # and refreshes the drifted hint


def test_stat_tier_no_record_for_current_root_falls_through_to_verify_hit(tmp_path: Path):
    text = "# A\n"
    doc = _write(tmp_path, text)
    entry = _entry_for(
        text, node=None, stats={"/abs/other": StatRecord(size=1, mtime_ns=1)}
    )
    result = resolve(entry, doc, TRUSTING)
    assert isinstance(result, CacheHit)


def test_stat_tier_raced_stat_failure_raises_unreadable(tmp_path: Path):
    doc = tmp_path / "docs" / "gone.md"  # never created
    entry = Entry(
        file_sha256="a" * 64,
        stats={ROOT: StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    with pytest.raises(UnreadableDocError):
        resolve(entry, doc, TRUSTING)


def test_verify_hit_stat_comes_from_the_read_handle_not_a_fresh_stat(tmp_path, monkeypatch):
    # TOCTOU pin: the refreshed_stat fact must be built from the stat captured by the same
    # read that produced the hashed bytes, never from a separate path.stat().
    text = "---\nid: a\n---\n# A\n"
    doc = _write(tmp_path, text)
    real_st = doc.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_st.st_size + 1000, st_mtime_ns=real_st.st_mtime_ns + 999_999_999
    )
    monkeypatch.setattr(
        lookup_module, "read_doc_bytes_and_stat", lambda _p: (text.encode("utf-8"), sentinel)
    )
    result = resolve(_entry_for(text, node=_node()), doc, VERIFY)
    assert isinstance(result, CacheHit)
    assert result.refreshed_stat == StatRecord(
        size=sentinel.st_size, mtime_ns=sentinel.st_mtime_ns
    )


def test_miss_carries_the_stat_captured_with_the_read(tmp_path, monkeypatch):
    text = "---\nid: a\n---\n# A\n"
    doc = _write(tmp_path, text)
    real_st = doc.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_st.st_size + 2000, st_mtime_ns=real_st.st_mtime_ns + 888_888_888
    )
    monkeypatch.setattr(
        lookup_module, "read_doc_bytes_and_stat", lambda _p: (text.encode("utf-8"), sentinel)
    )
    result = resolve(None, doc, VERIFY)
    assert isinstance(result, CacheMiss)
    assert result.stat is sentinel
```

- [ ] **Step 2: Run to verify failure**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_lookup.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'doc_lattice.cache.lookup'`.

- [ ] **Step 3: Create lookup.py**

```python
"""Per-file tier selection: resolve a discovered doc to a hit or a miss, mutating nothing.

The result carries facts for RunState to apply explicitly: a verify-tier hit carries the
current root's refreshed stat claim, a stat-tier hit carries none (its stored hint already
matches), and a miss carries the raw bytes plus the same-handle stat. This module reads and
stats docs but never touches cache state.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from ..discovery import _unreadable, read_doc_bytes_and_stat
from ..model import ParsedDoc
from .schema import Entry, StatRecord, reconstruct_doc, stat_record


@dataclass(frozen=True, slots=True)
class LookupPolicy:
    """How lookups run for one load: the root whose stat hints apply, and the stat tier.

    ``trust_stat`` is the already-effective value (the config flag AND not require_verified);
    ``resolve`` applies no further gating.
    """

    current_root: str
    trust_stat: bool


@dataclass(frozen=True, slots=True)
class CacheHit:
    """A tier hit. ``doc`` is the reconstructed ParsedDoc, or None for a cached non-node.

    ``refreshed_stat`` is set only by a verify-tier hit: the current root's stat claim for
    RunState to apply, captured from the same open handle as the hashed bytes. A stat-tier hit
    carries None because its stored hint already matches.
    """

    doc: ParsedDoc | None
    refreshed_stat: StatRecord | None = None


@dataclass(frozen=True, slots=True)
class CacheMiss:
    """A miss. ``data`` is the raw bytes already read, for the caller to decode and parse.

    ``stat`` is the ``os.stat_result`` captured from the same open handle as ``data``, so a
    caller that later records the miss threads a stat hint that corresponds exactly to the
    hashed bytes rather than one taken from a separate, possibly racy, stat call.
    """

    data: bytes
    stat: os.stat_result


def resolve(entry: Entry | None, path: Path, policy: LookupPolicy) -> CacheHit | CacheMiss:
    """Resolve one discovered file through the stat and verify tiers.

    Args:
        entry: The file's current cache entry, or None when the cache holds none.
        path: The absolute path to the file on disk.
        policy: The effective lookup policy for this load.

    Returns:
        A CacheHit reusing the cached derivation, or a CacheMiss carrying the freshly read
        bytes when the entry is absent, drifted, or unverifiable.

    Raises:
        UnreadableDocError: If the file cannot be read or stat-ed, with the same message an
            uncached read would produce.
    """
    if entry is not None and policy.trust_stat:
        hit = _stat_tier(entry, path, policy.current_root)
        if hit is not None:
            return hit
    data, st = read_doc_bytes_and_stat(path)
    if entry is not None and entry.file_sha256 == hashlib.sha256(data).hexdigest():
        return CacheHit(doc=reconstruct_doc(entry, path), refreshed_stat=stat_record(st))
    return CacheMiss(data=data, stat=st)


def _stat_tier(entry: Entry, path: Path, current_root: str) -> CacheHit | None:
    """Return a CacheHit if the current root's stat hint matches, else None."""
    record = entry.stats.get(current_root)
    if record is None:
        return None
    try:
        st = path.stat()
    except OSError as exc:
        raise _unreadable(path, exc) from exc
    if record.size != st.st_size or record.mtime_ns != st.st_mtime_ns:
        return None
    return CacheHit(doc=reconstruct_doc(entry, path))
```

- [ ] **Step 4: Point `__init__.py` at the moved dataclasses**

In `src/doc_lattice/cache/__init__.py`: delete the `CacheHit` and `CacheMiss` dataclass definitions and add `from .lookup import CacheHit, CacheMiss` to the imports. `LoadCache.lookup` and `_stat_tier` keep constructing `CacheHit(doc=...)`; the new `refreshed_stat` field defaults to `None`, so nothing else changes. If `dataclass` is now unused in `__init__.py`, remove that import.

- [ ] **Step 5: Run the gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_lookup.py -v
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests && uv run --group dev ty check src
```

Expected: all pass, including the old `test_cache.py` tier tests still running against `LoadCache`.

- [ ] **Step 6: Commit**

```bash
git add src/doc_lattice/cache tests/test_cache_lookup.py
git commit -m "refactor: extract fact-carrying tier selection into cache/lookup.py"
```

---

### Task 5: Add store.py (location, load, atomic save); delegate LoadCache; migrate store tests

**Files:**
- Create: `src/doc_lattice/cache/store.py`
- Create: `tests/test_cache_store.py`
- Modify: `src/doc_lattice/cache/__init__.py` (move `cache_home`/`cache_path`/`_read`/`_write` out; `open` and `finalize` delegate)
- Modify: `tests/test_cache.py` (move the cache_home/cache_path tests and the `_open` corruption tests out)

**Interfaces:**
- Consumes: `schema.CacheFile`; `constants.CACHE_FILE_NAME`, `constants.CACHE_VERSION`; `doc_lattice.__version__`.
- Produces (used by Task 6):
  - `store.cache_home(env: Mapping[str, str]) -> Path` (moved verbatim)
  - `store.cache_path(cache_key: str, env: Mapping[str, str]) -> Path` (moved verbatim)
  - `StoreSnapshot(cache: CacheFile | None, baseline: dict[str, object] | None)` frozen dataclass
  - `store.load(path: Path) -> StoreSnapshot`
  - `store.save_if_changed(path: Path, final: CacheFile, baseline: dict[str, object] | None) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cache_store.py`. The `cache_home`/`cache_path` tests move verbatim from `tests/test_cache.py`; the load tests re-express the old `_open`/`is_empty` corruption tests as `store.load` assertions; the save tests port the two write-failure tests with the monkeypatch target moved to the store module:

```python
"""Tests for the cache store: location, load, validation, and atomic save."""

import json
from pathlib import Path

import pytest

import doc_lattice.cache.store as store_module
from doc_lattice import __version__
from doc_lattice.cache.schema import (
    CacheFile,
    Entry,
    NodePayload,
    SectionRecordModel,
    StatRecord,
)
from doc_lattice.cache.store import StoreSnapshot, cache_home, cache_path, load, save_if_changed
from doc_lattice.constants import CACHE_FILE_NAME, CACHE_VERSION
from doc_lattice.model import NodeMeta


def _sample_cache_file() -> CacheFile:
    return CacheFile(
        version=CACHE_VERSION,
        tool_version=__version__,
        roots=["/abs/root"],
        entries={
            "docs/a.md": Entry(
                file_sha256="a" * 64,
                stats={"/abs/root": StatRecord(size=10, mtime_ns=123)},
                node=NodePayload(
                    meta=NodeMeta.model_validate({"id": "a"}),
                    body="# A\n",
                    total_lines=1,
                    sections=[SectionRecordModel(anchor="a-top", start=1, end=1)],
                ),
            ),
        },
    )


def test_cache_home_uses_absolute_xdg():
    home = cache_home({"XDG_CACHE_HOME": "/custom/cache", "HOME": "/home/u"})
    assert home == Path("/custom/cache")


def test_cache_home_ignores_relative_xdg():
    home = cache_home({"XDG_CACHE_HOME": "relative/cache", "HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_home_falls_back_to_home_dot_cache_when_xdg_unset():
    home = cache_home({"HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_home_falls_back_to_home_cache_dir_when_home_and_xdg_unset():
    home = cache_home({})
    assert home == Path.home() / ".cache"


def test_cache_path_composes_slot_and_file_name():
    path = cache_path("my-docs", {"XDG_CACHE_HOME": "/c", "HOME": "/home/u"})
    assert path == Path("/c") / "doc-lattice" / "my-docs" / CACHE_FILE_NAME


def test_load_missing_file_is_empty(tmp_path: Path):
    assert load(tmp_path / "absent.json") == StoreSnapshot(cache=None, baseline=None)


def test_load_valid_file_returns_cache_and_matching_baseline(tmp_path: Path):
    path = tmp_path / CACHE_FILE_NAME
    original = _sample_cache_file()
    path.write_text(original.model_dump_json(), encoding="utf-8")
    snapshot = load(path)
    assert snapshot.cache == original
    assert snapshot.baseline == original.model_dump(mode="json")


@pytest.mark.parametrize(
    "text",
    [
        "",  # truncated / empty
        "{ not json",  # invalid JSON
        '{"version": 1}',  # schema violation (missing fields)
    ],
)
def test_load_corrupt_file_is_empty(tmp_path: Path, text: str):
    path = tmp_path / CACHE_FILE_NAME
    path.write_text(text, encoding="utf-8")
    assert load(path) == StoreSnapshot(cache=None, baseline=None)


def test_load_invalid_utf8_is_empty(tmp_path: Path):
    # Invalid UTF-8 raises UnicodeDecodeError (not an OSError); it must still yield an empty
    # snapshot rather than propagate.
    path = tmp_path / CACHE_FILE_NAME
    path.write_bytes(b"\xff\xfe not valid utf-8\n")
    assert load(path) == StoreSnapshot(cache=None, baseline=None)


def test_load_wrong_version_is_empty(tmp_path: Path):
    path = tmp_path / CACHE_FILE_NAME
    bad = _sample_cache_file().model_copy(update={"version": 999})
    path.write_text(bad.model_dump_json(), encoding="utf-8")
    assert load(path).cache is None


def test_load_wrong_tool_version_is_empty(tmp_path: Path):
    path = tmp_path / CACHE_FILE_NAME
    bad = _sample_cache_file().model_copy(update={"tool_version": "0.0.0-other"})
    path.write_text(bad.model_dump_json(), encoding="utf-8")
    assert load(path).cache is None


def test_load_invalid_meta_is_empty(tmp_path: Path):
    # A structurally valid file whose node.meta violates NodeMeta must discard wholesale.
    path = tmp_path / CACHE_FILE_NAME
    payload = _sample_cache_file().model_dump(mode="json")
    payload["entries"]["docs/a.md"]["node"]["meta"]["id"] = "bad#id"  # '#' rejected by NodeMeta
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load(path).cache is None


def test_save_if_changed_skips_an_unchanged_document(tmp_path: Path):
    final = _sample_cache_file()
    path = tmp_path / "slot" / CACHE_FILE_NAME
    save_if_changed(path, final, final.model_dump(mode="json"))
    assert not path.exists()  # identical baseline: no write, no directory creation


def test_save_if_changed_writes_when_baseline_is_none(tmp_path: Path):
    final = _sample_cache_file()
    path = tmp_path / "slot" / CACHE_FILE_NAME
    save_if_changed(path, final, None)
    assert CacheFile.model_validate_json(path.read_text(encoding="utf-8")) == final


def test_save_if_changed_writes_when_document_changed(tmp_path: Path):
    final = _sample_cache_file()
    path = tmp_path / "slot" / CACHE_FILE_NAME
    save_if_changed(path, final, {"version": CACHE_VERSION})  # any differing baseline
    assert path.exists()


def test_save_write_failure_emits_one_stderr_line_and_does_not_raise(
    tmp_path, capsys, monkeypatch
):
    def _boom(*args, **kwargs):  # noqa: ARG001
        raise OSError("disk full")

    monkeypatch.setattr(store_module.os, "replace", _boom)
    path = tmp_path / "slot" / CACHE_FILE_NAME
    save_if_changed(path, _sample_cache_file(), None)  # must not raise
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1
    assert "cache" in captured.err.lower()
    assert list(path.parent.glob("*.tmp")) == []  # no partial file left behind


def test_save_write_failure_does_not_raise_when_temp_cleanup_also_fails(
    tmp_path, capsys, monkeypatch
):
    # Both the atomic replace and the finally-block cleanup fail. The cleanup OSError must not
    # escape the cache error handler and change the command's exit code.
    def _boom_replace(*args, **kwargs):  # noqa: ARG001
        raise OSError("disk full")

    def _boom_unlink(*args, **kwargs):  # noqa: ARG001
        raise OSError("permission denied")

    monkeypatch.setattr(store_module.os, "replace", _boom_replace)
    monkeypatch.setattr(store_module.Path, "unlink", _boom_unlink)
    path = tmp_path / "slot" / CACHE_FILE_NAME
    save_if_changed(path, _sample_cache_file(), None)  # must not raise
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1
    assert "cache" in captured.err.lower()
```

- [ ] **Step 2: Run to verify failure**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_store.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'doc_lattice.cache.store'`.

- [ ] **Step 3: Create store.py**

`cache_home`, `cache_path`, the `_read` logic, and the `_write` logic move verbatim from `cache/__init__.py` (including the `# noqa: PTH105` comment); `StoreSnapshot`, `load`, and `save_if_changed` wrap them:

```python
"""Cache file location, load, and atomic save: the only module that touches the cache file.

A read failure of any kind yields an empty snapshot and everything recomputes; a write failure
emits one stderr diagnostic and is swallowed, so a broken cache never changes a command's
result or exit code.
"""

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .. import __version__
from ..constants import CACHE_FILE_NAME, CACHE_VERSION
from .schema import CacheFile


def cache_home(env: Mapping[str, str]) -> Path:
    """Return the base cache directory per the XDG base directory spec.

    Args:
        env: The environment mapping to read ``XDG_CACHE_HOME`` and ``HOME`` from.

    Returns:
        ``$XDG_CACHE_HOME`` when it is set to an absolute path (a relative value is ignored
        per the spec), otherwise ``$HOME/.cache``, falling back to the user home directory.
    """
    xdg = env.get("XDG_CACHE_HOME", "")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg)
    home = env.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".cache"


def cache_path(cache_key: str, env: Mapping[str, str]) -> Path:
    """Return the cache file path for a slot.

    Args:
        cache_key: The validated single-segment cache slot name.
        env: The environment mapping used to resolve the cache home.

    Returns:
        ``<cache_home>/doc-lattice/<cache_key>/load-cache.json``.
    """
    return cache_home(env) / "doc-lattice" / cache_key / CACHE_FILE_NAME


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    """What a cache load produced: the validated document and its serialized baseline.

    ``cache`` is None when the file is missing, unreadable, invalid, or stale, in which case
    ``baseline`` is None too and everything recomputes. ``baseline`` is the loaded document's
    ``model_dump(mode="json")``, kept for write-time change detection.
    """

    cache: CacheFile | None
    baseline: dict[str, object] | None


def load(path: Path) -> StoreSnapshot:
    """Read and validate the cache file, returning an empty snapshot on any failure.

    A missing, unreadable, non-UTF-8, non-JSON, schema-invalid, wrong-version, or
    wrong-tool-version file is treated as empty, silently by design.

    Args:
        path: The cache file path (see ``cache_path``).

    Returns:
        The snapshot holding the validated document and its baseline, or an empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return StoreSnapshot(cache=None, baseline=None)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return StoreSnapshot(cache=None, baseline=None)
    try:
        parsed = CacheFile.model_validate(data)
    except ValidationError:
        return StoreSnapshot(cache=None, baseline=None)
    if parsed.version != CACHE_VERSION or parsed.tool_version != __version__:
        return StoreSnapshot(cache=None, baseline=None)
    return StoreSnapshot(cache=parsed, baseline=parsed.model_dump(mode="json"))


def save_if_changed(path: Path, final: CacheFile, baseline: dict[str, object] | None) -> None:
    """Atomically replace the cache file if the document changed, else write nothing.

    Args:
        path: The cache file path.
        final: The completed cache document (see ``RunState.complete``).
        baseline: The loaded document's serialized form, or None when the load was empty.
    """
    if final.model_dump(mode="json") == baseline:
        return
    _write(path, final)


def _write(path: Path, cache_file: CacheFile) -> None:
    """Atomically replace the cache file, emitting one stderr diagnostic on failure.

    Writes through a temp file in the same directory, fsyncs, then ``os.replace``. Any
    OSError (unwritable directory, failed write or replace) is reported on stderr with a
    single line and swallowed, so a broken cache never changes a command's result or exit
    code. The temp file is always removed.
    """
    text = cache_file.model_dump_json()
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=CACHE_FILE_NAME, suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)  # noqa: PTH105 (tests monkeypatch os.replace directly)
        tmp = None
    except OSError as exc:
        sys.stderr.write(f"doc-lattice: could not write load cache at {path}: {exc}\n")
    finally:
        if tmp is not None:
            # The cleanup unlink must never escape: a write failure already emitted its one
            # diagnostic, and an OSError here (missing_ok only swallows FileNotFoundError)
            # would propagate past the handler and change the command's exit code, which the
            # cache contract forbids. A leaked temp is reclaimed on the next successful write.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
```

- [ ] **Step 4: Delegate LoadCache and prune `__init__.py`**

In `src/doc_lattice/cache/__init__.py`:

1. Delete `cache_home`, `cache_path`, `_read`, and `_write`; add `from .store import cache_home, cache_path, load, save_if_changed  # noqa: F401 (cache_home re-exported for doc_lattice.cache importers)` (`tests/test_orchestrate.py` and `tests/test_cache.py` import `cache_path`/`cache_home` from `doc_lattice.cache`; the noqa is temporary until Task 6's `__all__`).
2. In `LoadCache.open`, replace the `loaded = cls._read(path)` block with:

```python
        path = cache_path(cache_key, env)
        current_root = str(project_root.resolve())
        snapshot = load(path)
        if snapshot.cache is None:
            entries: dict[str, Entry] = {}
            roots: list[str] = []
            original: dict[str, object] | None = None
        else:
            entries = dict(snapshot.cache.entries)
            roots = list(snapshot.cache.roots)
            original = snapshot.baseline
```

3. In `LoadCache.finalize`, replace the final compare-and-write block with:

```python
        final = CacheFile(
            version=CACHE_VERSION,
            tool_version=__version__,
            roots=self._roots,
            entries=self._entries,
        )
        save_if_changed(self._path, final, self._original)
```

4. Prune imports that only `_read`/`_write` used: `contextlib`, `json`, `sys`, `tempfile`, `ValidationError`, `CACHE_FILE_NAME`. Keep `Mapping` (the `open` signature), `os` (`record_miss`/`lookup` signatures), `hashlib` (`lookup`), `CACHE_VERSION`, `__version__`, `MAX_STAT_ROOTS`.

- [ ] **Step 5: Migrate the moved tests out of test_cache.py**

In `tests/test_cache.py`, delete (now covered in `tests/test_cache_store.py`):
`test_cache_home_uses_absolute_xdg`, `test_cache_home_ignores_relative_xdg`, `test_cache_home_falls_back_to_home_dot_cache_when_xdg_unset`, `test_cache_home_falls_back_to_home_cache_dir_when_home_and_xdg_unset`, `test_cache_path_composes_slot_and_file_name`, `test_open_missing_file_is_empty`, `test_open_valid_file_loads_entries`, `test_open_corrupt_file_is_empty`, `test_open_invalid_utf8_is_empty`, `test_open_wrong_version_is_empty`, `test_open_wrong_tool_version_is_empty`, `test_open_invalid_meta_is_empty`, `test_finalize_write_failure_emits_one_stderr_line_and_does_not_raise`, `test_finalize_write_failure_does_not_raise_when_temp_cleanup_also_fails`.

Keep `cache_home` out of the `test_cache.py` import list once unused; keep `_sample_cache_file` only if a remaining test still uses it (after these deletions none should; delete it and prune imports ruff flags).

- [ ] **Step 6: Run the gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_cache_store.py tests/test_cache.py -v
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests && uv run --group dev ty check src
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/doc_lattice/cache tests/test_cache_store.py tests/test_cache.py
git commit -m "refactor: extract the cache store into cache/store.py"
```

---

### Task 6: Rewire orchestrate to the new lifecycle; delete LoadCache; finish the facade and test redistribution

**Files:**
- Modify: `src/doc_lattice/orchestrate.py` (imports + `_load_cached`)
- Modify: `src/doc_lattice/cache/__init__.py` (becomes the pure re-export facade)
- Modify: `tests/test_cache.py` (delete LoadCache unit tests now covered per-module; rewrite two lifecycle tests end-to-end; add the facade test)

**Interfaces:**
- Consumes: everything Tasks 2-5 produced.
- Produces: the final public surface. `doc_lattice.cache` exports exactly: `CacheFile`, `CacheHit`, `CacheMiss`, `Entry`, `LookupPolicy`, `NodePayload`, `RunState`, `SectionRecordModel`, `StatRecord`, `StoreSnapshot`, `cache_home`, `cache_path`, `make_entry`. `LoadCache` and `is_empty` are gone (spec section 2: deliberate removals, no deprecation shim).

- [ ] **Step 1: Rewrite the cache facade**

Replace the entire content of `src/doc_lattice/cache/__init__.py` with:

```python
"""The opt-in incremental load cache, split by lifecycle phase.

``schema`` holds the persistence models and codec, ``store`` owns the cache file on disk,
``lookup`` resolves one discovered doc to a hit or a miss without mutating anything, and
``state`` holds the run-local document between load and save. This module re-exports the
public surface; ``orchestrate._load_cached`` is the only production wiring point and owns the
transaction boundary (persist only after ``build_lattice`` succeeds).
"""

from .lookup import CacheHit, CacheMiss, LookupPolicy
from .schema import CacheFile, Entry, NodePayload, SectionRecordModel, StatRecord, make_entry
from .state import RunState
from .store import StoreSnapshot, cache_home, cache_path

__all__ = [
    "CacheFile",
    "CacheHit",
    "CacheMiss",
    "Entry",
    "LookupPolicy",
    "NodePayload",
    "RunState",
    "SectionRecordModel",
    "StatRecord",
    "StoreSnapshot",
    "cache_home",
    "cache_path",
    "make_entry",
]
```

- [ ] **Step 2: Rewire orchestrate**

In `src/doc_lattice/orchestrate.py`, replace the import block and `_load_cached` (keep `load_lattice` and `_load_uncached` byte-identical to today):

```python
"""Wire config, discovery, parsing, and loading into a Lattice."""

import os

from .cache import CacheHit, LookupPolicy, RunState, cache_path, lookup, make_entry, store
from .config import ProjectConfig
from .discovery import decode_doc, discover_doc_paths, read_doc
from .frontmatter_parser import parse_meta, split_frontmatter
from .loader import build_lattice, derive_file_sections
from .model import Lattice, ParsedDoc
```

(`lookup` and `store` resolve as submodule attributes of the package; the facade import above makes them importable this way.)

```python
def _load_cached(project: ProjectConfig, *, require_verified: bool) -> Lattice:
    """The incremental load path. Writes the cache only after a successful build."""
    config = project.config
    # ty cannot narrow cache_key: str | None from the caller's is-None branch across the call;
    # this assert documents and enforces that invariant for cache_path's str parameter.
    assert config.cache_key is not None  # noqa: S101
    path = cache_path(config.cache_key, os.environ)
    snapshot = store.load(path)
    current_root = str(project.project_root.resolve())
    state = RunState.begin(snapshot.cache, current_root)
    stat_tier_enabled = config.cache_trust_stat and not require_verified
    policy = LookupPolicy(current_root=current_root, trust_stat=stat_tier_enabled)
    parsed: list[ParsedDoc] = []
    for doc_path in discover_doc_paths(project.resolved_roots, config.ignore_globs):
        rel_key = doc_path.relative_to(project.project_root).as_posix()
        result = lookup.resolve(state.entry(rel_key), doc_path, policy)
        if isinstance(result, CacheHit):
            state.claim(rel_key, result.refreshed_stat)
            if result.doc is not None:
                parsed.append(result.doc)
            continue
        text = decode_doc(doc_path, result.data)
        raw_meta, body = split_frontmatter(text)
        meta = parse_meta(raw_meta, doc_path)
        sections = derive_file_sections(body) if meta is not None else None
        state.replace(
            rel_key, make_entry(result.data, meta, body, sections, result.stat, current_root)
        )
        if meta is not None:
            parsed.append(ParsedDoc(path=doc_path, meta=meta, body=body, sections=sections))
    lattice = build_lattice(parsed)
    store.save_if_changed(path, state.complete(), snapshot.baseline)
    return lattice
```

- [ ] **Step 3: Finish the test redistribution in tests/test_cache.py**

Delete these tests (each is now covered by a per-module file, as noted):

| Delete from test_cache.py | Covered by |
|---|---|
| `_open`, `_doc_bytes`, `_entry_for` helpers | no longer needed |
| `test_verify_tier_hit_reconstructs_parsed_doc` | test_cache_lookup + test_cache_schema |
| `test_verify_tier_non_node_hit_returns_none_doc` | test_cache_lookup |
| `test_content_change_is_a_miss_carrying_current_bytes` | test_cache_lookup |
| `test_absent_entry_is_a_miss` | test_cache_lookup |
| `test_stat_tier_hit_skips_reading_the_file` | test_cache_lookup |
| `test_stat_tier_disabled_without_trust_stat_falls_to_verify` | test_cache_lookup |
| `test_stat_tier_size_mismatch_falls_through_to_verify_hit` | test_cache_lookup (parametrized) |
| `test_stat_tier_mtime_mismatch_falls_through_to_verify_hit` | test_cache_lookup (parametrized) |
| `test_stat_tier_no_record_for_current_root_falls_through_to_verify_hit` | test_cache_lookup |
| `test_require_verified_disables_stat_tier` | end-to-end `test_require_verified_load_sees_fresh_content_after_same_stat_rewrite` (kept) plus the orchestrate predicate is one line |
| `test_lookup_deleted_file_raises_unreadable` | test_cache_lookup (`test_stat_tier_raced_stat_failure_raises_unreadable`) |
| `test_verify_hit_stat_refresh_uses_the_stat_captured_with_the_read` | test_cache_lookup (refreshed_stat fact) + test_cache_state (claim applies it) |
| `test_miss_carries_and_records_the_stat_captured_with_the_read` | test_cache_lookup (miss fact) + test_cache_schema (make_entry stores it) |
| `test_record_miss_resets_stats_to_current_root` | test_cache_schema (`make_entry`) |
| `test_record_miss_non_node_stores_null_node` | test_cache_schema |
| `test_finalize_writes_current_root_at_ledger_tail` | rewritten end-to-end below |
| `test_fully_warm_same_root_run_writes_nothing` | rewritten end-to-end below |
| `test_presence_reclamation_drops_entry_no_root_claims` | test_cache_state |
| `test_presence_reclamation_keeps_entry_a_second_root_claims` | test_cache_state |
| `test_ledger_evicts_over_cap_head_roots_and_scrubs_their_stats` | test_cache_state |

Keep unchanged: `test_default_tier_matches_uncached_under_random_edits`, `test_require_verified_load_sees_fresh_content_after_same_stat_rewrite`, `test_trust_stat_serves_unreadable_file_from_cache_a_documented_caveat`, `test_verify_tier_serves_schema_valid_node_corruption_a_documented_limit`, and `_run_check`.

Add these three tests:

```python
def test_load_writes_current_root_at_ledger_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: tail\n", encoding="utf-8")
    load_lattice(load_config(None, tmp_path))
    path = cache_path("tail", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    written = CacheFile.model_validate_json(path.read_text(encoding="utf-8"))
    assert written.roots[-1] == str(tmp_path.resolve())
    assert "docs/a.md" in written.entries


def test_fully_warm_same_root_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: warm\n", encoding="utf-8")
    load_lattice(load_config(None, tmp_path))  # cold: writes the cache
    path = cache_path("warm", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns
    load_lattice(load_config(None, tmp_path))  # warm: verify-tier hits, zero writes
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime_before


def test_facade_exports_the_full_legacy_surface():
    # Pins spec section 2: every name that survives the refactor stays importable from
    # doc_lattice.cache, so the per-module test migration cannot silently shrink the facade.
    from doc_lattice.cache import (  # noqa: F401, PLC0415
        CacheFile,
        CacheHit,
        CacheMiss,
        Entry,
        LookupPolicy,
        NodePayload,
        RunState,
        SectionRecordModel,
        StatRecord,
        StoreSnapshot,
        cache_home,
        cache_path,
        make_entry,
    )
```

Update the module docstring to reflect the slimmed scope ("End-to-end lifecycle tests for the load cache through orchestrate and the CLI, plus the facade contract.") and prune the import block to what remains: expect `json`, `os`, `pytest`, hypothesis pieces, `CacheFile`, `cache_path`, `check_lattice`/`statuses_json`, `load_config`, `UnreadableDocError`, `load_lattice`. Delete `hashlib`, `types`, `LoadCache`, `CacheHit`, `CacheMiss`, `Entry`, `NodePayload`, `SectionRecordModel`, `StatRecord`, `cache_home`, `NodeMeta`, `FileSections`, `ParsedDoc`, `SectionRecord`, `MAX_STAT_ROOTS` if unused (ruff will confirm).

- [ ] **Step 4: Run the full gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
```

Expected: all pass. `grep -rn "LoadCache" src tests` returns nothing.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice tests/test_cache.py
git commit -m "refactor: rewire orchestrate to the phase-separated cache lifecycle and drop LoadCache"
```

---

### Task 7: Format-parity verification and documentation

**Files:**
- Modify: `CLAUDE.md` (the "Pure vs impure split" paragraph)
- Modify: `CHANGELOG.md` (`[Unreleased]` > `Changed`)

**Interfaces:**
- Consumes: the Task 1 baseline at `/tmp/dl-cache-parity/`.
- Produces: the verified, documented branch, ready for PR.

- [ ] **Step 1: Prove cache-file byte-parity across the refactor**

Rerun the exact Task 1 corpus with the refactored code and diff the two cache files. The corpus files have not been touched since Task 1, so sizes and mtimes are identical; `tool_version` is unchanged on this branch; therefore the files must be byte-identical.

```bash
env -u FORCE_COLOR XDG_CACHE_HOME=/tmp/dl-cache-parity/xdg-after \
  uv run doc-lattice check /tmp/dl-cache-parity/proj > /tmp/dl-cache-parity/check-after.txt; echo "exit=$?"
diff /tmp/dl-cache-parity/check-before.txt /tmp/dl-cache-parity/check-after.txt && echo OUTPUT-OK
diff /tmp/dl-cache-parity/xdg-before/doc-lattice/parity/load-cache.json \
     /tmp/dl-cache-parity/xdg-after/doc-lattice/parity/load-cache.json \
  && echo PARITY-OK
```

Expected: the same exit code as the Task 1 run, `OUTPUT-OK`, then `PARITY-OK` with no diff output. If the JSON diff is non-empty, STOP: the refactor changed the format or the write logic; find and fix the divergence before proceeding (compare with `jq .` on both files to localize it).

Also confirm the warm-run contract end-to-end:

```bash
env -u FORCE_COLOR XDG_CACHE_HOME=/tmp/dl-cache-parity/xdg-after \
  uv run doc-lattice check /tmp/dl-cache-parity/proj > /tmp/dl-cache-parity/warm1.txt; echo "exit=$?"
env -u FORCE_COLOR XDG_CACHE_HOME=/tmp/dl-cache-parity/xdg-after \
  uv run doc-lattice check /tmp/dl-cache-parity/proj > /tmp/dl-cache-parity/warm2.txt; echo "exit=$?"
diff /tmp/dl-cache-parity/warm1.txt /tmp/dl-cache-parity/warm2.txt && echo WARM-OK
```

Expected: identical warm outputs (`WARM-OK`) and identical exit codes across all runs, both also matching `check-after.txt`.

- [ ] **Step 2: Update CLAUDE.md**

In the "Pure vs impure split" paragraph of `CLAUDE.md`: remove `cache` from the impure list sentence ("Only `config`, `discovery`, `orchestrate`, `cli`, and `cache` touch the disk...") and describe the package split instead. Replace the clause "`cache` reads and atomically writes the opt-in load cache under the user cache home" with wording equivalent to: "the `cache` package splits by phase: `cache/schema.py` (models + codec) and `cache/state.py` (run-local `RunState`) are pure, while `cache/store.py` (reads and atomically writes the opt-in load cache under the user cache home) and `cache/lookup.py` (doc reads and stats for tier selection) are impure". Also add `RunState`/`store`/`lookup` phrasing to the pure-module list sentence where the other pure modules are enumerated.

- [ ] **Step 3: Update CHANGELOG.md**

Under `## [Unreleased]` add (create the `### Changed` subsection if absent, above the first versioned heading):

```markdown
### Changed

- Internal: the load cache module is now a phase-separated `doc_lattice/cache/` package
  (schema/codec, store, lookup, run state). No user-facing behavior change; the cache file
  format is unchanged.
```

- [ ] **Step 4: Run the final full gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
```

Expected: all pass; pytest coverage report at or above 99.40%.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
git commit -m "docs: record the cache package split in CLAUDE.md and the changelog"
```

---

## Post-plan checklist (for the finishing session)

- Run the repo verify flow before claiming completion (superpowers:verification-before-completion).
- PR against `main` referencing the spec `docs/superpowers/specs/2026-07-13-cache-package-refactor-design.md`; note the parity evidence (PARITY-OK / WARM-OK) in the PR body. No release/version bump: this is internal, ships with the next release train.
