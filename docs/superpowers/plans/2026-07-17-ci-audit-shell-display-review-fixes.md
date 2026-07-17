# CI Audit Shell and Display Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all six verified shell-scanner false-negative/false-positive gaps and make refresh previews safe from Unicode format controls.

**Architecture:** Keep audit bounded and non-executing by correcting static grammar at its shared parsing boundaries: word termination, ANSI-C decoding, uv option walks, reconcile argument parsing, and env option prechecks. Keep preview recovery possible by visibly escaping terminal-active content at rendering rather than rejecting repository files.

**Tech Stack:** Python 3.13+, pytest, Typer/Click, bounded Bash scanning, Unicode general categories, unified diff rendering, Ruff, `ty`, and `uv`.

---

### Task 1: Fail closed before extglob becomes command grouping

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`

- [x] **Step 1: Write the failing extglob regressions**

Add beside the active brace/glob subcommand tests:

```python
@pytest.mark.parametrize("operator", ["?", "*", "+", "@", "!"])
def test_scan_doc_lattice_invocations_fails_closed_on_extglob_operator(operator):
    result = scan_doc_lattice_invocations(
        f"shopt -s extglob\ndoc-lattice {operator}(reconcile) --all"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason == "extglob expansion cannot be scanned safely"


def test_direct_doc_lattice_invocations_keeps_quoted_extglob_text_literal():
    assert direct_doc_lattice_invocations("doc-lattice '@(reconcile)' --all") == (
        ("@(reconcile)", False),
    )
```

- [x] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_scan_doc_lattice_invocations_fails_closed_on_extglob_operator tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_keeps_quoted_extglob_text_literal -q
```

Expected: the five active cases report complete scans
with literal operator subcommands instead of the extglob incomplete reason; the quoted control
passes.

- [x] **Step 3: Reject active extglob openers while word provenance is available**

Add a boundary helper after `_ShellWordBuilder`:

```python
def _reject_active_extglob_opener(builder: _ShellWordBuilder, boundary: str) -> None:
    """Reject an unquoted extglob opener before ``(`` becomes a command-group operator."""
    if boundary == "(" and builder.active_syntax and builder.active_syntax[-1] in "?*+@!":
        raise _ShellScanIncomplete("extglob expansion cannot be scanned safely")
```

Call `_reject_active_extglob_opener(builder, self.source[index : index + 1])` after the existing
word loop and before building the word. Protected operators carry blank provenance, so quoted and
escaped syntax is not rejected. Keeping the loop unchanged also keeps `_parse_word` within its
enforced branch and statement budgets.

- [x] **Step 4: Run GREEN**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_scan_doc_lattice_invocations_fails_closed_on_extglob_operator tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_keeps_quoted_extglob_text_literal -q
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py -q
```

Expected: PASS.

### Task 2: Apply Bash's octal byte conversion before NUL validation

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`

- [x] **Step 1: Extend the ANSI-C NUL regression with wrapped octal**

Change the escape parameter list to:

```python
@pytest.mark.parametrize(
    "escape",
    [r"\0", r"\400", r"\x00", r"\u0000", r"\U00000000", r"\c@"],
)
```

- [x] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_scan_doc_lattice_invocations_rejects_ansi_c_nul_escape -q
```

Expected: the two `\400`
cases fail because the scanner decodes U+0100 instead of NUL.

- [x] **Step 3: Reduce only three-digit octal escapes to a byte**

Add `_ANSI_C_OCTAL_BYTE_MASK = 0xFF` near `_OCTAL_BASE`, then change the octal arm to:

```python
value, end = _read_ansi_c_digits(source, start, limit, _OCTAL_BASE, 3)
value &= _ANSI_C_OCTAL_BYTE_MASK
result = (_valid_ansi_c_character(value, source[start:end]), end)
```

- [x] **Step 4: Run GREEN**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_scan_doc_lattice_invocations_rejects_ansi_c_nul_escape -q
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py -q
```

Expected: PASS with the established NUL diagnostic.

### Task 3: Stop scanner resolution on eager uv options

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`

- [x] **Step 1: Add eager uv non-command regressions**

Extend `test_direct_doc_lattice_invocations_ignores_nonexecuting_command_forms` with:

```python
"uv --help run doc-lattice linear",
"uv -h run doc-lattice linear",
"uv --version run doc-lattice linear",
"uv -V run doc-lattice linear",
"uvx --help doc-lattice linear",
"uvx -h doc-lattice linear",
"uvx --version doc-lattice linear",
"uvx -V doc-lattice linear",
"uv run --help doc-lattice linear",
"uv run -h doc-lattice linear",
"uv tool run --help doc-lattice linear",
"uv tool run -h doc-lattice linear",
```

- [x] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_ignores_nonexecuting_command_forms -q
```

Expected: the new cases raise incomplete-scan errors.

- [x] **Step 3: Encode exact stop surfaces**

Add:

```python
_UV_HELP_OPTIONS = frozenset({"--help", "-h"})
_UV_VERSION_OPTIONS = frozenset({"--version", "-V"})
_UV_GLOBAL_STOP_OPTIONS = _UV_HELP_OPTIONS | _UV_VERSION_OPTIONS
```

Build `_UVX_LAUNCHER` with help and version in `non_command_options`, a new
`_UV_TOOL_RUN_LAUNCHER` with only help, and `_UV_RUN_LAUNCHER` with its existing non-command
options unioned with help. Use the tool-run configuration in `_uv_tool_payload_index`. In
`_static_uv_global_option_result`, stop before ordinary flags:

```python
if word.literal in _UV_GLOBAL_STOP_OPTIONS:
    return len(words), None
```

- [x] **Step 4: Run GREEN**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_ignores_nonexecuting_command_forms -q
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 4: Treat only effective reconcile help as non-mutating

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`

- [x] **Step 1: Add effective and consumed help regressions**

Add:

```python
@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile --help",
        "doc-lattice reconcile pc-design --format human --help",
    ],
)
def test_direct_doc_lattice_invocations_treats_effective_reconcile_help_as_non_mutating(script):
    assert direct_doc_lattice_invocations(script) == RECONCILE_DRY


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile pc-design --config --help",
        "doc-lattice reconcile -- --help",
    ],
)
def test_direct_doc_lattice_invocations_does_not_widen_consumed_reconcile_help(script):
    assert direct_doc_lattice_invocations(script) == RECONCILE
```

- [x] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_treats_effective_reconcile_help_as_non_mutating tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_does_not_widen_consumed_reconcile_help -q
```

Expected: effective help cases return `RECONCILE`; consumed and positional
controls pass.

- [x] **Step 3: Broaden and rename the effective-safe parser**

Rename `_reconcile_has_effective_dry_run` to `_reconcile_is_effectively_non_mutating`, update its
docstring, rename the caller local to `is_non_mutating`, and recognize:

```python
if literal in {"--dry-run", "--help"}:
    return True
```

Preserve value consumption before this check and the existing `--` termination.

- [x] **Step 4: Run GREEN**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_treats_effective_reconcile_help_as_non_mutating tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_does_not_widen_consumed_reconcile_help -q
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 5: Stop env split-string scanning at attached `-a` values

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`

- [x] **Step 1: Add the attached argv0 regression**

Add `"env -aS doc-lattice linear"` with an `"argv0"` id to
`test_direct_doc_lattice_invocations_handles_env_short_option_value_attached_to_short_option`.

- [x] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_handles_env_short_option_value_attached_to_short_option -q
```

Expected: the new case raises the env split-string diagnostic.

- [x] **Step 3: Correct the precheck**

Change the required-value branch in `_is_env_split_string_short_option` to:

```python
if option in {"a", "u", "C"}:
    return False
```

- [x] **Step 4: Run GREEN**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_invocations_handles_env_short_option_value_attached_to_short_option -q
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 6: Escape Unicode format controls in refresh diffs

**Files:**
- Modify: `tests/test_github_ci_filesystem.py`
- Modify: `src/doc_lattice/github_ci/filesystem.py`

- [x] **Step 1: Write the failing Unicode format-control regression**

Add:

```python
def test_render_diff_escapes_unicode_format_controls():
    controls = {
        "\u00ad": r"\u00ad",
        "\u061c": r"\u061c",
        "\u200e": r"\u200e",
        "\u202e": r"\u202e",
        "\u2066": r"\u2066",
        "\ufeff": r"\ufeff",
        "\U000e0001": r"\U000e0001",
    }
    artifact = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice.yml"),
        text="new:café 🧪\n",
    )
    change = ArtifactChange(
        artifact=artifact,
        root=Path("/repo"),
        destination=Path("/repo/.github/workflows/doc-lattice.yml"),
        action="replace",
        before=f"old:{''.join(controls)}:suffix\n".encode(),
    )

    rendered = render_diff((change,))

    for character, escaped in controls.items():
        assert character not in rendered
        assert escaped in rendered
    assert "café 🧪" in rendered
```

- [x] **Step 2: Run RED**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_filesystem.py::test_render_diff_escapes_unicode_format_controls -q
```

Expected: a raw format-control assertion fails.

- [x] **Step 3: Escape general-category `Cf` characters**

Import `unicodedata`, define `_BMP_MAX_CODEPOINT = 0xFFFF`, and add:

```python
def _unicode_format_escape(value: int) -> str:
    """Return a visible Python-style escape for one Unicode format control."""
    if value <= _BMP_MAX_CODEPOINT:
        return f"\\u{value:04x}"
    return f"\\U{value:08x}"
```

In `_escape_diff_terminal_controls`, insert after the byte-control branch:

```python
elif unicodedata.category(character) == "Cf":
    rendered.append(_unicode_format_escape(value))
```

- [x] **Step 4: Run GREEN**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_filesystem.py::test_render_diff_escapes_unicode_format_controls -q
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov tests/test_github_ci_filesystem.py tests/cli/test_ci.py -q
```

Expected: PASS.

### Task 7: Document, verify, audit, commit, and push

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-07-17-ci-audit-shell-display-review-fixes.md`

- [x] **Step 1: Update the public safety contract**

Document `\xNN`, `\uNNNN`, and `\UNNNNNNNN` preview escapes. State that unsupported active
extglob and byte-wrapped ANSI-C NUL fail closed while eager non-executing help/version forms do not
produce policy findings.

- [x] **Step 2: Run complete repository gates**

Run:

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff format --check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ty check src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_typing_boundaries.py src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_version_sync.py
git diff --check
```

Expected: every command exits 0, with pytest coverage at or above 80 percent.

- [x] **Step 3: Audit every requirement against fresh evidence**

Rerun all six focused regressions, inspect the complete diff and status, and map each finding to its
regression and production boundary. Confirm no unrelated file is staged.

- [ ] **Step 4: Commit the implementation**

Stage only README, scanner/filesystem source and tests, and this completed plan. Run:

```bash
git add README.md src/doc_lattice/github_ci/shell_scanner.py src/doc_lattice/github_ci/filesystem.py tests/test_github_ci_shell_scanner.py tests/test_github_ci_filesystem.py docs/superpowers/plans/2026-07-17-ci-audit-shell-display-review-fixes.md
git commit -m "fix: close shell and display review gaps"
```

Expected: all pre-commit hooks pass.

- [ ] **Step 5: Push the current branch**

```bash
git push origin feature/github-linear-ci-bootstrap-impl
```

Expected: the remote branch advances without force-pushing.
