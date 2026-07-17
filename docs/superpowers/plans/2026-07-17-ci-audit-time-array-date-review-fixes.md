# CI Audit Time, Array, and Date Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close forced-external `time` policy bypasses, ignore literal Bash array data without
hiding executable substitutions, and parse implicit workflow timestamps as strings.

**Architecture:** Extend shell-word provenance so Bash keywords are distinguished from decoded
external executable names, add a bounded compound-array data consumer that delegates executable
expansions to the existing recursive scanner, and remove only the timestamp implicit resolver
from each workflow-local YAML instance. Reuse the existing conservative external `time` grammar,
scanner budgets, typed workflow boundary, and fail-closed errors.

**Tech Stack:** Python 3.13, ruamel.yaml, pytest, Ruff, ty, pre-commit

---

## File map

- `src/doc_lattice/github_ci/shell_scanner.py`: shell keyword provenance, forced-external `time`
  routing, and compound array-assignment consumption.
- `tests/test_github_ci_shell_scanner.py`: focused scanner red/green regressions and executable
  substitution controls.
- `tests/test_github_ci_audit.py`: pull-request policy regressions for forced-external `time` and
  literal array data.
- `src/doc_lattice/github_ci/workflow_parser.py`: workflow-local implicit timestamp resolver
  configuration.
- `tests/test_github_ci_workflow_parser.py`: implicit timestamp string and explicit tag rejection
  coverage.

### Task 1: Distinguish forced-external `time` from the Bash keyword

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py:430-500,945-975`
- Modify: `tests/test_github_ci_audit.py:65-115,620-655`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:304-365,575-780,1641-1680,1890-1920`

- [x] **Step 1: Add scanner regressions and portable controls**

Extend the unknown external `time` option parameters with:

```python
r"\time -f '%e' doc-lattice linear",
"command time -f '%e' doc-lattice linear",
"exec time -f '%e' doc-lattice linear",
```

Extend the accepted compound grammar parameters with:

```python
(r"\time -p doc-lattice linear", LINEAR),
("'time' -- doc-lattice linear", LINEAR),
("command time -p doc-lattice linear", LINEAR),
("exec time -p -- doc-lattice reconcile --all", RECONCILE),
```

- [x] **Step 2: Add pull-request audit regressions**

Parameterize the unknown external-time audit test over `env time`, `\time`, `command time`, and
`exec time` GNU `-f` forms, all expecting `shell scan.*external time option`. Add the portable
escaped, quoted, `command`, and `exec` forms to the existing PR-policy parameter table with their
Linear or mutating-reconcile finding code.

- [x] **Step 3: Run RED and confirm the bypass**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_fails_closed_on_unknown_external_time_option \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_handles_root_options_and_compound_grammar \
  tests/test_github_ci_audit.py::test_global_audit_fails_closed_on_unknown_external_time_option \
  tests/test_github_ci_audit.py::test_global_audit_rejects_root_options_and_compound_grammar_on_pr -q
```

Expected: the new forced-external cases fail because escaped/quoted `time` is treated as a keyword
and because `command`/`exec` expose a target that is reclassified as a keyword. Existing controls
pass.

- [x] **Step 4: Preserve Bash reserved-word provenance**

Add a `keyword_eligible` field to `_ShellWord` and `_ShellWordBuilder`, clear it from
`append_protected`, and copy it from `build`. Require that field when recognizing `case`, the
reserved words in `_COMMAND_PREFIXES`, `time`, and `coproc`:

```python
@dataclass(frozen=True, slots=True)
class _ShellWord:
    literal: str
    dynamic: bool = False
    unquoted_dynamic: bool = False
    quoted_zero_field_expansion: bool = False
    active_argv_expansion: bool = False
    shell_assignment: bool = False
    keyword_eligible: bool = True
```

```python
def append_protected(...):
    ...
    self.keyword_eligible = False
```

- [x] **Step 5: Route wrapper-exposed `time` through the external grammar**

In `_skip_shell_prefixes`, retain the wrapper literal before advancing. When `command` or `exec`
resolves a static target whose decoded literal is `time`, return the payload position from
`_skip_external_time_prefix` instead of looping back through keyword recognition:

```python
wrapper_literal = word.literal
wrapper = _skip_shell_builtin_wrapper(words, index)
...
if (
    wrapper_literal in {"command", "exec"}
    and index < len(words)
    and not _word_may_change_argv(words[index])
    and words[index].literal == "time"
):
    return _ResolvedIndex(
        _skip_external_time_prefix(words, index + 1),
        ambiguous,
    )
```

- [x] **Step 6: Run GREEN and focused scanner/audit modules**

Run the command from Step 3, then:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: every selected test passes with no warnings.

### Task 2: Consume array literals as data while scanning executable substitutions

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py:520-610`
- Modify: `tests/test_github_ci_audit.py:115-160`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:545-620`

- [x] **Step 1: Add literal-array and executable-substitution scanner tests**

Add:

```python
@pytest.mark.parametrize(
    "script",
    [
        "args=(doc-lattice linear)",
        "declare -a args=(doc-lattice reconcile --all)",
        "args=([1+(2)]=doc-lattice linear)",
    ],
    ids=["indexed", "declare-indexed", "arithmetic-subscript"],
)
def test_direct_doc_lattice_invocations_treats_array_literals_as_data(script):
    assert direct_doc_lattice_invocations(script) == NONE


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("args=($(doc-lattice linear))", LINEAR),
        ("args=(<(doc-lattice reconcile --all))", RECONCILE),
        ("args=(doc-lattice linear)\ndoc-lattice check", CHECK),
    ],
    ids=["command-substitution", "process-substitution", "following-command"],
)
def test_direct_doc_lattice_invocations_scans_executable_array_contexts(script, expected):
    assert direct_doc_lattice_invocations(script) == expected
```

- [x] **Step 2: Add pull-request audit false-positive coverage**

Create one PR workflow whose run script builds both literal arrays:

```python
def test_global_audit_allows_literal_doc_lattice_array_data_on_pr():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          args=(doc-lattice linear)
          declare -a reconcile_args=(doc-lattice reconcile --all)
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == set()
```

- [x] **Step 3: Run RED and confirm literal elements are scanned as subshell commands**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_treats_array_literals_as_data \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_scans_executable_array_contexts \
  tests/test_github_ci_audit.py::test_global_audit_allows_literal_doc_lattice_array_data_on_pr -q
```

Expected: the three literal-data scanner parameters and the audit test fail with reported
invocations/findings; the executable substitution and following-command controls pass.

- [x] **Step 4: Add a bounded compound array consumer**

Before flushing an opening `(` operator, detect a final assignment word ending in `=` and call a
new `_consume_array_assignment` method. The method tracks balanced bare parentheses, uses
`_parse_word` for quotes, escapes, and `$()`/backtick expansion, calls
`_consume_process_substitution` for `<()` and `>()`, skips comments only at a word boundary, and
charges the existing step and recursion budgets. It returns just past the matching outer `)` and
leaves the pending assignment word in the current command state.

```python
if (
    operator == "("
    and state.words
    and state.words[-1].shell_assignment
    and state.words[-1].literal.endswith("=")
):
    return self._consume_array_assignment(index, limit, depth + 1)
```

The consumer must not recursively call `_scan_commands` for ordinary array elements. Only the
existing active-expansion and process-substitution routines may expose executable regions.

- [x] **Step 5: Run GREEN and focused scanner/audit modules**

Run the command from Step 3, then:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: every selected test passes with no warnings.

### Task 3: Preserve implicit workflow timestamps as strings

**Files:**
- Modify: `tests/test_github_ci_workflow_parser.py:110-190,650-710`
- Modify: `src/doc_lattice/github_ci/workflow_parser.py:22-90`

- [x] **Step 1: Add implicit timestamp and explicit-tag parser tests**

Add:

```python
def test_parse_workflow_keeps_implicit_timestamps_as_strings():
    parsed = parse_workflow(
        Path(".github/workflows/timestamps.yml"),
        """\
env:
  RELEASE_DATE: 2026-07-17
  RELEASE_AT: 2026-07-17T14:30:00Z
jobs: {}
""",
    )

    values = {scalar.path: scalar.value for scalar in parsed.scalars}
    assert values[("env", "RELEASE_DATE")] == "2026-07-17"
    assert values[("env", "RELEASE_AT")] == "2026-07-17T14:30:00Z"


def test_parse_workflow_rejects_explicit_timestamp_tag():
    with pytest.raises(ConfigError, match="unsupported YAML scalar"):
        parse_workflow(
            Path(".github/workflows/tagged-timestamp.yml"),
            "payload: !!timestamp 2026-07-17\njobs: {}\n",
        )
```

- [x] **Step 2: Run RED and verify implicit values construct date objects**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_workflow_parser.py::test_parse_workflow_keeps_implicit_timestamps_as_strings \
  tests/test_github_ci_workflow_parser.py::test_parse_workflow_rejects_explicit_timestamp_tag -q
```

Expected: the implicit timestamp test fails with `unsupported YAML scalar`; the explicit tag
control passes.

- [x] **Step 3: Remove only the per-instance implicit timestamp resolver**

Add the timestamp tag constant and configure the fresh safe loader before compose/load:

```python
_YAML_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"


def _disable_implicit_timestamps(yaml: YAML) -> None:
    for resolvers in yaml.resolver.versioned_resolver.values():
        resolvers[:] = [
            (tag, regexp) for tag, regexp in resolvers if tag != _YAML_TIMESTAMP_TAG
        ]
```

Call `_disable_implicit_timestamps(yaml)` immediately after setting duplicate-key behavior. Do not
modify the timestamp constructor or class-level resolver state.

- [x] **Step 4: Run GREEN and the focused parser module**

Run the command from Step 2, then:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_workflow_parser.py -q
```

Expected: every selected test passes with no warnings.

### Task 4: Verify, audit, commit, and push

**Files:**
- Verify: the complete repository and all changed files

- [x] **Step 1: Run the full test suite with coverage**

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest -q
```

Expected: all tests pass and coverage meets the configured 80 percent minimum.

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

- [x] **Step 3: Audit every requirement and the complete diff**

Confirm:

- escaped/quoted and `command`/`exec` external `time -f` forms fail closed;
- portable forced-external `time` forms reach Linear and mutating reconcile policy;
- literal array elements produce no scanner invocation or PR finding;
- command and process substitutions inside arrays remain visible;
- implicit ISO dates and datetimes are exact workflow strings;
- explicit timestamp tags remain rejected; and
- only the planned source, tests, design, and completed plan changed.

Run:

```bash
git diff --stat HEAD
git diff HEAD -- src/doc_lattice/github_ci/shell_scanner.py \
  src/doc_lattice/github_ci/workflow_parser.py tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_audit.py tests/test_github_ci_workflow_parser.py \
  docs/superpowers/plans/2026-07-17-ci-audit-time-array-date-review-fixes.md
git status --short
```

- [ ] **Step 4: Commit the implementation**

```bash
git add docs/superpowers/plans/2026-07-17-ci-audit-time-array-date-review-fixes.md \
  src/doc_lattice/github_ci/shell_scanner.py src/doc_lattice/github_ci/workflow_parser.py \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py \
  tests/test_github_ci_workflow_parser.py
git commit -m "fix: close time array and date audit gaps"
```

Expected: commit succeeds and all configured pre-commit hooks pass.

- [ ] **Step 5: Push the current branch without force**

```bash
git push origin feature/github-linear-ci-bootstrap-impl
```

Expected: the remote branch advances to include the design, plan, implementation, and tests.
