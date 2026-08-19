"""Every GTX-125-owned human-facing path sink goes through the display spelling.

The bug class this file exists for is an omitted construction site, not a wrong renderer, so
the coverage is per-sink rather than per-shape: each entry drives one construction site with a
hostile filename and asserts the message it built carries the display spelling and no raw
control byte. ``tests/test_path_utils.py`` pins what that spelling is; this file only asserts
that each sink reaches it. The renderer-level and raw-byte behavior is asserted end to end in
``tests/cli/test_contract.py``.

The sinks GTX-125 owned are the load boundary, the human reports, and the ordinary reconcile
paths. GTX-209 added the reconcile transaction layer, the shared durable-write helper, and
reconcile's recovery reporting, which is why ``TestTransactionSinks``, ``TestPersistenceSinks``,
and ``TestRecoveryReportSinks`` sit alongside the original three classes. The machine channels
(JSON, the GitHub annotation ``file=`` value, the journal serializer, and every staged-artifact
filename) are deliberately excluded: they carry their own encoders, and substituting a display
spelling into them breaks attachment semantics, journal validation, or the names on disk.
"""

import json
import re
import warnings
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from doc_lattice import discovery, orchestrate, persistence, reconcile, reconcile_transaction
from doc_lattice.cli.commands.reconcile import (
    _print_reconcile_lines,
    _recovery_json_payload,
    _report_recovery,
    _report_recovery_problems,
)
from doc_lattice.cli.output import github_annotation, warn_unattachable_annotations
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.constants import (
    PERSISTENCE_TEMP_SUFFIX,
    RECONCILE_AFTER_IMAGE_INFIX,
    RECONCILE_BEFORE_IMAGE_INFIX,
    RECONCILE_JOURNAL_VERSION,
)
from doc_lattice.error_types import (
    ProjectError,
    ReconcilePersistenceError,
    exception_details,
)
from doc_lattice.frontmatter_parser import parse_meta, split_frontmatter_parts
from doc_lattice.loader import build_lattice
from doc_lattice.model import Node, NodeMeta, ParsedDoc, RawEdge
from doc_lattice.path_utils import format_path_for_display
from doc_lattice.reconcile_transaction import (
    JournalProvenance,
    JournalSelector,
    RecoveryResult,
    ScanFailure,
    _LoadedJournal,
)
from doc_lattice.report_render import render_impact
from doc_lattice.text_utils import strip_control_chars

# One filename carrying the vector from the issue: a colour SGR, then a cursor-up that would
# overwrite the diagnostic printed above it. Every sink below is driven with this same name so
# a failure names the sink, not the input.
HOSTILE = "pwn\x1b[31m\x1b[Aevil.md"

# The C0, DEL, and C1 code points a terminal acts on. No sink may pass one through.
CONTROLS = frozenset(chr(code) for code in [*range(0x20), 0x7F, *range(0x80, 0xA0)]) - {
    "\n",  # a message may legitimately span lines; the filename itself carries no newline
}


def _assert_displayed(text: str, path: str | Path) -> None:
    """Assert a built message names ``path`` in the display spelling and carries no control.

    ``path`` is a ``str`` for the sinks that hold one: a journal entry's recorded path, a
    project-relative recovery string, and the expected staged-artifact name pattern are text,
    and the display helper spells them the same way it spells a ``Path``.
    """
    assert format_path_for_display(path) in text, f"sink did not use the display spelling: {text!r}"
    leaked = sorted(ch for ch in CONTROLS if ch in text)
    assert not leaked, f"sink leaked raw control bytes {leaked!r}: {text!r}"


def _capture_warning(func) -> str:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        func()
    assert len(caught) == 1, f"expected exactly one warning, got {len(caught)}"
    return str(caught[0].message)


def _console_output(func) -> str:
    output = StringIO()
    console = Console(file=output, width=1000, color_system=None)
    func(console)
    return output.getvalue()


def _runtime(stdout: StringIO, stderr: StringIO, cwd: Path) -> CliRuntime:
    """A runtime bound to plain consoles; neither loader is reached by these sinks."""

    def unused_load_config(_config, seen_cwd):
        raise AssertionError(f"unexpected load from {seen_cwd}")

    def unused_load_lattice(project, *, require_verified=False, persist_cache=True):
        del project, require_verified, persist_cache
        raise AssertionError("unexpected lattice load")

    return CliRuntime(
        stdout=Console(file=stdout, width=1000, color_system=None),
        stderr=Console(file=stderr, width=1000, color_system=None),
        cwd=cwd,
        load_config=unused_load_config,
        load_lattice=unused_load_lattice,
    )


def _node(path: Path, node_id: str = "down") -> Node:
    return Node(
        id=node_id,
        title="t",
        layer=None,
        authority=None,
        path=path,
        body="body\n",
        derives_from=(),
        tickets=(),
    )


def _parsed(path: Path, node_id: str, refs: tuple[str, ...] = ()) -> ParsedDoc:
    meta = NodeMeta(id=node_id, derives_from=[RawEdge(ref=ref) for ref in refs])
    return ParsedDoc(path=path, meta=meta, body="# H\n", sections=None)


class TestTypedErrorSinks:
    """Every typed error that interpolates a path builds it through the display spelling."""

    def test_unreadable_doc_read_failure(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            discovery.read_doc(path)
        _assert_displayed(str(exc.value), path)

    def test_unreadable_doc_non_utf8(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(ProjectError) as exc:
            discovery.read_doc(path)
        _assert_displayed(str(exc.value), path)

    def test_unreadable_doc_unclosed_fence(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            split_frontmatter_parts("---\nid: a\n", path)
        _assert_displayed(str(exc.value), path)

    def test_unreadable_doc_unparseable_frontmatter(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            parse_meta("id: [unclosed\n", path)
        _assert_displayed(str(exc.value), path)

    def test_frontmatter_error_invalid_schema(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            parse_meta("id: a\nauthority: nonsense\n", path)
        _assert_displayed(str(exc.value), path)

    def test_frontmatter_error_id_less_lattice_intent(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            parse_meta("derives_from: []\n", path)
        _assert_displayed(str(exc.value), path)

    def test_duplicate_id_error_names_both_registration_sites(self, tmp_path: Path):
        first = tmp_path / HOSTILE
        second = tmp_path / f"other-{HOSTILE}"
        with pytest.raises(ProjectError) as exc:
            build_lattice([_parsed(first, "collide"), _parsed(second, "collide")])
        _assert_displayed(str(exc.value), first)

    def test_reconcile_reader_failure(self, tmp_path: Path):
        path = tmp_path / HOSTILE

        def raise_os_error(_path: Path) -> bytes:
            msg = "disk vanished"
            raise OSError(msg)

        with pytest.raises(ProjectError) as exc:
            reconcile.plan_rewrites({path: {"a#x": "newhash"}}, raise_os_error)
        _assert_displayed(str(exc.value), path)

    def test_reconcile_unparseable_frontmatter(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        source = "---\nid: a\nderives_from:\n  - ref: b\n    seen: old\n---\nbody\n"

        with pytest.raises(ProjectError) as exc:
            reconcile.plan_rewrites(
                {path: {"b": "newhash"}},
                lambda _path: source.replace("id: a", "id: [unclosed").encode("utf-8"),
            )
        _assert_displayed(str(exc.value), path)

    def test_reconcile_unclosed_fence(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            reconcile.plan_rewrites({path: {"b": "newhash"}}, lambda _path: b"---\nid: a\n")
        _assert_displayed(str(exc.value), path)


class TestWarningSinks:
    """Every ``warnings.warn`` site that names a path builds it through the display spelling."""

    def test_id_less_skip(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        message = _capture_warning(lambda: orchestrate._report_skip("id-less", path))
        _assert_displayed(message, path)
        # AD-29: the prefix is load-bearing for PYTHONWARNINGS targetability.
        assert message.startswith("skipping ")

    def test_reused_anchor(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        message = _capture_warning(lambda: orchestrate._report_reused_anchors(True, path))
        _assert_displayed(message, path)
        assert message.startswith("reused anchor in ")

    def test_symlink_escape(self, tmp_path: Path):
        project_root = tmp_path / "project"
        outside = tmp_path / "outside"
        project_root.mkdir()
        outside.mkdir()
        target = outside / "target.md"
        target.write_text("---\nid: out\n---\n# Out\n", encoding="utf-8")
        link = project_root / HOSTILE
        link.symlink_to(target)

        message = _capture_warning(
            lambda: discovery.discover_doc_paths([project_root], (), project_root)
        )
        _assert_displayed(message, link)
        assert message.startswith("skipping ")


class TestDirectConsoleWriteSinks:
    """The three success-path console writes that print a path, none of them a diagnostic."""

    def test_impact_human_report(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        text = _console_output(lambda console: render_impact(console, [(_node(path), 1)]))
        _assert_displayed(text, path)

    def test_reconcile_success_line(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        output = StringIO()
        runtime = _runtime(output, StringIO(), tmp_path)
        _print_reconcile_lines(runtime, path, frozenset({"a#x"}), dry_run=False)
        # The adapter prints the basename, so that is the path this sink is asserted against.
        _assert_displayed(output.getvalue(), Path(path.name))

    def test_unattachable_annotation_warning(self, tmp_path: Path):
        outside = tmp_path / "outside" / HOSTILE
        errors = StringIO()
        runtime = _runtime(StringIO(), errors, tmp_path / "inside")
        warn_unattachable_annotations(runtime, [outside])
        _assert_displayed(errors.getvalue(), outside)


class TestTransactionSinks:
    """Every ``reconcile_transaction.py`` site that interpolates a path into human text.

    The list is per construction site rather than per shape, for GTX-125's reason: the bug
    class is an omitted sink. A site added to the module tomorrow is not covered here, which is
    what the static guard in ``tests/test_conventions.py`` exists to catch instead.

    Several sinks build a ``ValueError`` that ``_load_journal`` re-wraps, so they are driven
    directly and asserted on their own message. Others attach notes rather than raising, so
    they are asserted through ``exception_details``, which is what the CLI renderer flattens.
    """

    def test_invalid_journal_error_names_the_journal(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        error = reconcile_transaction._invalid_journal_error(journal, "boom")
        _assert_displayed(str(error), journal)

    def test_journal_already_exists_message(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        _assert_displayed(reconcile_transaction._journal_already_exists_message(journal), journal)

    def test_resolve_journal_path_rejects_an_absolute_recorded_path(self, tmp_path: Path):
        raw = f"/{HOSTILE}"
        with pytest.raises(ValueError, match="must be relative") as exc:
            reconcile_transaction._resolve_journal_path(tmp_path, "destination", raw)
        _assert_displayed(str(exc.value), raw)

    def test_resolve_journal_path_rejects_an_escaping_recorded_path(self, tmp_path: Path):
        raw = f"../{HOSTILE}"
        with pytest.raises(ValueError, match="unsafe destination") as exc:
            reconcile_transaction._resolve_journal_path(tmp_path, "destination", raw)
        _assert_displayed(str(exc.value), raw)

    def test_resolve_journal_path_shows_the_recorded_spelling_not_a_normalized_one(
        self, tmp_path: Path
    ):
        # The diagnostic rejects a recorded string, so it reports that string. Routing it
        # through ``Path()`` first would collapse the doubled separator it is rejecting.
        raw = f"/docs//{HOSTILE}"
        with pytest.raises(ValueError, match="must be relative") as exc:
            reconcile_transaction._resolve_journal_path(tmp_path, "destination", raw)
        assert repr(raw) in str(exc.value)

    def _artifact_error(self, tmp_path: Path, raw: str) -> str:
        field = "before_path"
        destination = tmp_path / HOSTILE
        with pytest.raises(ValueError, match=re.escape(field)) as exc:
            reconcile_transaction._validate_artifact_path(
                tmp_path, destination, tmp_path / Path(raw).name, role="before", raw_path=raw
            )
        return str(exc.value)

    def test_artifact_path_outside_the_destination_directory(self, tmp_path: Path):
        nested = tmp_path / "nested"
        nested.mkdir()
        destination = nested / HOSTILE
        raw = f".{HOSTILE}{RECONCILE_BEFORE_IMAGE_INFIX}x{PERSISTENCE_TEMP_SUFFIX}"
        with pytest.raises(ValueError, match="must be in destination directory") as exc:
            reconcile_transaction._validate_artifact_path(
                tmp_path, destination, tmp_path / raw, role="before", raw_path=raw
            )
        _assert_displayed(str(exc.value), raw)
        _assert_displayed(str(exc.value), tmp_path / raw)
        _assert_displayed(str(exc.value), destination.parent)

    def test_artifact_name_pattern_mismatch_displays_the_expected_pattern(self, tmp_path: Path):
        # The expected pattern embeds ``destination.name``, so it carries the hostile filename
        # even when the recorded artifact name does not.
        message = self._artifact_error(tmp_path, "plain.tmp")
        _assert_displayed(message, "plain.tmp")
        expected = f".{HOSTILE}{RECONCILE_BEFORE_IMAGE_INFIX}<nonempty>{PERSISTENCE_TEMP_SUFFIX}"
        _assert_displayed(message, expected)

    def test_artifact_path_symlink_rejection(self, tmp_path: Path):
        raw = f".{HOSTILE}{RECONCILE_BEFORE_IMAGE_INFIX}x{PERSISTENCE_TEMP_SUFFIX}"
        (tmp_path / raw).symlink_to(tmp_path / "target")
        _assert_displayed(self._artifact_error(tmp_path, raw), raw)

    def test_artifact_path_inspection_failure(self, tmp_path: Path):
        raw = f".{HOSTILE}{RECONCILE_BEFORE_IMAGE_INFIX}x{PERSISTENCE_TEMP_SUFFIX}"
        # A path whose parent is a file, not a directory: lstat fails with ENOTDIR rather
        # than the FileNotFoundError the validator treats as "nothing staged yet".
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="cannot inspect") as exc:
            reconcile_transaction._validate_artifact_path(
                tmp_path,
                tmp_path / HOSTILE,
                tmp_path / raw,
                role="before",
                raw_path=f"blocker/{raw}",
            )
        _assert_displayed(str(exc.value), f"blocker/{raw}")

    def test_artifact_path_nonregular_rejection(self, tmp_path: Path):
        raw = f".{HOSTILE}{RECONCILE_BEFORE_IMAGE_INFIX}x{PERSISTENCE_TEMP_SUFFIX}"
        (tmp_path / raw).mkdir()
        _assert_displayed(self._artifact_error(tmp_path, raw), raw)

    def _entry(self, destination: Path, before: Path, after: Path):
        return reconcile_transaction._ResolvedEntry(
            destination=destination,
            before_path=before,
            before_sha256="0" * 64,
            after_path=after,
            after_sha256="1" * 64,
        )

    def test_destination_aliases_the_journal(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        entry = self._entry(journal, tmp_path / "b", tmp_path / "a")
        with pytest.raises(ValueError, match="aliases journal path") as exc:
            reconcile_transaction._validate_path_roles((entry,), journal)
        _assert_displayed(str(exc.value), journal)

    def test_duplicate_destination_across_entries(self, tmp_path: Path):
        destination = tmp_path / HOSTILE
        entry = self._entry(destination, tmp_path / "b", tmp_path / "a")
        with pytest.raises(ValueError, match="destination alias across entries") as exc:
            reconcile_transaction._validate_path_roles((entry, entry), tmp_path / "journal")
        _assert_displayed(str(exc.value), destination)

    def test_artifact_aliases_the_journal(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        entry = self._entry(tmp_path / "d", journal, tmp_path / "a")
        with pytest.raises(ValueError, match="before_path aliases journal path") as exc:
            reconcile_transaction._validate_path_roles((entry,), journal)
        _assert_displayed(str(exc.value), journal)

    def test_artifact_aliases_a_destination(self, tmp_path: Path):
        artifact = tmp_path / HOSTILE
        entry = self._entry(artifact, artifact, tmp_path / "a")
        with pytest.raises(ValueError, match="aliases destination path") as exc:
            reconcile_transaction._validate_path_roles((entry,), tmp_path / "journal")
        _assert_displayed(str(exc.value), artifact)

    def test_artifact_aliases_another_artifact(self, tmp_path: Path):
        artifact = tmp_path / HOSTILE
        entry = self._entry(tmp_path / "d", artifact, artifact)
        with pytest.raises(ValueError, match="artifact alias between") as exc:
            reconcile_transaction._validate_path_roles((entry,), tmp_path / "journal")
        _assert_displayed(str(exc.value), artifact)

    def test_absent_journal_load_names_the_journal(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            reconcile_transaction._load_journal(tmp_path, journal)
        _assert_displayed(exception_details(exc.value), journal)

    def test_recovery_operation_error_names_the_operand(self, tmp_path: Path):
        operand = tmp_path / HOSTILE
        error = reconcile_transaction._recovery_operation_error(
            "cleaning staged artifact", operand, tmp_path / "journal", b"{}", OSError("boom")
        )
        _assert_displayed(str(error), operand)

    def test_exact_journal_status_names_the_journal_in_every_branch(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        # absent
        _assert_displayed(reconcile_transaction._exact_journal_status(journal, b"{}")[1], journal)
        # not a regular file
        journal.mkdir()
        _assert_displayed(reconcile_transaction._exact_journal_status(journal, b"{}")[1], journal)
        journal.rmdir()
        # different bytes
        journal.write_bytes(b"other")
        _assert_displayed(reconcile_transaction._exact_journal_status(journal, b"{}")[1], journal)
        # exact
        journal.write_bytes(b"{}")
        _assert_displayed(reconcile_transaction._exact_journal_status(journal, b"{}")[1], journal)

    def test_journal_retry_status_names_the_journal(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        journal.write_bytes(b"{}")
        _assert_displayed(reconcile_transaction._journal_retry_status(journal, b"{}"), journal)

    def test_unsafe_before_error_names_destination_and_before_image(self, tmp_path: Path):
        destination = tmp_path / HOSTILE
        before = tmp_path / f"before-{HOSTILE}"
        entry = self._entry(destination, before, tmp_path / "a")
        error = reconcile_transaction._unsafe_before_error(
            entry, tmp_path / "journal", b"{}", "missing"
        )
        _assert_displayed(str(error), destination)
        _assert_displayed(str(error), before)

    def test_unsafe_artifact_error_names_stage_and_destination(self, tmp_path: Path):
        staged = tmp_path / f"stage-{HOSTILE}"
        destination = tmp_path / HOSTILE
        error = reconcile_transaction._unsafe_artifact_error(
            staged, destination, tmp_path / "journal", b"{}", "unauthenticated"
        )
        _assert_displayed(str(error), staged)
        _assert_displayed(str(error), destination)

    def test_nearest_existing_directory_rejects_a_nondirectory_ancestor(self, tmp_path: Path):
        blocker = tmp_path / HOSTILE
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(NotADirectoryError) as exc:
            reconcile_transaction._nearest_existing_directory(blocker, tmp_path)
        _assert_displayed(str(exc.value), blocker)

    def test_resync_notes_name_the_path(self, tmp_path: Path, monkeypatch):
        target = tmp_path / HOSTILE

        def fail(_path: Path, _root: Path) -> None:
            raise OSError("resync blocked")

        monkeypatch.setattr(reconcile_transaction, "_sync_artifact_parent", fail)
        primary = OSError("primary")
        assert not reconcile_transaction._resync_after_unlink(target, tmp_path, primary)
        _assert_displayed(exception_details(primary), target)

    def test_resync_verification_note_names_the_path(self, tmp_path: Path, monkeypatch):
        target = tmp_path / HOSTILE

        def blow_up_on_lstat(_self):
            raise PermissionError("lstat blocked")

        monkeypatch.setattr(reconcile_transaction, "_sync_artifact_parent", lambda *_: None)
        monkeypatch.setattr(Path, "lstat", blow_up_on_lstat)
        primary = OSError("primary")
        assert not reconcile_transaction._resync_after_unlink(target, tmp_path, primary)
        _assert_displayed(exception_details(primary), target)

    def test_reappearance_note_names_the_path(self, tmp_path: Path, monkeypatch):
        target = tmp_path / HOSTILE
        monkeypatch.setattr(reconcile_transaction, "_sync_artifact_parent", lambda *_: None)
        primary = OSError("primary")
        # `exists()` is False (so the retry runs) but `lstat()` then succeeds: the path came
        # back between the two reads, which is the note this asserts.
        monkeypatch.setattr(Path, "exists", lambda _self: False)
        monkeypatch.setattr(Path, "lstat", lambda _self: object())
        assert not reconcile_transaction._resync_after_unlink(target, tmp_path, primary)
        _assert_displayed(exception_details(primary), target)

    def test_journal_restoration_note_names_the_journal(self, tmp_path: Path, monkeypatch):
        journal = tmp_path / HOSTILE

        def fail(*_args, **_kwargs) -> None:
            raise OSError("create blocked")

        monkeypatch.setattr(reconcile_transaction, "atomic_create_bytes", fail)
        primary = OSError("primary")
        assert not reconcile_transaction._restore_journal(journal, b"{}", primary)
        _assert_displayed(exception_details(primary), journal)

    def test_unsafe_journal_cleanup_names_the_journal(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        journal.write_bytes(b"other")
        with pytest.raises(ProjectError) as exc:
            reconcile_transaction._cleanup_journal(journal, b"{}")
        _assert_displayed(str(exc.value), journal)

    def test_unpublished_stage_cleanup_note_names_the_stage(self, tmp_path: Path, monkeypatch):
        staged = tmp_path / HOSTILE

        def fail(_path: Path) -> None:
            raise OSError("unlink blocked")

        monkeypatch.setattr(reconcile_transaction, "durable_unlink", fail)
        primary = OSError("primary")
        reconcile_transaction._cleanup_unpublished_stages([staged], primary)
        _assert_displayed(exception_details(primary), staged)

    def test_preflight_destination_aliasing_the_journal(self, tmp_path: Path):
        destination = tmp_path / HOSTILE
        destination.write_text("x", encoding="utf-8")
        journal = tmp_path / HOSTILE
        rewrite = reconcile.Rewrite(path=destination, before=b"", after=b"", applied=frozenset())
        with pytest.raises(ValueError, match="aliases journal path") as exc:
            reconcile_transaction._preflight_rewrite_destinations(
                tmp_path, journal, [rewrite], {destination: destination}
            )
        _assert_displayed(str(exc.value), destination.resolve())

    def test_preflight_duplicate_destination(self, tmp_path: Path):
        destination = tmp_path / HOSTILE
        destination.write_text("x", encoding="utf-8")
        rewrite = reconcile.Rewrite(path=destination, before=b"", after=b"", applied=frozenset())
        with pytest.raises(ValueError, match="duplicate reconcile destination") as exc:
            reconcile_transaction._preflight_rewrite_destinations(
                tmp_path, tmp_path / "journal", [rewrite, rewrite], {destination: destination}
            )
        _assert_displayed(str(exc.value), destination.resolve())

    def test_commit_operation_error_names_its_operand(self, tmp_path: Path):
        operand = tmp_path / HOSTILE
        error = reconcile_transaction._commit_operation_error(
            "replacing destination", operand, OSError("boom")
        )
        _assert_displayed(str(error), operand)

    def test_incomplete_rollback_joins_displayed_destinations(self, tmp_path: Path):
        first = tmp_path / HOSTILE
        second = tmp_path / f"other-{HOSTILE}"
        outcome = reconcile_transaction._RollbackOutcome(
            restored=(), already_before=(), unresolved=(first, second), untouched=()
        )
        prepared = reconcile_transaction._PreparedTransaction(
            journal=_LoadedJournal(
                version=RECONCILE_JOURNAL_VERSION, state="prepared", entries=(), provenance=None
            ),
            provenance=JournalProvenance(
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                tool_version="0.0.0",
                selector=JournalSelector(mode="all", downstream_id=None, ref=None),
            ),
            entries=(),
            journal_path=tmp_path / "journal",
            journal_bytes=b"{}",
        )
        primary = ReconcilePersistenceError("primary")

        def _unresolved_rollback(*_args, **_kwargs):
            return outcome

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(reconcile_transaction, "_rollback_prepared", _unresolved_rollback)
            with pytest.raises(ReconcilePersistenceError) as exc:
                reconcile_transaction._abort_prepared(
                    prepared, primary, candidates=frozenset(), authenticate_all=False
                )
        _assert_displayed(str(exc.value), first)
        _assert_displayed(str(exc.value), second)

    def test_dry_run_refusal_names_the_journal(self, tmp_path: Path, monkeypatch):
        journal = tmp_path / HOSTILE
        journal.write_bytes(b"{}")
        monkeypatch.setattr(reconcile_transaction, "_journal_path", lambda _root: journal)
        with pytest.raises(ProjectError) as exc:
            reconcile_transaction.ensure_dry_run_safe(tmp_path)
        _assert_displayed(str(exc.value), journal)


class TestPersistenceSinks:
    """The shared durable-write helper's one path-bearing note."""

    def test_helper_owned_stage_cleanup_note(self, tmp_path: Path):
        staged = tmp_path / HOSTILE
        note = persistence._unpublished_stage_cleanup_note(staged, OSError("cleanup blocked"))
        _assert_displayed(note, staged)


class TestRecoveryReportSinks:
    """Reconcile's human recovery reporting, the third group GTX-209 owns."""

    def _stderr(self, recovery: RecoveryResult, cwd: Path) -> str:
        errors = StringIO()
        _report_recovery_problems(_runtime(StringIO(), errors, cwd), recovery)
        return errors.getvalue()

    def test_summary_line_names_the_journal(self, tmp_path: Path):
        journal = tmp_path / HOSTILE
        output = StringIO()
        _report_recovery(
            _runtime(output, StringIO(), tmp_path),
            RecoveryResult(action="none", journal=journal),
            json_out=False,
        )
        _assert_displayed(output.getvalue(), journal)

    def test_unresolved_destination_line(self, tmp_path: Path):
        recovery = RecoveryResult(
            action="partially_rolled_back",
            journal=tmp_path / "journal",
            unresolved=(f"docs/{HOSTILE}",),
        )
        _assert_displayed(self._stderr(recovery, tmp_path), f"docs/{HOSTILE}")

    def test_orphaned_artifact_line(self, tmp_path: Path):
        orphan = f"docs/.{HOSTILE}{RECONCILE_AFTER_IMAGE_INFIX}x{PERSISTENCE_TEMP_SUFFIX}"
        recovery = RecoveryResult(action="none", journal=tmp_path / "journal", orphans=(orphan,))
        _assert_displayed(self._stderr(recovery, tmp_path), orphan)

    def test_scan_failure_displays_the_path_component_alone(self, tmp_path: Path):
        failure = ScanFailure(filename=str(tmp_path / HOSTILE), detail="Permission denied")
        recovery = RecoveryResult(
            action="none", journal=tmp_path / "journal", scan_errors=(failure,)
        )
        text = self._stderr(recovery, tmp_path)
        _assert_displayed(text, tmp_path / HOSTILE)
        # The operating system's own sentence stays prose: only its path component is quoted.
        assert "for orphaned artifacts: Permission denied" in text

    def test_scan_failure_json_keeps_the_legacy_spelling(self, tmp_path: Path):
        failure = ScanFailure(filename=str(tmp_path / HOSTILE), detail="Permission denied")
        payload = _recovery_json_payload(
            RecoveryResult(action="none", journal=tmp_path / "journal", scan_errors=(failure,))
        )
        # The machine channel is deliberately not display-spelled (AD-34): it reproduces the
        # raw path exactly, escaped only by JSON's own encoder.
        encoded = json.loads(payload)["scan_errors"]
        assert encoded == [failure.legacy_text]
        assert format_path_for_display(failure.filename) not in encoded[0]


def test_machine_channels_are_deliberately_untouched(tmp_path: Path):
    """JSON and the GitHub annotation ``file=`` keep their own encoders, per the issue's scope."""
    path = tmp_path / HOSTILE
    line = github_annotation(path, tmp_path, "title", "message")
    # The annotation encoder still emits the raw relative spelling, not the display one, so the
    # value stays something GitHub can attach to a diff.
    assert format_path_for_display(path) not in line
    assert HOSTILE in line


def test_strip_control_chars_is_unchanged():
    """The pre-existing network/init helper keeps deleting controls, and keeps its consumers."""
    # The very ambiguity that disqualified it for path display: two distinct inputs, one output.
    assert strip_control_chars("\x1b[31m") == strip_control_chars("[31m") == "[31m"
