"""Impure wiring: turn referenced identifiers into a resolved ticket map."""

from collections.abc import Iterable

from .constants import BlockedReason
from .linear_client import LinearClient
from .linear_parser import parse_tickets
from .linear_query import (
    BATCH_SIZE,
    build_query,
    chunk_numbers,
    group_by_team,
    partition_identifiers,
)
from .tickets import Ticket


def fetch_tickets(
    identifiers: Iterable[str],
    linear_team: str | None,
    client: LinearClient | None = None,
) -> tuple[dict[str, Ticket], dict[str, BlockedReason]]:
    """Resolve referenced identifiers against Linear.

    Args:
        identifiers: Raw ``tickets:`` values collected from trigger nodes.
        linear_team: The configured team key, or None.
        client: An injected client for tests; the default is a real ``LinearClient``.

    Returns:
        A tuple of the resolved tickets keyed by queried identifier and the ``rejected`` map
        of refs refused before any fetch. When no valid identifier remains, the network is
        not touched and ``LINEAR_API_KEY`` is not read.
    """
    valid, rejected = partition_identifiers(identifiers, linear_team)
    if not valid:
        return {}, rejected
    live = client if client is not None else LinearClient()
    tickets: dict[str, Ticket] = {}
    for team, numbers in group_by_team(valid):
        for chunk in chunk_numbers(numbers, BATCH_SIZE):
            plan = build_query(team, chunk)
            body = live.execute(plan.document, plan.variables)
            tickets.update(parse_tickets(body, plan.team))
    return tickets, rejected
