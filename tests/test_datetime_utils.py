"""Tests for datetime utilities."""

from datetime import UTC, datetime, timedelta

from game_lattice.datetime_utils import utc_now


def test_utc_now_is_aware():
    dt = utc_now()
    assert dt.tzinfo is not None
    assert dt.tzinfo == UTC


def test_utc_now_is_current():
    assert abs(utc_now() - datetime.now(UTC)) < timedelta(seconds=5)
