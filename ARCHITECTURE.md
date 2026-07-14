# doc-lattice Architecture

## System Overview

doc-lattice is a deterministic, offline traceability engine for design and
production documentation. It reads markdown docs that carry lattice frontmatter and
anchored sections, derives an id-indexed edge graph on demand, and reports staleness
between an upstream source and the downstream docs that derive from it.

The engine is a pure pipeline behind the thin, impure `doc_lattice.cli` package:

    config -> discovery -> frontmatter parse -> loader.build_lattice
        -> { check, impact, reconcile, graph, lint, linear }

`cli/application.py` constructs Typer and registers the commands, `cli/runtime.py`
captures fresh invocation state, and focused adapters under `cli/commands/` connect
Typer to the engine. Shared output and error policy live in `cli/output.py` and
`cli/errors.py`. `orchestrate.load_lattice(project)` is the single wiring point that
runs the pipeline; `init` is a separate scaffolding command that never loads the
lattice. The central structure is the `Lattice` (model.py), which every
lattice-reading command reads. CLAUDE.md holds the module-by-module pure/impure
inventory and the tooling-enforced invariants; this file records the load-bearing
decisions and their rationale.

## Decision Log

### AD-1: A broken ref is a lattice state, not a load error

**Date:** 2026-06-27
**Status:** Accepted
**Context:** A `derives_from` ref can point at an id that no longer exists.
**Decision:** An unresolved ref loads cleanly as `target_id=None` and is reported by
`check` as BROKEN (exit 1, drift). Index coherence fails only when two files repeat a
file id or two headings in one file resolve to the same file-scoped anchor; either
case raises `DuplicateIdError` (exit 2). Index keys are `TargetId(file_id, anchor)`,
so equal anchors in different files do not collide, and a file id equal to another
file's anchor does not collide.
**Consequences:** Exit 1 means 'the graph is coherent but drifting' and exit 2 means
'the index is incoherent'. A single broken edge never blocks a node's reconcilable
edges.

### AD-2: Pure core, thin impure shell

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Graph and report logic must be testable against synthetic inputs.
**Decision:** All graph and report logic is filesystem-free and pure. `config`,
`discovery`, and `orchestrate` own load-path filesystem work. `persistence.py` owns
shared low-level durable staging, replace, create-if-absent, fingerprint, sync, and
cleanup primitives. `reconcile_transaction.py` owns the reconcile lock, journal,
commit, rollback, and recovery state machine. The `doc_lattice.cli` package owns the
application boundary; its `cli/commands/reconcile.py` adapter orchestrates reconcile
and reports only completed outcomes. Within the cache package, `cache/schema.py` and
`cache/state.py` are filesystem-free, `cache/store.py` owns cache-file I/O, and
`cache/lookup.py` reads and stats documents to select the verify or stat tier.
`linear_fetch` is impure wiring and `linear_client` is the only module that touches
the network.
**Consequences:** Every command's logic is unit-tested with no I/O; the network slice
is quarantined to one module.

### AD-3: Per-invocation CLI package boundaries and output compatibility

**Date:** 2026-07-14
**Status:** Accepted
**Context:** The command-line application must isolate repeated invocations while
preserving the installed `doc_lattice.cli:main` entry point, the importable `app`
compatibility surface, exact script output, and the established 1.x output flags.
Command wiring also needs named ownership boundaries without moving durable reconcile
mutation into the CLI.
**Decision:** `doc_lattice.cli` is a package. `cli/application.py` constructs and
registers Typer; `cli/runtime.py` creates a frozen runtime for each invocation with
stdout, stderr, cwd, and config and lattice loaders; `cli/output.py` centralizes format
validation, the JSON alias, indentation, exact output, and GitHub annotations; and
`cli/errors.py` centralizes `ProjectError` and internal diagnostics. The seven modules
under `cli/commands/` are narrow command adapters. There are no mutable module-level
consoles and no mutations of Typer color globals.

`cli/commands/reconcile.py` continues to own selection, destination containment,
lock, recovery, load, plan, commit, and report orchestration. Durable mutation remains
in `reconcile_transaction.py`. `cli/__init__.py` preserves `doc_lattice.cli:main` and
loads the compatibility `app` export lazily.

For the entire 1.x series, `--json` remains a silent compatibility alias with no
deprecation diagnostic on stderr. The next breaking release, 2.0, removes `--json`
and uses `--format` consistently on output-producing commands. Format values remain
command-specific: reports use `human|json|github`, while graph output uses
`mermaid|dot|json`. In 1.x, commands documented only with `--json` do not gain an
undocumented `--format` flag. `--indent` is valid only when the effective format is
JSON.
**Consequences:** Invocation state and diagnostics can be tested without shared
console state. Tests under `tests/cli/` mirror the command adapters and add focused
runtime, output, and cross-command contract coverage. Existing 1.x scripts retain
byte-exact output and quiet stderr until the documented 2.0 flag convergence.

### AD-4: Untyped-to-typed boundary policy

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Raw YAML and Linear JSON arrive untyped.
**Decision:** `typing.Any`/`typing.cast` are allowed only in boundary modules
(`scripts/check_typing_boundaries.py`); the real boundaries are `frontmatter_parser`
and `linear_parser`, which validate into typed models. Everywhere else passes typed
values.
**Consequences:** Untyped data cannot leak past two named files; CI enforces it.

### AD-5: Canonicalized, truncated content hash

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Drift must be insensitive to cosmetic edits.
**Decision:** Each edge stores a `seen` hash; the live hash is
`sha256(canonicalize(text))` truncated to 32 hex chars (128 bits), where
`canonicalize` normalizes line endings, strips trailing whitespace per line, and
trims leading and trailing blank lines. It preserves internal line breaks and blank
lines.
**Consequences:** Paragraph reflow changes the hash; normalized line endings,
trailing whitespace, and leading or trailing blank lines do not. 128 bits is ample
for a human-scale corpus.

### AD-6: Reconcile is a durable whole-batch transaction

**Date:** 2026-07-13
**Status:** Accepted
**Context:** Reconcile is the only command that mutates tracked documents. It must
reject edits made after validation, prevent concurrent reconciles from interfering,
and leave an interrupted multi-file batch recoverable.
**Decision:** Every reconcile mode takes a nonblocking advisory lock on the existing
project-root directory through preflight, planning, and any recovery or commit. A real
run re-reads downstream files, retains exact before and after bytes, and stages synced
before-image and after-image files beside each destination. Before mutation, it durably
publishes the `prepared` journal at `.doc-lattice-reconcile.json`. Immediately before
each atomic replacement, it compares the destination's full SHA-256 fingerprint with
the validated before bytes;
a mismatch is a conflict. Replacement and namespace changes include file and parent
directory synchronization. Any pre-commit conflict or persistence failure rolls back
transaction-owned after images in reverse order while preserving unrelated edits. At
the point of no return (PONR), all destinations are durable and the journal is durably
marked `committed`; recovery then preserves those destinations and only cleans staged
evidence. Success output is emitted only after committed cleanup and clean lock
release.

Normal real startup recovers a valid outstanding journal before lattice loading.
`reconcile --recover` performs only that recovery, while dry-run never recovers or
persists anything and refuses an outstanding journal. Invalid or unauthenticated
recovery evidence is retained for explicit manual remediation rather than guessed at
or deleted. Journal paths and artifacts are project-relative, contained, role-checked,
and fingerprint-authenticated before recovery mutates them.

**Consequences:** A successful reconcile is a durable all-or-nothing batch from the
operator's perspective. A `prepared` journal rolls transaction-owned changes back; a
`committed` journal records that PONR has passed and makes recovery cleanup-only. The
lock serializes doc-lattice reconcile processes but does not coordinate unrelated
editors. This contract assumes local-filesystem `flock`, atomic rename, and directory
sync behavior; network filesystems are outside it.

Shared write semantics stop at the primitive boundary. Cache persistence uses the
same durable atomic-replace primitive but remains a disposable, best-effort
single-file write whose `OSError` is reported once and swallowed. `init` uses durable
create-if-absent, still refuses to replace an existing config, and never joins a
reconcile journal. It always prints transaction-artifact `.gitignore` guidance but
does not modify `.gitignore`.

### AD-7: lint is a pure structural check, separate from drift

**Date:** 2026-06-28
**Status:** Accepted
**Context:** Authority inversion (a more-authoritative doc deriving from a less
authoritative one) is a structural error, not staleness.
**Decision:** `lint` ranks `derives_from` edges on the binding > derived > exploratory
ladder, flags inversions, reports edges it cannot rank, never mutates, and exits 1 on
a violation (mirroring `check`).
**Consequences:** Structural validity and drift are independent gates.

### AD-8: Tag-gated PyPI distribution

**Date:** 2026-07-12
**Status:** Accepted
**Context:** Releases publish wheels and source distributions to PyPI, with the tag as
the immutable source identity and no stored PyPI credential.
**Decision:** A merge-triggered `release` job validates or creates the `vX.Y.Z` tag.
The dependent, unprivileged `build-release` job checks out that exact tag, builds and
validates the distributions, and transfers them as an artifact. The OIDC-only
`publish` job downloads and publishes that artifact without checking out repository
code.
**Consequences:** Build input is tied to the validated tag, while the credentialed
publisher executes neither repository code nor package build code. See RELEASING.md.

### AD-9: Symlink targets and document identity

**Date:** 2026-07-13
**Status:** Accepted
**Context:** A discovered markdown path may be a symlink, and multiple configured
roots or aliases may reach the same physical document.
**Decision:** Discovery resolves each candidate against the project root for
containment and deduplication, but retains the first unresolved path as the document's
identity. Project-internal targets are allowed; external targets are skipped with a
warning. Before reconcile writes, the `cli/commands/reconcile.py` adapter re-resolves
the document identity path and requires the current destination to remain inside the
project root.
**Consequences:** Internal symlink paths remain stable in reports and cache keys,
aliases load a resolved document only once, external content is never read, and a
symlink retargeted after load cannot redirect a reconcile write outside the project.
