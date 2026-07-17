"""Typer adapters for offline GitHub CI audit and managed refresh."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Annotated, TextIO

import typer
from rich.markup import escape

from ... import __version__
from ...error_types import ConfigError, copy_exception_notes
from ...github_ci.audit import audit_repository
from ...github_ci.filesystem import (
    apply_changes,
    discover_workflows,
    inspect_installed_artifacts,
    preflight_refresh,
    render_diff,
)
from ...github_ci.identity import (
    parse_origin_repository,
    parse_repository,
)
from ...github_ci.path_display import display_path
from ...github_ci.render import CANONICAL_ARTIFACT_TARGETS, render_managed_artifacts
from ..errors import EXIT_FINDING, exit_on_project_error
from ..git_repository import resolve_git_repository_root
from ..runtime import CliRuntime, get_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from ...github_ci.model import ArtifactChange, ManagedArtifact, RepositoryIdentity

_GIT_TIMEOUT_SECONDS = 5


def register_ci(app: typer.Typer) -> None:
    """Register offline GitHub CI audit and managed refresh commands."""
    ci_app = typer.Typer(no_args_is_help=True)
    app.add_typer(
        ci_app,
        name="ci",
        help="Audit or refresh managed GitHub CI artifacts.",
    )

    @ci_app.command()
    def audit(
        ctx: typer.Context,
        repository: Annotated[
            str | None,
            typer.Option(
                "--repository",
                help="GitHub OWNER/REPO; defaults to the local origin.",
            ),
        ] = None,
    ) -> None:
        """Audit repository workflows and the managed GitHub CI installation."""
        runtime = get_runtime(ctx)
        exit_code = 0
        with exit_on_project_error(runtime):
            root = resolve_git_repository_root(runtime.cwd)
            identity = _resolve_repository(root, repository)
            discovery = discover_workflows(root)
            installed = inspect_installed_artifacts(root, CANONICAL_ARTIFACT_TARGETS)
            findings = audit_repository(discovery, installed, identity, __version__)
            if findings:
                for finding in findings:
                    display_path = _display_finding_path(finding.path)
                    runtime.write_stdout(f"{display_path}: {finding.code}: {finding.message}")
                exit_code = EXIT_FINDING
            else:
                runtime.write_stdout("doc-lattice ci audit: ok")
        raise typer.Exit(exit_code)

    @ci_app.command()
    def refresh(
        ctx: typer.Context,
        repository: Annotated[
            str,
            typer.Option(
                "--repository",
                help="Exact GitHub OWNER/REPO for regenerated guards.",
            ),
        ],
        apply: Annotated[
            bool,
            typer.Option(
                "--apply",
                help="Apply the preview after exact interactive confirmation.",
            ),
        ] = False,
    ) -> None:
        """Preview or interactively apply a managed GitHub CI artifact refresh."""
        runtime = get_runtime(ctx)
        exit_code = 0
        with exit_on_project_error(runtime):
            root = resolve_git_repository_root(runtime.cwd)
            identity = parse_repository(repository)
            artifacts = render_managed_artifacts(identity.display, __version__)
            changes = preflight_refresh(root, artifacts)
            diff = render_diff(changes)
            if not diff:
                runtime.write_stdout("doc-lattice ci refresh: current")
            else:
                runtime.write_stdout(diff, newline=False)
                if not apply:
                    exit_code = EXIT_FINDING
                else:
                    require_repository_confirmation(
                        typer.get_text_stream("stdin"),
                        runtime,
                        identity.display,
                    )
                    repeated_changes = _repeat_refresh_preflight(
                        root,
                        artifacts,
                    )
                    if repeated_changes != changes:
                        raise ConfigError(
                            "managed artifacts changed after confirmation; "
                            "run a fresh preview before applying"
                        )
                    apply_changes(repeated_changes)
                    _verify_refresh_converged(root, artifacts)
        raise typer.Exit(exit_code)


def _display_finding_path(path: str) -> str:
    """Escape repository-controlled path text without changing ordinary path output."""
    return display_path(path)


def require_repository_confirmation(stream: TextIO, runtime: CliRuntime, repository: str) -> None:
    """Require exact repository text from an attached stdin TTY."""
    if not stream.isatty():
        raise ConfigError("ci refresh --apply requires an interactive TTY on stdin")
    runtime.stderr.print(
        f"Type {escape(repository)} to apply managed refresh:",
        end=" ",
    )
    answer = stream.readline()
    if answer == "":
        raise ConfigError("refresh confirmation ended before a repository was entered")
    if answer.removesuffix("\n") != repository:
        raise ConfigError("refresh confirmation did not match the requested repository")


def _resolve_repository(
    root: Path,
    repository: str | None,
) -> RepositoryIdentity:
    """Resolve an explicit repository or one supported local Git origin."""
    if repository is not None:
        return parse_repository(repository)
    try:
        completed = subprocess.run(
            [  # noqa: S607 - git is intentionally resolved from the maintainer's PATH
                "git",
                "config",
                "--local",
                "--get-all",
                "remote.origin.url",
            ],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            "git executable not found; install Git or pass --repository OWNER/REPO"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("cannot resolve repository from git origin") from exc
    if completed.returncode != 0:
        raise ConfigError("cannot resolve repository from git origin")
    try:
        stdout = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("cannot decode repository from git origin as UTF-8") from exc
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise ConfigError("cannot resolve repository from git origin")
    return parse_origin_repository(lines[0])


def _repeat_refresh_preflight(
    root: Path,
    artifacts: tuple[ManagedArtifact, ...],
) -> tuple[ArtifactChange, ...]:
    """Repeat refresh preflight and replace races with an actionable diagnostic."""
    try:
        return preflight_refresh(root, artifacts)
    except ConfigError as exc:
        error = ConfigError(
            "managed artifacts changed after confirmation; run a fresh preview before applying"
        )
        copy_exception_notes(error, exc)
        error.add_note(str(exc))
        raise error from exc


def _verify_refresh_converged(
    root: Path,
    artifacts: tuple[ManagedArtifact, ...],
) -> None:
    """Require every managed artifact to remain current after refresh writes."""
    try:
        installed = preflight_refresh(root, artifacts)
    except ConfigError as exc:
        error = ConfigError(
            "managed refresh did not converge; inspect installed artifacts and run a fresh preview"
        )
        copy_exception_notes(error, exc)
        error.add_note(str(exc))
        raise error from exc
    if any(change.action != "current" for change in installed):
        raise ConfigError(
            "managed refresh did not converge; inspect installed artifacts and run a fresh preview"
        )
