# CI Audit, Terminal, and Durability Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all four verified executable-expansion, ANSI-C NUL, terminal-control, and create-retry durability review gaps.

**Architecture:** Reject static expansion provenance at each supported executable resolver and reject NUL at the shared ANSI-C decoding boundary. Escape terminal controls only at diff rendering, and make create traversal synchronize every validated ancestor entry so retries re-establish durability without destructive cleanup.

**Tech Stack:** Python 3.13+, pytest, bounded Bash scanning, unified diff rendering, POSIX directory descriptors and `fsync`, Ruff, `ty`, and `uv`.

---

### Task 1: Fail closed on expanded executable words

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`
- Modify: `README.md`

- [x] **Step 1: Write the failing executable-position regression**

Add a parameterized test beside the expanded-subcommand cases:

```python
@pytest.mark.parametrize(
    "script",
    [
        "{doc-lattice,} linear",
        "command {doc-lattice,} linear",
        "exec {doc-lattice,} linear",
        "builtin exec {doc-lattice,} linear",
        "time {doc-lattice,} linear",
        "coproc {doc-lattice,} linear",
        "coproc worker {doc-lattice,} linear",
        "uv run {doc-lattice,} linear",
        "uvx {doc-lattice,} linear",
    ],
)
def test_scan_doc_lattice_invocations_fails_closed_on_expanded_executable(script):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "executable word uses brace or glob expansion"
```

The existing expanded-argument test remains the positive control proving that
`doc-lattice linear {one,two}` is still classified normally.

- [x] **Step 2: Run RED**

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py::test_scan_doc_lattice_invocations_fails_closed_on_expanded_executable -q
```

Expected: all cases fail because the scanner reports `incomplete_reason=None`.

- [x] **Step 3: Reject active expansion at supported executable boundaries**

Add the shared guard near the argv-provenance helpers:

```python
def _reject_active_executable_word(word: _ShellWord) -> None:
    """Reject a brace- or glob-expanded word that may become the executed program."""
    if word.active_argv_expansion:
        raise _ShellScanIncomplete("executable word uses brace or glob expansion")
```

Use it directly when a selected payload word still carries active expansion. Add a range guard that
rejects active expansion traversed by top-level, Bash-wrapper, coprocess, and uv resolution only
when no later payload was classified. This preserves established diagnostics for a later sensitive
payload while preventing unresolved expansion ambiguity from being discarded. Require a closing
`]` before treating `[` as an active bracket glob so the literal Bash `[` command stays valid.

Update the README audit contract to state that active brace/glob expansion in executable or
subcommand words fails closed.

- [x] **Step 4: Run GREEN and the scanner/audit suites**

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest --no-cov \
  tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py -q
```

Expected: PASS.

### Task 2: Reject ANSI-C escapes that decode to NUL

**Files:**
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`
- Modify: `README.md`

- [x] **Step 1: Write the failing ANSI-C NUL regressions**

Add executable and protected-subcommand coverage for every supported NUL spelling:

```python
@pytest.mark.parametrize("escape", [r"\0", r"\x00", r"\u0000", r"\U00000000", r"\c@"])
@pytest.mark.parametrize(
    "template",
    [
        "$'doc-lattice{escape}suffix' linear",
        "doc-lattice $'linear{escape}suffix'",
    ],
    ids=["executable", "subcommand"],
)
def test_scan_doc_lattice_invocations_rejects_ansi_c_nul_escape(escape, template):
    result = scan_doc_lattice_invocations(template.format(escape=escape))

    assert result.invocations == NONE
    assert result.incomplete_reason == "ANSI-C quoted word decodes to NUL"
```

- [x] **Step 2: Run RED**

Run the new parameterized test with `pytest --no-cov -q`. Expected: executable cases report a
complete empty result and subcommand cases retain a literal containing `chr(0)`.

- [x] **Step 3: Reject zero at the common decoder boundary**

Change `_valid_ansi_c_character` before its Unicode-range validation:

```python
if value == 0:
    raise _ShellScanIncomplete("ANSI-C quoted word decodes to NUL")
```

Update the same README audit paragraph to document NUL-producing ANSI-C quoted words as
fail-closed input.

- [x] **Step 4: Run GREEN**

Run the new test, then all of `tests/test_github_ci_shell_scanner.py` and
`tests/test_github_ci_audit.py` with `pytest --no-cov -q`. Expected: PASS.

### Task 3: Escape terminal controls in refresh diffs

**Files:**
- Modify: `tests/test_github_ci_filesystem.py`
- Modify: `src/doc_lattice/github_ci/filesystem.py`
- Modify: `README.md`

- [x] **Step 1: Write the failing terminal-safety regression**

Add a replacement diff test after the stable render cases:

```python
def test_render_diff_escapes_non_line_ending_terminal_controls():
    controls = "".join(
        chr(value)
        for value in (*range(0x20), 0x7F, *range(0x80, 0xA0))
        if value != 0x0A
    )
    artifact = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice.yml"),
        text=f"new:{controls}:suffix\n",
    )
    change = ArtifactChange(
        artifact=artifact,
        root=Path("/repo"),
        destination=Path("/repo/.github/workflows/doc-lattice.yml"),
        action="replace",
        before=f"old:{controls}:suffix\n".encode(),
    )

    rendered = render_diff((change,))

    for value in (*range(0x20), 0x7F, *range(0x80, 0xA0)):
        if value == 0x0A:
            continue
        assert chr(value) not in rendered
        assert f"\\x{value:02x}" in rendered
```

The existing CRLF test remains the positive control for a final line-ending CR.

- [x] **Step 2: Run RED**

Run the new test with `pytest --no-cov -q`. Expected: the raw control-character assertion fails.

- [x] **Step 3: Escape C0, DEL, and C1 controls at record rendering**

Add a helper beside `_render_diff_record`:

```python
def _escape_diff_terminal_controls(record: str) -> str:
    """Render terminal controls visibly while preserving LF and a final CRLF sequence."""
    final_cr = len(record) - 2 if record.endswith("\r\n") else -1
    rendered: list[str] = []
    for index, character in enumerate(record):
        value = ord(character)
        if character == "\n" or index == final_cr:
            rendered.append(character)
        elif value < 0x20 or 0x7F <= value <= 0x9F:
            rendered.append(f"\\x{value:02x}")
        else:
            rendered.append(character)
    return "".join(rendered)
```

Have `_render_diff_record` render the escaped record while using the original record to identify
headers and final-newline state. Update the README refresh contract to say non-line-ending terminal
controls are shown as visible `\xNN` escapes.

- [x] **Step 4: Run GREEN**

Run the new test and `tests/test_github_ci_filesystem.py tests/cli/test_ci.py` with
`pytest --no-cov -q`. Expected: PASS, including the existing CRLF/no-final-newline cases.

### Task 4: Resynchronize ancestors on create retry

**Files:**
- Modify: `tests/test_github_ci_filesystem.py`
- Modify: `src/doc_lattice/github_ci/filesystem.py`
- Modify: `README.md`

- [x] **Step 1: Write the failing two-attempt durability regression**

Add a test beside the parent synchronization coverage. The first attempt fails its first `fsync`
after creating `.github`. The retry preflights again, records directory identities for `fsync` and
leaf publication, and requires a root-directory sync before the write:

```python
def test_apply_create_retry_resynchronizes_existing_ancestor(tmp_path: Path, monkeypatch):
    artifact = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")[0]
    real_fsync = filesystem.os.fsync

    def _fail_parent_sync(_fd: int) -> None:
        raise OSError("synthetic parent sync failure")

    with monkeypatch.context() as context:
        context.setattr(filesystem.os, "fsync", _fail_parent_sync)
        with pytest.raises(ConfigError, match="cannot synchronize managed artifact parent"):
            apply_changes(preflight_create(tmp_path, (artifact,)))

    assert (tmp_path / ".github").is_dir()
    assert not (tmp_path / artifact.relative_path).exists()
    root_stat = tmp_path.stat()
    root_identity = root_stat.st_dev, root_stat.st_ino
    events: list[tuple[str, tuple[int, int]]] = []
    real_create = filesystem.atomic_create_bytes_at

    def _identity(fd: int) -> tuple[int, int]:
        result = os.fstat(fd)
        return result.st_dev, result.st_ino

    def _record_fsync(fd: int) -> None:
        events.append(("fsync", _identity(fd)))
        real_fsync(fd)

    def _record_create(
        directory_fd: int,
        destination_name: str,
        data: bytes,
        *,
        prefix: str,
    ) -> None:
        events.append(("write", _identity(directory_fd)))
        real_create(directory_fd, destination_name, data, prefix=prefix)

    monkeypatch.setattr(filesystem.os, "fsync", _record_fsync)
    monkeypatch.setattr(filesystem, "atomic_create_bytes_at", _record_create)

    apply_changes(preflight_create(tmp_path, (artifact,)))

    assert ("fsync", root_identity) in events
    assert events.index(("fsync", root_identity)) < next(
        index for index, event in enumerate(events) if event[0] == "write"
    )
```

- [x] **Step 2: Run RED**

Run the new test with `pytest --no-cov -q`. Expected: the retry writes successfully but has no
`("fsync", root_identity)` event.

- [x] **Step 3: Synchronize every validated create ancestor**

Initialize `synchronize_parent = create` in `_ensure_locked_artifact_ancestor` rather than only
setting it after this attempt's `FileNotFoundError`. Keep validation before `fsync` and keep
`create=False` behavior unchanged.

Update the README refresh contract to state that create retries resynchronize ancestor entries
before publishing a missing artifact.

- [x] **Step 4: Run GREEN**

Run the new test and all of `tests/test_github_ci_filesystem.py` with `pytest --no-cov -q`.
Expected: PASS.

### Task 5: Full verification, requirement audit, commit, and push

**Files:**
- Verify every modified source, test, README, design, and plan file.

- [x] **Step 1: Run the complete repository gates**

```bash
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev pytest
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ruff format --check src tests
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev ty check src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_typing_boundaries.py src
env UV_CACHE_DIR=/tmp/doc-lattice-review-uv-cache uv run --offline --group dev python scripts/check_version_sync.py
git diff --check
```

Expected: every command exits 0; pytest reports zero failures and coverage at or above 80 percent.

- [x] **Step 2: Audit all four findings against fresh evidence**

Rerun the four focused regression tests, inspect `git diff --stat`, `git diff`, and
`git status --short`, and map every finding to its regression and production boundary. Confirm the
README owns the user-visible fail-closed, terminal-rendering, and retry-durability contracts and
that no unrelated user change is included.

- [ ] **Step 3: Commit the implementation**

```bash
git add README.md src/doc_lattice/github_ci/shell_scanner.py \
  src/doc_lattice/github_ci/filesystem.py tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_filesystem.py \
  docs/superpowers/specs/2026-07-17-ci-audit-terminal-durability-review-fixes-design.md \
  docs/superpowers/plans/2026-07-17-ci-audit-terminal-durability-review-fixes.md
git commit -m "fix: close final ci audit review gaps"
```

Expected: pre-commit hooks pass and the commit contains only the reviewed implementation, tests,
and authoritative README updates.

- [ ] **Step 4: Push the current branch**

```bash
git push origin feature/github-linear-ci-bootstrap-impl
```

Expected: the remote branch advances to the local implementation commit without force-pushing.
