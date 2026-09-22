"""Pure, verified byte-local rewrites for sidecar manifests.

The caller supplies fresh path observations. This module reads no paths and never treats a
record's sequence position as identity; the position is used only after its ``meta.id`` has
selected exactly one fresh record.
"""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from . import reconcile as source_edit
from .error_types import ManifestError, UnreadableDocError
from .hashing import normalize_newlines
from .model import ExternalIdentity
from .path_utils import format_path_for_display
from .sidecar_manifest import (
    ManifestRecordSnapshot,
    iter_selected_record_positions,
    parse_manifest_snapshot,
)
from .text_utils import uniform_line_ending


def _nodes_occurrences(
    context: source_edit._SourceContext, records: tuple[ManifestRecordSnapshot, ...]
) -> source_edit._SequenceOccurrence:
    """Match the source's nodes sequence with its already validated semantic records."""
    root = context.root
    shown = format_path_for_display(context.source)
    if not isinstance(root, source_edit._MappingOccurrence):
        raise ManifestError(f"manifest {shown} is not written as a mapping")
    nodes = source_edit._resolved_member(context.anchors, root, "nodes")
    if not isinstance(nodes, source_edit._SequenceOccurrence) or len(nodes.value) != len(records):
        raise ManifestError(f"manifest {shown} source and parsed records disagree")
    return nodes


def _edge_occurrences(
    context: source_edit._SourceContext,
    record: source_edit._YamlOccurrence,
    edge_count: int,
) -> source_edit._SequenceOccurrence:
    """Locate the edge sequence inside a record after its id has been matched."""
    shown = format_path_for_display(context.source)
    if not isinstance(record, source_edit._MappingOccurrence):
        raise ManifestError(f"manifest {shown} record is not written as a mapping")
    meta = source_edit._resolved_member(context.anchors, record, "meta")
    if not isinstance(meta, source_edit._MappingOccurrence):
        raise ManifestError(f"manifest {shown} metadata is not written as a mapping")
    edges = source_edit._resolved_member(context.anchors, meta, "derives_from")
    if not isinstance(edges, source_edit._SequenceOccurrence) or len(edges.value) != edge_count:
        raise ManifestError(f"manifest {shown} source and parsed edges disagree")
    return edges


def _splice_expected_bytes(
    original: str, edits: list[source_edit._SourceEdit], ending: str
) -> bytes:
    """Independently spell the complete expected output from untouched source spans."""
    pieces: list[bytes] = []
    cursor = 0
    for edit in sorted(edits, key=lambda item: (item.start, item.end)):
        if edit.start < cursor:
            raise ManifestError("manifest source edits overlap")
        pieces.append(original[cursor : edit.start].replace("\n", ending).encode("utf-8"))
        pieces.append(edit.replacement.replace("\n", ending).encode("utf-8"))
        cursor = edit.end
    pieces.append(original[cursor:].replace("\n", ending).encode("utf-8"))
    return b"".join(pieces)


def _selected_positions(
    records: tuple[ManifestRecordSnapshot, ...],
    expected: Mapping[str, ExternalIdentity],
    observed: Mapping[str, ExternalIdentity],
    updates: Mapping[tuple[str, str], str],
) -> dict[str, int]:
    """Locate each selected id uniquely and bind it to load-time and fresh path evidence.

    Every refusal is reported in lexical node-id order, whatever its kind, because each id is
    located and checked in one pass.
    """
    selected: dict[str, int] = {}
    selected_ids = {node_id for node_id, _ in updates}
    for node_id, position in iter_selected_record_positions(records, selected_ids):
        load_identity = expected.get(node_id)
        fresh_identity = observed.get(node_id)
        if load_identity is None or fresh_identity is None:
            raise ManifestError(f"selected manifest record {node_id!r} lacks identity evidence")
        declared = records[position].declared_path
        if (
            load_identity.declared_path != declared
            or fresh_identity.declared_path != declared
            or load_identity.resolved_target != fresh_identity.resolved_target
            or load_identity.resolved_manifest != fresh_identity.resolved_manifest
        ):
            raise ManifestError(f"selected manifest record {node_id!r} was repointed")
        selected[node_id] = position
    return selected


def _plan_updates(
    context: source_edit._SourceContext,
    nodes: source_edit._SequenceOccurrence,
    records: tuple[ManifestRecordSnapshot, ...],
    selected: Mapping[str, int],
    updates: Mapping[tuple[str, str], str],
) -> tuple[list[source_edit._SourceEdit], list[ManifestRecordSnapshot]]:
    """Plan only last-occurrence edits and the complete expected semantic after-image."""
    planned: list[source_edit._SourceEdit] = []
    expected_records = list(records)
    loader = source_edit._probe_loader(context.version)
    shown = format_path_for_display(context.source)
    last_refs: dict[str, dict[str, int]] = {}
    source_edges: dict[str, source_edit._SequenceOccurrence] = {}
    for (node_id, ref), new_seen in updates.items():
        position = selected[node_id]
        record = expected_records[position]
        indices = last_refs.get(node_id)
        if indices is None:
            indices = {edge.ref: index for index, edge in enumerate(record.meta.derives_from)}
            last_refs[node_id] = indices
        index = indices.get(ref)
        if index is None:
            raise ManifestError(f"selected manifest record {node_id!r} has no ref {ref!r}")
        old_edge = record.meta.derives_from[index]
        if old_edge.seen == new_seen:
            continue
        occurrences = source_edges.get(node_id)
        if occurrences is None:
            occurrences = _edge_occurrences(
                context, nodes.value[position], len(record.meta.derives_from)
            )
            source_edges[node_id] = occurrences
        occurrence = occurrences.value[index]
        if not isinstance(occurrence, source_edit._MappingOccurrence):
            raise ManifestError(f"manifest {shown} edge is not written as a mapping")
        entry_edit = source_edit._plan_entry_edit(
            context,
            occurrence,
            old_edge.seen,
            source_edit._seen_scalar_source(new_seen, loader),
            frozenset(),
        )
        planned.extend(entry_edit.edits)
        edges = list(record.meta.derives_from)
        edges[index] = old_edge.model_copy(update={"seen": new_seen})
        expected_records[position] = replace(
            record, meta=record.meta.model_copy(update={"derives_from": edges})
        )
    return planned, expected_records


def rewrite_manifest_bytes(
    fresh_bytes: bytes,
    *,
    source: str,
    expected: Mapping[str, ExternalIdentity],
    observed: Mapping[str, ExternalIdentity],
    updates: Mapping[tuple[str, str], str],
) -> bytes:
    """Set selected logical edges' ``seen`` values in a fresh manifest after identity checks.

    Args:
        fresh_bytes: The complete, freshly read manifest bytes.
        source: Declared manifest spelling for diagnostics.
        expected: Load-time identities keyed by node id.
        observed: Freshly resolved record and manifest identities keyed by node id.
        updates: Desired ``seen`` values keyed by node id and ref.

    Returns:
        Verified after-bytes, or the original bytes for a valid no-op.

    Raises:
        ManifestError: If a selected identity, edge, format, edit, or verification is invalid.
    """
    records = parse_manifest_snapshot(fresh_bytes, source)
    shown = format_path_for_display(source)
    selected = _selected_positions(records, expected, observed, updates)
    text = fresh_bytes.decode("utf-8")
    ending = uniform_line_ending(text)
    normalized = normalize_newlines(text)
    context = source_edit._build_source_context(normalized, Path(source))
    nodes = _nodes_occurrences(context, records)
    try:
        planned, expected_records = _plan_updates(context, nodes, records, selected, updates)
        if not planned:
            return fresh_bytes
        if ending is None:
            raise ManifestError(f"manifest {shown} has mixed line endings; rewrite refused")
        if normalized.replace("\n", ending).encode("utf-8") != fresh_bytes:
            raise ManifestError(f"manifest {shown} source bytes cannot be preserved")
        rewritten = source_edit._apply_source_edits(normalized, planned)
    except UnreadableDocError as exc:
        raise ManifestError(f"cannot rewrite manifest {shown}: {exc}") from exc
    after = rewritten.replace("\n", ending).encode("utf-8")
    if after != _splice_expected_bytes(normalized, planned, ending):
        raise ManifestError(f"manifest {shown} after-bytes did not match the planned edits")
    if parse_manifest_snapshot(after, source) != tuple(expected_records):
        raise ManifestError(f"manifest {shown} rewrite changed unselected content or record order")
    return after
