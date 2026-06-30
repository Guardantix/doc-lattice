"""Tests for the linear fetch wiring (mocked client, no real network)."""

import json

import pytest

from game_lattice.error_types import LinearError
from game_lattice.linear_fetch import fetch_tickets


class _RecordingClient:
    def __init__(self, body_for):
        self.body_for = body_for
        self.calls = 0
        self.seen_vars = []

    def execute(self, _document, variables):
        self.calls += 1
        self.seen_vars.append(variables)
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


def test_default_client_constructed_and_used(monkeypatch):
    """With client=None and valid ids, the real LinearClient is constructed and driven."""
    built = []

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            built.append(self)
            self.calls = 0

        def execute(self, _document, variables):
            self.calls += 1
            return _echo_issues(variables)

    monkeypatch.setattr("game_lattice.linear_fetch.LinearClient", _FakeClient)
    tickets, rejected = fetch_tickets(["PC-1"], None)  # no client kwarg
    assert len(built) == 1
    assert built[0].calls == 1
    assert set(tickets) == {"PC-1"}
    assert rejected == {}


def test_transport_error_propagates():
    """A transport LinearError raised by execute propagates out of fetch_tickets."""

    class _Boom:
        def execute(self, _document, _variables):
            raise LinearError("Linear network error: refused")

    with pytest.raises(LinearError) as exc:
        fetch_tickets(["PC-1"], None, client=_Boom())  # type: ignore
    assert exc.value.code == "LINEAR_ERROR"


def test_malformed_body_propagates_and_aborts(monkeypatch):
    # First chunk parses fine; the second returns a GraphQL errors envelope -> whole fetch fails.
    monkeypatch.setattr("game_lattice.linear_fetch.BATCH_SIZE", 1)
    bodies = iter([_connection([_issue("PC-1", 1)]), '{"errors": [{"message": "boom"}]}'])
    client = _RecordingClient(lambda _v: next(bodies))
    with pytest.raises(LinearError) as exc:
        fetch_tickets(["PC-1", "PC-2"], None, client=client)  # type: ignore
    assert exc.value.code == "LINEAR_ERROR"


def test_each_chunk_carries_exactly_its_numbers(monkeypatch):
    # With batch size 1, each request must carry exactly one of the two same-team numbers.
    monkeypatch.setattr("game_lattice.linear_fetch.BATCH_SIZE", 1)
    client = _RecordingClient(_echo_issues)
    fetch_tickets(["PC-1", "PC-2"], None, client=client)  # type: ignore
    assert [v["team"] for v in client.seen_vars] == ["PC", "PC"]
    assert sorted(n for v in client.seen_vars for n in v["numbers"]) == [1, 2]
    assert all(len(v["numbers"]) == 1 for v in client.seen_vars)
