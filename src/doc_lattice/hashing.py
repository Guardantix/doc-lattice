"""Canonicalize section content and compute its content hash."""

import hashlib

# 128 bits (32 hex chars): collision-safe for any realistic doc corpus while keeping the
# stored ``seen`` scalar short and readable in frontmatter. See the local-core design spec
# section 5 for why the full SHA-256 digest is truncated rather than stored whole.
_HASH_HEX_LEN = 32


def normalize_newlines(text: str) -> str:
    """Collapse CRLF and lone CR line endings to LF.

    Args:
        text: Raw text with any mix of line endings.

    Returns:
        ``text`` with every ``\\r\\n`` and lone ``\\r`` replaced by ``\\n``.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def canonicalize(text: str) -> str:
    """Normalize content so cosmetic edits do not change the hash.

    Args:
        text: Raw section or file content.

    Returns:
        Line endings normalized to ``\\n``, trailing whitespace stripped per line,
        and leading and trailing blank lines trimmed. Internal blank lines are kept.
    """
    unified = normalize_newlines(text)
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
