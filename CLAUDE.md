# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

doc-lattice is a deterministic, offline traceability engine for design and production docs.
It parses lattice frontmatter and anchored sections from a markdown doc set, derives an id-indexed
edge graph, and reports staleness between upstream and downstream documents.

When documentation sources conflict, current code, current supported documentation, later accepted
decisions, and changelog migrations supersede historical design and implementation documents.
Files under `docs/superpowers/specs/` and `docs/superpowers/plans/` preserve historical context;
they are not the current source of truth.

## Commands

Dependency management and execution go through `uv` (Python 3.13+).

```bash
uv sync --group dev                       # install (incl. dev deps)
uv run doc-lattice --help                 # run the CLI (commands: check, impact, reconcile, graph, linear, init, lint)

uv run --group dev pytest                 # full suite (enforces coverage >= 80%)
uv run --group dev pytest tests/test_loader.py::test_duplicate_id_raises   # a single test
uv run --group dev pytest tests/test_check.py -v                           # one file, verbose

uv run --group dev ruff check src tests   # lint
uv run --group dev ruff format src tests  # format (add --check to verify only)
uv run --group dev ty check src           # type check
uv run --group dev python scripts/check_typing_boundaries.py src           # boundary rule check
uv run --group dev python scripts/check_version_sync.py    # version-consistency guard
uv run --group dev python scripts/generate_github_slugger_data.py --check  # exhaustive slug parity
uv run --group dev python scripts/bench_sections.py        # large-document section benchmark
```

A pre-commit hook runs ruff (with `--fix`), ruff-format, `ty`, the typing-boundary check, the version-sync check, and detect-secrets on every commit, and blocks direct commits to `main`.
A commit that fails any of these is rejected, so code must be lint/type/boundary clean before it
lands; if a hook auto-fixes a file, re-stage and re-commit.

## Architecture

The engine is a pure pipeline feeding six lattice-reading commands; `linear` adds a network
fetch of ticket status on top of the loaded lattice, and `init` is a separate scaffolding command
that writes `.doc-lattice.yml` without loading the lattice at all:

```
config -> discovery -> frontmatter parse -> loader.build_lattice -> { check, impact, reconcile, graph, lint, linear }
```

`orchestrate.load_lattice(project)` is the single wiring point that runs that pipeline.
Real reconcile is the exception to the displayed startup order: after config and project-directory
lock setup, it recovers an outstanding transaction before calling `load_lattice`, so planning uses
the recovered bytes. Recovery-only mode never loads the lattice, and dry-run refuses an outstanding
journal without changing files, namespaces, or the load cache.

**The `Lattice` (model.py) is the central structure** and every command reads from it:

- `nodes_by_id`: each tracked file as a `Node` (frontmatter + verbatim body).
- `index`: every `TargetId` mapped to a `Location`. A file target is `TargetId(file_id)`; a
  section target is `TargetId(file_id, anchor)`. Section ids are file-scoped, so the same anchor
  in two files does not collide; a within-file clash is a `DuplicateIdError` (a load failure, exit 2).
- `dependents`: reverse adjacency (target id -> source ids that derive from it), built from
  resolved edges only.
- `ancestors`: for a section anchor, the enclosing anchored sections (outermost to innermost), so
  editing a nested sub-section propagates impact to dependents of its parent.

**Refs and edge state.** A `derives_from` ref resolves file-scoped through `model.parse_ref`:
`save-format#slot-table` parses to `TargetId("save-format", "slot-table")` and resolves against
that file's headings, while a bare ref (`save-format`) is a whole-file target. A heading is
addressed by an explicit `{#marker}` when present, otherwise by its GitHub slug (byte-parity with
GitHub's rendered anchor; see `markdown_compat.github_slug`/`anchor_ids`). Heading and fence
recognition is owned by `markdown_compat.py` against `markdown-it-py==4.2.0`; slug data in
`_github_slugger_data.py` is generated from `github-slugger@2.0.0` under JavaScript Unicode 17.0
and must not be hand-edited. It also patches the minimum Python 3.13 Unicode 15.1 lowercase table.
Regenerate and verify it through `scripts/generate_github_slugger_data.py`; maintenance requires a
Unicode 17 Node runtime and `python3.13`. A ref that resolves to nothing is not a load error: it is a
normal lattice state (`target_id=None`) that `check` reports as
`BROKEN`. The same slug in two different files does not collide, because their `TargetId`s differ;
a within-file clash (two equal markers or a marker equal to a computed slug) is a
`DuplicateIdError`. A file id equal to an anchor in another file also does not collide.

Before declaring support for a Python runtime whose `unicodedata.unidata_version` exceeds the
pinned JavaScript Unicode target, update that target, regenerate the artifact, and rerun the slug
compatibility and benchmark gates.

**Drift detection.** Each edge carries a `seen` hash captured when it was last reconciled.
`check` classifies every edge as OK / STALE / UNRECONCILED / BROKEN by comparing `seen` against
`content_hash(target_content(...))`, where the hash is `sha256(canonicalize(text))` truncated to 32
hex chars (128 bits). Canonicalization normalizes line endings, strips trailing whitespace per line,
and trims leading and trailing blank lines, while preserving internal line breaks and blank lines;
paragraph reflow therefore changes the hash. `impact` reverse-walks `dependents` transitively (with
ancestor and enclosing-file expansion) to list everything a change touches.

`lint` is a pure structural check separate from drift: it flags a `derives_from` edge whose source
is more authoritative than its target (binding > derived > exploratory), reports edges it cannot rank
because an endpoint lacks `authority`, and never mutates. It exits 1 on a violation, mirroring `check`.
Historical spec: `docs/superpowers/specs/2026-06-28-doc-lattice-lint-design.md`.

**Reconcile is the only command that mutates tracked documents.** It plans new `seen` values from
the loaded snapshot, then re-reads each downstream file and retains exact before and replacement
bytes while rewriting only targeted `seen` scalars through round-trip YAML. The reconcile adapter
resolves identity paths against the project root before fresh reads and orchestrates lock
acquisition and lifetime across recovery, load, plan, fresh reads, and the commit call. It delays
final outcome and success reporting until clean lock release; an automatic-recovery notice may be
emitted on stderr while the lock is held. The adapter does not own direct per-file atomic writes.
`reconcile_transaction.py` independently contains live commit destinations before staging, stages
and syncs exact before/after images, publishes the `prepared` journal, fingerprints each destination
immediately before replacement, and rolls the batch back in reverse on conflict or pre-commit
persistence failure. Once every destination is durable, it marks the journal `committed` before
cleanup. Human or JSON success is reported only after transaction cleanup and clean lock release.
`--recover` is recovery-only; normal real runs recover before lattice load; dry-run stays byte-,
namespace-, and cache-read-only and refuses an outstanding journal. `--ref` selects on
resolved identity; `--all` clears only STALE/UNRECONCILED edges (it skips
BROKEN and already-OK), and `--all --ref REF` narrows that selection across every downstream node
without treating no matches as an error. A node's BROKEN edge is skipped, not fatal, so it never
blocks the node's reconcilable edges; only a single-node `--ref` aimed directly at a broken edge is
refused.

**The `linear` slice is the only network-touching command.** It builds a trigger map from the
loaded lattice, then fetches live ticket status over GraphQL: `linear_query` constructs the batched
`issues(filter:)` document (pure), `linear_fetch` drives the request, `linear_client` performs the
POST (the only module that reads `LINEAR_API_KEY`, lazily; https-only, redirect-refusing,
size-capped, SSRF-hardened), `linear_parser` validates the JSON envelope into typed `Ticket`s (a
boundary module), and `stale_shipped` joins lattice plus tickets into graded `Finding`s (pure),
rendered by `linear_render`. A ticket the filter does not return is absence, not an error, and
grades as a BLOCKED `not-found`. Ticket ids are uppercase ASCII `TEAM-NUMBER`, with `0` or a decimal
without leading zeros, and one run accepts at most 500 distinct refs. A transient HTTP 429 or 5xx
gets two retries (three total attempts), using 1- and 2-second fallback delays or a capped
non-negative integer `Retry-After`. Historical spec:
`docs/superpowers/specs/2026-06-27-doc-lattice-linear-design.md`.

**The `init` slice scaffolds an adopting repo and never loads the lattice.** `scaffold.py` is pure
(`render_config`/`render_gitignore`/`render_precommit`/`render_ci`/`build_scaffold` return text);
`persistence.atomic_create_bytes` durably publishes `.doc-lattice.yml` only if absent. `init` always
prints `.gitignore`, pre-commit, and CI guidance pinned to Python 3.13 and the exact PyPI requirement
`doc-lattice==X.Y.Z`. The printed ignore block covers the reconcile journal, journal stages, and
before/after images; `init` never reads or modifies `.gitignore`. Git commit sources are only
fallbacks for testing unreleased code. Historical spec:
`docs/superpowers/specs/2026-06-28-doc-lattice-init-design.md`.

**Pure vs impure split.** All graph and report logic is pure and filesystem-free: `model`,
`hashing`, `markdown_compat`, `sections`, `resolve`, `loader`, `check`, `lint`, `impact`, `render`,
`reconcile.reconcile`/
`apply_reconcile`/`plan_rewrites` (which return planned updates or rewritten text rather than
writing it), the shared `report_render`, plus the linear pure core (`tickets`, `linear_query`,
`stale_shipped`, `linear_render`), `scaffold`, and the release version-consistency core
`version_check`, plus `cache/schema.py` (models and codec) and `cache/state.py` (run-local
`RunState`). The untyped-to-typed boundary modules are `frontmatter_parser` and `linear_parser`.
Load-path filesystem I/O is owned by `config`, `discovery`, and `orchestrate`. `persistence.py` owns
the shared durable staging, atomic replace, create-if-absent, fingerprint, directory sync, and
cleanup primitives. `reconcile_transaction.py` is the impure owner of the reconcile lock capability
and mechanics, independent live commit destination preflight, journal prepare/commit, rollback,
recovery containment and validation, and artifact cleanup. The `doc_lattice.cli` package owns the
impure application boundary: `cli/application.py` constructs Typer and registers all seven
commands; `cli/runtime.py` creates a frozen per-invocation runtime containing stdout, stderr, cwd,
and the config and lattice loaders; and `cli/output.py` owns output selection, JSON indentation,
exact writes, and GitHub annotations. `cli/errors.py` owns diagnostic rendering, exit constants,
and command-level `ProjectError` context conversion. `cli/__init__.py` owns entry-point exception
mapping for `ProjectError` and supported unexpected errors, converts them to exit 2, and preserves
intended `SystemExit` values.
Each `cli/commands/*.py` module is a narrow adapter. `cli/commands/reconcile.py` owns selection,
document identity resolution before fresh reads, lock acquisition and lifetime, recovery, load,
plan, the transaction commit call, and delayed report orchestration. Durable mutation remains in
`reconcile_transaction.py`; `cli/commands/init.py` delegates durable creation to `persistence.py`.
`cli/__init__.py` also preserves the `doc_lattice.cli:main` entry point and lazy `app`
compatibility. Do not add mutable module-level consoles or mutate Typer color globals.
The current `path_utils.safe_resolve()` consumers are `config`, `discovery`,
`cli/commands/reconcile.py` before fresh reconcile reads, and `reconcile_transaction.py` for both
independent live commit destination preflight and journal recovery paths. The helper is
filesystem-aware path resolution: it calls `Path.resolve()`, which follows symlinks.
The cache package splits by phase: `cache/schema.py` (models and codec) and `cache/state.py`
(run-local `RunState`) are pure in this architecture's I/O-boundary sense; `RunState` is mutable,
deterministic, and filesystem-free. In contrast, `cache/store.py` (reads and atomically writes the
opt-in load cache under the user cache home) and `cache/lookup.py` (doc reads and stats for tier
selection) are impure. `doc_lattice.cache` is an internal convenience facade, not a supported
external Python compatibility promise; keep production lifecycle wiring through the phase owners
in `orchestrate._load_cached`. `linear_fetch` is impure wiring and `linear_client` is the only
module that touches the network. This is what lets the graph and report layers be tested against
synthetic inputs with no I/O.

## Project-specific invariants

These are enforced by tooling (pre-commit, `scripts/`, or `tests/test_conventions.py`), so a
violation fails CI rather than just being a style preference:

- **Untyped-to-typed boundary.** `typing.Any` and `typing.cast` are allowed only in boundary
  modules, defined by `scripts/check_typing_boundaries.py` as a file whose stem is one of `boundary`,
  `adapter`, `parser`, `validator`, `external`, or `inbound`, or ends with one of those words
  prefixed by `_` (for example `frontmatter_parser`) (or sits under a directory of that
  name). The real engine boundaries are `frontmatter_parser.py` (raw YAML to `NodeMeta`) and
  `linear_parser.py` (Linear JSON to `Ticket`). In `path_utils.py`, only `safe_resolve` is wired in
  (by `config`, `discovery`, `cli/commands/reconcile.py`, and `reconcile_transaction.py`).
  Everywhere else, convert at the boundary and pass typed models.
- **Errors.** All custom exceptions extend `ProjectError` (error_types.py) and carry a `code`; no
  bare `except Exception`/`except BaseException`. Messages name the file and the fix.
- **Constants.** Use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py` and import
  them; do not write raw string literals that duplicate a constant value.
- **Paths and containment.** User-provided paths go through `safe_resolve()` (path_utils.py).
  `config` rejects docs roots outside the project root. `discovery` allows project-internal
  document symlinks, skips external targets with a warning, and deduplicates resolved aliases while
  retaining the first unresolved path as document identity. Before reconcile writes,
  `cli/commands/reconcile.py` resolves each identity path and rejects a destination outside the
  project root before fresh reads. The transaction's `_preflight_rewrite_destinations` independently
  applies `safe_resolve` to every supplied live commit destination before staging. Transaction
  journals store only project-relative paths; recovery separately applies `safe_resolve` and
  revalidates containment, role-specific artifact names and locations, regular-file type, and
  recorded fingerprints before mutation.
- **Node ids.** A frontmatter `id` may not contain `#`; `#` separates a file id from a section
  anchor in a ref. Enforced by a `NodeMeta` field validator (exit 2, names the id).
- **No `datetime.now()`/`utcnow()` outside `datetime_utils.py`.**
- **Version sync.** `__version__` (`src/doc_lattice/__init__.py`), the `pyproject.toml` `version`,
  the first versioned `## [X.Y.Z]` `CHANGELOG.md` heading, and every exact README pin in either
  `doc-lattice==X.Y.Z` or `doc-lattice@vX.Y.Z` form must agree (a `## [Unreleased]` block above the
  versioned changelog heading is tolerated and skipped, so notes can accumulate there between
  releases). The pure core is `version_check.py`;
  `scripts/check_version_sync.py` wraps it and runs in pre-commit and CI. On merge to `main` the
  `release` job in `.github/workflows/ci.yml` verifies sync, smoke-tests the commit, creates the
  lightweight `vX.Y.Z` tag, and publishes its GitHub Release. The unprivileged `build-release` job
  checks out that exact tag, builds and validates the distributions with Twine, and uploads the
  artifact; the OIDC-only `publish` job downloads that artifact and publishes it to PyPI. Release
  flow: `RELEASING.md`.
- ruff line length 100; module docstring on every module; Google-style docstrings on public
  functions; no em-dashes in any drafted content (docstrings, messages, comments).

## Testing notes

Test files mirror sources (`src/doc_lattice/foo.py` -> `tests/test_foo.py`) and use `tmp_path` for
filesystem work. CLI tests live under `tests/cli/`: command test modules mirror the seven adapters,
focused modules cover runtime and output behavior, and the concise `test_contract.py` suite owns
cross-command entry behavior.
`tests/conftest.py` provides the shared `lattice_dir` fixture: a small synthetic doc set
(art-direction with STALE/UNRECONCILED downstream edges, plus a BROKEN ref) that the check,
reconcile, and CLI tests build on, so its contents are load-bearing across those suites.
Property-based invariants (hashing canonicalization) use hypothesis.
