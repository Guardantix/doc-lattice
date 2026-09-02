"""Typer adapter for project scaffold initialization."""

from __future__ import annotations

import errno
import stat
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ... import __version__
from ...config import DEFAULT_CONFIG_NAME, declares_lattice_format
from ...constants import LATTICE_FORMAT_VERSION
from ...error_types import InitPersistenceError, ValidationError, copy_exception_notes
from ...linear_query import is_valid_team_key
from ...link_selectors import validate_link_selector
from ...path_utils import format_path_for_display, safe_resolve
from ...persistence import DestinationExistsError, atomic_create_bytes
from ...scaffold import (
    RootKind,
    build_scaffold,
    render_ci,
    render_gitignore,
    render_precommit,
    selector_for_root,
)
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

# The referent for _BASELINE_GUIDANCE's "before enabling the gates". Without it the CLI asserted
# an ordering constraint while leaving the act it orders against undefined in its own output,
# which is the terminal-only half of the gap GTX-175 closed in README.md and MANAGED_CI.md.
#
# Placement and activation stay distinct sentences on purpose. The pasted block is inert until a
# per-clone hook is installed, and an initial adoption must delay that until the baseline is
# acknowledged and check and lint are clean, so a terse unconditional "then install" appended to
# the placement line would reintroduce exactly the ordering failure GTX-175 fixed.
#
# Scoped to a clone that is not already gated, because the same narration is emitted by
# --print-only, which is the documented upgrade path: replacing an existing block needs no
# reactivation, since the installed hook re-reads .pre-commit-config.yaml on every commit.
#
# README.md owns the rule and states it at length, including why the uv tool pair is preferred
# over uvx; this is the short form the terminal can carry. Changing it means editing README.md
# and this string, and tests/cli/test_init.py holds the only mechanically enforced copy.
#
# Printed with soft_wrap=True for the same reason as the baseline line: default wrapping would
# split a command across a real line break, which survives redirection and breaks it on copy.
_ACTIVATION_GUIDANCE = (
    "Enabling the gates is that separate step, and adding the pre-commit block does not perform "
    "it: all three hooks stay inert until pre-commit writes `.git/hooks/pre-commit` in this "
    "clone. If this clone is not already gated, run `uv tool install pre-commit` and then "
    "`uv tool run pre-commit install`, or plain `pre-commit install` when a durable runner is "
    "already available. On an initial adoption do that only after the baseline above, and after "
    "`doc-lattice check` and `doc-lattice lint` are clean; an established installation enables "
    "them immediately."
)


# GTX-298. `init` has no Git requirement and every lattice-loading command runs without Git, but
# the recipe it prints is entirely Git and GitHub artifacts, so an adopter outside a worktree was
# handed a complete set of instructions none of which could be followed -- and the terminal said
# nothing, because the gap was never in README.md. Naming the precondition once is the arm chosen
# over conditioning the output on a worktree, for two reasons that are properties of this command
# rather than preferences. The three blocks are a stable stdout payload shared byte-for-byte with
# --print-only, which is the documented upgrade retrieval path, and varying them would make the
# read-only path a second source of truth. And there is no classifier to vary them on that is not
# a repurposing: --print-only runs no ancestor walk at all, `_REPOSITORY_MARKER` bounds a refusal
# rather than selecting output and may disagree with Git under GIT_DIR and friends, and the branch
# probe collapses "no git", "no remote", and "outside a worktree" into one None.
#
# Worded about the destination rather than the invocation directory, because those are genuinely
# different: README.md documents --print-only as carrying no directory precondition, so a sentence
# telling the reader to go and enter a repository would be wrong for the adopter who ran it from a
# subdirectory precisely because the directory does not matter.
#
# Deliberately a separate line from the branch record above it. That record's `(fallback)` label
# is one three-cause label by design, and folding the precondition into it would make the label
# disambiguate a case it does not measure. Placement, baseline, and activation are separate lines
# for the same reason; this is the precondition on all three.
#
# Printed without soft_wrap, unlike the two lines above: it carries no copyable command, so it is
# prose that wraps like the placement line rather than a record that must survive redirection
# intact. README.md owns the rule and states it in "Ordinary offline setup"; tests/cli/test_init.py
# holds the only mechanically enforced copy of the wording.
_GIT_PRECONDITION_GUIDANCE = (
    "The three blocks and the activation step install into a Git checkout of the docs "
    "repository, and none of them is usable outside one: the ignore patterns need a repository "
    "to be ignored in, the workflow needs a GitHub repository to run in, and activation needs a "
    "clone to write its hook into. Producing this output needs no repository of its own, so the "
    "precondition is on the repository you install into rather than on the directory this run "
    "happened in."
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

# The errnos that answer "not here" rather than "cannot tell". Everything else leaves the walk
# unable to decide, and `_walk_entry_mode` refuses instead of guessing. ENAMETOOLONG is
# deliberately absent: a name too long to stat cannot name an existing entry, but reading that as
# absence means inferring a filesystem fact from a length limit, and refusing is the safe side of
# a case no real invocation reaches.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


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
        if "\\" in root:
            msg = (
                f"--docs-root {format_path_for_display(root)} contains a backslash, which the "
                "link_sources selector grammar cannot express; spell the path with '/'"
            )
            raise ValidationError(msg)
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


def _link_selectors(docs_roots: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    """Derive the ``link_sources`` selector for each docs root, from what the root is.

    A root that exists is classified by stat and spelled from its resolved, contained
    project-relative path rather than from the flag: the link gate never enters a symlinked
    directory, so a selector written over the link would fail on every run. A root that does not
    exist yet takes the directory form of its literal spelling.

    Every derived selector is then put through the same validator the config loader runs, and a
    rejection is reported here rather than written. ``_validate_init_flags`` refuses the flag
    spellings a reader can see are wrong, but it is not the whole grammar: a drive prefix is not
    absolute on POSIX, and a resolved symlink target contributes path segments the flag never
    carried. Writing such a selector would produce a config that every lattice-loading command
    refuses at load, so ``init`` would exit 0 and leave the project unable to run.

    Args:
        docs_roots: The validated ``--docs-root`` values.
        cwd: The invocation directory the config is written into.

    Returns:
        One selector per root, in root order.

    Raises:
        ValidationError: If an existing root resolves outside the invocation directory, or if a
            root's derived selector is one the ``link_sources`` grammar cannot express.
        InitPersistenceError: If a root can be neither confirmed nor ruled out.
    """
    project_root = cwd.resolve()
    selectors: list[str] = []
    for root in docs_roots:
        candidate = cwd / root
        mode = _walk_entry_mode(candidate)
        if mode is None:
            selector = selector_for_root(root, "directory")
        else:
            try:
                resolved = safe_resolve(candidate, project_root)
            except ValueError as exc:
                msg = (
                    f"--docs-root {format_path_for_display(root)} resolves outside the project "
                    f"directory {format_path_for_display(cwd)}; a link_sources selector cannot "
                    "reach it"
                )
                raise ValidationError(msg) from exc
            kind: RootKind = "file" if stat.S_ISREG(mode) else "directory"
            selector = selector_for_root(resolved.relative_to(project_root).as_posix(), kind)
        try:
            validate_link_selector(selector)
        except ValueError as exc:
            msg = (
                f"--docs-root {format_path_for_display(root)} becomes the link_sources selector "
                f"{format_path_for_display(selector)}, which {exc}"
            )
            raise ValidationError(msg) from exc
        selectors.append(selector)
    return tuple(selectors)


def _init_persistence_error(target_name: str, cause: OSError) -> InitPersistenceError:
    """Wrap one scaffold write failure, preserving the low-level remediation notes."""
    error = InitPersistenceError(f"cannot write {format_path_for_display(target_name)}: {cause}")
    copy_exception_notes(error, cause)
    return error


def _walk_unreadable_error(path: Path, cause: OSError) -> InitPersistenceError:
    """Wrap one unreadable walk entry, preserving the low-level remediation notes."""
    error = InitPersistenceError(
        f"cannot determine whether {format_path_for_display(path)} exists: {cause}. That "
        "answer decides whether this directory already sits inside a configured lattice, so "
        "init refuses to scaffold rather than guess. Pass --print-only to obtain the snippets "
        f"without writing anything, or write {DEFAULT_CONFIG_NAME} here by hand if a nested "
        "lattice is what you intend."
    )
    copy_exception_notes(error, cause)
    return error


def _walk_entry_mode(path: Path, *, follow_symlinks: bool = True) -> int | None:
    """Read one walk entry's mode, refusing to answer when the filesystem cannot.

    ``Path.exists`` is not a stable predicate across the versions this package supports: 3.13
    re-raises an ``OSError`` outside its ignored set, while 3.14 delegates to ``os.path.exists``
    and answers False for every one of them. A guard built on it therefore crashes with an
    uncoded error on one interpreter and silently scaffolds the nested config it exists to
    prevent on the other, against the same filesystem. ``Path.stat`` raises on both, which is why
    the decision is made here rather than delegated: ``_ABSENT_ERRNOS`` means the entry is not
    there, and anything else means the question is unanswerable, which becomes a coded refusal
    rather than a guess in either direction.

    Args:
        path: The entry to stat.
        follow_symlinks: False to stat the link itself, so a dangling symlink still reads as
            present. The repository marker wants that; a configuration file does not.

    Returns:
        The entry's ``st_mode``, or None when the entry is absent.

    Raises:
        InitPersistenceError: If the entry can be neither confirmed nor ruled out.
    """
    try:
        return path.stat(follow_symlinks=follow_symlinks).st_mode
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return None
        raise _walk_unreadable_error(path, exc) from exc


def _is_config_file(path: Path) -> bool:
    """Report whether one path is a configuration file rather than absent or a directory.

    Raises:
        InitPersistenceError: If the entry can be neither confirmed nor ruled out.
    """
    mode = _walk_entry_mode(path)
    return mode is not None and stat.S_ISREG(mode)


def _is_repository_marker(path: Path) -> bool:
    """Report whether one path is the ``.git`` entry that bounds the walk.

    Presence is the whole test, whatever kind of entry it is: a linked worktree and a submodule
    checkout carry ``.git`` as a regular file, and a dangling symlink still marks a root Git
    itself would recognize, so the link is stated rather than followed.

    Raises:
        InitPersistenceError: If the entry can be neither confirmed nor ruled out.
    """
    return _walk_entry_mode(path, follow_symlinks=False) is not None


def _print_unmanaged_guidance(runtime: CliRuntime, ci_text: str) -> None:
    """Print the ordinary workflow, where every printed block goes, and how to activate them.

    Placement, the first-adoption baseline, and activation are three separate lines because they
    are three separate acts in a fixed order. Collapsing any pair would either lose the ordering
    constraint or state it without naming the act it orders against.
    """
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
    runtime.stderr.print(_ACTIVATION_GUIDANCE, soft_wrap=True)


def _print_artifacts(
    runtime: CliRuntime, gitignore_text: str, precommit_text: str, ci_text: str
) -> None:
    """Print the recipe's Git precondition, then the three hand-installed blocks in order.

    The one site that prints the three blocks, reached by both modes, so block order and the
    headers around them cannot differ between an ordinary run and ``--print-only``. It also
    carries the placement, baseline, and activation guidance that accompanies them on stderr,
    by way of ``_print_unmanaged_guidance``; the branch narration is the other shared output and
    is emitted by the caller. What the two modes still choose independently is where those three
    texts came from, which is why ``tests/cli/test_init.py`` pins their exact bytes against each
    other rather than trusting this function alone.

    The precondition leads, unconditionally, because it qualifies everything after it: this is
    the one function both modes reach, so emitting it here makes the two modes share it by
    construction rather than by two call sites being kept in step. "Before the blocks" is a
    terminal-only property, since stdout and stderr interleave nowhere else -- an adopter
    redirecting stdout to a file gets the precondition on stderr and the blocks in the file, in
    no relative order at all -- which is why the wording states the precondition outright rather
    than pointing at what follows it.

    Args:
        runtime: The invocation's output streams.
        gitignore_text: The ignore patterns block.
        precommit_text: The pre-commit hooks block.
        ci_text: The ordinary GitHub Actions workflow.
    """
    runtime.stderr.print(_GIT_PRECONDITION_GUIDANCE)
    runtime.write_stdout("# ===== .gitignore (append these lines) =====")
    runtime.write_stdout(gitignore_text)
    runtime.write_stdout("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
    runtime.write_stdout(precommit_text)
    _print_unmanaged_guidance(runtime, ci_text)


def _find_ancestor_config(cwd: Path) -> Path | None:
    """Find the configuration an ancestor directory already holds, bounded by the repository.

    ``init`` writes into the invocation directory, and every lattice-loading command selects a
    default config from *its* invocation directory, so a run from a subdirectory of a configured
    repository would otherwise scaffold a second, nested config that the configured directory
    never sees. Only a command launched from that same subdirectory would load it, which is what
    makes it a silently divergent second lattice rather than an inert file. This is the detection
    half of the refusal that replaced that write.

    The walk is bounded by the nearest ``.git`` entry, inclusive, and yields nothing at all when
    no such entry is found, which is why the nearest config is held rather than returned on
    sight: whether it counts is not known until a boundary is. Both bounds are deliberate.
    Scanning to the filesystem root would let one stray config in a home directory refuse every
    new project created under it, and stopping at the first marker means a nested repository or
    submodule beneath a configured root is its own scope and scaffolds normally. The marker is
    tested for presence rather than for being a directory, because a linked worktree and a
    submodule checkout both carry ``.git`` as a regular file and an ``is_dir`` test would walk
    straight past their roots. A configuration entry, by contrast, has to be a regular file:
    a directory of that name configures nothing, and counting it would refuse a scaffold on the
    strength of a name.

    This is a filesystem walk and not a Git query on purpose. ``init`` has no Git prerequisite,
    and resolving the boundary through Git would re-create the top-level resolution contract
    ``git_repository.py`` records as retired with the managed commands under AD-32, and make the
    refusal depend on an executable. One consequence is stated rather than fixed: Git's own
    discovery honors ``GIT_DIR``, ``GIT_CEILING_DIRECTORIES``, and
    ``GIT_DISCOVERY_ACROSS_FILESYSTEM``, so under those settings the default-branch probe and
    this walk can disagree about where the repository begins. The cost is bounded at one refusal
    too many or one too few, never a write to a directory the user did not name, because this
    boundary only bounds a refusal and never selects a destination; a missed refusal leaves the
    pre-existing nested-scaffold outcome rather than creating a new one. An entry the filesystem
    cannot report on is not in that bounded cost and is not guessed at: ``_walk_entry_mode``
    turns it into a coded refusal.

    Args:
        cwd: The invocation directory, whose own config the caller has already ruled out.

    Returns:
        The nearest ancestor's configuration file when the walk reached a repository boundary,
        and None otherwise, which covers both a boundary holding no config and no boundary at
        all.

    Raises:
        InitPersistenceError: If an entry along the walk can be neither confirmed nor ruled out.
    """
    if _is_repository_marker(cwd / _REPOSITORY_MARKER):
        return None
    nearest: Path | None = None
    for ancestor in cwd.parents:
        candidate = ancestor / DEFAULT_CONFIG_NAME
        if nearest is None and _is_config_file(candidate):
            nearest = candidate
        if _is_repository_marker(ancestor / _REPOSITORY_MARKER):
            return nearest
    return None


def _nested_scaffold_error(ancestor: Path) -> ValidationError:
    """Build the refusal for a run that would scaffold a config beneath an existing one."""
    return ValidationError(
        f"{format_path_for_display(ancestor)} already configures this repository, and init "
        "writes only into the current directory, so scaffolding here would leave a second, "
        "nested lattice that only commands run from this directory would load, and that check, "
        "lint, reconcile, and the rest never see when run from the directory holding that "
        "configuration. Run init from that directory instead, pass --print-only to obtain the "
        f"snippets without writing anything, or write {DEFAULT_CONFIG_NAME} here by hand if a "
        "nested lattice is what you intend."
    )


def _report_pre_v7_config(runtime: CliRuntime, target: Path) -> None:
    """Say why the config left untouched will not load, when that is why it was left.

    ``init`` reports rather than repairs. Its whole persistence design is create-only, down to
    publishing through ``os.link``, which cannot replace a destination; adding the key here would
    give the command a write path over a file the user owns and this one never wrote. The
    adopter edits the file, as the 7.0.0 migration instructs.

    Reporting is still worth doing, because the alternative is the failure this closes: a run
    that narrates a full success while every other command in the repository exits 2, and names
    nothing that connects the two.

    Args:
        runtime: The invocation's output streams.
        target: The existing config, which the caller established is there by failing to create
            it.
    """
    if declares_lattice_format(target) is not False:
        return
    runtime.stderr.print(
        f"[yellow]warning[/yellow]: {escape(format_path_for_display(target.name))} does not "
        f"declare 'lattice_format: {LATTICE_FORMAT_VERSION}', so doc-lattice {__version__} "
        "refuses to load it. Add the key as the first line, then follow the 7.0.0 migration in "
        "CHANGELOG.md; init does not edit a config it did not write.",
        soft_wrap=True,
        emoji=False,
        highlight=False,
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
        InitPersistenceError: If an entry the guard has to read can be neither confirmed nor
            ruled out, if the file could not be written, or if a failed staging cleanup left an
            orphan behind. An existing config is not one of these: the boundary reports that
            as ``DestinationExistsError`` and this reports it and exits 0.
    """
    target = runtime.cwd / DEFAULT_CONFIG_NAME
    if not _is_config_file(target):
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
            # exemptions are keyed by (module, qualified function, expression), so one
            # written for this line would have covered those two sinks as well.
            prefix=f"{DEFAULT_CONFIG_NAME}.",
        )
    # The benign case is the one the boundary names: the destination was already there and
    # nothing was left behind. It used to be inferred here from a FileExistsError carrying no
    # notes, which answers "did the stage cleanup fail" rather than "did the destination
    # exist", so any other note-free FileExistsError escaping the write path was reported as an
    # existing config and exited 0. A collision that orphaned its stage keeps the plain type
    # and falls to the arm below with everything else, since the orphan is a real failure
    # whatever collided. Both abnormal branches carry INIT_PERSISTENCE rather than
    # CONFIG_ERROR: the defect is in the directory being scaffolded, and init's write path never
    # reads .doc-lattice.yml, so naming config sent the user to a file that had nothing to do
    # with the failure. `_report_pre_v7_config` does read it, but only on the benign arm and
    # only to narrate; it swallows every read and parse failure rather than reclassifying one of
    # these, so neither error's provenance changes.
    # Both arms print with soft_wrap=True for the one-record-per-line contract every other
    # renderer and adapter carries: a status record stays on one physical line at any terminal
    # width rather than hard-wrapping mid-token into a fragment. That is a property of being a
    # record, not of the token being externally controlled -- `target.name` is always
    # DEFAULT_CONFIG_NAME here. The placement guidance at `_print_unmanaged_guidance` is prose
    # and is deliberately left to wrap; the baseline guidance beside it already opts out.
    except DestinationExistsError:
        runtime.stderr.print(
            f"{escape(format_path_for_display(target.name))} already exists, leaving it untouched",
            soft_wrap=True,
        )
        _report_pre_v7_config(runtime, target)
    except OSError as exc:
        raise _init_persistence_error(target.name, exc) from exc
    else:
        runtime.stderr.print(
            f"wrote {escape(format_path_for_display(target.name))}", soft_wrap=True
        )


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
                selectors = _link_selectors(roots, runtime.cwd)
                branch, branch_source = _resolve_default_branch(default_branch, runtime.cwd)
                scaffold = build_scaffold(
                    roots, selectors, linear_team, __version__, default_branch=branch
                )
                _scaffold_config(runtime, scaffold.config_text)
                gitignore_text = scaffold.gitignore_text
                precommit_text = scaffold.precommit_text
                ci_text = scaffold.ci_text
            # A record, and the one here whose token is externally controlled: --default-branch
            # or the origin/HEAD probe supplies `branch`, and a name long enough to exceed the
            # console would otherwise split mid-token into something no reader can match against
            # the workflow the run just rendered.
            runtime.stderr.print(
                f"workflow triggers on branch {escape(branch)} ({branch_source})", soft_wrap=True
            )
            _print_artifacts(runtime, gitignore_text, precommit_text, ci_text)
        raise typer.Exit(0)
