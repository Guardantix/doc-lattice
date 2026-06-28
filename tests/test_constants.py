"""Tests for constants."""

from typing import get_args

from game_lattice.constants import (
    VALID_AUTHORITIES,
    VALID_EDGE_STATES,
    VALID_LAYERS,
    VALID_STATUSES,
    Authority,
    EdgeState,
    Layer,
    Status,
)


def test_valid_statuses_matches_literal():
    assert frozenset(get_args(Status)) == VALID_STATUSES


def test_invalid_value_not_in_set():
    assert "deleted" not in VALID_STATUSES


def test_layers_match_literal():
    assert frozenset(get_args(Layer)) == VALID_LAYERS
    assert "design" in VALID_LAYERS


def test_authorities_match_literal():
    assert frozenset(get_args(Authority)) == VALID_AUTHORITIES
    assert "binding" in VALID_AUTHORITIES


def test_edge_states_match_literal():
    assert frozenset(get_args(EdgeState)) == VALID_EDGE_STATES
    assert {"OK", "STALE", "UNRECONCILED", "BROKEN"} == set(VALID_EDGE_STATES)
