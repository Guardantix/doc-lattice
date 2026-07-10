# CLI JSON Indentation and Color Control: Design Spec

**Date:** 2026-07-10
**Status:** Approved for implementation.
**Issue:** GitHub issue #20, `cli: --indent for JSON output and a global --no-color flag`.

## Goal

Make the four requested JSON reports readable on demand and let users explicitly suppress ANSI
color, while preserving byte-identical command behavior when neither new option is present.

## Scope

`--indent` applies only to `check`, `lint`, `impact`, and `linear`, as prescribed by issue #20.
It does not change `reconcile --json` or `graph --format json`. `--no-color` is a global option and
therefore appears before the subcommand, for example `game-lattice --no-color check`.

## Architecture

`cli.py` will define a reusable `IndentOpt` annotation beside the existing `ConfigOpt` and
`JsonOpt` annotations. A small validation helper will reject any non-`None` indent when JSON mode
is disabled, print a clear error through `_err`, and raise `typer.Exit(2)`. Each of the four command
functions will validate before loading project data and will pass the accepted value directly to
its existing `json.dumps(payload, indent=indent)` call. An indent of zero is valid because Python's
JSON encoder still emits line breaks while adding no spaces before nested values; negative values
are rejected by Typer's `min=0` constraint.

Color remains a presentation concern in the CLI shell. `_disable_color()` will contain the only
`global` declarations and replace `_out` and `_err` with `Console(no_color=True)` and
`Console(stderr=True, no_color=True)`. `main_callback` will expose `--no-color` and call this helper
before a subcommand runs. Existing call sites already look up the module-level consoles at call
time, so report and error rendering need no further changes.

## Data and Error Flow

For the four JSON commands, absence of `--indent` follows the current path exactly. When an indent
is supplied, validation runs first. Human mode fails immediately with exit 2 and a stderr message
that names both `--indent` and `--json`; JSON mode builds the same Python payload as before and only
changes its textual serialization. Parsing compact and indented forms therefore produces equal
values.

For color, Typer processes the global option and calls `main_callback` before dispatching the
selected command. Without `--no-color`, the callback leaves both consoles untouched. With the
flag, every later `_out` and `_err` lookup uses a no-color console, including project errors and
validation errors.

## Alternatives Considered

1. Shared option and validation helper: selected. It gives all four commands identical validation
   and help text without changing unrelated JSON producers.
2. Inline option declarations and checks in each command: rejected because it duplicates the same
   option contract and error path four times.
3. A general JSON-emission abstraction for every command: rejected because it broadens the change
   to `reconcile` and `graph`, whose JSON interfaces are outside issue #20.

## Testing

Tests in `tests/test_cli.py` will be added before production changes and run in red-green cycles.
They will prove:

- `check --json --indent 2` contains formatted newlines and round-trips to the same value as the
  compact output.
- `check`, `lint`, `impact`, and `linear` each exit 2 with a clear stderr error when `--indent` is
  used without JSON mode.
- Typer rejects a negative indent with exit 2.
- Existing compact JSON and human-output assertions remain unchanged when `--indent` is absent.
- A control invocation through `CliRunner` uses a temporarily forced-color `_out` console and
  contains an ANSI escape sequence, while `--no-color check` contains none. Test isolation restores
  the original module console after the invocation so later in-process CLI tests are unaffected.
- CLI help exposes `--indent` on exactly the four requested commands and exposes global
  `--no-color`.

The final verification will run the full pytest suite and every repository quality command named
in the issue.

## Documentation

README command documentation will describe `--indent N`, global `--no-color`, and Rich's support
for the `NO_COLOR` environment variable as the environment-level equivalent. CHANGELOG will add an
`Added` entry under `[Unreleased]` referencing issue #20. No architecture decision changes, so
`ARCHITECTURE.md` and `CLAUDE.md` remain current.
