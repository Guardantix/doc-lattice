"""Tests for constants."""

import re
from typing import get_args

from doc_lattice.constants import (
    AUTHORITY_LADDER,
    CHECKOUT_REF,
    CHECKOUT_USES,
    CHECKOUT_VERSION,
    COMMENT_ENVELOPE_CLOSE,
    COMMENT_ENVELOPE_OPEN,
    EDGE_STATES,
    SETUP_UV_REF,
    SETUP_UV_USES,
    SETUP_UV_VERSION,
    VALID_AUTHORITIES,
    VALID_BLOCKED_REASONS,
    VALID_EDGE_STATES,
    VALID_ENVELOPE_KINDS,
    VALID_FRONTMATTER_DISPOSITIONS,
    VALID_LAYERS,
    VALID_LINEAR_STATE_TYPES,
    VALID_LOCATION_KINDS,
    VALID_REPORT_FORMATS,
    VALID_SEVERITIES,
    VALID_SKIP_REASONS,
    VALID_YAML_PARSERS,
    Authority,
    BlockedReason,
    EdgeState,
    EnvelopeKind,
    FrontmatterDisposition,
    Layer,
    LinearStateType,
    LocationKind,
    ReportFormat,
    Severity,
    SkipReason,
    YamlParser,
)


def test_layers_match_literal():
    assert frozenset(get_args(Layer)) == VALID_LAYERS
    assert {"design", "technical", "production"} == set(VALID_LAYERS)


def test_authorities_match_literal():
    assert frozenset(get_args(Authority)) == VALID_AUTHORITIES
    assert "binding" in VALID_AUTHORITIES


def test_edge_states_match_literal():
    assert frozenset(get_args(EdgeState)) == VALID_EDGE_STATES
    assert {"OK", "STALE", "UNRECONCILED", "BROKEN"} == set(VALID_EDGE_STATES)


def test_edge_states_keep_literal_declaration_order():
    # Report output iterates EDGE_STATES, so its order is a user-visible contract and must
    # come from the Literal rather than from the unordered VALID_EDGE_STATES frozenset.
    assert get_args(EdgeState) == EDGE_STATES
    assert EDGE_STATES == ("OK", "STALE", "UNRECONCILED", "BROKEN")


def test_location_kinds_match_literal():
    assert frozenset(get_args(LocationKind)) == VALID_LOCATION_KINDS
    assert {"file", "section"} == set(VALID_LOCATION_KINDS)


def test_frontmatter_dispositions_match_literal():
    assert frozenset(get_args(FrontmatterDisposition)) == VALID_FRONTMATTER_DISPOSITIONS
    assert {"tracked", "untracked", "id-less", "misplaced-envelope"} == set(
        VALID_FRONTMATTER_DISPOSITIONS
    )


def test_yaml_parsers_match_literal():
    assert frozenset(get_args(YamlParser)) == VALID_YAML_PARSERS
    assert {"pure", "platform-default"} == set(VALID_YAML_PARSERS)


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


def test_action_refs_are_approved_full_commit_shas():
    # The pin values themselves, asserted where constants.py's invariants live. A floating tag
    # re-resolves on every run, which is exactly what pinning by commit exists to prevent, so
    # both halves are checked here once rather than at each renderer that emits them.
    assert CHECKOUT_REF == "3d3c42e5aac5ba805825da76410c181273ba90b1"  # pragma: allowlist secret
    assert SETUP_UV_REF == "20cfd1bf945f4377ade1205e4dbc17946fc9a30d"  # pragma: allowlist secret
    assert re.fullmatch(r"[0-9a-f]{40}", CHECKOUT_REF) is not None
    assert re.fullmatch(r"[0-9a-f]{40}", SETUP_UV_REF) is not None
    assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", CHECKOUT_VERSION) is not None
    assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", SETUP_UV_VERSION) is not None


def test_composed_uses_fragments_join_both_halves():
    assert f"actions/checkout@{CHECKOUT_REF} # {CHECKOUT_VERSION}" == CHECKOUT_USES
    assert f"astral-sh/setup-uv@{SETUP_UV_REF} # {SETUP_UV_VERSION}" == SETUP_UV_USES


def test_envelope_kind_domain_is_derived_from_the_literal():
    assert frozenset(get_args(EnvelopeKind)) == VALID_ENVELOPE_KINDS
    assert {"fence", "comment"} == VALID_ENVELOPE_KINDS
    assert COMMENT_ENVELOPE_OPEN == "<!-- doc-lattice"
    assert COMMENT_ENVELOPE_CLOSE == "-->"
