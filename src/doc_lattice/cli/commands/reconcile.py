"""Typer adapter for transactional reconcile orchestration."""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ...constants import VALID_BASIC_OUTPUT_FORMATS
from ...error_types import UnreadableDocError
from ...path_utils import safe_resolve
from ...reconcile import Rewrite, plan_rewrites
from ...reconcile import reconcile as plan_reconcile
from ...reconcile_transaction import (
    JournalSelector,
    RecoveryAction,
    RecoveryResult,
    commit_rewrites,
    ensure_dry_run_safe,
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
    for target_ref in sorted(applied):
        runtime.stdout.print(f"{verb} {escape(path.name)}: {escape(target_ref)}")


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
            msg = f"cannot write {path}: it escapes the project root"
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


def _recovery_json_payload(recovery: RecoveryResult) -> str:
    return json.dumps(
        {
            "action": recovery.action,
            "journal": str(recovery.journal),
            "restored": recovery.restored,
            "already_before": recovery.already_before,
            "unresolved": list(recovery.unresolved),
            "orphans": list(recovery.orphans),
            "scan_errors": list(recovery.scan_errors),
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
    runtime.stdout.print(f"{summary}: {escape(str(recovery.journal))}", soft_wrap=True)


def _report_recovery_problems(runtime: CliRuntime, recovery: RecoveryResult) -> None:
    if recovery.unresolved:
        runtime.stderr.print(
            f"[red]error[/red]: reconcile recovery could not restore "
            f"{len(recovery.unresolved)} destination(s)",
            soft_wrap=True,
        )
        for destination in recovery.unresolved:
            runtime.stderr.print(f"  unresolved destination: {escape(destination)}", soft_wrap=True)
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
            runtime.stderr.print(f"  orphaned artifact: {escape(orphan)}", soft_wrap=True)
        runtime.stderr.print(
            "inspect each artifact and remove it manually after confirming it is not a destination",
            soft_wrap=True,
        )
    for detail in recovery.scan_errors:
        runtime.stderr.print(f"[red]error[/red]: {escape(detail)}", soft_wrap=True)


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
