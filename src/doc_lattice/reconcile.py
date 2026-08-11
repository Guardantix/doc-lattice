"""Plan reconcile updates: recompute upstream hashes and build exact-byte rewrites.

Nothing here touches the filesystem: ``plan_rewrites`` reads only through a reader its
caller injects, and ``reconcile_transaction`` owns durable publication of the rewrites
planned here.
"""

import json
from collections import defaultdict
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import (
    AliasEvent,
    Event,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from ruamel.yaml.tokens import ScalarToken, Token

from .error_types import BrokenRefError, UnreadableDocError, ValidationError
from .frontmatter_parser import split_frontmatter
from .hashing import normalize_newlines
from .model import Lattice, TargetId, parse_ref
from .resolve import cached_target_hash


@dataclass(frozen=True, slots=True)
class Rewrite:
    """Describe one exact-byte reconcile rewrite.

    Attributes:
        path: Document identity path for the rewrite.
        before: Exact source bytes read before planning the rewrite.
        after: UTF-8 replacement bytes with planned updates applied.
        applied: Refs whose seen scalar changed.
    """

    path: Path
    before: bytes
    after: bytes
    applied: frozenset[str]


@dataclass(frozen=True, slots=True)
class _SourceEdit:
    start: int
    end: int
    replacement: str


@dataclass(frozen=True, slots=True)
class _ScalarOccurrence:
    start: int
    end: int
    column: int
    value: str
    style: str | None
    anchor: str | None


@dataclass(frozen=True, slots=True)
class _AnchoredSeen:
    occurrence: _ScalarOccurrence
    value: str | None


@dataclass(frozen=True, slots=True)
class _AliasOccurrence:
    start: int
    end: int
    anchor: str


@dataclass(frozen=True, slots=True)
class _MappingOccurrence:
    start: int
    end: int
    flow_style: bool
    value: tuple[tuple["_YamlOccurrence", "_YamlOccurrence"], ...]


@dataclass(frozen=True, slots=True)
class _SequenceOccurrence:
    start: int
    end: int
    value: tuple["_YamlOccurrence", ...]


@dataclass(frozen=True, slots=True)
class _ScalarSpan:
    start: int
    end: int


type _YamlOccurrence = (
    _ScalarOccurrence | _AliasOccurrence | _MappingOccurrence | _SequenceOccurrence
)


def _parse_yaml_occurrence(events: list[Event], index: int) -> tuple[_YamlOccurrence, int]:
    event = events[index]
    if isinstance(event, ScalarEvent):
        return (
            _ScalarOccurrence(
                event.start_mark.index,
                event.end_mark.index,
                event.start_mark.column,
                event.value,
                event.style,
                event.anchor,
            ),
            index + 1,
        )
    if isinstance(event, AliasEvent):
        return (
            _AliasOccurrence(event.start_mark.index, event.end_mark.index, event.anchor),
            index + 1,
        )
    if isinstance(event, MappingStartEvent):
        pairs: list[tuple[_YamlOccurrence, _YamlOccurrence]] = []
        next_index = index + 1
        while not isinstance(events[next_index], MappingEndEvent):
            key, next_index = _parse_yaml_occurrence(events, next_index)
            value, next_index = _parse_yaml_occurrence(events, next_index)
            pairs.append((key, value))
        end_event = events[next_index]
        return (
            _MappingOccurrence(
                event.start_mark.index,
                end_event.end_mark.index,
                bool(event.flow_style),
                tuple(pairs),
            ),
            next_index + 1,
        )
    if isinstance(event, SequenceStartEvent):
        values: list[_YamlOccurrence] = []
        next_index = index + 1
        while not isinstance(events[next_index], SequenceEndEvent):
            value, next_index = _parse_yaml_occurrence(events, next_index)
            values.append(value)
        end_event = events[next_index]
        return (
            _SequenceOccurrence(event.start_mark.index, end_event.end_mark.index, tuple(values)),
            next_index + 1,
        )
    raise UnreadableDocError("frontmatter structure is malformed; cannot reconcile")


def _source_occurrence_tree(events: list[Event]) -> _YamlOccurrence:
    for index, event in enumerate(events):
        if isinstance(event, (ScalarEvent, AliasEvent, MappingStartEvent, SequenceStartEvent)):
            root, _ = _parse_yaml_occurrence(events, index)
            return root
    raise UnreadableDocError("frontmatter structure is malformed; cannot reconcile")


def _scalar_spans(tokens: list[Token]) -> tuple[_ScalarSpan, ...]:
    return tuple(
        _ScalarSpan(token.start_mark.index, token.end_mark.index)
        for token in tokens
        if isinstance(token, ScalarToken)
    )


def _mapping_value_occurrence(mapping: _MappingOccurrence, name: str) -> _YamlOccurrence | None:
    for key, value in mapping.value:
        if isinstance(key, _ScalarOccurrence) and key.value == name:
            return value
    return None


def _mapping_key_occurrence(mapping: _MappingOccurrence, name: str) -> _ScalarOccurrence | None:
    for key, _ in mapping.value:
        if isinstance(key, _ScalarOccurrence) and key.value == name:
            return key
    return None


def _null_seen_source_edit(
    raw_meta: str,
    entry: _MappingOccurrence,
    seen_key: _ScalarOccurrence,
    new_seen: str,
) -> _SourceEdit:
    colon_at = raw_meta.find(":", seen_key.end, entry.end)
    if colon_at == -1:
        raise UnreadableDocError("frontmatter derives_from entry seen is malformed")
    value_start = colon_at + 1
    boundary = entry.end - 1 if entry.flow_style else entry.end
    markers = [raw_meta.find(marker, value_start, entry.end) for marker in ("#", "\n", ",", "}")]
    value_end = min((marker for marker in markers if marker != -1), default=boundary)
    suffix = " " if value_end < len(raw_meta) and raw_meta[value_end] == "#" else ""
    return _SourceEdit(value_start, value_end, f" {new_seen}{suffix}")


def _flow_mapping_has_trailing_comma(raw_meta: str, entry: _MappingOccurrence) -> bool:
    _, final_value = entry.value[-1]
    trailing_content = raw_meta[final_value.end : entry.end - 1]
    # This begins after the final parsed value, so hashes in this segment introduce comments.
    without_comments = "".join(line.partition("#")[0] for line in trailing_content.splitlines())
    return "," in without_comments


def _scalar_span(
    occurrence: _ScalarOccurrence, scalar_spans: tuple[_ScalarSpan, ...]
) -> _ScalarSpan | None:
    matches = [
        span
        for span in scalar_spans
        if occurrence.start <= span.start and span.end <= occurrence.end
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise UnreadableDocError("frontmatter derives_from entry seen is malformed")
    return matches[0]


def _scalar_source_end(raw_meta: str, occurrence: _ScalarOccurrence, span: _ScalarSpan) -> int:
    if occurrence.style in {"|", ">"} and raw_meta[span.end - 1 : span.end] == "\n":
        return span.end - 1
    return span.end


def _alias_occurrences(occurrence: _YamlOccurrence, anchor: str) -> list[_AliasOccurrence]:
    aliases: list[_AliasOccurrence] = []
    if isinstance(occurrence, _AliasOccurrence):
        if occurrence.anchor == anchor:
            aliases.append(occurrence)
    elif isinstance(occurrence, _MappingOccurrence):
        for key, value in occurrence.value:
            aliases.extend(_alias_occurrences(key, anchor))
            aliases.extend(_alias_occurrences(value, anchor))
    elif isinstance(occurrence, _SequenceOccurrence):
        for value in occurrence.value:
            aliases.extend(_alias_occurrences(value, anchor))
    return aliases


def _seen_anchor_relocation_edit(
    anchored_seen: _AnchoredSeen,
    alias: _AliasOccurrence,
) -> _SourceEdit:
    seen = anchored_seen.occurrence
    if seen.anchor is None:
        raise UnreadableDocError("frontmatter derives_from entry seen anchor is malformed")
    scalar_source = (
        "null"
        if anchored_seen.value is None
        else json.dumps(anchored_seen.value, ensure_ascii=False)
    )
    return _SourceEdit(alias.start, alias.end, f"&{seen.anchor} {scalar_source}")


def _seen_source_edit(
    raw_meta: str,
    entry: _MappingOccurrence,
    scalar_spans: tuple[_ScalarSpan, ...],
    new_seen: str,
) -> _SourceEdit:
    seen = _mapping_value_occurrence(entry, "seen")
    if isinstance(seen, _AliasOccurrence):
        return _SourceEdit(seen.start, seen.end, new_seen)
    if isinstance(seen, _ScalarOccurrence):
        span = _scalar_span(seen, scalar_spans)
        if span is None:
            seen_key = _mapping_key_occurrence(entry, "seen")
            if seen_key is None:
                raise UnreadableDocError("frontmatter derives_from entry seen is malformed")
            return _null_seen_source_edit(raw_meta, entry, seen_key, new_seen)
        start = seen.start if seen.anchor is not None else span.start
        return _SourceEdit(start, _scalar_source_end(raw_meta, seen, span), new_seen)
    if seen is not None:
        raise UnreadableDocError("frontmatter derives_from entry seen is malformed")
    if entry.flow_style:
        insert_at = entry.end - 1
        if _flow_mapping_has_trailing_comma(raw_meta, entry):
            separator = "" if raw_meta[insert_at - 1].isspace() else " "
        else:
            separator = ", "
        return _SourceEdit(insert_at, insert_at, f"{separator}seen: {new_seen}")
    first_key, _ = entry.value[0]
    if not isinstance(first_key, _ScalarOccurrence):
        raise UnreadableDocError("frontmatter derives_from entry is malformed")
    insert_at = entry.end
    replacement = f"{' ' * first_key.column}seen: {new_seen}\n"
    return _SourceEdit(insert_at, insert_at, replacement)


def _mapping_alias_source_edit(
    raw_meta: str, alias: _AliasOccurrence, new_seen: str
) -> _SourceEdit:
    alias_source = raw_meta[alias.start : alias.end]
    return _SourceEdit(alias.start, alias.end, f"{{<<: {alias_source}, seen: {new_seen}}}")


def _append_seen_anchor_relocations(
    source_root: _YamlOccurrence,
    anchored_seen: list[_AnchoredSeen],
    edits: list[_SourceEdit],
) -> None:
    edited_spans = {(edit.start, edit.end) for edit in edits}
    for anchored in anchored_seen:
        seen = anchored.occurrence
        if seen.anchor is None:
            continue
        untouched_aliases = [
            alias
            for alias in _alias_occurrences(source_root, seen.anchor)
            if (alias.start, alias.end) not in edited_spans
        ]
        if untouched_aliases:
            relocation = _seen_anchor_relocation_edit(anchored, untouched_aliases[0])
            edits.append(relocation)
            edited_spans.add((relocation.start, relocation.end))


def _apply_source_edits(raw_meta: str, edits: list[_SourceEdit]) -> str:
    unique_edits: dict[tuple[int, int], _SourceEdit] = {}
    for edit in edits:
        span = (edit.start, edit.end)
        existing = unique_edits.get(span)
        if existing is not None and existing.replacement != edit.replacement:
            raise UnreadableDocError("frontmatter aliases require conflicting seen updates")
        unique_edits[span] = edit
    for edit in sorted(unique_edits.values(), key=lambda item: item.start, reverse=True):
        raw_meta = raw_meta[: edit.start] + edit.replacement + raw_meta[edit.end :]
    return raw_meta


def reconcile(
    lattice: Lattice, downstream_id: str, *, ref: str | None, reconcile_all: bool
) -> dict[Path, dict[str, str]]:
    """Plan the seen-scalar updates needed to clear drift for the selection.

    Selection: when ``reconcile_all`` is True, every STALE and UNRECONCILED edge across
    the lattice is updated, narrowed to edges matching ``ref`` when one is given; BROKEN
    and already-OK edges are skipped, and no match is a successful no-op rather than an
    error.  When targeting a specific node, all of that node's STALE or UNRECONCILED
    edges are updated (or just the matching edge if ``ref`` is given); an already-OK edge
    is skipped in both modes, since restamping it to the same hash is a no-op.  The match
    uses the parsed TargetId so an identical ref selects the same edge.  A node's BROKEN
    edge is skipped (it does not block the node's reconcilable edges); only a single-node
    ``--ref`` aimed directly at a broken edge is refused, and a single-node ``--ref`` that
    matches no edge on the node is reported rather than silently doing nothing.

    Args:
        lattice: The built lattice (its upstream content is the reconcile snapshot).
        downstream_id: The node whose edges to reconcile (ignored if ``reconcile_all``).
        ref: A single upstream ref to narrow to, or None for all of the node's edges.
        reconcile_all: Reconcile every node's STALE or UNRECONCILED edges.

    Returns:
        A mapping of downstream file path to ``{target_ref: new_seen}`` updates. The
        caller applies these via ``apply_reconcile`` and an atomic write (the CLI does).

    Raises:
        ValidationError: If ``downstream_id`` is not in the lattice, or if ``ref`` is
            given but matches no edge on the node (both only when not ``reconcile_all``).
        BrokenRefError: If ``ref`` targets an edge that has no resolvable target.
    """
    if not reconcile_all and downstream_id not in lattice.nodes_by_id:
        raise ValidationError(f"unknown downstream id {downstream_id!r}; run check to list ids")
    node_ids = sorted(lattice.nodes_by_id) if reconcile_all else [downstream_id]
    requested_target_id = parse_ref(ref) if ref is not None else None
    targeting_specific_ref = ref is not None and not reconcile_all
    plan: dict[Path, dict[str, str]] = defaultdict(dict)
    cache: dict[TargetId, str] = {}
    ref_matched = False
    for node_id in node_ids:
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            if requested_target_id is not None:
                # A broken edge has no resolved target_id, so parse its raw ref to compare.
                # Without this fallback a --ref aimed straight at a broken edge would match
                # nothing and get the generic no-such-edge error instead of the
                # BrokenRefError below.
                edge_target_id = (
                    edge.target_id if edge.target_id is not None else parse_ref(edge.target_ref)
                )
                if edge_target_id != requested_target_id:
                    continue
            ref_matched = True
            if edge.target_id is None:
                if targeting_specific_ref:
                    raise BrokenRefError(
                        f"cannot reconcile broken ref {edge.target_ref!r} on {node_id};"
                        " fix the ref first"
                    )
                continue
            new_seen = cached_target_hash(lattice, edge.target_id, cache)
            if edge.seen is not None and new_seen == edge.seen:
                continue
            plan[node.path][edge.target_ref] = new_seen
    if targeting_specific_ref and not ref_matched:
        raise ValidationError(
            f"node {downstream_id!r} has no edge matching ref {ref!r}; run check to list its edges"
        )
    return dict(plan)


def apply_reconcile(  # noqa: PLR0912
    current_file_text: str, updates: dict[str, str], source: Path
) -> tuple[str, set[str]]:
    """Return ``current_file_text`` with matching edges' seen scalars set.

    The fresh read is parsed defensively: a concurrent edit that leaves the frontmatter
    unparseable or in an unexpected shape (not a mapping, a non-list ``derives_from``, a
    non-mapping entry, a non-string entry ``ref``) raises ``UnreadableDocError`` (a
    ``ProjectError``) so the CLI exits cleanly instead of crashing with a traceback.

    Args:
        current_file_text: A fresh read of the downstream file at write time.
        updates: ``{target_ref: new_seen}`` for edges in this file.
        source: The downstream file the frontmatter came from, for error messages.

    Returns:
        A pair of the rewritten file text and the set of refs from ``updates`` whose
        ``seen`` was changed; a ref already holding its planned value is left untouched
        and excluded from the set. When nothing changed (for example a ref was edited
        away between load and write, or already held the planned hash) the original text
        is returned unchanged and the set is empty, so the caller does not report a write
        that did not happen. The body after the closing fence is reattached verbatim from
        ``current_file_text``.

    Raises:
        UnreadableDocError: If the fresh frontmatter cannot be parsed or is malformed.
    """
    raw_meta, body = split_frontmatter(current_file_text, source)
    if raw_meta is None:
        return current_file_text, set()
    # Round-trip loaders retain document-specific state, so construct one for each call.
    yaml = YAML(typ="rt")
    try:
        data = yaml.load(raw_meta)
    except YAMLError as exc:
        msg = f"cannot parse frontmatter to reconcile: {exc}"
        raise UnreadableDocError(msg) from exc
    if data is None:
        return current_file_text, set()
    try:
        source_root = _source_occurrence_tree(list(YAML(typ="base").parse(raw_meta)))
        scalar_spans = _scalar_spans(list(YAML(typ="base").scan(raw_meta)))
    except YAMLError as exc:
        msg = f"cannot parse frontmatter to reconcile: {exc}"
        raise UnreadableDocError(msg) from exc
    if not isinstance(data, MutableMapping):
        raise UnreadableDocError("frontmatter is not a mapping; cannot reconcile")
    entries = data.get("derives_from")
    if entries is None:
        return current_file_text, set()
    if not isinstance(entries, list):
        raise UnreadableDocError("frontmatter derives_from is not a list; cannot reconcile")
    if not isinstance(source_root, _MappingOccurrence):
        raise UnreadableDocError("frontmatter is not a mapping; cannot reconcile")
    entry_occurrences = _mapping_value_occurrence(source_root, "derives_from")
    if not isinstance(entry_occurrences, _SequenceOccurrence) or len(
        entry_occurrences.value
    ) != len(entries):
        raise UnreadableDocError("frontmatter derives_from is not a list; cannot reconcile")
    edits: list[_SourceEdit] = []
    anchored_seen: list[_AnchoredSeen] = []
    applied: set[str] = set()
    for entry, entry_occurrence in zip(entries, entry_occurrences.value, strict=True):
        if not isinstance(entry, MutableMapping) or not isinstance(
            entry_occurrence, (_MappingOccurrence, _AliasOccurrence)
        ):
            raise UnreadableDocError("frontmatter derives_from entry is not a mapping")
        ref = entry.get("ref")
        if not isinstance(ref, str):
            raise UnreadableDocError("frontmatter derives_from entry ref is not a string")
        if ref in updates:
            new_seen = updates[ref]
            if entry.get("seen") != new_seen:
                seen = (
                    _mapping_value_occurrence(entry_occurrence, "seen")
                    if isinstance(entry_occurrence, _MappingOccurrence)
                    else None
                )
                if isinstance(seen, _ScalarOccurrence) and seen.anchor is not None:
                    old_seen = entry.get("seen")
                    anchored_seen.append(
                        _AnchoredSeen(seen, None if old_seen is None else str(old_seen))
                    )
                edit = (
                    _mapping_alias_source_edit(raw_meta, entry_occurrence, new_seen)
                    if isinstance(entry_occurrence, _AliasOccurrence)
                    else _seen_source_edit(raw_meta, entry_occurrence, scalar_spans, new_seen)
                )
                edits.append(edit)
                applied.add(ref)
    if not applied:
        return current_file_text, applied
    _append_seen_anchor_relocations(source_root, anchored_seen, edits)
    new_meta = _apply_source_edits(raw_meta, edits)
    return f"---\n{new_meta}---\n{body}", applied


def plan_rewrites(
    plan: dict[Path, dict[str, str]],
    read_bytes: Callable[[Path], bytes],
) -> list[Rewrite]:
    """Compute exact-byte fresh-read reconcile rewrites before any write lands.

    The injected reader retains the exact source bytes for later fingerprinting and
    restoration while UTF-8 text is decoded and newline-normalized only for
    ``apply_reconcile``.

    Args:
        plan: The planned mapping of downstream file path to ``{ref: new_seen}``.
        read_bytes: Reader injected by the caller for fresh downstream file bytes.

    Returns:
        Rewrite records for files whose fresh content changed. Files whose planned
        updates are already applied are skipped.

    Raises:
        UnreadableDocError: If the injected reader cannot read a downstream file, or
            if the fresh frontmatter cannot be parsed or is malformed.
    """
    rewrites: list[Rewrite] = []
    for path, updates in plan.items():
        try:
            before = read_bytes(path)
            fresh = normalize_newlines(before.decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read {path} to reconcile: {exc}"
            raise UnreadableDocError(msg) from exc
        new_text, applied = apply_reconcile(fresh, updates, path)
        if applied:
            rewrites.append(Rewrite(path, before, new_text.encode("utf-8"), frozenset(applied)))
    return rewrites
