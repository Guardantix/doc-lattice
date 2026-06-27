"""Tests for datetime utilities."""

from datetime import UTC

import pytest

from game_lattice.datetime_utils import format_iso, local_now, parse_iso, utc_now


def test_local_now_is_aware():
    dt = local_now()
    assert dt.tzinfo is not None


def test_utc_now_is_aware():
    dt = utc_now()
    assert dt.tzinfo is not None
    assert dt.tzinfo == UTC


def test_parse_format_roundtrip():
    dt = utc_now()
    s = format_iso(dt)
    parsed = parse_iso(s)
    assert parsed == dt


def test_parse_iso_raises_on_invalid():
    with pytest.raises(ValueError, match="Invalid isoformat"):
        parse_iso("not-a-date")
