"""Shared no-follow filesystem selection, retaining every spelling and its selectors.

Grammar validation remains in ``link_selectors``. Consumers choose the error taxonomy and
whether reaching a symlinked directory is a refusal; neither policy follows such directories.
Selection does not resolve or deduplicate file aliases, enroll nodes, or read file contents.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .error_types import ConfigError, ProjectError
from .link_selectors import (
    LINK_SOURCES_KEY,
    RECURSIVE_SEGMENT,
    SELECTOR_SEPARATOR,
    segment_matches,
    selector_defect_message,
    validate_link_selector,
)
from .path_utils import format_path_for_display


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """The consumer's diagnostic context and traversal refusal policy."""

    key: str = LINK_SOURCES_KEY
    purpose: str = "links command"
    error_type: type[ProjectError] = ConfigError
    refuse_symlink_directories: bool = False


_LINK_POLICY = SelectionPolicy()


@dataclass(frozen=True, slots=True)
class SelectedPath:
    """A project-relative POSIX spelling and its sorted, unique selecting declarations."""

    path: str
    selectors: tuple[str, ...]


def select_paths(
    project_root: Path,
    selectors: Sequence[str],
    *,
    policy: SelectionPolicy = _LINK_POLICY,
) -> tuple[SelectedPath, ...]:
    """Expand selectors without following directory symlinks or collapsing file aliases.

    Args:
        project_root: Root from which every selector is expanded.
        selectors: Selectors in the shared grammar, validated here even for direct callers.
        policy: Diagnostic context and whether traversable directory symlinks are refused.

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
    matched: dict[str, set[str]] = {}
    for selector in selectors:
        try:
            segments = validate_link_selector(selector)
        except ValueError as exc:
            raise policy.error_type(selector_defect_message(policy.key, selector, exc)) from exc
        try:
            found = _walk(root, segments, policy)
        except ProjectError as exc:
            if policy.refuse_symlink_directories:
                exc.add_note(
                    f"selected by {format_path_for_display(selector)}; repair the path "
                    "or narrow the selector"
                )
            raise
        if not found:
            raise policy.error_type(
                f"{policy.key} entry {format_path_for_display(selector)} matches no file "
                f"under the project root {format_path_for_display(root)}; the {policy.purpose} "
                "refuses to run over a selector that selects nothing"
            )
        for path in found:
            matched.setdefault(path, set()).add(selector)
    return tuple(
        SelectedPath(path, tuple(sorted(entries))) for path, entries in sorted(matched.items())
    )


def _scan(directory: Path, policy: SelectionPolicy = _LINK_POLICY) -> list[os.DirEntry[str]]:
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
    entry: os.DirEntry[str], policy: SelectionPolicy = _LINK_POLICY, *, traverse: bool = False
) -> bool:
    """Report whether an entry is a directory in its own right, never through a symlink.

    Raises:
        ProjectError: If the filesystem refuses the inspection. The consumer chooses its
            selection-time error type.
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
                f"{format_path_for_display(entry.path)}; "
                "use a real directory or narrow the selector"
            )
        return directory
    except OSError as exc:
        displayed = format_path_for_display(entry.path)
        msg = f"{policy.key} selection could not inspect {displayed}: {exc}"
        raise policy.error_type(msg) from exc


def _join(prefix: str, name: str) -> str:
    return name if prefix == "" else f"{prefix}{SELECTOR_SEPARATOR}{name}"


@dataclass(frozen=True, slots=True)
class _Frame:
    """One pending ``_walk`` step: match ``segments[index]`` against ``directory``.

    ``prefix`` is the project-relative spelling ``directory`` was reached by, and is what the
    memo is keyed on rather than the path, since a spelling is what the walk returns.
    ``entries`` is a listing carried from the frame that already scanned this directory, and is
    None for every step that descends into a directory nothing has read yet.
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
    policy: SelectionPolicy,
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
        entries: That directory's listing.
        last: Whether ``**`` is the selector's final segment.
        found: The selector's matched spellings, added to in place.

    Returns:
        The frames to push, children first and the ``index + 1`` handoff last.

    Raises:
        ProjectError: If the filesystem refuses to inspect an entry, a selection-time refusal.
    """
    frames: list[_Frame] = []
    for entry in entries:
        if _is_directory(entry, policy, traverse=True):
            frames.append(_Frame(Path(entry.path), _join(frame.prefix, entry.name), frame.index))
        elif last:
            found.add(_join(frame.prefix, entry.name))
    if not last:
        frames.append(_Frame(frame.directory, frame.prefix, frame.index + 1, entries))
    return frames


def _walk(root: Path, segments: tuple[str, ...], policy: SelectionPolicy) -> set[str]:
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

    Args:
        root: The resolved project root the selector is anchored to.
        segments: The validated selector segments.

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
        entries = _scan(frame.directory, policy) if frame.entries is None else frame.entries
        if segment == RECURSIVE_SEGMENT:
            pending.extend(_recursive_frames(frame, entries, last, found, policy))
            continue
        for entry in entries:
            if not segment_matches(entry.name, segment):
                continue
            if last:
                if not _is_directory(entry, policy):
                    found.add(_join(frame.prefix, entry.name))
            elif _is_directory(entry, policy, traverse=True):
                spelling = _join(frame.prefix, entry.name)
                pending.append(_Frame(Path(entry.path), spelling, frame.index + 1))
    return found
