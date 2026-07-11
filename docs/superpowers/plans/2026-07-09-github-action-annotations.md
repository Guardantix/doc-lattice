# GitHub Actions Annotation Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in GitHub Actions workflow-command output to `check` and `lint` while preserving existing human and JSON contracts and gate exit codes.

**Architecture:** Keep output selection and rendering in `cli.py`, where these commands currently own their human and JSON forms. Add two CLI-local escape helpers plus one format resolver, then branch each command into JSON, GitHub, and existing human output without changing the pure check or lint layers.

**Tech Stack:** Python 3.13+, Typer, Rich for existing human and error output, pytest with `CliRunner`, uv, ruff, ty.

---

**Binding spec:** `docs/superpowers/specs/2026-07-09-github-action-annotations-design.md` and GitHub issue #18. When current implementation and either requirement source disagree, the issue and design win.

**File map:**

- `src/doc_lattice/cli.py`: validate output selection, escape workflow-command fields, and render check and lint annotations.
- `src/doc_lattice/constants.py`: define the typed report-format domain and runtime validation set.
- `tests/test_cli.py`: unit-test both escaping rules and exercise every new CLI contract against real loaded lattices.
- `tests/test_constants.py`: keep the runtime report-format set tied to its `Literal` domain.
- `README.md`: document both `--format` options, `--json` compatibility, annotations, and unchanged exits.
- `CHANGELOG.md`: record the user-visible feature under `## [Unreleased]`.

### Task 1: Escape helpers and format selection

**Files:**

- Modify: `tests/test_cli.py`
- Modify: `tests/test_constants.py`
- Modify: `src/doc_lattice/cli.py`
- Modify: `src/doc_lattice/constants.py`

- [ ] **Step 1: Write direct failing tests for both escape rules**

Extend the `doc_lattice.cli` import in `tests/test_cli.py` and add:

```python
from doc_lattice.cli import (
    _STATE_COLORS,
    _escape_github_message,
    _escape_github_property,
    app,
)


def test_escape_github_message_encodes_workflow_command_metacharacters():
    assert _escape_github_message("100%\rfirst\nsecond: a,b") == (
        "100%25%0Dfirst%0Asecond: a,b"
    )


def test_escape_github_property_encodes_message_and_property_metacharacters():
    assert _escape_github_property("100%\rfirst\nsecond: a,b") == (
        "100%25%0Dfirst%0Asecond%3A a%2Cb"
    )


def test_report_formats_match_literal():
    assert frozenset(get_args(ReportFormat)) == VALID_REPORT_FORMATS
    assert {"human", "json", "github"} == set(VALID_REPORT_FORMATS)
```

- [ ] **Step 2: Run the helper tests and verify RED**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_cli.py tests/test_constants.py \
  -k "escape_github or report_formats" -v
```

Expected: collection fails because the private helpers and typed report-format constants do not
exist.

- [ ] **Step 3: Implement the minimal escape helpers and format resolver**

Add the typed domain to `src/doc_lattice/constants.py`:

```python
ReportFormat = Literal["human", "json", "github"]
VALID_REPORT_FORMATS: frozenset[str] = frozenset(get_args(ReportFormat))
```

Import `ReportFormat` and `VALID_REPORT_FORMATS` in `src/doc_lattice/cli.py`, then add below
`_STATE_COLORS`:

```python
def _escape_github_message(value: str) -> str:
    """Escape a GitHub workflow-command message value."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_github_property(value: str) -> str:
    """Escape a GitHub workflow-command property value."""
    return _escape_github_message(value).replace(":", "%3A").replace(",", "%2C")


def _resolve_report_format(fmt: str, json_out: bool) -> ReportFormat:
    """Validate output flags and return the effective report format.

    Args:
        fmt: Explicit ``--format`` value.
        json_out: Whether the legacy ``--json`` alias was supplied.

    Returns:
        ``human``, ``json``, or ``github``.

    Raises:
        typer.Exit: Exit code 2 for a conflicting or unsupported selection.
    """
    if json_out and fmt == "github":
        _err.print("[red]error[/red]: --json cannot be combined with --format github")
        raise typer.Exit(2)
    if json_out:
        return "json"
    if fmt == "human":
        return "human"
    if fmt == "json":
        return "json"
    if fmt == "github":
        return "github"
    valid = ", ".join(sorted(VALID_REPORT_FORMATS))
    _err.print(f"[red]error[/red]: --format {escape(f'{fmt!r}')} must be one of: {valid}")
    raise typer.Exit(2)
```

- [ ] **Step 4: Run the helper tests and verify GREEN**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_cli.py tests/test_constants.py \
  -k "escape_github or report_formats" -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit the first green slice**

```bash
git add src/doc_lattice/cli.py src/doc_lattice/constants.py tests/test_cli.py tests/test_constants.py
git commit -m "feat: add GitHub annotation escaping"
```

### Task 2: Check annotation output

**Files:**

- Modify: `tests/test_cli.py`
- Modify: `src/doc_lattice/cli.py`

- [ ] **Step 1: Write failing check CLI tests**

Add tests covering exact fixture output, JSON compatibility, invalid selection, and clean output:

```python
def test_check_github_emits_each_drift_annotation(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "github"])

    assert result.exit_code == 1
    assert result.stdout == (
        f"::error file={lattice_dir / 'docs/gdd.md'},title=doc-lattice BROKEN::"
        "gdd -> ghost is BROKEN\n"
        f"::error file={lattice_dir / 'docs/pc-design.md'},title=doc-lattice STALE::"
        "pc-design -> art-direction#accent is STALE\n"
        f"::error file={lattice_dir / 'docs/pc-design.md'},title=doc-lattice UNRECONCILED::"
        "pc-design -> art-direction#motion is UNRECONCILED\n"
    )


def test_check_github_suppresses_ok_edges(tmp_path: Path, monkeypatch):
    _clean_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["reconcile", "down"]).exit_code == 0

    result = runner.invoke(app, ["check", "--format", "github"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_check_format_json_matches_json_alias(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    alias = runner.invoke(app, ["check", "--json"])
    explicit = runner.invoke(app, ["check", "--format", "json"])

    assert explicit.exit_code == alias.exit_code == 1
    assert explicit.stdout == alias.stdout


def test_check_rejects_json_github_conflict(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json", "--format", "github"])

    assert result.exit_code == 2
    assert "--json" in result.stderr
    assert "--format github" in result.stderr


def test_check_rejects_unknown_format(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--format", "nonsense"])

    assert result.exit_code == 2
    assert "nonsense" in result.stderr
    assert "human" in result.stderr
    assert "json" in result.stderr
    assert "github" in result.stderr
```

- [ ] **Step 2: Run the check format tests and verify RED**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_cli.py -k "check and (github or format)" -v
```

Expected: failures because `check` does not accept `--format`.

- [ ] **Step 3: Implement check format selection and annotation rendering**

Add the prescribed option to `check`, resolve it before loading, switch the JSON condition to the
effective format, and add a GitHub branch:

```python
def check(
    config: ConfigOpt = None,
    json_out: JsonOpt = False,
    fmt: Annotated[str, typer.Option("--format", help="human, json, or github.")] = "human",
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help=(
                "Show only these states (repeatable): OK, STALE, UNRECONCILED, BROKEN. "
                "Filters display only; the exit code always reflects every edge."
            ),
        ),
    ] = None,
) -> None:
    """Classify every edge; exit 1 on drift, 2 on tool error."""
    report_format = _resolve_report_format(fmt, json_out)
    only_states = _parse_only_states(only)
    with _exit_on_project_error():
        lattice = _load(config)
        statuses = check_lattice(lattice)
    displayed = _filter_statuses(statuses, only_states)
    if report_format == "json":
        payload = {
            "edges": [
                {
                    "source_id": status.source_id,
                    "target_ref": status.target_ref,
                    "target_id": status.target_id.as_ref() if status.target_id else None,
                    "state": status.state,
                    "expected": status.expected,
                    "actual": status.actual,
                }
                for status in displayed
            ]
        }
        typer.echo(json.dumps(payload))
    elif report_format == "github":
        for status in displayed:
            if status.state == "OK":
                continue
            path = lattice.nodes_by_id[status.source_id].path
            title = _escape_github_property(f"doc-lattice {status.state}")
            message = _escape_github_message(
                f"{status.source_id} -> {status.target_ref} is {status.state}"
            )
            typer.echo(
                f"::error file={_escape_github_property(str(path))},title={title}::{message}"
            )
    else:
        for status in displayed:
            color = _STATE_COLORS[status.state]
            _out.print(
                f"[{color}]{status.state:<{_STATE_COL_WIDTH}}[/{color}] "
                f"{escape(status.source_id)} -> {escape(status.target_ref)}"
            )
    raise typer.Exit(1 if has_drift(statuses) else 0)
```

Do not change payload keys, ordering, filtering, colors, or exit calculation.

- [ ] **Step 4: Run all check tests and verify GREEN**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_cli.py -k "check" -v
```

Expected: all selected tests pass, including pre-existing human byte-identity and `--only` tests.

- [ ] **Step 5: Commit the check slice**

```bash
git add src/doc_lattice/cli.py tests/test_cli.py
git commit -m "feat: emit check GitHub annotations"
```

### Task 3: Lint annotation output

**Files:**

- Modify: `tests/test_cli.py`
- Modify: `src/doc_lattice/cli.py`

- [ ] **Step 1: Write failing lint CLI tests**

Add:

```python
def test_lint_github_emits_each_violation_annotation(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint", "--format", "github"])

    assert result.exit_code == 1
    assert result.stdout == (
        f"::error file={tmp_path / 'docs/down.md'},title=doc-lattice ladder violation::"
        "down (binding) -> up (derived)\n"
    )


def test_lint_github_suppresses_skipped_edges(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--format", "github"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_lint_format_json_matches_json_alias(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    alias = runner.invoke(app, ["lint", "--json"])
    explicit = runner.invoke(app, ["lint", "--format", "json"])

    assert explicit.exit_code == alias.exit_code == 1
    assert explicit.stdout == alias.stdout


@pytest.mark.parametrize("command", ["check", "lint"])
def test_report_commands_reject_json_github_conflict(
    lattice_dir: Path, monkeypatch, command: str
):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, [command, "--json", "--format", "github"])

    assert result.exit_code == 2
    assert "--json" in result.stderr
    assert "--format github" in result.stderr


@pytest.mark.parametrize("command", ["check", "lint"])
def test_report_commands_reject_unknown_format(lattice_dir: Path, monkeypatch, command: str):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, [command, "--format", "nonsense"])

    assert result.exit_code == 2
    assert "nonsense" in result.stderr
```

Consolidate the equivalent check-only conflict and unknown-format tests from Task 2 into these
parameterized tests instead of keeping duplicate coverage.

- [ ] **Step 2: Run the lint format tests and verify RED**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_cli.py -k "lint and (github or format)" -v
```

Expected: failures because `lint` does not accept `--format`.

- [ ] **Step 3: Implement lint format selection and annotation rendering**

Change the signature and output branch while preserving the existing JSON and human bodies:

```python
def lint(
    config: ConfigOpt = None,
    json_out: JsonOpt = False,
    fmt: Annotated[str, typer.Option("--format", help="human, json, or github.")] = "human",
) -> None:
    """Validate the authority ladder; exit 1 on a violation, 2 on tool error."""
    report_format = _resolve_report_format(fmt, json_out)
    with _exit_on_project_error():
        lattice = _load(config)
        result = lint_lattice(lattice)
    if report_format == "json":
        payload = {
            "violations": [
                {
                    "source_id": violation.source_id,
                    "source_authority": violation.source_authority,
                    "target_id": violation.target_id.as_ref(),
                    "target_ref": violation.target_ref,
                    "target_authority": violation.target_authority,
                }
                for violation in result.violations
            ],
            "skipped": [
                {
                    "source_id": skipped.source_id,
                    "target_ref": skipped.target_ref,
                    "target_id": skipped.target_id.as_ref(),
                    "reason": skipped.reason,
                }
                for skipped in result.skipped
            ],
        }
        typer.echo(json.dumps(payload))
    elif report_format == "github":
        for violation in result.violations:
            path = lattice.nodes_by_id[violation.source_id].path
            title = _escape_github_property("doc-lattice ladder violation")
            message = _escape_github_message(
                f"{violation.source_id} ({violation.source_authority}) -> "
                f"{violation.target_ref} ({violation.target_authority})"
            )
            typer.echo(
                f"::error file={_escape_github_property(str(path))},title={title}::{message}"
            )
    else:
        for violation in result.violations:
            _out.print(
                f"[red]VIOLATION[/red]  {escape(violation.source_id)} "
                f"({violation.source_authority}) -> {escape(violation.target_ref)} "
                f"({violation.target_authority})"
            )
        _out.print(_skip_summary(result))
    raise typer.Exit(1 if result.violations else 0)
```

- [ ] **Step 4: Run all CLI tests and verify GREEN**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_cli.py -v
```

Expected: all CLI tests pass.

- [ ] **Step 5: Commit the lint slice**

```bash
git add src/doc_lattice/cli.py tests/test_cli.py
git commit -m "feat: emit lint GitHub annotations"
```

### Task 4: Documentation and acceptance verification

**Files:**

- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update command signatures and format guidance**

Change the README command table entries to:

```markdown
| `check [--only STATE (repeatable)] [--format human\|json\|github]` | Classify every `derives_from` edge as OK / STALE / UNRECONCILED / BROKEN. | 1 on drift, 2 on tool error |
| `lint [--format human\|json\|github]` | Validate the authority ladder (binding > derived > exploratory) over the edges. | 1 on a violation, 2 on tool error |
```

After the existing `--json` paragraph, add:

```markdown
`check` and `lint` also accept `--format human|json|github`. `human` is the default and
`json` is equivalent to the existing `--json` alias. `github` emits one GitHub Actions
`::error` workflow command per drift finding or ladder violation, so findings appear as
pull-request annotations. Output selection never changes gate exit codes. Do not combine
`--json` with `--format github`.
```

- [ ] **Step 2: Add the Unreleased changelog entry**

Insert an Added subsection above Changed when absent:

```markdown
### Added

- `check --format github` and `lint --format github` emit escaped GitHub Actions error
  annotations while preserving the existing gate exit codes; both commands also accept
  `--format human|json`, and `--json` remains the JSON alias (#18).
```

- [ ] **Step 3: Check docs and run the full required verification matrix**

Run each command independently and retain its complete output as evidence:

```bash
git diff --check
uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
```

Expected: every command exits 0, pytest reports coverage at or above 80%, and all quality tools
report success without warnings.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: explain GitHub annotation output"
```

### Task 5: Review, root-cause fixes, and publication

**Files:**

- Inspect all changes from the merge base of `origin/main` through `HEAD`
- Modify only files required by valid review findings

- [ ] **Step 1: Run the requested `/review` against the complete branch diff**

Review correctness, acceptance-criterion coverage, compatibility, escaping order, Rich bypass,
BROKEN-edge path resolution, filtering, exit codes, documentation accuracy, and test quality.
Record every finding with severity and exact file and line evidence.

- [ ] **Step 2: Fix every valid finding through a new red-green cycle**

For each behavioral finding, add a test that fails for the reported root cause, run it to verify
RED, implement the smallest root-cause correction, run the focused test to verify GREEN, then run
the full related test file. Do not patch symptoms or waive valid findings.

- [ ] **Step 3: Re-run the complete verification matrix**

Repeat every command from Task 4 Step 3 after review fixes. Expected: all exit 0 with pristine
output.

- [ ] **Step 4: Commit any review fixes**

```bash
git add src/doc_lattice/cli.py tests/test_cli.py README.md CHANGELOG.md
git commit -m "fix: address GitHub annotation review"
```

Skip the commit only when `/review` produces no valid findings and the worktree is clean.

- [ ] **Step 5: Push and open the pull request**

Push `feat/github-action-annotation`, then open a draft PR against `main` with issue-closing text,
a requirement summary, and the exact verification commands:

```bash
git push -u origin feat/github-action-annotation
```

PR title:

```text
feat: add GitHub Actions annotation output
```

PR body must include `Closes #18`, summarize check and lint annotations plus compatibility, and
list every successful verification command.

- [ ] **Step 6: Audit the final GitHub and local state**

Confirm the PR URL is open, targets `main`, points to the pushed branch head, and references #18.
Confirm `git status --short --branch` is clean. Map every issue acceptance criterion, docs update,
test gate, review outcome, and PR requirement to authoritative evidence before declaring complete.
