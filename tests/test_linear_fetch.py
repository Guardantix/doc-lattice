"""Tests for the linear fetch wiring (mocked client, no real network)."""

import json

from game_lattice.linear_fetch import fetch_tickets


class _RecordingClient:
    def __init__(self, body_for):
        self.body_for = body_for
        self.calls = 0

    def execute(self, _document, variables):
        self.calls += 1
        return self.body_for(variables)


def _issue(identifier, number):
    return {
        "identifier": identifier,
        "number": number,
        "title": "t",
        "url": "https://x/" + identifier,
        "state": {"name": "Done", "type": "completed"},
        "parent": None,
        "children": {"nodes": []},
    }


def _connection(nodes):
    """Wrap nodes in the filtered issues connection envelope."""
    return json.dumps({"data": {"issues": {"nodes": nodes}}})


def _echo_issues(variables):
    """Return a connection echoing one issue per requested number in the team."""
    team = variables["team"]
    numbers = variables["numbers"]
    return _connection([_issue(f"{team}-{n}", n) for n in numbers])


def test_empty_identifiers_skip_network(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    def explode(*_args, **_kwargs):
        raise AssertionError("must not construct a client")

    monkeypatch.setattr("game_lattice.linear_fetch.LinearClient", explode)
    tickets, rejected = fetch_tickets(["not-a-ticket"], None)
    assert tickets == {}
    assert rejected == {"not-a-ticket": "malformed"}


def test_missing_node_absent_not_error():
    """HEADLINE regression: a queried id absent from the returned nodes is simply absent."""
    # Queries PC-1 and PC-2; the response contains a node only for number 1.
    # Before the fix, a missing aliased issue returned data: null and crashed. After the fix,
    # the id is just absent from the ticket map.
    issue1 = _issue("PC-1", 1)
    client = _RecordingClient(lambda _v: _connection([issue1]))
    tickets, _ = fetch_tickets(["PC-1", "PC-2"], None, client=client)  # type: ignore
    assert "PC-1" in tickets
    assert "PC-2" not in tickets


def test_two_teams_two_queries():
    # With linear_team=None, ids spanning two teams trigger one query per team.
    client = _RecordingClient(_echo_issues)
    tickets, _ = fetch_tickets(["PC-1", "SEC-9"], None, client=client)  # type: ignore
    assert set(tickets) == {"PC-1", "SEC-9"}
    assert client.calls == 2


def test_cross_team_rejected_before_fetch():
    # When linear_team is set, cross-team ids are rejected before any request is issued.
    client = _RecordingClient(_echo_issues)
    tickets, rejected = fetch_tickets(["PC-1", "SEC-9"], "PC", client=client)  # type: ignore
    assert rejected == {"SEC-9": "cross-team"}
    assert "PC-1" in tickets
    assert client.calls == 1


def test_chunks_merge(monkeypatch):
    # When the batch size is 1, two ids in the same team require two requests whose results merge.
    monkeypatch.setattr("game_lattice.linear_fetch.BATCH_SIZE", 1)
    client = _RecordingClient(_echo_issues)
    tickets, _ = fetch_tickets(["PC-1", "PC-2"], None, client=client)  # type: ignore
    assert set(tickets) == {"PC-1", "PC-2"}
    assert client.calls == 2
