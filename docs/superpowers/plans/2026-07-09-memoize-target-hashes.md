# Per-Run Target-Hash Memoization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute each resolved target's content hash at most once during a single `check` or
`reconcile` invocation without changing behavior.

**Architecture:** Add a caller-owned cache helper to the pure `resolve.py` module. `check_lattice`
and `reconcile` each allocate one `dict[TargetId, str]` and pass it to the helper for every
resolvable edge. Tests monkeypatch `resolve.content_hash`, the helper's lookup location, to verify
deduplication.

**Tech Stack:** Python 3.13+, `pytest`, `pytest-mock`, `uv`, `ruff`, `ty`.

---

## File Structure

| File | Change |
| --- | --- |
| `src/game_lattice/resolve.py` | Add pure `cached_target_hash` with caller-owned cache. |
| `src/game_lattice/check.py` | Allocate one cache per `check_lattice` call and thread it to `_classify`. |
| `src/game_lattice/reconcile.py` | Allocate one cache per `reconcile` call and use it for hash lookups. |
| `tests/test_resolve.py` | Verify a cache has one calculation per distinct `TargetId`. |
| `tests/test_check.py` | Verify three or more shared edges invoke the helper's hash function once. |
| `tests/test_reconcile.py` | Verify `reconcile_all=True` invokes the helper's hash function once. |
| `CHANGELOG.md` | Record the internal performance improvement under `[Unreleased]`. |

### Task 1: Add the cached resolver helper

**Files:**
- Modify: `tests/test_resolve.py`
- Modify: `src/game_lattice/resolve.py`

- [ ] **Step 1: Write the failing helper test**

Import `game_lattice.resolve as resolve` and `cached_target_hash`. Add a test that creates the
existing `_lattice()`, wraps `resolve.content_hash` with a counter using `monkeypatch`, calls
`cached_target_hash` twice for `TargetId("doc", "accent")` and once for `TargetId("doc")`, and
asserts equal repeated results plus exactly two underlying hash calls.

- [ ] **Step 2: Run the test to verify red**

Run: `uv run --group dev pytest tests/test_resolve.py -k cached_target_hash -v`

Expected: collection fails because `cached_target_hash` does not exist.

- [ ] **Step 3: Implement the minimal pure helper**

Add this function to `resolve.py` and import `content_hash` from `.hashing`:

```python
def cached_target_hash(lattice: Lattice, target_id: TargetId, cache: dict[TargetId, str]) -> str:
    """Return the content hash for target_id, computing it at most once per cache.

    A second-level cache of split body lines per path could avoid repeated section parsing, but is
    a separate optimization and intentionally out of scope.
    """
    if target_id not in cache:
        cache[target_id] = content_hash(target_content(lattice, target_id))
    return cache[target_id]
```

- [ ] **Step 4: Run the test to verify green**

Run: `uv run --group dev pytest tests/test_resolve.py -k cached_target_hash -v`

Expected: one test passes.

### Task 2: Use the cache from check and reconcile

**Files:**
- Modify: `tests/test_check.py`
- Modify: `tests/test_reconcile.py`
- Modify: `src/game_lattice/check.py`
- Modify: `src/game_lattice/reconcile.py`

- [ ] **Step 1: Write failing caller tests**

In each test module, create a synthetic lattice with a single upstream section and at least three
downstream edges referencing it. Monkeypatch `game_lattice.resolve.content_hash` with a counting
wrapper around the original. For `check_lattice`, assert the number of statuses and exactly one
hash invocation. For `reconcile(..., "", ref=None, reconcile_all=True)`, assert the plan has the
three downstream refs and exactly one hash invocation.

- [ ] **Step 2: Run both tests to verify red**

Run: `uv run --group dev pytest tests/test_check.py tests/test_reconcile.py -k "memoize or cache" -v`

Expected: both counting assertions fail because each edge still hashes independently.

- [ ] **Step 3: Implement the cache wiring**

In `check.py`, replace direct hashing imports with `cached_target_hash`; allocate
`cache: dict[TargetId, str] = {}` at the start of `check_lattice`, add it to `_classify`, and call
`cached_target_hash(lattice, edge.target_id, cache)` after the broken-edge guard.

In `reconcile.py`, replace direct hashing imports with `cached_target_hash`; allocate
`cache: dict[TargetId, str] = {}` before the node loop and call the helper after the broken-edge
guard. Import `TargetId` for the annotations.

- [ ] **Step 4: Run the caller tests to verify green**

Run: `uv run --group dev pytest tests/test_check.py tests/test_reconcile.py -k "memoize or cache" -v`

Expected: both tests pass with one hash operation per distinct target.

### Task 3: Record and validate the change

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add documentation**

Under `## [Unreleased]`, `### Changed`, add:

```markdown
- `check` and `reconcile` now memoize target-content hashes within each run, avoiding repeated
  section extraction and hashing for multiple edges that share a target (#25).
```

- [ ] **Step 2: Run focused behavioral tests**

Run: `uv run --group dev pytest tests/test_resolve.py tests/test_check.py tests/test_reconcile.py -v`

Expected: all focused tests pass.

- [ ] **Step 3: Run repository quality gates**

Run:

```bash
uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
```

Expected: every command exits zero; pytest reports at least 80 percent coverage.

- [ ] **Step 4: Review and publish**

Inspect the complete diff against the branch base, fix all valid review findings at their root cause,
then stage only issue #25 files, commit, push `perf/memoize-content-hashes`, and open a draft pull
request with validation output in its body.
