# game-lattice Architecture

## System Overview

game-lattice is a deterministic, offline traceability engine for game design and
production documentation. It reads markdown docs that carry lattice frontmatter and
anchored sections, derives an id-indexed edge graph on demand, and reports staleness
between an upstream source and the downstream docs that derive from it.

The engine is a pure pipeline behind a thin impure shell:

    config -> discovery -> frontmatter parse -> loader.build_lattice
        -> { check, impact, reconcile, graph, lint, linear }

`orchestrate.load_lattice(project)` is the single wiring point that runs the
pipeline; `init` is a separate scaffolding command that never loads the lattice. The
central structure is the `Lattice` (model.py), which every command reads. CLAUDE.md
holds the module-by-module pure/impure inventory and the tooling-enforced invariants;
this file records the load-bearing decisions and their rationale.

## Decision Log

### AD-1: A broken ref is a lattice state, not a load error

**Date:** 2026-06-27
**Status:** Accepted
**Context:** A `derives_from` ref can point at an id that no longer exists.
**Decision:** An unresolved ref loads cleanly as `target_id=None` and is reported by
`check` as BROKEN (exit 1, drift). The only structural load failure is a duplicate id
in the flat file/anchor namespace, which raises `DuplicateIdError` (exit 2).
**Consequences:** Exit 1 means 'the graph is coherent but drifting' and exit 2 means
'the index is incoherent'. A single broken edge never blocks a node's reconcilable
edges.

### AD-2: Pure core, thin impure shell

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Graph and report logic must be testable against synthetic inputs.
**Decision:** All graph and report logic is filesystem-free and pure. Only `config`,
`discovery`, `orchestrate`, and `cli` touch the disk; `linear_fetch` is impure wiring
and `linear_client` is the only module that touches the network.
**Consequences:** Every command's logic is unit-tested with no I/O; the network slice
is quarantined to one module.

### AD-3: Untyped-to-typed boundary policy

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Raw YAML and Linear JSON arrive untyped.
**Decision:** `typing.Any`/`typing.cast` are allowed only in boundary modules
(`scripts/check_typing_boundaries.py`); the real boundaries are `frontmatter_parser`
and `linear_parser`, which validate into typed models. Everywhere else passes typed
values.
**Consequences:** Untyped data cannot leak past two named files; CI enforces it.

### AD-4: Canonicalized, truncated content hash

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Drift must be insensitive to cosmetic edits.
**Decision:** Each edge stores a `seen` hash; the live hash is
`sha256(canonicalize(text))` truncated to 32 hex chars (128 bits), where
`canonicalize` strips line endings, trailing whitespace, and edge blank lines.
**Consequences:** Reflowing whitespace does not trip drift; 128 bits is ample for a
human-scale corpus.

### AD-5: Reconcile re-reads fresh and rewrites one scalar atomically

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Reconcile is the only mutating command and must not clobber edits.
**Decision:** At write time reconcile re-reads each downstream file fresh, rewrites
only the targeted `seen` scalar(s) through round-trip YAML (preserving body, key
order, and comments), and writes atomically; all rewrites are computed before any
file is written, so a malformed concurrent edit aborts the whole command.
**Consequences:** Concurrent edits survive; there is no cross-file half-reconcile.

### AD-6: lint is a pure structural check, separate from drift

**Date:** 2026-06-28
**Status:** Accepted
**Context:** Authority inversion (a more-authoritative doc deriving from a less
authoritative one) is a structural error, not staleness.
**Decision:** `lint` ranks `derives_from` edges on the binding > derived > exploratory
ladder, flags inversions, reports edges it cannot rank, never mutates, and exits 1 on
a violation (mirroring `check`).
**Consequences:** Structural validity and drift are independent gates.

### AD-7: Distribution from git with a merge-triggered tag

**Date:** 2026-06-29
**Status:** Accepted
**Context:** The tool ships over `uvx --from git+...@vX.Y.Z`, so the tag must resolve.
**Decision:** The version bump is the human step; a version-sync guard fails any PR
whose `__version__`, `pyproject.toml`, and first versioned `CHANGELOG.md` heading
disagree, and a merge-triggered CI `release` job smoke-tests the commit and cuts the
lightweight `vX.Y.Z` tag.
**Consequences:** A half-done release (code without a tag, or a tag without the bump)
cannot land. See RELEASING.md.
