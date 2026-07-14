# CLI Orchestration Decomposition Design

**Date:** 2026-07-14
**Status:** Approved
**Issue:** #89

## 1. Objective

Decompose the command-line shell so command work no longer converges on one change hotspot and
output behavior no longer depends on mutable module-level consoles. Preserve the existing CLI
contract while introducing explicit boundaries for per-invocation state, shared output policy,
error mapping, and command orchestration.

The user-visible compatibility contract covers command names, options, help text, stdout, stderr,
exit codes, filesystem mutations, cache behavior, GitHub annotations, and color suppression.

## 2. Constraints

- Keep the console-script target `doc_lattice.cli:main` valid.
- Keep `from doc_lattice.cli import app` valid for Typer integration tests and internal callers.
- Preserve every current command and option. This refactor adds no runtime deprecation warning.
- Keep domain computation in the existing pure modules. Command adapters only validate options,
  invoke dependencies, render results, and select an exit code.
- Do not introduce `typing.Any` or `typing.cast` outside the repository's approved boundary modules.
- Do not weaken reconcile locking, recovery, containment, cache verification, or delayed-success
  guarantees.
- Preserve import portability when `fcntl` is unavailable.

## 3. Chosen Structure

Replace `src/doc_lattice/cli.py` with a `src/doc_lattice/cli/` package. A package preserves the
existing import path while allowing each command to have a focused adapter and test module.

```text
src/doc_lattice/cli/
├── __init__.py          # lazy compatibility export and console-script entry point
├── application.py       # Typer construction, callback, and command registration
├── runtime.py           # immutable per-invocation state and loader injection
├── output.py            # output selection, indentation, JSON, and annotations
├── errors.py            # project/internal error rendering and exit mapping
├── options.py           # shared Typer option annotations
└── commands/
    ├── __init__.py
    ├── check.py
    ├── lint.py
    ├── impact.py
    ├── graph.py
    ├── reconcile.py
    ├── linear.py
    └── init.py
```

The previously considered alternatives were a facade `cli.py` plus flat sibling modules and a
single-module class decomposition. The package is preferred because it preserves compatibility,
makes ownership discoverable, and prevents the facade from becoming the next hotspot.

## 4. Runtime and Dependency Injection

`CliRuntime` is a frozen, slotted dataclass created once per Typer invocation and stored in
`typer.Context.obj`. It contains:

- a Rich console bound to the invocation's stdout;
- a Rich console bound to the invocation's stderr;
- the invocation cwd captured at runtime creation;
- the `load_config` callable;
- the `load_lattice` callable.

The default factory binds the real dependencies. Tests can build an application with a replacement
runtime factory, streams, cwd, or loaders without monkeypatching module globals. Commands obtain the
runtime through one checked accessor. No command reads `Path.cwd()` directly, and no command uses or
reassigns a module-level console.

Command-specific domain dependencies remain imports in their owning adapter. This gives each
adapter a narrow and visible dependency surface without turning the common runtime into a service
locator for every subsystem.

## 5. Application and Entry Point

`application.py` exposes `create_app(runtime_factory=default_runtime)` and one default `app`.
Registration functions attach each command to an application. The root callback creates the
runtime after parsing the global `--no-color` option and stores it in the context.

`cli.__init__` exposes `app` lazily through module `__getattr__` for compatibility and defines
`main` without importing Typer. The console entry point scans argv and `NO_COLOR` before importing
the Typer application. For explicit no-color execution it sets the environment Rich already
recognizes and Typer's import-time terminal-disable environment flag, then imports and invokes the
application. This preserves escape-free help and parse errors under terminal-forcing CI variables
without mutating `typer.rich_utils.COLOR_SYSTEM` or any console global. Command output uses the
no-color consoles created for that invocation.

`main` preserves intended `SystemExit` values. It maps `ProjectError` to the standard project-error
line and exit 2, and maps the existing unexpected `OSError`, `RuntimeError`, and `ValueError` set to
the standard internal-error line and exit 2.

## 6. Shared Output and Error Policy

`output.py` owns the rules currently repeated or scattered through command code:

- validate a requested format against a command's allowed formats;
- resolve the legacy `--json` alias after validating explicit `--format`;
- reject `--json` combined with `--format github`;
- validate that `--indent` is used only with effective JSON output;
- serialize JSON to the injected stdout with deterministic existing separators and newlines;
- write exact non-JSON text to injected stdout;
- escape and build GitHub workflow annotations.

The resolver returns an immutable output selection containing the effective format and indent.
Commands that currently expose only `--json` use the same resolver with their existing implicit
human format. Graph uses the same format validator with its existing Mermaid, DOT, and JSON set.
No command gains or loses a flag in 1.x.

`errors.py` owns project-error rendering, the `ProjectError` command boundary, internal-error
rendering, and exit-code constants. A project error always produces the existing escaped one-line
diagnostic on stderr and exit 2. Routine findings remain data: check and lint can exit 1, Linear can
exit 1 only under its gate flags, and informational commands exit 0 on success.

## 7. Command Adapters

Each adapter owns only its Typer declaration and command-specific orchestration:

- `check`: parse the state filter, load, classify, render, and derive drift exit status.
- `lint`: load, validate the authority ladder, render, and derive violation exit status.
- `impact`: load, walk dependents, and render.
- `graph`: load, compute stale edges, and render the selected graph representation.
- `reconcile`: validate selectors, coordinate config, lock, recovery, verified loading, containment,
  planning, transaction commit, and delayed reporting.
- `linear`: validate targeting flags, load, fetch tickets, build findings, render, and apply the
  optional gate.
- `init`: validate scaffold values, create the config safely, and print guidance.

Reconcile helpers that encode reconcile reporting or write-path orchestration remain beside the
reconcile adapter. Init flag validation remains beside init. Check state filtering remains beside
check. Shared policy moves only when two or more commands use it or when issue #89 explicitly
requires one implementation.

## 8. JSON Compatibility and Deprecation Policy

During the entire 1.x series, `--json` remains silent and behaviorally compatible. A warning would
change stderr and break scripts that treat stderr as a failure channel, so 1.x emits no warning.

Documentation will identify `--format` as the future uniform selector and record this 2.0 policy:

- remove the `--json` alias;
- add `--format` to output-producing commands that currently expose only `--json`;
- retain command-specific allowed values, such as `human|json|github` for report gates and
  `mermaid|dot|json` for graph;
- make `--indent` depend on effective format `json` everywhere.

This PR documents the breaking-release path but does not implement the break.

## 9. Test Organization

Move the monolithic `tests/test_cli.py` suite under `tests/cli/`:

```text
tests/cli/
├── helpers.py
├── test_runtime.py
├── test_output.py
├── test_check.py
├── test_lint.py
├── test_impact.py
├── test_graph.py
├── test_reconcile.py
├── test_linear.py
├── test_init.py
└── test_contract.py
```

Command tests retain the existing success, failure, mutation, exact-output, and cache cases.
`test_runtime.py` proves per-invocation console and loader injection and the absence of cross-run
color state. `test_output.py` exercises the shared format, indentation, JSON, annotation, and error
rules once. `test_contract.py` stays concise and covers the public application boundary: import
portability, global help/version, command help, subprocess no-color behavior, main exit mapping,
representative exact stdout/stderr, cache parity, and reconcile's verified-load contract.

The original test assertions are migrated rather than discarded. New focused tests first fail
against the current global-console architecture, then drive the runtime and output APIs.

## 10. Documentation and Architecture Updates

- README documents the silent 1.x alias and explicit 2.0 migration path.
- ARCHITECTURE records the CLI package, runtime boundary, command adapters, and output policy.
- CHANGELOG notes the internal decomposition and documented compatibility policy.
- CLAUDE updates its pure/impure inventory and replaces references to a single `cli` module.

## 11. Acceptance Evidence

| Requirement | Evidence |
|---|---|
| No mutable module-level console state | Runtime tests plus a source scan for former `_out`/`_err` globals and console reassignment |
| Narrow command dependencies | One adapter per command, reviewed imports, and architecture documentation |
| One output/error implementation | Focused output/error unit tests and command adapters importing those helpers |
| Preserve stdout, stderr, exits, help, no-color | Migrated exact contract tests, subprocess color tests, and full-suite pass |
| Organize tests by command | `tests/cli/test_<command>.py` plus the concise contract suite |
| Document `--json` policy | README, architecture decision, and changelog entry |

Repository-wide completion requires the full pytest suite, Ruff check and format check, ty type
check, typing-boundary check, version-sync check, slug-data check, and an independent spec and code
quality review with all important findings resolved.

## 12. Non-Goals

- No command semantics, output schema, option spelling, or default changes in 1.x.
- No domain-module refactor unrelated to CLI orchestration.
- No new plugin framework or generalized dependency-injection container.
- No replacement of Typer or Rich.
- No change to reconcile durability or Linear network behavior.
