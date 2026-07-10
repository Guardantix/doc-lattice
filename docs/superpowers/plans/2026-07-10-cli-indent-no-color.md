# CLI JSON Indentation and Color Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in pretty-printed JSON to four report commands and an explicit global color-disable
flag without changing default CLI behavior.

**Architecture:** Keep the change inside the CLI presentation boundary. A shared `IndentOpt` and
validation helper enforce one contract for the four requested commands, while `_disable_color()`
replaces the module consoles before command dispatch. Existing payload construction and report
renderers remain unchanged.

**Tech Stack:** Python 3.13+, Typer, Rich, pytest, uv, Ruff, ty.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/game_lattice/cli.py` | Define both options, validate JSON indentation, and configure consoles. |
| `tests/test_cli.py` | Prove formatted JSON, misuse errors, help text, color suppression, and defaults. |
| `README.md` | Document the two flags and the `NO_COLOR` environment variable. |
| `CHANGELOG.md` | Record issue #20 under `[Unreleased]`. |

### Task 1: Add indentation to `check`

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `src/game_lattice/cli.py`

- [ ] **Step 1: Write failing `check` indentation tests**

Add these tests beside the existing `check --json` cases:

```python
def test_check_json_indent_round_trips_to_compact_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, ["check", "--json"])
    pretty = runner.invoke(app, ["check", "--json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == 1
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert '\n  "edges": [\n' in pretty.stdout


def test_check_indent_without_json_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--indent", "2"])
    assert result.exit_code == 2
    assert "--indent requires --json" in result.stderr


def test_check_negative_indent_is_rejected(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json", "--indent", "-1"])
    assert result.exit_code == 2
```

- [ ] **Step 2: Run the tests and verify red**

Run:

```bash
uv run --group dev pytest tests/test_cli.py -k "check and indent" -v
```

Expected: the new tests fail because `check` does not recognize `--indent`, and the misuse error
does not say that `--json` is required.

- [ ] **Step 3: Add the shared option, validator, and `check` wiring**

Beside `JsonOpt`, add:

```python
IndentOpt = Annotated[
    int | None,
    typer.Option(
        "--indent", min=0, help="Pretty-print JSON with this indent (requires --json)."
    ),
]
```

Before `_parse_only_states`, add:

```python
def _validate_indent(indent: int | None, *, json_out: bool) -> None:
    """Reject JSON indentation when JSON output is disabled."""
    if indent is not None and not json_out:
        _err.print("[red]error[/red]: --indent requires --json")
        raise typer.Exit(2)
```

Add `indent: IndentOpt = None` to `check`, call
`_validate_indent(indent, json_out=json_out)` before `_parse_only_states`, and change its JSON dump
to:

```python
typer.echo(json.dumps(payload, indent=indent))
```

- [ ] **Step 4: Run the focused tests and verify green**

Run:

```bash
uv run --group dev pytest tests/test_cli.py -k "check and indent" -v
```

Expected: all selected indentation tests pass, including the compact-payload equality assertion.

- [ ] **Step 5: Commit the first TDD slice**

```bash
git add tests/test_cli.py src/game_lattice/cli.py
git commit -m "feat(cli): indent check JSON output"
```

### Task 2: Apply indentation to `lint`, `impact`, and `linear`

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `src/game_lattice/cli.py`

- [ ] **Step 1: Write failing remaining-command tests**

Add these tests near the existing impact and linear cases:

```python
@pytest.mark.parametrize(
    "args",
    [
        ["lint"],
        ["impact", "art-direction#accent"],
        ["linear"],
    ],
)
def test_indent_without_json_exits_2_before_project_loading(tmp_path: Path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [*args, "--indent", "2"])
    assert result.exit_code == 2
    assert "--indent requires --json" in result.stderr


@pytest.mark.parametrize(
    ("args", "expected_exit"),
    [
        (["lint"], 0),
        (["impact", "art-direction#accent"], 0),
    ],
)
def test_offline_json_indent_round_trips(lattice_dir: Path, monkeypatch, args, expected_exit):
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, [*args, "--json"])
    pretty = runner.invoke(app, [*args, "--json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == expected_exit
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert "\n  " in pretty.stdout
```

Add this test beside the existing linear JSON tests, where `_ticket` and `_fake_fetch` are already
defined:

```python
def test_linear_json_indent_round_trips(lattice_dir: Path, monkeypatch):
    ticket = _ticket(TicketState(name="Done", type="completed"))
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    compact = runner.invoke(app, ["linear", "--json"])
    pretty = runner.invoke(app, ["linear", "--json", "--indent", "2"])
    assert compact.exit_code == pretty.exit_code == 0
    assert json.loads(pretty.stdout) == json.loads(compact.stdout)
    assert '\n  "findings": [\n' in pretty.stdout
```

- [ ] **Step 2: Run the tests and verify red**

Run:

```bash
uv run --group dev pytest tests/test_cli.py -k "indent and not check" -v
```

Expected: the new tests fail because the three commands do not recognize `--indent`, and misuse
does not reach the shared validator.

- [ ] **Step 3: Wire the remaining three commands**

Add `indent: IndentOpt = None` to `lint`, `impact`, and `linear`. At the start of each function,
call:

```python
_validate_indent(indent, json_out=json_out)
```

Change their JSON emission sites to:

```python
typer.echo(json.dumps(payload, indent=indent))
typer.echo(json.dumps(findings_json(findings), indent=indent))
```

The first form applies to the local `payload` in `lint` and `impact`; the second applies to
`linear`. Leave `reconcile` and `graph` unchanged.

- [ ] **Step 4: Run all indentation tests and verify green**

Run:

```bash
uv run --group dev pytest tests/test_cli.py -k indent -v
```

Expected: every indentation test passes for all four commands.

- [ ] **Step 5: Commit the completed indentation feature**

```bash
git add tests/test_cli.py src/game_lattice/cli.py
git commit -m "feat(cli): indent JSON reports"
```

### Task 3: Add explicit global color suppression

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `src/game_lattice/cli.py`

- [ ] **Step 1: Write failing color and help tests**

Import `Console` from `rich.console`, then add:

```python
def test_no_color_suppresses_forced_ansi(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    monkeypatch.setattr(cli_mod, "_out", Console(force_terminal=True, color_system="standard"))
    colored = runner.invoke(app, ["check"])
    assert colored.exit_code == 1
    assert "\x1b[" in colored.stdout

    monkeypatch.setattr(cli_mod, "_out", Console(force_terminal=True, color_system="standard"))
    plain = runner.invoke(app, ["--no-color", "check"])
    assert plain.exit_code == 1
    assert "\x1b[" not in plain.stdout


def test_global_help_lists_no_color():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--no-color" in result.stdout


@pytest.mark.parametrize("command", ["check", "lint", "impact", "linear"])
def test_json_commands_help_lists_indent(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert "--indent" in result.stdout
    assert "requires --json" in result.stdout
```

- [ ] **Step 2: Run the tests and verify red**

Run:

```bash
uv run --group dev pytest tests/test_cli.py -k "no_color or help_lists" -v
```

Expected: the forced-color control assertion passes, while `--no-color` and global help fail
because the option does not exist. The command-help test already passes after Tasks 1 and 2.

- [ ] **Step 3: Implement `_disable_color` and the global option**

Before `_version_callback`, add:

```python
def _disable_color() -> None:
    """Replace the CLI consoles with explicit no-color consoles."""
    global _out, _err
    _out = Console(no_color=True)
    _err = Console(stderr=True, no_color=True)
```

Extend `main_callback` with the prescribed option and body:

```python
def main_callback(
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
    """game-lattice: documentation traceability engine."""
    if no_color:
        _disable_color()
```

- [ ] **Step 4: Run focused and byte-identity tests**

Run:

```bash
uv run --group dev pytest tests/test_cli.py -k "no_color or help_lists or byte_identical" -v
```

Expected: every selected test passes; the existing default human output remains byte-identical.

- [ ] **Step 5: Commit the color-control slice**

```bash
git add tests/test_cli.py src/game_lattice/cli.py
git commit -m "feat(cli): add explicit no-color output"
```

### Task 4: Update user documentation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update README command documentation**

After the paragraph that lists commands accepting `--json`, add:

```markdown
Pass `--indent N` with `--json` on `check`, `lint`, `impact`, or `linear` to pretty-print the JSON
with `N` spaces per level. `--indent` without `--json` is a usage error.

Use the global `--no-color` option before the command to disable colored output explicitly, for
example `game-lattice --no-color check`. Rich also honors the [`NO_COLOR`](https://no-color.org/)
environment variable; `--no-color` is the command-line equivalent.
```

- [ ] **Step 2: Add the CHANGELOG entry**

Under `## [Unreleased]`, add an `### Added` section before `### Changed`:

```markdown
### Added

- `check`, `lint`, `impact`, and `linear` accept `--indent N` with `--json`, and the global
  `--no-color` option explicitly disables colored output (#20).
```

- [ ] **Step 3: Run documentation convention checks**

Run:

```bash
uv run --group dev pytest tests/test_conventions.py tests/test_version_check.py -v
git diff --check
```

Expected: all selected tests pass and `git diff --check` emits no output.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document JSON indent and color control"
```

### Task 5: Verify, review, and publish

**Files:**
- Review all changes from `d1b028d` through `HEAD`.

- [ ] **Step 1: Run the focused CLI suite**

```bash
uv run --group dev pytest tests/test_cli.py -v
```

Expected: every CLI test passes.

- [ ] **Step 2: Run every issue-mandated quality gate**

```bash
uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
```

Expected: every command exits zero; pytest reports at least 80 percent coverage.

- [ ] **Step 3: Perform `/review` against the branch base**

Inspect `git diff d1b028d...HEAD`, validate every issue acceptance criterion against tests and
runtime behavior, and classify each finding by correctness and impact. Fix every valid finding at
its root cause using a new failing regression test before production changes, then rerun Step 2.

- [ ] **Step 4: Commit any review fixes**

```bash
git add src/game_lattice/cli.py tests/test_cli.py README.md CHANGELOG.md
git commit -m "fix: address CLI option review findings"
```

Skip this commit when review finds no valid issues.

- [ ] **Step 5: Push and open the pull request**

Push `feat/cli-indent-no-color`, then open a draft pull request that links `#20`, summarizes both
options and documentation, and pastes the exact quality-gate output. Verify the returned PR URL,
head branch, base branch, changed files, and open state before reporting completion.
