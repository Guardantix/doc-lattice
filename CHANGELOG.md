# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Centralized CLI `ProjectError` handling behind the shared tool-error exit path (#30).
- Moved reconcile phase-1 rewrite planning into the pure reconcile module via an injected reader
  (#31).

## [0.7.0] - 2026-07-09

### Added

- `check --only STATE` (repeatable) filters human and JSON output by edge state; the exit code
  still reflects every edge (#19).
- `graph --format json` emits a machine-readable node and edge dump that matches the Mermaid and
  DOT edge collapsing (#21).
- `impact --depth N` bounds the reverse walk, and `impact --json` entries now carry a `depth`
  field (#22).
- `reconcile --dry-run` previews the plan without writing, and `reconcile --json` emits a
  machine-readable plan for both dry and real runs (#17).
- The Linear client retries transient HTTP 429 and 5xx failures with bounded backoff, honoring
  `Retry-After` up to a 30 second cap (#24).
- The version-sync guard now also checks README pinned install refs (`game-lattice@vX.Y.Z`)
  against `__version__` (#34).
- The release job now publishes a GitHub Release for each tag it cuts, with the body taken from
  the matching `## [X.Y.Z]` CHANGELOG section; `scripts/extract_release_notes.py` (pure core
  `version_check.changelog_section`) extracts it and fails the release if that section is missing
  or empty (#47).

### Changed

- `graph --format` now rejects unknown formats with exit 2 instead of silently rendering
  Mermaid (#21).
- Ancestor recording in the loader is a single stack pass instead of a quadratic scan (#26).
- CI runs the code-quality job on both Python 3.13 and 3.14 (#33).

### Removed

- Unused `datetime_utils` helpers (`local_now`, `parse_iso`, `format_iso`); `utc_now` remains the
  single sanctioned current-time entry point (#35).

## [0.6.0] - 2026-07-05

### Changed

- Lowered the minimum supported Python from 3.14 to 3.13 (`requires-python = ">=3.13"`). 3.13 was
  already the true floor: the engine's only version-gated dependency is `PurePath.full_match`
  (added in Python 3.13, used by `ignore_globs` matching), so no engine change was required. CI now
  runs the test suite on a 3.13 and 3.14 matrix, and `init`'s generated pre-commit and CI gates pin
  `--python 3.13` so an adopting repo provisions the declared minimum.

## [0.5.0] - 2026-07-01

### Added

- GitHub-native heading-slug fallback for anchor resolution: a `derives_from` section ref now
  resolves against a plain heading with no `{#slug}` marker, computing the same slug GitHub
  renders for that heading (ported verbatim from `github-slugger@2.0.0` for byte-parity). An
  explicit `{#marker}` still resolves and takes precedence, remaining the escape hatch for
  headings whose rendered text diverges from their source (inline links, images).

### Changed

- **Breaking:** section refs must now be namespaced `<file>#<anchor>`; a bare ref resolves only
  to a file id. A bare anchor ref that previously resolved against the flat anchor namespace
  (for example a plain `#accent` matching `art-direction#accent`) now reports `BROKEN` instead.
  Adopters relying on bare-anchor refs must repoint them to the `file#anchor` form.

## [0.4.0] - 2026-06-29

### Added

- Version-consistency guard (`scripts/check_version_sync.py`, pure core `version_check.py`) wired into pre-commit and CI: `__version__`, `pyproject.toml`, and the top `CHANGELOG.md` entry must agree.
- Merge-triggered `release` CI job that creates and verifies the lightweight `vX.Y.Z` tag, smoke-testing `check`, `lint`, and `init` against the pinned ref.

## [0.3.0] - 2026-06-28

### Added

- `lint` command: validates the authority ladder over `derives_from` edges and reports edges it cannot rank.
- Generated pre-commit and CI now run both `game-lattice check` and `game-lattice lint`.

## [0.2.0] - 2026-06-28

### Added

- `init` command: scaffolds `.game-lattice.yml` and prints pre-commit and CI codegen for an adopting repo.
- `RELEASING.md`: release checklist that makes the version tag an atomic part of cutting a release.
- `linear` command: resolve referenced tickets to live Linear status over GraphQL and report tickets shipped against a spec that has since drifted; supports `--from`, `--exit-code`, and `--warn-exit`.

## [0.1.0] - 2026-06-27

### Added

- Initial project scaffolding.
- Local traceability engine that parses lattice frontmatter and anchored `{#anchor}` sections into an id-indexed edge graph, with four offline commands:
  - `check`: classify every `derives_from` edge as OK, STALE, UNRECONCILED, or BROKEN against the upstream content hash; exit 1 on drift, 2 on a tool error.
  - `impact`: list every downstream doc and ticket affected by a change to an id, walking the reverse adjacency with ancestor and enclosing-file expansion.
  - `reconcile`: rewrite the `seen` hash for the selected edges (a downstream id, `--ref`, or `--all`) through round-trip YAML, atomically and only after a fresh re-read so a concurrent edit is preserved.
  - `graph`: emit the edge graph as Mermaid or DOT (`--format`), marking stale edges.
