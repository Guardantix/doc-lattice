"""Shared no-follow filesystem selection, retaining every spelling and its selectors.

Grammar validation remains in ``link_selectors``. Consumers choose the error taxonomy, whether
reaching a symlinked directory is a refusal, and the selectors that prune the walk; no policy
follows such directories. Selection does not resolve or deduplicate file aliases, enroll nodes,
or read file contents.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .error_types import ProjectError
from .link_selectors import (
    RECURSIVE_SEGMENT,
    SELECTOR_SEPARATOR,
    segment_matches,
    selector_defect_message,
    selector_matches_path,
    validate_link_selector,
)
from .path_utils import format_path_for_display


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """The consumer's diagnostic context and traversal refusal policy.

    Nothing here defaults to a consumer: this module names no key, purpose, or error type of
    its own, so a caller that forgets one cannot inherit another consumer's taxonomy and report
    a refusal under a key the reader will not find in their configuration.

    Attributes:
        key: The config key whose entries are being expanded, named in every diagnostic.
        purpose: How a refusal spells the gate that refused, as in "the links command refuses".
        error_type: The ``ProjectError`` subclass every refusal is raised as.
        refuse_symlink_directories: Whether reaching a symlinked directory is a refusal rather
            than a directory the walk declines to enter.
        selector_note: The note every refusal met inside the walk gains, with ``{selector}``
            standing in for the declaration that reached it. The remedies it carries belong to
            the consumer and not to this module, which names no key of its own and would
            otherwise offer one gate's escape hatch to a gate that does not have it.
    """

    key: str
    purpose: str
    error_type: type[ProjectError]
    refuse_symlink_directories: bool = False
    selector_note: str | None = None

    def __post_init__(self) -> None:
        """Refuse a traversal refusal with nowhere to send the reader.

        A consumer strict enough to refuse a symlinked directory owes the author a way out of
        it, and the note is how a remedy reaches the terminal, since ``exception_details``
        renders every note after the message. Checked here because both policies in the engine
        are module constants, so a policy that forgot one fails at import and not on a user.
        """
        if self.refuse_symlink_directories and self.selector_note is None:
            msg = "refuse_symlink_directories requires selector_note to carry its remedy"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Exclusions:
    """The selectors that prune a selection, and the key an author declared them under.

    The two travel as one argument because they are only correct together: a diagnostic about
    an exclusion has to name the key that carried it, and a caller able to hand over selectors
    without their key would have them reported under the key its policy names for selection,
    sending the reader to the wrong list.

    Attributes:
        key: The config key these entries came from, named in every exclusion diagnostic.
        selectors: Selectors in the shared grammar, validated by ``select_paths``.
    """

    key: str
    selectors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectedPath:
    """A project-relative POSIX spelling and its sorted, unique selecting declarations."""

    path: str
    selectors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Selection:
    """One call's policy and validated exclusion segments, threaded through the walk.

    One object rather than two parameters because ``_recursive_frames`` already takes the five
    arguments the lint ceiling allows, and because the pair is what every step of the walk
    needs together.
    """

    policy: SelectionPolicy
    exclusions: tuple[tuple[str, ...], ...]


def _validated(selector: str, key: str, policy: SelectionPolicy) -> tuple[str, ...]:
    """Validate one selector, naming the key that carried it.

    Args:
        selector: One selector in the shared grammar.
        key: The config key the selector was declared under, which is what its defect names.
        policy: The consumer's diagnostic context.

    Returns:
        The selector's validated segments.

    Raises:
        ProjectError: Using the policy's error type, if the grammar refuses the selector.
    """
    try:
        return validate_link_selector(selector)
    except ValueError as exc:
        raise policy.error_type(selector_defect_message(key, selector, exc)) from exc


def select_paths(
    project_root: Path,
    selectors: Sequence[str],
    *,
    policy: SelectionPolicy,
    exclude: Exclusions | None = None,
) -> tuple[SelectedPath, ...]:
    """Expand selectors without following directory symlinks or collapsing file aliases.

    Args:
        project_root: Root from which every selector is expanded.
        selectors: Selectors in the shared grammar, validated here even for direct callers.
        policy: Diagnostic context and whether traversable directory symlinks are refused.
        exclude: Selectors that prune the walk, and the key that declared them. An excluded
            directory is never entered, so nothing beneath it is selected, inspected, or
            scanned; an excluded file is never selected.

    Returns:
        Every matched spelling in project-relative order, with all its selecting declarations.

    Raises:
        ProjectError: Using the policy's error type, if a selector is invalid, matches nothing,
            or the filesystem cannot be inspected under the chosen traversal policy.
    """
    try:
        root = project_root.resolve()
    except OSError as exc:
        raise policy.error_type(
            f"{policy.key} project root {format_path_for_display(project_root)} "
            f"could not be resolved: {exc}"
        ) from exc
    if not selectors:
        raise policy.error_type(
            f"{policy.key} names no selector for the project root "
            f"{format_path_for_display(root)}; the {policy.purpose} refuses to run "
            "without a selector"
        )
    exclusions = (
        tuple(_validated(entry, exclude.key, policy) for entry in exclude.selectors)
        if exclude is not None
        else ()
    )
    pruned_by = exclude.key if exclude is not None and exclusions else None
    selection = _Selection(policy, exclusions)
    matched: dict[str, set[str]] = {}
    for selector in selectors:
        segments = _validated(selector, policy.key, policy)
        try:
            found = _walk(root, segments, selection)
        except ProjectError as exc:
            if policy.selector_note is not None:
                displayed = format_path_for_display(selector)
                exc.add_note(policy.selector_note.format(selector=displayed))
            raise
        if not found:
            surviving = f" that {pruned_by} did not prune" if pruned_by is not None else ""
            raise policy.error_type(
                f"{policy.key} entry {format_path_for_display(selector)} matches no file "
                f"under the project root {format_path_for_display(root)}{surviving}; "
                f"the {policy.purpose} refuses to run over a selector that selects nothing"
            )
        for path in found:
            matched.setdefault(path, set()).add(selector)
    return tuple(
        SelectedPath(path, tuple(sorted(entries))) for path, entries in sorted(matched.items())
    )


def _scan(directory: Path, policy: SelectionPolicy) -> list[os.DirEntry[str]]:
    """List one directory.

    Raises:
        ProjectError: If the filesystem refuses the scan. The consumer chooses its
            selection-time error type.
    """
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError as exc:
        displayed = format_path_for_display(directory)
        msg = f"{policy.key} selection could not scan {displayed}: {exc}"
        raise policy.error_type(msg) from exc


def _is_directory(
    entry: os.DirEntry[str], policy: SelectionPolicy, spelling: str, *, traverse: bool = False
) -> bool:
    """Report whether an entry is a directory in its own right, never through a symlink.

    Args:
        entry: The directory entry to classify.
        policy: The consumer's diagnostic context and traversal refusal policy.
        spelling: The entry's project-relative spelling, which is what a refusal names. An
            author needs the spelling they can write into a configuration, not the absolute
            path the walk happens to be holding.
        traverse: Whether the walk is about to enter this entry, the only case in which a
            symlinked directory can be refused.

    Returns:
        True when the entry is a directory and not a symlink to one.

    Raises:
        ProjectError: If the filesystem refuses the inspection, or the entry is a symlinked
            directory the walk is about to enter under a policy that refuses those. The
            consumer chooses its selection-time error type.
    """
    try:
        directory = entry.is_dir(follow_symlinks=False)
        if (
            traverse
            and policy.refuse_symlink_directories
            and entry.is_symlink()
            and entry.is_dir(follow_symlinks=True)
        ):
            raise policy.error_type(
                f"{policy.key} selection refuses to traverse symlinked directory "
                f"{format_path_for_display(spelling)}"
            )
        return directory
    except OSError as exc:
        displayed = format_path_for_display(entry.path)
        msg = f"{policy.key} selection could not inspect {displayed}: {exc}"
        raise policy.error_type(msg) from exc


def _join(prefix: str, name: str) -> str:
    return name if prefix == "" else f"{prefix}{SELECTOR_SEPARATOR}{name}"


def _retained(
    entries: list[os.DirEntry[str]], prefix: str, exclusions: tuple[tuple[str, ...], ...]
) -> list[os.DirEntry[str]]:
    """Drop the entries an exclusion selector matches.

    Applied to a listing as it is read, ahead of every other judgment the walk makes about an
    entry, so an excluded spelling is never inspected, never entered, and never selected. One
    site rather than one per branch: the refusal an exclusion exists to prune is raised while
    deciding whether an entry is a traversable directory, so a branch that pruned second would
    reintroduce it, and every branch added later would have to remember the same rule.

    Matching is the lexical half of the walk, so an exclusion names the directory itself. A
    contents-shaped selector matches what is inside a directory and never the directory, which
    leaves it traversable and its refusal standing.

    Args:
        entries: One directory's listing.
        prefix: The project-relative spelling that directory was reached by.
        exclusions: The validated exclusion segments, never empty.

    Returns:
        The entries no exclusion matched, in the order they were given.
    """
    return [
        entry
        for entry in entries
        if not any(
            selector_matches_path(segments, _join(prefix, entry.name)) for segments in exclusions
        )
    ]


@dataclass(frozen=True, slots=True)
class _Frame:
    """One pending ``_walk`` step: match ``segments[index]`` against ``directory``.

    ``prefix`` is the project-relative spelling ``directory`` was reached by, and is what the
    memo is keyed on rather than the path, since a spelling is what the walk returns.
    ``entries`` is a listing carried from the frame that already scanned this directory, with
    exclusions already applied, and is None for every step that descends into a directory
    nothing has read yet.
    """

    directory: Path
    prefix: str
    index: int
    entries: list[os.DirEntry[str]] | None = None


def _recursive_frames(
    frame: _Frame,
    entries: list[os.DirEntry[str]],
    last: bool,
    found: set[str],
    selection: _Selection,
) -> list[_Frame]:
    """Expand one ``**`` frame: collect its file matches and return the steps it spawns.

    ``**`` matches zero or more directories, so the frame both takes each child directory with
    itself still current and hands its own directory to ``index + 1``. The handoff is the one
    step that stays in the same directory, so it is the one that can reuse the listing its frame
    already scanned, and reusing it is what keeps the common ``docs/**/*.md`` shape at one
    ``scandir`` per directory instead of two. It is returned last precisely so the caller's stack
    pops it first and it claims its state before any child of this directory can.

    Args:
        frame: The frame being expanded, whose segment is ``**``.
        entries: That directory's listing, with exclusions already applied.
        last: Whether ``**`` is the selector's final segment.
        found: The selector's matched spellings, added to in place.
        selection: The consumer's policy and this call's validated exclusions.

    Returns:
        The frames to push, children first and the ``index + 1`` handoff last.

    Raises:
        ProjectError: If the filesystem refuses to inspect an entry, a selection-time refusal.
    """
    frames: list[_Frame] = []
    for entry in entries:
        spelling = _join(frame.prefix, entry.name)
        if _is_directory(entry, selection.policy, spelling, traverse=True):
            frames.append(_Frame(Path(entry.path), spelling, frame.index))
        elif last:
            found.add(spelling)
    if not last:
        frames.append(_Frame(frame.directory, frame.prefix, frame.index + 1, entries))
    return frames


def _walk(root: Path, segments: tuple[str, ...], selection: _Selection) -> set[str]:
    """Match ``segments`` beneath ``root``, returning the project-relative spellings found.

    A directory is a traversal node and never a match; only a non-directory entry can satisfy
    the last segment.

    The walk keeps its own stack rather than the interpreter's, because its depth is the
    repository's and not the selector's: a ``**`` over a tree about a thousand directories deep
    would otherwise exhaust the recursion limit and end this mandatory gate in a traceback rather
    than a diagnostic or a clean run. Nothing else in the engine has that ceiling, since
    ``discovery`` walks with ``rglob`` and ``reconcile_transaction`` with ``os.walk``, both of
    which iterate. Popping from the end keeps the traversal depth-first, which is what lets a
    ``**`` handoff claim its state ahead of the children pushed beneath it.

    ``visited`` memoizes ``(prefix, index)`` states already scanned. It lives for this call and
    this call only, so one selector's memoization never suppresses a scan another selector needs.
    Adjacent ``**`` segments are valid grammar, and without memoization a directory at depth d
    reached through k of them is scanned about C(d+k-1, k-1) times: the non-last ``**`` branch
    both takes each child directory at the same index and hands the same
    directory to ``index + 1``, and those two spreads converge on the same ``(prefix, index)``
    state from many paths lower in the tree. Claiming each state as it is popped bounds the whole
    walk to at most one scan per directory per segment index. What it removes is a state being
    reached from several paths, not the handoff to ``index + 1`` itself: that lands on a
    different key, so the bound stays directories times segments rather than directories alone.
    The bound does not change what is found: reaching a state a second time could only add
    matches the first arrival already added, and ``found`` is a set the caller sorts.

    Exclusions are applied to each listing as it is scanned, which is before any entry is
    classified and therefore before a symlinked directory can be refused. The handoff frame
    carries the filtered listing rather than the raw one, so a state is pruned identically
    however it is reached, and the memo is unaffected: exclusion is a function of the spelling
    alone and is constant for the whole call.

    Args:
        root: The resolved project root the selector is anchored to.
        segments: The validated selector segments.
        selection: The consumer's policy and this call's validated exclusions.

    Returns:
        Every project-relative spelling the selector matched, unordered.

    Raises:
        ProjectError: If the filesystem refuses to scan a directory or inspect an entry, both
            selection-time refusals.
    """
    found: set[str] = set()
    visited: set[tuple[str, int]] = set()
    pending = [_Frame(root, "", 0)]
    while pending:
        frame = pending.pop()
        key = (frame.prefix, frame.index)
        if key in visited:
            continue
        visited.add(key)
        segment = segments[frame.index]
        last = frame.index == len(segments) - 1
        if frame.entries is None:
            entries = _scan(frame.directory, selection.policy)
            if selection.exclusions:
                entries = _retained(entries, frame.prefix, selection.exclusions)
        else:
            entries = frame.entries
        if segment == RECURSIVE_SEGMENT:
            pending.extend(_recursive_frames(frame, entries, last, found, selection))
            continue
        for entry in entries:
            if not segment_matches(entry.name, segment):
                continue
            spelling = _join(frame.prefix, entry.name)
            if last:
                if not _is_directory(entry, selection.policy, spelling):
                    found.add(spelling)
            elif _is_directory(entry, selection.policy, spelling, traverse=True):
                pending.append(_Frame(Path(entry.path), spelling, frame.index + 1))
    return found
