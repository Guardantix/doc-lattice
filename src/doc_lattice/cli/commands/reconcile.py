"""Typer adapter for transactional reconcile orchestration."""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ...constants import VALID_BASIC_OUTPUT_FORMATS
from ...error_types import UnreadableDocError
from ...path_utils import format_path_for_display, safe_resolve
from ...reconcile import Rewrite, plan_rewrites
from ...reconcile import reconcile as plan_reconcile
from ...reconcile_transaction import (
    JournalProvenance,
    JournalSelector,
    RecoveryAction,
    RecoveryResult,
    commit_rewrites,
    ensure_dry_run_safe,
    journal_timestamp_text,
    reconcile_lock,
    recover_transaction,
)
from ..errors import EXIT_TOOL_ERROR, exit_on_project_error
from ..options import BasicFormatOpt, ConfigOpt
from ..output import select_output, write_text
from ..runtime import CliRuntime, get_runtime


def _reconcile_json_payload(
    plan: dict[Path, dict[str, str]], rewrites: list[Rewrite], *, dry_run: bool
) -> str:
    entries = sorted(
        (
            {
                "path": str(rewrite.path),
                "ref": target_ref,
                "new_seen": plan[rewrite.path][target_ref],
            }
            for rewrite in rewrites
            for target_ref in rewrite.applied
        ),
        key=lambda entry: (entry["path"], entry["ref"]),
    )
    return json.dumps({"dry_run": dry_run, "reconciled": entries})


def _print_reconcile_lines(
    runtime: CliRuntime,
    path: Path,
    applied: frozenset[str],
    *,
    dry_run: bool,
) -> None:
    verb = "would reconcile" if dry_run else "reconciled"
    # The basename is still repo-controlled, so it is displayed rather than interpolated raw.
    name = escape(format_path_for_display(Path(path.name)))
    for target_ref in sorted(applied):
        runtime.stdout.print(f"{verb} {name}: {escape(target_ref)}")


def _journal_selector(
    downstream_id: str,
    *,
    reconcile_all: bool,
    ref: str | None,
) -> JournalSelector:
    """Build the typed selector the transaction journal records for this run.

    The transaction boundary never sees these arguments, so the adapter that owns them
    builds the selector. It mirrors the planner's precedence rather than the raw argument
    pair: ``--all`` wins and the downstream id is ignored, exactly as ``reconcile`` plans it,
    so a recovered journal names the selection that actually ran.

    Args:
        downstream_id: The node argument, empty when the run selected everything.
        reconcile_all: Whether the run selected every drifting edge.
        ref: The single upstream ref the run narrowed to, or None for all of them.

    Returns:
        The selector to record in the journal.
    """
    if reconcile_all:
        return JournalSelector(mode="all", downstream_id=None, ref=ref)
    return JournalSelector(mode="downstream", downstream_id=downstream_id, ref=ref)


def _resolve_reconcile_write_paths(
    plan: dict[Path, dict[str, str]], project_root: Path
) -> dict[Path, Path]:
    write_paths: dict[Path, Path] = {}
    for path in plan:
        try:
            write_paths[path] = safe_resolve(path, project_root)
        except ValueError as exc:
            msg = f"cannot write {format_path_for_display(path)}: it escapes the project root"
            raise UnreadableDocError(msg) from exc
    return write_paths


def _report_reconcile(
    runtime: CliRuntime,
    plan: dict[Path, dict[str, str]],
    rewrites: list[Rewrite],
    *,
    dry_run: bool,
    json_out: bool,
) -> None:
    if json_out:
        write_text(runtime, _reconcile_json_payload(plan, rewrites, dry_run=dry_run))
        return
    for rewrite in rewrites:
        _print_reconcile_lines(runtime, rewrite.path, rewrite.applied, dry_run=dry_run)
    if not rewrites:
        runtime.stdout.print("nothing to reconcile")


_RECOVERY_SUMMARIES: dict[RecoveryAction, str] = {
    "none": "nothing to recover",
    "rolled_back": "rolled back reconcile transaction",
    "partially_rolled_back": "partially rolled back reconcile transaction",
    "cleaned_committed": "cleaned committed reconcile transaction",
}


def _provenance_payload(
    provenance: JournalProvenance | None,
) -> dict[str, str | dict[str, str | None]] | None:
    """Render journal provenance for the machine channel, or None when there is none.

    Null covers both a recovered version 1 journal and a run that found no journal at all.
    ``action`` already separates those two, so a second key spelling the journal version
    would only give a consumer a way to disagree with it.

    Args:
        provenance: The provenance a version 2 journal recorded, or None.

    Returns:
        The provenance object with its values in their recorded spelling, or None.
    """
    if provenance is None:
        return None
    selector = provenance.selector
    return {
        "created_at": journal_timestamp_text(provenance.created_at),
        "tool_version": provenance.tool_version,
        "selector": {
            "mode": selector.mode,
            "downstream_id": selector.downstream_id,
            "ref": selector.ref,
        },
    }


def _recovery_json_payload(recovery: RecoveryResult) -> str:
    return json.dumps(
        {
            "action": recovery.action,
            "journal": str(recovery.journal),
            "restored": recovery.restored,
            "already_before": recovery.already_before,
            "unresolved": list(recovery.unresolved),
            "orphans": list(recovery.orphans),
            # The machine channel keeps the pre-GTX-209 spelling exactly, path component
            # included: AD-34 excludes JSON from the display spelling, and `_scan_orphan_artifacts`
            # already orders the records by this rendering so the array order is unchanged too.
            "scan_errors": [failure.legacy_text for failure in recovery.scan_errors],
            # The journal's own strings, in the spelling it recorded them in: this channel is
            # excluded from the display spelling for the reason AD-34 records, and the
            # encoder escapes a control character rather than emitting it raw.
            "provenance": _provenance_payload(recovery.provenance),
        }
    )


def _report_recovery(runtime: CliRuntime, recovery: RecoveryResult, *, json_out: bool) -> None:
    if json_out:
        write_text(runtime, _recovery_json_payload(recovery))
        return
    # "nothing to recover" is a completeness claim, so an orphan-bearing run must not make
    # it even though no journal was there to recover.
    if recovery.action == "none" and recovery.is_incomplete:
        summary = "no reconcile journal to recover"
    else:
        summary = _RECOVERY_SUMMARIES[recovery.action]
    runtime.stdout.print(
        f"{summary}: {escape(format_path_for_display(recovery.journal))}", soft_wrap=True
    )
    _report_provenance(runtime, recovery)


def _display_journal_string(value: str | None) -> str:
    """Spell one journal-recorded string for a person, or name its absence.

    A journal is a file a person can edit, so its strings are untrusted exactly as a document
    path is. AD-36 gives them AD-34's spelling for that reason: ``repr`` is injective and
    leaves no control character in the line, and the Rich escape on top of it neutralizes
    markup, which is printable text no control-character rule would catch.

    Args:
        value: A recorded string, or None for a selector field the run left unset.

    Returns:
        The quoted display spelling, or a bare ``null``. The quoting is what keeps the two
        apart: a recorded string spelling the word null reads as ``'null'``.
    """
    return "null" if value is None else escape(repr(value))


def _report_provenance(runtime: CliRuntime, recovery: RecoveryResult) -> None:
    """Print what produced the recovered journal, or say the format recorded nothing.

    Silent when no journal was found: there is no provenance to be absent.
    """
    provenance = recovery.provenance
    if provenance is None:
        if recovery.journal_version is not None:
            runtime.stdout.print(
                f"  provenance: not recorded by journal version {recovery.journal_version}"
            )
        return
    selector = provenance.selector
    runtime.stdout.print(f"  created_at: {journal_timestamp_text(provenance.created_at)}")
    runtime.stdout.print(
        f"  tool_version: {_display_journal_string(provenance.tool_version)}", soft_wrap=True
    )
    runtime.stdout.print(
        f"  selector: mode {_display_journal_string(selector.mode)}, "
        f"downstream_id {_display_journal_string(selector.downstream_id)}, "
        f"ref {_display_journal_string(selector.ref)}",
        soft_wrap=True,
    )


def _report_recovery_problems(runtime: CliRuntime, recovery: RecoveryResult) -> None:
    if recovery.unresolved:
        runtime.stderr.print(
            f"[red]error[/red]: reconcile recovery could not restore "
            f"{len(recovery.unresolved)} destination(s)",
            soft_wrap=True,
        )
        for destination in recovery.unresolved:
            runtime.stderr.print(
                f"  unresolved destination: {escape(format_path_for_display(destination))}",
                soft_wrap=True,
            )
        runtime.stderr.print(
            "the prepared journal and every remaining staged image were retained; to finish "
            "the rollback, restore each destination to its recorded before or after image, "
            "then rerun 'doc-lattice reconcile --recover'; to keep the current bytes instead, "
            "inspect the journal and then move it and the stages it names aside yourself, "
            "since rerunning recovery cannot resolve bytes it has no record of",
            soft_wrap=True,
        )
    if recovery.orphans:
        runtime.stderr.print(
            "[red]error[/red]: orphaned reconcile artifacts remain; nothing was deleted",
            soft_wrap=True,
        )
        for orphan in recovery.orphans:
            runtime.stderr.print(
                f"  orphaned artifact: {escape(format_path_for_display(orphan))}", soft_wrap=True
            )
        runtime.stderr.print(
            "inspect each artifact and remove it manually after confirming it is not a destination",
            soft_wrap=True,
        )
    # The human encoder for a scan failure. The display spelling applies to the path
    # component alone; the operating system's own message is escaped as ordinary prose, exactly
    # as every other interpolated cause in this adapter is.
    for failure in recovery.scan_errors:
        runtime.stderr.print(
            "[red]error[/red]: cannot scan "
            f"{escape(format_path_for_display(failure.filename))} for orphaned artifacts: "
            f"{escape(failure.detail)}",
            soft_wrap=True,
        )


def register_reconcile(app: typer.Typer) -> None:
    """Register the ``reconcile`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def reconcile(  # noqa: PLR0913
        ctx: typer.Context,
        downstream_id: Annotated[
            str, typer.Argument(help="Node whose edges to reconcile (omit when using --all).")
        ] = "",
        ref: Annotated[
            str | None, typer.Option("--ref", help="Reconcile only this upstream ref.")
        ] = None,
        reconcile_all: Annotated[
            bool, typer.Option("--all", help="Reconcile every drifting edge.")
        ] = False,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Show what would be reconciled without writing.")
        ] = False,
        recover: Annotated[
            bool,
            typer.Option("--recover", help="Recover or clean up a prior transaction, then exit."),
        ] = False,
        config: ConfigOpt = None,
        fmt: BasicFormatOpt = "human",
    ) -> None:
        """Set seen to current upstream hashes for the selected edges.

        With --dry-run, computes and reports the same plan without writing anything.
        With --recover, performs only recovery or cleanup and never plans a batch.
        """
        runtime = get_runtime(ctx)
        selection = select_output(runtime, fmt=fmt, valid=VALID_BASIC_OUTPUT_FORMATS)
        json_out = selection.format == "json"
        if recover and (downstream_id or reconcile_all or ref is not None or dry_run):
            runtime.stderr.print(
                "[red]error[/red]: --recover cannot be combined with a downstream id, "
                "--all, --ref, or --dry-run"
            )
            raise typer.Exit(EXIT_TOOL_ERROR)
        if not recover and not reconcile_all and not downstream_id:
            runtime.stderr.print("[red]error[/red]: provide a downstream id or --all")
            raise typer.Exit(EXIT_TOOL_ERROR)
        with exit_on_project_error(runtime):
            project = runtime.project(config)

            if recover:
                with reconcile_lock(project.project_root) as lock:
                    recovery = recover_transaction(project.project_root, lock=lock)
                _report_recovery(runtime, recovery, json_out=json_out)
                _report_recovery_problems(runtime, recovery)
                if recovery.is_incomplete:
                    raise typer.Exit(EXIT_TOOL_ERROR)
                return

            with reconcile_lock(project.project_root) as lock:
                if dry_run:
                    ensure_dry_run_safe(project.project_root)
                else:
                    recovery = recover_transaction(project.project_root, lock=lock)
                    if recovery.action != "none":
                        runtime.stderr.print(f"recovered reconcile transaction: {recovery.action}")
                    _report_recovery_problems(runtime, recovery)
                    # An incomplete recovery stops here: planning against a tree that was
                    # never fully restored would reconcile from unrecovered bytes.
                    if recovery.is_incomplete:
                        raise typer.Exit(EXIT_TOOL_ERROR)

                lattice = runtime.lattice(
                    project,
                    require_verified=True,
                    persist_cache=not dry_run,
                )
                # The rewrite phase rereads each downstream file's frontmatter, so it is a
                # second place a YAML warning can reach the user in one invocation. Without
                # this the load's warnings would carry the CLI's voice and the reread's would
                # carry Python's default format, in the same run.
                with runtime.rendered_warnings():
                    plan = plan_reconcile(
                        lattice,
                        downstream_id,
                        ref=ref,
                        reconcile_all=reconcile_all,
                    )
                    write_paths = _resolve_reconcile_write_paths(plan, project.project_root)
                    rewrites = plan_rewrites(plan, lambda path: write_paths[path].read_bytes())
                    if not dry_run and rewrites:
                        commit_rewrites(
                            project.project_root,
                            rewrites,
                            write_paths,
                            selector=_journal_selector(
                                downstream_id,
                                reconcile_all=reconcile_all,
                                ref=ref,
                            ),
                            lock=lock,
                        )
            _report_reconcile(
                runtime,
                plan,
                rewrites,
                dry_run=dry_run,
                json_out=json_out,
            )
