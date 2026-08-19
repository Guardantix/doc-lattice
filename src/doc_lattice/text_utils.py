"""Small pure text helpers, and the project's one definition of a control character.

``strip_control_chars`` stays scoped to the network-sourced Linear data and ``init`` input it
was written for (AD-34); the two predicates beside it answer the same range question for a
caller that refuses text rather than cleaning it (AD-35). One range, three helpers, so a change
to what counts as a control character cannot move one of them and leave the others behind.
"""

from .constants import ASCII_DELETE, ASCII_PRINTABLE_MIN, C1_CONTROL_MAX, C1_CONTROL_MIN


def strip_control_chars(text: str) -> str:
    """Remove control bytes so untrusted strings cannot corrupt terminal output.

    Args:
        text: Any string, possibly from a repo or a network response.

    Returns:
        The text with every C0 control (below ``0x20``), DEL (``0x7F``), and C1 control
        (``0x80`` to ``0x9F``) code point removed. C1 controls are stripped because bytes
        such as ``0x9B`` (single-byte CSI) and ``0x85`` (NEL) still drive 8-bit terminals.
        Ordinary printable characters, including non-ASCII letters, are preserved.
    """
    return "".join(ch for ch in text if not is_control_char(ch))


def is_control_char(char: str) -> bool:
    """Report whether one character is a terminal control character.

    Args:
        char: A single character.

    Returns:
        True for a C0 control (below ``0x20``), DEL (``0x7F``), or a C1 control (``0x80``
        to ``0x9F``). Tab, newline, and carriage return are C0 controls and are included.
    """
    code = ord(char)
    return (
        code < ASCII_PRINTABLE_MIN
        or code == ASCII_DELETE
        or C1_CONTROL_MIN <= code <= C1_CONTROL_MAX
    )


def first_control_index(text: str) -> int | None:
    """Locate the first control character in a string.

    A caller rejecting untrusted text needs the position rather than the character, because a
    diagnostic naming the offending value would print the very byte it is refusing. Returning
    the index lets the caller spell the code point as ``U+XXXX`` instead.

    Args:
        text: Any string, possibly repo-controlled.

    Returns:
        The index of the first character ``is_control_char`` accepts, or None when the string
        holds none.
    """
    for index, char in enumerate(text):
        if is_control_char(char):
            return index
    return None
