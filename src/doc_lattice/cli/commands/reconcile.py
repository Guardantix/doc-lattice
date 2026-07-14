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


def _report_recovery(runtime: CliRuntime, recovery: RecoveryResult, *, json_out: bool) -> None:
    if json_out:
        write_text(
            runtime,
            json.dumps({"action": recovery.action, "journal": str(recovery.journal)}),
        )
    elif recovery.action == "none":
        runtime.stdout.print(
            f"nothing to recover: {escape(str(recovery.journal))}",
            soft_wrap=True,
        )
    elif recovery.action == "rolled_back":
        runtime.stdout.print(
            f"rolled back reconcile transaction: {escape(str(recovery.journal))}",
            soft_wrap=True,
        )
    else:
        runtime.stdout.print(
            f"cleaned committed reconcile transaction: {escape(str(recovery.journal))}",
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
                return

            with reconcile_lock(project.project_root) as lock:
                if dry_run:
                    ensure_dry_run_safe(project.project_root)
                else:
                    recovery = recover_transaction(project.project_root, lock=lock)
                    if recovery.action != "none":
                        runtime.stderr.print(f"recovered reconcile transaction: {recovery.action}")

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
                        lock=lock,
                    )
            _report_reconcile(
                runtime,
                plan,
                rewrites,
                dry_run=dry_run,
                json_out=json_out,
            )
