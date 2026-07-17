# CI Audit Linear Help Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent pull-request audit findings for effective command help while preserving conservative classification for commands that may execute.

**Architecture:** Replace the reconcile-only boolean argument walk with a shared command-disposition classifier. Static per-command option grammar distinguishes non-executing help, non-mutating reconcile dry runs, and policy-sensitive execution without changing the scanner's public invocation tuple.

**Tech Stack:** Python 3.13, Typer/Click option semantics, pytest, Ruff, ty, pre-commit

---

## File map

- `tests/test_github_ci_shell_scanner.py`: focused scanner disposition regressions and conservative controls.
- `tests/test_github_ci_audit.py`: end-to-end PR workflow policy regression.
- `src/doc_lattice/github_ci/shell_scanner.py`: command option grammar and disposition classification.

### Task 1: Add command-help regressions

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py:650-680`
- Modify: `tests/test_github_ci_audit.py:35-130`

- [x] **Step 1: Write the failing scanner tests**

Replace the reconcile-only effective-help test with a command-level non-execution test and add
Linear conservative controls:

```python
@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice linear --help",
        "doc-lattice linear target --format human --indent 2 --help",
        "doc-lattice linear --exit-code --warn-exit --help",
        "doc-lattice reconcile --help",
        "doc-lattice reconcile pc-design --format human --help",
    ],
)
def test_direct_doc_lattice_invocations_ignores_effective_command_help(script):
    assert direct_doc_lattice_invocations(script) == NONE


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice linear --from --help",
        "doc-lattice linear --config --help",
        "doc-lattice linear --format --help",
        "doc-lattice linear --indent --help",
        "doc-lattice linear -- --help",
        'doc-lattice linear "$OPTION" --help',
    ],
)
def test_direct_doc_lattice_invocations_does_not_widen_consumed_linear_help(script):
    assert direct_doc_lattice_invocations(script) == LINEAR
```

Keep the existing consumed reconcile-help controls unchanged.

- [x] **Step 2: Write the failing audit regression**

Add an end-to-end workflow audit test:

```python
def test_global_audit_allows_effective_linear_help_on_pr():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice linear --help
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == set()
```

- [x] **Step 3: Run RED and confirm the finding is reproduced**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_ignores_effective_command_help \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_does_not_widen_consumed_linear_help \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_does_not_widen_consumed_reconcile_help \
  tests/test_github_ci_audit.py::test_global_audit_allows_effective_linear_help_on_pr -q
```

Expected: effective Linear help returns `LINEAR`, effective reconcile help returns
`RECONCILE_DRY`, and audit reports `PR_LINEAR_INVOCATION`; all conservative controls pass.

### Task 2: Implement shared command disposition

**Files:**
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1-230,1456-1515`

- [x] **Step 1: Define the disposition and Linear option grammar**

Import `Enum` and `auto`, then add the Linear option sets beside the existing reconcile grammar:

```python
from enum import Enum, auto

_LINEAR_OPTIONS_WITH_ARGUMENTS = frozenset({"--config", "--format", "--from", "--indent"})
_LINEAR_FLAGS = frozenset({"--exit-code", "--warn-exit"})


class _CommandDisposition(Enum):
    """Describe whether a recognized policy-sensitive command can run."""

    SENSITIVE = auto()
    NON_MUTATING = auto()
    NON_EXECUTING = auto()
```

- [x] **Step 2: Route Linear and reconcile through one classifier**

Replace the command-specific branch in `_invocation_in_simple_command` with:

```python
    if subcommand.literal == "linear":
        disposition = _classify_command_disposition(
            arguments,
            options_with_arguments=_LINEAR_OPTIONS_WITH_ARGUMENTS,
            flags=_LINEAR_FLAGS,
        )
    elif subcommand.literal == "reconcile":
        disposition = _classify_command_disposition(
            arguments,
            options_with_arguments=_RECONCILE_OPTIONS_WITH_ARGUMENTS,
            flags=_RECONCILE_FLAGS,
            non_mutating_options=frozenset({"--dry-run"}),
        )
    else:
        disposition = _CommandDisposition.SENSITIVE
    if disposition is _CommandDisposition.NON_EXECUTING:
        return None
    return subcommand.literal, disposition is _CommandDisposition.NON_MUTATING
```

- [x] **Step 3: Generalize the bounded option walk**

Replace `_reconcile_is_effectively_non_mutating` with:

```python
def _classify_command_disposition(
    arguments: list[_ShellWord],
    *,
    options_with_arguments: frozenset[str],
    flags: frozenset[str],
    non_mutating_options: frozenset[str] = frozenset(),
) -> _CommandDisposition:
    """Classify a static Typer argv prefix without executing the command."""
    disposition = _CommandDisposition.SENSITIVE
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if _word_may_change_argv(argument):
            return disposition
        literal = argument.literal
        option_name, separator, _value = literal.partition("=")
        if separator and option_name in options_with_arguments:
            index += 1
            continue
        if literal == "--help":
            return _CommandDisposition.NON_EXECUTING
        if literal in non_mutating_options:
            disposition = _CommandDisposition.NON_MUTATING
            index += 1
            continue
        if literal == "--":
            return disposition
        if literal in options_with_arguments:
            value_index = index + 1
            if value_index >= len(arguments) or _word_may_change_argv(arguments[value_index]):
                return disposition
            index += 2
            continue
        if literal in flags:
            index += 1
            continue
        if literal.startswith("-"):
            return disposition
        index += 1
    return disposition
```

- [x] **Step 4: Run GREEN**

Run the focused command from Task 1 Step 3.

Expected: all focused tests pass.

- [x] **Step 5: Run focused regression suites**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: all scanner and CI-audit tests pass.

### Task 3: Verify, audit, commit, and push

**Files:**
- Verify: all changed files and repository gates

- [x] **Step 1: Run the full test suite**

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest -q
```

Expected: all tests pass and coverage remains above the configured 80% threshold.

- [x] **Step 2: Run static and repository checks**

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff format --check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ty check src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_typing_boundaries.py src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_version_sync.py
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pre-commit run --all-files
git diff --check
```

Expected: every command exits 0 with no findings or formatting changes.

- [x] **Step 3: Audit the implementation diff against the design**

Run:

```bash
git diff --stat HEAD
git diff HEAD -- tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py \
  src/doc_lattice/github_ci/shell_scanner.py
git status --short
```

Expected: only the planned tests and scanner implementation are uncommitted; the diff proves
effective help is non-executing and conservative controls remain policy-sensitive.

- [x] **Step 4: Commit the implementation**

```bash
git add tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py \
  src/doc_lattice/github_ci/shell_scanner.py
git commit -m "fix: treat effective Linear help as non-executing"
```

Expected: commit succeeds and all configured pre-commit hooks pass.

- [x] **Step 5: Push the current branch without force**

```bash
git push origin feature/github-linear-ci-bootstrap-impl
```

Expected: the remote branch advances to include the design, plan, and implementation commits.
