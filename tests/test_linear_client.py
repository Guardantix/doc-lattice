"""Tests for the Linear transport client (mocked, no real network)."""

import io
import urllib.error

import pytest

from game_lattice.error_types import LinearError
from game_lattice.linear_client import LinearClient


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):  # type: ignore
        self.close()
        return False


class _FakeOpener:
    def __init__(self, body: bytes = b'{"data":{}}'):
        self.body = body
        self.captured = None
        self.timeout = None

    def open(self, req, timeout=None):
        self.captured = req
        self.timeout = timeout
        return _FakeResp(self.body)


def test_rejects_non_https_url():
    with pytest.raises(LinearError):
        LinearClient(url="http://api.linear.app/graphql")


def test_execute_sends_authorized_post(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")
    opener = _FakeOpener(b'{"data":{"i0":null}}')
    client = LinearClient(opener=opener)
    body = client.execute("query {}", {"id0": "PC-1"})
    assert body == '{"data":{"i0":null}}'
    assert opener.captured.get_method() == "POST"  # type: ignore
    assert opener.captured.headers["Authorization"] == "secret-key"  # type: ignore
    assert opener.captured.headers["Content-type"] == "application/json"  # type: ignore
    assert opener.captured.headers["User-agent"].startswith("game-lattice/")  # type: ignore
    assert opener.timeout == client._timeout


def test_execute_serializes_document_and_variables(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")
    import json  # noqa: PLC0415

    opener = _FakeOpener()
    LinearClient(opener=opener).execute("query Q {}", {"id0": "PC-1", "ids": [1, 2, 3]})
    sent = json.loads(opener.captured.data.decode("utf-8"))  # type: ignore
    assert sent == {"query": "query Q {}", "variables": {"id0": "PC-1", "ids": [1, 2, 3]}}


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(LinearError) as exc:
        LinearClient(opener=_FakeOpener()).execute("query {}", {})
    assert "secret-key" not in str(exc.value)


def test_whitespace_only_key_treated_as_missing(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "   ")
    with pytest.raises(LinearError) as exc:
        LinearClient(opener=_FakeOpener()).execute("query {}", {})
    assert exc.value.code == "LINEAR_ERROR"


def test_surrounding_whitespace_in_key_is_stripped(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "  abc  ")
    opener = _FakeOpener()
    LinearClient(opener=opener).execute("query {}", {})
    assert opener.captured.headers["Authorization"] == "abc"  # type: ignore


def test_http_error_maps_to_linear_error_without_key(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")

    class _Boom:
        def open(self, req, timeout=None):  # noqa: ARG002
            raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)  # type: ignore

    with pytest.raises(LinearError) as exc:
        LinearClient(opener=_Boom()).execute("query {}", {})
    assert "429" in str(exc.value)
    assert "secret-key" not in str(exc.value)


def test_url_error_maps_to_linear_error(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")

    class _Boom:
        def open(self, req, timeout=None):  # noqa: ARG002
            raise urllib.error.URLError("name resolution failed")

    with pytest.raises(LinearError) as exc:
        LinearClient(opener=_Boom()).execute("query {}", {})
    assert exc.value.code == "LINEAR_ERROR"
    assert "name resolution failed" in str(exc.value)  # load-bearing: reason is surfaced
    assert "run impact" in str(exc.value)  # documented offline-view guidance


def test_read_timeout_maps_to_linear_error(monkeypatch):
    # A body-read timeout raises TimeoutError after open() returns, not URLError; it must
    # still surface as a LinearError with the retry message, not escape as an internal error.
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")

    class _SlowResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            raise TimeoutError("read timed out")

    class _SlowOpener:
        def open(self, req, timeout=None):  # noqa: ARG002
            return _SlowResp()

    with pytest.raises(LinearError) as exc:
        LinearClient(opener=_SlowOpener()).execute("query {}", {})
    assert "secret-key" not in str(exc.value)


def test_oversized_response_raises(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")
    from game_lattice.linear_client import MAX_RESPONSE_BYTES  # noqa: PLC0415

    opener = _FakeOpener(b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(LinearError):
        LinearClient(opener=opener).execute("query {}", {})


def test_response_exactly_at_cap_is_accepted(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")
    from game_lattice.linear_client import MAX_RESPONSE_BYTES  # noqa: PLC0415

    body = b"x" * MAX_RESPONSE_BYTES
    out = LinearClient(opener=_FakeOpener(body)).execute("query {}", {})
    assert len(out) == MAX_RESPONSE_BYTES  # at-limit body returned, not rejected


def test_no_redirect_handler_returns_none():
    from game_lattice.linear_client import _NoRedirect  # noqa: PLC0415

    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://evil") is None


def test_default_opener_installs_no_redirect_handler():
    from game_lattice.linear_client import _NoRedirect  # noqa: PLC0415

    client = LinearClient()  # no opener -> default build_opener(_NoRedirect)
    assert any(isinstance(h, _NoRedirect) for h in client._opener.handlers)  # type: ignore


def test_invalid_utf8_body_is_replaced_not_raised(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")
    out = LinearClient(opener=_FakeOpener(b'\xff\xfe{"data":{}}')).execute("query {}", {})
    assert "�" in out  # replacement char, no UnicodeDecodeError escaped
