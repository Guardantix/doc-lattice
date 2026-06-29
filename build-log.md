# Build Log

## 2026-06-27 -- Project initialized

Project initialized. Goals: Traceability engine for game design and production documentation. Scaffolded with gx-new v0.1.0.

## 2026-06-27 -- local-core engine (PR #1)

Deterministic offline core: config -> discovery -> frontmatter parse -> loader.build_lattice, the id-indexed edge graph, and the check, impact, reconcile, and graph commands. No network, no secrets, no LLM.

## 2026-06-28 -- linear slice (PR #3)

First network-touching command: `linear` resolves referenced tickets to live status over GraphQL and reports shipped-against-stale-spec drift.

## 2026-06-28 -- init slice, release 0.2.0 (PR #4)

`init` scaffolds `.game-lattice.yml` and prints pinned pre-commit and CI codegen for an adopting repo. Cut tag v0.2.0.

## 2026-06-28 -- lint slice, release 0.3.0 (PR #6)

`lint` validates the authority ladder (binding > derived > exploratory) over `derives_from` edges and is wired into the generated gates alongside `check`. Cut tag v0.3.0.

## 2026-06-29 -- release automation (PR #8)

Version-sync guard (`scripts/check_version_sync.py`) plus a merge-triggered CI `release` job that smoke-tests `check`, `lint`, and `init` against the pinned ref, then cuts the `vX.Y.Z` tag. No version bump, so the job no-ops the tag step.
