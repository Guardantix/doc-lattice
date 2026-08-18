"""Tests for the package's single current-time boundary."""

from datetime import UTC, datetime, timedelta

from doc_lattice.datetime_utils import utc_now


def test_utc_now_returns_an_aware_utc_instant():
    before = datetime.now(UTC)
    observed = utc_now()
    after = datetime.now(UTC)

    assert observed.tzinfo is not None
    assert observed.utcoffset() == timedelta(0)
    assert before <= observed <= after


def test_utc_now_serializes_with_an_explicit_offset():
    """A journal timestamp must not read as ambiguous local time on the machine that opens it."""
    rendered = utc_now().isoformat()

    assert rendered.endswith("+00:00")
    assert datetime.fromisoformat(rendered).tzinfo is not None
