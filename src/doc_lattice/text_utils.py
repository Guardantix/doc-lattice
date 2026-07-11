"""Small pure text helpers shared across the linear slice."""

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
    return "".join(
        ch
        for ch in text
        if (code := ord(ch)) >= ASCII_PRINTABLE_MIN
        and code != ASCII_DELETE
        and not (C1_CONTROL_MIN <= code <= C1_CONTROL_MAX)
    )
