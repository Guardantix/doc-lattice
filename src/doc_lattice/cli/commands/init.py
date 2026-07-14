"""Typer adapter for project scaffold initialization."""

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ... import __version__
from ...config import DEFAULT_CONFIG_NAME
from ...error_types import ConfigError, copy_exception_notes
from ...linear_query import is_valid_team_key
from ...persistence import atomic_create_bytes
from ...scaffold import build_scaffold
from ...text_utils import strip_control_chars
from ..errors import exit_on_project_error
from ..runtime import get_runtime


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
    ) -> None:
        """Scaffold .doc-lattice.yml and print ignore, pre-commit, and CI guidance."""
        runtime = get_runtime(ctx)
        with exit_on_project_error(runtime):
            roots = tuple(docs_root) if docs_root else ("docs",)
            _validate_init_flags(roots, linear_team)
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
            runtime.write_stdout("# ===== .gitignore (append these lines) =====")
            runtime.write_stdout(scaffold.gitignore_text)
            runtime.write_stdout("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
            runtime.write_stdout(scaffold.precommit_text)
            runtime.write_stdout("# ===== .github/workflows/doc-lattice.yml (new file) =====")
            runtime.write_stdout(scaffold.ci_text)
            runtime.stderr.print(
                "Append the .gitignore block, add the pre-commit block under `repos:`, "
                "save the workflow as "
                ".github/workflows/doc-lattice.yml, and make sure the "
                f"exact pinned version {__version__} is published on PyPI so the "
                "snippets resolve."
            )
        raise typer.Exit(0)
