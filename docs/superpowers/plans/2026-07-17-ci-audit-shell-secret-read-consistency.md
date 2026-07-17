# CI Audit Shell, Secret, and Read Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close effective-shell, whole-context-secret, and same-size workflow replacement gaps in the offline GitHub CI audit.

**Architecture:** Normalize shell configuration at the YAML boundary, resolve GitHub shell precedence in the pure audit, and fail closed outside a narrow Bash-compatible grammar. Classify every unquoted `secrets` token in GitHub expressions, and bind bounded reads to matching pre-open, descriptor, and post-read path snapshots.

**Tech Stack:** Python 3.13+, pytest, ruamel.yaml, POSIX stat/fstat metadata, Ruff, `ty`, and `uv`.

---

### Task 1: Normalize and audit the effective pull-request shell

**Files:**
- Modify: `tests/test_github_ci_workflow_parser.py`
- Modify: `tests/test_github_ci_audit.py`
- Modify: `src/doc_lattice/github_ci/model.py`
- Modify: `src/doc_lattice/github_ci/workflow_parser.py`
- Modify: `src/doc_lattice/github_ci/audit.py`

- [ ] **Step 1: Write failing parser and audit regressions**

Add a parser test that supplies workflow, job, and step shell fields and asserts:

```python
assert parsed.default_shell == "workflow-shell {0}"
assert parsed.jobs[0].default_shell == "job-shell {0}"
assert parsed.jobs[0].steps[0].shell == "step-shell {0}"
```

Add parameterized pull-request workflows whose effective shell comes from the step, job defaults,
or workflow defaults and equals `doc-lattice linear --config {0}`. Give each step a configuration
body with no Bash invocation and require exactly `PR_LINEAR_INVOCATION`. Add an explicit `pwsh`
case with `Write-Output safe` and require `ConfigError` containing `unsupported shell semantics`.

- [ ] **Step 2: Run RED**

Run:

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev pytest --no-cov \
  tests/test_github_ci_workflow_parser.py::test_parse_workflow_normalizes_effective_shell_fields \
  tests/test_github_ci_audit.py::test_global_audit_scans_effective_shell_template \
  tests/test_github_ci_audit.py::test_global_audit_fails_closed_on_unsupported_shell_semantics -q
```

Expected: model attribute assertions fail, configured template cases emit no finding, and the
PowerShell case does not raise.

- [ ] **Step 3: Extend the typed boundary model**

Add `shell: str | None` to `WorkflowStep`, `default_shell: str | None` to `WorkflowJob`, and
`default_shell: str | None` to `WorkflowDocument`. Parse defaults with:

```python
def _parse_default_shell(
    raw: Any,
    workflow_path: Path,
    yaml_path: tuple[str, ...],
) -> str | None:
    if raw is None:
        return None
    defaults = _require_mapping(raw, workflow_path, yaml_path)
    if "run" not in defaults:
        return None
    run_defaults = _require_mapping(defaults["run"], workflow_path, (*yaml_path, "run"))
    return _optional_audited_string(
        run_defaults.get("shell"), workflow_path, (*yaml_path, "run", "shell")
    )
```

Populate the three new fields from the root, job, and step mappings without changing unrelated
normalization.

- [ ] **Step 4: Resolve and scan effective shell semantics**

Replace the direct body loop with a helper that computes:

```python
shell = step.shell or job.default_shell or document.default_shell
```

For an explicit shell, replace every `{0}` with `__doc_lattice_script__` and pass the template to
`direct_doc_lattice_invocations`. If it yields any direct invocation, retain that result. Scan the
body only if `_supports_bash_run_body(shell)` accepts exact `bash`/`sh` or a parsed template whose
final argument is exactly `{0}` and whose preceding options are a narrow non-command-string
allowlist. Reject `-c`, dynamic expressions, multiple/missing placeholders, and other interpreters.

For no explicit shell, accept the default only when literal `runs-on` starts with `ubuntu-` or
`macos-`. Otherwise raise:

```python
raise ConfigError(f"{context}: unsupported shell semantics for pull-request run step")
```

If a fully scanned unsupported template itself directly invokes `doc-lattice`, return those
invocations and do not interpret its `run` value as Bash.

- [ ] **Step 5: Run GREEN and surrounding suites**

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev pytest --no-cov \
  tests/test_github_ci_workflow_parser.py tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 2: Reject whole-context and wildcard secret access

**Files:**
- Modify: `tests/test_github_ci_audit.py`
- Modify: `src/doc_lattice/github_ci/audit.py`

- [ ] **Step 1: Write failing expression regressions**

Parameterize these values and require `LINEAR_SECRET_REFERENCE`:

```python
[
    "${{ toJSON(secrets) }}",
    "${{ secrets }}",
    "${{ secrets.* }}",
    "${{ secrets[*] }}",
]
```

Add safe controls for `${{ secrets.RELEASE_TOKEN }}`, `${{ secrets['RELEASE_TOKEN'] }}`, and
`${{ 'secrets' }}`, requiring no finding.

- [ ] **Step 2: Run RED**

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev pytest --no-cov \
  tests/test_github_ci_audit.py::test_global_audit_rejects_unbounded_secret_context_access \
  tests/test_github_ci_audit.py::test_global_audit_allows_static_unrelated_secret_access -q
```

Expected: the whole-context and wildcard cases emit no finding while safe controls pass.

- [ ] **Step 3: Implement bounded expression access classification**

Add a case-insensitive token expression and static access expressions:

```python
_SECRETS_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])", re.IGNORECASE)
_STATIC_SECRET_DOT_RE = re.compile(r"\s*\.\s*[A-Za-z_][A-Za-z0-9_]*")
_STATIC_SECRET_INDEX_RE = re.compile(r"\s*\[\s*'[A-Za-z_][A-Za-z0-9_]*'\s*\]")
```

Walk only `${{ ... }}` spans, tracking single-quoted strings and doubled quote escapes. For every
unquoted token, require one static dot or bracket match and reject a following dot, bracket, or
asterisk. Treat an unterminated expression as extending to the scalar end so unsafe access still
fails closed. Replace `_has_computed_secret_index` with this complete classification in
`_has_linear_secret_reference`.

- [ ] **Step 4: Run GREEN and the complete audit suite**

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev pytest --no-cov \
  tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 3: Bind bounded reads to descriptor and path identity

**Files:**
- Modify: `tests/test_github_ci_audit.py`
- Modify: `src/doc_lattice/github_ci/filesystem.py`

- [ ] **Step 1: Write the failing same-size replacement regression**

Create a safe and unsafe workflow with equal UTF-8 byte length. Open the safe target, atomically
replace its path with the unsafe staged file before returning the old handle, and assert discovery
raises `ConfigError` containing `changed during discovery`:

```python
def _replace_after_open(path: Path, *args, **kwargs):
    handle = real_open(path, *args, **kwargs)
    if path == target and not replaced:
        replacement.replace(target)
    return handle
```

The current implementation reads the safe descriptor and accepts it because both path and size
are unchanged, so the regression must fail before production edits.

- [ ] **Step 2: Run RED**

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev pytest --no-cov \
  tests/test_github_ci_audit.py::test_discover_workflows_rejects_same_size_replacement_after_open -q
```

Expected: FAIL because no `ConfigError` is raised.

- [ ] **Step 3: Preserve and compare stat snapshots**

Store the pre-read `os.stat_result` on `_WorkflowCandidate` and pass it directly for managed
artifact reads. Define:

```python
def _file_snapshot(result: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_mode,
        result.st_nlink,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )
```

Inside `_read_bounded_with_recheck`, keep the descriptor open while taking `os.fstat` snapshots
before and after the bounded read. Require both descriptor snapshots and the final non-following
path stat to equal the pre-read snapshot. Use `wording.changed` for identity mismatches and
`wording.size_changed` for same-identity metadata mismatches. Preserve existing byte-limit,
containment, symlink, and regular-file checks.

- [ ] **Step 4: Run GREEN and filesystem coverage**

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev pytest --no-cov \
  tests/test_github_ci_audit.py tests/test_github_ci_filesystem.py -q
```

Expected: PASS.

### Task 4: Document policy, verify, commit, and push

**Files:**
- Modify: `README.md`
- Modify: all files from Tasks 1 through 3

- [ ] **Step 1: Update the authoritative user contract**

In the `ci audit` limitations paragraph, state that the audit resolves configured shell
precedence, fails closed for unsupported pull-request shell semantics, and rejects whole-context,
wildcard, or computed secret access that cannot be proven static and unrelated.

- [ ] **Step 2: Run focused documentation and diff checks**

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev python \
  scripts/check_version_sync.py
git diff --check
```

Expected: both exit 0.

- [ ] **Step 3: Run the complete production handoff verification**

```bash
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev pytest
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev ruff check src tests
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev ruff format --check src tests
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev ty check src
UV_CACHE_DIR=/tmp/doc_lattice_uv_cache uv run --active --group dev python \
  scripts/check_typing_boundaries.py src
```

Expected: all commands exit 0 with no failures.

- [ ] **Step 4: Audit the final diff against all three findings**

Confirm the diff contains effective step/job/workflow shell resolution, whole-context and wildcard
secret rejection, descriptor/path snapshot validation, the observed-red regression tests, and the
README contract. Confirm no unrelated user changes are included.

- [ ] **Step 5: Commit and push**

```bash
git add README.md src/doc_lattice/github_ci/model.py \
  src/doc_lattice/github_ci/workflow_parser.py src/doc_lattice/github_ci/audit.py \
  src/doc_lattice/github_ci/filesystem.py tests/test_github_ci_workflow_parser.py \
  tests/test_github_ci_audit.py \
  docs/superpowers/plans/2026-07-17-ci-audit-shell-secret-read-consistency.md
git commit -m "fix: close ci audit shell and read consistency gaps"
git push origin feature/github-linear-ci-bootstrap-impl
```

Expected: pre-commit hooks pass, the branch advances, and the remote tracking ref matches `HEAD`.
