"""Command-line interface."""

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from . import __version__
from .check import check_lattice, has_drift
from .config import DEFAULT_CONFIG_NAME, load_config
from .error_types import ConfigError, ProjectError, UnreadableDocError
from .impact import impact as impact_walk
from .linear_fetch import fetch_tickets
from .linear_query import is_valid_team_key
from .linear_render import findings_json, render_findings
from .lint import LintResult, lint_lattice
from .model import Lattice
from .orchestrate import load_lattice
from .reconcile import apply_reconcile
from .reconcile import reconcile as plan_reconcile
from .render import to_dot, to_mermaid
from .scaffold import build_scaffold
from .stale_shipped import build_audit_trigger, build_from_trigger, stale_shipped
from .text_utils import strip_control_chars

app = typer.Typer(no_args_is_help=True, add_completion=False)
_out = Console()
_err = Console(stderr=True)

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to .game-lattice.yml.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]

_STATE_COL_WIDTH = 13  # widest EdgeState ("UNRECONCILED") is 12 chars, plus one trailing space


def _print_project_error(exc: ProjectError) -> None:
    """Render a ProjectError to stderr in the standard one-line format."""
    _err.print(f"[red]error[/red]: {escape(str(exc))} ({exc.code})")


def _version_callback(value: bool) -> None:
    if value:
        _out.print(__version__)
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """game-lattice: documentation traceability engine."""


def _load(config: Path | None) -> Lattice:
    project = load_config(config, Path.cwd())
    return load_lattice(project)


def _skip_summary(result: LintResult) -> str:
    """Render the one-line coverage summary printed after any human lint run."""
    violations = len(result.violations)
    unranked = len(result.skipped)
    targets = sum(1 for skipped in result.skipped if skipped.reason == "target-unannotated")
    sources = sum(1 for skipped in result.skipped if skipped.reason == "source-unannotated")
    label = "violation" if violations == 1 else "violations"
    line = f"{violations} ladder {label}, {unranked} edges unranked"
    if unranked:
        line += f" ({targets} target unannotated, {sources} source unannotated)"
    return line


@app.command()
def check(config: ConfigOpt = None, json_out: JsonOpt = False) -> None:
    """Classify every edge; exit 1 on drift, 2 on tool error."""
    try:
        lattice = _load(config)
        statuses = check_lattice(lattice)
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc
    if json_out:
        payload = {
            "edges": [
                {
                    "source_id": status.source_id,
                    "target_ref": status.target_ref,
                    "target_id": status.target_id,
                    "state": status.state,
                    "expected": status.expected,
                    "actual": status.actual,
                }
                for status in statuses
            ]
        }
        typer.echo(json.dumps(payload))
    else:
        state_colors = {"OK": "green", "STALE": "yellow", "UNRECONCILED": "yellow", "BROKEN": "red"}
        for status in statuses:
            color = state_colors[status.state]
            _out.print(
                f"[{color}]{status.state:<{_STATE_COL_WIDTH}}[/{color}] "
                f"{escape(status.source_id)} -> {escape(status.target_ref)}"
            )
    raise typer.Exit(1 if has_drift(statuses) else 0)


@app.command()
def lint(config: ConfigOpt = None, json_out: JsonOpt = False) -> None:
    """Validate the authority ladder; exit 1 on a violation, 2 on tool error."""
    try:
        lattice = _load(config)
        result = lint_lattice(lattice)
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc
    if json_out:
        payload = {
            "violations": [
                {
                    "source_id": violation.source_id,
                    "source_authority": violation.source_authority,
                    "target_id": violation.target_id,
                    "target_ref": violation.target_ref,
                    "target_authority": violation.target_authority,
                }
                for violation in result.violations
            ],
            "skipped": [
                {
                    "source_id": skipped.source_id,
                    "target_ref": skipped.target_ref,
                    "target_id": skipped.target_id,
                    "reason": skipped.reason,
                }
                for skipped in result.skipped
            ],
        }
        typer.echo(json.dumps(payload))
    else:
        for violation in result.violations:
            _out.print(
                f"[red]VIOLATION[/red]  {escape(violation.source_id)} "
                f"({violation.source_authority}) -> {escape(violation.target_ref)} "
                f"({violation.target_authority})"
            )
        _out.print(_skip_summary(result))
    raise typer.Exit(1 if result.violations else 0)


@app.command()
def impact(token: str, config: ConfigOpt = None, json_out: JsonOpt = False) -> None:
    """List every downstream doc affected by a change to TOKEN."""
    try:
        lattice = _load(config)
        affected = impact_walk(lattice, token)
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc
    if json_out:
        payload = {
            "affected": [
                {
                    "id": node.id,
                    "title": node.title,
                    "path": str(node.path),
                    "tickets": list(node.tickets),
                }
                for node in affected
            ]
        }
        typer.echo(json.dumps(payload))
    else:
        for node in affected:
            tickets = ", ".join(node.tickets) if node.tickets else "-"
            _out.print(f"{escape(node.id)}  ({escape(str(node.path))})  tickets: {escape(tickets)}")


@app.command()
def reconcile(
    downstream_id: Annotated[
        str, typer.Argument(help="Node whose edges to reconcile (omit when using --all).")
    ] = "",
    ref: Annotated[
        str | None, typer.Option("--ref", help="Reconcile only this upstream ref.")
    ] = None,
    reconcile_all: Annotated[
        bool, typer.Option("--all", help="Reconcile every drifting edge.")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Set seen to current upstream hashes for the selected edges."""
    if not reconcile_all and not downstream_id:
        _err.print("[red]error[/red]: provide a downstream id or --all")
        raise typer.Exit(2)
    try:
        lattice = _load(config)
        plan = plan_reconcile(lattice, downstream_id, ref=ref, reconcile_all=reconcile_all)
        # Phase 1: compute every rewrite from a fresh read before touching disk, so a
        # malformed concurrent edit aborts the whole command instead of leaving an
        # earlier file already rewritten (no cross-file half-reconcile).
        rewrites: list[tuple[Path, str, set[str]]] = []
        for path, updates in plan.items():
            try:
                fresh = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                msg = f"cannot read {path} to reconcile: {exc}"
                raise UnreadableDocError(msg) from exc
            new_text, applied = apply_reconcile(fresh, updates)
            if applied:
                rewrites.append((path, new_text, applied))
        # Phase 2: only after all rewrites computed cleanly, write them.
        for path, new_text, applied in rewrites:
            try:
                _atomic_write(path, new_text)
            except OSError as exc:
                msg = f"cannot write {path}: {exc}"
                raise UnreadableDocError(msg) from exc
            for target_ref in sorted(applied):
                _out.print(f"reconciled {escape(path.name)}: {escape(target_ref)}")
        if not rewrites:
            _out.print("nothing to reconcile")
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc


@app.command()
def graph(
    fmt: Annotated[str, typer.Option("--format", help="mermaid or dot.")] = "mermaid",
    config: ConfigOpt = None,
) -> None:
    """Emit the edge graph as Mermaid or DOT."""
    try:
        lattice = _load(config)
        stale = {
            (s.source_id, s.target_id)
            for s in check_lattice(lattice)
            if s.state == "STALE" and s.target_id is not None
        }
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc
    rendered = to_dot(lattice, stale) if fmt == "dot" else to_mermaid(lattice, stale)
    typer.echo(rendered, nl=False)


@app.command()
def linear(  # noqa: PLR0913
    target: Annotated[
        str, typer.Argument(help="Narrow the audit to this id and the nodes that derive from it.")
    ] = "",
    from_id: Annotated[
        str | None, typer.Option("--from", help="Forward-looking: impact-walk from this id.")
    ] = None,
    exit_code: Annotated[
        bool, typer.Option("--exit-code", help="Exit 1 on any DANGER or BLOCKED finding.")
    ] = False,
    warn_exit: Annotated[
        bool, typer.Option("--warn-exit", help="With --exit-code, also exit 1 on WARNING.")
    ] = False,
    config: ConfigOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Report tickets shipped against a spec that has since drifted."""
    if from_id is not None and target:
        _err.print("[red]error[/red]: pass a positional target or --from, not both")
        raise typer.Exit(2)
    try:
        project = load_config(config, Path.cwd())
        lattice = load_lattice(project)
        if from_id is not None:
            trigger = build_from_trigger(lattice, from_id)
        else:
            trigger = build_audit_trigger(lattice, target or None)
        refs = {ref for node_id in trigger for ref in lattice.nodes_by_id[node_id].tickets}
        tickets, rejected = fetch_tickets(refs, project.config.linear_team)
        findings = stale_shipped(lattice, trigger, tickets, rejected)
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc
    if json_out:
        typer.echo(json.dumps(findings_json(findings)))
    else:
        render_findings(_out, findings)
    if exit_code:
        gate = {"DANGER", "BLOCKED"} | ({"WARNING"} if warn_exit else set())
        if any(finding.severity in gate for finding in findings):
            raise typer.Exit(1)
    raise typer.Exit(0)


def _validate_init_flags(docs_roots: tuple[str, ...], linear_team: str | None) -> None:
    """Reject flag values that would corrupt the generated config.

    Args:
        docs_roots: The docs roots from --docs-root (or the default).
        linear_team: The --linear-team value, or None.

    Raises:
        ConfigError: If a value is empty or holds a control character, a docs root
            is absolute or contains a parent reference, or linear_team is not a valid
            Linear team key (which the linear command would later reject).
    """
    values = list(docs_roots)
    if linear_team is not None:
        values.append(linear_team)
    for value in values:
        if not value or strip_control_chars(value) != value:
            msg = f"flag value {value!r} is empty or contains a control character"
            raise ConfigError(msg)
    for root in docs_roots:
        if Path(root).is_absolute() or ".." in Path(root).parts:
            msg = (
                f"--docs-root {root!r} must be a relative path inside the project, "
                "without '..' or a leading slash"
            )
            raise ConfigError(msg)
    if linear_team is not None and not is_valid_team_key(linear_team):
        msg = (
            f"--linear-team {linear_team!r} must be a Linear team key: uppercase letters "
            "and digits, starting with a letter, for example ENG. The linear command "
            "rejects any other value."
        )
        raise ConfigError(msg)


@app.command()
def init(
    docs_root: Annotated[
        list[str] | None,
        typer.Option("--docs-root", help="Docs root to write (repeatable). Defaults to docs."),
    ] = None,
    linear_team: Annotated[
        str | None,
        typer.Option(
            "--linear-team",
            help="Linear team key (uppercase, for example ENG) to bake into the config.",
        ),
    ] = None,
) -> None:
    """Scaffold .game-lattice.yml and print pre-commit and CI codegen."""
    try:
        roots = tuple(docs_root) if docs_root else ("docs",)
        _validate_init_flags(roots, linear_team)
        scaffold = build_scaffold(roots, linear_team, f"v{__version__}")
        target = Path.cwd() / DEFAULT_CONFIG_NAME
        try:
            _atomic_create(target, scaffold.config_text)
        except FileExistsError:
            _err.print(f"{escape(target.name)} already exists, leaving it untouched")
        except OSError as exc:
            msg = f"cannot write {target.name}: {exc}"
            raise ConfigError(msg) from exc
        else:
            _err.print(f"wrote {escape(target.name)}")
        typer.echo("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
        typer.echo(scaffold.precommit_text)
        typer.echo("# ===== .github/workflows/game-lattice.yml (new file) =====")
        typer.echo(scaffold.ci_text)
        _err.print(
            "Add the pre-commit block under `repos:`, save the workflow as "
            ".github/workflows/game-lattice.yml, and make sure the "
            f"v{__version__} tag is pushed so the pinned snippets resolve."
        )
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc
    raise typer.Exit(0)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_create(path: Path, text: str) -> None:
    """Create path with text, crash-safe and never overwriting an existing file.

    Writes the full text to a unique temp file in the same directory through a
    buffered file object (so a short write surfaces as an error rather than a
    truncated file), fsyncs it so the bytes are durable, then publishes by
    hard-linking the temp onto the final path. os.link is atomic and raises
    FileExistsError if the target already exists, so the final path only ever
    appears complete, never empty or partial. The temp is always removed, so a
    failed run leaves no litter.

    Raises:
        FileExistsError: If path already exists.
        OSError: If the write or the link fails for another reason.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    """Console-script entry point.

    Wraps ``app()`` so an unexpected filesystem or path error (for example a symlink
    loop surfacing as ``RuntimeError`` from ``Path.resolve()``) exits with the tool-error
    code 2 instead of Python's default 1, which ``check`` reserves to mean "drift
    detected". Intended exits raised by typer (``SystemExit``) propagate unchanged.
    """
    try:
        app()
    except ProjectError as exc:
        _print_project_error(exc)
        raise SystemExit(2) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        _err.print(f"[red]internal error[/red]: {type(exc).__name__}: {escape(str(exc))}")
        raise SystemExit(2) from exc
