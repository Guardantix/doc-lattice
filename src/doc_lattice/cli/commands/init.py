"""Typer adapter for project scaffold initialization."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ... import __version__
from ...config import DEFAULT_CONFIG_NAME
from ...error_types import InitPersistenceError, ValidationError, copy_exception_notes
from ...linear_query import is_valid_team_key
from ...path_utils import format_path_for_display
from ...persistence import atomic_create_bytes
from ...scaffold import build_scaffold, render_ci, render_gitignore, render_precommit
from ...text_utils import strip_control_chars
from ..errors import EXIT_TOOL_ERROR, exit_on_project_error
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

# The entry that ends the ancestor-config walk, whether it is a directory or the regular file a
# linked worktree and a submodule checkout carry. It is a filesystem marker and not a Git query;
# `_find_ancestor_config` records why, and what that costs.
_REPOSITORY_MARKER = ".git"


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
        ValidationError: If a supplied or discovered name is outside the supported domain. Both
            sources report the same code deliberately: a rejected ``origin/HEAD`` target is not a
            command-line value, but it is still an input this run validated, and ``init`` has no
            config file to blame for either.
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
            raise ValidationError(msg)
    for root in docs_roots:
        if Path(root).is_absolute() or ".." in Path(root).parts:
            # The recorded flag string is handed to the helper as text. Path(root) would
            # normalize away a doubled separator, a trailing separator, and a leading "./",
            # and a diagnostic that rejects a value has to show the value it rejected. The
            # loop above has already refused every control-bearing root, so this reaches no
            # control byte today; it goes through the helper so the spelling is centralized
            # and statically visible rather than correct by coincidence of `!r`.
            msg = (
                f"--docs-root {format_path_for_display(root)} must be a relative path inside "
                "the project, without '..' or a leading slash"
            )
            raise ValidationError(msg)
    if linear_team is not None and not is_valid_team_key(linear_team):
        msg = (
            f"--linear-team {linear_team!r} must be a Linear team key: uppercase letters "
            "and digits, starting with a letter, for example ENG. The linear command "
            "rejects any other value."
        )
        raise ValidationError(msg)


def _init_persistence_error(target_name: str, cause: OSError) -> InitPersistenceError:
    """Wrap one scaffold write failure, preserving the low-level remediation notes."""
    error = InitPersistenceError(f"cannot write {format_path_for_display(target_name)}: {cause}")
    copy_exception_notes(error, cause)
    return error


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


def _print_artifacts(
    runtime: CliRuntime, gitignore_text: str, precommit_text: str, ci_text: str
) -> None:
    """Print the three hand-installed blocks in their fixed order.

    The one printing site both modes reach, so block order and the headers around them cannot
    differ between an ordinary run and ``--print-only``. What the two modes still choose
    independently is where those three texts came from, which is why ``tests/cli/test_init.py``
    pins their exact bytes against each other rather than trusting this function alone.

    Args:
        runtime: The invocation's output streams.
        gitignore_text: The ignore patterns block.
        precommit_text: The pre-commit hooks block.
        ci_text: The ordinary GitHub Actions workflow.
    """
    runtime.write_stdout("# ===== .gitignore (append these lines) =====")
    runtime.write_stdout(gitignore_text)
    runtime.write_stdout("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
    runtime.write_stdout(precommit_text)
    _print_unmanaged_guidance(runtime, ci_text)


def _find_ancestor_config(cwd: Path) -> Path | None:
    """Find the configuration an ancestor directory already holds, bounded by the repository.

    ``init`` writes into the invocation directory, and every lattice-loading command selects a
    default config from *its* invocation directory, so a run from a subdirectory of a configured
    repository would otherwise scaffold a second, nested config that the next ``check`` never
    reads. This is the detection half of the refusal that replaced that write.

    The walk is bounded by the nearest ``.git`` entry, inclusive, and yields nothing at all when
    no such entry is found, which is why the nearest config is held rather than returned on
    sight: whether it counts is not known until a boundary is. Both bounds are deliberate.
    Scanning to the filesystem root would let one stray config in a home directory refuse every
    new project created under it, and stopping at the first marker means a nested repository or
    submodule beneath a configured root is its own scope and scaffolds normally. The marker is
    tested for existence rather than for being a directory, because a linked worktree and a
    submodule checkout both carry ``.git`` as a regular file and an ``is_dir`` test would walk
    straight past their roots.

    This is a filesystem walk and not a Git query on purpose. ``init`` has no Git prerequisite,
    and resolving the boundary through Git would re-create the resolver AD-32 retired and make
    the refusal depend on an executable. One consequence is stated rather than fixed: Git's own
    discovery honors ``GIT_DIR``, ``GIT_CEILING_DIRECTORIES``, and
    ``GIT_DISCOVERY_ACROSS_FILESYSTEM``, so under those settings the default-branch probe and
    this walk can disagree about where the repository begins. The cost is bounded at one refusal
    too many or one too few, never a write in the wrong place, because this boundary only bounds
    a refusal and never selects a destination.

    Args:
        cwd: The invocation directory, whose own config the caller has already ruled out.

    Returns:
        The nearest ancestor's configuration file when the walk reached a repository boundary,
        and None otherwise, which covers both a boundary holding no config and no boundary at
        all.
    """
    if (cwd / _REPOSITORY_MARKER).exists():
        return None
    nearest: Path | None = None
    for ancestor in cwd.parents:
        candidate = ancestor / DEFAULT_CONFIG_NAME
        if nearest is None and candidate.exists():
            nearest = candidate
        if (ancestor / _REPOSITORY_MARKER).exists():
            return nearest
    return None


def _nested_scaffold_error(ancestor: Path) -> ValidationError:
    """Build the refusal for a run that would scaffold a config beneath an existing one."""
    return ValidationError(
        f"{format_path_for_display(ancestor)} already configures this repository, and init "
        "writes only into the current directory, so scaffolding here would leave a second, "
        "nested configuration that check, lint, reconcile, and the rest never read. Run init "
        "from that directory instead, pass --print-only to obtain the snippets without writing "
        f"anything, or write {DEFAULT_CONFIG_NAME} here by hand if a nested lattice is what you "
        "intend."
    )


def _scaffold_config(runtime: CliRuntime, config_text: str) -> None:
    """Create the config in the invocation directory, or report why it was not written.

    Args:
        runtime: The invocation's working directory and output streams.
        config_text: The rendered configuration file.

    Raises:
        ValidationError: If the directory holds no config of its own but an ancestor inside the
            same repository does. It is checked here, with the other inputs ``init`` refuses
            before writing anything, because the directory is the input in question.
        InitPersistenceError: If the file could not be written, or a failed staging cleanup left
            an orphan behind.
    """
    target = runtime.cwd / DEFAULT_CONFIG_NAME
    if not target.exists():
        ancestor = _find_ancestor_config(runtime.cwd)
        if ancestor is not None:
            raise _nested_scaffold_error(ancestor)
    try:
        atomic_create_bytes(
            target,
            config_text.encode("utf-8"),
            # The staged filename, not text: spelled from the constant `target` was
            # built from rather than from `target.name`, so this machine construction
            # needs no exemption shared with the human messages below. Display
            # exemptions are keyed by (module, function, expression), so one written
            # for this line would have covered those two sinks as well.
            prefix=f"{DEFAULT_CONFIG_NAME}.",
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
                f"{escape(format_path_for_display(target.name))} already exists, "
                "leaving it untouched"
            )
        else:
            raise _init_persistence_error(target.name, exc) from exc
    except OSError as exc:
        raise _init_persistence_error(target.name, exc) from exc
    else:
        runtime.stderr.print(f"wrote {escape(format_path_for_display(target.name))}")


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
        print_only: Annotated[
            bool,
            typer.Option(
                "--print-only",
                help="Print the three blocks and write nothing. Rejects the config-only flags.",
            ),
        ] = False,
    ) -> None:
        """Scaffold .doc-lattice.yml and print ignore, pre-commit, and CI guidance."""
        runtime = get_runtime(ctx)
        # A flag combination that has no meaning, rather than a value that failed validation, so
        # it takes the uncoded usage-error path reconcile's own incompatible-flag check uses.
        # Both flags feed only the config renderer, and --print-only renders no config, so
        # accepting them silently would report success for a request nothing acted on.
        if print_only and (docs_root or linear_team is not None):
            runtime.stderr.print(
                "[red]error[/red]: --print-only cannot be combined with --docs-root or "
                "--linear-team"
            )
            raise typer.Exit(EXIT_TOOL_ERROR)
        with exit_on_project_error(runtime):
            if print_only:
                # Deliberately ahead of every config concern. This mode exists so an adopter can
                # obtain the snippets from a directory where writing is refused, so it neither
                # renders nor validates configuration text, and it runs no ancestor guard. Only
                # --default-branch has an effect here, resolved exactly as the ordinary path
                # resolves it.
                branch, branch_source = _resolve_default_branch(default_branch, runtime.cwd)
                gitignore_text = render_gitignore()
                precommit_text = render_precommit(__version__)
                ci_text = render_ci(__version__, default_branch=branch)
            else:
                roots = tuple(docs_root) if docs_root else ("docs",)
                _validate_init_flags(roots, linear_team)
                branch, branch_source = _resolve_default_branch(default_branch, runtime.cwd)
                scaffold = build_scaffold(roots, linear_team, __version__, default_branch=branch)
                _scaffold_config(runtime, scaffold.config_text)
                gitignore_text = scaffold.gitignore_text
                precommit_text = scaffold.precommit_text
                ci_text = scaffold.ci_text
            runtime.stderr.print(f"workflow triggers on branch {escape(branch)} ({branch_source})")
            _print_artifacts(runtime, gitignore_text, precommit_text, ci_text)
        raise typer.Exit(0)
