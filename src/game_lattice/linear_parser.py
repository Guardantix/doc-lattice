"""Boundary: validate a raw Linear GraphQL response into typed tickets.

This is the only linear module permitted ``Any`` and ``cast``: it converts the untyped JSON
envelope into typed ``Ticket`` models keyed by the identifier that was queried.
"""

import json
from typing import Any

from pydantic import ValidationError

from .error_types import LinearError
from .tickets import Ticket, TicketRef, TicketState


def parse_tickets(response_text: str, team: str) -> dict[str, Ticket]:
    """Parse a filtered ``issues`` response into a ticket map keyed by queried identifier.

    Args:
        response_text: The raw response body from the transport.
        team: The validated team key the query filtered on, used to reconstruct keys.

    Returns:
        Resolved tickets keyed by ``f"{team}-{number}"`` (the queried identifier), never by the
        echoed ``identifier``. A queried number with no returned node is simply absent.

    Raises:
        LinearError: On invalid JSON, a GraphQL ``errors`` array, a missing ``data`` object, a
            malformed ``issues`` connection, or a malformed issue node.
    """
    try:
        parsed: Any = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LinearError(f"Linear response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LinearError("Linear response was not a JSON object")
    errors = parsed.get("errors")
    if errors:
        items = errors if isinstance(errors, list) else []
        messages = "; ".join(
            str(e.get("message", "<no message>")) for e in items if isinstance(e, dict)
        )
        raise LinearError(f"Linear returned GraphQL errors: {messages or '<malformed errors>'}")
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise LinearError("Linear response is missing its data object")

    issues = data.get("issues")
    if not isinstance(issues, dict) or not isinstance(issues.get("nodes"), list):
        raise LinearError("Linear response is missing its issues connection")
    nodes: list[Any] = issues["nodes"]

    tickets: dict[str, Ticket] = {}
    for node in nodes:
        # Outer guard catches int(node['number']) failures; _ticket_from_node wraps its own.
        try:
            key = f"{team}-{int(node['number'])}"
            tickets[key] = _ticket_from_node(node)
        except (KeyError, TypeError, AttributeError, ValidationError, ValueError) as exc:
            raise LinearError(f"Linear issue node was malformed: {exc}") from exc
    return tickets


def _state(raw: Any) -> TicketState:
    return TicketState(name=raw["name"], type=raw["type"])


def _ref(raw: Any) -> TicketRef:
    return TicketRef(
        identifier=raw["identifier"], title=raw.get("title"), state=_state(raw["state"])
    )


def _ticket_from_node(node: Any) -> Ticket:
    """Build a typed Ticket from one issue node, ignoring fields we did not request.

    Raises:
        LinearError: If a required field is missing or the node is malformed.
    """
    try:
        children_nodes = (node.get("children") or {}).get("nodes") or []
        parent_raw = node.get("parent")
        return Ticket(
            identifier=node["identifier"],
            title=node.get("title"),
            url=node["url"],
            state=_state(node["state"]),
            parent=_ref(parent_raw) if parent_raw else None,
            children=tuple(_ref(child) for child in children_nodes),
        )
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise LinearError(f"Linear issue node was malformed: {exc}") from exc
