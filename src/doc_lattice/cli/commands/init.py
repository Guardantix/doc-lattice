"""Typer adapter for project scaffold initialization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from ... import __version__
from ...config import DEFAULT_CONFIG_NAME
from ...error_types import ConfigError, copy_exception_notes
from ...github_ci.filesystem import apply_changes, preflight_create
from ...github_ci.identity import parse_repository
from ...github_ci.render import render_managed_artifacts
from ...linear_query import is_valid_team_key
from ...persistence import atomic_create_bytes
from ...scaffold import build_scaffold
from ...text_utils import strip_control_chars
from ..errors import exit_on_project_error
from ..runtime import get_runtime

if TYPE_CHECKING:
    from ...github_ci.model import ArtifactChange


@dataclass(frozen=True, slots=True)
class _GithubInitPlan:
    """Preflighted inputs for explicit managed GitHub artifact creation."""

    repository: str
    changes: tuple[ArtifactChange, ...]


def _validate_github_options(github: bool, repository: str | None) -> str | None:
    """Validate explicit GitHub option pairing and return the required identity."""
    if github:
        if repository is None:
            raise ConfigError("--repository is required with --github")
        return repository
    if repository is not None:
        raise ConfigError("--repository requires --github")
    return None


def _validate_init_flags(docs_roots: tuple[str, ...], linear_team: str | None) -> None:
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


def _prepare_github_init(root: Path, repository: str) -> _GithubInitPlan:
    """Validate and preflight explicit GitHub artifact initialization.

    The renderer validates the pinned final-release version internally, so no separate
    caller-side version check is needed. The full preflight result is stored unfiltered
    because ``apply_changes`` skips already-current artifacts on its own.
    """
    identity = parse_repository(repository)
    artifacts = render_managed_artifacts(identity.display, __version__)
    changes = preflight_create(root, artifacts)
    return _GithubInitPlan(repository=identity.display, changes=changes)


def register_init(app: typer.Typer) -> None:
    """Register the ``init`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def init(
        ctx: typer.Context,
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
        github: Annotated[
            bool,
            typer.Option(
                "--github",
                help="Create managed GitHub Actions and bootstrap artifacts.",
            ),
        ] = False,
        repository: Annotated[
            str | None,
            typer.Option(
                "--repository",
                help="Exact GitHub OWNER/REPO for generated guards.",
            ),
        ] = None,
    ) -> None:
        """Scaffold .doc-lattice.yml and print ignore, pre-commit, and CI guidance."""
        runtime = get_runtime(ctx)
        with exit_on_project_error(runtime):
            github_repository = _validate_github_options(github, repository)
            roots = tuple(docs_root) if docs_root else ("docs",)
            _validate_init_flags(roots, linear_team)
            github_plan = None
            if github_repository is not None:
                github_plan = _prepare_github_init(runtime.cwd, github_repository)
            scaffold = build_scaffold(roots, linear_team, __version__)
            target = runtime.cwd / DEFAULT_CONFIG_NAME
            try:
                atomic_create_bytes(
                    target,
                    scaffold.config_text.encode("utf-8"),
                    prefix=f"{target.name}.",
                )
            except FileExistsError as exc:
                if not getattr(exc, "__notes__", ()):
                    runtime.stderr.print(
                        f"{escape(target.name)} already exists, leaving it untouched"
                    )
                else:
                    error = ConfigError(f"cannot write {target.name}: {exc}")
                    copy_exception_notes(error, exc)
                    raise error from exc
            except OSError as exc:
                error = ConfigError(f"cannot write {target.name}: {exc}")
                copy_exception_notes(error, exc)
                raise error from exc
            else:
                runtime.stderr.print(f"wrote {escape(target.name)}")
            if github_plan is not None:
                apply_changes(github_plan.changes)
            runtime.write_stdout("# ===== .gitignore (append these lines) =====")
            runtime.write_stdout(scaffold.gitignore_text)
            runtime.write_stdout("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
            runtime.write_stdout(scaffold.precommit_text)
            if github_plan is None:
                runtime.write_stdout("# ===== .github/workflows/doc-lattice.yml (new file) =====")
                runtime.write_stdout(scaffold.ci_text)
                runtime.stderr.print(
                    "Append the .gitignore block, add the pre-commit block under `repos:`, "
                    "save the workflow as "
                    ".github/workflows/doc-lattice.yml, and make sure the "
                    f"exact pinned version {__version__} is published on PyPI so the "
                    "snippets resolve."
                )
            else:
                offline_path, linear_path, bootstrap_path = (
                    escape(change.artifact.relative_path.as_posix())
                    for change in github_plan.changes
                )
                runtime.stderr.print(
                    "Append the .gitignore block and add the pre-commit block under `repos:`. "
                    f"Review {offline_path}, {linear_path}, and "
                    f"{bootstrap_path} before enabling or running them, and make sure "
                    f"the exact pinned version {__version__} is published on PyPI so the "
                    "generated workflows resolve."
                )
                runtime.stderr.print(
                    f"bash {bootstrap_path} plan {escape(github_plan.repository)}",
                    soft_wrap=True,
                )
        raise typer.Exit(0)
