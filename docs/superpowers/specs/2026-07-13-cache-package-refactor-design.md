# Cache Package Refactor: Design Spec

**Date:** 2026-07-13
**Status:** Approved. Internal refactor; no user-facing behavior change.
**Supersedes:** the single-module statement in section 8 of
`2026-07-10-doc-lattice-load-cache-design.md` ("`cache.py` (new, impure): the only module that
touches the cache"). Every behavioral guarantee in that spec's sections 1 through 7 is preserved
unchanged; only the internal module layout described here replaces it.

## 1. Goal and scope

`src/doc_lattice/cache.py` (407 lines) holds one class, `LoadCache`, that mixes four kinds of
responsibility: disk I/O (cache path resolution, read and validation, atomic write, the stderr
diagnostic), pure persistence logic (the pydantic schema, DTO reconstruction, entry construction),
pure run-local bookkeeping (entries, root claims, LRU ledger maintenance, eviction), and impure
per-file tier selection (stat tier, verify tier). Its public surface reflects the entanglement:
`open()`, `lookup()`, `record_miss()`, and `finalize()` each drive a different lifecycle phase of
one mutable object.

This refactor splits the module into a `doc_lattice/cache/` package whose modules separate those
phases, makes lookup results carry facts instead of performing hidden mutations, and leaves
runtime behavior and the v1 JSON cache format bit-identical. Motivations, in equal measure:
clarity at the module level, direct unit testability of the pure logic (today reachable only
through `tmp_path` plus full `LoadCache` setup), and seams for future cache work (for example the
target-hash caching deferred by the load-cache spec).

Out of scope: any change to tier semantics, the cache schema, the change-detection rule, error
messages, exit codes, the stderr diagnostic, or the config surface. The uncached load path is
untouched.

## 2. Package layout

`src/doc_lattice/cache.py` becomes:

```
src/doc_lattice/cache/
  __init__.py   re-exports the orchestrate-facing public surface
  schema.py     pure: persistence models and the codec
  store.py      impure: cache file location, load, atomic save, diagnostics
  state.py      pure: RunState, the mutable run-local cache document
  lookup.py     impure, non-mutating: stat-tier and verify-tier resolution
```

There is no `cache/orchestrate.py`. The cached-load loop stays in
`doc_lattice/orchestrate.py::_load_cached`, which remains the single wiring point; the cache
serves the engine and never imports the loader or parser layers.

- **`schema.py` (pure).** The five pydantic models moved verbatim: `StatRecord`,
  `SectionRecordModel`, `NodePayload`, `Entry`, `CacheFile`. Plus the codec, pure functions
  operating only on schema and domain types:
  - `reconstruct_doc(entry, path) -> ParsedDoc | None` (today's `LoadCache._reconstruct`),
  - `make_entry(data, meta, body, sections, st, current_root) -> Entry` (the construction half
    of today's `record_miss`, including the full sha256 of the raw bytes and the stats map reset
    to the current root only),
  - `stat_record(st) -> StatRecord`, a small helper shared by `make_entry` and the verify-tier
    refresh.

  The codec is folded into `schema.py` rather than a separate `codec.py`: it is about 40 lines,
  operates exclusively on the schema types, and `schema.py` already imports the domain layer
  because `NodePayload.meta` is a `NodeMeta`. A schema change (for example a v2 target-hash
  field) touches models and conversion together, so they are one seam.

- **`store.py` (impure).** `cache_home(env)` and `cache_path(cache_key, env)` moved verbatim.
  `load(path) -> StoreSnapshot` is today's `LoadCache._read` including the `version` and
  `tool_version` gates. `save_if_changed(path, final, baseline)` is today's dump comparison plus
  `_write` verbatim: mkdir, mkstemp in the same directory, fsync, `os.replace`, one stderr
  diagnostic on failure, suppressed temp cleanup with its explanatory comment.

  ```python
  @dataclass(frozen=True, slots=True)
  class StoreSnapshot:
      cache: CacheFile | None                 # None: missing, invalid, or stale
      baseline: dict[str, object] | None      # model_dump(mode="json") of what loaded, or None
  ```

- **`state.py` (pure).** `RunState`, the only mutable abstraction, narrowly defined as the cache
  document being updated during one load attempt. It holds entries, the roots ledger, the
  current root, and the discovered set. It knows nothing about paths, JSON, stderr, or hashing.

- **`lookup.py` (impure, non-mutating).** `resolve(entry, path, policy)` plus the fact types
  `LookupPolicy`, `CacheHit`, and `CacheMiss`. It stats and reads docs (via
  `discovery.read_doc_bytes_and_stat`) but never touches `RunState`.

- **`__init__.py`.** Re-exports the complete surviving surface of today's `cache.py`, not just
  what `orchestrate` needs: the five schema models (`CacheFile`, `Entry`, `NodePayload`,
  `SectionRecordModel`, `StatRecord`), `cache_home`, `cache_path`, and the new names
  (`CacheHit`, `CacheMiss`, `LookupPolicy`, `RunState`, `StoreSnapshot`, `make_entry`). Every
  import of a surviving name from `doc_lattice.cache` (as in `tests/test_orchestrate.py` and
  today's `tests/test_cache.py`) keeps working unchanged. `orchestrate` imports the `store` and
  `lookup` submodules directly for their functions.

  To be explicit about the contract: `doc_lattice.cache` defines `__all__` as the exact 13-name
  internal facade marker and pinned refactor contract. It still has no documented external Python
  API, and neither the module nor its import surface is a public compatibility promise. The
  removals of `LoadCache` and `is_empty` are deliberate and get no deprecation shim; every name
  that survives the refactor stays importable from `doc_lattice.cache`.

Deletions: the `LoadCache` class, and its `is_empty` property (test-only today, no production
caller; the new tests assert on `RunState` and `StoreSnapshot` directly). The in-method lazy
import of `discovery._unreadable` and its `noqa: PLC0415` also die: `lookup.py` imports the error
constructor at module top.

## 3. Fact types and the RunState API

Lookup results carry facts; `RunState` applies them explicitly.

```python
# lookup.py
@dataclass(frozen=True, slots=True)
class LookupPolicy:
    current_root: str
    trust_stat: bool          # already effective: config value AND not require_verified

@dataclass(frozen=True, slots=True)
class CacheHit:
    doc: ParsedDoc | None                      # None for a cached non-node
    refreshed_stat: StatRecord | None = None   # set only by a verify-tier hit

@dataclass(frozen=True, slots=True)
class CacheMiss:
    data: bytes               # raw bytes already read, for the caller to decode and parse
    stat: os.stat_result      # captured from the same open handle as data (TOCTOU guarantee)

def resolve(entry: Entry | None, path: Path, policy: LookupPolicy) -> CacheHit | CacheMiss: ...
```

`resolve` preserves today's tier order exactly: with an entry present and `policy.trust_stat`
true, the stat tier compares `entry.stats[policy.current_root]` against a fresh `path.stat()`
(an `OSError` there raises the same `UnreadableDocError` an uncached read would) and a match is
`CacheHit(doc, refreshed_stat=None)`. Otherwise the bytes are read and hashed; a `file_sha256`
match is `CacheHit(doc, refreshed_stat=stat_record(st))`, and anything else is
`CacheMiss(data, st)`. A miss on a stale entry still hashes twice across lookup and
`make_entry`, exactly as today.

```python
# state.py
class RunState:
    @classmethod
    def begin(cls, cache: CacheFile | None, current_root: str) -> "RunState": ...
    def entry(self, rel_key: str) -> Entry | None: ...
    def claim(self, rel_key: str, refreshed_stat: StatRecord | None = None) -> None: ...
    def replace(self, rel_key: str, entry: Entry) -> None: ...
    def complete(self) -> CacheFile: ...
```

`RunState` tracks the discovered set itself: every lookup outcome is reported to it.
`claim(rel_key)` records a stat-tier hit; `claim(rel_key, refreshed_stat)` records a verify-tier
hit and inserts or refreshes the current root's stat record on the entry;
`replace(rel_key, entry)` records a miss. The parallel `discovered: set[str]` that `orchestrate`
maintains today disappears, and `complete()` needs no argument. `complete()` runs today's four
finalize steps in order (move the current root to the ledger tail, withdraw the current root's
claims on undiscovered paths, evict over-cap head roots and scrub their stat keys, drop entries
with an empty stats map) and returns the final `CacheFile` built with `CACHE_VERSION` and
`__version__`.

## 4. Lifecycle in `orchestrate._load_cached`

```python
path = cache_path(config.cache_key, os.environ)
snapshot = store.load(path)
current_root = str(project.project_root.resolve())
state = RunState.begin(snapshot.cache, current_root)
policy = LookupPolicy(current_root=current_root,
                      trust_stat=config.cache_trust_stat and not require_verified)
parsed = []
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
    state.replace(rel_key,
                  make_entry(result.data, meta, body, sections, result.stat, current_root))
    if meta is not None:
        parsed.append(ParsedDoc(path=doc_path, meta=meta, body=body, sections=sections))
lattice = build_lattice(parsed)
store.save_if_changed(path, state.complete(), snapshot.baseline)
return lattice
```

The transaction boundary is unchanged: any load error aborts before `complete()` and
`save_if_changed`, so no cache write happens. The baseline threads from `store.load` to
`store.save_if_changed` through `orchestrate`, keeping change detection out of `RunState`.
The `require_verified` flag and the `cache_key is not None` assertion keep their current form.

## 5. Behavior preservation

Bit-for-bit equivalences the implementation must hold:

- Same tier order and tier semantics; a verify-tier hit refreshes the current root's stat record
  and a stat-tier hit does not (now visible as `refreshed_stat` instead of a hidden mutation).
- The v1 JSON format is untouched. Models move verbatim, so `model_dump_json` field order and
  content are identical; a cold-then-warm run produces a byte-identical `load-cache.json`
  across the refactor.
- Write-only-if-changed compares the final `model_dump(mode="json")` against the loaded
  baseline, exactly as today; a fully warm repeat run performs one cache read and zero writes.
- Error parity: the raced stat failure and all doc read and decode failures raise the same
  `UnreadableDocError` messages; cache read failures stay silent; a cache write failure emits
  exactly one stderr line and never changes a command's result or exit code.
- No new exception types. The package stays free of `typing.Any` and `cast` (`json.loads`
  output flows straight into `model_validate` inside `store.py`), so the typing-boundary check
  passes unchanged.
- Scope of the contract: "no user-facing behavior change" means CLI stdout, stderr, exit codes,
  file mutations, and the cache file format. The Python import surface is internal (section 2);
  within it, every surviving name remains importable from `doc_lattice.cache` via the facade,
  and only `LoadCache` and `is_empty` are removed, deliberately.

## 6. Testing

Tests mirror the new modules as flat files, matching the repo's flat `tests/` directory:

- **`tests/test_cache_schema.py`**: model strictness (`extra="forbid"`), codec round-trips (the
  existing `derive_file_sections` round-trip property test moves here), `make_entry` semantics
  (stats reset to the current root only, `node=None` for non-nodes, full sha256 of raw bytes).
- **`tests/test_cache_store.py`**: XDG `cache_home` and `cache_path` handling; every corruption
  mode returning an empty snapshot; the `version` and `tool_version` gates; atomic write; the
  write-failure diagnostic (including under `PYTHONWARNINGS=error`); temp-file cleanup; and
  no-write-when-unchanged via an identical baseline.
- **`tests/test_cache_state.py`**: pure, no `tmp_path`: `begin` from `None` and from a snapshot;
  claim, replace, and discovered tracking; cross-root claim withdrawal and reclamation; LRU touch
  and eviction at `MAX_STAT_ROOTS`; scrubbing of evicted roots; dropping unclaimed entries.
- **`tests/test_cache_lookup.py`**: stat-tier hit, mismatch, and absent record; the raced-stat
  `UnreadableDocError`; verify-tier hit carrying `refreshed_stat`; miss carrying the same-handle
  stat; `trust_stat=False` never calling `path.stat()`.
- **`tests/test_cache.py` (retained, slimmed)**: mirrors `cache/__init__.py` and keeps end-to-end
  coverage for a cold current-root ledger tail, a fully warm no-write run, hypothesis
  cached-versus-uncached edit parity, a `require_verified` same-stat rewrite, the documented
  trust-stat unreadable caveat, the documented schema-valid corruption limit, and the exact
  13-name facade import contract.

Existing tests are migrated to the module that now owns the behavior, not duplicated. Coverage
stays at or above the current level (99.40 percent); the pure modules should reach 100 percent
trivially.

Implementation-phase verification: the full suite green under `env -u FORCE_COLOR`, plus a
manual before-and-after check that a cold-then-warm run produces a byte-identical
`load-cache.json` across the refactor.

## 7. Documentation

- **CLAUDE.md**: the "Pure vs impure split" paragraph replaces the single `cache` mention with
  the package breakdown: `cache/schema.py` and `cache/state.py` pure, `cache/store.py` and
  `cache/lookup.py` impure.
- **The 2026-07-10 load-cache spec is not edited** (dated historical document); this spec's
  header records the supersession of its section 8 module statement.
- **CHANGELOG.md**: one `[Unreleased]` line under `Changed` noting the internal restructure
  with no user-facing behavior change.
- **README**: no change; nothing user-visible moves.

## 8. Alternatives considered

1. **Separate `codec.py` (six modules).** Rejected: the codec is about 40 lines, changes with
   the schema, and `schema.py` imports the domain layer regardless. Splitting later is a
   mechanical move if the codec grows.
2. **`cache/orchestrate.py` owning the cached-load loop.** Rejected: it would invert the
   dependency direction (the cache importing loader and parser layers, or taking them as
   callables) and blur "orchestrate is the single wiring point". The transaction boundary stays
   visible at the one existing wiring point.
3. **A `CacheSession` facade enforcing phase order.** Rejected as an extra abstraction: only
   one call site exists (`_load_cached`), so runtime phase-order enforcement protects nothing
   the plain `RunState.begin` to `complete` surface does not already make obvious.
4. **Single-file restructure without a package.** Rejected: the pure and impure halves stay
   invisible at module level, which is how this repo documents and audits purity, and the
   mirrored per-module test layout presumes modules to mirror.
5. **Keeping `is_empty`.** Rejected: no production caller; it existed so tests could peek at
   `LoadCache` internals, which the new shapes expose directly.

## 9. Non-goals

Tier semantics, schema, or format changes; target-hash caching (still a v2 candidate behind a
version bump); a `--no-cache` flag; any change to the uncached load path; renaming config keys.
