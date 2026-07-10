# Load-Path Micro-Cleanups: Design Spec

**Date:** 2026-07-10
**Status:** Approved for implementation.
**Issue:** GitHub issue #27, `tech-debt: load-path micro-cleanups`.

## Goal

Remove three small sources of repeated load-path work while preserving every observable result:
count each document's lines once, reuse safe-mode YAML loaders, and centralize newline
normalization. Keep round-trip YAML isolated per reconcile operation because its instance retains
document-specific state.

## Architecture

`loader.build_lattice` will compute `total_lines` once at the start of each document iteration,
before registering the file target. The file span and every section span for that document will
reuse the value. No cache or additional module state is needed.

`frontmatter_parser.py` and `config.py` will each own one module-level `_YAML = YAML(typ="safe")`
instance. Their existing parsing functions will call that instance. These are the only new
module-level mutable objects. They are intentionally local to their modules, safe to reuse in the
single-threaded CLI, and do not create a shared cross-module abstraction.

A YAML directive can set persistent version state on a reusable parser. A valid config can retain
that state after loading, and malformed frontmatter can retain it before raising a parse error.
Both parsing functions will reset their local `_YAML.version` to `None` immediately before every
load so one file cannot change the scalar semantics of a later file. This retains both singletons
while matching a fresh safe loader's default version for each independent document.

`sections.split_body_lines` will import and call `hashing.normalize_newlines` before splitting.
The helper performs the same CRLF replacement followed by lone-CR replacement as the existing
inline expression, so headings, spans, hashes, and output remain unchanged. `hashing.py` has no
package imports, so this dependency cannot create an import cycle.

`reconcile.apply_reconcile` will continue constructing `YAML(typ="rt")` inside each call. A nearby
comment will document that round-trip loaders retain document-specific state and therefore must
not be reused.

## Alternatives Considered

1. Focused local changes with narrow implementation-contract tests: selected. This directly
   matches the issue, provides meaningful TDD failures, and avoids unrelated abstractions.
2. Prescribed production edits backed only by the existing behavioral suite: rejected because
   behavior-preserving refactors would never produce a test-first RED signal.
3. A shared YAML factory or package-wide singleton: rejected because the two safe parsing
   boundaries are already clear, while round-trip reconciliation has different lifetime rules.

## Testing

Add focused tests that fail against the current implementation and prove each internal contract:

- `build_lattice` calls `_line_count` once for each document.
- `frontmatter_parser.parse_meta` uses its module-level `_YAML` instance across calls.
- `config._read_yaml` uses its module-level `_YAML` instance across loads.
- Config loads reset any YAML version directive before parsing the next independent file.
- Frontmatter loads reset version state that a malformed earlier document may have retained.
- `split_body_lines` delegates newline normalization to `hashing.normalize_newlines` through the
  symbol imported by `sections.py`.

The existing `test_split_body_lines_normalizes_crlf_and_lone_cr` already covers the issue's lone
carriage-return regression requirement and will remain unchanged. After each focused RED and GREEN
cycle, run the relevant test module. Final verification will run the full pytest, Ruff lint, Ruff
format check, ty type check, and typing-boundary commands prescribed by the issue.

## Documentation

Add a `Changed` entry under `CHANGELOG.md`'s `[Unreleased]` section describing the internal load-path
cleanup. README and architecture documentation remain accurate because public behavior, command
flow, and ownership boundaries do not change.
