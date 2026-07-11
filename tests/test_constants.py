"""Tests for constants."""

from typing import get_args

from doc_lattice.constants import (
    AUTHORITY_LADDER,
    VALID_AUTHORITIES,
    VALID_BLOCKED_REASONS,
    VALID_EDGE_STATES,
    VALID_LAYERS,
    VALID_LINEAR_STATE_TYPES,
    VALID_LOCATION_KINDS,
    VALID_REPORT_FORMATS,
    VALID_SEVERITIES,
    VALID_SKIP_REASONS,
    VALID_STATUSES,
    Authority,
    BlockedReason,
    EdgeState,
    Layer,
    LinearStateType,
    LocationKind,
    ReportFormat,
    Severity,
    SkipReason,
    Status,
)


def test_valid_statuses_matches_literal():
    assert frozenset(get_args(Status)) == VALID_STATUSES
    assert {"active", "inactive"} == set(VALID_STATUSES)


def test_invalid_value_not_in_set():
    assert "deleted" not in VALID_STATUSES


def test_layers_match_literal():
    assert frozenset(get_args(Layer)) == VALID_LAYERS
    assert {"design", "technical", "production"} == set(VALID_LAYERS)


def test_authorities_match_literal():
    assert frozenset(get_args(Authority)) == VALID_AUTHORITIES
    assert "binding" in VALID_AUTHORITIES


def test_edge_states_match_literal():
    assert frozenset(get_args(EdgeState)) == VALID_EDGE_STATES
    assert {"OK", "STALE", "UNRECONCILED", "BROKEN"} == set(VALID_EDGE_STATES)


def test_location_kinds_match_literal():
    assert frozenset(get_args(LocationKind)) == VALID_LOCATION_KINDS
    assert {"file", "section"} == set(VALID_LOCATION_KINDS)


def test_report_formats_match_literal():
    assert frozenset(get_args(ReportFormat)) == VALID_REPORT_FORMATS
    assert {"human", "json", "github"} == set(VALID_REPORT_FORMATS)


def test_linear_state_types_match_literal():
    assert frozenset(get_args(LinearStateType)) == VALID_LINEAR_STATE_TYPES
    assert {
        "triage",
        "backlog",
        "unstarted",
        "started",
        "completed",
        "canceled",
        "duplicate",
    } == set(VALID_LINEAR_STATE_TYPES)


def test_severities_match_literal():
    assert frozenset(get_args(Severity)) == VALID_SEVERITIES
    assert {"DANGER", "WARNING", "INFO", "BLOCKED"} == set(VALID_SEVERITIES)


def test_blocked_reasons_match_literal():
    assert frozenset(get_args(BlockedReason)) == VALID_BLOCKED_REASONS
    assert {"malformed", "not-found", "cross-team"} == set(VALID_BLOCKED_REASONS)


def test_authority_ladder_covers_every_authority():
    assert frozenset(AUTHORITY_LADDER) == VALID_AUTHORITIES


def test_authority_ladder_is_ordered_weak_to_strong():
    assert AUTHORITY_LADDER == ("exploratory", "derived", "binding")


def test_skip_reasons_match_literal():
    assert frozenset(get_args(SkipReason)) == VALID_SKIP_REASONS
    assert {"source-unannotated", "target-unannotated"} == set(VALID_SKIP_REASONS)
