# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Empty ATX headings such as `#` and `##   ` are now recognized and receive the same empty GitHub
  slug that `github-slugger` generates.
- Document symlinks whose targets remain inside the project root are now supported. Aliases to the
  same resolved document are loaded once, while external targets are skipped with a warning and
  reconcile revalidates containment before writing.

### Changed

- Internal: Markdown heading recognition and GitHub-compatible slug generation now pass through a
  documented adapter pinned to `markdown-it-py==4.2.0` and `github-slugger@2.0.0`. The slug-strip
  and JavaScript Unicode 17 lowercase and contextual-casing compatibility data is generated from
  upstream. Section spans and the cache schema are unchanged, but existing version-2 load caches
  are rebuilt so parser-derived anchors and spans use the new adapter. Rare headings whose casing
  data was absent from Python Unicode 15.1 now receive the upstream-compatible section id.
- Internal: the load cache module is now a phase-separated `doc_lattice/cache/` package
  (schema/codec, store, lookup, run state). No user-facing behavior change; the cache file
  format is unchanged.

### Fixed

- Markdown files that open YAML frontmatter without a closing `---` now fail with a
  source-naming tool error (exit 2) across cached and uncached loads instead of being silently
  omitted from the lattice. Existing version-1 load caches are rebuilt.

## [1.0.1] - 2026-07-13

### Fixed

- `graph` Mermaid output now assigns each node a collision-free identifier. Distinct node ids
  that sanitized to the same Mermaid-safe token previously merged into one graph node.
- Heading anchor parsing now recognizes `{#marker}` only as a trailing heading marker
  (optionally followed by an ATX closing `#` sequence). An anchor-like token in the middle of
  heading text is no longer mistaken for the heading's explicit anchor.

## [1.0.0] - 2026-07-12

### Added

- Publish release wheels and source distributions to PyPI through GitHub Actions Trusted
  Publishing, with no stored PyPI credential.

### Changed

- Generated pre-commit and CI gates install an exact `doc-lattice==1.0.0` PyPI requirement
  instead of cloning and building a tagged Git revision.
- Release retries distinguish the current tagged commit from an ordinary unversioned merge,
  making GitHub Release and PyPI publication safe to resume after a partial failure.
- Source distributions contain only package source, tests, license, README, build metadata, and
  Hatchling's required `.gitignore`.

## [0.9.0] - 2026-07-11

### Changed

- **BREAKING:** the project is renamed from game-lattice to doc-lattice. The engine was never
  game-specific; the name now matches its general purpose. In one release this renames the
  repository (https://github.com/Guardantix/doc-lattice, with GitHub redirects from the old
  URL), the distribution and package (`doc-lattice` / `doc_lattice`), the CLI executable
  (`doc-lattice`), the config file (only `.doc-lattice.yml` is recognized; no fallback), and
  the opt-in load-cache location (`<cache_home>/doc-lattice/`; old cache directories are
  orphaned and safe to delete). Doc sets themselves need no edits; lattice frontmatter
  (`id`, `derives_from`, `authority`, `seen`) is unchanged.

### Migration (v0.8.x to v0.9.0)

Nothing breaks until you bump your pin: checked-in gates pin a tag
(`uvx --from git+.../game-lattice@v0.8.0 game-lattice ...`) and GitHub's rename redirect keeps
that resolving indefinitely. Upgrading the pin to v0.9.0 requires, in one commit:

1. Rename `.game-lattice.yml` to `.doc-lattice.yml` (contents unchanged).
2. Regenerate the checked-in pre-commit hook and CI workflow (re-run `doc-lattice init`
   codegen, or by hand update the repo URL, the `@v0.9.0` pin, and the executable name
   `game-lattice` to `doc-lattice` in each invocation).
3. Any Python code importing `game_lattice` switches to `doc_lattice` (the package is not on
   PyPI and no import consumers are known; listed for completeness).

## [0.8.0] - 2026-07-10

### Added

- Opt-in incremental load cache: set `cache_key` in `.game-lattice.yml` to skip re-parsing
  unchanged docs across runs and git worktrees, with byte-identical output to an uncached run by
  default; `cache_trust_stat: true` adds a faster stat tier for read-only commands under the
  documented mtime caveat, and `reconcile` always verifies content (#28).
- `check`, `lint`, `impact`, and `linear` accept `--indent N` with JSON output (`--json`, or the
  equivalent `--format json` on `check` and `lint`), and the global `--no-color` option and the
  `NO_COLOR` environment variable both explicitly disable colored output, including the styling on
  help and usage-error text even when a terminal-forcing environment variable is set (#20).
- `check --format github` and `lint --format github` emit escaped GitHub Actions error
  annotations with repo-relative file paths so findings attach inline to the offending doc in
  the pull request diff, while preserving the existing gate exit codes; both commands also accept
  `--format human|json`, and `--json` remains the JSON alias (#18).

### Changed

- Moved `check`, `lint`, and `impact` JSON builders beside their result types and centralized their
  human console output in a dedicated report renderer, leaving the CLI as dispatch-only wiring
  (#29).
- Centralized CLI `ProjectError` handling behind the shared tool-error exit path (#30).
- Internal performance: `check` and `reconcile` memoize target-content hashes within each run,
  avoiding repeated section extraction and hashing for edges that share a target (#25).
- Reduced repeated load-path work by counting document lines once, reusing safe YAML loaders, and
  sharing newline normalization between section parsing and hashing (#27).
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
