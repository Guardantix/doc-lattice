"""Typer adapters for offline GitHub CI audit and managed refresh."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Annotated, TextIO

import typer
from rich.markup import escape

from ... import __version__
from ...error_types import ConfigError
from ...github_ci.audit import audit_global_workflows, audit_managed_installation
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
    validate_final_release_version,
)
from ...github_ci.render import render_managed_artifacts
from ..errors import EXIT_FINDING, exit_on_project_error
from ..runtime import CliRuntime, get_runtime

if TYPE_CHECKING:
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
            identity = _resolve_repository(runtime, repository)
            artifacts = render_managed_artifacts(identity.display, __version__)
            discovery = discover_workflows(runtime.cwd)
            installed = inspect_installed_artifacts(runtime.cwd, artifacts)
            findings = tuple(
                sorted(
                    set(
                        audit_global_workflows(discovery.documents)
                        + audit_managed_installation(
                            discovery,
                            installed,
                            identity,
                            __version__,
                        )
                    )
                )
            )
            if findings:
                for finding in findings:
                    runtime.write_stdout(f"{finding.path}: {finding.code}: {finding.message}")
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
            identity = parse_repository(repository)
            validate_final_release_version(__version__)
            artifacts = render_managed_artifacts(identity.display, __version__)
            changes = preflight_refresh(runtime.cwd, artifacts)
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
                        runtime,
                        artifacts,
                    )
                    repeated_diff = render_diff(repeated_changes)
                    if repeated_changes != changes or repeated_diff != diff:
                        raise ConfigError(
                            "managed artifacts changed after confirmation; "
                            "run a fresh preview before applying"
                        )
                    apply_changes(repeated_changes)
                    _verify_refresh_converged(runtime, artifacts)
        raise typer.Exit(exit_code)


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
    runtime: CliRuntime,
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
                "--get",
                "remote.origin.url",
            ],
            cwd=runtime.cwd,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("cannot resolve repository from git origin") from exc
    if completed.returncode != 0:
        raise ConfigError("cannot resolve repository from git origin")
    try:
        stdout = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("cannot decode repository from git origin as UTF-8") from exc
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise ConfigError("cannot resolve repository from git origin")
    return parse_origin_repository(lines[0])


def _repeat_refresh_preflight(
    runtime: CliRuntime,
    artifacts: tuple[ManagedArtifact, ...],
) -> tuple[ArtifactChange, ...]:
    """Repeat refresh preflight and replace races with an actionable diagnostic."""
    try:
        return preflight_refresh(runtime.cwd, artifacts)
    except ConfigError as exc:
        raise ConfigError(
            "managed artifacts changed after confirmation; run a fresh preview before applying"
        ) from exc


def _verify_refresh_converged(
    runtime: CliRuntime,
    artifacts: tuple[ManagedArtifact, ...],
) -> None:
    """Require every managed artifact to remain current after refresh writes."""
    try:
        installed = preflight_refresh(runtime.cwd, artifacts)
    except ConfigError as exc:
        raise ConfigError(
            "managed refresh did not converge; inspect installed artifacts and run a fresh preview"
        ) from exc
    if any(change.action != "current" for change in installed):
        raise ConfigError(
            "managed refresh did not converge; inspect installed artifacts and run a fresh preview"
        )
