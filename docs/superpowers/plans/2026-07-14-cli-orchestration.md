# CLI Orchestration Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic CLI module and test file with an injected per-invocation runtime,
one command adapter per command, centralized output/error policy, command-organized tests, and a
documented silent 1.x `--json` compatibility path.

**Architecture:** Convert `doc_lattice.cli` from a module to a compatibility-preserving package.
The Typer callback stores a fresh `CliRuntime` in `Context.obj`; shared output and error modules own
all cross-command policy; seven registration modules own command-specific orchestration. The
console entry point imports Typer lazily so no-color setup does not mutate Typer or console globals.

**Tech Stack:** Python 3.13+, Typer, Rich, pytest, pytest-mock, uv, Ruff, ty

---

## File Map

- Create `src/doc_lattice/cli/__init__.py`: lazy `app` compatibility export and console entry point.
- Create `src/doc_lattice/cli/application.py`: application factory, root callback, command
  registration, and version callback.
- Create `src/doc_lattice/cli/runtime.py`: immutable runtime, runtime factory protocol, context
  accessor, exact stream writes, and injected config/lattice loaders.
- Create `src/doc_lattice/cli/output.py`: output selection, format and indent validation, JSON
  serialization, and GitHub annotation rendering.
- Create `src/doc_lattice/cli/errors.py`: project-error boundary and unexpected-error rendering.
- Create `src/doc_lattice/cli/options.py`: shared Typer option annotations.
- Create `src/doc_lattice/cli/commands/check.py`: check adapter and state filtering.
- Create `src/doc_lattice/cli/commands/lint.py`: lint adapter.
- Create `src/doc_lattice/cli/commands/impact.py`: impact adapter.
- Create `src/doc_lattice/cli/commands/graph.py`: graph adapter.
- Create `src/doc_lattice/cli/commands/reconcile.py`: reconcile adapter, containment, and reports.
- Create `src/doc_lattice/cli/commands/linear.py`: Linear adapter and gate.
- Create `src/doc_lattice/cli/commands/init.py`: init adapter and flag validation.
- Modify `src/doc_lattice/constants.py`: add the common human/JSON format literal and valid set.
- Delete `src/doc_lattice/cli.py`: replaced by the package after its behavior is migrated.
- Replace `tests/test_cli.py` with `tests/cli/` command tests, shared helpers, runtime/output tests,
  and a concise contract suite.
- Modify `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, and `CLAUDE.md`: document the new boundary
  and the 1.x/2.0 JSON policy.

## Task 1: Drive the Runtime and Shared Output APIs

**Files:**
- Create: `tests/cli/__init__.py`
- Create: `tests/cli/test_runtime.py`
- Create: `tests/cli/test_output.py`
- Create: `src/doc_lattice/cli/runtime.py`
- Create: `src/doc_lattice/cli/output.py`
- Create: `src/doc_lattice/cli/errors.py`
- Create: `src/doc_lattice/cli/options.py`
- Modify: `src/doc_lattice/constants.py`

- [ ] **Step 1: Write failing runtime tests**

Add tests that import the wished-for package APIs before the package exists:

```python
from io import StringIO
from pathlib import Path

import typer
from rich.console import Console
from typer.testing import CliRunner

from doc_lattice.cli.application import create_app
from doc_lattice.cli.runtime import CliRuntime, get_runtime


def _runtime(stdout: StringIO, stderr: StringIO, cwd: Path, *, no_color: bool) -> CliRuntime:
    def load_config(_config, seen_cwd):
        raise AssertionError(f"unexpected load from {seen_cwd}")

    def load_lattice(_project, *, require_verified=False, persist_cache=True):
        raise AssertionError(
            f"unexpected lattice load {require_verified=} {persist_cache=}"
        )

    return CliRuntime(
        stdout=Console(file=stdout, no_color=no_color),
        stderr=Console(file=stderr, no_color=no_color),
        cwd=cwd,
        load_config=load_config,
        load_lattice=load_lattice,
    )


def test_runtime_factory_creates_isolated_invocation_state(tmp_path: Path):
    created: list[CliRuntime] = []

    def factory(*, no_color: bool) -> CliRuntime:
        runtime = _runtime(StringIO(), StringIO(), tmp_path, no_color=no_color)
        created.append(runtime)
        return runtime

    app = create_app(runtime_factory=factory)

    @app.command("runtime-probe")
    def runtime_probe(ctx: typer.Context) -> None:
        runtime = get_runtime(ctx)
        runtime.write_stdout(str(runtime.cwd))

    runner = CliRunner()
    colored = runner.invoke(app, ["runtime-probe"])
    plain = runner.invoke(app, ["--no-color", "runtime-probe"])

    assert colored.exit_code == plain.exit_code == 0
    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].stdout.no_color is False
    assert created[1].stdout.no_color is True
```

Also test `CliRuntime.project(config)` passes its captured cwd to the injected config loader and
`CliRuntime.lattice(project, require_verified=True, persist_cache=False)` forwards both keyword
arguments to the injected lattice loader.

- [ ] **Step 2: Write failing shared-output tests**

Add focused tests with `CliRuntime` backed by `StringIO`:

```python
import json
from io import StringIO

import pytest
import typer
from rich.console import Console

from doc_lattice.cli.output import (
    github_annotation,
    select_output,
    write_json,
)
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.constants import VALID_REPORT_FORMATS


@pytest.fixture
def runtime(tmp_path):
    def unexpected_config(_config, _cwd):
        raise AssertionError("output policy must not load config")

    def unexpected_lattice(_project, *, require_verified=False, persist_cache=True):
        raise AssertionError(
            f"output policy must not load lattice {require_verified=} {persist_cache=}"
        )

    return CliRuntime(
        stdout=Console(file=StringIO(), no_color=True),
        stderr=Console(file=StringIO(), stderr=True, no_color=True),
        cwd=tmp_path,
        load_config=unexpected_config,
        load_lattice=unexpected_lattice,
    )


def test_json_alias_resolves_after_explicit_format_validation(runtime):
    selection = select_output(
        runtime,
        fmt="human",
        json_alias=True,
        valid=VALID_REPORT_FORMATS,
        indent=2,
    )
    assert selection.format == "json"
    assert selection.indent == 2


def test_unknown_format_wins_over_json_alias(runtime):
    with pytest.raises(typer.Exit) as raised:
        select_output(
            runtime,
            fmt="yaml",
            json_alias=True,
            valid=VALID_REPORT_FORMATS,
        )
    assert raised.value.exit_code == 2
    assert "--format 'yaml' must be one of" in runtime.stderr.file.getvalue()


def test_indent_requires_effective_json(runtime):
    with pytest.raises(typer.Exit) as raised:
        select_output(
            runtime,
            fmt="human",
            json_alias=False,
            valid=VALID_REPORT_FORMATS,
            indent=2,
        )
    assert raised.value.exit_code == 2
    assert runtime.stderr.file.getvalue() == "error: --indent requires --json\n"


def test_write_json_uses_exact_injected_stdout(runtime):
    write_json(runtime, {"a": [1]}, indent=2)
    assert json.loads(runtime.stdout.file.getvalue()) == {"a": [1]}
    assert runtime.stdout.file.getvalue().endswith("\n")


def test_github_annotation_escapes_all_workflow_metacharacters(tmp_path):
    result = github_annotation(
        tmp_path / "sub%:,\nline.md",
        tmp_path,
        "title%:,\r\nline",
        "message%:,\r\nline",
    )
    assert result == (
        "::error file=sub%25%3A%2C%0Aline.md,"
        "title=title%25%3A%2C%0D%0Aline::"
        "message%25:,%0D%0Aline"
    )
```

Add one test for the `--json` plus `--format github` conflict and one for zero indentation.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
uv run --group dev pytest --no-cov tests/cli/test_runtime.py tests/cli/test_output.py -q
```

Expected: collection fails because `doc_lattice.cli` is still a module and has no `runtime`,
`output`, or `application` submodule. This is the required RED result.

- [ ] **Step 4: Add the shared format constant**

In `src/doc_lattice/constants.py`, add:

```python
BasicOutputFormat = Literal["human", "json"]
VALID_BASIC_OUTPUT_FORMATS: frozenset[str] = frozenset(get_args(BasicOutputFormat))
```

Use the existing `Literal` plus `get_args()` convention and keep the imports sorted.

- [ ] **Step 5: Implement the immutable runtime**

Implement `runtime.py` with these exact public contracts:

```python
class LatticeLoader(Protocol):
    def __call__(
        self,
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice: ...


class RuntimeFactory(Protocol):
    def __call__(self, *, no_color: bool) -> "CliRuntime": ...


@dataclass(frozen=True, slots=True)
class CliRuntime:
    stdout: Console
    stderr: Console
    cwd: Path
    load_config: Callable[[Path | None, Path], ProjectConfig]
    load_lattice: LatticeLoader

    def project(self, config: Path | None) -> ProjectConfig:
        return self.load_config(config, self.cwd)

    def lattice(
        self,
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        return self.load_lattice(
            project,
            require_verified=require_verified,
            persist_cache=persist_cache,
        )

    def write_stdout(self, text: str, *, newline: bool = True) -> None:
        self.stdout.file.write(text)
        if newline:
            self.stdout.file.write("\n")
        self.stdout.file.flush()


def default_runtime(*, no_color: bool) -> CliRuntime:
    disabled = no_color or os.environ.get("NO_COLOR", "") != ""
    return CliRuntime(
        stdout=Console(file=sys.stdout, no_color=disabled),
        stderr=Console(file=sys.stderr, stderr=True, no_color=disabled),
        cwd=Path.cwd(),
        load_config=load_config,
        load_lattice=load_lattice,
    )


def get_runtime(ctx: typer.Context) -> CliRuntime:
    if not isinstance(ctx.obj, CliRuntime):
        msg = "CLI runtime was not initialized"
        raise RuntimeError(msg)
    return ctx.obj
```

Include a module docstring and Google-style docstrings for public functions.

- [ ] **Step 6: Implement shared output and error policy**

Implement `OutputSelection(format: str, indent: int | None)` as a frozen, slotted dataclass.
`select_output` must execute in this exact order:

1. reject `fmt` unless it is in `valid`;
2. reject alias plus GitHub format;
3. select JSON when the alias is true, otherwise retain `fmt`;
4. reject non-None indentation unless the effective format is JSON;
5. return `OutputSelection`.

Use `runtime.stderr.print` with escaped dynamic values for diagnostics. Implement
`write_json(runtime, payload, indent)` as `runtime.write_stdout(json.dumps(payload,
indent=indent))`. Move the existing workflow escape and annotation functions without changing
their byte output.

Implement `errors.py` with:

```python
EXIT_FINDING = 1
EXIT_TOOL_ERROR = 2


def print_project_error(runtime: CliRuntime, exc: ProjectError) -> None:
    runtime.stderr.print(
        f"[red]error[/red]: {escape(exception_details(exc))} ({exc.code})",
        soft_wrap=True,
    )


@contextmanager
def exit_on_project_error(runtime: CliRuntime) -> Iterator[None]:
    try:
        yield
    except ProjectError as exc:
        print_project_error(runtime, exc)
        raise typer.Exit(EXIT_TOOL_ERROR) from exc


def print_internal_error(runtime: CliRuntime, exc: Exception) -> None:
    runtime.stderr.print(
        f"[red]internal error[/red]: {type(exc).__name__}: {escape(str(exc))}"
    )
```

`options.py` contains the existing `ConfigOpt`, `JsonOpt`, and `IndentOpt` annotations with byte-
identical help text.

- [ ] **Step 7: Keep tests RED until application exists**

Run the Task 1 tests again. Expected: collection still fails because the existing `cli.py` module
correctly remains authoritative until Task 2 creates the complete package, including
`application.py` and `cli/__init__.py`. Do not weaken that test or delete `cli.py` early.

- [ ] **Step 8: Commit the shared policy slice after Task 2 makes it green**

Do not commit Task 1 alone because introducing a package without an application would shadow the
working `cli.py`. Task 1 and Task 2 form one deployable commit after the GREEN verification below.

## Task 2: Convert the CLI Module into Focused Command Adapters

**Files:**
- Create: `src/doc_lattice/cli/__init__.py`
- Create: `src/doc_lattice/cli/application.py`
- Create: `src/doc_lattice/cli/commands/__init__.py`
- Create: `src/doc_lattice/cli/commands/check.py`
- Create: `src/doc_lattice/cli/commands/lint.py`
- Create: `src/doc_lattice/cli/commands/impact.py`
- Create: `src/doc_lattice/cli/commands/graph.py`
- Create: `src/doc_lattice/cli/commands/reconcile.py`
- Create: `src/doc_lattice/cli/commands/linear.py`
- Create: `src/doc_lattice/cli/commands/init.py`
- Delete: `src/doc_lattice/cli.py`
- Modify: `tests/test_cli.py` (temporary import and patch-target compatibility before Task 3 split)

- [ ] **Step 1: Implement the application factory**

`application.py` must construct a new Typer instance for every factory call and register all seven
adapters:

```python
def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


def create_app(*, runtime_factory: RuntimeFactory = default_runtime) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, add_completion=False)

    @app.callback()
    def main_callback(
        ctx: typer.Context,
        version: Annotated[
            bool,
            typer.Option(
                "--version",
                callback=_version_callback,
                is_eager=True,
                help="Show the version and exit.",
            ),
        ] = False,
        no_color: Annotated[
            bool, typer.Option("--no-color", help="Disable colored output.")
        ] = False,
    ) -> None:
        """doc-lattice: documentation traceability engine."""
        ctx.obj = runtime_factory(no_color=no_color)

    register_check(app)
    register_lint(app)
    register_impact(app)
    register_reconcile(app)
    register_graph(app)
    register_linear(app)
    register_init(app)
    return app


app = create_app()
```

The unused `version` callback parameter retains the existing Ruff suppression.

- [ ] **Step 2: Move check into its adapter**

Move the current `_parse_only_states`, `_filter_statuses`, and `check` behavior into
`commands/check.py`. Expose only `register_check(app)`. The registered callback takes
`ctx: typer.Context` first, resolves output through `select_output`, loads through
`runtime.project` plus `runtime.lattice`, renders human output to `runtime.stdout`, writes JSON and
annotations through `output.py`, and raises exit 1 based on the unfiltered statuses.

Keep the existing validation order: format, indent, state filter, project load, lattice load,
classification, display filtering, output, exit selection.

- [ ] **Step 3: Move lint into its adapter**

Move `lint` into `commands/lint.py` and expose only `register_lint(app)`. Use the same output
selection and project-error helpers as check. Preserve the exact GitHub title/message and exit 1
only when `result.violations` is non-empty.

- [ ] **Step 4: Move impact and graph into adapters**

`commands/impact.py` uses `select_output` with `fmt="human"`, `json_alias=json_out`,
`valid=VALID_BASIC_OUTPUT_FORMATS`, and the supplied indent. It preserves depth validation in the
Typer declaration and always exits 0 after successful output.

`commands/graph.py` uses `select_output` with `json_alias=False`, `valid=VALID_GRAPH_FORMATS`, and
no indent. Preserve JSON's trailing newline and DOT/Mermaid's lack of a trailing newline by calling
`runtime.write_stdout(text, newline=False)` for the latter two.

- [ ] **Step 5: Move reconcile into its adapter without changing phase order**

Move `_reconcile_json_payload`, `_print_reconcile_lines`, `_resolve_reconcile_write_paths`,
`_report_reconcile`, `_report_recovery`, and `reconcile` into `commands/reconcile.py`. Make every
report helper take `runtime: CliRuntime`. Replace direct loader calls as follows:

```python
project = runtime.project(config)
lattice = runtime.lattice(
    project,
    require_verified=True,
    persist_cache=not dry_run,
)
```

Keep selector validation before project loading. Keep explicit recovery under the lock and return
without loading. Keep dry-run safety before loading. Keep automatic real-run recovery before
loading. Keep containment before fresh reads, commit inside the lock, and all success reporting
after clean lock exit. Use `select_output` with the basic human/JSON set so reconcile shares alias
policy without gaining `--format`.

- [ ] **Step 6: Move Linear and init into adapters**

`commands/linear.py` retains `_validate_indent` behavior through `select_output`, preserves the
positional target/`--from` conflict before loading, and retains the exact DANGER/BLOCKED/WARNING
gate semantics. Patchable `fetch_tickets` remains a module import in this adapter.

`commands/init.py` retains `_validate_init_flags`, create-only persistence, flattened notes,
stdout snippet ordering, stderr wording, and explicit exit 0. Replace every `Path.cwd()` with
`runtime.cwd`. Patchable `atomic_create_bytes` remains a module import in this adapter.

- [ ] **Step 7: Implement lazy compatibility exports and main**

`cli/__init__.py` must not import Typer at module import time. Implement:

```python
__all__ = ["app", "main"]


def _load_app() -> object:
    cached = globals().get("app")
    if cached is not None:
        return cached
    from .application import app as application

    globals()["app"] = application
    return application


def __getattr__(name: str) -> object:
    if name == "app":
        return _load_app()
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def main() -> None:
    no_color = "--no-color" in sys.argv[1:] or os.environ.get("NO_COLOR", "") != ""
    if no_color:
        os.environ["NO_COLOR"] = "1"
        os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

    application = _load_app()
    if not callable(application):
        msg = "CLI application is not callable"
        raise RuntimeError(msg)

    try:
        application()
    except ProjectError as exc:
        print_project_error(default_runtime(no_color=no_color), exc)
        raise SystemExit(EXIT_TOOL_ERROR) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        print_internal_error(default_runtime(no_color=no_color), exc)
        raise SystemExit(EXIT_TOOL_ERROR) from exc
```

Import errors/runtime only after no-color environment setup. Intended `SystemExit` remains
uncaught. When tests assign `doc_lattice.cli.app`, `_load_app` uses that replacement.

- [ ] **Step 8: Update temporary test patch targets**

Until Task 3 reorganizes the suite, update `tests/test_cli.py` imports:

```python
import doc_lattice.cli.commands.init as init_command
import doc_lattice.cli.commands.linear as linear_command
import doc_lattice.cli.commands.reconcile as reconcile_command
import doc_lattice.cli.runtime as runtime_module
```

Replace `cli_mod.fetch_tickets`, `atomic_create_bytes`, `commit_rewrites`, and `reconcile_lock`
patches with the owning adapter module. Replace loader patches with `runtime_module.load_lattice`.
Replace the former global-console color test with an app created from an injected runtime factory;
assert two invocations receive distinct consoles and that the no-color invocation contains no ANSI.

- [ ] **Step 9: Run focused tests and verify GREEN**

Run:

```bash
uv run --group dev pytest --no-cov tests/cli/test_runtime.py tests/cli/test_output.py tests/test_cli.py -q
```

Expected: all CLI tests pass. Confirm the old `tests/test_cli.py` count plus the new runtime/output
tests and no warnings or ANSI regressions.

- [ ] **Step 10: Run source checks for the package slice**

Run:

```bash
uv run --group dev ruff check src/doc_lattice/cli src/doc_lattice/constants.py tests/cli tests/test_cli.py
uv run --group dev ruff format --check src/doc_lattice/cli src/doc_lattice/constants.py tests/cli tests/test_cli.py
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
rg -n '^_out\s*=|^_err\s*=|global _out|global _err|COLOR_SYSTEM\s*=' src/doc_lattice/cli
```

Expected: all four checks exit 0 and the source scan prints nothing.

- [ ] **Step 11: Commit the deployable source conversion**

```bash
git add src/doc_lattice/cli.py src/doc_lattice/cli src/doc_lattice/constants.py \
  tests/cli tests/test_cli.py
git commit -m "refactor: decompose CLI orchestration"
```

## Task 3: Organize CLI Tests by Command

**Files:**
- Create: `tests/cli/helpers.py`
- Create: `tests/cli/test_check.py`
- Create: `tests/cli/test_lint.py`
- Create: `tests/cli/test_impact.py`
- Create: `tests/cli/test_graph.py`
- Create: `tests/cli/test_reconcile.py`
- Create: `tests/cli/test_linear.py`
- Create: `tests/cli/test_init.py`
- Create: `tests/cli/test_contract.py`
- Modify: `tests/cli/test_runtime.py`
- Modify: `tests/cli/test_output.py`
- Delete: `tests/test_cli.py`

- [ ] **Step 1: Create shared test helpers**

Move the existing helper implementations without semantic changes:

- `_tree_snapshot`, `_write_cli_transaction`, `_two_downstream_project`, and reconcile constants
  into helpers used by reconcile tests;
- `_run`, `_run_cli_subprocess`, and the shared `CliRunner` into `helpers.py`;
- `_chain_docs`, `_clean_docs`, `_fake_fetch`, `_ticket`, and `_write_lint_docs` into `helpers.py`.

Rename helpers without a leading underscore when imported across modules. Add `tests/cli/__init__.py`
so command tests use explicit relative imports from `.helpers`.

- [ ] **Step 2: Split command tests with no lost assertions**

Move tests by behavior:

- `test_check.py`: all `test_check_*` cases and check-specific exact human/JSON/GitHub cases.
- `test_lint.py`: all `test_lint_*` cases and lint-specific format cases.
- `test_impact.py`: all `test_impact_*` cases and depth/indent cases scoped to impact.
- `test_graph.py`: all `test_graph_*` cases.
- `test_reconcile.py`: all `test_reconcile_*` cases, including recovery, transaction, cache-byte
  parity, containment, dry-run, verified loading, and delayed success.
- `test_linear.py`: all `test_linear_*` cases and ticket helpers.
- `test_init.py`: all `test_init_*` cases and persistence-note cases.

Keep parameterized cases spanning multiple commands in `test_contract.py` rather than duplicating
them.

- [ ] **Step 3: Build the concise integration contract suite**

`test_contract.py` contains only cross-command or entry-point contracts:

- import when `fcntl` is unavailable;
- `--version`, global help, command help, and option help text;
- explicit flag and environment no-color subprocess cases;
- all lattice-loading commands map unclosed frontmatter to exit 2;
- all JSON-capable offline commands enforce indent ordering and round-trip JSON;
- main maps project/internal errors to exit 2 and preserves intended SystemExit;
- cached and uncached CLI output parity;
- representative exact stdout/stderr and exit code checks across command families.

Target fewer than 350 lines for this file. Command edge cases do not belong here.

- [ ] **Step 4: Verify collection parity**

Before deleting `tests/test_cli.py`, record its collected test count:

```bash
uv run --group dev pytest --no-cov --collect-only -q tests/test_cli.py
```

After the split, run:

```bash
uv run --group dev pytest --no-cov --collect-only -q tests/cli
```

Expected: the new count is at least the old count plus the new runtime/output tests. If lower,
compare `rg '^def test_'` names and restore every missing case before proceeding.

- [ ] **Step 5: Run the split CLI suite**

```bash
uv run --group dev pytest --no-cov tests/cli -q
```

Expected: all tests pass with no collection warnings.

- [ ] **Step 6: Run test formatting and lint checks**

```bash
uv run --group dev ruff check tests/cli
uv run --group dev ruff format --check tests/cli
```

Expected: both exit 0.

- [ ] **Step 7: Commit the test organization**

```bash
git add tests/test_cli.py tests/cli
git commit -m "test: organize CLI contracts by command"
```

## Task 4: Document Architecture and JSON Compatibility

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CHANGELOG.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update README command policy**

Immediately after the existing `--json`/`--format` explanation, add:

```markdown
`--json` is a silent compatibility alias throughout the 1.x series. It does not emit a
deprecation warning, so existing scripts keep identical stderr behavior. In the next breaking
release, output-producing commands will use `--format` consistently and the `--json` alias will be
removed. Commands will retain their own valid format sets, such as `human|json|github` for report
gates and `mermaid|dot|json` for `graph`.
```

- [ ] **Step 2: Record the accepted CLI architecture**

Add an architecture decision after AD-2 documenting:

- `doc_lattice.cli` is an impure package boundary;
- a fresh `CliRuntime` owns stdout, stderr, cwd, and loading dependencies per invocation;
- command adapters have no shared mutable console state;
- output selection and project-error mapping each have one owner;
- the package preserves `doc_lattice.cli:main` and the Typer `app` compatibility export.

Update the system overview and AD-2 references from singular `cli` module wording to the package.

- [ ] **Step 3: Update contributor inventory and changelog**

In `CLAUDE.md`, replace the single-module CLI inventory with runtime/output/error/application and
command-adapter ownership. Preserve the rule that reconcile orchestration performs containment and
delegates durable mutation to `reconcile_transaction.py`.

In `CHANGELOG.md` under Unreleased Changed, add:

```markdown
- Internal: CLI orchestration now uses an injected per-invocation runtime, focused command
  adapters, and centralized output/error policy instead of mutable module-level consoles. CLI tests
  now mirror commands. The silent 1.x `--json` compatibility alias and its 2.0 removal path are
  documented; runtime behavior is unchanged.
```

- [ ] **Step 4: Verify documentation consistency**

Run:

```bash
rg -n 'cli\.py|module-level console|--json' README.md ARCHITECTURE.md CLAUDE.md CHANGELOG.md
uv run --group dev python scripts/check_version_sync.py
git diff --check
```

Expected: remaining `cli.py` references are historical and accurate, the new JSON policy is visible,
version sync exits 0, and the diff check is clean.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md ARCHITECTURE.md CHANGELOG.md CLAUDE.md
git commit -m "docs: define CLI output compatibility policy"
```

## Task 5: Full Verification, Independent Review, and PR Readiness

**Files:**
- Modify: any files required by verified review findings

- [ ] **Step 1: Run the full repository verification matrix**

Run every command fresh and inspect its exit code:

```bash
uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
uv run --group dev python scripts/generate_github_slugger_data.py --check
git diff --check origin/main...HEAD
```

Expected: 0 failures, coverage at least 80 percent, and every command exits 0.

- [ ] **Step 2: Audit acceptance criteria against current evidence**

Run:

```bash
rg -n '^_out\s*=|^_err\s*=|global _out|global _err|COLOR_SYSTEM\s*=' src/doc_lattice/cli
find src/doc_lattice/cli/commands -maxdepth 1 -type f -name '*.py' -print | sort
find tests/cli -maxdepth 1 -type f -name 'test_*.py' -print | sort
rg -n 'silent compatibility alias|next breaking release|2\.0' README.md ARCHITECTURE.md CHANGELOG.md
```

Expected: no mutable console/global-color matches; seven command adapters; command-mirrored tests
plus runtime/output/contract tests; and explicit JSON compatibility documentation.

- [ ] **Step 3: Dispatch independent spec compliance review**

Give a fresh reviewer the issue acceptance criteria, approved design, implementation plan, and
`origin/main..HEAD` diff. Require file-and-line evidence for missing or extra behavior. If issues
are found, the implementer fixes them and the same reviewer re-reviews until spec compliant.

- [ ] **Step 4: Dispatch independent code quality review**

Only after spec compliance passes, give another fresh reviewer the same diff and require review of
runtime isolation, lazy import/color behavior, adapter dependencies, output byte compatibility,
reconcile phase order, test quality, and documentation. Fix every Critical and Important finding,
then re-run that reviewer until approved.

- [ ] **Step 5: Re-run verification after review fixes**

Repeat the complete Step 1 matrix after the last review fix. Prior green runs do not prove the
reviewed revision.

- [ ] **Step 6: Inspect final scope and commit any review fixes**

```bash
git status --short
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
git log --oneline origin/main..HEAD
```

Stage only issue #89 files and commit review fixes with a terse message. The tree must be clean
before publishing.

- [ ] **Step 7: Push and open a draft PR**

Push `refactor/cli-orchestration` with tracking and open a draft PR targeting the remote default
branch. The PR body must cover what changed, why the mutable global state was unsafe, preserved
behavior, the 1.x/2.0 JSON policy, test organization, independent review, and the complete
verification matrix. Include `Closes #89`.
