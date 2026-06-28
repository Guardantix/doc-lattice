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


def _issue(identifier):
    return {
        "identifier": identifier,
        "title": "t",
        "url": "https://x/" + identifier,
        "state": {"name": "Done", "type": "completed"},
        "parent": None,
        "children": {"nodes": []},
    }


def test_empty_identifiers_skip_network(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    def explode(*_a, **_k):
        raise AssertionError("must not construct a client")

    monkeypatch.setattr("game_lattice.linear_fetch.LinearClient", explode)
    tickets, rejected = fetch_tickets(["not-a-ticket"], None)
    assert tickets == {}
    assert rejected == {"not-a-ticket": "malformed"}


def test_dedup_and_keying():
    client = _RecordingClient(
        lambda v: json.dumps(
            {"data": {f"i{i}": _issue(ident) for i, ident in enumerate(v.values())}}
        )
    )
    tickets, rejected = fetch_tickets(["PC-1", "PC-1", "PC-2"], None, client=client)  # type: ignore
    assert set(tickets) == {"PC-1", "PC-2"}
    assert rejected == {}


def test_chunks_merge(monkeypatch):
    monkeypatch.setattr("game_lattice.linear_fetch.BATCH_SIZE", 1)
    client = _RecordingClient(
        lambda v: json.dumps(
            {"data": {f"i{i}": _issue(ident) for i, ident in enumerate(v.values())}}
        )
    )
    tickets, _ = fetch_tickets(["PC-1", "PC-2"], None, client=client)  # type: ignore
    assert set(tickets) == {"PC-1", "PC-2"}
    assert client.calls == 2  # one request per chunk


def test_unresolved_is_absent_from_map():
    client = _RecordingClient(lambda _v: json.dumps({"data": {"i0": None}}))
    tickets, _ = fetch_tickets(["PC-404"], None, client=client)  # type: ignore
    assert tickets == {}
