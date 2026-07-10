# GitHub Actions Annotation Output Design

## Goal

Add an explicit GitHub Actions annotation format to `check` and `lint` so CI findings appear
inline on pull requests while preserving all existing human and JSON behavior and exit codes.
This design implements GitHub issue #18.

## Requirements

- `check` and `lint` accept `--format human|json|github`, defaulting to `human`.
- The existing `--json` flag remains an alias for JSON output.
- `--json --format github` is a conflicting selection and exits 2 with a clear stderr message.
- Any unsupported `--format` value exits 2 with a clear stderr message.
- Format errors are rejected before the lattice is loaded.
- `check --format github` emits one raw `::error` workflow command for every displayed edge
  whose state is not `OK`. Each command includes the source document path and state. `OK` edges
  emit nothing.
- `lint --format github` emits one raw `::error` workflow command for every authority-ladder
  violation. Skipped edges emit nothing.
- Annotation message values escape `%`, carriage return, and newline. Annotation property values
  additionally escape `:`, `,`, and the message escape set.
- Human and JSON payloads remain byte-compatible with their current output.
- Exit codes remain 0 for a clean gate, 1 for drift or ladder violations, and 2 for tool errors.
- README command documentation and the Unreleased CHANGELOG section describe the new format.

## Current Architecture

`src/game_lattice/cli.py` still owns human and JSON rendering for both commands. The rendering
extraction anticipated by issue #29 has not landed on `main`, so the issue's prescribed placement
in `cli.py` matches the current architecture. `check_lattice` and `lint_lattice` remain pure and
unchanged. Both commands already retain the loaded `Lattice`, which provides the source path through
`lattice.nodes_by_id[source_id].path` even when a check edge is BROKEN and has no `target_id`.

## Considered Approaches

### 1. Small CLI-local helpers and format branches

Add two private escaping helpers, one private format resolver, and narrow GitHub rendering branches
in the existing `check` and `lint` commands. This is the selected approach because it follows the
issue prescription and current ownership without broadening the change.

### 2. Extract a dedicated annotation renderer module

A new module could isolate workflow-command construction from Typer. This creates a premature
partial rendering extraction while human and JSON rendering remain in `cli.py`, and would overlap
with the separate #29 refactor.

### 3. Introduce a shared formatter abstraction

A protocol or strategy layer could unify human, JSON, and GitHub output. The two commands have
different payload shapes and only need one added format, so the abstraction would add indirection
without serving an acceptance criterion.

## CLI Selection

Each command receives:

```python
fmt: Annotated[str, typer.Option("--format", help="human, json, or github.")] = "human"
```

A private resolver validates the pair `(fmt, json_out)` and returns the effective format:

1. If `json_out` is true and `fmt == "github"`, print a conflict error to stderr and exit 2.
2. If `json_out` is true for any other format value, select `json`. This keeps `--json` working as
   an alias and makes it authoritative over an explicitly supplied `human` or `json` value.
3. Reject an effective format outside `human`, `json`, and `github` with exit 2.

Validation happens before `_load`, consistent with existing `--only` and graph format validation.

## Annotation Rendering

The message escaper applies replacements in workflow-command order:

```text
%  -> %25
CR -> %0D
LF -> %0A
```

The property escaper first applies message escaping and then replaces:

```text
: -> %3A
, -> %2C
```

Escaping `%` first prevents percent signs introduced by later replacements from being escaped a
second time. Every interpolated property and message fragment goes through the appropriate helper.
The fixed workflow-command syntax remains literal.

For `check`, the annotation shape is:

```text
::error file={path},title=game-lattice {state}::{source_id} -> {target_ref} is {state}
```

The command renders only the already display-filtered statuses, preserving `--only` semantics, and
then suppresses `OK`. Gate exit status still uses the full unfiltered status list.

For `lint`, the annotation shape is:

```text
::error file={path},title=game-lattice ladder violation::{source_id} ({source_authority}) -> {target_ref} ({target_authority})
```

Only `result.violations` is rendered. The human-only skip summary remains absent from GitHub output.

## Testing

Tests in `tests/test_cli.py` proceed red-first and cover:

- the two escaping helpers directly, including `%`, CR, LF, `:`, and `,`;
- exact three-line fixture output for BROKEN, STALE, and UNRECONCILED check findings;
- no annotation for OK check edges;
- source path lookup for the BROKEN fixture edge;
- one annotation per lint violation and no skipped-edge annotation;
- unchanged gate exit codes in GitHub format, including clean runs;
- byte-compatible existing `--json` behavior and explicit `--format json` behavior;
- the `--json --format github` conflict for both commands;
- unsupported format rejection for both commands;
- property and message escaping through complete emitted annotations.

The full repository verification commands from issue #18 run after implementation.

## Documentation

The README command table will show both new `--format` options. The command guidance will explain
that `human` is the default, `json` matches `--json`, and `github` emits workflow commands without
changing gate exit behavior. The CHANGELOG records the new CI-facing format under Unreleased.

## Non-goals

- SARIF output.
- Automatic `GITHUB_ACTIONS` detection.
- Annotation output for `impact`, `linear`, or other commands.
- Moving all renderers out of `cli.py`.
