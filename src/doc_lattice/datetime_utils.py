"""Timezone-aware datetime utilities.

This module is the only sanctioned call site for datetime.now (enforced by
tests/test_conventions.py); code needing the current time imports utc_now from here.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=UTC)
