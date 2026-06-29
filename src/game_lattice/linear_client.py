"""Synchronous Linear GraphQL transport over stdlib urllib, hardened against SSRF."""

import json
import os
import urllib.error
import urllib.request

from . import __version__
from .error_types import LinearError

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
DEFAULT_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses every redirect.

    ``urllib`` follows 3xx by default, which would let a hostile or intercepted response
    steer the credentialed client at an internal address. Returning None turns a redirect
    into an HTTPError that the client maps to a LinearError.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN202, ARG002, PLR0913
        return None


class LinearClient:
    """A GraphQL client that reads ``LINEAR_API_KEY`` lazily and never follows redirects."""

    def __init__(
        self,
        url: str = LINEAR_GRAPHQL_URL,
        timeout: float = DEFAULT_TIMEOUT,
        opener: object | None = None,
    ) -> None:
        """Build a client.

        Args:
            url: The GraphQL endpoint. Must be ``https`` so the key never crosses the wire
                in cleartext.
            timeout: Per-request timeout in seconds.
            opener: An opener with an ``open(req, timeout=...)`` method, for tests. The
                default refuses redirects.

        Raises:
            LinearError: If ``url`` is not an ``https`` URL.
        """
        if not url.startswith("https://"):
            msg = f"LinearClient refuses non-https URL {url!r}; the API key rides in a header"
            raise LinearError(msg)
        self._url = url
        self._timeout = timeout
        self._opener = opener if opener is not None else urllib.request.build_opener(_NoRedirect)

    def execute(self, document: str, variables: dict[str, str | list[int]]) -> str:
        """POST a GraphQL document and return the raw response text.

        Args:
            document: The GraphQL query document.
            variables: The query variables.

        Returns:
            The decoded response body. GraphQL ``errors`` and ``data`` are interpreted by the
            parser, not here.

        Raises:
            LinearError: On a missing key, an HTTP or URL error, or an oversized response. The
                message never includes the key or the Authorization header.
        """
        api_key = os.environ.get("LINEAR_API_KEY", "").strip()
        if not api_key:
            msg = "LINEAR_API_KEY is not set; export it, or run impact for the offline view"
            raise LinearError(msg)
        payload = json.dumps({"query": document, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - https scheme enforced in __init__
            self._url,
            data=payload,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "User-Agent": f"game-lattice/{__version__}",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as resp:  # ty: ignore[unresolved-attribute]
                # Read one byte past the cap so an at-the-limit body stays distinguishable from
                # an over-the-limit one; the len() check below then rejects only the latter.
                body = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise LinearError(
                f"Linear HTTP error {exc.code}; if it is a 429 or 5xx wait and re-run, "
                "or run impact for the offline view"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            # A connect-phase failure arrives as URLError (urllib wraps it). A body-read
            # timeout or reset is a bare OSError/TimeoutError raised after open() returns,
            # which the URLError branch alone would miss and let escape as an internal error.
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise LinearError(
                f"Linear network error: {reason}; check the connection and re-run, "
                "or run impact for the offline view"
            ) from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise LinearError("Linear response exceeded the size cap; refusing to parse it")
        return body.decode("utf-8", errors="replace")
