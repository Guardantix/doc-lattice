"""Canonicalize section content and compute its content hash."""

import hashlib

_HASH_HEX_LEN = 32


def canonicalize(text: str) -> str:
    """Normalize content so cosmetic edits do not change the hash.

    Args:
        text: Raw section or file content.

    Returns:
        Line endings normalized to ``\\n``, trailing whitespace stripped per line,
        and leading and trailing blank lines trimmed. Internal blank lines are kept.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    start = 0
    end = len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return "\n".join(lines[start:end])


def content_hash(text: str) -> str:
    """Return the 128-bit (32 hex char) SHA-256 hash of the canonicalized text.

    Args:
        text: Raw section or file content.

    Returns:
        The first 32 hex characters of ``sha256(canonicalize(text))``.
    """
    canonical = canonicalize(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_HASH_HEX_LEN]
