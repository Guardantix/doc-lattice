"""Behavior tests for the action-pin correspondence checker.

The script is loaded with `run_path` rather than imported, for the reason
`tests/test_audit_action_runtimes.py` does the same: `scripts/` is not a package and the workflow
runs the file by path, so this exercises exactly what the runner executes.

Every test drives the check through an injected transport, so the suite stays offline. That is
the whole reason the transport is a parameter: the correspondence this script exists to establish
is only observable with network access, and the default `uv run --group dev pytest` run must not
acquire that dependency.
"""

import io
import os
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from runpy import run_path

import pytest

from doc_lattice.constants import (
    CHECKOUT_REF,
    CHECKOUT_USES,
    CHECKOUT_VERSION,
    SETUP_UV_USES,
)

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _ROOT / "scripts" / "check_action_pin_correspondence.py"
_SCRIPT = run_path(str(_SCRIPT_PATH))

CLEAN = _SCRIPT["CLEAN"]
FAILURE = _SCRIPT["FAILURE"]
FINDING = _SCRIPT["FINDING"]
Pin = _SCRIPT["Pin"]
PinFormatError = _SCRIPT["PinFormatError"]
Response = _SCRIPT["Response"]
SHIPPED_PINS = _SCRIPT["SHIPPED_PINS"]
TransportError = _SCRIPT["TransportError"]
check = _SCRIPT["check"]
fetch_json = _SCRIPT["fetch_json"]
main = _SCRIPT["main"]
parse_pin = _SCRIPT["parse_pin"]
render_summary = _SCRIPT["render_summary"]
resolve_pin = _SCRIPT["resolve_pin"]

# A second commit and a tag-object SHA, both distinct from the shipped checkout pin. The tag
# object is what `/git/ref/tags/` returns for an annotated tag, and comparing it would be wrong
# every time, so it is spelled out here rather than reused from the commit.
_OTHER_COMMIT = "1111111111111111111111111111111111111111"
_TAG_OBJECT = "2222222222222222222222222222222222222222"
_ACTION = "actions/checkout"
_PROBE = f"/repos/{_ACTION}/git/ref/tags/{CHECKOUT_VERSION}"
_COMMITS = f"/repos/{_ACTION}/commits/tags/{CHECKOUT_VERSION}"
_PIN = Pin(action=_ACTION, sha=CHECKOUT_REF, version=CHECKOUT_VERSION)


def _ref_payload(sha: str, kind: str = "commit") -> dict:
    return {
        "ref": f"refs/tags/{CHECKOUT_VERSION}",
        "object": {"sha": sha, "type": kind},
    }


def _fake_api(responses: dict, calls: list | None = None):
    """Return a transport over canned responses, recording every path it is asked for."""
    recorded = calls if calls is not None else []

    def fetch(path: str):
        recorded.append(path)
        try:
            response = responses[path]
        except KeyError:
            raise AssertionError(f"unexpected path {path}") from None
        if isinstance(response, Exception):
            raise response
        return response

    return fetch, recorded


def _ok(payload) -> object:
    return Response(status=200, payload=payload)


def _correspondent() -> dict:
    """Responses describing a pin whose SHA is exactly the commit its tag names."""
    return {
        _PROBE: _ok(_ref_payload(CHECKOUT_REF)),
        _COMMITS: _ok({"sha": CHECKOUT_REF}),
    }


def test_parse_pin_splits_a_fragment_into_the_two_halves_that_must_agree():
    pin = parse_pin(CHECKOUT_USES)

    assert pin.action == _ACTION
    assert pin.sha == CHECKOUT_REF
    assert pin.version == CHECKOUT_VERSION


def test_every_shipped_pin_parses():
    # Offline, and deliberately so: this is the half of the contract that still holds without
    # network access. A hand edit that breaks the `@<sha> # vX.Y.Z` shape fails here on the next
    # pull request rather than waiting for the monthly scheduled run to report it.
    assert [parse_pin(fragment).action for fragment in SHIPPED_PINS] == [
        "actions/checkout",
        "astral-sh/setup-uv",
    ]


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        (f"{_ACTION}@{CHECKOUT_REF}", "no trailing"),
        (f"{_ACTION} # {CHECKOUT_VERSION}", "names no pinned ref"),
        (f"{_ACTION}@{CHECKOUT_REF[:12]} # {CHECKOUT_VERSION}", "40-character"),
        (f"{_ACTION}@{CHECKOUT_REF.upper()} # {CHECKOUT_VERSION}", "40-character"),
        (f"{_ACTION}@{CHECKOUT_REF} # v7", "not an exact"),
        (f"{_ACTION}@{CHECKOUT_REF} # main", "not an exact"),
        (f"{_ACTION}@{CHECKOUT_REF} # ", "not an exact"),
    ],
)
def test_parse_pin_rejects_a_pair_that_cannot_be_resolved_to_one_commit(fragment, expected):
    # `v7` is the case that matters most: it is a real, valid, widely used reference, and it
    # moves. Resolving it would compare the pin against whatever the publisher last pointed the
    # channel at and call a green result correspondence.
    with pytest.raises(PinFormatError, match=expected):
        parse_pin(fragment)


def test_resolve_pin_reports_a_pin_that_names_its_release():
    fetch, calls = _fake_api(_correspondent())

    outcome = resolve_pin(fetch, _PIN)

    assert outcome.kind == CLEAN
    assert CHECKOUT_REF in outcome.detail
    assert calls == [_PROBE, _COMMITS]


def test_resolve_pin_compares_the_peeled_commit_not_the_tag_object():
    # An annotated tag's reference names a tag object, not a commit. Comparing the ref
    # endpoint's SHA would report a mismatch on every correctly pinned annotated release, which
    # is the failure mode that makes this the wrong endpoint to compare with.
    fetch, _calls = _fake_api(
        {
            _PROBE: _ok(_ref_payload(_TAG_OBJECT, kind="tag")),
            _COMMITS: _ok({"sha": CHECKOUT_REF}),
        }
    )

    assert resolve_pin(fetch, _PIN).kind == CLEAN


def test_resolve_pin_reports_a_tag_that_names_a_different_commit():
    # The wrong-comment case: the pin is a real commit and the tag is a real release, and they
    # are not each other. Both SHAs go in the detail, because a maintainer has to decide which
    # half is wrong.
    fetch, _calls = _fake_api(
        {
            _PROBE: _ok(_ref_payload(_OTHER_COMMIT)),
            _COMMITS: _ok({"sha": _OTHER_COMMIT}),
        }
    )

    outcome = resolve_pin(fetch, _PIN)

    assert outcome.kind == FINDING
    assert _OTHER_COMMIT in outcome.detail
    assert CHECKOUT_REF in outcome.detail


def test_resolve_pin_reports_a_missing_tag_as_a_correspondence_finding():
    # The second Dependabot limit AD-42 leaves here: a SHA with no direct tag is advanced to
    # branch HEAD, so the pin can end up naming a commit no release resolves to. A deleted tag
    # upstream lands here too.
    fetch, calls = _fake_api({_PROBE: Response(status=404, payload=None)})

    outcome = resolve_pin(fetch, _PIN)

    assert outcome.kind == FINDING
    assert f"no tag {CHECKOUT_VERSION}" in outcome.detail
    # The commits endpoint is never asked: there is nothing to resolve.
    assert calls == [_PROBE]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
def test_resolve_pin_keeps_an_outage_out_of_the_finding_channel(status):
    # A bad credential and a rate limit must never be reported as a mislabeled release. Both
    # fail closed, but only one of them is a claim about the pin.
    fetch, _calls = _fake_api({_PROBE: Response(status=status, payload=None)})

    outcome = resolve_pin(fetch, _PIN)

    assert outcome.kind == FAILURE
    assert str(status) in outcome.detail


def test_resolve_pin_treats_a_rejected_commit_lookup_as_infrastructure():
    # 422 from the commits endpoint is why the ref probe exists. GitHub documents that status
    # as either a validation failure or abuse protection, so it cannot carry "no such tag" --
    # and by this point the probe has already established that the tag does exist.
    fetch, _calls = _fake_api(
        {
            _PROBE: _ok(_ref_payload(CHECKOUT_REF)),
            _COMMITS: Response(status=422, payload=None),
        }
    )

    outcome = resolve_pin(fetch, _PIN)

    assert outcome.kind == FAILURE
    assert "422" in outcome.detail


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"sha": None},
        {"sha": 7},
        {"sha": "not-a-sha"},
        {"commit": {"sha": CHECKOUT_REF}},
    ],
)
def test_resolve_pin_reports_an_unreadable_payload_as_infrastructure(payload):
    # A payload that is not shaped as documented says nothing about the pin. Reading a missing
    # or malformed `sha` as a mismatch would turn an upstream shape change into a red pin.
    fetch, _calls = _fake_api({_PROBE: _ok(_ref_payload(CHECKOUT_REF)), _COMMITS: _ok(payload)})

    outcome = resolve_pin(fetch, _PIN)

    assert outcome.kind == FAILURE
    assert "'sha'" in outcome.detail


def test_check_reports_every_pin_even_when_one_transport_fails():
    # Each pin is read independently so a transport failure on the first cannot hide a real
    # finding on the second, which is exactly the pair a partial outage produces.
    setup_uv = parse_pin(SETUP_UV_USES)
    fetch, _calls = _fake_api(
        {
            _PROBE: TransportError("GET failed: [Errno -3] Temporary failure in name resolution"),
            f"/repos/{setup_uv.action}/git/ref/tags/{setup_uv.version}": Response(
                status=404, payload=None
            ),
        }
    )

    outcomes = check(fetch, [CHECKOUT_USES, SETUP_UV_USES])

    assert [outcome.kind for outcome in outcomes] == [FAILURE, FINDING]
    assert "name resolution" in outcomes[0].detail


def test_check_reports_an_unparseable_pin_as_a_finding_without_asking_the_api():
    fetch, calls = _fake_api({})

    outcomes = check(fetch, [f"{_ACTION}@{CHECKOUT_REF} # v7"])

    assert [outcome.kind for outcome in outcomes] == [FINDING]
    assert calls == []


def test_render_summary_tables_every_pin_and_points_at_the_procedure_only_when_it_must():
    clean_fetch, _clean_calls = _fake_api(_correspondent())
    missing_fetch, _missing_calls = _fake_api({_PROBE: Response(status=404, payload=None)})

    clean = render_summary(check(clean_fetch, [CHECKOUT_USES]))
    finding = render_summary(check(missing_fetch, [CHECKOUT_USES]))

    assert clean.startswith("## Action pin correspondence\n")
    assert "Pins checked: 1." in clean
    assert "| Pin | Result | Detail |" in clean
    assert f"`{CHECKOUT_USES}`" in clean
    assert "RELEASING.md" not in clean
    assert clean.endswith("\n")
    # The pointer is what tells a reader a finding is a coupled multi-file edit rather than a
    # workflow edit, so it appears exactly when there is something to act on.
    assert "RELEASING.md" in finding


def test_main_checks_the_shipped_pins_by_default(monkeypatch, capsys):
    # Nothing else names the two pins to the workflow. If the default ever narrowed to one, the
    # run would still be green and would still say so, having checked half of what it claims.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    setup_uv = parse_pin(SETUP_UV_USES)
    fetch, calls = _fake_api(
        {
            **_correspondent(),
            f"/repos/{setup_uv.action}/git/ref/tags/{setup_uv.version}": _ok(
                {"object": {"sha": setup_uv.sha, "type": "commit"}}
            ),
            f"/repos/{setup_uv.action}/commits/tags/{setup_uv.version}": _ok({"sha": setup_uv.sha}),
        }
    )

    code = main([], fetch)

    assert code == 0
    assert calls == [
        _PROBE,
        _COMMITS,
        f"/repos/{setup_uv.action}/git/ref/tags/{setup_uv.version}",
        f"/repos/{setup_uv.action}/commits/tags/{setup_uv.version}",
    ]
    assert "Pins checked: 2." in capsys.readouterr().out


def test_main_writes_the_summary_to_the_step_summary_file(tmp_path, monkeypatch, capsys):
    # Without this the only report is the log, which nobody reads on a scheduled run that went
    # green -- and a green run with no summary is indistinguishable from one that never ran.
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    fetch, _calls = _fake_api(_correspondent())

    assert main(["--pin", CHECKOUT_USES], fetch) == 0
    capsys.readouterr()

    assert "## Action pin correspondence" in summary_path.read_text(encoding="utf-8")


def test_main_exits_one_on_a_correspondence_finding(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fetch, _calls = _fake_api({_PROBE: Response(status=404, payload=None)})

    code = main(["--pin", CHECKOUT_USES], fetch)

    assert code == 1
    assert "::error::correspondence finding:" in capsys.readouterr().err


def test_main_exits_two_when_it_could_not_establish_anything(monkeypatch, capsys):
    # Exit 2 keeps "could not check" distinct from "checked and found something", so an outage
    # never reads as a mislabeled pin and never reads as a clean run either.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fetch, _calls = _fake_api({_PROBE: Response(status=403, payload=None)})

    code = main(["--pin", CHECKOUT_USES], fetch)

    assert code == 2
    assert "::error::infrastructure failure:" in capsys.readouterr().err


def test_main_reports_a_finding_over_a_concurrent_outage(monkeypatch, capsys):
    # A mislabeled pin is actionable now. Letting an unrelated outage on the other pin decide
    # the exit code would report the weaker of the two answers, and both are on stderr anyway.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    setup_uv = parse_pin(SETUP_UV_USES)
    fetch, _calls = _fake_api(
        {
            _PROBE: Response(status=404, payload=None),
            f"/repos/{setup_uv.action}/git/ref/tags/{setup_uv.version}": Response(
                status=500, payload=None
            ),
        }
    )

    code = main([], fetch)
    captured = capsys.readouterr()

    assert code == 1
    assert "::error::correspondence finding:" in captured.err
    assert "::error::infrastructure failure:" in captured.err


def test_main_checks_exactly_the_pins_an_override_names(monkeypatch, capsys):
    # The override is how a maintainer proves a failure mode live, without editing the constants
    # and every copy the parity tests hold to them.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fetch, calls = _fake_api(_correspondent())

    assert main(["--pin", CHECKOUT_USES], fetch) == 0
    assert calls == [_PROBE, _COMMITS]
    assert "Pins checked: 1." in capsys.readouterr().out


def _fake_urlopen(status: int, body: bytes):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        @property
        def status(self) -> int:
            return status

        def read(self) -> bytes:
            return body

    def urlopen(_request, timeout=None):  # noqa: ARG001 - matches the stdlib signature
        return _Response()

    return urlopen


def test_fetch_json_returns_the_status_and_the_decoded_body(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(200, b'{"sha": "abc"}'))

    response = fetch_json(_COMMITS)

    assert response.status == 200
    assert response.payload == {"sha": "abc"}


def test_fetch_json_returns_an_error_status_rather_than_raising(monkeypatch):
    # The status *is* the evidence: 404 from the probe is a finding and every other non-200 is a
    # failure, and neither classification is possible if the transport raises them all alike.
    def raise_http_error(_request, timeout=None):  # noqa: ARG001 - matches the stdlib signature
        raise urllib.error.HTTPError(
            "https://api.github.com",
            404,
            "Not Found",
            Message(),
            io.BytesIO(b'{"message": "Not Found"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)

    assert fetch_json(_PROBE).status == 404


def test_fetch_json_raises_when_the_request_produced_no_status(monkeypatch):
    def refuse(_request, timeout=None):  # noqa: ARG001 - matches the stdlib signature
        raise urllib.error.URLError("Temporary failure in name resolution")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    with pytest.raises(TransportError, match="name resolution"):
        fetch_json(_PROBE)


def test_fetch_json_raises_when_a_success_carries_no_json(monkeypatch):
    # A proxy or a captive portal answers 200 with HTML. Reading that as a payload would reach
    # the payload check and be reported as a malformed commit rather than as a broken transport.
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(200, b"<html>nope</html>"))

    with pytest.raises(TransportError, match="did not return JSON"):
        fetch_json(_COMMITS)


def test_script_reports_a_bad_pair_by_path_without_touching_the_network():
    # The workflow invokes the file by path, so the contract has to hold for a real process and
    # not only for an in-process call. A format finding needs no transport, which is what makes
    # this runnable in the offline suite.
    result = subprocess.run(  # noqa: S603 - controlled test interpreter arguments
        (sys.executable, str(_SCRIPT_PATH), "--pin", f"{_ACTION}@{CHECKOUT_REF} # v7"),
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_ROOT / "src")},
    )

    assert result.returncode == 1
    assert "::error::correspondence finding:" in result.stderr
