"""Executable classification of every fail-closed guard origin in the CI shell scanner.

This registry *is* the guard inventory. A guard is classified by appearing here, and every entry
carries the executable evidence for its classification rather than a prose claim or a test name:

- `ReachableWitness` names an authored script (and optional shrunk limits) that drives the public
  scan path and must return that exact guard origin identifier.
- `InvariantWitness` records why authored input cannot reach the origin, plus a boundary script
  that exercises the nearest reachable state transition and the outcome it must produce.

Anything absent from both lists is unclassified rollout debt, frozen in
`tests/fixtures/shell_guard_debt.json` and gated by `scripts/check_guard_inventory.py`. The
tree-local closure check in `tests/test_github_ci_shell_guards.py` requires source origins to
partition exactly into this registry and that debt snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from doc_lattice.github_ci.shell_taint import TaintLimits


@dataclass(frozen=True, slots=True)
class ReachableWitness:
    """One authored input that provably reaches a guard origin through the public scan path.

    Attributes:
        origin_id: The guard origin identifier the scan must report.
        script: Literal Bash source handed to `scan_doc_lattice_invocations`.
        limits: Shrunk deterministic caps, when the guard is a resource bound that a realistic
            script cannot exhaust. `None` uses production caps.
        bash_runs_marker: Expected real-Bash behavior for the direction check, or `None` when the
            script is not valid Bash and the direction assertion carries no meaning.
    """

    origin_id: str
    script: str
    limits: TaintLimits | None = None
    bash_runs_marker: bool | None = None


@dataclass(frozen=True, slots=True)
class InvariantWitness:
    """One guard origin that authored input cannot reach, with its nearest reachable boundary.

    Attributes:
        origin_id: The guard origin identifier claimed unreachable from authored input.
        rationale: Why no authored input can satisfy the guard's condition. Non-empty.
        boundary_script: Authored input exercising the nearest reachable state transition, so a
            change that makes the origin reachable shows up as a changed boundary outcome.
        boundary_origin_id: Guard origin the boundary script must report, or `None` when the
            boundary certifies.
    """

    origin_id: str
    rationale: str
    boundary_script: str
    boundary_origin_id: str | None = None


REACHABLE_WITNESSES: tuple[ReachableWitness, ...] = ()

INVARIANT_WITNESSES: tuple[InvariantWitness, ...] = ()


REACHABLE_IDS = frozenset(witness.origin_id for witness in REACHABLE_WITNESSES)
INVARIANT_IDS = frozenset(witness.origin_id for witness in INVARIANT_WITNESSES)
CLASSIFIED_IDS = REACHABLE_IDS | INVARIANT_IDS
