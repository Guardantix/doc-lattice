"""Plan reconcile updates: recompute upstream hashes and build exact-byte rewrites.

Nothing here touches the filesystem: ``plan_rewrites`` reads only through a reader its
caller injects, and ``reconcile_transaction`` owns durable publication of the rewrites
planned here.
"""

from collections import defaultdict
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

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


def _mapping_value_node(mapping: MappingNode, name: str) -> Node | None:
    for key_node, value_node in mapping.value:
        if isinstance(key_node, ScalarNode) and key_node.value == name:
            return value_node
    return None


def _mapping_key_node(mapping: MappingNode, name: str) -> ScalarNode | None:
    for key_node, _ in mapping.value:
        if isinstance(key_node, ScalarNode) and key_node.value == name:
            return key_node
    return None


def _null_seen_source_edit(
    raw_meta: str, entry: MappingNode, seen_node: Node, new_seen: str
) -> _SourceEdit:
    seen_key = _mapping_key_node(entry, "seen")
    if seen_key is None:
        raise UnreadableDocError("frontmatter derives_from entry seen is malformed")
    colon_at = raw_meta.find(":", seen_key.end_mark.index, seen_node.start_mark.index)
    if colon_at == -1:
        raise UnreadableDocError("frontmatter derives_from entry seen is malformed")
    line_end = raw_meta.find("\n", colon_at, seen_node.start_mark.index)
    comment_at = raw_meta.find("#", colon_at, line_end)
    if comment_at == -1:
        return _SourceEdit(colon_at + 1, line_end, f" {new_seen}")
    return _SourceEdit(colon_at + 1, comment_at, f" {new_seen} ")


def _flow_mapping_has_trailing_comma(raw_meta: str, entry: MappingNode) -> bool:
    _, final_value = entry.value[-1]
    trailing_content = raw_meta[final_value.end_mark.index : entry.end_mark.index - 1]
    # This begins after the final parsed value, so hashes in this segment introduce comments.
    without_comments = "".join(line.partition("#")[0] for line in trailing_content.splitlines())
    return "," in without_comments


def _seen_source_edit(raw_meta: str, entry: MappingNode, new_seen: str) -> _SourceEdit:
    seen_node = _mapping_value_node(entry, "seen")
    if seen_node is not None:
        if seen_node.start_mark.index == seen_node.end_mark.index:
            return _null_seen_source_edit(raw_meta, entry, seen_node, new_seen)
        return _SourceEdit(seen_node.start_mark.index, seen_node.end_mark.index, new_seen)
    if entry.flow_style:
        insert_at = entry.end_mark.index - 1
        if _flow_mapping_has_trailing_comma(raw_meta, entry):
            separator = "" if raw_meta[insert_at - 1].isspace() else " "
        else:
            separator = ", "
        return _SourceEdit(insert_at, insert_at, f"{separator}seen: {new_seen}")
    first_key, _ = entry.value[0]
    insert_at = entry.end_mark.index
    replacement = f"{' ' * first_key.start_mark.column}seen: {new_seen}\n"
    return _SourceEdit(insert_at, insert_at, replacement)


def _apply_source_edits(raw_meta: str, edits: list[_SourceEdit]) -> str:
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
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
    node_yaml = YAML(typ="base")
    try:
        data = yaml.load(raw_meta)
        root_node = node_yaml.compose(raw_meta)
    except YAMLError as exc:
        msg = f"cannot parse frontmatter to reconcile: {exc}"
        raise UnreadableDocError(msg) from exc
    if data is None:
        return current_file_text, set()
    if not isinstance(data, MutableMapping):
        raise UnreadableDocError("frontmatter is not a mapping; cannot reconcile")
    entries = data.get("derives_from")
    if entries is None:
        return current_file_text, set()
    if not isinstance(entries, list):
        raise UnreadableDocError("frontmatter derives_from is not a list; cannot reconcile")
    if not isinstance(root_node, MappingNode):
        raise UnreadableDocError("frontmatter is not a mapping; cannot reconcile")
    entry_nodes = _mapping_value_node(root_node, "derives_from")
    if not isinstance(entry_nodes, SequenceNode) or len(entry_nodes.value) != len(entries):
        raise UnreadableDocError("frontmatter derives_from is not a list; cannot reconcile")

    edits: list[_SourceEdit] = []
    applied: set[str] = set()
    for entry, entry_node in zip(entries, entry_nodes.value, strict=True):
        if not isinstance(entry, MutableMapping) or not isinstance(entry_node, MappingNode):
            raise UnreadableDocError("frontmatter derives_from entry is not a mapping")
        ref = entry.get("ref")
        if not isinstance(ref, str):
            raise UnreadableDocError("frontmatter derives_from entry ref is not a string")
        if ref in updates:
            new_seen = updates[ref]
            if entry.get("seen") != new_seen:
                edits.append(_seen_source_edit(raw_meta, entry_node, new_seen))
                applied.add(ref)
    if not applied:
        return current_file_text, applied
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
