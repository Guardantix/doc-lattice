"""Fail-closed guard identity shared by the CI shell scanner and its taint analysis.

Every fail-closed guard in the CI shell scanner has one *origin*: the site that detects the
condition. `GuardRefusal` is the immutable value constructed at an origin, and it is the only
refusal shape the scanner's exceptions and results accept. Transports (exception handlers,
deferred fields, result projections) carry that same object rather than re-deriving one from
text, so a refusal observed at the public boundary still names the guard that produced it.

See AD-20 in ARCHITECTURE.md for the durable decision this module implements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True, slots=True)
class GuardRefusal:
    """One fail-closed guard origin's stable identity and operator-facing reason.

    Attributes:
        origin_id: Stable semantic identifier for the guard origin that refused. Origin identity,
            never transport identity: a handler that re-raises or re-wraps a refusal preserves
            this value instead of minting a new one.
        reason: The operator-facing text for the refusal. Unchanged by transport.
    """

    origin_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Certified:
    """The bounded scan completed and found no authored marker flow to an execution sink."""


@dataclass(frozen=True, slots=True)
class MarkerDetected:
    """Authored marker flow reaches an execution sink.

    This verdict is deliberately guard-identity-free. It reports the analysis's own conclusion
    about the script rather than a bound that stopped the analysis, so no guard origin owns it.
    """


ScanVerdict: TypeAlias = Certified | MarkerDetected | GuardRefusal  # noqa: UP040
