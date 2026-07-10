# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

game-lattice is a deterministic, offline traceability engine for game design and production docs.
It parses lattice frontmatter and anchored sections from a markdown doc set, derives an id-indexed
edge graph, and reports staleness between upstream and downstream documents.
The binding design is `docs/superpowers/specs/2026-06-27-game-lattice-local-core-design.md`; treat
it as the source of truth when code and intent disagree.

## Commands

Dependency management and execution go through `uv` (Python 3.13+).

```bash
uv sync --group dev                       # install (incl. dev deps)
uv run game-lattice --help                # run the CLI (commands: check, impact, reconcile, graph, linear, init, lint)

uv run --group dev pytest                 # full suite (enforces coverage >= 80%)
uv run --group dev pytest tests/test_loader.py::test_duplicate_id_raises   # a single test
uv run --group dev pytest tests/test_check.py -v                           # one file, verbose

uv run --group dev ruff check src tests   # lint
uv run --group dev ruff format src tests  # format (add --check to verify only)
uv run --group dev ty check src           # type check
uv run --group dev python scripts/check_typing_boundaries.py src           # boundary rule check
uv run --group dev python scripts/check_version_sync.py    # version-consistency guard
```

A pre-commit hook runs ruff (with `--fix`), ruff-format, `ty`, the typing-boundary check, the version-sync check, and detect-secrets on every commit, and blocks direct commits to `main`.
A commit that fails any of these is rejected, so code must be lint/type/boundary clean before it
lands; if a hook auto-fixes a file, re-stage and re-commit.

## Architecture

The engine is a pure pipeline feeding six lattice-reading commands; `linear` adds a network
fetch of ticket status on top of the loaded lattice, and `init` is a separate scaffolding command
that writes `.game-lattice.yml` without loading the lattice at all:

```
config -> discovery -> frontmatter parse -> loader.build_lattice -> { check, impact, reconcile, graph, lint, linear }
```

`orchestrate.load_lattice(project)` is the single wiring point that runs that pipeline.

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
GitHub's rendered anchor; see `sections.github_slug`/`anchor_ids`). A ref that resolves to nothing
is not a load error: it is a normal lattice state (`target_id=None`) that `check` reports as
`BROKEN`. The same slug in two different files does not collide, because their `TargetId`s differ;
a within-file clash (a marker equal to a computed slug) is a `DuplicateIdError`.

**Drift detection.** Each edge carries a `seen` hash captured when it was last reconciled.
`check` classifies every edge as OK / STALE / UNRECONCILED / BROKEN by comparing `seen` against
`content_hash(target_content(...))`, where the hash is `sha256(canonicalize(text))` truncated to 32
hex chars (128 bits) and `canonicalize` strips cosmetic differences (line endings, trailing
whitespace, edge blank lines). `impact` reverse-walks `dependents` transitively (with ancestor
and enclosing-file expansion) to list everything a change touches.

`lint` is a pure structural check separate from drift: it flags a `derives_from` edge whose source
is more authoritative than its target (binding > derived > exploratory), reports edges it cannot rank
because an endpoint lacks `authority`, and never mutates. It exits 1 on a violation, mirroring `check`.
Spec: `docs/superpowers/specs/2026-06-28-game-lattice-lint-design.md`.

**Reconcile is the only mutating command.** It plans new `seen` values from the loaded snapshot,
then at write time **re-reads each downstream file fresh**, rewrites only the targeted `seen`
scalar(s) through round-trip YAML (preserving body, key order, comments, and any concurrent edit),
and writes atomically. `--ref` selects on resolved identity; `--all` clears only STALE/UNRECONCILED
edges (it skips BROKEN and already-OK). A node's BROKEN edge is skipped, not fatal, so it never
blocks the node's reconcilable edges; only a `--ref` aimed directly at a broken edge is refused.

**The `linear` slice is the only network-touching command.** It builds a trigger map from the
loaded lattice, then fetches live ticket status over GraphQL: `linear_query` constructs the batched
`issues(filter:)` document (pure), `linear_fetch` drives the request, `linear_client` performs the
POST (the only module that reads `LINEAR_API_KEY`, lazily; https-only, redirect-refusing,
size-capped, SSRF-hardened), `linear_parser` validates the JSON envelope into typed `Ticket`s (a
boundary module), and `stale_shipped` joins lattice plus tickets into graded `Finding`s (pure),
rendered by `linear_render`. A ticket the filter does not return is absence, not an error, and
grades as a BLOCKED `not-found`. Spec: `docs/superpowers/specs/2026-06-27-game-lattice-linear-design.md`.

**The `init` slice scaffolds an adopting repo and never loads the lattice.** `scaffold.py` is pure
(`render_config`/`render_precommit`/`render_ci`/`build_scaffold` return text); `cli._atomic_create`
publishes each file via temp -> fsync -> `os.link`, so a partial write never lands. `init` writes
`.game-lattice.yml` only if absent and always prints pre-commit and CI codegen pinned to the current
release tag. Spec: `docs/superpowers/specs/2026-06-28-game-lattice-init-design.md`.

**Pure vs impure split.** All graph and report logic is pure and filesystem-free: `model`,
`hashing`, `sections`, `resolve`, `loader`, `check`, `lint`, `impact`, `render`, `reconcile.reconcile`/
`apply_reconcile`/`plan_rewrites` (which return planned updates or rewritten text rather than
writing it), plus the linear pure core (`tickets`, `linear_query`, `stale_shipped`,
`linear_render`), `scaffold`, and the release version-consistency core `version_check`. The
untyped-to-typed
boundary modules are `frontmatter_parser` and `linear_parser`. Only `config`, `discovery`,
`orchestrate`, and `cli` touch the disk (`cli` performs the reconcile and init writes); `linear_fetch`
is impure wiring and `linear_client` is the only module that touches the network. This is what lets
the graph and report layers be tested against synthetic inputs with no I/O.

## Project-specific invariants

These are enforced by tooling (pre-commit, `scripts/`, or `tests/test_conventions.py`), so a
violation fails CI rather than just being a style preference:

- **Untyped-to-typed boundary.** `typing.Any` and `typing.cast` are allowed only in boundary
  modules, defined by `scripts/check_typing_boundaries.py` as a file whose stem is one of `boundary`,
  `adapter`, `parser`, `validator`, `external`, or `inbound`, or ends with one of those words
  prefixed by `_` (for example `frontmatter_parser`) (or sits under a directory of that
  name). The real engine boundaries are `frontmatter_parser.py` (raw YAML to `NodeMeta`) and
  `linear_parser.py` (Linear JSON to `Ticket`). In `path_utils.py`, only `safe_resolve` is wired in
  (by `config` and `discovery`).
  Everywhere else, convert at the boundary and pass typed models.
- **Errors.** All custom exceptions extend `ProjectError` (error_types.py) and carry a `code`; no
  bare `except Exception`/`except BaseException`. Messages name the file and the fix.
- **Constants.** Use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py` and import
  them; do not write raw string literals that duplicate a constant value.
- **Paths and containment.** User-provided paths go through `safe_resolve()` (path_utils.py). Both
  `config` (docs roots) and `discovery` (each discovered file) reject any path that escapes the
  project root via `..`, an absolute path, or a symlink, before any read or write.
- **Node ids.** A frontmatter `id` may not contain `#`; `#` separates a file id from a section
  anchor in a ref. Enforced by a `NodeMeta` field validator (exit 2, names the id).
- **No `datetime.now()`/`utcnow()` outside `datetime_utils.py`.**
- **Version sync.** `__version__` (`src/game_lattice/__init__.py`), the `pyproject.toml` `version`,
  and the first versioned `## [X.Y.Z]` `CHANGELOG.md` heading must agree (a `## [Unreleased]` block above it is tolerated and skipped, so notes can accumulate there between releases). The pure core is `version_check.py`;
  `scripts/check_version_sync.py` wraps it and runs in pre-commit and CI. On merge to `main` the
  `release` job in `.github/workflows/ci.yml` verifies sync, smoke-tests the commit, and cuts the
  lightweight `vX.Y.Z` tag. Release flow: `RELEASING.md`.
- ruff line length 100; module docstring on every module; Google-style docstrings on public
  functions; no em-dashes in any drafted content (docstrings, messages, comments).

## Testing notes

Test files mirror sources (`src/game_lattice/foo.py` -> `tests/test_foo.py`) and use `tmp_path` for
filesystem work.
`tests/conftest.py` provides the shared `lattice_dir` fixture: a small synthetic doc set
(art-direction with STALE/UNRECONCILED downstream edges, plus a BROKEN ref) that the check,
reconcile, and CLI tests build on, so its contents are load-bearing across those suites.
Property-based invariants (hashing canonicalization) use hypothesis.
