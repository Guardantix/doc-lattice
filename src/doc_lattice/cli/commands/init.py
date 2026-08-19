"""Typer adapter for project scaffold initialization."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ... import __version__
from ...config import DEFAULT_CONFIG_NAME
from ...error_types import ConfigError, InitPersistenceError, copy_exception_notes
from ...linear_query import is_valid_team_key
from ...persistence import atomic_create_bytes
from ...scaffold import build_scaffold
from ...text_utils import strip_control_chars
from ..errors import exit_on_project_error
from ..git_repository import (
    DEFAULT_BRANCH_FALLBACK,
    probe_default_branch,
    validate_default_branch,
)
from ..runtime import CliRuntime, get_runtime

# Scoped to a first adoption on purpose: init is rerunnable against an existing config, and
# reconcile --all acknowledges every STALE and UNRECONCILED edge, so an unconditional instruction
# would tell an established adopter to erase legitimate drift. It deliberately stops short of
# promising green CI, because reconcile --all skips BROKEN edges and those remain findings.
# README.md owns this rule for users and states it at length; MANAGED_CI.md only sequences the
# command and links there. Tightening the rule means editing README.md and this string, and
# tests/cli/test_init.py holds the only mechanically enforced copy of the wording.
#
# Printed with soft_wrap=True so Rich does not insert hard newlines: default wrapping split
# `doc-lattice reconcile --all` across a real line break, which survives redirection and breaks
# the command on copy.
_BASELINE_GUIDANCE = (
    "For an initial adoption with no established baseline, run `doc-lattice reconcile --all` "
    "once after annotating documents and before enabling the gates. It acknowledges the "
    "current state so the gates start from a known baseline; BROKEN edges are skipped and "
    "remain findings, so this does not by itself make CI green."
)


# Narrated on stderr next to the generated workflow so a wrong probe result is visible when it
# happens, rather than buried in YAML nobody re-reads. The probe is a local hint that cannot
# detect an upstream rename whose old target still exists, so naming the source is what makes
# the difference between "detected" and "fell back" legible to an adopter.
_BRANCH_SOURCE_FLAG = "--default-branch"
_BRANCH_SOURCE_ORIGIN_HEAD = "origin/HEAD"
_BRANCH_SOURCE_FALLBACK = "fallback"


def _resolve_default_branch(default_branch: str | None, cwd: Path) -> tuple[str, str]:
    """Resolve the ordinary workflow's trigger branch and record where it came from.

    Precedence is deterministic: an explicit ``--default-branch`` wins, otherwise the local
    ``origin/HEAD`` probe, otherwise the fixed fallback. Discovery failure and policy rejection
    stay separate. A probe that finds nothing degrades to the fallback and does not error, which
    is what keeps ordinary ``init`` runnable with no remote and no Git at all; a name that was
    actually supplied or discovered and then fails the branch policy raises instead, because
    rendering it would produce a workflow whose filter is wrong or is a pattern.

    Args:
        default_branch: The explicit flag value, or None when it was not passed.
        cwd: Invocation directory the probe reads Git state from.

    Returns:
        The validated branch name and the source label to narrate.

    Raises:
        ConfigError: If a supplied or discovered name is outside the supported domain.
    """
    if default_branch is not None:
        return validate_default_branch(default_branch), _BRANCH_SOURCE_FLAG
    probed = probe_default_branch(cwd)
    if probed is not None:
        return validate_default_branch(probed), _BRANCH_SOURCE_ORIGIN_HEAD
    return DEFAULT_BRANCH_FALLBACK, _BRANCH_SOURCE_FALLBACK


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


def _print_unmanaged_guidance(runtime: CliRuntime, ci_text: str) -> None:
    """Print the ordinary workflow and the instructions for placing every printed block."""
    runtime.write_stdout("# ===== .github/workflows/doc-lattice.yml (new file) =====")
    runtime.write_stdout(ci_text)
    runtime.stderr.print(
        "Append the .gitignore block, add the pre-commit block under `repos:`, "
        "save the workflow as "
        ".github/workflows/doc-lattice.yml, and make sure the "
        f"exact pinned version {__version__} is published on PyPI so the "
        "snippets resolve."
    )
    runtime.stderr.print(_BASELINE_GUIDANCE, soft_wrap=True)


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
        default_branch: Annotated[
            str | None,
            typer.Option(
                "--default-branch",
                help=(
                    "Branch the printed workflow triggers on. Defaults to the local "
                    f"origin/HEAD, then {DEFAULT_BRANCH_FALLBACK}."
                ),
            ),
        ] = None,
    ) -> None:
        """Scaffold .doc-lattice.yml and print ignore, pre-commit, and CI guidance."""
        runtime = get_runtime(ctx)
        with exit_on_project_error(runtime):
            roots = tuple(docs_root) if docs_root else ("docs",)
            _validate_init_flags(roots, linear_team)
            branch, branch_source = _resolve_default_branch(default_branch, runtime.cwd)
            scaffold = build_scaffold(roots, linear_team, __version__, default_branch=branch)
            target = runtime.cwd / DEFAULT_CONFIG_NAME
            try:
                atomic_create_bytes(
                    target,
                    scaffold.config_text.encode("utf-8"),
                    prefix=f"{target.name}.",
                )
            # A bare FileExistsError means the destination already existed and the staged file
            # was cleaned up normally, which is the benign already-exists case. Notes are
            # attached only when that cleanup also failed and left stray staged evidence, so
            # treat a noted error as a real failure. Both abnormal branches carry
            # INIT_PERSISTENCE rather than CONFIG_ERROR: the defect is in the directory being
            # scaffolded, and init never reads .doc-lattice.yml, so naming config sent the user
            # to a file that had nothing to do with the failure.
            except FileExistsError as exc:
                if not getattr(exc, "__notes__", ()):
                    runtime.stderr.print(
                        f"{escape(target.name)} already exists, leaving it untouched"
                    )
                else:
                    error = InitPersistenceError(f"cannot write {target.name}: {exc}")
                    copy_exception_notes(error, exc)
                    raise error from exc
            except OSError as exc:
                error = InitPersistenceError(f"cannot write {target.name}: {exc}")
                copy_exception_notes(error, exc)
                raise error from exc
            else:
                runtime.stderr.print(f"wrote {escape(target.name)}")
            runtime.stderr.print(f"workflow triggers on branch {escape(branch)} ({branch_source})")
            runtime.write_stdout("# ===== .gitignore (append these lines) =====")
            runtime.write_stdout(scaffold.gitignore_text)
            runtime.write_stdout("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
            runtime.write_stdout(scaffold.precommit_text)
            _print_unmanaged_guidance(runtime, scaffold.ci_text)
        raise typer.Exit(0)
