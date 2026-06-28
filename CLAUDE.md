# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

game-lattice is a deterministic, offline traceability engine for game design and production docs.
It parses lattice frontmatter and anchored sections from a markdown doc set, derives an id-indexed
edge graph, and reports staleness between upstream and downstream documents.
The binding design is `docs/superpowers/specs/2026-06-27-game-lattice-local-core-design.md`; treat
it as the source of truth when code and intent disagree.

## Commands

Dependency management and execution go through `uv` (Python 3.14+).

```bash
uv sync --group dev                       # install (incl. dev deps)
uv run game-lattice --help                # run the CLI (commands: check, impact, reconcile, graph)

uv run --group dev pytest                 # full suite (enforces coverage >= 80%)
uv run --group dev pytest tests/test_loader.py::test_duplicate_id_raises   # a single test
uv run --group dev pytest tests/test_check.py -v                           # one file, verbose

uv run --group dev ruff check src tests   # lint
uv run --group dev ruff format src tests  # format (add --check to verify only)
uv run --group dev ty check src           # type check
uv run --group dev python scripts/check_typing_boundaries.py src           # boundary rule check
```

A pre-commit hook runs ruff (with `--fix`), ruff-format, `ty`, the typing-boundary check, and
detect-secrets on every commit, and blocks direct commits to `main`.
A commit that fails any of these is rejected, so code must be lint/type/boundary clean before it
lands; if a hook auto-fixes a file, re-stage and re-commit.

## Architecture

The engine is a pure pipeline feeding four read-mostly commands:

```
config -> discovery -> frontmatter parse -> loader.build_lattice -> { check, impact, reconcile, graph }
```

`orchestrate.load_lattice(project)` is the single wiring point that runs that pipeline.

**The `Lattice` (model.py) is the central structure** and every command reads from it:

- `nodes_by_id`: each tracked file as a `Node` (frontmatter + verbatim body).
- `index`: every stable id mapped to a `Location`. File ids and `{#anchor}` section ids share one
  flat namespace; a collision anywhere is a `DuplicateIdError` (a load failure, exit 2).
- `dependents`: reverse adjacency (target id -> source ids that derive from it), built from
  resolved edges only.
- `ancestors`: for a section anchor, the enclosing anchored sections (outermost to innermost), so
  editing a nested sub-section propagates impact to dependents of its parent.

**Refs and edge state.** A `derives_from` ref resolves on the trailing segment after the last `#`
(`art-direction#accent` and bare `accent` resolve to the same id; see `model.split_ref`).
A ref that resolves to nothing is **not** a load error: it is a normal lattice state
(`target_id=None`) that `check` reports as `BROKEN`. This is the key modeling decision that
distinguishes a broken edge (exit 1, drift) from a duplicate id (exit 2, incoherent index).

**Drift detection.** Each edge carries a `seen` hash captured when it was last reconciled.
`check` classifies every edge as OK / STALE / UNRECONCILED / BROKEN by comparing `seen` against
`content_hash(target_content(...))`, where the hash is `sha256(canonicalize(text))` truncated to 32
hex chars (128 bits) and `canonicalize` strips cosmetic differences (line endings, trailing
whitespace, edge blank lines). `impact` reverse-walks `dependents` transitively (with ancestor
and enclosing-file expansion) to list everything a change touches.

**Reconcile is the only mutating command.** It plans new `seen` values from the loaded snapshot,
then at write time **re-reads each downstream file fresh**, rewrites only the targeted `seen`
scalar(s) through round-trip YAML (preserving body, key order, comments, and any concurrent edit),
and writes atomically. `--ref` selects on resolved identity; `--all` clears only STALE/UNRECONCILED
edges (it skips BROKEN and already-OK). A node's BROKEN edge is skipped, not fatal, so it never
blocks the node's reconcilable edges; only a `--ref` aimed directly at a broken edge is refused.

**Pure vs impure split.** All graph logic is pure and filesystem-free: `model`, `hashing`,
`sections`, `resolve`, `loader`, `check`, `impact`, `render`, and `reconcile.reconcile`/
`apply_reconcile` (which returns rewritten text rather than writing it). Only `config`, `discovery`,
`orchestrate`, and `cli` touch the disk; `cli` performs the actual reconcile writes. This is what
lets the graph layer be tested against synthetic `ParsedDoc` lists with no I/O.

## Project-specific invariants

These are enforced by tooling (pre-commit, `scripts/`, or `tests/test_conventions.py`), so a
violation fails CI rather than just being a style preference:

- **Untyped-to-typed boundary.** `typing.Any` and `typing.cast` are allowed only in modules whose
  name ends in `_parser`/`_boundary`/`_validator` (and similar roles). Currently that is only
  `frontmatter_parser.py`, where raw YAML becomes a typed `NodeMeta`. Everywhere else, convert at
  the boundary and pass typed models.
- **Errors.** All custom exceptions extend `ProjectError` (error_types.py) and carry a `code`; no
  bare `except Exception`/`except BaseException`. Messages name the file and the fix.
- **Constants.** Use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py` and import
  them; do not write raw string literals that duplicate a constant value.
- **Paths and containment.** User-provided paths go through `safe_resolve()` (path_utils.py). Both
  `config` (docs roots) and `discovery` (each discovered file) reject any path that escapes the
  project root via `..`, an absolute path, or a symlink, before any read or write.
- **No `datetime.now()`/`utcnow()` outside `datetime_utils.py`.**
- ruff line length 100; module docstring on every module; Google-style docstrings on public
  functions; no em-dashes in any drafted content (docstrings, messages, comments).

## Testing notes

Test files mirror sources (`src/game_lattice/foo.py` -> `tests/test_foo.py`) and use `tmp_path` for
filesystem work.
`tests/conftest.py` provides the shared `lattice_dir` fixture: a small synthetic doc set
(art-direction with STALE/UNRECONCILED downstream edges, plus a BROKEN ref) that the check,
reconcile, and CLI tests build on, so its contents are load-bearing across those suites.
Property-based invariants (hashing canonicalization) use hypothesis.
