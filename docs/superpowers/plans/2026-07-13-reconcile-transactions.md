# Conflict-Safe Reconcile Transactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make reconcile writes conflict-detecting, durably all-or-nothing, automatically recoverable, and reportable only after commit while consolidating low-level write primitives.

**Architecture:** A new `persistence.py` module owns durable same-directory staging, replacement, create-if-absent, fingerprinting, directory sync, and cleanup. `reconcile.py` produces exact byte snapshots, while `reconcile_transaction.py` serializes reconcile processes with a project-directory `flock` and implements a prepared/committed journal with rollback. The CLI holds the lock through recovery, verified lattice loading, commit, and reporting; cache and `init` reuse only the primitives matching their existing semantics.

**Tech Stack:** Python 3.13+, pathlib/os/tempfile/fcntl/hashlib, Pydantic journal models, Typer, pytest, pytest-mock, uv, ruff, ty

---

## File map

- Create `src/doc_lattice/persistence.py`: shared durable filesystem primitives with no caller error policy.
- Create `src/doc_lattice/reconcile_transaction.py`: advisory lock, typed journal, prepare, commit, rollback, recovery, and dry-run guard.
- Create `tests/test_persistence.py`: low-level write, sync, collision, and cleanup fault injection.
- Create `tests/test_reconcile_transaction.py`: lock, journal validation, recovery, conflict, rollback, and durability fault injection.
- Modify `src/doc_lattice/reconcile.py`: exact-byte `Rewrite` snapshots from fresh reads.
- Modify `src/doc_lattice/cli.py`: recovery selector, lock lifetime, transactional commit, delayed output, shared init create.
- Modify `src/doc_lattice/cache/store.py`: shared atomic replace with unchanged best-effort error policy.
- Modify `src/doc_lattice/scaffold.py`: always-generated `.gitignore` snippet.
- Modify `src/doc_lattice/constants.py`: journal filename and schema version.
- Modify `src/doc_lattice/error_types.py`: distinct in-progress, conflict, and transaction persistence errors.
- Modify `tests/test_reconcile.py`, `tests/test_cli.py`, `tests/test_cache_store.py`, and `tests/test_scaffold.py`: integration and migrated API coverage.
- Modify `ARCHITECTURE.md`, `README.md`, and `CLAUDE.md`: durability, recovery, lock, artifact, and reporting contracts.

### Task 1: Durable shared persistence primitives and cache migration

**Files:**
- Create: `src/doc_lattice/persistence.py`
- Create: `tests/test_persistence.py`
- Modify: `src/doc_lattice/cache/store.py:1-140`
- Modify: `tests/test_cache_store.py:122-190`

- [ ] **Step 1: Write failing persistence tests**

Create `tests/test_persistence.py` with focused tests that assert exact prefixes, unique files,
file and directory sync, atomic replacement, create-if-absent, and cleanup:

```python
"""Tests for shared durable filesystem persistence primitives."""

from pathlib import Path

import pytest

import doc_lattice.persistence as persistence


def test_stage_bytes_uses_unique_same_directory_files(tmp_path: Path):
    destination = tmp_path / "doc.md"
    first = persistence.stage_bytes(
        destination, b"one", prefix=".doc.md.doc-lattice-before."
    )
    second = persistence.stage_bytes(
        destination, b"two", prefix=".doc.md.doc-lattice-before."
    )
    try:
        assert first != second
        assert first.parent == second.parent == tmp_path
        assert first.name.startswith(".doc.md.doc-lattice-before.")
        assert first.suffix == second.suffix == ".tmp"
        assert first.read_bytes() == b"one"
        assert second.read_bytes() == b"two"
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def test_stage_bytes_cleans_temp_when_file_sync_fails(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    monkeypatch.setattr(persistence.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("sync")))
    with pytest.raises(OSError, match="sync"):
        persistence.stage_bytes(destination, b"new", prefix=".doc.md.doc-lattice-after.")
    assert list(tmp_path.iterdir()) == []


def test_replace_staged_publishes_and_syncs_directory(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"old")
    staged = persistence.stage_bytes(destination, b"new", prefix=".doc.md.test.")
    synced: list[Path] = []
    monkeypatch.setattr(persistence, "sync_directory", synced.append)
    persistence.replace_staged(staged, destination)
    assert destination.read_bytes() == b"new"
    assert synced == [tmp_path]


def test_atomic_create_bytes_refuses_existing_and_cleans_temp(tmp_path: Path):
    destination = tmp_path / ".doc-lattice.yml"
    destination.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        persistence.atomic_create_bytes(
            destination, b"new", prefix=".doc-lattice.yml."
        )
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob("*.tmp")) == []


def test_durable_unlink_removes_path_and_syncs_parent(tmp_path: Path, monkeypatch):
    target = tmp_path / "artifact.tmp"
    target.write_bytes(b"x")
    synced: list[Path] = []
    monkeypatch.setattr(persistence, "sync_directory", synced.append)
    persistence.durable_unlink(target)
    assert not target.exists()
    assert synced == [tmp_path]
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `uv run --group dev pytest tests/test_persistence.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'doc_lattice.persistence'`.

- [ ] **Step 3: Implement the minimal persistence module**

Create `src/doc_lattice/persistence.py` with these exact public functions and semantics:

```python
"""Durable low-level filesystem persistence primitives."""

import hashlib
import os
import tempfile
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    """Return the full SHA-256 hex digest for exact bytes."""
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    """Return the full SHA-256 hex digest of a file's exact bytes."""
    return sha256_bytes(path.read_bytes())


def sync_directory(path: Path) -> None:
    """Flush directory-entry changes for an existing directory."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_bytes(destination: Path, data: bytes, *, prefix: str) -> Path:
    """Write and durably stage bytes beside destination under a unique name."""
    fd, tmp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=prefix, suffix=".tmp"
    )
    staged = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(destination.parent)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return staged


def replace_staged(staged: Path, destination: Path) -> None:
    """Atomically replace destination with staged and sync its directory."""
    os.replace(staged, destination)
    sync_directory(destination.parent)


def atomic_replace_bytes(path: Path, data: bytes, *, prefix: str) -> None:
    """Durably replace path with bytes through a unique same-directory stage."""
    staged = stage_bytes(path, data, prefix=prefix)
    try:
        replace_staged(staged, path)
    finally:
        durable_unlink(staged)


def atomic_create_bytes(path: Path, data: bytes, *, prefix: str) -> None:
    """Durably create path without overwriting an existing destination."""
    staged = stage_bytes(path, data, prefix=prefix)
    try:
        os.link(staged, path)
        sync_directory(path.parent)
    finally:
        durable_unlink(staged)


def durable_unlink(path: Path) -> None:
    """Remove path if present and durably publish the removal."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_directory(path.parent)
```

- [ ] **Step 4: Run persistence tests and verify GREEN**

Run: `uv run --group dev pytest tests/test_persistence.py -q`

Expected: all persistence tests pass.

- [ ] **Step 5: Add failing cache delegation coverage**

Extend `tests/test_cache_store.py` with a spy proving the cache delegates to the new primitive while
retaining its one-line, swallowed-error contract:

```python
def test_cache_write_delegates_to_shared_atomic_replace(tmp_path: Path, monkeypatch):
    import doc_lattice.cache.store as store_module

    path = tmp_path / "cache" / CACHE_FILE_NAME
    calls: list[tuple[Path, bytes, str]] = []

    def capture(target: Path, data: bytes, *, prefix: str) -> None:
        calls.append((target, data, prefix))

    monkeypatch.setattr(store_module, "atomic_replace_bytes", capture)
    save_if_changed(path, _sample_cache_file(), None)
    assert calls[0][0] == path
    assert CacheFile.model_validate_json(calls[0][1]) == _sample_cache_file()
    assert calls[0][2] == f"{CACHE_FILE_NAME}."
```

- [ ] **Step 6: Run the delegation test and verify RED**

Run: `uv run --group dev pytest tests/test_cache_store.py::test_cache_write_delegates_to_shared_atomic_replace -q`

Expected: FAIL because `cache.store` has no `atomic_replace_bytes` attribute.

- [ ] **Step 7: Migrate cache persistence**

In `src/doc_lattice/cache/store.py`, remove `tempfile`, remove its private stage/replace code, import
`atomic_replace_bytes`, and make `_write` create the parent then call:

```python
def _write(path: Path, cache_file: CacheFile) -> None:
    """Atomically replace the cache file, emitting one stderr diagnostic on failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace_bytes(
            path,
            cache_file.model_dump_json().encode("utf-8"),
            prefix=f"{CACHE_FILE_NAME}.",
        )
    except OSError as exc:
        sys.stderr.write(f"doc-lattice: could not write load cache at {path}: {exc}\n")
```

Delete the now-unused `contextlib`, `os`, and `tempfile` imports. Update cache fault injection to
patch `store_module.atomic_replace_bytes` rather than `os.replace`; keep assertions that one stderr
line is emitted and no exception escapes.

- [ ] **Step 8: Run persistence and cache suites**

Run: `uv run --group dev pytest tests/test_persistence.py tests/test_cache_store.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add src/doc_lattice/persistence.py src/doc_lattice/cache/store.py tests/test_persistence.py tests/test_cache_store.py
git commit -m "refactor: centralize durable persistence primitives"
```

### Task 2: Exact-byte reconcile rewrite snapshots

**Files:**
- Modify: `src/doc_lattice/reconcile.py:1-190`
- Modify: `src/doc_lattice/cli.py:359-510`
- Modify: `tests/test_reconcile.py:1-80`
- Modify: `tests/test_cli.py:640-730`

- [ ] **Step 1: Write failing exact-byte snapshot tests**

Replace the tuple expectations in `tests/test_reconcile.py` and add exact source-byte assertions:

```python
def test_plan_rewrites_retains_exact_source_and_replacement_bytes():
    path = Path("downstream.md")
    before = b"---\r\nid: d\r\nderives_from:\r\n  - ref: a#x\r\n---\r\nbody\r\n"

    rewrites = plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: before)

    assert len(rewrites) == 1
    rewrite = rewrites[0]
    assert rewrite.path == path
    assert rewrite.before == before
    assert b"seen: newhash" in rewrite.after
    assert rewrite.applied == frozenset({"a#x"})


def test_plan_rewrites_wraps_invalid_utf8_with_path():
    path = Path("downstream.md")
    with pytest.raises(UnreadableDocError, match="cannot read downstream.md to reconcile"):
        plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: b"\xff")
```

- [ ] **Step 2: Run snapshot tests and verify RED**

Run: `uv run --group dev pytest tests/test_reconcile.py::test_plan_rewrites_retains_exact_source_and_replacement_bytes tests/test_reconcile.py::test_plan_rewrites_wraps_invalid_utf8_with_path -q`

Expected: FAIL because `plan_rewrites` still accepts text and returns tuples.

- [ ] **Step 3: Implement the frozen rewrite model and byte reader**

Add this model near the imports in `src/doc_lattice/reconcile.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rewrite:
    """One validated fresh-read document replacement."""

    path: Path
    before: bytes
    after: bytes
    applied: frozenset[str]
```

Change `plan_rewrites` to accept `Callable[[Path], bytes]`, decode UTF-8 only for
`apply_reconcile`, and return `Rewrite` objects:

```python
def plan_rewrites(
    plan: dict[Path, dict[str, str]],
    read_bytes: Callable[[Path], bytes],
) -> list[Rewrite]:
    """Compute exact-byte fresh-read reconcile rewrites before mutation."""
    rewrites: list[Rewrite] = []
    for path, updates in plan.items():
        try:
            before = read_bytes(path)
            fresh = before.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read {path} to reconcile: {exc}"
            raise UnreadableDocError(msg) from exc
        new_text, applied = apply_reconcile(fresh, updates)
        if applied:
            rewrites.append(
                Rewrite(
                    path=path,
                    before=before,
                    after=new_text.encode("utf-8"),
                    applied=frozenset(applied),
                )
            )
    return rewrites
```

- [ ] **Step 4: Adapt current CLI reporting without changing write semantics yet**

Import `Rewrite` from `reconcile`, delete the CLI tuple alias, change report comprehensions to use
`rewrite.path` and `rewrite.applied`, call `_atomic_write` with
`rewrite.after.decode("utf-8")`, and invoke `plan_rewrites` with:

```python
rewrites = plan_rewrites(plan, lambda path: write_paths[path].read_bytes())
```

This keeps the suite green until the transaction layer replaces `_atomic_write` in Task 5.

- [ ] **Step 5: Run reconcile unit and CLI tests**

Run: `uv run --group dev pytest tests/test_reconcile.py tests/test_cli.py -q`

Expected: all tests pass with the new `Rewrite` API.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/doc_lattice/reconcile.py src/doc_lattice/cli.py tests/test_reconcile.py tests/test_cli.py
git commit -m "refactor: retain exact reconcile source bytes"
```

### Task 3: Advisory lock, journal validation, and explicit recovery

**Files:**
- Create: `src/doc_lattice/reconcile_transaction.py`
- Create: `tests/test_reconcile_transaction.py`
- Modify: `src/doc_lattice/constants.py:1-60`
- Modify: `src/doc_lattice/error_types.py:1-80`

- [ ] **Step 1: Add constants and typed error tests first**

Extend `tests/test_reconcile_transaction.py` with imports that do not yet exist and assertions for
lock contention and a read-only journal guard:

```python
"""Tests for recoverable reconcile transactions."""

import json
from pathlib import Path

import pytest

from doc_lattice.constants import RECONCILE_JOURNAL_NAME
from doc_lattice.error_types import ReconcileInProgressError, ReconcilePersistenceError
from doc_lattice.reconcile_transaction import (
    ensure_dry_run_safe,
    reconcile_lock,
    recover_transaction,
)


def test_reconcile_lock_refuses_a_second_live_holder(tmp_path: Path):
    with reconcile_lock(tmp_path):
        with pytest.raises(ReconcileInProgressError, match="another reconcile is in progress"):
            with reconcile_lock(tmp_path):
                pass


def test_dry_run_guard_names_journal_without_mutating_it(tmp_path: Path):
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal.write_text("sentinel", encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(ReconcilePersistenceError, match="run 'doc-lattice reconcile --recover'"):
        ensure_dry_run_safe(tmp_path)
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before
```

- [ ] **Step 2: Run lock tests and verify RED**

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py -q`

Expected: collection fails because the constants, errors, and transaction module are missing.

- [ ] **Step 3: Add constants and errors**

In `src/doc_lattice/constants.py` add:

```python
RECONCILE_JOURNAL_NAME: str = ".doc-lattice-reconcile.json"
RECONCILE_JOURNAL_VERSION: int = 1
```

In `src/doc_lattice/error_types.py` add classes whose constructors accept only `message: str` so
`tests/test_conventions.py` can instantiate them:

```python
class ReconcileInProgressError(ProjectError):
    """Another reconcile process owns the project lock."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="RECONCILE_IN_PROGRESS")


class ReconcileConflictError(ProjectError):
    """A reconciled destination changed after validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="RECONCILE_CONFLICT")


class ReconcilePersistenceError(ProjectError):
    """A reconcile transaction could not commit or recover safely."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="RECONCILE_PERSISTENCE")
```

- [ ] **Step 4: Implement lock, models, containment, and dry-run guard**

Create `src/doc_lattice/reconcile_transaction.py` with:

```python
"""Durable journal and recovery protocol for reconcile document writes."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .constants import RECONCILE_JOURNAL_NAME, RECONCILE_JOURNAL_VERSION
from .error_types import ReconcileInProgressError, ReconcilePersistenceError
from .path_utils import safe_resolve

JournalState = Literal["prepared", "committed"]
RecoveryAction = Literal["none", "rolled_back", "cleaned_committed"]


class JournalEntry(BaseModel):
    """Contained paths and fingerprints for one document replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    destination: str
    before_path: str
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_path: str
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Journal(BaseModel):
    """Versioned durable reconcile transaction record."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int
    state: JournalState
    entries: tuple[JournalEntry, ...]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Outcome of checking and recovering one project journal."""

    action: RecoveryAction
    journal: Path


@contextmanager
def reconcile_lock(project_root: Path) -> Iterator[None]:
    """Hold a nonblocking advisory lock on the existing project directory."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(project_root, flags)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            msg = "another reconcile is in progress; retry after it exits"
            raise ReconcileInProgressError(msg) from exc
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ensure_dry_run_safe(project_root: Path) -> None:
    """Refuse dry-run when recovery artifacts exist, without changing them."""
    journal = project_root / RECONCILE_JOURNAL_NAME
    if journal.exists():
        msg = (
            f"cannot dry-run while reconcile journal {journal} exists; "
            "run 'doc-lattice reconcile --recover' first"
        )
        raise ReconcilePersistenceError(msg)
```

Add private `_journal_path`, `_relative`, `_contained`, and `_load_journal` helpers. `_load_journal`
must call `Journal.model_validate_json`, require `version == RECONCILE_JOURNAL_VERSION`, reject
absolute or escaping paths through `safe_resolve`, and wrap `(OSError, UnicodeDecodeError,
ValidationError)` in `ReconcilePersistenceError` whose message names the journal and gives the
manual sequence: inspect destinations and staged files, move the invalid journal aside, then rerun
`doc-lattice reconcile --recover`.

- [ ] **Step 5: Run lock and convention tests**

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py::test_reconcile_lock_refuses_a_second_live_holder tests/test_reconcile_transaction.py::test_dry_run_guard_names_journal_without_mutating_it tests/test_conventions.py::test_all_error_types_extend_project_error_with_code -q`

Expected: all tests pass.

- [ ] **Step 6: Add failing prepared and committed recovery tests**

Add a test helper that writes exact before/after artifacts and a journal, then test both states:

```python
def _write_journal(
    root: Path, destination: Path, before: Path, after: Path, *, state: str
) -> Path:
    payload = {
        "version": 1,
        "state": state,
        "entries": [{
            "destination": str(destination.relative_to(root)),
            "before_path": str(before.relative_to(root)),
            "before_sha256": persistence.file_sha256(before),
            "after_path": str(after.relative_to(root)),
            "after_sha256": persistence.file_sha256(after),
        }],
    }
    journal = root / RECONCILE_JOURNAL_NAME
    journal.write_text(json.dumps(payload), encoding="utf-8")
    return journal


def test_recover_prepared_restores_after_image_and_cleans_artifacts(tmp_path: Path):
    destination = tmp_path / "doc.md"
    before = tmp_path / ".doc.md.doc-lattice-before.a.tmp"
    after = tmp_path / ".doc.md.doc-lattice-after.a.tmp"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    destination.write_bytes(b"after")
    journal = _write_journal(tmp_path, destination, before, after, state="prepared")
    result = recover_transaction(tmp_path)
    assert result.action == "rolled_back"
    assert destination.read_bytes() == b"before"
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


def test_recover_committed_keeps_destination_and_cleans_artifacts(tmp_path: Path):
    destination = tmp_path / "doc.md"
    before = tmp_path / ".doc.md.doc-lattice-before.a.tmp"
    after = tmp_path / ".doc.md.doc-lattice-after.a.tmp"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    destination.write_bytes(b"after")
    journal = _write_journal(tmp_path, destination, before, after, state="committed")
    result = recover_transaction(tmp_path)
    assert result.action == "cleaned_committed"
    assert destination.read_bytes() == b"after"
    assert not journal.exists()
```

- [ ] **Step 7: Run recovery tests and verify RED**

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py -q`

Expected: FAIL because `recover_transaction` is not implemented.

- [ ] **Step 8: Implement idempotent rollback and cleanup recovery**

Implement `recover_transaction(project_root: Path) -> RecoveryResult` plus private helpers:

```python
def recover_transaction(project_root: Path) -> RecoveryResult:
    """Roll back prepared work or finish cleanup for a committed journal."""
    journal_path = project_root / RECONCILE_JOURNAL_NAME
    if not journal_path.exists():
        return RecoveryResult(action="none", journal=journal_path)
    journal = _load_journal(project_root, journal_path)
    if journal.state == "committed":
        _cleanup(project_root, journal, journal_path)
        return RecoveryResult(action="cleaned_committed", journal=journal_path)
    _rollback(project_root, journal, journal_path)
    return RecoveryResult(action="rolled_back", journal=journal_path)
```

`_rollback` must iterate entries in reverse. If destination bytes equal `after_sha256`, verify the
before-image exists and matches `before_sha256`, then call `replace_staged(before, destination)`.
If destination is missing or matches neither fingerprint, preserve it. After every destination is
safe, durably unlink remaining before/after paths, then unlink the journal last. `_cleanup` durably
unlinks before/after paths and the committed journal without changing any destination. Wrap an
unsafe or failed recovery in `ReconcilePersistenceError` and leave the journal in place.

- [ ] **Step 9: Run transaction recovery tests**

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py tests/test_conventions.py -q`

Expected: all tests pass.

- [ ] **Step 10: Commit Task 3**

```bash
git add src/doc_lattice/constants.py src/doc_lattice/error_types.py src/doc_lattice/reconcile_transaction.py tests/test_reconcile_transaction.py
git commit -m "feat: add serialized reconcile recovery"
```

### Task 4: Prepared commit, conflict detection, and rollback fault handling

**Files:**
- Modify: `src/doc_lattice/reconcile_transaction.py`
- Modify: `tests/test_reconcile_transaction.py`

- [ ] **Step 1: Write a failing successful-commit test**

```python
def test_commit_rewrites_durably_replaces_batch_and_cleans_artifacts(tmp_path: Path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        Rewrite(first, b"old first", b"new first", frozenset({"up#a"})),
        Rewrite(second, b"old second", b"new second", frozenset({"up#b"})),
    ]
    commit_rewrites(tmp_path, rewrites, {first: first, second: second})
    assert first.read_bytes() == b"new first"
    assert second.read_bytes() == b"new second"
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2: Run the commit test and verify RED**

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py::test_commit_rewrites_durably_replaces_batch_and_cleans_artifacts -q`

Expected: FAIL because `commit_rewrites` is missing.

- [ ] **Step 3: Implement prepare and successful commit**

Add `commit_rewrites(project_root, rewrites, write_paths)` and private `_prepare`. `_prepare` must:

1. Stage every `rewrite.before` with
   `prefix=f".{destination.name}.doc-lattice-before."`.
2. Stage every `rewrite.after` with
   `prefix=f".{destination.name}.doc-lattice-after."`.
3. Build ordered `JournalEntry` values with project-relative paths and full fingerprints.
4. Atomically create the prepared journal using
   `atomic_create_bytes(journal_path, journal.model_dump_json().encode("utf-8"),
   prefix=".doc-lattice-reconcile.json.")`.
5. On preparation failure, durably remove every stage created by this attempt.

The success path must re-fingerprint each destination immediately before `replace_staged`, replace
all after-images, atomically replace the journal with state `committed` using the pinned journal
prefix, then call `_cleanup`. The journal state update and every destination replacement must use
the shared functions that sync parent-directory metadata.

- [ ] **Step 4: Run the successful commit test and verify GREEN**

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py::test_commit_rewrites_durably_replaces_batch_and_cleans_artifacts -q`

Expected: PASS.

- [ ] **Step 5: Write failing conflict and mid-batch rollback tests**

```python
def test_commit_detects_edit_before_replace_and_preserves_it(tmp_path: Path, monkeypatch):
    destination = tmp_path / "doc.md"
    destination.write_bytes(b"validated")
    rewrite = Rewrite(destination, b"validated", b"replacement", frozenset({"up#x"}))
    real_file_sha256 = transaction.file_sha256
    calls = 0

    def edit_then_hash(path: Path) -> str:
        nonlocal calls
        calls += 1
        if path == destination and calls == 1:
            destination.write_bytes(b"editor change")
        return real_file_sha256(path)

    monkeypatch.setattr(transaction, "file_sha256", edit_then_hash)
    with pytest.raises(ReconcileConflictError, match=r"doc\.md.*changed after validation"):
        commit_rewrites(tmp_path, [rewrite], {destination: destination})
    assert destination.read_bytes() == b"editor change"
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()


def test_second_replace_failure_rolls_back_first_and_reports_no_commit(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    rewrites = [
        Rewrite(first, b"old first", b"new first", frozenset({"up#a"})),
        Rewrite(second, b"old second", b"new second", frozenset({"up#b"})),
    ]
    real_replace = transaction.replace_staged
    after_replaces = 0

    def fail_second_after(staged: Path, destination: Path) -> None:
        nonlocal after_replaces
        if "doc-lattice-after" in staged.name:
            after_replaces += 1
            if after_replaces == 2:
                raise OSError("disk full")
        real_replace(staged, destination)

    monkeypatch.setattr(transaction, "replace_staged", fail_second_after)
    with pytest.raises(ReconcilePersistenceError, match=r"disk full.*rollback complete"):
        commit_rewrites(tmp_path, rewrites, {first: first, second: second})
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"
    assert not (tmp_path / RECONCILE_JOURNAL_NAME).exists()
```

- [ ] **Step 6: Run conflict and rollback tests and verify RED**

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py -q`

Expected: the new tests fail because commit errors are not yet converted and rolled back.

- [ ] **Step 7: Implement typed abort handling**

On a fingerprint mismatch, create a `ReconcileConflictError` naming the destination. On any
`OSError` before the committed marker is durably synced, create a `ReconcilePersistenceError`
naming the operation, path, and OS error. Before rollback, durably restore the journal to
`prepared` state so a failed committed-marker sync cannot leave a visible `committed` marker
governing an attempted rollback. Then call `_rollback`.

Track a `committed` boolean that becomes true only after the committed journal replacement and
project-root sync return. A cleanup failure after that point must not roll documents back: keep the
visible committed journal whenever possible and raise a persistence error instructing the next
real reconcile or `--recover` to finish cleanup. `_cleanup` removes the journal last.

If rollback succeeds, raise the original typed error with `; no files were reconciled (rollback
complete)` appended. If rollback fails, raise `ReconcilePersistenceError` naming both failures,
the journal, and `doc-lattice reconcile --recover`; retain the journal and all artifacts still
needed by recovery.

- [ ] **Step 8: Add and pass directory-sync and cleanup fault tests**

Inject a one-shot `sync_directory` failure after the first after-image replacement and assert both
destinations return to their before bytes. Inject cleanup failure with a committed journal and
assert the documents keep after bytes, the journal stays `committed`, no success is returned, and
`recover_transaction` later finishes cleanup without changing the documents.

Run: `uv run --group dev pytest tests/test_reconcile_transaction.py tests/test_persistence.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit Task 4**

```bash
git add src/doc_lattice/reconcile_transaction.py tests/test_reconcile_transaction.py
git commit -m "feat: commit reconcile batches transactionally"
```

### Task 5: CLI recovery mode, durable reporting, and init scaffold integration

**Files:**
- Modify: `src/doc_lattice/cli.py:1-55,359-510,600-700`
- Modify: `src/doc_lattice/scaffold.py:1-145`
- Modify: `tests/test_cli.py:570-760,1100-1190`
- Modify: `tests/test_scaffold.py:1-140`

- [ ] **Step 1: Write failing CLI behavior tests**

Add integration tests for the required selector, dry-run, and output contracts:

```python
def test_reconcile_recover_without_journal_reports_none(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--recover", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["action"] == "none"


def test_reconcile_recover_rejects_selection_flags(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "pc-design", "--recover"])
    assert result.exit_code == 2
    assert "--recover cannot be combined" in result.stderr


def test_reconcile_dry_run_refuses_journal_without_mutating(tmp_path: Path, monkeypatch):
    journal = tmp_path / ".doc-lattice-reconcile.json"
    journal.write_text("sentinel", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    before = journal.read_bytes()
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])
    assert result.exit_code == 2
    assert "--recover" in result.stderr
    assert journal.read_bytes() == before


def test_reconcile_midbatch_failure_reports_no_durable_outcomes(
    tmp_path: Path, monkeypatch
):
    project = _two_downstream_project(tmp_path)
    monkeypatch.chdir(project)
    real_replace = transaction.replace_staged
    after_replaces = 0

    def fail_second_after(staged: Path, destination: Path) -> None:
        nonlocal after_replaces
        if "doc-lattice-after" in staged.name:
            after_replaces += 1
            if after_replaces == 2:
                raise OSError("disk full")
        real_replace(staged, destination)

    monkeypatch.setattr(transaction, "replace_staged", fail_second_after)
    result = runner.invoke(app, ["reconcile", "--all"])
    assert result.exit_code == 2
    assert "reconciled " not in result.stdout
    assert "disk full" in result.stderr
```

Create `_two_downstream_project` by extracting the existing setup from
`test_reconcile_real_run_reports_progress_before_midbatch_write_error`; return the project root.

- [ ] **Step 2: Run the new CLI tests and verify RED**

Run: `uv run --group dev pytest tests/test_cli.py -k 'reconcile and (recover or midbatch or dry_run_refuses)' -q`

Expected: FAIL because `--recover` and transactional CLI wiring do not exist.

- [ ] **Step 3: Replace the CLI write loop with locked transaction flow**

Add `recover: bool` to the Typer command. Reject `--recover` combined with downstream id, `--all`,
`--ref`, or `--dry-run`; retain the existing id-or-all validation for normal mode.

Inside `_exit_on_project_error`, load config, then hold:

```python
with reconcile_lock(project.project_root):
    if dry_run:
        ensure_dry_run_safe(project.project_root)
    else:
        recovery = recover_transaction(project.project_root)
        if recover:
            _report_recovery(recovery, json_out=json_out)
            return
        if recovery.action != "none":
            _err.print(f"recovered reconcile transaction: {recovery.action}")

    lattice = load_lattice(project, require_verified=True)
    plan = plan_reconcile(lattice, downstream_id, ref=ref, reconcile_all=reconcile_all)
    write_paths = _resolve_reconcile_write_paths(plan, project.project_root)
    rewrites = plan_rewrites(plan, lambda path: write_paths[path].read_bytes())
    if not dry_run and rewrites:
        commit_rewrites(project.project_root, rewrites, write_paths)
    _report_reconcile(plan, rewrites, dry_run=dry_run, json_out=json_out)
```

Delete `_atomic_write` and the per-file progress loop. `_report_reconcile` must only format already
committed rewrites, so both human and JSON output happen after `commit_rewrites` returns. Add
`_report_recovery`; JSON output is exactly:

```python
{"action": recovery.action, "journal": str(recovery.journal)}
```

- [ ] **Step 4: Run all reconcile CLI tests**

Run: `uv run --group dev pytest tests/test_cli.py -k reconcile -q`

Expected: all reconcile tests pass after replacing the obsolete partial-progress assertion with
the no-output, fully-rolled-back assertion.

- [ ] **Step 5: Write failing scaffold and init output tests**

In `tests/test_scaffold.py` and `tests/test_cli.py` add:

```python
def test_scaffold_gitignore_matches_transaction_artifact_names():
    text = build_scaffold(("docs",), None, "1.0.0").gitignore_text
    assert text.splitlines() == [
        ".doc-lattice-reconcile.json",
        ".doc-lattice-reconcile.json.*.tmp",
        ".*.doc-lattice-before.*.tmp",
        ".*.doc-lattice-after.*.tmp",
    ]


def test_init_always_prints_gitignore_guidance_with_existing_file(
    tmp_path: Path, monkeypatch
):
    (tmp_path / ".gitignore").write_text("existing\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "# ===== .gitignore" in result.stdout
    assert ".doc-lattice-reconcile.json" in result.stdout
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "existing\n"
```

- [ ] **Step 6: Run scaffold tests and verify RED**

Run: `uv run --group dev pytest tests/test_scaffold.py tests/test_cli.py -k 'gitignore or init' -q`

Expected: FAIL because `Scaffold.gitignore_text` is missing and init does not print it.

- [ ] **Step 7: Add unconditional printed gitignore scaffold and shared init create**

Extend `Scaffold` with `gitignore_text: str`, add:

```python
def render_gitignore() -> str:
    """Render ignore patterns for recoverable reconcile artifacts."""
    return (
        ".doc-lattice-reconcile.json\n"
        ".doc-lattice-reconcile.json.*.tmp\n"
        ".*.doc-lattice-before.*.tmp\n"
        ".*.doc-lattice-after.*.tmp\n"
    )
```

Populate it in `build_scaffold`. In `init`, replace `_atomic_create` with
`atomic_create_bytes(target, scaffold.config_text.encode("utf-8"), prefix=f"{target.name}.")`,
delete `_atomic_create`, and always print the gitignore block before the pre-commit block:

```python
typer.echo("# ===== .gitignore (append these lines) =====")
typer.echo(scaffold.gitignore_text)
```

Update the final stderr instruction to mention appending the gitignore block. Do not inspect or
write `.gitignore`.

- [ ] **Step 8: Run CLI, scaffold, cache, and persistence suites**

Run: `uv run --group dev pytest tests/test_cli.py tests/test_scaffold.py tests/test_cache_store.py tests/test_persistence.py tests/test_reconcile_transaction.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit Task 5**

```bash
git add src/doc_lattice/cli.py src/doc_lattice/scaffold.py tests/test_cli.py tests/test_scaffold.py
git commit -m "feat: expose recoverable reconcile transactions"
```

### Task 6: Documentation, architecture synchronization, and full verification

**Files:**
- Modify: `ARCHITECTURE.md:77-95`
- Modify: `README.md:145-280,360-385,445-470`
- Modify: `CLAUDE.md:35-105`
- Modify: `tests/test_package_metadata.py`

- [ ] **Step 1: Add a failing documentation regression test**

Add to `tests/test_package_metadata.py`:

```python
def test_supported_docs_describe_conflict_safe_reconcile():
    root = Path(__file__).parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    architecture = (root / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for text in (readme, architecture):
        assert "edit racing after validation may be overwritten" not in text
        assert "multi-file run is not transactional" not in text
        assert ".doc-lattice-reconcile.json" in text
        assert "--recover" in text
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run: `uv run --group dev pytest tests/test_package_metadata.py::test_supported_docs_describe_conflict_safe_reconcile -q`

Expected: FAIL on the admitted race and non-transactional wording.

- [ ] **Step 3: Update supported documentation**

Rewrite AD-5 in `ARCHITECTURE.md` to define exact-byte fresh-read fingerprints, immediate
pre-replace checks, staged before/after files, `prepared` and `committed` journal states, reverse
rollback, project-directory advisory locking, file and directory sync, and output after commit.

Update README command/options and reconcile sections to cover:

- `reconcile --recover` and its JSON actions;
- automatic recovery only for real reconcile runs;
- strictly read-only dry-run refusal when a journal exists;
- conflict errors naming the changed destination versus I/O/durability errors;
- local-filesystem lock/durability assumption and the weaker semantics of network mounts;
- journal and exact temp glob names plus the always-printed init `.gitignore` block;
- human and JSON success output only after the durable batch commit.

Update `CLAUDE.md` current architecture to remove both admitted failure modes, name
`persistence.py` and `reconcile_transaction.py` as impure owners, and state the tests and path
containment invariants for recovery artifacts.

- [ ] **Step 4: Run docs, conventions, format, lint, and type checks**

Run:

```bash
uv run --group dev pytest tests/test_package_metadata.py tests/test_conventions.py -q
uv run --group dev ruff format --check src tests
uv run --group dev ruff check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
```

Expected: every command exits 0 with no formatting, lint, type, boundary, or version errors.

- [ ] **Step 5: Run the full test suite**

Run: `uv run --group dev pytest`

Expected: all tests pass and coverage remains at least 80 percent.

- [ ] **Step 6: Inspect the complete diff against issue #86 before the docs commit**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
```

Expected: no whitespace errors, only issue-86 files are changed, and the worktree is clean after
the final commit. Before Step 7, `git status --short` should list only the four Task 6 files.

- [ ] **Step 7: Commit Task 6**

```bash
git add ARCHITECTURE.md README.md CLAUDE.md tests/test_package_metadata.py
git commit -m "docs: document durable reconcile recovery"
```

- [ ] **Step 8: Confirm the final commit is clean**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
```

Expected: no whitespace errors and empty status. The full verification commands in Steps 4 and 5
must be rerun after this commit if hooks modify any file.
