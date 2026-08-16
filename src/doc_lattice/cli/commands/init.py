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
from ..git_repository import (
    DEFAULT_BRANCH_FALLBACK,
    probe_default_branch,
    resolve_git_repository_root,
    validate_default_branch,
)
from ..runtime import CliRuntime, get_runtime

if TYPE_CHECKING:
    from ...github_ci.model import ArtifactChange


# Printed by both init branches, so the managed and unmanaged paths cannot drift. Scoped to a
# first adoption on purpose: init is rerunnable against an existing config, and reconcile --all
# acknowledges every STALE and UNRECONCILED edge, so an unconditional instruction would tell an
# established adopter to erase legitimate drift. It deliberately stops short of promising green
# CI, because reconcile --all skips BROKEN edges and those remain findings. README.md owns this
# rule for users and states it at length; MANAGED_CI.md only sequences the command and links
# there. Tightening the rule means editing README.md and this string, and tests/cli/test_init.py
# holds the only mechanically enforced copy of the wording.
#
# Printed with soft_wrap=True so Rich does not insert hard newlines: default wrapping split
# `doc-lattice reconcile --all` across a real line break, which survives redirection and breaks
# the command on copy. Same per-site opt-in the bootstrap command line below already uses.
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


@dataclass(frozen=True, slots=True)
class _GithubInitPlan:
    """Preflighted inputs for explicit managed GitHub artifact creation."""

    repository: str
    changes: tuple[ArtifactChange, ...]


def _validate_github_options(
    github: bool,
    repository: str | None,
    default_branch: str | None,
) -> str | None:
    """Validate explicit GitHub option pairing and return the required identity."""
    if github:
        if repository is None:
            raise ConfigError("--repository is required with --github")
        if default_branch is not None:
            # The branch named here is the one github_ci/render.py hard-wires, not the ordinary
            # fallback. They spell the same word today, but interpolating DEFAULT_BRANCH_FALLBACK
            # is exactly the coupling that constant's own comment forbids: changing the ordinary
            # fallback would silently rewrite this description of the managed security control.
            raise ConfigError(
                "--default-branch cannot be combined with --github: every managed artifact is "
                "pinned to the exact main branch as a security control, so the flag would have "
                "no effect. Run init without --github to generate an ordinary workflow for "
                "another branch."
            )
        return repository
    if repository is not None:
        raise ConfigError("--repository requires --github")
    return None


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


def _prepare_github_init(root: Path, repository: str) -> _GithubInitPlan:
    """Validate and preflight explicit GitHub artifact initialization.

    The renderer validates the pinned final-release version internally, so no separate
    caller-side version check is needed. The full preflight result is stored unfiltered
    because ``apply_changes`` re-validates every already-current artifact under the
    publication lock whenever the batch has anything to write, and refuses when one
    drifted, so filtering them out here would drop that check.
    """
    identity = parse_repository(repository)
    artifacts = render_managed_artifacts(identity.display, __version__)
    changes = preflight_create(root, artifacts)
    return _GithubInitPlan(repository=identity.display, changes=changes)


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


def _print_managed_guidance(runtime: CliRuntime, plan: _GithubInitPlan) -> None:
    """Print review instructions and the bootstrap command for the created managed artifacts."""
    offline_path, linear_path, bootstrap_path, attributes_path = (
        escape(change.artifact.relative_path.as_posix()) for change in plan.changes
    )
    runtime.stderr.print(
        "Append the .gitignore block and add the pre-commit block under `repos:`. "
        f"Review {offline_path}, {linear_path}, and "
        f"{bootstrap_path}, plus {attributes_path}, before enabling or running them, "
        "and make sure "
        f"the exact pinned version {__version__} is published on PyPI so the "
        "generated workflows resolve."
    )
    runtime.stderr.print(_BASELINE_GUIDANCE, soft_wrap=True)
    runtime.stderr.print(
        f"bash {bootstrap_path} plan {escape(plan.repository)}",
        soft_wrap=True,
    )


def register_init(app: typer.Typer) -> None:
    """Register the ``init`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def init(  # noqa: PLR0913
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
        default_branch: Annotated[
            str | None,
            typer.Option(
                "--default-branch",
                help=(
                    "Branch the printed workflow triggers on. Defaults to the local "
                    f"origin/HEAD, then {DEFAULT_BRANCH_FALLBACK}. Rejected with --github."
                ),
            ),
        ] = None,
    ) -> None:
        """Scaffold .doc-lattice.yml and print ignore, pre-commit, and CI guidance."""
        runtime = get_runtime(ctx)
        with exit_on_project_error(runtime):
            github_repository = _validate_github_options(github, repository, default_branch)
            roots = tuple(docs_root) if docs_root else ("docs",)
            _validate_init_flags(roots, linear_team)
            github_plan = None
            root = runtime.cwd
            # Managed mode never probes: its artifacts are pinned to the exact fallback branch
            # by the security control MANAGED_CI.md describes, and probing there would couple
            # the managed path to unreliable local Git state for no gain. The value below only
            # reaches ci_text, which managed mode builds and never prints.
            branch, branch_source = DEFAULT_BRANCH_FALLBACK, _BRANCH_SOURCE_FALLBACK
            if github_repository is not None:
                root = resolve_git_repository_root(runtime.cwd)
                github_plan = _prepare_github_init(root, github_repository)
            else:
                branch, branch_source = _resolve_default_branch(default_branch, runtime.cwd)
            scaffold = build_scaffold(roots, linear_team, __version__, default_branch=branch)
            target = root / DEFAULT_CONFIG_NAME
            try:
                atomic_create_bytes(
                    target,
                    scaffold.config_text.encode("utf-8"),
                    prefix=f"{target.name}.",
                )
            # A bare FileExistsError means the destination already existed and the staged file
            # was cleaned up normally, which is the benign already-exists case. Notes are
            # attached only when that cleanup also failed and left stray staged evidence, so
            # treat a noted error as a real failure.
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
            else:
                runtime.stderr.print(
                    f"workflow triggers on branch {escape(branch)} ({branch_source})"
                )
            runtime.write_stdout("# ===== .gitignore (append these lines) =====")
            runtime.write_stdout(scaffold.gitignore_text)
            runtime.write_stdout("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
            runtime.write_stdout(scaffold.precommit_text)
            if github_plan is None:
                _print_unmanaged_guidance(runtime, scaffold.ci_text)
            else:
                _print_managed_guidance(runtime, github_plan)
        raise typer.Exit(0)
