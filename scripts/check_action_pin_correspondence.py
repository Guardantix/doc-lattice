#!/usr/bin/env python3
"""Check that each shipped action pin names the commit its trailing version comment claims.

`src/doc_lattice/constants.py` owns two pinned actions as a SHA and a release tag, rendered
together as ``owner/action@<sha> # vX.Y.Z`` everywhere the pin ships. Every check in the suite
compares that text against another copy of itself, so a SHA paired with the wrong tag is
self-consistent in every file and passes green. Nothing else in this repository ever asks GitHub
whether the commit really is the release the comment names. This script asks.

AD-42 in ARCHITECTURE.md leaves two Dependabot limits here: an already-wrong comment is not
corrected by a bump, and a SHA with no direct tag is advanced to branch HEAD, so a pin can come
to name a commit that resolves to no release at all. A tag is also mutable -- the current
`actions/checkout` release reports ``immutable: false``, and GitHub exposes reference update and
deletion -- so a pair authored correctly can stop being true later. That is why this runs on a
schedule rather than once after a bump.

Two outcomes are kept apart, and the separation is the point. A *correspondence finding* is a
claim about the pin: the comment is not an exact release, the tag does not exist, or the tag
names a different commit. An *infrastructure failure* is this check failing to establish
anything: authentication, rate limiting, transport, an unexpected status, or a payload that is
not shaped as the endpoint documents. Both fail the run, because an unverified pin is not a
verified one, but an outage must never be reported as a mislabeled release.

Resolution is two requests per pin, and neither one is redundant. ``GET /git/ref/tags/{tag}`` is
documented to answer 404 for a reference that does not exist, which is what gives the missing-tag
finding a status to key on; the commits endpoint answers 422 there, and GitHub documents that
status as either a validation failure or abuse protection, so it cannot carry that meaning alone.
The SHA the ref endpoint returns is then deliberately discarded: for an annotated tag it is the
tag object, not the commit. Only ``GET /commits/tags/{tag}`` peels both tag kinds to the commit
this pin is comparable against.

The script imports the pins rather than restating them, which is what keeps a bump from having to
remember this file. `scripts/audit_action_runtimes.py` cannot do that because its workflow runs it
under ``uv run --no-project``; this one runs against a synced project for exactly this reason.

What the two scripts do share is how a report is written: ``scripts/_ci_report.py`` owns the
guarded exception set, the per-write guard, and `emit`. Both reserve exit 1 for a finding, so both
need the same guard, and holding it in one place is what keeps the two from drifting apart again.
This file keeps its own renderers and its own annotation vocabulary.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from _ci_report import emit

from doc_lattice.constants import CHECKOUT_USES, SETUP_UV_USES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

_API_HOST = "api.github.com"
_TIMEOUT_SECONDS = 30.0
_HTTP_OK = 200
_HTTP_NOT_FOUND = 404
# Sent on every request. The API version header pins the response shape this script narrows, so
# an unannounced default bump upstream cannot change what `sha` means underneath it.
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "doc-lattice-action-pin-correspondence",
}
# Read when set, absent otherwise: the two lookups are public reads, so a token buys a rate limit
# rather than access. The workflow supplies `github.token`.
_TOKEN_VARIABLES = ("GITHUB_TOKEN", "GH_TOKEN")

# An exact release, not a channel. `v7` and `main` resolve to whatever the publisher last moved
# them to, so they name no single commit and there is nothing to compare a pin against.
_EXACT_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")

# The pins this repository ships, composed by `constants.py` from `CHECKOUT_REF`/`CHECKOUT_VERSION`
# and `SETUP_UV_REF`/`SETUP_UV_VERSION`. Read as fragments rather than as four values so the action
# name is not spelled a second time here, and so the parser below is the same code that reads a
# `--pin` override.
SHIPPED_PINS = (CHECKOUT_USES, SETUP_UV_USES)

CLEAN = "clean"
FINDING = "finding"
FAILURE = "failure"

_POINTER = (
    'A finding is a pin whose comment and SHA disagree. RELEASING.md, under "Keeping the action '
    'pins current", owns the coupled edit that corrects it. A failure means the check could not '
    "establish correspondence at all, and says nothing about the pins."
)
_TABLE_HEADER = ("| Pin | Result | Detail |", "| --- | --- | --- |")
# How each non-clean outcome is named in the workflow log.
_LABELS = {FINDING: "correspondence finding", FAILURE: "infrastructure failure"}


class TransportError(RuntimeError):
    """A request that never produced an HTTP status at all."""


class PinFormatError(ValueError):
    """A pin fragment that does not carry an exact commit-SHA and release pair."""


@dataclass(frozen=True)
class Pin:
    """One pinned action, split into the halves that must agree.

    Attributes:
        action: The action as ``owner/repo``, such as ``actions/checkout``.
        sha: The 40-character commit SHA the workflow pins.
        version: The exact release the trailing comment names, such as ``v7.0.1``.
    """

    action: str
    sha: str
    version: str


@dataclass(frozen=True)
class Response:
    """One HTTP response, kept as a status and a body rather than raised or discarded.

    Attributes:
        status: The HTTP status code, including the error statuses this script classifies.
        payload: The decoded JSON body, or None when the status carried no readable one.
    """

    status: int
    payload: object


@dataclass(frozen=True)
class Outcome:
    """What the check established about one pin.

    Attributes:
        fragment: The pin fragment as written, so a report names the text a maintainer edits.
        kind: One of `CLEAN`, `FINDING`, or `FAILURE`.
        detail: A sentence naming what was established, or what stopped it being established.
    """

    fragment: str
    kind: str
    detail: str


def _authorization() -> dict[str, str]:
    """Return an Authorization header when a token is in the environment, or no header."""
    for variable in _TOKEN_VARIABLES:
        token = os.environ.get(variable)
        if token:
            return {"Authorization": f"Bearer {token}"}
    return {}


def fetch_json(path: str) -> Response:
    """Return the status and decoded body of a GET against the GitHub API.

    Every network call in this script goes through here, which is what lets the tests drive the
    whole check with a plain callable. An error status is *returned* rather than raised, because
    the status is the evidence: 404 from the reference probe is a correspondence finding, and
    every other non-200 is an infrastructure failure. Only a request that produced no status at
    all raises.

    A connection rather than a URL is what makes that shape simple, and it is also what keeps the
    scheme out of reach: ``HTTPSConnection`` fixes both the host and TLS in its constructor, so no
    path composed here can move the request onto another host, and none can reach a ``file://``
    the way a URL-opening transport composed from the same string could. Nothing is followed
    either -- a redirect is returned as its own status and classified as an infrastructure
    failure, which is the right answer for these two endpoints, where a 3xx means the action's
    repository moved rather than that the pin is wrong.

    Args:
        path: An API path such as ``/repos/actions/checkout/git/ref/tags/v7.0.1``.

    Returns:
        The response status and its decoded JSON body, which callers narrow themselves. A body
        that is not JSON at all is carried as None rather than raising, because an error status
        may legitimately have none; only an undecodable *success* raises.

    Raises:
        TransportError: If the request produced no HTTP response, or a 200 whose body was not
            decodable JSON. Neither says anything about the pin.
    """
    # The suppression below answers `httpsconnection-detected`, which warns that Python before
    # 3.4.3 does not verify certificates by default. AD-24 makes 3.13 the supported floor and
    # `requires-python` enforces it, so no interpreter this runs on predates verification. It is
    # suppressed by rule id rather than blanket, and AD-43 records it as this repository's one
    # semgrep suppression. Semgrep honors the directive only on the matched line or the one
    # directly above it, which is why the reasoning is here and the directive is there; the
    # trailing `noqa` is because Ruff reads the bare rule name as commented-out code.
    # nosemgrep: httpsconnection-detected  # noqa: ERA001
    connection = http.client.HTTPSConnection(_API_HOST, timeout=_TIMEOUT_SECONDS)
    try:
        connection.request("GET", path, headers={**_HEADERS, **_authorization()})
        response = connection.getresponse()
        status = response.status
        body = response.read()
    except (OSError, http.client.HTTPException) as error:
        raise TransportError(f"GET {path} failed: {error}") from error
    finally:
        connection.close()
    try:
        return Response(status=status, payload=json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # Both are `ValueError`, and neither is caught by `check`, so letting either escape would
        # end the run on a traceback -- with the interpreter's exit 1, which is this script's
        # *finding* code. An unreadable body would then read as a mislabeled pin, which is the one
        # confusion the finding-versus-failure split exists to prevent. `json.loads` decodes bytes
        # itself, so invalid UTF-8 raises the decode error rather than the JSON one.
        if status != _HTTP_OK:
            return Response(status=status, payload=None)
        raise TransportError(f"GET {path} did not return JSON: {error}") from error


def parse_pin(fragment: str) -> Pin:
    """Split one ``uses:`` fragment into the commit SHA and the release it claims.

    Args:
        fragment: A pin as written, such as ``actions/checkout@<sha> # v7.0.1``.

    Returns:
        The action, its pinned SHA, and the exact release its comment names.

    Raises:
        PinFormatError: If the fragment carries no version comment, is not pinned to a
            40-character lowercase commit SHA, or names something other than an exact
            ``vX.Y.Z`` release. Each of those is a claim about the pin, so callers report it as
            a correspondence finding rather than as a failure of this check.
    """
    reference, marker, comment = fragment.partition("#")
    if not marker:
        raise PinFormatError(f"carries no trailing '# vX.Y.Z' version comment: {fragment!r}")
    action, at_sign, sha = reference.strip().partition("@")
    if not at_sign:
        raise PinFormatError(f"names no pinned ref: {reference.strip()!r}")
    version = comment.strip()
    if not _COMMIT_SHA.match(sha):
        raise PinFormatError(
            f"{action} is not pinned to a 40-character lowercase commit SHA: {sha!r}"
        )
    if not _EXACT_VERSION.match(version):
        raise PinFormatError(
            f"{action} names {version!r}, which is not an exact vX.Y.Z release; a moving channel "
            f"resolves to whatever it was last pointed at, so no commit can be compared to it"
        )
    return Pin(action=action, sha=sha, version=version)


def _commit_sha(payload: object) -> str | None:
    """Return the commit SHA a commits-endpoint payload carries, or None when it carries none."""
    if not isinstance(payload, dict):
        return None
    sha = payload.get("sha")
    return sha if isinstance(sha, str) and _COMMIT_SHA.match(sha) else None


def resolve_pin(fetch: Callable[[str], Response], pin: Pin) -> Outcome:
    """Establish whether one pin's SHA is the commit its release tag names.

    The reference probe answers only one question -- does this tag exist -- and its own SHA is
    then discarded, because for an annotated tag it is the tag object rather than the commit.
    The commits endpoint peels both tag kinds, so it is the only comparable value.

    Args:
        fetch: The transport to read the API through.
        pin: The parsed pin to establish correspondence for.

    Returns:
        A clean outcome, a correspondence finding, or an infrastructure failure.
    """
    fragment = f"{pin.action}@{pin.sha} # {pin.version}"
    probe = fetch(f"/repos/{pin.action}/git/ref/tags/{pin.version}")
    if probe.status == _HTTP_NOT_FOUND:
        return Outcome(fragment, FINDING, f"{pin.action} has no tag {pin.version}")
    if probe.status != _HTTP_OK:
        return Outcome(
            fragment,
            FAILURE,
            f"probing {pin.action} for tag {pin.version} returned HTTP {probe.status}",
        )
    commit = fetch(f"/repos/{pin.action}/commits/tags/{pin.version}")
    if commit.status != _HTTP_OK:
        return Outcome(
            fragment,
            FAILURE,
            f"resolving {pin.action} tag {pin.version} to a commit returned HTTP {commit.status}",
        )
    resolved = _commit_sha(commit.payload)
    if resolved is None:
        return Outcome(
            fragment,
            FAILURE,
            f"the commit payload for {pin.action} tag {pin.version} carried no 'sha' string",
        )
    if resolved != pin.sha:
        return Outcome(
            fragment,
            FINDING,
            f"{pin.action} {pin.version} is commit {resolved}, but the pin names {pin.sha}",
        )
    return Outcome(fragment, CLEAN, f"{pin.action} {pin.version} is commit {pin.sha}")


def check(fetch: Callable[[str], Response], fragments: Iterable[str]) -> list[Outcome]:
    """Establish correspondence for every pin, one outcome each.

    Each pin is handled on its own so a transport failure reading one of them cannot hide a
    finding about the other.

    Args:
        fetch: The transport to read the API through.
        fragments: The pin fragments to check, in report order.

    Returns:
        One outcome per fragment, in the order they were supplied.
    """
    outcomes: list[Outcome] = []
    for fragment in fragments:
        try:
            outcomes.append(resolve_pin(fetch, parse_pin(fragment)))
        except PinFormatError as error:
            outcomes.append(Outcome(fragment, FINDING, str(error)))
        except (TransportError, OSError) as error:
            outcomes.append(Outcome(fragment, FAILURE, str(error)))
    return outcomes


def _cell(text: str) -> str:
    """Return text safe to place inside a Markdown table cell."""
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("|", r"\|")


def render_summary(outcomes: Sequence[Outcome]) -> str:
    """Render the job summary for one correspondence run.

    Args:
        outcomes: What the check established about each pin.

    Returns:
        GitHub-flavored Markdown, ending in a newline.
    """
    lines = [
        "## Action pin correspondence",
        "",
        f"Pins checked: {len(outcomes)}.",
        "",
        *_TABLE_HEADER,
    ]
    lines.extend(
        f"| `{_cell(outcome.fragment)}` | {outcome.kind} | {_cell(outcome.detail)} |"
        for outcome in outcomes
    )
    if any(outcome.kind != CLEAN for outcome in outcomes):
        lines.extend(("", _POINTER))
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line, defaulting to the pins this repository ships."""
    parser = argparse.ArgumentParser(
        description="Check each pinned action SHA against the release its comment names."
    )
    parser.add_argument(
        "--pin",
        action="append",
        metavar="FRAGMENT",
        help=(
            "A pin to check, as 'owner/action@<sha> # vX.Y.Z'. Repeatable. Defaults to the pins "
            "constants.py ships. Supply one to prove a failure mode live without editing the "
            "constants and every copy the parity tests hold to them."
        ),
    )
    return parser.parse_args(argv)


def annotation_lines(outcomes: Sequence[Outcome]) -> list[str]:
    """Render one workflow-log annotation per outcome that is not clean.

    `emit` owns the writing and knows nothing of an outcome, so the vocabulary a reader sees --
    which kinds are worth annotating, and what each one is called -- is decided here.

    Args:
        outcomes: What the check established about each pin, in report order.

    Returns:
        One ``::error::`` line per finding and per failure, in the order the pins were checked.
    """
    return [
        f"::error::{_LABELS[outcome.kind]}: {outcome.detail}"
        for outcome in outcomes
        if outcome.kind != CLEAN
    ]


def main(argv: Sequence[str] | None = None, fetch: Callable[[str], Response] = fetch_json) -> int:
    """Check every pin and report what was established about each.

    Args:
        argv: Command-line arguments, or None to read ``sys.argv``.
        fetch: The transport to read the API through.

    Returns:
        1 when any pin has a correspondence finding, 2 when none does but the check could not
        establish one of them or could not report what it established, and 0 otherwise. A finding
        outranks both: a mislabeled pin is actionable now, and letting an unrelated outage on the
        other pin -- or a failed write of a report that already reached stdout -- mask it would
        report the weaker of the two answers.
    """
    args = _parse_args(argv)
    outcomes = check(fetch, args.pin or SHIPPED_PINS)
    reported = emit(render_summary(outcomes), annotation_lines(outcomes))
    kinds = {outcome.kind for outcome in outcomes}
    if FINDING in kinds:
        return 1
    return 2 if FAILURE in kinds or not reported else 0


if __name__ == "__main__":
    sys.exit(main())
