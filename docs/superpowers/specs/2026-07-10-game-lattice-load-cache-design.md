# Opt-In Incremental Load Cache: Design Spec

**Date:** 2026-07-10
**Status:** Proposed (the spec PR for issue #28). Implementation follows as a separate PR
referencing this spec.
**Issue:** GitHub issue #28, `opt-in incremental lattice load cache`.

## 1. Goal and scope

Every command rebuilds the lattice from scratch: discovery re-walks the roots, every doc is
re-read, its frontmatter re-parsed and re-validated, and its TOC, slugs, and spans re-derived.
The binding local-core design deferred a "gitignored performance cache" as not needed at the
original corpus size. This spec adds that cache for adopters whose doc sets have grown to the
thousands, as a pure accelerator with an absolute correctness guarantee:

> For every command, under any cache state (no cache configured, cold, warm, stale, corrupt,
> wrong version, `cache_trust_stat` on or off), stdout, stderr, exit code, and any file
> mutations are byte-identical to an uncached run. Only timing and the contents of the cache
> file may differ.

The single stated exception is a warning emitted when the cache file itself cannot be written
(section 7), which exists only when the cache is broken and never changes command results.

Out of scope: watch mode, a daemon, cross-machine cache sharing, and everything in section 11.

## 2. Config surface

Two new optional keys in `Config` (`.game-lattice.yml`):

- `cache_key: str | null = null`. Absent or null disables caching entirely; the load path is
  bit-for-bit today's. When set, it names the cache slot under the user-level cache home
  (section 3). Validated by a pydantic field validator as a single safe path segment:
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. The leading character class rejects `.` and `..` and
  hidden-directory names; there is no way to express a separator or a traversal. A violation
  is a `ConfigError` (exit 2) naming the key and the allowed pattern.
- `cache_trust_stat: bool = false`. Enables the stat fast tier (section 5). Setting it without
  `cache_key` is a `ConfigError` naming the fix, keeping the config strict and explicit.

No CLI flag controls the cache in v1. Opt-in and opt-out are config-only.

## 3. Cache location: why a key, not a path

The issue sketch proposed an in-repo `cache_dir` contained by `safe_resolve`. That placement
fails the dominant workflow: work happens in per-task git worktrees. A gitignored in-repo cache
starts cold in every new worktree, so the warm case rarely materializes; a committed cache
violates the binding design's rule that the derived graph is never committed and would be
constant rebase churn. The cache therefore lives outside every checkout:

```
<cache_home>/game-lattice/<cache_key>/load-cache.json
```

where `<cache_home>` is `$XDG_CACHE_HOME` when that variable is set to an absolute path
(a relative value is ignored, per the XDG base directory spec), and `~/.cache` otherwise.
The directory is created on first write. Because `.game-lattice.yml` is committed, every
clone and every worktree of the project shares one warm cache with zero per-checkout setup.

This supersedes the issue's `safe_resolve` containment bullet with a stricter rule:
the config carries no path semantics at all. `cache_key` is one validated segment, so
containment holds by construction and a hostile config can write nothing outside
`<cache_home>/game-lattice/`.

Two different projects configured with the same `cache_key` remain correct: a content-hash hit
implies identical bytes and therefore identical derivations, and stat records are looked up per
project root (section 4). The collision only causes overwrite churn, which the README notes.

## 4. Cache file and entry schema

One JSON document, version 1:

```json
{
  "version": 1,
  "roots": ["/abs/path/least-recently-used-root", "/abs/path/most-recently-used-root"],
  "entries": {
    "docs/design/save-format.md": {
      "file_sha256": "<sha256 of the raw file bytes, 64 hex chars>",
      "stats": {
        "/abs/path/to/a/project/root": { "size": 4213, "mtime_ns": 1720512345123456789 }
      },
      "node": {
        "meta": { "...": "validated NodeMeta model_dump(mode='json')" },
        "body": "verbatim decoded text after the closing frontmatter fence",
        "total_lines": 57,
        "sections": [ { "anchor": "slot-table", "start": 10, "end": 22 } ]
      }
    },
    "docs/notes/scratch.md": {
      "file_sha256": "<64 hex chars>",
      "stats": {
        "/abs/path/to/a/project/root": { "size": 90, "mtime_ns": 1720512345000000000 }
      },
      "node": null
    }
  }
}
```

- **Entries are keyed by path relative to the project root** (POSIX separators). The cache is
  therefore independent of `docs_roots` and `ignore_globs`: discovery decides the file set each
  run and the cache is consulted only per discovered file, so config changes need no
  invalidation logic.
- **`file_sha256` is the full sha256 of the raw file bytes.** It is deliberately not the
  lattice's 32-hex truncated hash of canonicalized text; the name keeps the two from being
  confused. It is the sole authority for entry validity.
- **`stats` maps a project-root realpath to that checkout's `(size, mtime_ns)`** for the file.
  Per-root keying is what lets several worktrees share one cache without ping-ponging each
  other's stat data.
- **`roots` is a least-recently-used ledger of project roots, bounded by
  `MAX_STAT_ROOTS = 8`** (a fixed constant in `constants.py`, not configurable). Every run
  moves its own root to the tail at write time; when the ledger exceeds the cap, head roots are
  evicted and their keys scrubbed from every entry's `stats` map in the same write. This bounds
  stat data to at most 8 roots times the tracked files, with no clock and no age heuristic:
  dead worktrees age to the head and are scrubbed by runs from live roots. An evicted root that
  returns simply re-warms through the verify tier. Correctness is never involved; stats are
  only ever a hint beneath the content hash.
- **`node: null` caches the verdict "not a lattice node"** for a discovered file without an
  `id`, so such files skip frontmatter split and YAML parsing on a hit. Non-node entries carry
  no body or sections.
- **`sections` stores exactly what `build_lattice` consumes**: each heading's resolved anchor
  id and inclusive line span, plus `total_lines`. `Heading` objects are not needed after
  anchor and span derivation. Duplicate-anchor detection still happens at registration time
  inside `build_lattice`, so `DuplicateIdError` behavior is identical from cache or fresh parse.
- **Target hashes are deliberately not cached** (a deviation from the issue sketch, evaluated
  and rejected). Issue #25's per-run memoization already bounds that cost to one
  canonicalize-plus-hash per referenced target per run, and pre-seeding the caller-owned memo
  would leak cache state through the pure command layer, since `check` and `reconcile` would
  need cache-aware wiring. If the benchmark shows referenced-target hashing is material at 5k
  docs, adding a field is a clean v2 schema change behind a version bump.
- **Only successful loads are cached.** A file whose frontmatter fails to parse or validate
  aborts the load (exit 2) before any cache write, so errors are never cached and a broken file
  fails identically under any cache state.

## 5. Load flow and tiers

With `cache_key` unset, `orchestrate.load_lattice` is unchanged. With it set, the flow is:
read and validate the cache file (any failure yields an empty cache, section 7), then for each
discovered path:

1. **Stat tier**, only when `cache_trust_stat: true`: if `stats[current_root]` exists and
   matches the file's `(st_size, st_mtime_ns)`, use the entry without opening the file. The
   body comes from the entry. This tier carries the standard mtime caveat: a rewrite that
   preserves size and `mtime_ns` serves stale data until the file is touched. That caveat is
   why the tier is opt-in and why the default tier exists.
2. **Verify tier**, the default: read the raw bytes and hash them. On a `file_sha256` match,
   reuse the entry's parsed results, skipping UTF-8 decode, frontmatter split, YAML parse,
   pydantic validation, and TOC, slug, and span derivation, which is the dominant CPU cost.
   `stats[current_root]` is refreshed if it drifted (for example after `touch`), so a later
   switch to `cache_trust_stat` starts warm.
3. **Miss**: run today's full parse path (identical errors, identical warnings), then replace
   the entry: new `file_sha256`, `stats` reset to only the current root, new `node` payload.

At the end of a successful load: the current root moves to the ledger tail, over-cap head roots
are evicted and scrubbed, and the file is atomically replaced (temp file in the same directory,
fsync, `os.replace`) if and only if something changed: an entry added or replaced, a stat
refreshed, or the ledger reordered or scrubbed. A fully warm repeat run performs one cache read
and zero cache writes. If the load aborts on any error, no cache write happens at all.

Entries whose paths were not discovered this run are kept, not pruned. Pruning by one
checkout's view would evict entries a sibling worktree on another branch still needs. Growth is
bounded by the union of doc paths across branches; a format version bump resets wholesale, and
deleting the cache directory is the manual reset.

`reconcile` is unaffected in its write phase: it already re-reads each downstream file fresh at
write time, bypassing the lattice snapshot entirely. Its rewrites simply make those files miss
on the next load.

## 6. Invalidation summary

- The content hash is the only validity authority. The verify tier proves it per run; the stat
  tier presumes it from an exact `(size, mtime_ns)` match under the documented caveat.
- Any miss replaces the entry and resets its `stats` to the current root.
- A `version` mismatch, or any structural invalidity, discards the whole file (section 7).
- Config changes never require invalidation (relative-path keying, section 4).

## 7. Failure modes

- **Cache read**: file missing, unreadable, invalid JSON, wrong `version`, or failing schema
  validation (including `NodeMeta.model_validate` on an entry's `meta`) causes the whole cache
  to be treated as empty. Everything recomputes and the file is rewritten. Silent by design;
  per-entry salvage is not worth the code for a rare event.
- **Cache write**: an unwritable directory or a failed write emits one `warnings.warn` (the
  channel discovery already uses for skipped symlinks) and the command result is unchanged.
  Reads stay silent; writes warn because a permanently unwritable cache means the accelerator
  is off and the user should know.
- **Concurrent runs** (several worktrees at once): atomic replace means a reader sees the old
  or the new file, never a torn one. Last writer wins; a lost update is only a future miss.

No new exception types are needed. The cache never raises; config validation reuses
`ConfigError`.

## 8. Purity boundary and module changes

- **`cache.py` (new, impure)**: the only module that touches the cache. It computes the cache
  path from the environment, reads, validates, and atomically writes the file, owns the entry
  schema as pydantic models, and implements tier selection. It is wired only from
  `orchestrate.py`. It stays clean under `scripts/check_typing_boundaries.py`: raw
  `json.loads` output flows directly into `model_validate`, so the module needs no
  `typing.Any` and no `cast`.
- **`config.py`**: the two new fields and their validators.
- **`model.py`**: new frozen dataclasses `FileSections` (`total_lines: int`,
  `sections: tuple[SectionRecord, ...]`) and `SectionRecord` (`anchor: str`, `start: int`,
  `end: int`). `ParsedDoc` gains `sections: FileSections | None = None`.
- **`loader.py` (stays pure)**: the existing inline TOC, anchor, and span logic is extracted
  into `derive_file_sections(body) -> FileSections`; `build_lattice` uses `doc.sections` when
  present and derives it otherwise. This is the only pure-layer change and it is a seam, not
  behavior: cached and derived sections are the same values by construction, pinned by a
  property test (section 9).
- **`orchestrate.py`**: the branch described in section 5; the no-cache path is unchanged code.
- **`constants.py`**: `CACHE_VERSION`, `MAX_STAT_ROOTS`, and the cache file name.

`model`, `sections`, `hashing`, `resolve`, and every command module are otherwise untouched,
and no pure module gains filesystem access.

## 9. Determinism guarantee and testing

The guarantee in section 1 is enforced by:

- **A hypothesis property test**: random edit sequences over a synthetic doc set (body edits,
  frontmatter edits, file add, delete, and rename, `touch` without content change, and a
  same-size same-`mtime_ns` rewrite to pin the documented stat-tier caveat). After each step,
  a cached and an uncached load must produce structurally equal `Lattice` values and
  byte-identical `check` output. The trust-stat caveat case asserts the documented stale
  outcome, so the caveat is tested behavior rather than folklore.
- **Round-trip property test**: for generated bodies, `derive_file_sections` equals the value
  reconstructed from its serialized cache form.
- **Unit tests**: each corruption mode (truncated file, invalid JSON, wrong version, schema
  violation, invalid `meta`) recomputes silently; a non-node hit performs no YAML parse
  (counting monkeypatch on the parser); per-root stats isolation across two roots; stats reset
  on content change; ledger LRU order, eviction at the cap, and scrubbing of evicted roots;
  fully warm run writes nothing; an injected write failure warns once and leaves no partial
  file; `cache_key` validation accepts and rejects the right shapes; `cache_trust_stat`
  without `cache_key` is a `ConfigError`; `XDG_CACHE_HOME` absolute, relative, and unset
  handling; and cached-versus-uncached CLI byte-equality for `check`, `impact`, `graph`, and
  `lint` on the shared `lattice_dir` fixture.

The suite stays above the existing 80 percent coverage gate; new tests mirror sources as
`tests/test_cache.py` plus additions to the loader, config, and CLI suites.

## 10. Benchmark plan

`scripts/bench_load_cache.py` (dev-only, not shipped): generates a synthetic corpus at 1k and
5k docs with parameterized heading and edge counts, then reports the median of 5 runs of
`load_lattice` wall time in four states: uncached, cold cache (including the write), warm
verify tier, and warm stat tier. It also reports the cache file size and the share of warm-run
time spent in `json.loads`, which is the known cost of the single-file JSON format.

Acceptance threshold: the warm verify tier is at least 3x faster than uncached at 5k docs,
otherwise the feature is reconsidered before release. The measured numbers go in the
implementation PR description.

## 11. Alternatives considered

1. **SQLite storage**: indexed rows, partial IO, `user_version` pragma. Rejected: an opaque
   binary artifact in a deterministic, debuggable-text tool, more corruption modes, and more
   code for the same guarantee. The `version` field gives a clean migration path if the
   benchmark ever shows whole-file JSON parsing eating the win.
2. **One JSON entry file per doc**: trivially incremental writes. Rejected: thousands of tiny
   reads per run make the cache's own IO comparable to reading the docs.
3. **In-repo `cache_dir`** (the issue sketch): rejected for the worktree dilemma in section 3.
4. **`cache_dir` allowed to point outside the repo under a dual-root containment rule**:
   rejected; it puts path semantics and developer-specific paths into a committed config and
   needs a subtler containment rule than "one validated segment".
5. **Trusting stat by default with opt-in verification**: rejected; the issue's own priority
   is that invalidation correctness beats speed, so the exact tier is the default and the
   presumptive tier is the opt-in.
6. **Caching target hashes**: deferred with rationale in section 4.
7. **Pruning entries not discovered in the current run**: rejected; it evicts entries other
   worktrees still need (section 5).
8. **Age-based garbage collection of stat roots**: rejected; it needs a wall clock, against
   the repo's determinism posture, while the LRU ledger is deterministic given the run
   sequence (section 4).

## 12. Non-goals

Target-hash caching (v2 candidate), a `--no-cache` CLI flag, entry pruning or garbage
collection beyond the roots ledger, compression, per-entry corruption salvage, watch mode, a
daemon, and cross-machine cache sharing.

## 13. Documentation

The implementation PR updates: the README (config keys, cache location, the worktree
rationale, the shared-`cache_key` collision note, and the stat-tier caveat), `CHANGELOG.md`
under `[Unreleased]` as `Added`, and `init`'s config template with a commented-out
`# cache_key: my-project-docs` example. CLAUDE.md's module map gains `cache.py` on the impure
side.
