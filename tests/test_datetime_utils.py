"""Tests for datetime utilities."""

from datetime import UTC, datetime, timedelta

import pytest

from game_lattice.datetime_utils import format_iso, local_now, parse_iso, utc_now


def test_local_now_is_aware():
    dt = local_now()
    assert dt.tzinfo is not None


def test_local_now_matches_utc_instant():
    # same instant, different zone representation
    assert abs(local_now() - utc_now()) < timedelta(seconds=5)


def test_utc_now_is_aware():
    dt = utc_now()
    assert dt.tzinfo is not None
    assert dt.tzinfo == UTC


def test_utc_now_is_current():
    assert abs(utc_now() - datetime.now(UTC)) < timedelta(seconds=5)


def test_parse_format_roundtrip():
    dt = utc_now()
    s = format_iso(dt)
    parsed = parse_iso(s)
    assert parsed == dt


def test_parse_iso_naive_defaults_to_utc():
    dt = parse_iso("2026-06-29T12:00:00")
    assert dt.tzinfo == UTC
    assert dt.utcoffset() == timedelta(0)
    # wall-clock components preserved, not shifted
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 6, 29, 12)


def test_parse_iso_preserves_explicit_offset():
    dt = parse_iso("2026-06-29T12:00:00+05:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=5)


def test_parse_iso_raises_on_invalid():
    with pytest.raises(ValueError, match="Invalid isoformat"):
        parse_iso("not-a-date")
