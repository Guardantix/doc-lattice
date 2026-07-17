# CI Audit Dynamic Subcommand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make direct dynamic `doc-lattice` subcommands fail closed when static resolution reaches the end of a simple command.

**Architecture:** Preserve `_doc_lattice_subcommand_index` as a provenance-returning resolver and enforce its `ambiguous` result at `_invocation_in_simple_command`, before accepting an absent or exhausted index. This reuses the existing incomplete-scan error path and leaves statically non-executing root forms unchanged.

**Tech Stack:** Python 3.13, pytest, Ruff, ty, pre-commit

---

## File map

- `tests/test_github_ci_shell_scanner.py`: focused scanner regressions and static controls.
- `tests/test_github_ci_audit.py`: end-to-end pull-request audit regression.
- `src/doc_lattice/github_ci/shell_scanner.py`: ambiguity enforcement at invocation classification.

### Task 1: Reproduce exhausted dynamic subcommands

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py:1200`
- Modify: `tests/test_github_ci_audit.py:550`

- [ ] **Step 1: Write the failing scanner regression and static controls**

Add:

```python
@pytest.mark.parametrize(
    "script",
    [
        'CMD=linear; doc-lattice "$CMD"',
        "CMD='reconcile --all'; doc-lattice $CMD",
    ],
    ids=["quoted-scalar", "unquoted-multiple-fields"],
)
def test_direct_doc_lattice_fails_closed_on_exhausted_dynamic_subcommand(script):
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    ["doc-lattice", "doc-lattice --help", "doc-lattice --version"],
    ids=["bare", "root-help", "root-version"],
)
def test_direct_doc_lattice_allows_static_missing_or_nonexecuting_subcommand(script):
    assert direct_doc_lattice_invocations(script) == NONE
```

- [ ] **Step 2: Write the failing PR-audit regression**

Add:

```python
@pytest.mark.parametrize(
    "script",
    [
        'CMD=linear; doc-lattice "$CMD"',
        "CMD='reconcile --all'; doc-lattice $CMD",
    ],
    ids=["quoted-scalar", "unquoted-multiple-fields"],
)
def test_global_audit_fails_closed_on_exhausted_dynamic_subcommand(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        audit_global_workflows((document,))
```

- [ ] **Step 3: Run RED and confirm the bypass**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_fails_closed_on_exhausted_dynamic_subcommand \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_allows_static_missing_or_nonexecuting_subcommand \
  tests/test_github_ci_audit.py::test_global_audit_fails_closed_on_exhausted_dynamic_subcommand -q
```

Expected: the four dynamic-subcommand parameters fail because no `ConfigError` is raised; the
three static controls pass.

### Task 2: Enforce ambiguity before accepting exhaustion

**Files:**
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1468-1477`

- [ ] **Step 1: Move the existing ambiguity guard**

Change `_invocation_in_simple_command` to check provenance before index absence or exhaustion:

```python
def _invocation_in_simple_command(words: list[_ShellWord]) -> _Invocation | None:
    executable = _doc_lattice_command_index(words, 0)
    if executable.index is None:
        return None
    subcommand_resolution = _doc_lattice_subcommand_index(words, executable.index + 1)
    if executable.ambiguous or subcommand_resolution.ambiguous:
        raise _ShellScanIncomplete("command-position expansion cannot be scanned safely")
    if subcommand_resolution.index is None or subcommand_resolution.index >= len(words):
        return None
```

- [ ] **Step 2: Run GREEN**

Run the focused command from Task 1 Step 3.

Expected: all seven parameters pass.

- [ ] **Step 3: Run focused regression modules**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: both modules pass without failures or warnings.

### Task 3: Verify, commit, and push

**Files:**
- Verify: the complete repository and all changed files

- [ ] **Step 1: Run the full test suite with coverage**

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest -q
```

Expected: all tests pass and coverage meets the configured 80% minimum.

- [ ] **Step 2: Run static and repository checks**

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

- [ ] **Step 3: Review the implementation diff**

```bash
git diff --stat HEAD
git diff HEAD -- tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py \
  src/doc_lattice/github_ci/shell_scanner.py
git status --short
```

Expected: only the planned tests, scanner guard ordering, and completed plan checkboxes are
uncommitted.

- [ ] **Step 4: Commit the implementation**

```bash
git add docs/superpowers/plans/2026-07-17-ci-audit-dynamic-subcommand.md \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py \
  src/doc_lattice/github_ci/shell_scanner.py
git commit -m "fix: reject dynamic doc-lattice subcommands"
```

Expected: commit succeeds and all configured pre-commit hooks pass.

- [ ] **Step 5: Push the current branch without force**

```bash
git push origin feature/github-linear-ci-bootstrap-impl
```

Expected: the remote branch advances to include the design, plan, and implementation commits.
