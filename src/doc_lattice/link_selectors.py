"""The selector grammar the source keys share: lexical validation, matching, literal escaping.

Pure and filesystem-free, so ``config`` can validate a selector at load without reaching the
walk that expands it, and ``scaffold`` can spell one for a literal root without reaching the
filesystem at all. The walk itself lives in ``link_check``.

Two config keys are written in this grammar and are matched by different means. ``link_sources``
is expanded against the filesystem by that walk, which is what turns a selector into files.
``legacy_marker_sources`` is matched against the spellings the walk already retained, by
``selector_matches_path`` here, so a compatibility declaration can never add a file to the gate;
the two therefore have to agree about what a selector means, which is why the matcher is a
sibling of the walk's own ``segment_matches`` rather than an ``fnmatch`` call at the call site.

A selector is project-relative and POSIX on every platform: ``/`` is the only separator, and a
backslash is refused rather than read as one, so a config is accepted or rejected identically
wherever it runs. Within one segment ``*``, ``?``, and bracket classes carry ``fnmatch``
semantics, case-sensitively by code point and never crossing ``/``; ``**`` is accepted only as a
whole segment and matches zero or more directories. An unclosed ``[`` is refused rather than read
as a literal, which is what ``fnmatch`` would do, because a selector that silently means something
other than what was written is how a mandatory gate ends up green over the wrong files.
"""

import re
from fnmatch import fnmatchcase
from pathlib import PureWindowsPath

from .path_utils import format_path_for_display
from .text_utils import strip_control_chars

SELECTOR_SEPARATOR = "/"
RECURSIVE_SEGMENT = "**"
# The config keys written in this grammar, named here because a diagnostic about a rejected entry
# has to say which key carried it and both keys' refusals are built by one function below.
LINK_SOURCES_KEY = "link_sources"
LEGACY_MARKER_SOURCES_KEY = "legacy_marker_sources"
_DOT_SEGMENTS = frozenset({".", ".."})
# One pass over the text, because each replacement introduces a bracket of its own: a sequence
# of per-character replacements is correct only while ``[`` is handled first, and that ordering
# is an invariant nothing enforces. A single substitution never revisits what it wrote.
_LITERAL_METACHARACTER = re.compile(r"([*?\[])")


def validate_link_selector(entry: str) -> tuple[str, ...]:
    """Return a selector's segments, or raise ``ValueError`` naming the first defect.

    The message is a predicate about the entry with no subject, such as ``"contains a
    backslash; '/' is the only separator"``, so a caller can prefix it with however it spells
    the entry.

    Args:
        entry: One ``link_sources`` entry as written.

    Returns:
        The segments between separators, at least one.

    Raises:
        ValueError: If the entry is empty, carries a control character or a backslash, is
            absolute or drive-prefixed, ends in a separator, has an empty or dot segment,
            spells ``**`` inside a segment, or leaves a bracket class unclosed.
    """
    if not entry:
        msg = "is empty"
        raise ValueError(msg)
    if strip_control_chars(entry) != entry:
        msg = "contains a control character"
        raise ValueError(msg)
    if "\\" in entry:
        msg = "contains a backslash; '/' is the only separator"
        raise ValueError(msg)
    if entry.startswith(SELECTOR_SEPARATOR) or PureWindowsPath(entry).drive:
        msg = "is absolute; a selector is relative to the project root"
        raise ValueError(msg)
    if entry.endswith(SELECTOR_SEPARATOR):
        msg = "ends in a separator; a selector names files, not a directory"
        raise ValueError(msg)
    segments = tuple(entry.split(SELECTOR_SEPARATOR))
    for segment in segments:
        if segment == "":
            msg = "has an empty segment"
            raise ValueError(msg)
        if segment in _DOT_SEGMENTS:
            msg = "has a '.' or '..' segment"
            raise ValueError(msg)
        if RECURSIVE_SEGMENT in segment and segment != RECURSIVE_SEGMENT:
            msg = "spells '**' inside a segment; it is accepted only as a whole segment"
            raise ValueError(msg)
        if _has_unclosed_bracket(segment):
            msg = "leaves a '[' bracket class unclosed"
            raise ValueError(msg)
    return segments


def selector_defect_message(key: str, entry: str, defect: ValueError) -> str:
    """Return the user-facing diagnostic for one rejected selector entry.

    ``validate_link_selector`` raises a subjectless predicate, so the subject is supplied here
    rather than at each call site. Both callers that reject an entry -- config load and the
    selection walk -- report the same defect about the same value, and a reader who meets one
    message should not have to recognize the other as the same refusal.

    ``key`` is a parameter rather than the literal ``link_sources`` because a second key is
    written in this grammar: a compatibility entry rejected under the name of the key that did
    not carry it sends the reader to the wrong list to repair it.

    Args:
        key: The config key the entry was written under.
        entry: The entry as written.
        defect: The ``ValueError`` ``validate_link_selector`` raised for it.

    Returns:
        The full diagnostic, ready to carry whatever error type the caller raises.
    """
    return f"{key} entry {format_path_for_display(entry)} {defect}"


def _has_unclosed_bracket(segment: str) -> bool:
    """Report whether a ``[`` in the segment never finds the ``]`` that closes it.

    Mirrors the scan ``fnmatch.translate`` performs: an optional ``!`` may follow the ``[``, and a
    ``]`` in the first position after that is a member of the class rather than its close.
    """
    index = 0
    end = len(segment)
    while index < end:
        if segment[index] != "[":
            index += 1
            continue
        cursor = index + 1
        if cursor < end and segment[cursor] == "!":
            cursor += 1
        if cursor < end and segment[cursor] == "]":
            cursor += 1
        while cursor < end and segment[cursor] != "]":
            cursor += 1
        if cursor >= end:
            return True
        index = cursor + 1
    return False


def segment_matches(name: str, pattern: str) -> bool:
    """Report whether one directory entry name matches one non-recursive selector segment.

    Args:
        name: A single entry name, with no separator in it.
        pattern: One validated selector segment other than ``**``.

    Returns:
        True when ``fnmatch`` matches them case-sensitively.
    """
    return fnmatchcase(name, pattern)


def _recursive_closure(positions: set[int], segments: tuple[str, ...]) -> set[int]:
    """Return ``positions`` plus every position reachable by consuming no directory.

    ``**`` matches zero or more directories, so a position on one is also a position on the
    segment after it. Adjacent ``**`` segments are valid grammar, so the step is taken to a
    fixpoint rather than once. The last segment is never stepped past: a position beyond it has
    consumed the whole selector and can match nothing further.
    """
    end = len(segments) - 1
    closed = set(positions)
    pending = list(positions)
    while pending:
        position = pending.pop()
        if segments[position] != RECURSIVE_SEGMENT or position >= end:
            continue
        if position + 1 not in closed:
            closed.add(position + 1)
            pending.append(position + 1)
    return closed


def selector_matches_path(segments: tuple[str, ...], path: str) -> bool:
    """Report whether one validated selector matches one project-relative POSIX spelling.

    The lexical half of what ``link_check``'s walk does against the filesystem, and it has to
    agree with it: the selector is anchored at the project root, every part but the last is a
    directory the walk would have descended into, and only the last part can satisfy the last
    segment. ``**`` consumes zero or more directories, and as the final segment it also matches
    the file itself, which is what makes ``docs/**`` cover everything beneath ``docs``.

    Positions are advanced as a set rather than by backtracking, so the cost is the parts times
    the segments and the interpreter's stack is never spent: matching is reached from a mandatory
    gate, and a repository deeper than the recursion limit is the case the walk already keeps its
    own stack for. Recursing here would put that ceiling back on the same paths from the other
    side.

    Args:
        segments: The selector's segments, already through ``validate_link_selector``.
        path: A project-relative POSIX spelling naming a file, with ``/`` as its only separator.

    Returns:
        True when the selector matches that spelling.
    """
    parts = path.split(SELECTOR_SEPARATOR)
    end = len(segments) - 1
    positions = {0}
    for part in parts[:-1]:
        advanced: set[int] = set()
        for position in _recursive_closure(positions, segments):
            if segments[position] == RECURSIVE_SEGMENT:
                advanced.add(position)
            elif position < end and segment_matches(part, segments[position]):
                advanced.add(position + 1)
        if not advanced:
            return False
        positions = advanced
    return any(
        position == end
        and (
            segments[position] == RECURSIVE_SEGMENT
            or segment_matches(parts[-1], segments[position])
        )
        for position in _recursive_closure(positions, segments)
    )


def escape_selector_literal(text: str) -> str:
    """Return ``text`` spelled so the grammar reads every character literally.

    Args:
        text: A path or path fragment meant as itself, not as a pattern.

    Returns:
        The text with ``[``, ``*``, and ``?`` each wrapped in a one-member bracket class.
    """
    return _LITERAL_METACHARACTER.sub(r"[\1]", text)
