"""Fail-closed guard identity shared by the CI shell scanner and its taint analysis.

Every fail-closed guard in the CI shell scanner has one *origin*: the site that detects the
condition. `GuardRefusal` is the immutable value constructed at an origin, and it is the only
refusal shape the scanner's exceptions and results accept. Transports (exception handlers,
deferred fields, result projections) carry that same object rather than re-deriving one from
text, so a refusal observed at the public boundary still names the guard that produced it.

See AD-20 in ARCHITECTURE.md for the durable decision this module implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TaintLimits:
    """Deterministic caps for one taint pass."""

    max_alternatives: int = 256
    max_expression_nodes: int = 100_000
    max_table_entries: int = 10_000
    max_edges: int = 50_000
    max_fixed_point_updates: int = 100_000
    max_brace_expansions: int = 4_096
    max_brace_depth: int = 16
    max_exact_value_chars: int = 8_192
    max_eval_reparse_branches: int = 256
    max_eval_reparse_depth: int = 128
    max_function_effect_depth: int = 64
    max_local_substitution_depth: int = 128


@dataclass(frozen=True, slots=True)
class ScannerLimits:
    """Deterministic caps for one bounded shell scan."""

    max_source_chars: int = 1_048_576
    max_scan_steps: int = 4_194_304
    max_recursion_depth: int = 64
    max_invocations: int = 10_000
    max_launcher_nesting_depth: int = 64
    max_case_arms: int = 256
    max_case_dynamic_branches: int = 32


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Every deterministic cap for one scan, constructed once at the public boundary.

    One immutable value is threaded through the scanner, content construction, and the taint
    pass, so a shrunk cap reaches every guard that enforces it instead of some layer silently
    falling back to a fresh default.
    """

    taint: TaintLimits = field(default_factory=TaintLimits)
    scanner: ScannerLimits = field(default_factory=ScannerLimits)


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


# Spelled as a `type` statement rather than the `TypeAlias` assignment used elsewhere in this
# package. The guard inventory's constructor-reference rule rejects a tracked constructor named
# anywhere it cannot follow, and the value of a `TypeAlias` assignment is such a position: the
# annotation is unenforced, so the same spelling could hold a callable. A `type` statement binds a
# lazily evaluated `TypeAliasType` that is not callable, which is why the rule reads it as the type
# declaration it is. See AD-20.
#
# This spelling is therefore gate-required, not style: reverting it to `TypeAlias` fails
# `scripts/check_guard_inventory.py`. The `TypeAlias` aliases in `shell_taint.py` name no tracked
# constructor and so are untouched by the rule, whether they predate it or not; a future ruff
# UP040 cleanup must carry that awareness rather than normalizing every alias in the package in
# either direction.
type ScanVerdict = Certified | MarkerDetected | GuardRefusal
