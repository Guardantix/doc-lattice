"""CLI integration tests for the reconcile command."""

import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

import doc_lattice.cli.commands.reconcile as reconcile_command
import doc_lattice.cli.runtime as runtime_module
import doc_lattice.reconcile_transaction as transaction
from doc_lattice.cli import app
from doc_lattice.cli.commands.reconcile import _recovery_json_payload
from doc_lattice.constants import RECONCILE_JOURNAL_NAME, RECONCILE_JOURNAL_VERSION
from doc_lattice.error_types import ReconcilePersistenceError
from doc_lattice.path_utils import format_path_for_display
from doc_lattice.reconcile_transaction import (
    JournalEntry,
    JournalProvenance,
    JournalSelector,
    JournalState,
    JournalV2,
    RecoveryResult,
    ScanFailure,
    _serialize_journal,
    reconcile_lock,
)
from doc_lattice.text_utils import is_control_char

from .helpers import _clean_docs, _run, runner

_SRC = Path(__file__).resolve().parents[2] / "src"
# GTX-209: the same vector GTX-125 used, reused here because a stage inherits its
# destination's name, so a hostile document filename propagates into the transaction's own
# artifact paths and back out through recovery reporting.
_HOSTILE_DOC_NAME = "pwn\x1b[31m\x1b[Aevil.md"


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


_DEFAULT_CLI_PROVENANCE = JournalProvenance(
    created_at=datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
    tool_version="9.9.9",
    selector=JournalSelector(mode="all", downstream_id=None, ref=None),
)


def _cli_transaction_entry(
    root: Path, destination: Path, before_bytes: bytes, after_bytes: bytes
) -> JournalEntry:
    """Stage one entry's before and after images and describe them for a journal."""
    before = destination.with_name(f".{destination.name}.doc-lattice-before.test.tmp")
    after = destination.with_name(f".{destination.name}.doc-lattice-after.test.tmp")
    before.write_bytes(before_bytes)
    after.write_bytes(after_bytes)
    return JournalEntry(
        destination=destination.relative_to(root).as_posix(),
        before_path=before.relative_to(root).as_posix(),
        before_sha256=sha256(before_bytes).hexdigest(),
        after_path=after.relative_to(root).as_posix(),
        after_sha256=sha256(after_bytes).hexdigest(),
    )


def _write_cli_transaction(  # noqa: PLR0913
    root: Path,
    destination: Path,
    before_bytes: bytes,
    after_bytes: bytes,
    *,
    state: JournalState = "prepared",
    provenance: JournalProvenance = _DEFAULT_CLI_PROVENANCE,
) -> tuple[Path, Path, Path]:
    """Write a valid single-entry recovery transaction for CLI integration tests."""
    entry = _cli_transaction_entry(root, destination, before_bytes, after_bytes)
    journal = root / RECONCILE_JOURNAL_NAME
    journal.write_text(
        _serialize_journal(
            JournalV2(
                version=RECONCILE_JOURNAL_VERSION,
                state=state,
                provenance=provenance,
                entries=(entry,),
            )
        ).decode("utf-8"),
        encoding="utf-8",
    )
    return journal, root / entry.before_path, root / entry.after_path


def _write_legacy_cli_transaction(
    root: Path,
    destination: Path,
    before_bytes: bytes,
    after_bytes: bytes,
    *,
    state: JournalState = "prepared",
) -> Path:
    """Write the version 1 journal a pre-provenance release would have left behind.

    Built as literal JSON rather than through a model, because no model this release ships
    writes version 1 any more and the point of the fixture is the bytes an older one wrote.
    """
    entry = _cli_transaction_entry(root, destination, before_bytes, after_bytes)
    journal = root / RECONCILE_JOURNAL_NAME
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "state": state,
                "entries": [entry.model_dump(mode="json")],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return journal


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
    pc_check = runner.invoke(app, ["check", "--format", "json"])
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
    payload = json.loads(runner.invoke(app, ["check", "--format", "json"]).stdout)
    pc_states = [e["state"] for e in payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_all_skips_broken_edge(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "--all"]).exit_code == 0
    payload = json.loads(runner.invoke(app, ["check", "--format", "json"]).stdout)
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
    # No journal means no provenance to be absent, so neither the fields nor the sentence
    # naming their absence is printed. Asserted rather than left implied, because the guard
    # that produces it would otherwise regress into `not recorded by journal version None`.
    assert "provenance" not in result.stdout
    assert "created_at" not in result.stdout


def test_reconcile_recover_without_journal_reports_exact_json(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "action": "none",
        "journal": str(lattice_dir / RECONCILE_JOURNAL_NAME),
        "restored": 0,
        "already_before": 0,
        "unresolved": [],
        "orphans": [],
        "scan_errors": [],
        # Null because there was no journal at all, which `action` is what distinguishes from
        # a recovered version 1 journal that carried no provenance.
        "provenance": None,
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
    result = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "action": "cleaned_committed",
        "journal": str(journal),
        "restored": 0,
        "already_before": 0,
        "unresolved": [],
        "orphans": [],
        "scan_errors": [],
        # Captured before cleanup deleted the journal this describes.
        "provenance": {
            "created_at": "2026-08-17T12:00:00Z",
            "tool_version": "9.9.9",
            "selector": {"mode": "all", "downstream_id": None, "ref": None},
        },
    }
    assert destination.read_bytes() == after_bytes
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


def _transaction_destination(tmp_path: Path) -> Path:
    """Create the one document a prepared transaction is staged over."""
    docs = tmp_path / "docs"
    docs.mkdir()
    destination = docs / "down.md"
    destination.write_bytes(b"transaction document\n")
    return destination


def _prepared_project(
    tmp_path: Path, *, provenance: JournalProvenance = _DEFAULT_CLI_PROVENANCE
) -> tuple[Path, Path]:
    """Build a project holding one prepared transaction ready to roll back."""
    destination = _transaction_destination(tmp_path)
    journal, _before, _after = _write_cli_transaction(
        tmp_path,
        destination,
        b"original document\n",
        b"transaction document\n",
        provenance=provenance,
    )
    return journal, destination


def _legacy_prepared_project(tmp_path: Path) -> tuple[Path, Path]:
    """Build the same prepared transaction under the version 1 journal format."""
    destination = _transaction_destination(tmp_path)
    journal = _write_legacy_cli_transaction(
        tmp_path, destination, b"original document\n", b"transaction document\n"
    )
    return journal, destination


def test_reconcile_recover_reports_v2_provenance_in_human_output(tmp_path: Path, monkeypatch):
    """A recovered version 2 journal says when it was written, by what, and from which run."""
    _prepared_project(
        tmp_path,
        provenance=JournalProvenance(
            created_at=datetime(2026, 8, 17, 12, 0, 0, 123456, tzinfo=UTC),
            tool_version="5.0.0",
            selector=JournalSelector(mode="downstream", downstream_id="pc-design", ref="up#x"),
        ),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 0
    assert "rolled back reconcile transaction" in result.stdout
    assert "  created_at: 2026-08-17T12:00:00.123456Z" in result.stdout
    assert "  tool_version: '5.0.0'" in result.stdout
    assert "  selector: mode 'downstream', downstream_id 'pc-design', ref 'up#x'" in result.stdout


def test_reconcile_recover_prints_an_unset_selector_field_as_a_bare_null(
    tmp_path: Path, monkeypatch
):
    """An unset selector field reads as `null`, and a recorded string spelling it as `'null'`.

    AD-36 rests the distinction between the two entirely on the quoting, so both halves are
    pinned here in one run rather than left to the display helper's docstring.
    """
    _prepared_project(
        tmp_path,
        provenance=JournalProvenance(
            created_at=datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
            tool_version="null",
            selector=JournalSelector(mode="all", downstream_id=None, ref=None),
        ),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 0
    assert "  tool_version: 'null'" in result.stdout
    assert "  selector: mode 'all', downstream_id null, ref null" in result.stdout


def test_reconcile_recover_json_carries_a_downstream_selector(tmp_path: Path, monkeypatch):
    """The machine payload spells a narrowed downstream selection, ref included."""
    journal, _destination = _prepared_project(
        tmp_path,
        provenance=JournalProvenance(
            created_at=datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
            tool_version="5.0.0",
            selector=JournalSelector(mode="downstream", downstream_id="pc-design", ref="up#x"),
        ),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "action": "rolled_back",
        "journal": str(journal),
        "restored": 1,
        "already_before": 0,
        "unresolved": [],
        "orphans": [],
        "scan_errors": [],
        "provenance": {
            "created_at": "2026-08-17T12:00:00Z",
            "tool_version": "5.0.0",
            "selector": {"mode": "downstream", "downstream_id": "pc-design", "ref": "up#x"},
        },
    }


def test_reconcile_recover_normalizes_a_plus_offset_timestamp(tmp_path: Path, monkeypatch):
    """A journal spelled with +00:00 reports in the single spelling this project emits.

    The wire format accepts both spellings and parsing keeps neither token, so the report
    has to pick one; this pins which, in both channels at once.
    """
    journal, _destination = _prepared_project(tmp_path)
    wire = journal.read_text(encoding="utf-8")
    assert '"2026-08-17T12:00:00Z"' in wire
    journal.write_text(
        wire.replace('"2026-08-17T12:00:00Z"', '"2026-08-17T12:00:00+00:00"'), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    human = runner.invoke(app, ["reconcile", "--recover"])

    assert human.exit_code == 0
    assert "  created_at: 2026-08-17T12:00:00Z" in human.stdout
    assert "+00:00" not in human.stdout


def test_reconcile_recover_reports_v1_provenance_as_absent(tmp_path: Path, monkeypatch):
    """A version 1 journal says its format recorded no provenance, rather than showing blanks."""
    journal, destination = _legacy_prepared_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 0
    assert "rolled back reconcile transaction" in result.stdout
    assert "  provenance: not recorded by journal version 1" in result.stdout
    assert destination.read_bytes() == b"original document\n"
    assert not journal.exists()


def test_reconcile_recover_v1_json_provenance_is_null(tmp_path: Path, monkeypatch):
    """Version 1 recovery reports null provenance; `action` is what separates it from no journal."""
    journal, _destination = _legacy_prepared_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"] is None
    assert payload["action"] == "rolled_back"
    assert payload["journal"] == str(journal)


def test_reconcile_recover_displays_control_bearing_provenance_without_refusing(
    tmp_path: Path, monkeypatch
):
    """A hand-edited journal recovers, and its control bytes reach neither channel raw.

    AD-36: provenance is informational, so a control character in it is spelled for display
    rather than made a reason to strand an otherwise valid rollback. The values survive
    exactly in JSON, where the encoder escapes them instead of emitting them raw.
    """
    hostile_version = "5.0.0\x1b[31m"
    hostile_id = "pc\x1b[Adesign"
    hostile_ref = "up#\x7fx"
    hostile_provenance = JournalProvenance(
        created_at=datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
        tool_version=hostile_version,
        selector=JournalSelector(mode="downstream", downstream_id=hostile_id, ref=hostile_ref),
    )
    journal, destination = _prepared_project(tmp_path, provenance=hostile_provenance)
    monkeypatch.chdir(tmp_path)

    human = runner.invoke(app, ["reconcile", "--recover"])

    assert human.exit_code == 0
    assert "rolled back reconcile transaction" in human.stdout
    assert destination.read_bytes() == b"original document\n"
    assert not journal.exists()
    for text in (human.stdout, human.stderr):
        assert not any(is_control_char(char) for char in text.replace("\n", ""))
    assert "  tool_version: '5.0.0\\x1b[31m'" in human.stdout

    _write_cli_transaction(
        tmp_path,
        destination,
        b"original document\n",
        b"transaction document\n",
        provenance=hostile_provenance,
    )
    destination.write_bytes(b"transaction document\n")
    machine = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])

    assert machine.exit_code == 0
    assert not any(is_control_char(char) for char in machine.stdout.replace("\n", ""))
    provenance = json.loads(machine.stdout)["provenance"]
    assert provenance["tool_version"] == hostile_version
    assert provenance["selector"] == {
        "mode": "downstream",
        "downstream_id": hostile_id,
        "ref": hostile_ref,
    }


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
        ["reconcile", "--recover", "--format", "json"],
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

    result = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])

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
    assert "reconciled 'pc-design.md'" in result.stdout
    assert "recovered reconcile transaction: rolled_back" in result.stderr
    assert b"seen:" in destination.read_bytes()
    assert not journal.exists()
    assert not before.exists()
    assert not after.exists()


def test_reconcile_recover_reports_partial_rollback_and_exits_nonzero(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    destination = docs / "down.md"
    destination.write_bytes(b"unrelated editor bytes\n")
    journal, before, after = _write_cli_transaction(
        tmp_path, destination, b"original document\n", b"transaction document\n"
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 2
    assert "partially rolled back reconcile transaction" in result.stdout
    assert "could not restore 1 destination" in result.stderr
    assert "unresolved destination: 'docs/down.md'" in result.stderr
    assert "every remaining staged image were retained" in result.stderr
    assert "rerun 'doc-lattice reconcile --recover'" in result.stderr
    # Rerunning cannot resolve bytes the journal has no record of, so the guidance has to
    # name the other way out rather than leaving the operator in a loop.
    assert "to keep the current bytes instead" in result.stderr
    assert destination.read_bytes() == b"unrelated editor bytes\n"
    assert journal.exists()
    assert before.read_bytes() == b"original document\n"
    assert after.read_bytes() == b"transaction document\n"


def test_reconcile_recover_partial_json_names_the_unresolved_destination(
    tmp_path: Path, monkeypatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    destination = docs / "down.md"
    destination.write_bytes(b"unrelated editor bytes\n")
    journal, _before, _after = _write_cli_transaction(
        tmp_path, destination, b"original document\n", b"transaction document\n"
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "action": "partially_rolled_back",
        "journal": str(journal),
        "restored": 0,
        "already_before": 0,
        "unresolved": ["docs/down.md"],
        "orphans": [],
        "scan_errors": [],
        "provenance": {
            "created_at": "2026-08-17T12:00:00Z",
            "tool_version": "9.9.9",
            "selector": {"mode": "all", "downstream_id": None, "ref": None},
        },
    }


def test_reconcile_lost_journal_double_failure_maps_orphans_and_recover_cannot_act(
    tmp_path: Path, monkeypatch
):
    """Drive both failures through the CLI and read what an operator is actually told.

    GTX-146: the committed-marker replace loses the journal and the restoring create fails
    too, so nothing on disk describes the transaction while both destinations hold their
    after images. The unit suite pins the internal ``RecoveryResult``; this asserts the
    operator-visible outcome end to end, including that the follow-on ``--recover`` really
    is as useless as the diagnostic now says it is.
    """
    project = _two_downstream_project(tmp_path)
    monkeypatch.chdir(project)
    journal = project / RECONCILE_JOURNAL_NAME
    real_create = transaction.atomic_create_bytes
    marker_failed = False

    def lose_journal_then_fail(path: Path, data: bytes, *, prefix: str) -> None:  # noqa: ARG001
        nonlocal marker_failed
        marker_failed = True
        path.unlink()
        raise OSError("committed marker replace failed")

    def fail_journal_restore(path: Path, data: bytes, *, prefix: str) -> None:
        if marker_failed:
            raise OSError("prepared journal restore failed")
        real_create(path, data, prefix=prefix)

    monkeypatch.setattr(transaction, "atomic_replace_bytes", lose_journal_then_fail)
    monkeypatch.setattr(transaction, "atomic_create_bytes", fail_journal_restore)

    failed = runner.invoke(app, ["reconcile", "--all"])

    assert failed.exit_code == 2
    assert failed.stdout == ""
    assert "rollback was not attempted" in failed.stderr
    # The prescription an operator cannot act on is gone from the operator-visible text.
    assert "run 'doc-lattice reconcile --recover'" not in failed.stderr
    assert "has no journal to read" in failed.stderr
    assert "the retained stage against its before image" in failed.stderr
    assert "preserve any destination or stage that does not match" in failed.stderr
    assert not journal.exists()
    stages = sorted(project.rglob("*.doc-lattice-before.*.tmp"))
    assert len(stages) == 2
    for stage in stages:
        destination = stage.parent / stage.name.split(".doc-lattice-before.")[0].lstrip(".")
        relative = destination.relative_to(project).as_posix()
        assert f"destination {format_path_for_display(relative)} holds after image" in (
            failed.stderr
        )
        assert format_path_for_display(stage.relative_to(project).as_posix()) in failed.stderr
        assert sha256(destination.read_bytes()).hexdigest() in failed.stderr
        assert sha256(stage.read_bytes()).hexdigest() in failed.stderr

    monkeypatch.undo()
    monkeypatch.chdir(project)
    crashed = _tree_snapshot(project)

    recovered = runner.invoke(app, ["reconcile", "--recover"])

    # Exactly the uselessness the diagnostic now names: no journal to read, both stages
    # reported as opaque orphans, nothing tying either one back to its destination.
    assert recovered.exit_code == 2
    assert "no reconcile journal to recover" in recovered.stdout
    assert "orphaned reconcile artifacts remain; nothing was deleted" in recovered.stderr
    for stage in stages:
        relative = stage.relative_to(project).as_posix()
        assert f"orphaned artifact: {format_path_for_display(relative)}" in recovered.stderr
    assert _tree_snapshot(project) == crashed


def test_reconcile_recover_reports_orphans_without_deleting_them(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    stage = docs / ".down.md.doc-lattice-after.leaked.tmp"
    stage.write_bytes(b"leaked stage\n")
    journal_stage = tmp_path / f"{RECONCILE_JOURNAL_NAME}.leaked.tmp"
    journal_stage.write_bytes(b"leaked journal stage\n")
    monkeypatch.chdir(tmp_path)
    before = _tree_snapshot(tmp_path)

    result = runner.invoke(app, ["reconcile", "--recover"])

    assert result.exit_code == 2
    assert "nothing to recover" not in result.stdout
    assert "no reconcile journal to recover" in result.stdout
    assert "orphaned reconcile artifacts remain; nothing was deleted" in result.stderr
    assert "orphaned artifact: '.doc-lattice-reconcile.json.leaked.tmp'" in result.stderr
    assert "orphaned artifact: 'docs/.down.md.doc-lattice-after.leaked.tmp'" in result.stderr
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory read permissions")
def test_reconcile_recover_reports_an_unscannable_directory(tmp_path: Path, monkeypatch):
    unreadable = tmp_path / "locked"
    unreadable.mkdir()
    unreadable.chmod(0o000)
    monkeypatch.chdir(tmp_path)

    try:
        result = runner.invoke(app, ["reconcile", "--recover"])
    finally:
        unreadable.chmod(0o755)

    assert result.exit_code == 2
    assert "no reconcile journal to recover" in result.stdout
    assert "for orphaned artifacts" in result.stderr
    # GTX-209: the human encoder applies the display spelling to the path component alone,
    # leaving the operating system's own sentence as the prose it is.
    assert format_path_for_display(unreadable) in result.stderr


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory read permissions")
@pytest.mark.skipif(os.name != "posix", reason="a directory name holding ESC is POSIX-only")
def test_reconcile_recover_splits_a_scan_failure_between_its_two_encoders(
    tmp_path: Path, monkeypatch
):
    """The human line displays the path; the JSON array keeps the pre-GTX-209 spelling.

    A scan failure is the one recovery detail whose path was already fused into prose at the
    producer, so it is the case that proves the structured record reaches both encoders rather
    than one spelling being applied to the whole sentence.
    """
    unreadable = tmp_path / _HOSTILE_DOC_NAME
    unreadable.mkdir()
    unreadable.chmod(0o000)
    monkeypatch.chdir(tmp_path)

    try:
        human = runner.invoke(app, ["reconcile", "--recover"])
        machine = runner.invoke(app, ["reconcile", "--recover", "--format", "json"])
    finally:
        unreadable.chmod(0o755)

    assert human.exit_code == 2
    assert format_path_for_display(unreadable) in human.stderr
    assert "\x1b" not in human.stderr
    assert "for orphaned artifacts" in human.stderr

    assert machine.exit_code == 2
    scan_errors = json.loads(machine.stdout)["scan_errors"]
    assert len(scan_errors) == 1
    # The machine channel is untouched: the raw path, unquoted, exactly as before.
    assert scan_errors[0].startswith(f"cannot scan {unreadable} for orphaned artifacts: ")
    assert format_path_for_display(unreadable) not in scan_errors[0].split(": ", 1)[0]


def test_recovery_json_payload_keeps_the_raw_path_encoding_and_order():
    """The machine channel spells a hostile path raw and orders its arrays as the producer did.

    GTX-196 added the eighth key, so this no longer claims the whole payload is byte-identical
    to the pre-GTX-209 one; what it still pins is the part that criterion was about. Compared
    as literal payload bytes rather than as parsed JSON, because a parsed comparison would
    accept a reordered array or a different escape for the same code point.
    """
    hostile = f"docs/{_HOSTILE_DOC_NAME}"
    journal = Path("/project") / _HOSTILE_DOC_NAME
    # Two failures, so the array's order is asserted as well as its wording. That the producer
    # orders them by the legacy rendering rather than by field is pinned separately, in
    # tests/test_reconcile_transaction.py, against a pair where the two orders differ.
    first = ScanFailure(filename="/b", detail="aaa")
    second = ScanFailure(filename="/a", detail="zzz")
    recovery = RecoveryResult(
        action="partially_rolled_back",
        journal=journal,
        restored=1,
        already_before=2,
        unresolved=(hostile,),
        orphans=(f"{hostile}.doc-lattice-after.x.tmp",),
        scan_errors=tuple(sorted((first, second), key=lambda f: f.legacy_text)),
    )

    payload = _recovery_json_payload(recovery)

    expected = json.dumps(
        {
            "action": "partially_rolled_back",
            "journal": str(journal),
            "restored": 1,
            "already_before": 2,
            "unresolved": [hostile],
            "orphans": [f"{hostile}.doc-lattice-after.x.tmp"],
            "scan_errors": [
                "cannot scan /a for orphaned artifacts: zzz",
                "cannot scan /b for orphaned artifacts: aaa",
            ],
            "provenance": None,
        }
    )
    assert payload.encode("utf-8") == expected.encode("utf-8")
    # The spelling really is the raw one: no display quoting reached the machine channel.
    assert format_path_for_display(hostile) not in payload


def test_reconcile_partial_automatic_recovery_halts_before_loading_the_lattice(
    lattice_dir: Path, monkeypatch
):
    destination = lattice_dir / "docs" / "pc-design.md"
    destination.write_bytes(b"unrelated editor bytes\n")
    journal, before, after = _write_cli_transaction(
        lattice_dir, destination, b"original document\n", b"transaction document\n"
    )
    monkeypatch.chdir(lattice_dir)

    def fail_if_loaded(*_args, **_kwargs):
        pytest.fail("a partial automatic recovery planned against an unrestored tree")

    monkeypatch.setattr(runtime_module, "load_lattice", fail_if_loaded)
    result = runner.invoke(app, ["reconcile", "pc-design"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "recovered reconcile transaction: partially_rolled_back" in result.stderr
    assert "unresolved destination: 'docs/pc-design.md'" in result.stderr
    assert destination.read_bytes() == b"unrelated editor bytes\n"
    assert journal.exists()
    assert before.exists()
    assert after.exists()


@pytest.mark.parametrize("json_out", [False, True], ids=["human", "json"])
def test_reconcile_concurrent_edit_is_preserved_without_success_report(
    lattice_dir: Path, monkeypatch, json_out: bool
):
    monkeypatch.chdir(lattice_dir)
    real_commit = transaction.commit_rewrites
    editor_bytes = b"editor-owned concurrent bytes\n"
    edited_path: Path | None = None

    def edit_then_commit(project_root, rewrites, write_paths, *, selector, lock):
        nonlocal edited_path
        edited_path = next(iter(write_paths.values()))
        edited_path.write_bytes(editor_bytes)
        return real_commit(project_root, rewrites, write_paths, selector=selector, lock=lock)

    monkeypatch.setattr(reconcile_command, "commit_rewrites", edit_then_commit)
    args = ["reconcile", "pc-design"]
    if json_out:
        args.extend(["--format", "json"])
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
    result = runner.invoke(app, ["reconcile", "--all", "--format", "json"])

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
    args = ["reconcile", "--recover", "--format", "json"]
    if mode == "reconcile":
        args = ["reconcile", "--all", "--format", "json"]

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
    assert "reconciled 'pc-design.md': art-direction#accent" in result.stdout
    assert "reconciled 'pc-design.md': art-direction#motion" in result.stdout


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
    assert "would reconcile 'pc-design.md': art-direction#accent" in result.stdout
    assert "would reconcile 'pc-design.md': art-direction#motion" in result.stdout
    # gdd's ghost ref is BROKEN, which --all skips, so gdd never appears.
    assert "gdd" not in result.stdout
    assert "reconciled pc-design" not in result.stdout


def test_reconcile_dry_run_single_node_selection(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(app, ["reconcile", "pc-design", "--dry-run"])
    assert result.exit_code == 0
    assert "would reconcile 'pc-design.md': art-direction#accent" in result.stdout
    assert "would reconcile 'pc-design.md': art-direction#motion" in result.stdout
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_dry_run_composes_with_ref(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    pc_path = lattice_dir / "docs" / "pc-design.md"
    before = pc_path.read_text(encoding="utf-8")
    result = runner.invoke(
        app, ["reconcile", "pc-design", "--ref", "art-direction#accent", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "would reconcile 'pc-design.md': art-direction#accent" in result.stdout
    assert "art-direction#motion" not in result.stdout
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_dry_run_json_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run", "--format", "json"])
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
    result = runner.invoke(app, ["reconcile", "--all", "--dry-run", "--format", "json"])
    assert result.exit_code == 0
    assert pc_path.read_text(encoding="utf-8") == before


def test_reconcile_real_run_json_payload(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["reconcile", "--all", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    stripped = {(Path(e["path"]).name, e["ref"]) for e in payload["reconciled"]}
    assert stripped == {
        ("pc-design.md", "art-direction#accent"),
        ("pc-design.md", "art-direction#motion"),
    }
    # the real run actually wrote: check now reports both edges OK.
    check_payload = json.loads(runner.invoke(app, ["check", "--format", "json"]).stdout)
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
    result = runner.invoke(app, ["reconcile", "--all", "--format", "json"])
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
    payload = json.loads(runner.invoke(app, ["check", "--format", "json"]).stdout)
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


# --- Journal selector construction (GTX-126) --------------------------------------------------


@pytest.mark.parametrize(
    ("downstream_id", "reconcile_all", "ref", "expected"),
    [
        ("", True, None, {"mode": "all", "downstream_id": None, "ref": None}),
        ("", True, "up#x", {"mode": "all", "downstream_id": None, "ref": "up#x"}),
        (
            "pc-design",
            False,
            None,
            {"mode": "downstream", "downstream_id": "pc-design", "ref": None},
        ),
        (
            "pc-design",
            False,
            "up#x",
            {"mode": "downstream", "downstream_id": "pc-design", "ref": "up#x"},
        ),
        # --all wins over a downstream id, exactly as the planner resolves the same pair.
        ("pc-design", True, None, {"mode": "all", "downstream_id": None, "ref": None}),
    ],
    ids=["all", "all-ref", "downstream", "downstream-ref", "all-beats-downstream-id"],
)
def test_journal_selector_mirrors_the_planner_precedence(
    downstream_id: str, reconcile_all: bool, ref: str | None, expected: dict
):
    selector = reconcile_command._journal_selector(
        downstream_id,
        reconcile_all=reconcile_all,
        ref=ref,
    )

    assert json.loads(selector.model_dump_json()) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["reconcile", "pc-design"],
            {"mode": "downstream", "downstream_id": "pc-design", "ref": None},
        ),
        (["reconcile", "--all"], {"mode": "all", "downstream_id": None, "ref": None}),
    ],
    ids=["downstream", "all"],
)
def test_reconcile_hands_the_selector_it_built_to_the_transaction(
    lattice_dir: Path, monkeypatch, args: list[str], expected: dict
):
    monkeypatch.chdir(lattice_dir)
    real_commit = transaction.commit_rewrites
    captured: list[JournalSelector] = []

    def _capture(project_root, rewrites, write_paths, *, selector, lock):
        captured.append(selector)
        return real_commit(project_root, rewrites, write_paths, selector=selector, lock=lock)

    monkeypatch.setattr(reconcile_command, "commit_rewrites", _capture)

    result = runner.invoke(app, args)

    assert result.exit_code == 0
    assert [json.loads(selector.model_dump_json()) for selector in captured] == [expected]


def _reused_anchor_project(tmp_path: Path) -> Path:
    """Write a project whose one downstream document reuses a YAML anchor in its frontmatter."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#s}\nupstream body\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\ntitle: &shared Down\nlayer: &shared design\n"
        "derives_from:\n  - ref: up#s\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    return tmp_path


def test_reconcile_rewrite_warnings_use_the_same_stderr_voice_as_the_load(tmp_path: Path):
    """The rewrite phase rereads frontmatter, so it is a second warning site in one command.

    A real interpreter is required twice over: the filter has to be set at startup, and the
    point of the assertion is the bytes a user's stderr receives. ``always`` is not decoration
    either. Both the load and the reread raise from the same ruamel composer line, so under
    the default once-per-location filter the second copy is suppressed and an unwrapped
    rewrite phase looks identical to a wrapped one.
    """
    project_root = _reused_anchor_project(tmp_path)

    completed = subprocess.run(
        [sys.executable, "-c", "from doc_lattice.cli import main; main()", "reconcile", "--all"],
        cwd=project_root,
        env={
            **os.environ,
            "NO_COLOR": "1",
            "PYTHONPATH": str(_SRC),
            "PYTHONWARNINGS": "always",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    # Never Python's default formatter, at either of the two warning sites.
    assert "ReusedAnchorWarning" not in completed.stderr
    assert "composer.py" not in completed.stderr
    assert "site-packages" not in completed.stderr
    # AD-33 pins the strict load to the pure parser, which warns and rebinds rather than
    # refusing this document, so the rewrite phase is reached on both cells of the
    # yaml-compatibility leg and both warnings arrive in this voice.
    assert completed.returncode == 0
    assert "warning: found duplicate anchor 'shared'" in completed.stderr


def test_reconcile_reread_warning_escalated_to_an_error_lands_on_the_coded_contract(
    tmp_path: Path,
):
    """The second of the two dependency-raised paths AD-29 names, reached end to end.

    A bare ``PYTHONWARNINGS=error`` cannot reach it. This document's frontmatter reuses an
    anchor, so the strict load raises this engine's own ``reused anchor in`` advisory first and
    the run now exits 2 there, before any rewrite is planned. Silencing that one warning is what
    leaves the reread as the only site left to raise: ``PYTHONWARNINGS`` entries are processed
    left to right and each is inserted at the front of the filter list, so the rightmost wins,
    and ``ignore:reused anchor`` matches this engine's message prefix while ruamel's own message
    (which opens with a newline and then ``found duplicate anchor``) is left to escalate.

    Both parsers involved are pinned pure -- ``frontmatter_parser`` by AD-33 and the reread's own
    loader by AD-26 -- so unlike the ``config.py`` path this behaves identically on both cells of
    the ``yaml-compatibility`` leg and is asserted unconditionally.
    """
    project_root = _reused_anchor_project(tmp_path)
    before = (project_root / "docs" / "down.md").read_bytes()

    completed = subprocess.run(
        [sys.executable, "-c", "from doc_lattice.cli import main; main()", "reconcile", "--all"],
        cwd=project_root,
        env={
            **os.environ,
            "NO_COLOR": "1",
            "PYTHONPATH": str(_SRC),
            "PYTHONWARNINGS": "error,ignore:reused anchor",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stderr.startswith(
        "error (WARNING_AS_ERROR): ReusedAnchorWarning: found duplicate anchor 'shared'"
    )
    assert "a warning filter escalated this advisory to an error" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert "composer.py" not in completed.stderr
    assert "site-packages" not in completed.stderr
    # The reread happens before any write, so the escalation stops a planned rewrite rather
    # than leaving a half-applied one behind.
    assert (project_root / "docs" / "down.md").read_bytes() == before
