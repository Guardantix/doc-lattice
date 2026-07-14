"""Command-line interface."""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import typer.rich_utils
from rich.console import Console
from rich.markup import escape

from . import __version__
from .check import EdgeStatus, check_lattice, has_drift, statuses_json
from .config import DEFAULT_CONFIG_NAME, load_config
from .constants import (
    VALID_EDGE_STATES,
    VALID_GRAPH_FORMATS,
    VALID_REPORT_FORMATS,
    ReportFormat,
)
from .error_types import (
    ConfigError,
    ProjectError,
    UnreadableDocError,
    copy_exception_notes,
    exception_details,
)
from .impact import impact as impact_walk
from .impact import impact_json
from .linear_fetch import fetch_tickets
from .linear_query import is_valid_team_key
from .linear_render import findings_json, render_findings
from .lint import lint_json, lint_lattice
from .model import Lattice
from .orchestrate import load_lattice
from .path_utils import safe_resolve
from .persistence import atomic_create_bytes
from .reconcile import Rewrite, plan_rewrites
from .reconcile import reconcile as plan_reconcile
from .reconcile_transaction import (
    RecoveryResult,
    commit_rewrites,
    ensure_dry_run_safe,
    reconcile_lock,
    recover_transaction,
)
from .render import to_dot, to_json, to_mermaid
from .report_render import render_impact, render_lint, render_statuses
from .scaffold import build_scaffold
from .stale_shipped import build_audit_trigger, build_from_trigger, stale_shipped
from .text_utils import strip_control_chars

app = typer.Typer(no_args_is_help=True, add_completion=False)
_out = Console()
_err = Console(stderr=True)

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to .doc-lattice.yml.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]
IndentOpt = Annotated[
    int | None,
    typer.Option("--indent", min=0, help="Pretty-print JSON with this indent (requires --json)."),
]


def _escape_github_message(value: str) -> str:
    """Escape a GitHub workflow-command message value."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_github_property(value: str) -> str:
    """Escape a GitHub workflow-command property value."""
    return _escape_github_message(value).replace(":", "%3A").replace(",", "%2C")


def _github_annotation(path: Path, root: Path, title: str, message: str) -> str:
    """Render one ``::error`` GitHub Actions annotation for a finding.

    The ``file`` property is emitted relative to ``root`` so GitHub Actions can attach
    the annotation to the offending document in the pull request diff; an absolute path
    would strand the annotation in the run summary, detached from the file. ``root``
    should be the invocation ``cwd``, not the resolved project root: a ``--config``
    pointing at a lattice in a subdirectory (a monorepo layout) must not strip that
    subdirectory prefix from the reported path, since GitHub Actions checks out the
    repository at ``GITHUB_WORKSPACE`` and a ``run:`` step's cwd defaults to it. When
    ``path`` falls outside ``root`` (an out-of-tree ``--config``), the absolute path is
    used instead of raising.

    Args:
        path: Absolute path of the source document.
        root: Root the file path is reported relative to (the invocation cwd).
        title: Annotation title, before escaping.
        message: Annotation message, before escaping.

    Returns:
        A single workflow-command line, with the file path, title, and message escaped.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return (
        f"::error file={_escape_github_property(str(relative))},"
        f"title={_escape_github_property(title)}::{_escape_github_message(message)}"
    )


def _reject_bad_format(fmt: str, valid: frozenset[str]) -> NoReturn:
    """Print the standard unsupported-format error and exit 2.

    Args:
        fmt: The rejected ``--format`` value.
        valid: The formats the command accepts.

    Raises:
        typer.Exit: Always, with exit code 2.
    """
    options = ", ".join(sorted(valid))
    _err.print(f"[red]error[/red]: --format {escape(f'{fmt!r}')} must be one of: {options}")
    raise typer.Exit(2)


def _resolve_report_format(fmt: str, json_out: bool) -> ReportFormat:
    """Validate output flags and return the effective report format.

    ``--format`` is validated before the ``--json`` alias is honored, so a typoed or
    unsupported format fails loudly with exit 2 rather than being silently masked by
    a concurrent ``--json``.

    Args:
        fmt: Explicit ``--format`` value.
        json_out: Whether the legacy ``--json`` alias was supplied.

    Returns:
        ``human``, ``json``, or ``github``.

    Raises:
        typer.Exit: Exit code 2 for a conflicting or unsupported selection.
    """
    if fmt not in VALID_REPORT_FORMATS:
        _reject_bad_format(fmt, VALID_REPORT_FORMATS)
    if json_out and fmt == "github":
        _err.print("[red]error[/red]: --json cannot be combined with --format github")
        raise typer.Exit(2)
    if json_out:
        return "json"
    return fmt  # ty: ignore[invalid-return-type]


def _print_project_error(exc: ProjectError) -> None:
    """Render a ProjectError to stderr in the standard one-line format."""
    _err.print(
        f"[red]error[/red]: {escape(exception_details(exc))} ({exc.code})",
        soft_wrap=True,
    )


@contextmanager
def _exit_on_project_error() -> Iterator[None]:
    """Convert ProjectError into the standard stderr line and tool-error exit 2.

    Yields:
        Control to CLI command code.

    Raises:
        typer.Exit: Exit code 2 when command code raises ProjectError.
    """
    try:
        yield
    except ProjectError as exc:
        _print_project_error(exc)
        raise typer.Exit(2) from exc


def _validate_indent(indent: int | None, *, json_out: bool) -> None:
    """Reject JSON indentation when JSON output is disabled."""
    if indent is not None and not json_out:
        _err.print("[red]error[/red]: --indent requires --json")
        raise typer.Exit(2)


def _parse_only_states(only: list[str] | None) -> frozenset[str] | None:
    """Normalize and validate the ``--only`` flag's values.

    Args:
        only: Raw repeated ``--only`` values, or None when the flag is absent.

    Returns:
        None when the flag is absent (no filtering), otherwise the set of
        upper-cased, validated edge states to keep.

    Raises:
        typer.Exit: Exit code 2 when a value is not a valid edge state.
    """
    if not only:
        return None
    states = frozenset(value.upper() for value in only)
    unknown = states - VALID_EDGE_STATES
    if unknown:
        valid = ", ".join(sorted(VALID_EDGE_STATES))
        bad = ", ".join(sorted(unknown))
        _err.print(f"[red]error[/red]: unknown --only state(s): {escape(bad)} (valid: {valid})")
        raise typer.Exit(2)
    return states


def _filter_statuses(statuses: list[EdgeStatus], only: frozenset[str] | None) -> list[EdgeStatus]:
    """Filter statuses to the requested states for display, leaving the input untouched.

    Args:
        statuses: The full, unfiltered edge statuses.
        only: States to keep, or None to keep everything.

    Returns:
        The subset of statuses whose state is in ``only``, or all statuses when
        ``only`` is None.
    """
    if only is None:
        return statuses
    return [status for status in statuses if status.state in only]


def _disable_color() -> None:
    """Replace the CLI consoles with explicit no-color consoles."""
    global _out, _err  # noqa: PLW0603
    _out = Console(no_color=True)
    _err = Console(stderr=True, no_color=True)


def _version_callback(value: bool) -> None:
    if value:
        _out.print(__version__)
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[  # noqa: ARG001
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable colored output.")] = False,
) -> None:
    """doc-lattice: documentation traceability engine."""
    if no_color:
        _disable_color()


def _load(config: Path | None, *, require_verified: bool = False) -> Lattice:
    """Load the lattice from the resolved project config.

    Args:
        config: Explicit ``--config`` path, or None to discover it from the cwd.
        require_verified: Force the verify tier for every file, disabling the stat fast
            tier. Reconcile passes True so its writes never derive from a stat-tier
            stale read; every other command uses the default.

    Returns:
        The built Lattice.
    """
    project = load_config(config, Path.cwd())
    return load_lattice(project, require_verified=require_verified)


@app.command()
def check(
    config: ConfigOpt = None,
    json_out: JsonOpt = False,
    indent: IndentOpt = None,
    fmt: Annotated[str, typer.Option("--format", help="human, json, or github.")] = "human",
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help=(
                "Show only these states (repeatable): OK, STALE, UNRECONCILED, BROKEN. "
                "Filters display only; the exit code always reflects every edge."
            ),
        ),
    ] = None,
) -> None:
    """Classify every edge; exit 1 on drift, 2 on tool error."""
    report_format = _resolve_report_format(fmt, json_out)
    _validate_indent(indent, json_out=report_format == "json")
    only_states = _parse_only_states(only)
    with _exit_on_project_error():
        lattice = _load(config)
        statuses = check_lattice(lattice)
    displayed = _filter_statuses(statuses, only_states)
    if report_format == "json":
        typer.echo(json.dumps(statuses_json(displayed), indent=indent))
    elif report_format == "github":
        for status in displayed:
            if status.state == "OK":
                continue
            path = lattice.nodes_by_id[status.source_id].path
            typer.echo(
                _github_annotation(
                    path,
                    Path.cwd(),
                    f"doc-lattice {status.state}",
                    f"{status.source_id} -> {status.target_ref} is {status.state}",
                )
            )
    else:
        render_statuses(_out, displayed)
    raise typer.Exit(1 if has_drift(statuses) else 0)


@app.command()
def lint(
    config: ConfigOpt = None,
    json_out: JsonOpt = False,
    indent: IndentOpt = None,
    fmt: Annotated[str, typer.Option("--format", help="human, json, or github.")] = "human",
) -> None:
    """Validate the authority ladder; exit 1 on a violation, 2 on tool error."""
    report_format = _resolve_report_format(fmt, json_out)
    _validate_indent(indent, json_out=report_format == "json")
    with _exit_on_project_error():
        lattice = _load(config)
        result = lint_lattice(lattice)
    if report_format == "json":
        typer.echo(json.dumps(lint_json(result), indent=indent))
    elif report_format == "github":
        for violation in result.violations:
            path = lattice.nodes_by_id[violation.source_id].path
            typer.echo(
                _github_annotation(
                    path,
                    Path.cwd(),
                    "doc-lattice ladder violation",
                    f"{violation.source_id} ({violation.source_authority}) -> "
                    f"{violation.target_ref} ({violation.target_authority})",
                )
            )
    else:
        render_lint(_out, result)
    raise typer.Exit(1 if result.violations else 0)


@app.command()
def impact(
    token: str,
    config: ConfigOpt = None,
    json_out: JsonOpt = False,
    indent: IndentOpt = None,
    depth: Annotated[
        int | None,
        typer.Option("--depth", min=1, help="Limit the walk to this many hops from the target."),
    ] = None,
) -> None:
    """List every downstream doc affected by a change to TOKEN.

    Informational only: it always exits 0 (2 on a tool error), so it never gates CI.
    """
    _validate_indent(indent, json_out=json_out)
    with _exit_on_project_error():
        lattice = _load(config)
        affected = impact_walk(lattice, token, max_depth=depth)
    if json_out:
        typer.echo(json.dumps(impact_json(affected), indent=indent))
    else:
        render_impact(_out, affected)


def _reconcile_json_payload(
    plan: dict[Path, dict[str, str]], rewrites: list[Rewrite], *, dry_run: bool
) -> str:
    """Build the single-line JSON payload for a reconcile run (dry or real).

    Args:
        plan: The full planned mapping of path to ``{ref: new_seen}``, used to look up
            ``new_seen`` for each applied ref.
        rewrites: The rewrites actually applied (fresh-read, non-empty ``applied`` set).
        dry_run: Whether this was a preview (no writes) or a completed real run.

    Returns:
        The JSON text, entries sorted by path then ref for deterministic output.
    """
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


def _print_reconcile_lines(path: Path, applied: frozenset[str], *, dry_run: bool) -> None:
    """Print one file's human-readable reconcile confirmation lines.

    Args:
        path: The downstream file that was (or would be) rewritten.
        applied: The refs whose seen scalar was updated in this file.
        dry_run: Whether this was a preview (would reconcile) or a real write (reconciled).
    """
    verb = "would reconcile" if dry_run else "reconciled"
    for target_ref in sorted(applied):
        _out.print(f"{verb} {escape(path.name)}: {escape(target_ref)}")


def _resolve_reconcile_write_paths(
    plan: dict[Path, dict[str, str]], project_root: Path
) -> dict[Path, Path]:
    """Map document identity paths to contained, resolved write destinations."""
    write_paths: dict[Path, Path] = {}
    for path in plan:
        try:
            write_paths[path] = safe_resolve(path, project_root)
        except ValueError as exc:
            msg = f"cannot write {path}: it escapes the project root"
            raise UnreadableDocError(msg) from exc
    return write_paths


def _report_reconcile(
    plan: dict[Path, dict[str, str]],
    rewrites: list[Rewrite],
    *,
    dry_run: bool,
    json_out: bool,
) -> None:
    """Emit a reconcile report for a dry-run preview or durable committed batch.

    Args:
        plan: The full planned mapping of path to ``{ref: new_seen}``.
        rewrites: The validated rewrites previewed or durably committed.
        dry_run: Whether this is a read-only preview.
        json_out: Whether to emit the machine-readable payload instead of human lines.
    """
    if json_out:
        typer.echo(_reconcile_json_payload(plan, rewrites, dry_run=dry_run))
        return
    for rewrite in rewrites:
        _print_reconcile_lines(rewrite.path, rewrite.applied, dry_run=dry_run)
    if not rewrites:
        _out.print("nothing to reconcile")


def _report_recovery(recovery: RecoveryResult, *, json_out: bool) -> None:
    """Report one explicit recovery-only outcome."""
    if json_out:
        typer.echo(json.dumps({"action": recovery.action, "journal": str(recovery.journal)}))
    elif recovery.action == "none":
        _out.print(f"nothing to recover: {escape(str(recovery.journal))}", soft_wrap=True)
    elif recovery.action == "rolled_back":
        _out.print(
            f"rolled back reconcile transaction: {escape(str(recovery.journal))}",
            soft_wrap=True,
        )
    else:
        _out.print(
            f"cleaned committed reconcile transaction: {escape(str(recovery.journal))}",
            soft_wrap=True,
        )


@app.command()
def reconcile(  # noqa: PLR0913
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
    json_out: JsonOpt = False,
) -> None:
    """Set seen to current upstream hashes for the selected edges.

    With --dry-run, computes and reports the same plan without writing anything.
    With --recover, performs only recovery or cleanup and never plans a batch.
    """
    if recover and (downstream_id or reconcile_all or ref is not None or dry_run):
        _err.print(
            "[red]error[/red]: --recover cannot be combined with a downstream id, "
            "--all, --ref, or --dry-run"
        )
        raise typer.Exit(2)
    if not recover and not reconcile_all and not downstream_id:
        _err.print("[red]error[/red]: provide a downstream id or --all")
        raise typer.Exit(2)
    with _exit_on_project_error():
        project = load_config(config, Path.cwd())

        if recover:
            with reconcile_lock(project.project_root) as lock:
                recovery = recover_transaction(project.project_root, lock=lock)
            _report_recovery(recovery, json_out=json_out)
            return

        with reconcile_lock(project.project_root) as lock:
            if dry_run:
                ensure_dry_run_safe(project.project_root)
            else:
                recovery = recover_transaction(project.project_root, lock=lock)
                if recovery.action != "none":
                    _err.print(f"recovered reconcile transaction: {recovery.action}")

            lattice = load_lattice(
                project,
                require_verified=True,
                persist_cache=not dry_run,
            )
            plan = plan_reconcile(lattice, downstream_id, ref=ref, reconcile_all=reconcile_all)
            write_paths = _resolve_reconcile_write_paths(plan, project.project_root)
            rewrites = plan_rewrites(plan, lambda path: write_paths[path].read_bytes())
            if not dry_run and rewrites:
                commit_rewrites(
                    project.project_root,
                    rewrites,
                    write_paths,
                    lock=lock,
                )
        _report_reconcile(plan, rewrites, dry_run=dry_run, json_out=json_out)


@app.command()
def graph(
    fmt: Annotated[str, typer.Option("--format", help="mermaid, dot, or json.")] = "mermaid",
    config: ConfigOpt = None,
) -> None:
    """Emit the edge graph as Mermaid, DOT, or JSON."""
    if fmt not in VALID_GRAPH_FORMATS:
        _reject_bad_format(fmt, VALID_GRAPH_FORMATS)
    with _exit_on_project_error():
        lattice = _load(config)
        stale = {
            (s.source_id, s.target_id)
            for s in check_lattice(lattice)
            if s.state == "STALE" and s.target_id is not None
        }
    if fmt == "json":
        typer.echo(json.dumps(to_json(lattice, stale)))
    elif fmt == "dot":
        typer.echo(to_dot(lattice, stale), nl=False)
    else:
        typer.echo(to_mermaid(lattice, stale), nl=False)


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
    indent: IndentOpt = None,
) -> None:
    """Report tickets shipped against a spec that has since drifted.

    Exits 0 unless --exit-code is passed, which gates CI on any DANGER or BLOCKED
    finding; add --warn-exit to also gate on WARNING. Tool errors always exit 2.
    """
    _validate_indent(indent, json_out=json_out)
    if from_id is not None and target:
        _err.print("[red]error[/red]: pass a positional target or --from, not both")
        raise typer.Exit(2)
    with _exit_on_project_error():
        project = load_config(config, Path.cwd())
        lattice = load_lattice(project)
        if from_id is not None:
            trigger = build_from_trigger(lattice, from_id)
        else:
            trigger = build_audit_trigger(lattice, target or None)
        refs = {ref for node_id in trigger for ref in lattice.nodes_by_id[node_id].tickets}
        tickets, rejected = fetch_tickets(refs, project.config.linear_team)
        findings = stale_shipped(lattice, trigger, tickets, rejected)
    if json_out:
        typer.echo(json.dumps(findings_json(findings), indent=indent))
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
    """Scaffold .doc-lattice.yml and print ignore, pre-commit, and CI guidance."""
    with _exit_on_project_error():
        roots = tuple(docs_root) if docs_root else ("docs",)
        _validate_init_flags(roots, linear_team)
        scaffold = build_scaffold(roots, linear_team, __version__)
        target = Path.cwd() / DEFAULT_CONFIG_NAME
        try:
            atomic_create_bytes(
                target,
                scaffold.config_text.encode("utf-8"),
                prefix=f"{target.name}.",
            )
        except FileExistsError as exc:
            if not getattr(exc, "__notes__", ()):
                _err.print(f"{escape(target.name)} already exists, leaving it untouched")
            else:
                error = ConfigError(f"cannot write {target.name}: {exc}")
                copy_exception_notes(error, exc)
                raise error from exc
        except OSError as exc:
            error = ConfigError(f"cannot write {target.name}: {exc}")
            copy_exception_notes(error, exc)
            raise error from exc
        else:
            _err.print(f"wrote {escape(target.name)}")
        typer.echo("# ===== .gitignore (append these lines) =====")
        typer.echo(scaffold.gitignore_text)
        typer.echo("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
        typer.echo(scaffold.precommit_text)
        typer.echo("# ===== .github/workflows/doc-lattice.yml (new file) =====")
        typer.echo(scaffold.ci_text)
        _err.print(
            "Append the .gitignore block, add the pre-commit block under `repos:`, "
            "save the workflow as "
            ".github/workflows/doc-lattice.yml, and make sure the "
            f"exact pinned version {__version__} is published on PyPI so the "
            "snippets resolve."
        )
    raise typer.Exit(0)


def main() -> None:
    """Console-script entry point.

    Wraps ``app()`` so an unexpected filesystem or path error (for example a symlink
    loop surfacing as ``RuntimeError`` from ``Path.resolve()``) exits with the tool-error
    code 2 instead of Python's default 1, which ``check`` reserves to mean "drift
    detected". Intended exits raised by typer (``SystemExit``) propagate unchanged.
    """
    # Neutralize color before typer/click parse argv: --help and parameter-validation errors
    # (like a bad --indent) are rendered by typer's own rich_utils console before main_callback
    # runs, so _disable_color() never reaches them. Setting NO_COLOR handles Rich color, but
    # typer forces terminal styling whenever GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS is set (as CI
    # is), which keeps bold/dim escapes on even with NO_COLOR. Setting rich_utils.COLOR_SYSTEM to
    # None (its documented disable switch, read afresh by each _get_rich_console call) makes those
    # consoles emit plain text unconditionally, independent of the terminal-forcing env vars.
    # Honor the --no-color flag and the documented NO_COLOR variable alike, using the no-color.org
    # rule (present and non-empty) that rich itself applies to our module-level consoles.
    if "--no-color" in sys.argv[1:] or os.environ.get("NO_COLOR", "") != "":
        os.environ["NO_COLOR"] = "1"
        typer.rich_utils.COLOR_SYSTEM = None
    try:
        app()
    except ProjectError as exc:
        _print_project_error(exc)
        raise SystemExit(2) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        _err.print(f"[red]internal error[/red]: {type(exc).__name__}: {escape(str(exc))}")
        raise SystemExit(2) from exc
