"""Tests for the Linear response parser boundary."""

import json

import pytest

from game_lattice.error_types import LinearError
from game_lattice.linear_parser import parse_tickets


def _issue(identifier="PC-1", state_type="completed"):
    return {
        "identifier": identifier,
        "title": "Accent",
        "url": "https://linear.app/acme/issue/" + identifier,
        "state": {"name": "Done", "type": state_type},
        "parent": None,
        "children": {
            "nodes": [
                {
                    "identifier": "PC-9",
                    "title": "Sub",
                    "state": {"name": "Doing", "type": "started"},
                }
            ]
        },
    }


def test_parses_ticket_with_children():
    text = json.dumps({"data": {"i0": _issue()}})
    tickets, unresolved = parse_tickets(text, {"i0": "PC-1"})
    assert unresolved == set()
    ticket = tickets["PC-1"]
    assert ticket.url.endswith("PC-1")
    assert tickets["PC-1"].children[0].identifier == "PC-9"


def test_keys_by_queried_id_not_echo():
    # Linear echoes a lowercased identifier; the map must still be keyed by what we queried.
    echo = _issue(identifier="pc-1")
    text = json.dumps({"data": {"i0": echo}})
    tickets, _ = parse_tickets(text, {"i0": "PC-1"})
    assert "PC-1" in tickets
    assert "pc-1" not in tickets


def test_null_issue_is_unresolved():
    text = json.dumps({"data": {"i0": None}})
    tickets, unresolved = parse_tickets(text, {"i0": "PC-404"})
    assert tickets == {}
    assert unresolved == {"PC-404"}


def test_unknown_extra_field_ignored():
    issue = _issue()
    issue["surprise"] = "new linear field"
    text = json.dumps({"data": {"i0": issue}})
    tickets, _ = parse_tickets(text, {"i0": "PC-1"})
    assert tickets["PC-1"].identifier == "PC-1"


def test_control_chars_stripped_from_url_and_identifier():
    issue = _issue()
    issue["url"] = "https://x/\x1bPC-1"
    text = json.dumps({"data": {"i0": issue}})
    tickets, _ = parse_tickets(text, {"i0": "PC-1"})
    assert "\x1b" not in tickets["PC-1"].url


def test_duplicate_state_type_parses():
    # Linear's "Duplicate" is its own state category, distinct from canceled. It must parse
    # (not fail the whole audit with exit 2); stale_shipped omits it as terminal.
    issue = _issue(state_type="duplicate")
    issue["parent"] = {
        "identifier": "PC-7",
        "title": "Parent",
        "state": {"name": "Dup", "type": "duplicate"},
    }
    text = json.dumps({"data": {"i0": issue}})
    tickets, _ = parse_tickets(text, {"i0": "PC-1"})
    assert tickets["PC-1"].state.type == "duplicate"
    assert tickets["PC-1"].parent is not None
    assert tickets["PC-1"].parent.state.type == "duplicate"


def test_graphql_errors_raise():
    text = json.dumps({"errors": [{"message": "rate limited"}]})
    with pytest.raises(LinearError):
        parse_tickets(text, {"i0": "PC-1"})


def test_missing_data_raises():
    with pytest.raises(LinearError):
        parse_tickets(json.dumps({"meta": 1}), {"i0": "PC-1"})


def test_invalid_json_raises():
    with pytest.raises(LinearError):
        parse_tickets("not json", {"i0": "PC-1"})


def test_malformed_issue_raises():
    text = json.dumps({"data": {"i0": {"identifier": "PC-1"}}})  # missing url/state
    with pytest.raises(LinearError):
        parse_tickets(text, {"i0": "PC-1"})


def test_non_list_errors_raise_linear_error():
    for bad in (json.dumps({"errors": 42}), json.dumps({"errors": "rate limited"})):
        with pytest.raises(LinearError):
            parse_tickets(bad, {"i0": "PC-1"})
