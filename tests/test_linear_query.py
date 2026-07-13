"""Tests for the linear query builder and identifier partition."""

import pytest

from doc_lattice.error_types import ConfigError, LinearError
from doc_lattice.linear_query import (
    BATCH_SIZE,
    CHILD_TICKET_LIMIT,
    MAX_IDENTIFIERS,
    build_query,
    chunk_numbers,
    group_by_team,
    is_valid_team_key,
    partition_identifiers,
)


def test_partition_splits_valid_and_malformed():
    valid, rejected = partition_identifiers(["PC-228", "not-a-ticket", "", "  "], None)
    assert valid == ["PC-228"]
    assert rejected == {"not-a-ticket": "malformed", "": "malformed", "  ": "malformed"}


def test_partition_dedupes_preserving_order():
    valid, _ = partition_identifiers(["PC-2", "PC-1", "PC-2"], None)
    assert valid == ["PC-2", "PC-1"]


def test_partition_rejects_non_ascii_digits():
    # Arabic-Indic digits must not pass the ASCII identifier guard.
    valid, rejected = partition_identifiers(["PC-٢٣"], None)
    assert valid == []
    assert rejected["PC-٢٣"] == "malformed"


def test_partition_tags_cross_team_when_team_set():
    valid, rejected = partition_identifiers(["PC-1", "SEC-9"], "PC")
    assert valid == ["PC-1"]
    assert rejected == {"SEC-9": "cross-team"}


def test_partition_tags_malformed_over_cross_team_when_team_set():
    # Malformed is checked before cross-team, so a malformed ref stays malformed even
    # with a team boundary active; swapping the if/elif order would mis-tag "bad".
    valid, rejected = partition_identifiers(["bad", "SEC-9"], "PC")
    assert valid == []
    # "bad" is malformed (not cross-team); "SEC-9" is well-formed but off-team.
    assert rejected == {"bad": "malformed", "SEC-9": "cross-team"}


def test_partition_queries_off_team_when_no_team_set():
    valid, rejected = partition_identifiers(["SEC-9"], None)
    assert valid == ["SEC-9"]
    assert rejected == {}


def test_partition_rejects_malformed_team_key():
    with pytest.raises(ConfigError):
        partition_identifiers(["PC-1"], "p c")


def test_partition_rejects_trailing_newline_team_key():
    # A trailing newline must not slip past the team-key guard (\Z, not $).
    with pytest.raises(ConfigError):
        partition_identifiers(["PC-1"], "PC\n")


def test_partition_rejects_trailing_newline_identifier():
    # A trailing newline must not slip past the identifier guard (\Z, not $).
    valid, rejected = partition_identifiers(["PC-1\n"], None)
    assert valid == []
    assert rejected == {"PC-1\n": "malformed"}


def test_partition_rejects_leading_zero_numbers():
    # Leading zeros are disallowed: "PC-007" would be keyed "PC-7" after int(), so
    # tickets.get("PC-007") would miss and produce a spurious not-found result.
    valid, rejected = partition_identifiers(["PC-007", "PC-01"], None)
    assert valid == []
    assert rejected["PC-007"] == "malformed"
    assert rejected["PC-01"] == "malformed"
    # PC-0 (exactly zero, no leading zero) and PC-7 (no leading zero) remain valid.
    valid_no_leading_zero, rejected_no_leading_zero = partition_identifiers(["PC-0", "PC-7"], None)
    assert valid_no_leading_zero == ["PC-0", "PC-7"]
    assert rejected_no_leading_zero == {}


def test_partition_cap_raises_one_over_but_not_at_cap():
    at_cap = [f"PC-{i}" for i in range(MAX_IDENTIFIERS)]
    valid, _ = partition_identifiers(at_cap, None)
    assert len(valid) == MAX_IDENTIFIERS
    over = [f"PC-{i}" for i in range(MAX_IDENTIFIERS + 1)]
    with pytest.raises(LinearError):
        partition_identifiers(over, None)


def test_partition_cap_counts_distinct_not_raw():
    # The cap is applied after dedup, so many copies of one ref collapse before the
    # count and must not raise even when the raw input far exceeds MAX_IDENTIFIERS.
    flooded = ["PC-1"] * (MAX_IDENTIFIERS + 5)
    valid, rejected = partition_identifiers(flooded, None)
    assert valid == ["PC-1"]
    assert rejected == {}


def test_group_by_team_preserves_first_seen_order():
    result = group_by_team(["PC-2", "PC-1", "SEC-9"])
    assert result == [("PC", [2, 1]), ("SEC", [9])]


def test_chunk_numbers_boundary():
    exactly = list(range(BATCH_SIZE))
    assert len(chunk_numbers(exactly)) == 1
    one_more = list(range(BATCH_SIZE + 1))
    assert len(chunk_numbers(one_more)) == 2


def test_chunk_numbers_preserves_contents_across_boundary():
    # Length alone is not enough: a full first chunk must spill its remainder to a second
    # chunk with no element dropped, reordered, or duplicated.
    numbers = list(range(BATCH_SIZE + 3))
    chunks = chunk_numbers(numbers)
    assert chunks[0] == list(range(BATCH_SIZE))
    assert chunks[1] == [BATCH_SIZE, BATCH_SIZE + 1, BATCH_SIZE + 2]
    assert [n for c in chunks for n in c] == numbers


def test_chunk_numbers_empty_yields_empty():
    assert chunk_numbers([]) == []


def test_build_query_filter_shape():
    plan = build_query("PC", [1, 2])
    # Connection shape, not alias shape
    assert "issues(filter:" in plan.document
    assert "mutation" not in plan.document
    # Archived issues are included so an archived completed/canceled ticket grades correctly.
    assert "includeArchived: true" in plan.document
    # Variables carry the team and numbers; neither is interpolated into the document
    assert plan.variables == {"team": "PC", "numbers": [1, 2]}
    assert plan.team == "PC"
    # Document declares both variables
    assert "$team: String!" in plan.document
    assert "$numbers: [Float!]!" in plan.document
    # Filter references both variables by name
    assert "team: { key: { eq: $team } }" in plan.document
    assert "number: { in: $numbers }" in plan.document
    # Fragment carries number as a top-level field for keying results
    assert "\n  number\n" in plan.document


def test_build_query_caps_first_at_batch_size():
    plan = build_query("PC", [1, 2])
    issues_line = next(
        (line for line in plan.document.splitlines() if line.strip().startswith("issues(")),
        None,
    )
    assert issues_line is not None, "Query document is missing the 'issues(' line"
    children_line = next(
        (line for line in plan.document.splitlines() if line.strip().startswith("children(")),
        None,
    )
    assert children_line is not None, "Query document is missing the 'children(' line"
    # first must equal BATCH_SIZE so a full chunk returns on one page (no pagination).
    assert f"first: {BATCH_SIZE}" in issues_line
    assert f"children(first: {CHILD_TICKET_LIMIT})" in children_line


@pytest.mark.parametrize(
    ("team", "expected"),
    [
        ("PC", True),
        ("ENG2", True),
        ("P", True),
        ("pc", False),
        ("2PC", False),
        ("P-C", False),
        ("", False),
    ],
)
def test_is_valid_team_key_shapes(team, expected):
    # Leading digit, lowercase, embedded hyphen, and empty are all rejected; a single
    # uppercase letter and trailing digits are accepted.
    assert is_valid_team_key(team) is expected
