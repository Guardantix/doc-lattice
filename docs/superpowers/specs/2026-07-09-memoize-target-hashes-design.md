# Per-Run Target-Hash Memoization: Design Spec

**Date:** 2026-07-09
**Status:** Approved for implementation.
**Issue:** GitHub issue #25, `perf: memoize target content hashes within check and reconcile runs`.

## Goal

Avoid recomputing identical target-content hashes within one `check` or `reconcile` run while
preserving all command output, error behavior, purity, and cache lifetime semantics.

## Architecture

`resolve.py` owns the pure `target_content` operation, so it will expose
`cached_target_hash(lattice, target_id, cache)`. The helper checks a caller-supplied
`dict[TargetId, str]`; on a miss it calls `content_hash(target_content(lattice, target_id))`, stores
the result, and returns it. The cache is intentionally neither global nor retained between command
runs. A future cache of split body lines per path could reduce section extraction work further, but
is explicitly out of scope.

`check_lattice` constructs one cache for its full edge traversal and passes it into `_classify`.
`reconcile` constructs one cache before iterating selected nodes and reuses it for every resolvable
edge. Broken-edge handling remains before the helper call, and all downstream comparison and plan
logic remains unchanged.

## Alternatives Considered

1. Shared helper in `resolve.py`: selected. It centralizes the cache invariant and gives a direct
   unit-test seam without introducing state.
2. Separate local cache implementations in `check.py` and `reconcile.py`: rejected because it
   duplicates lookup behavior and has no direct shared test.
3. Module-global cache: rejected because document content can change between invocations and the
   issue requires caller-owned cache lifetime.

## Testing

Tests patch `game_lattice.resolve.content_hash`, the lookup location used by the helper, with a
counting wrapper. The helper unit test proves one calculation per target per cache. The `check` and
`reconcile_all=True` tests each create at least three edges sharing one target and assert exactly
one invocation per distinct target. Existing test cases verify unchanged user-visible behavior; the
full suite confirms byte-identical assertions remain intact.

## Documentation

Add one `Changed` entry under `CHANGELOG.md`'s `[Unreleased]` heading. This is an internal
performance improvement and does not describe a user-visible behavioral change.
