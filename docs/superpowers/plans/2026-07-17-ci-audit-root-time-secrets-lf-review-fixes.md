# CI Audit Root, Time, Secrets, and LF Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the nested-root, Bash `time --`, reusable-workflow secret inheritance, and Windows bootstrap line-ending findings without weakening the existing fail-closed audit.

**Architecture:** Resolve one validated Git top-level in a focused CLI adapter and pass it to every managed-CI filesystem operation. Correct shell and secret grammar at their existing shared policy boundaries. Prevent checkout conversion with a fourth, scoped managed `.github/.gitattributes` artifact whose required LF rule is audited semantically.

**Tech Stack:** Python 3.13+, Typer, Git subprocess adapter, bounded Bash scanning, ruamel.yaml workflow normalization, Git attributes, pytest, Ruff, `ty`, and `uv`.

---

### Task 1: Anchor managed CI commands at the Git top-level

**Files:**
- Create: `src/doc_lattice/cli/git_repository.py`
- Create: `tests/cli/test_git_repository.py`
- Modify: `src/doc_lattice/cli/commands/ci.py`
- Modify: `src/doc_lattice/cli/commands/init.py`
- Modify: `tests/cli/test_ci.py`
- Modify: `tests/cli/test_init.py`

- [ ] **Step 1: Write focused Git-root and nested-command regressions**

Create adapter tests that initialize a real repository and require a nested invocation to resolve
the outer root, plus non-repository and malformed-output failures. In the CI tests, initialize the
temporary test root as a Git repository and add these behavior cases:

```python
def test_ci_audit_from_subdirectory_ignores_nested_managed_decoy(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    _install(nested)
    _install(tmp_path)
    (tmp_path / ".github/workflows/unsafe.yml").write_text(
        "on: pull_request_target\njobs: {}\n", encoding="utf-8"
    )
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"]
    )

    assert result.exit_code == 1
    assert "PULL_REQUEST_TARGET" in result.stdout


def test_ci_refresh_from_subdirectory_previews_git_root_not_nested_decoy(
    tmp_path, monkeypatch
):
    nested = tmp_path / "nested"
    nested.mkdir()
    apply_changes(preflight_create(tmp_path, render_managed_artifacts(
        "Guardantix/doc-lattice", "1.9.0"
    )))
    _install(nested)
    monkeypatch.chdir(nested)

    result = runner.invoke(
        app, ["ci", "refresh", "--repository", "Guardantix/doc-lattice"]
    )

    assert result.exit_code == 1
    assert "--- a/.github/workflows/doc-lattice.yml" in result.stdout
```

Add a GitHub-init case that invokes from `tmp_path / "nested"`, then asserts the config and every
rendered artifact exist under `tmp_path` and that the nested directory has no `.github` or config.
Rename the existing explicit-repository test to assert origin lookup is skipped, since explicit
identity no longer skips the top-level lookup.

- [ ] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/cli/test_git_repository.py \
  tests/cli/test_ci.py::test_ci_audit_from_subdirectory_ignores_nested_managed_decoy \
  tests/cli/test_ci.py::test_ci_refresh_from_subdirectory_previews_git_root_not_nested_decoy \
  tests/cli/test_init.py::test_init_github_from_subdirectory_anchors_at_git_top_level -q
```

Expected: the adapter import is absent initially; after the test scaffold imports cleanly, all
three command regressions expose the nested directory behavior.

- [ ] **Step 3: Implement the focused Git repository adapter**

Add `resolve_git_repository_root(cwd: Path) -> Path` in `cli/git_repository.py`. Run:

```python
subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=cwd,
    capture_output=True,
    check=False,
    timeout=5,
)
```

Catch `FileNotFoundError` separately, map other `OSError` and timeout failures to `ConfigError`,
require return code zero, decode UTF-8, require one nonempty absolute path, resolve both root and
cwd strictly, require a real directory, and require the cwd to be contained by the root. Do not
include raw Git output or absolute untrusted paths in diagnostics.

In `ci audit` and `ci refresh`, resolve the root before identity or artifact work and pass that root
to discovery, inspection, origin lookup, refresh preflight, repeated preflight, and convergence
verification. Change `_resolve_repository` to accept a root `Path` instead of a runtime.

In GitHub-mode `init`, resolve the root after option validation and use it for both managed
artifacts and `.doc-lattice.yml`. Leave ordinary init rooted at `runtime.cwd` and Git-free.

- [ ] **Step 4: Run GREEN**

Run the RED command again, then:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/cli/test_git_repository.py tests/cli/test_ci.py tests/cli/test_init.py -q
```

Expected: PASS.

### Task 2: Consume Bash `time`'s option terminator

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `tests/test_github_ci_audit.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`

- [ ] **Step 1: Add scanner and PR-policy regressions**

Extend the existing root-option parameter tables with:

```python
("time -- doc-lattice linear", LINEAR),
("time -p -- doc-lattice reconcile --all", RECONCILE),
```

and the audit equivalents:

```python
("time -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
("time -p -- doc-lattice reconcile --all", "PR_MUTATING_RECONCILE"),
```

- [ ] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_handles_root_options_and_compound_grammar \
  tests/test_github_ci_audit.py::test_global_audit_rejects_root_options_and_compound_grammar_on_pr -q
```

Expected: the four new parameter cases fail because `--` remains in executable position.

- [ ] **Step 3: Consume one static terminator after optional `-p`**

In `_skip_shell_prefixes`, keep the existing `time` and optional `-p` handling, then add:

```python
if (
    index < len(words)
    and not _word_may_change_argv(words[index])
    and words[index].literal == "--"
):
    index += 1
```

Do not widen external `/usr/bin/time` grammar or dynamic option handling.

- [ ] **Step 4: Run GREEN**

Run the RED command again, then the complete scanner and audit suites:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 3: Reject reusable-workflow whole-context secret inheritance

**Files:**
- Modify: `tests/test_github_ci_audit.py`
- Modify: `src/doc_lattice/github_ci/audit.py`

- [ ] **Step 1: Add exact-path positive and negative regressions**

Add a reusable workflow caller test:

```python
def test_global_audit_rejects_reusable_workflow_secret_inheritance():
    document = _workflow(
        """\
on: pull_request
jobs:
  reusable:
    uses: ./.github/workflows/reusable.yml
    secrets: inherit
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {
        "LINEAR_SECRET_REFERENCE"
    }
```

Add a negative case with `inherit` under a step `with:` value and ordinary prose to prove the
keyword is not banned outside `jobs.<job_id>.secrets`.

- [ ] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_audit.py::test_global_audit_rejects_reusable_workflow_secret_inheritance \
  tests/test_github_ci_audit.py::test_global_audit_allows_inherit_outside_reusable_job_secrets -q
```

Expected: the positive case returns no finding while the negative control passes.

- [ ] **Step 3: Detect exact job-level inheritance**

Add a helper with the structural constraint:

```python
def _is_reusable_workflow_secret_inheritance(path: tuple[str, ...], value: str) -> bool:
    return (
        len(path) == 3
        and path[0] == "jobs"
        and path[2] == "secrets"
        and value == "inherit"
    )
```

Call it in `_has_linear_secret_reference`'s scalar loop before the existing name and expression
checks. Keep the canonical secret-slot exemption unchanged.

- [ ] **Step 4: Run GREEN**

Run the RED command again, then:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 4: Render and publish a scoped LF attributes artifact

**Files:**
- Modify: `tests/test_github_ci_render.py`
- Modify: `tests/test_github_ci_bootstrap.py`
- Modify: `tests/test_github_ci_filesystem.py`
- Modify: `tests/cli/test_init.py`
- Modify: `tests/cli/test_ci.py`
- Modify: `src/doc_lattice/github_ci/model.py`
- Modify: `src/doc_lattice/github_ci/render.py`
- Modify: `src/doc_lattice/github_ci/filesystem.py`
- Modify: `src/doc_lattice/cli/commands/init.py`

- [ ] **Step 1: Add renderer, init, and refresh regressions**

Add a render test requiring canonical roles and paths to end with:

```python
("bootstrap", PurePosixPath(".github/doc-lattice-bootstrap.sh")),
("attributes", PurePosixPath(".github/.gitattributes")),
```

Assert the attributes text has the four ownership lines followed by exactly:

```text
doc-lattice-bootstrap.sh text eol=lf
```

Update the bootstrap ordering test to require four artifacts while retaining bootstrap index 2.
Add an init test asserting `.github/.gitattributes` is created and named in review guidance. Add a
refresh test that removes `.github/.gitattributes` with `missing_ok=True`, then requires a create
preview for that path and successful recreation after confirmed apply.

- [ ] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_render.py::test_render_managed_artifacts_include_scoped_bootstrap_lf_policy \
  tests/test_github_ci_bootstrap.py::test_rendered_bootstrap_is_the_third_managed_artifact_and_is_valid_bash \
  tests/cli/test_init.py::test_init_github_creates_managed_lf_attributes_policy \
  tests/cli/test_ci.py::test_ci_refresh_recreates_missing_attributes_policy -q
```

Expected: all new expectations fail against the three-artifact renderer.

- [ ] **Step 3: Add the fourth managed artifact**

Extend `ArtifactRole` with `"attributes"` and teach `_parse_artifact_role` to return every exact
literal explicitly. In `render.py`, add:

```python
GIT_ATTRIBUTES_PATH = PurePosixPath(".github/.gitattributes")
BOOTSTRAP_EOL_RULE = "doc-lattice-bootstrap.sh text eol=lf"
```

Append the attributes target to `CANONICAL_ARTIFACT_TARGETS`. Add
`render_git_attributes(repository, version)` using the shared ownership header plus the single LF
rule, and return it fourth from `render_managed_artifacts`. Update the fixed return annotation.

Update GitHub init's guidance destructuring and prose to include the attributes path. Adjust
existing exact-count/order tests and mixed artifact fixtures to carry the fourth slot while keeping
the bootstrap at index 2.

- [ ] **Step 4: Run GREEN for rendering and publication**

Run the RED command again, then:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_render.py tests/test_github_ci_bootstrap.py \
  tests/test_github_ci_filesystem.py tests/cli/test_init.py tests/cli/test_ci.py -q
```

Expected: PASS after updating all canonical four-slot expectations.

### Task 5: Fail audit when the LF rule is weakened or removed

**Files:**
- Modify: `tests/test_github_ci_audit.py`
- Modify: `src/doc_lattice/github_ci/audit.py`

- [ ] **Step 1: Add semantic attributes audit regressions**

Add one test that changes only `doc-lattice-bootstrap.sh text eol=lf` to
`doc-lattice-bootstrap.sh text`, retains the valid marker, and expects `MANAGED_ATTRIBUTES`. Add a
positive control that rewrites only the attributes file's separators to CRLF and still expects no
finding, because Git can parse CRLF attributes files and the rule remains effective.

- [ ] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_audit.py::test_managed_audit_rejects_weakened_bootstrap_lf_rule \
  tests/test_github_ci_audit.py::test_managed_audit_accepts_crlf_attributes_file_with_exact_rule -q
```

Expected: the weakened rule is misclassified as a missing workflow or accepted; the CRLF control
must remain accepted after the implementation.

- [ ] **Step 3: Audit the exact effective rule**

Add `MANAGED_ATTRIBUTES` to `_MANAGED_MESSAGES`. In `_audit_installed_artifact`, branch on the
`attributes` role before workflow lookup. Extract nonempty, non-comment `splitlines()` records and
require the tuple to equal `(BOOTSTRAP_EOL_RULE,)`; otherwise emit the managed attributes finding.
Return after this check. Keep marker, version, and repository findings additive.

Update managed-installation slot and order diagnostics from three to four and include the
`attributes` role. Update marker/inspection tests so the bootstrap still occupies slot 2 and the
attributes policy occupies slot 3.

- [ ] **Step 4: Run GREEN**

Run the RED command again, then:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_audit.py tests/test_github_ci_filesystem.py -q
```

Expected: PASS.

### Task 6: Update authoritative behavior and architecture documentation

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Update the user contract**

Change the command table and setup section from three to four managed artifacts. Document
`.github/.gitattributes`, its scoped `eol=lf` rule, and why it is required for Git Bash under
`core.autocrlf=true`. State that GitHub-mode init, audit, and refresh resolve the Git top-level from
subdirectories, while ordinary init retains cwd behavior. State that reusable-workflow
`secrets: inherit` is whole-context secret access and fails audit.

- [ ] **Step 2: Update AD-16**

Record that the local managed set includes a scoped Git attributes policy and that local audit
semantically checks its bootstrap LF rule. Do not change the external human GitHub administration
boundary.

- [ ] **Step 3: Verify documentation integrity**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_version_sync.py
git diff --check
```

Expected: both commands exit 0.

### Task 7: Complete verification, requirement audit, commit, and push

**Files:**
- Review: all changed files

- [ ] **Step 1: Run the complete handoff verification**

Run fresh:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff format --check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ty check src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_typing_boundaries.py src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_version_sync.py
git diff --check
```

Expected: all exit 0; pytest reports zero failures and coverage at or above 80 percent.

- [ ] **Step 2: Audit every requirement against current evidence**

Inspect `git diff --stat`, `git diff`, and `git status --short`. Confirm:

1. all three managed-CI commands resolve and use the Git top-level;
2. both `time --` forms are detected by scanner and PR audit;
3. only job-level `secrets: inherit` is classified as whole-context access;
4. init and refresh manage `.github/.gitattributes`, audit requires its LF rule, and README still
   advertises Git Bash only with that checkout guarantee; and
5. no unrelated user changes are included.

- [ ] **Step 3: Commit and push without force**

Run:

```bash
git add README.md ARCHITECTURE.md src tests
git commit -m "fix: close CI root secret and bootstrap gaps"
git push origin feature/github-linear-ci-bootstrap-impl
```

Expected: pre-commit hooks pass, the commit succeeds, and the branch advances on origin without
force.
