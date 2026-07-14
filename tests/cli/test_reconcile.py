"""CLI integration tests for the reconcile command."""

import json
import os
import shutil
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

import doc_lattice.cli.commands.reconcile as reconcile_command
import doc_lattice.cli.runtime as runtime_module
import doc_lattice.reconcile_transaction as transaction
from doc_lattice.cli import app
from doc_lattice.constants import RECONCILE_JOURNAL_NAME, RECONCILE_JOURNAL_VERSION
from doc_lattice.error_types import ReconcilePersistenceError
from doc_lattice.reconcile_transaction import (
    Journal,
    JournalEntry,
    JournalState,
    reconcile_lock,
)

from .helpers import _clean_docs, _run, runner


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    """Capture every namespace entry without following symlinks or reading special files."""
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            entry = ("symlink", os.fsencode(path.readlink()))
        elif stat.S_ISREG(mode):
            entry = ("file", path.read_bytes())
        elif stat.S_ISDIR(mode):
            entry = ("directory", b"")
        else:
            entry = ("special", b"")
        snapshot[path.relative_to(root).as_posix()] = entry
    return snapshot


def _write_cli_transaction(
    root: Path,
    destination: Path,
    before_bytes: bytes,
    after_bytes: bytes,
    *,
    state: JournalState = "prepared",
) -> tuple[Path, Path, Path]:
    """Write a valid single-entry recovery transaction for CLI integration tests."""
    before = destination.with_name(f".{destination.name}.doc-lattice-before.test.tmp")
    after = destination.with_name(f".{destination.name}.doc-lattice-after.test.tmp")
    journal = root / RECONCILE_JOURNAL_NAME
    before.write_bytes(before_bytes)
    after.write_bytes(after_bytes)
    entry = JournalEntry(
        destination=destination.relative_to(root).as_posix(),
        before_path=before.relative_to(root).as_posix(),
        before_sha256=sha256(before_bytes).hexdigest(),
        after_path=after.relative_to(root).as_posix(),
        after_sha256=sha256(after_bytes).hexdigest(),
    )
    journal.write_text(
        Journal(
            version=RECONCILE_JOURNAL_VERSION,
            state=state,
            entries=(entry,),
        ).model_dump_json(),
        encoding="utf-8",
    )
    return journal, before, after


def _two_downstream_project(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#s}\nupstream body\n", encoding="utf-8")
    for name in ("down-a", "down-b"):
        (docs / f"{name}.md").write_text(
            f"---\nid: {name}\nderives_from:\n  - ref: up#s\n---\n# {name}\nbody\n",
            encoding="utf-8",
        )
    return tmp_path


def test_reconcile_unknown_id_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "does-not-exist"])
    assert result.exit_code == 2


def test_reconcile_then_check_clean(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "pc-design"]).exit_code == 0
    after = runner.invoke(app, ["check"])
    # gdd's BROKEN ref still drifts, so check is still 1; pc-design itself is clean.
    pc_check = runner.invoke(app, ["check", "--json"])
    payload = json.loads(pc_check.stdout)
    pc_states = [e["state"] for e in payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]
    assert after.exit_code == 1


def test_reconcile_writes_through_in_project_symlink(tmp_path: Path, monkeypatch):
    project_root = tmp_path / "repo"
    docs = project_root / "docs"
    shared = project_root / "shared"
    docs.mkdir(parents=True)
    shared.mkdir()
    (project_root / ".doc-lattice.yml").write_text('docs_roots: ["docs"]\n', encoding="utf-8")
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nupstream\n", encoding="utf-8")
    target = shared / "down.md"
    target.write_text(
        "---\nid: down\nderives_from:\n  - ref: up#sec\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    link = docs / "down.md"
    link.symlink_to(Path("../shared/down.md"))
    before = target.read_text(encoding="utf-8")
    monkeypatch.chdir(project_root)

    result = runner.invoke(app, ["reconcile", "down"])

    assert result.exit_code == 0
    assert link.is_symlink()
    rewritten = target.read_text(encoding="utf-8")
    assert rewritten != before
    assert "seen:" in rewritten
    assert link.read_text(encoding="utf-8") == rewritten


def test_reconcile_all_without_positional_id(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all"])
    assert result.exit_code == 0
    payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    pc_states = [e["state"] for e in payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_all_skips_broken_edge(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "--all"]).exit_code == 0
    payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    states = {(e["source_id"], e["target_ref"]): e["state"] for e in payload["edges"]}
    assert states[("gdd", "ghost")] == "BROKEN"
    assert runner.invoke(app, ["check"]).exit_code == 1


def test_reconcile_requires_id_or_all(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 2


def test_reconcile_recover_without_journal_reports_none_human(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "nothing to recover" in result.stdout
    assert str(lattice_dir / RECONCILE_JOURNAL_NAME) in result.stdout


def test_reconcile_recover_without_journal_reports_exact_json(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--recover", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "action": "none",
        "journal": str(lattice_dir / RECONCILE_JOURNAL_NAME),
    }


def test_reconcile_recover_rolls_back_prepared_without_planning(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    destination = docs / "down.md"
    before_bytes = b"original document\n"
    after_bytes = b"transaction document\n"
    destination.write_bytes(after_bytes)
    journal, before, after = _write_cli_transaction(
        tmp_path, destination, before_bytes, after_bytes
    )
    monkeypatch.chdir(tmp_path)

    def fail_if_loaded(*_args, **_kwargs):
        pytest.fail("recovery-only mode loaded or planned a lattice")

    monkeypatch.setattr(runtime_module, "load_lattice", fail_if_loaded)
    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 0
    assert "rolled back reconcile transaction" in result.stdout
    assert str(journal) in result.stdout
    assert destination.read_bytes() == before_bytes
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


def test_reconcile_recover_cleans_committed_without_planning(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    destination = docs / "down.md"
    before_bytes = b"original document\n"
    after_bytes = b"committed document\n"
    destination.write_bytes(after_bytes)
    journal, before, after = _write_cli_transaction(
        tmp_path,
        destination,
        before_bytes,
        after_bytes,
        state="committed",
    )
    monkeypatch.chdir(tmp_path)

    def fail_if_loaded(*_args, **_kwargs):
        pytest.fail("recovery-only mode loaded or planned a lattice")

    monkeypatch.setattr(runtime_module, "load_lattice", fail_if_loaded)
    result = runner.invoke(app, ["reconcile", "--recover", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "action": "cleaned_committed",
        "journal": str(journal),
    }
    assert destination.read_bytes() == after_bytes
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["downstream", "--recover"],
        ["--recover", "--all"],
        ["--recover", "--ref", "upstream"],
        ["--recover", "--dry-run"],
    ],
    ids=["positional", "all", "ref", "dry-run"],
)
def test_reconcile_recover_rejects_selection_and_dry_run_flags(
    tmp_path: Path, monkeypatch, args: list[str]
):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["reconcile", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--recover cannot be combined" in result.stderr


def test_reconcile_dry_run_refuses_journal_without_mutating_or_loading(
    lattice_dir: Path, monkeypatch
):
    journal = lattice_dir / RECONCILE_JOURNAL_NAME
    journal.write_bytes(b"sentinel journal bytes\n")
    before = _tree_snapshot(lattice_dir)
    monkeypatch.chdir(lattice_dir)

    def fail_if_loaded(*_args, **_kwargs):
        pytest.fail("dry-run loaded the lattice before refusing recovery")

    monkeypatch.setattr(runtime_module, "load_lattice", fail_if_loaded)
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert str(journal) in result.stderr
    assert "--recover" in result.stderr
    assert _tree_snapshot(lattice_dir) == before


@pytest.mark.parametrize(
    "args",
    [
        ["reconcile", "--recover", "--json"],
        ["reconcile", "--all"],
        ["reconcile", "--all", "--dry-run"],
    ],
    ids=["recover-json", "real-run", "dry-run"],
)
def test_reconcile_dangling_journal_symlink_never_reports_success_or_mutates_empty_project(
    tmp_path: Path, monkeypatch, args: list[str]
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "node.md").write_text("---\nid: node\n---\n# Node\nbody\n", encoding="utf-8")
    journal = tmp_path / RECONCILE_JOURNAL_NAME
    journal.symlink_to("missing-journal-target")
    before = _tree_snapshot(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert str(journal) in result.stderr
    assert "symlink" in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert _tree_snapshot(tmp_path) == before


def test_reconcile_dangling_journal_symlink_blocks_nonempty_real_plan(
    lattice_dir: Path, monkeypatch
):
    journal = lattice_dir / RECONCILE_JOURNAL_NAME
    journal.symlink_to("missing-journal-target")
    before = _tree_snapshot(lattice_dir)
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "--all"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "symlink" in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert _tree_snapshot(lattice_dir) == before


def test_reconcile_dry_run_does_not_mutate_external_load_cache(
    lattice_dir: Path, tmp_path: Path, monkeypatch
):
    cache_home = tmp_path / "xdg"
    cache_file = cache_home / "doc-lattice" / "dry-run-proof" / "load-cache.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"existing cache sentinel\n")
    (lattice_dir / ".doc-lattice.yml").write_text(
        "cache_key: dry-run-proof\ncache_trust_stat: true\n",
        encoding="utf-8",
    )
    project_before = _tree_snapshot(lattice_dir)
    cache_before = _tree_snapshot(cache_home)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])

    assert result.exit_code == 0
    assert _tree_snapshot(lattice_dir) == project_before
    assert _tree_snapshot(cache_home) == cache_before


def test_reconcile_lock_contention_does_not_inspect_or_mutate_journal(
    lattice_dir: Path, monkeypatch
):
    journal = lattice_dir / RECONCILE_JOURNAL_NAME
    journal.write_bytes(b"not even valid journal json\n")
    before = _tree_snapshot(lattice_dir)
    monkeypatch.chdir(lattice_dir)

    with reconcile_lock(lattice_dir):
        result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "another reconcile is in progress" in result.stderr
    assert "invalid reconcile journal" not in result.stderr
    assert _tree_snapshot(lattice_dir) == before


@pytest.mark.parametrize("failure", ["open", "flock", "fstat"])
def test_reconcile_lock_setup_failure_is_typed_without_internal_error_or_mutation(
    lattice_dir: Path, monkeypatch, failure: str
):
    before = _tree_snapshot(lattice_dir)
    real_open = transaction.os.open
    real_flock = transaction._flock

    if failure == "open":

        def fail_open(path: Path, flags: int) -> int:
            if Path(path) == lattice_dir:
                raise PermissionError("injected open failure")
            return real_open(path, flags)

        monkeypatch.setattr(transaction.os, "open", fail_open)
    elif failure == "flock":

        def fail_flock(fd: int, *, release: bool) -> None:
            if not release:
                raise OSError("injected flock failure")
            real_flock(fd, release=release)

        monkeypatch.setattr(transaction, "_flock", fail_flock)
    else:
        monkeypatch.setattr(
            transaction.os,
            "fstat",
            lambda _fd: (_ for _ in ()).throw(OSError("injected fstat failure")),
        )
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "--recover", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert f"injected {failure} failure" in result.stderr
    assert "internal error" not in result.stderr
    assert "Traceback" not in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _tree_snapshot(lattice_dir) == before


def test_reconcile_real_run_recovers_before_loading_and_plans_recovered_bytes(
    lattice_dir: Path, monkeypatch
):
    destination = lattice_dir / "docs" / "pc-design.md"
    before_bytes = destination.read_bytes()
    after_bytes = b"not a valid lattice document\n"
    destination.write_bytes(after_bytes)
    journal, before, after = _write_cli_transaction(
        lattice_dir, destination, before_bytes, after_bytes
    )
    monkeypatch.chdir(lattice_dir)

    result = runner.invoke(app, ["reconcile", "pc-design"])

    assert result.exit_code == 0
    assert "reconciled pc-design.md" in result.stdout
    assert "recovered reconcile transaction: rolled_back" in result.stderr
    assert b"seen:" in destination.read_bytes()
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


@pytest.mark.parametrize("json_out", [False, True], ids=["human", "json"])
def test_reconcile_concurrent_edit_is_preserved_without_success_report(
    lattice_dir: Path, monkeypatch, json_out: bool
):
    monkeypatch.chdir(lattice_dir)
    real_commit = transaction.commit_rewrites
    editor_bytes = b"editor-owned concurrent bytes\n"
    edited_path: Path | None = None

    def edit_then_commit(project_root, rewrites, write_paths, *, lock):
        nonlocal edited_path
        edited_path = next(iter(write_paths.values()))
        edited_path.write_bytes(editor_bytes)
        return real_commit(project_root, rewrites, write_paths, lock=lock)

    monkeypatch.setattr(reconcile_command, "commit_rewrites", edit_then_commit)
    args = ["reconcile", "pc-design"]
    if json_out:
        args.append("--json")
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert edited_path is not None
    assert str(edited_path) in result.stderr
    assert "changed after validation" in result.stderr
    assert "RECONCILE_CONFLICT" in result.stderr
    assert edited_path.read_bytes() == editor_bytes


@pytest.mark.parametrize(
    ("failure", "message"),
    [("replace", "disk full"), ("fsync", "directory fsync failed")],
    ids=["replace-failure", "fsync-failure"],
)
def test_reconcile_midbatch_persistence_failure_rolls_back_without_success(
    tmp_path: Path, monkeypatch, failure: str, message: str
):
    project = _two_downstream_project(tmp_path)
    before = _tree_snapshot(project)
    monkeypatch.chdir(project)
    real_replace = transaction.replace_staged
    after_replaces = 0

    def fail_second_after(staged: Path, destination: Path) -> None:
        nonlocal after_replaces
        if "doc-lattice-after" in staged.name:
            after_replaces += 1
            if after_replaces == 2:
                if failure == "fsync":
                    staged.replace(destination)
                raise OSError(message)
        real_replace(staged, destination)

    monkeypatch.setattr(transaction, "replace_staged", fail_second_after)
    result = runner.invoke(app, ["reconcile", "--all"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert message in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr
    assert _tree_snapshot(project) == before


def test_reconcile_success_cleans_transaction_artifacts(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["reconciled"]
    assert not (lattice_dir / RECONCILE_JOURNAL_NAME).exists()
    assert not list(lattice_dir.rglob(".*.doc-lattice-before.*.tmp"))
    assert not list(lattice_dir.rglob(".*.doc-lattice-after.*.tmp"))
    assert not list(lattice_dir.glob(f"{RECONCILE_JOURNAL_NAME}.*.tmp"))


@pytest.mark.parametrize("mode", ["recover", "reconcile"])
def test_reconcile_lock_exit_failure_publishes_no_success(
    lattice_dir: Path, monkeypatch, mode: str
):
    real_lock = reconcile_command.reconcile_lock

    @contextmanager
    def fail_after_lock_body(project_root: Path):
        with real_lock(project_root) as lock:
            yield lock
        raise ReconcilePersistenceError("injected reconcile lock release failure")

    monkeypatch.setattr(reconcile_command, "reconcile_lock", fail_after_lock_body)
    monkeypatch.chdir(lattice_dir)
    args = ["reconcile", "--recover", "--json"]
    if mode == "reconcile":
        args = ["reconcile", "--all", "--json"]

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "injected reconcile lock release failure" in result.stderr
    assert "RECONCILE_PERSISTENCE" in result.stderr


def test_reconcile_write_error_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(transaction, "stage_bytes", boom)
    result = runner.invoke(app, ["reconcile", "pc-design"])
    assert result.exit_code == 2


def test_reconcile_real_run_reports_reconciled_lines(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all"])
    assert result.exit_code == 0
    assert "reconciled pc-design.md: art-direction#accent" in result.stdout
    assert "reconciled pc-design.md: art-direction#motion" in result.stdout


def test_reconcile_dry_run_leaves_files_unchanged(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    docs = lattice_dir / "docs"
    before = {p: p.read_text(encoding="utf-8") for p in docs.glob("*.md")}
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])
    assert result.exit_code == 0
    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text


def test_reconcile_dry_run_lists_stale_and_unreconciled_edges(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])
    assert result.exit_code == 0
    assert "would reconcile pc-design.md: art-direction#accent" in result.stdout
    assert "would reconcile pc-design.md: art-direction#motion" in result.stdout
    # gdd's ghost ref is BROKEN, which --all skips, so gdd never appears.
    assert "gdd" not in result.stdout
    assert "reconciled pc-design" not in result.stdout


def test_reconcile_dry_run_single_node_selection(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(app, ["reconcile", "pc-design", "--dry-run"])
    assert result.exit_code == 0
    assert "would reconcile pc-design.md: art-direction#accent" in result.stdout
    assert "would reconcile pc-design.md: art-direction#motion" in result.stdout
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_dry_run_composes_with_ref(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(
        app, ["reconcile", "pc-design", "--ref", "art-direction#accent", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "would reconcile pc-design.md: art-direction#accent" in result.stdout
    assert "art-direction#motion" not in result.stdout
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_dry_run_json_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1  # single-line JSON
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    entries = payload["reconciled"]
    assert entries == sorted(entries, key=lambda e: (e["path"], e["ref"]))
    stripped = {(Path(e["path"]).name, e["ref"]) for e in entries}
    assert stripped == {
        ("pc-design.md", "art-direction#accent"),
        ("pc-design.md", "art-direction#motion"),
    }
    for entry in entries:
        assert len(entry["new_seen"]) == 32
        int(entry["new_seen"], 16)  # must be hex


def test_reconcile_dry_run_json_leaves_files_unchanged(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_real_run_json_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    stripped = {(Path(e["path"]).name, e["ref"]) for e in payload["reconciled"]}
    assert stripped == {
        ("pc-design.md", "art-direction#accent"),
        ("pc-design.md", "art-direction#motion"),
    }
    # the real run actually wrote: check now reports both edges OK.
    check_payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    pc_states = [e["state"] for e in check_payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_dry_run_after_clean_reports_nothing_to_reconcile(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "--all"]).exit_code == 0  # real run clears drift
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run"])
    assert result.exit_code == 0
    assert "nothing to reconcile" in result.stdout


def test_reconcile_json_after_clean_reports_empty_list(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "--all"]).exit_code == 0  # real run clears drift
    result = runner.invoke(app, ["reconcile", "--all", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"dry_run": False, "reconciled": []}


def test_reconcile_ref_typo_exits_2(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "pc-design", "--ref", "accnt"])
    assert result.exit_code == 2


def test_reconcile_ref_selects_single_edge(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "pc-design", "--ref", "art-direction#accent"])
    assert result.exit_code == 0
    payload = json.loads(runner.invoke(app, ["check", "--json"]).stdout)
    edges = [e for e in payload["edges"] if e["source_id"] == "pc-design"]
    states = {e["target_ref"]: e["state"] for e in edges}
    assert states["art-direction#accent"] == "OK"
    assert states["art-direction#motion"] == "UNRECONCILED"


def test_reconcile_noop_reports_nothing_to_reconcile(tmp_path: Path, monkeypatch):
    _clean_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["reconcile", "down"])  # first run clears the UNRECONCILED edge
    result = runner.invoke(app, ["reconcile", "down"])  # nothing left to do
    assert result.exit_code == 0
    assert "nothing to reconcile" in result.stdout


def test_reconcile_all_cached_matches_uncached_bytes(lattice_dir: Path, tmp_path: Path):
    # Twin copies of the fixture tree: one uncached, one cached under cache_trust_stat.
    # The resulting file bytes and exit code must match.
    twin = tmp_path / "twin"
    shutil.copytree(lattice_dir, twin)
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1"}
    uncached = _run(["reconcile", "--all"], lattice_dir, env)
    (twin / ".doc-lattice.yml").write_text(
        "cache_key: recon\ncache_trust_stat: true\n", encoding="utf-8"
    )
    cached = _run(["reconcile", "--all"], twin, env)
    assert cached.exit_code == uncached.exit_code
    for name in ["pc-design.md", "art-direction.md", "gdd.md"]:
        assert (twin / "docs" / name).read_bytes() == (lattice_dir / "docs" / name).read_bytes()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["reconcile", "--all"], (True, True)),
        (["reconcile", "--all", "--dry-run"], (True, False)),
        (["check"], (False, True)),
    ],
    ids=["reconcile-real", "reconcile-dry-run", "check-default"],
)
def test_cli_forces_require_verified_only_for_reconcile(
    lattice_dir: Path,
    tmp_path: Path,
    monkeypatch,
    args,
    expected,
):
    # Mutant-killer: spy on cli.runtime.load_lattice, which default_runtime captures for
    # each invocation. Wrap the real function so the command still runs and record the
    # loader policy: reconcile must force the verify tier; check must not.
    seen: dict[str, bool] = {}
    real = runtime_module.load_lattice

    def spy(project, *, require_verified=False, persist_cache=True):
        seen["require_verified"] = require_verified
        seen["persist_cache"] = persist_cache
        return real(
            project,
            require_verified=require_verified,
            persist_cache=persist_cache,
        )

    monkeypatch.setattr(runtime_module, "load_lattice", spy)
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1"}
    _run(args, lattice_dir, env)
    assert (seen["require_verified"], seen["persist_cache"]) == expected
