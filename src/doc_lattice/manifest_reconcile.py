"""Pure orchestration for verified sidecar-manifest reconcile rewrites."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .model import ExternalIdentity
from .reconcile import (
    ReconcileDestinationPlan,
    ReconcileKey,
    ReconcilePlan,
    Rewrite,
    is_manifest_group,
)
from .sidecar_manifest import ManifestRecordSnapshot, parse_manifest_snapshot
from .sidecar_rewrite import rewrite_manifest_bytes


@dataclass(frozen=True, slots=True)
class ManifestChange:
    """One changed logical edge and its position in the fresh manifest."""

    node_id: str
    ref: str
    record_index: int


@dataclass(frozen=True, slots=True)
class ManifestRewriteResult:
    """One verified manifest replacement kept separate from transaction rewrites."""

    destination: Path
    before: bytes
    verified_bytes: bytes
    changed: tuple[ManifestChange, ...]


@dataclass(frozen=True, slots=True)
class ManifestCaptureRequest:
    """What the I/O boundary must capture fresh for one manifest destination.

    Attributes:
        declared: The manifest's ``sidecar_manifests`` spelling, which the boundary resolves
            again rather than reusing the load-time path the destination is keyed on.
        selected_ids: Node ids whose records the boundary observes in the fresh bytes.
    """

    declared: str
    selected_ids: frozenset[str]


def _manifest_arguments(
    destination: Path, logical_updates: ReconcilePlan
) -> tuple[str, dict[str, ExternalIdentity], dict[ReconcileKey, str]]:
    """Derive the rewriter's pure inputs from one manifest destination group."""
    source: str | None = None
    expected: dict[str, ExternalIdentity] = {}
    updates: dict[ReconcileKey, str] = {}
    for key, update in logical_updates.items():
        node_id, _ref = key
        declaration = update.origin.declaration
        identity = update.origin.identity
        if declaration is None or identity is None:
            raise ValueError("manifest reconcile groups require external identity evidence")
        if identity.resolved_manifest != destination:
            raise ValueError("manifest reconcile group does not match its resolved destination")
        if source is None:
            source = declaration.manifest_path
        elif source != declaration.manifest_path:
            raise ValueError("manifest reconcile group carries conflicting manifest declarations")
        previous = expected.setdefault(node_id, identity)
        if previous != identity:
            raise ValueError(f"manifest node {node_id!r} carries conflicting identities")
        updates[key] = update.new_seen
    if source is None:
        raise ValueError("manifest reconcile group is empty")
    return source, expected, updates


def _changed_pairs(
    before: tuple[ManifestRecordSnapshot, ...],
    after: tuple[ManifestRecordSnapshot, ...],
) -> tuple[ManifestChange, ...]:
    """Report verified edge changes at their freshly parsed record positions."""
    changed: list[ManifestChange] = []
    for record_index, (old_record, new_record) in enumerate(zip(before, after, strict=True)):
        for old_edge, new_edge in zip(
            old_record.meta.derives_from, new_record.meta.derives_from, strict=True
        ):
            if old_edge.seen != new_edge.seen:
                changed.append(ManifestChange(new_record.meta.id, new_edge.ref, record_index))
    return tuple(changed)


def manifest_capture_requests(plan: ReconcileDestinationPlan) -> dict[Path, ManifestCaptureRequest]:
    """Name the fresh capture each manifest destination in ``plan`` needs.

    Args:
        plan: Logical updates grouped by their write destination.

    Returns:
        One request per manifest destination, keyed as ``plan`` keys it. Inline groups need no
        capture and are absent.

    Raises:
        ValueError: If a manifest group does not carry coherent external origin evidence.
    """
    requests: dict[Path, ManifestCaptureRequest] = {}
    for destination, logical_updates in plan.items():
        if is_manifest_group(logical_updates):
            source, expected, _updates = _manifest_arguments(destination, logical_updates)
            requests[destination] = ManifestCaptureRequest(source, frozenset(expected))
    return requests


def plan_manifest_rewrites(
    plan: ReconcileDestinationPlan,
    *,
    fresh_bytes: Mapping[Path, bytes],
    observations: Mapping[Path, Mapping[str, ExternalIdentity]],
) -> tuple[ReconcileDestinationPlan, tuple[ManifestRewriteResult, ...]]:
    """Plan verified manifest rewrites while returning inline groups unchanged.

    Args:
        plan: Logical updates grouped by their write destination.
        fresh_bytes: Captured bytes for each manifest destination in ``plan``.
        observations: Fresh selected-record identities for each manifest destination.

    Returns:
        Inline destination groups and changed manifest results. A manifest whose selected
        effective refs are all current produces no result.

    Raises:
        ManifestError: If manifest bytes, identities, or rewrites fail validation.
        ValueError: If a manifest group does not carry coherent external origin evidence, or
            if its destination has no captured bytes or fresh observation.
    """
    inline: ReconcileDestinationPlan = {}
    rewrites: list[ManifestRewriteResult] = []
    for destination, logical_updates in plan.items():
        if not is_manifest_group(logical_updates):
            inline[destination] = logical_updates
            continue
        source, expected, updates = _manifest_arguments(destination, logical_updates)
        if destination not in fresh_bytes or destination not in observations:
            raise ValueError("manifest reconcile group lacks a fresh capture or observation")
        before = fresh_bytes[destination]
        verified = rewrite_manifest_bytes(
            before,
            source=source,
            expected=expected,
            observed=observations[destination],
            updates=updates,
        )
        if verified == before:
            continue
        before_records = parse_manifest_snapshot(before, source)
        after_records = parse_manifest_snapshot(verified, source)
        changed = _changed_pairs(before_records, after_records)
        rewrites.append(ManifestRewriteResult(destination, before, verified, changed))
    return inline, tuple(rewrites)


def transaction_rewrite(result: ManifestRewriteResult) -> Rewrite:
    """Convert one verified manifest result into the rewrite the transaction publishes.

    AD-30 admits this as the manifest producer: the after image is the result's
    ``verified_bytes`` exactly, which ``plan_manifest_rewrites`` binds from
    ``sidecar_rewrite.rewrite_manifest_bytes`` and nowhere else.

    Args:
        result: One changed manifest from ``plan_manifest_rewrites``.

    Returns:
        The exact-byte rewrite of that manifest, whose ``applied`` refs are the changed edges'.
    """
    return Rewrite(
        result.destination,
        result.before,
        result.verified_bytes,
        frozenset(change.ref for change in result.changed),
    )
