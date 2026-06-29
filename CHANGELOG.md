# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
