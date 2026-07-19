# uv Tool Launcher Flags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the offline shell scanner recognize documented valueless `uvx` and `uv tool run` options while preserving fail-closed handling for unknown launcher options.

**Architecture:** Add a dedicated immutable tool-run flag set and use it in the existing `uvx` and `uv tool run` launcher descriptors. Keep `uv run` separate, and verify the behavior through direct scanner tests plus end-to-end audit tests.

**Tech Stack:** Python 3.13, pytest, uv, Ruff, ty, pre-commit

---

### Task 1: Specify documented tool-run launcher behavior

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py:1690`
- Modify: `tests/test_github_ci_audit.py:65`
- Modify: `tests/test_github_ci_audit.py:1267`

- [ ] **Step 1: Add the parameterized scanner regression**

Add this test after the existing documented `uv run` flag test:

```python
@pytest.mark.parametrize("launcher", ["uvx", "uv tool run"])
@pytest.mark.parametrize(
    "flag",
    [
        "--compile-bytecode",
        "--lfs",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-index",
        "--no-sources",
        "--refresh",
        "--reinstall",
        "--system-certs",
        "--upgrade",
        "-U",
        "-n",
    ],
)
def test_direct_doc_lattice_invocations_recognizes_documented_uv_tool_run_flags(
    launcher,
    flag,
):
    assert (
        direct_doc_lattice_invocations(
            f"{launcher} {flag} doc-lattice reconcile --dry-run"
        )
        == RECONCILE_DRY
    )
```

- [ ] **Step 2: Add end-to-end audit regressions**

Add this mutating case to the `script` / `expected_code` table:

```python
(
    "uv tool run --refresh doc-lattice reconcile --all",
    "PR_MUTATING_RECONCILE",
),
```

Add the safe case to `test_global_audit_allows_pr_dry_run_reconcile` and extend its IDs:

```python
"uvx --no-index --find-links dist doc-lattice reconcile --dry-run",
```

```python
ids=["direct", "uv-run-all-extras", "uvx-no-index"],
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_recognizes_documented_uv_tool_run_flags tests/test_github_ci_audit.py::test_global_audit_rejects_root_options_and_compound_grammar_on_pr tests/test_github_ci_audit.py::test_global_audit_allows_pr_dry_run_reconcile -q
```

Expected: failures report `unresolved uv launcher option`; this proves the regressions exercise the missing allowlist entries.

### Task 2: Add the dedicated tool-run flag surface

**Files:**
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:197`
- Test: `tests/test_github_ci_shell_scanner.py`
- Test: `tests/test_github_ci_audit.py`

- [ ] **Step 1: Add the immutable tool-run flag set**

Add immediately after `_UV_LAUNCHER_FLAGS`:

```python
_UV_TOOL_RUN_FLAGS = _UV_LAUNCHER_FLAGS | frozenset(
    {
        "--compile-bytecode",
        "--lfs",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-index",
        "--no-sources",
        "--refresh",
        "--reinstall",
        "--system-certs",
        "--upgrade",
        "-U",
        "-n",
    }
)
```

- [ ] **Step 2: Apply the set only to equivalent tool launchers**

Change the two descriptors to:

```python
_UVX_LAUNCHER = _LauncherOptions.build(
    _UVX_OPTIONS_WITH_ARGUMENTS,
    _UV_TOOL_RUN_FLAGS,
    _UV_HELP_OPTIONS | _UV_VERSION_OPTIONS,
)
_UV_TOOL_RUN_LAUNCHER = _LauncherOptions.build(
    _UVX_OPTIONS_WITH_ARGUMENTS,
    _UV_TOOL_RUN_FLAGS,
    _UV_HELP_OPTIONS,
)
```

Leave `_UV_RUN_LAUNCHER` and its `_UV_RUN_FLAGS` unchanged.

- [ ] **Step 3: Run focused tests and verify GREEN**

Run the Task 1 pytest command again.

Expected: all selected tests pass.

- [ ] **Step 4: Verify unknown options remain fail-closed**

Run:

```bash
uv run pytest tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_fails_closed_on_unknown_launcher_option -q
```

Expected: 4 tests pass.

- [ ] **Step 5: Run affected suites and repository checks**

Run:

```bash
uv run pytest tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
uv run ruff check src/doc_lattice/github_ci/shell_scanner.py tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py
uv run ruff format --check src/doc_lattice/github_ci/shell_scanner.py tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py
uv run ty check
uv run pytest -q
pre-commit run --all-files
```

Expected: every command exits 0 with no lint, formatting, type, audit, or regression failures.

### Task 3: Publish and close the review thread

**Files:**
- Add: `docs/superpowers/plans/2026-07-18-uv-tool-launcher-flags.md`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `tests/test_github_ci_audit.py`

- [ ] **Step 1: Inspect and commit the implementation**

Run:

```bash
git diff --check
git diff --stat
git add docs/superpowers/plans/2026-07-18-uv-tool-launcher-flags.md src/doc_lattice/github_ci/shell_scanner.py tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py
git commit -m "fix: support documented uv tool run flags"
```

Expected: the commit succeeds after repository hooks pass.

- [ ] **Step 2: Push the branch**

Run `git push origin feature/github-linear-ci-bootstrap-impl` and verify the remote branch advances to the implementation commit.

- [ ] **Step 3: Reply inline and resolve the thread**

Reply to review comment `PRRC_kwDOTG9dSM7XIiyi` with the implementation commit, behavior summary, and verification results. Then resolve review thread `PRRT_kwDOTG9dSM6SBzsP`.

- [ ] **Step 4: Verify remote completion**

Refetch PR #99 review threads and confirm no unresolved thread remains.
