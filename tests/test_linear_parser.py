"""Tests for the Linear response parser boundary."""

import json

import pytest

from game_lattice.error_types import LinearError
from game_lattice.linear_parser import parse_tickets


def _issue(identifier="PC-1", state_type="completed", number=1):
    return {
        "identifier": identifier,
        "number": number,
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


def _wrap(nodes):
    """Wrap a list of issue nodes in the connection envelope."""
    return json.dumps({"data": {"issues": {"nodes": nodes}}})


def test_parses_ticket_with_children():
    text = _wrap([_issue(number=1)])
    tickets = parse_tickets(text, "PC")
    assert "PC-1" in tickets
    assert tickets["PC-1"].url.endswith("PC-1")
    assert tickets["PC-1"].children[0].identifier == "PC-9"


def test_keys_by_team_and_number_not_echoed_identifier():
    # Linear may echo a lowercased or differently-cased identifier; the map must be keyed
    # by the queried team key plus the node's own number, never by what Linear echoes.
    node = _issue(identifier="pc-1", number=1)
    text = _wrap([node])
    tickets = parse_tickets(text, "PC")
    assert "PC-1" in tickets
    assert "pc-1" not in tickets


def test_empty_nodes_yields_empty_map():
    # A queried id absent from the returned nodes is simply absent from the map.
    text = _wrap([])
    tickets = parse_tickets(text, "PC")
    assert tickets == {}


def test_unknown_extra_field_ignored():
    issue = _issue(number=1)
    issue["surprise"] = "new linear field"
    text = _wrap([issue])
    tickets = parse_tickets(text, "PC")
    assert tickets["PC-1"].identifier == "PC-1"


def test_control_chars_stripped_from_url_and_identifier():
    issue = _issue(number=1)
    issue["url"] = "https://x/\x1bPC-1"
    text = _wrap([issue])
    tickets = parse_tickets(text, "PC")
    assert "\x1b" not in tickets["PC-1"].url


def test_duplicate_state_type_parses():
    # Linear's "Duplicate" is its own state category, distinct from canceled. It must parse
    # (not fail the whole audit with exit 2); stale_shipped omits it as terminal.
    issue = _issue(state_type="duplicate", number=1)
    issue["parent"] = {
        "identifier": "PC-7",
        "title": "Parent",
        "state": {"name": "Dup", "type": "duplicate"},
    }
    text = _wrap([issue])
    tickets = parse_tickets(text, "PC")
    assert tickets["PC-1"].state.type == "duplicate"
    assert tickets["PC-1"].parent is not None
    assert tickets["PC-1"].parent.state.type == "duplicate"


def test_graphql_errors_raise():
    text = json.dumps({"errors": [{"message": "rate limited"}]})
    with pytest.raises(LinearError):
        parse_tickets(text, "PC")


def test_non_list_errors_raise_linear_error():
    for bad in (json.dumps({"errors": 42}), json.dumps({"errors": "rate limited"})):
        with pytest.raises(LinearError):
            parse_tickets(bad, "PC")


def test_missing_data_raises():
    with pytest.raises(LinearError):
        parse_tickets(json.dumps({"meta": 1}), "PC")


def test_invalid_json_raises():
    with pytest.raises(LinearError):
        parse_tickets("not json", "PC")


def test_malformed_issue_missing_url_raises():
    issue = _issue(number=1)
    del issue["url"]
    text = _wrap([issue])
    with pytest.raises(LinearError):
        parse_tickets(text, "PC")


def test_malformed_issue_missing_number_raises():
    issue = _issue(number=1)
    del issue["number"]
    text = _wrap([issue])
    with pytest.raises(LinearError):
        parse_tickets(text, "PC")


def test_missing_issues_connection_raises():
    # data is a dict but has no "issues" key.
    text = json.dumps({"data": {}})
    with pytest.raises(LinearError):
        parse_tickets(text, "PC")


def test_malformed_issues_connection_raises():
    # "issues" is present but not a dict.
    text = json.dumps({"data": {"issues": "not-a-dict"}})
    with pytest.raises(LinearError):
        parse_tickets(text, "PC")


def test_malformed_nodes_raises():
    # "nodes" is not a list.
    text = json.dumps({"data": {"issues": {"nodes": "not-a-list"}}})
    with pytest.raises(LinearError):
        parse_tickets(text, "PC")
