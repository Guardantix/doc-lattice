"""Pure construction of the batched Linear GraphQL query and identifier partition."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .constants import BlockedReason
from .error_types import ConfigError, LinearError

BATCH_SIZE = 50
MAX_IDENTIFIERS = 500

# Anchored with \A...\Z, not ^...$: in Python ``$`` also matches just before a trailing
# newline, so a value like "PC-1\n" would slip past ``^...$``. \Z matches only the very end.
# Leading zeros are disallowed (0|[1-9][0-9]*) because the number is later normalized via
# int() to reconstruct the queried identifier as the result key; a ref like "PC-007" would
# be keyed "PC-7", so tickets.get("PC-007") would miss and produce a spurious not-found.
_IDENTIFIER_RE = re.compile(r"\A[A-Z][A-Z0-9]*-(0|[1-9][0-9]*)\Z", re.ASCII)
_TEAM_RE = re.compile(r"\A[A-Z][A-Z0-9]*\Z", re.ASCII)

# children(first: 50) caps the child tickets fetched per issue for context only; this 50 is a
# separate limit from BATCH_SIZE and the two need not stay equal.
_TICKET_FRAGMENT = """
fragment T on Issue {
  identifier
  number
  title
  url
  state { name type }
  parent { identifier title state { name type } }
  children(first: 50) { nodes { identifier title state { name type } } }
}
"""


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A built query: the document, its variables, and the team key its results belong to."""

    document: str
    variables: dict[str, str | list[int]]
    team: str


def is_valid_team_key(team: str) -> bool:
    """Return whether team matches the Linear team-key shape (uppercase alphanumeric).

    Args:
        team: A candidate Linear team key.

    Returns:
        True if team is a valid Linear team key, for example "ENG" or "PC".
    """
    return bool(_TEAM_RE.match(team))


def partition_identifiers(
    identifiers: Iterable[str], linear_team: str | None
) -> tuple[list[str], dict[str, BlockedReason]]:
    """Split identifiers into the set to query and the refs refused before any fetch.

    Args:
        identifiers: Raw ``tickets:`` values, possibly with duplicates.
        linear_team: The configured team key, or None for no team boundary.

    Returns:
        A tuple of the valid identifiers (deduplicated, order preserved) and a map of each
        refused ref to ``"malformed"`` or ``"cross-team"``.

    Raises:
        ConfigError: If ``linear_team`` is set but is not a valid team key.
        LinearError: If the distinct identifier count exceeds ``MAX_IDENTIFIERS``.
    """
    if linear_team is not None and not is_valid_team_key(linear_team):
        msg = f"linear_team {linear_team!r} is not a valid team key; fix .doc-lattice.yml"
        raise ConfigError(msg)
    distinct = list(dict.fromkeys(identifiers))
    if len(distinct) > MAX_IDENTIFIERS:
        msg = (
            f"too many referenced tickets ({len(distinct)} > {MAX_IDENTIFIERS}); "
            "narrow the scope with a positional target or --from"
        )
        raise LinearError(msg)
    valid: list[str] = []
    rejected: dict[str, BlockedReason] = {}
    for identifier in distinct:
        identifier_team = identifier.split("-", 1)[0]
        if not _IDENTIFIER_RE.match(identifier):
            rejected[identifier] = "malformed"
        elif linear_team is not None and identifier_team != linear_team:
            rejected[identifier] = "cross-team"
        else:
            valid.append(identifier)
    return valid, rejected


def group_by_team(identifiers: Sequence[str]) -> list[tuple[str, list[int]]]:
    """Group validated identifiers by team key, preserving first-seen order.

    Args:
        identifiers: Validated ``TEAM-NUMBER`` identifiers from ``partition_identifiers``.

    Returns:
        ``(team_key, numbers)`` pairs, teams in first-seen order, each team's numbers in
        first-seen order.
    """
    groups: dict[str, list[int]] = {}
    for identifier in identifiers:
        team_key, issue_number = identifier.split("-", 1)
        groups.setdefault(team_key, []).append(int(issue_number))
    return list(groups.items())


def chunk_numbers(numbers: Sequence[int], size: int = BATCH_SIZE) -> list[list[int]]:
    """Split a team's numbers into batches of at most ``size``; empty input yields ``[]``.

    Args:
        numbers: The issue numbers for one team.
        size: The maximum batch size.

    Returns:
        A list of batches; empty input yields an empty list.
    """
    return [list(numbers[i : i + size]) for i in range(0, len(numbers), size)]


def build_query(team: str, numbers: Sequence[int]) -> QueryPlan:
    """Build one filtered connection query for one team's numbers (at most ``BATCH_SIZE``).

    External API contract (verified against live Linear API 2026-06-28): a number that does
    not exist returns an empty ``nodes`` list, not a top-level ``errors`` array. This is what
    makes not-found non-fatal; a future Linear schema change there would silently reintroduce
    the batch-fatal path.

    Args:
        team: The validated team key.
        numbers: The issue numbers to fetch for that team.

    Returns:
        A QueryPlan whose document filters ``issues`` by team key and number list, passes both
        as variables (never interpolated), and records the team key for keying results.
    """
    filter_clause = (
        "filter: { team: { key: { eq: $team } }, number: { in: $numbers } },"
        # includeArchived: true so an archived completed or canceled ticket grades correctly
        # instead of being excluded (the connection defaults it to false) and misreported as
        # not-found; this tool audits shipped work, which is exactly what gets archived.
        # first matches the per-chunk cap, so all of a chunk's numbers return on one page and
        # no cursor pagination is needed; raising the chunk size without raising this would
        # silently drop results past the first page.
        f" first: {BATCH_SIZE}, includeArchived: true"
    )
    document = (
        "query Audit($team: String!, $numbers: [Float!]!) {\n"
        f"  issues({filter_clause}) {{\n"
        "    nodes { ...T }\n"
        "  }\n"
        "}\n" + _TICKET_FRAGMENT
    )
    variables: dict[str, str | list[int]] = {"team": team, "numbers": list(numbers)}
    return QueryPlan(document=document, variables=variables, team=team)
