"""Reconcile edges: recompute upstream hashes and rewrite seen scalars in place."""

import io
from collections import defaultdict
from collections.abc import Callable, MutableMapping
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .error_types import BrokenRefError, UnreadableDocError, ValidationError
from .frontmatter_parser import split_frontmatter
from .model import Lattice, TargetId, parse_ref
from .resolve import cached_target_hash


def reconcile(
    lattice: Lattice, downstream_id: str, *, ref: str | None, reconcile_all: bool
) -> dict[Path, dict[str, str]]:
    """Plan the seen-scalar updates needed to clear drift for the selection.

    Selection: when ``reconcile_all`` is True, every STALE and UNRECONCILED edge across
    the lattice is updated; BROKEN and already-OK edges are skipped.  When targeting a
    specific node, all of that node's STALE or UNRECONCILED edges are updated (or just
    the matching edge if ``ref`` is given); an already-OK edge is skipped in both modes,
    since restamping it to the same hash is a no-op.  The match uses the parsed TargetId
    so an identical ref selects the same edge.  A node's BROKEN edge is
    skipped (it does not block the node's reconcilable edges); only a ``--ref`` aimed
    directly at a broken edge is refused, and a ``--ref`` that matches no edge on the node
    is reported rather than silently doing nothing.

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
    targeting_specific_ref = ref is not None and not reconcile_all
    plan: dict[Path, dict[str, str]] = defaultdict(dict)
    cache: dict[TargetId, str] = {}
    ref_matched = False
    for node_id in node_ids:
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            if ref is not None and parse_ref(edge.target_ref) != parse_ref(ref):
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


def apply_reconcile(current_file_text: str, updates: dict[str, str]) -> tuple[str, set[str]]:
    """Return ``current_file_text`` with matching edges' seen scalars set.

    The fresh read is parsed defensively: a concurrent edit that leaves the frontmatter
    unparseable or in an unexpected shape (not a mapping, a non-list ``derives_from``, a
    non-mapping entry) raises ``UnreadableDocError`` (a ``ProjectError``) so the CLI exits
    cleanly instead of crashing with a traceback.

    Args:
        current_file_text: A fresh read of the downstream file at write time.
        updates: ``{target_ref: new_seen}`` for edges in this file.

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
    raw_meta, body = split_frontmatter(current_file_text)
    if raw_meta is None:
        return current_file_text, set()
    yaml = YAML(typ="rt")
    try:
        data = yaml.load(raw_meta)
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
    applied: set[str] = set()
    for entry in entries:
        if not isinstance(entry, MutableMapping):
            raise UnreadableDocError("frontmatter derives_from entry is not a mapping")
        ref = entry.get("ref")
        if ref in updates:
            new_seen = updates[ref]
            if entry.get("seen") != new_seen:
                entry["seen"] = new_seen
                applied.add(ref)
    if not applied:
        return current_file_text, applied
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    # ruamel always ends its dump with a newline, so the closing ``---`` lands on its own
    # line. body has no leading newline (the split joined the post-fence lines), so it is
    # reattached directly after the fence.
    return f"---\n{buffer.getvalue()}---\n{body}", applied


def plan_rewrites(
    plan: dict[Path, dict[str, str]],
    read_text: Callable[[Path], str],
) -> list[tuple[Path, str, set[str]]]:
    """Compute fresh-read reconcile rewrites before any write lands.

    The CLI passes ``lambda p: p.read_text(encoding="utf-8")`` so this pure
    helper can validate every target file and build every rewrite before the
    CLI starts its atomic write phase.

    Args:
        plan: The planned mapping of downstream file path to ``{ref: new_seen}``.
        read_text: Reader injected by the caller for fresh downstream file text.

    Returns:
        Rewrite entries ``(path, new_text, applied_refs)`` for files whose fresh
        content changed. Files whose planned updates are already applied are skipped.

    Raises:
        UnreadableDocError: If the injected reader cannot read a downstream file, or
            if the fresh frontmatter cannot be parsed or is malformed.
    """
    rewrites: list[tuple[Path, str, set[str]]] = []
    for path, updates in plan.items():
        try:
            fresh = read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read {path} to reconcile: {exc}"
            raise UnreadableDocError(msg) from exc
        new_text, applied = apply_reconcile(fresh, updates)
        if applied:
            rewrites.append((path, new_text, applied))
    return rewrites
