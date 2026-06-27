"""Tests for constants."""

from typing import get_args

from game_lattice.constants import VALID_STATUSES, Status


def test_valid_statuses_matches_literal():
    assert frozenset(get_args(Status)) == VALID_STATUSES


def test_invalid_value_not_in_set():
    assert "deleted" not in VALID_STATUSES
