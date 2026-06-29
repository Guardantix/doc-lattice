"""Timezone-aware datetime utilities."""

from datetime import UTC, datetime


def local_now() -> datetime:
    """Return the current local time as a timezone-aware datetime."""
    return datetime.now(tz=UTC).astimezone()


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=UTC)


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string to a timezone-aware datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def format_iso(dt: datetime) -> str:
    """Format a datetime as ISO 8601 string."""
    return dt.isoformat()
