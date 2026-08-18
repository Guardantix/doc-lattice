"""The package's only reader of the current wall-clock time.

AD-2 classifies this module as the narrow impure time boundary. Every timestamp the engine
records enters here, so the pure modules stay testable against fixed inputs and a test that
needs a deterministic clock has exactly one function to substitute.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime.

    Returns:
        The current time in UTC. Always timezone-aware, so a serialized timestamp carries
        its offset instead of reading as ambiguous local time on the machine that opens it.
    """
    return datetime.now(UTC)
