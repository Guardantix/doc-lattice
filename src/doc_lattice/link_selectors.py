"""The ``link_sources`` selector grammar: lexical validation, segment matching, literal escaping.

Pure and filesystem-free, so ``config`` can validate a selector at load without reaching the
walk that expands it, and ``scaffold`` can spell one for a literal root without reaching the
filesystem at all. The walk itself lives in ``link_check``.

A selector is project-relative and POSIX on every platform: ``/`` is the only separator, and a
backslash is refused rather than read as one, so a config is accepted or rejected identically
wherever it runs. Within one segment ``*``, ``?``, and bracket classes carry ``fnmatch``
semantics, case-sensitively by code point and never crossing ``/``; ``**`` is accepted only as a
whole segment and matches zero or more directories. An unclosed ``[`` is refused rather than read
as a literal, which is what ``fnmatch`` would do, because a selector that silently means something
other than what was written is how a mandatory gate ends up green over the wrong files.
"""

from fnmatch import fnmatchcase
from pathlib import PureWindowsPath

from .text_utils import strip_control_chars

SELECTOR_SEPARATOR = "/"
RECURSIVE_SEGMENT = "**"
_DOT_SEGMENTS = frozenset({".", ".."})
# ``[`` first: the later two introduce brackets of their own that must not be re-escaped.
_LITERAL_ESCAPES = (("[", "[[]"), ("*", "[*]"), ("?", "[?]"))


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


def escape_selector_literal(text: str) -> str:
    """Return ``text`` spelled so the grammar reads every character literally.

    Args:
        text: A path or path fragment meant as itself, not as a pattern.

    Returns:
        The text with ``[``, ``*``, and ``?`` each wrapped in a one-member bracket class.
    """
    for character, escaped in _LITERAL_ESCAPES:
        text = text.replace(character, escaped)
    return text
