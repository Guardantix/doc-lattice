"""Shared no-follow filesystem selection with consumer-neutral refusal records.

Grammar validation remains in ``link_selectors``. The walk retains every spelling and its
selectors, and never resolves file aliases, enrolls nodes, or reads contents. Consumers render
the refusal records in their own error taxonomy.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .link_selectors import (
    RECURSIVE_SEGMENT,
    SELECTOR_SEPARATOR,
    segment_matches,
    selector_matches_path,
    validate_link_selector,
)


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Choose whether traversable directory symlinks become refusal records."""

    refuse_symlink_directories: bool = False


@dataclass(frozen=True, slots=True)
class Exclusions:
    """Selectors that prune a selection before the walk inspects an entry."""

    selectors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectedPath:
    """A project-relative POSIX spelling and its sorted, unique selecting declarations."""

    path: str
    selectors: tuple[str, ...]


RefusalKind = Literal[
    "root-unresolved",
    "no-selectors",
    "invalid-selector",
    "no-match",
    "scan-failed",
    "inspect-failed",
    "symlink-directory",
]


@dataclass(frozen=True, slots=True)
class SelectionRefusal:
    """One selection refusal, without a consumer error type or diagnostic wording.

    ``spelling`` is project-relative for entries and selectors, and the root or inspected
    absolute path for filesystem failures. ``source`` distinguishes selection from exclusion
    grammar; ``selector`` names the declaration that reached a traversal refusal.
    """

    kind: RefusalKind
    spelling: str
    selector: str | None = None
    source: Literal["select", "exclude"] = "select"
    detail: str | None = None
    pruned: bool = False


def refusal_from_error(error: ValueError) -> SelectionRefusal:
    """Extract a structured refusal from the built-in error carrying it.

    Args:
        error: A value error raised during selection.

    Returns:
        The structured refusal the selection boundary attached.

    Raises:
        ValueError: The original error when it did not come from the selection boundary.
    """
    if len(error.args) != 1 or not isinstance(error.args[0], SelectionRefusal):
        raise error
    return error.args[0]


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Selected paths and collectable traversal or symlink-only no-match refusals."""

    paths: tuple[SelectedPath, ...]
    refusals: tuple[SelectionRefusal, ...]


@dataclass(frozen=True, slots=True)
class _Selection:
    """One selector's traversal policy, exclusions, and refusal sink.

    One object rather than four parameters because ``_recursive_frames`` already takes the five
    arguments the lint ceiling allows, and every step needs these values together.
    """

    policy: SelectionPolicy
    exclusions: tuple[tuple[str, ...], ...]
    selector: str
    refusals: list[SelectionRefusal]


def _validated(selector: str, source: Literal["select", "exclude"]) -> tuple[str, ...]:
    """Validate one selector and retain which list carried it.

    Args:
        selector: One selector in the shared grammar.
        source: The list the selector was declared under.

    Returns:
        The selector's validated segments.

    Raises:
        ValueError: Carrying a ``SelectionRefusal`` when the grammar refuses the selector.
    """
    try:
        return validate_link_selector(selector)
    except ValueError as exc:
        raise ValueError(
            SelectionRefusal("invalid-selector", selector, source=source, detail=str(exc))
        ) from exc


def select_paths(
    project_root: Path,
    selectors: Sequence[str],
    *,
    policy: SelectionPolicy,
    exclude: Exclusions | None = None,
) -> SelectionResult:
    """Expand selectors without following directory symlinks or collapsing file aliases.

    Args:
        project_root: Root from which every selector is expanded.
        selectors: Selectors in the shared grammar, validated here even for direct callers.
        policy: Whether traversable directory symlinks are refused.
        exclude: Selectors that prune the walk. An excluded directory is never entered, so
            nothing beneath it is selected, inspected, or scanned; an excluded file is never
            selected.

    Returns:
        Matched spellings and traversal refusals in project-relative order.

    Raises:
        ValueError: Carrying a ``SelectionRefusal`` when a selector is invalid, matches nothing,
            or the filesystem cannot be inspected.
    """
    try:
        root = project_root.resolve()
    except OSError as exc:
        raise ValueError(
            SelectionRefusal("root-unresolved", str(project_root), detail=str(exc))
        ) from exc
    if not selectors:
        raise ValueError(SelectionRefusal("no-selectors", str(root)))
    exclusions = (
        tuple(_validated(entry, "exclude") for entry in exclude.selectors)
        if exclude is not None
        else ()
    )
    pruned = bool(exclusions)
    matched: dict[str, set[str]] = {}
    refusals: list[SelectionRefusal] = []
    for selector in selectors:
        segments = _validated(selector, "select")
        selection = _Selection(policy, exclusions, selector, refusals)
        found = _walk(root, segments, selection)
        if not found:
            unmatched = SelectionRefusal("no-match", selector, detail=str(root), pruned=pruned)
            if not refusals:
                raise ValueError(unmatched)
            refusals.append(unmatched)
        for path in found:
            matched.setdefault(path, set()).add(selector)
    return SelectionResult(
        tuple(
            SelectedPath(path, tuple(sorted(entries))) for path, entries in sorted(matched.items())
        ),
        tuple(
            sorted(
                set(refusals),
                key=lambda refusal: (refusal.spelling, refusal.kind, refusal.selector or ""),
            )
        ),
    )


def _scan(directory: Path, selector: str) -> list[os.DirEntry[str]]:
    """List one directory.

    Raises:
        ValueError: Carrying a ``SelectionRefusal`` if the filesystem refuses the scan.
    """
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError as exc:
        raise ValueError(
            SelectionRefusal("scan-failed", str(directory), selector=selector, detail=str(exc))
        ) from exc


def _is_directory(
    entry: os.DirEntry[str], selection: _Selection, spelling: str, *, traverse: bool = False
) -> bool | None:
    """Report whether an entry is a directory in its own right, never through a symlink.

    Args:
        entry: The directory entry to classify.
        selection: The traversal policy and refusal sink.
        spelling: The entry's project-relative spelling, which is what a refusal names. An
            author needs the spelling they can write into a configuration, not the absolute
            path the walk happens to be holding.
        traverse: Whether the walk is about to enter this entry, the only case in which a
            symlinked directory can be refused.

    Returns:
        True for a real directory, false for a file, or None for a refused directory symlink.

    Raises:
        ValueError: Carrying a ``SelectionRefusal`` if the filesystem refuses inspection.
    """
    try:
        directory = entry.is_dir(follow_symlinks=False)
        if (
            traverse
            and selection.policy.refuse_symlink_directories
            and entry.is_symlink()
            and entry.is_dir(follow_symlinks=True)
        ):
            selection.refusals.append(
                SelectionRefusal("symlink-directory", spelling, selector=selection.selector)
            )
            return None
        return directory
    except OSError as exc:
        raise ValueError(
            SelectionRefusal(
                "inspect-failed", entry.path, selector=selection.selector, detail=str(exc)
            )
        ) from exc


def _join(prefix: str, name: str) -> str:
    return name if prefix == "" else f"{prefix}{SELECTOR_SEPARATOR}{name}"


def _retained(
    entries: list[os.DirEntry[str]], prefix: str, exclusions: tuple[tuple[str, ...], ...]
) -> list[os.DirEntry[str]]:
    """Drop the entries an exclusion selector matches.

    Applied to a listing as it is read, ahead of every other judgment the walk makes about an
    entry, so an excluded spelling is never inspected, never entered, and never selected. One
    site rather than one per branch: the refusal an exclusion exists to prune is recorded while
    deciding whether an entry is a traversable directory, so a branch that pruned second would
    reintroduce it, and every branch added later would have to remember the same rule.

    Matching is the lexical half of the walk, so an exclusion names the directory itself. A
    contents-shaped selector matches what is inside a directory and never the directory, which
    leaves it traversable and its refusal standing.

    Args:
        entries: One directory's listing.
        prefix: The project-relative spelling that directory was reached by.
        exclusions: The validated exclusion segments, empty for a consumer that prunes nothing.

    Returns:
        The entries no exclusion matched, in the order they were given, and the listing itself
        when nothing is excluded.
    """
    if not exclusions:
        return entries
    retained: list[os.DirEntry[str]] = []
    for entry in entries:
        spelling = _join(prefix, entry.name)
        if not any(selector_matches_path(segments, spelling) for segments in exclusions):
            retained.append(entry)
    return retained


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
        ValueError: Carrying a ``SelectionRefusal`` if the filesystem refuses inspection.
    """
    frames: list[_Frame] = []
    for entry in entries:
        spelling = _join(frame.prefix, entry.name)
        kind = _is_directory(entry, selection, spelling, traverse=True)
        if kind is True:
            frames.append(_Frame(Path(entry.path), spelling, frame.index))
        elif kind is False and last:
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
        ValueError: Carrying a ``SelectionRefusal`` for a scan or inspection refusal.
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
        entries = frame.entries
        if entries is None:
            entries = _retained(
                _scan(frame.directory, selection.selector), frame.prefix, selection.exclusions
            )
        if segment == RECURSIVE_SEGMENT:
            pending.extend(_recursive_frames(frame, entries, last, found, selection))
            continue
        for entry in entries:
            if not segment_matches(entry.name, segment):
                continue
            spelling = _join(frame.prefix, entry.name)
            if last:
                if _is_directory(entry, selection, spelling) is False:
                    found.add(spelling)
            elif _is_directory(entry, selection, spelling, traverse=True) is True:
                pending.append(_Frame(Path(entry.path), spelling, frame.index + 1))
    return found
