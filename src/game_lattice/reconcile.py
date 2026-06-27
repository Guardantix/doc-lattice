"""Reconcile edges: recompute upstream hashes and rewrite seen scalars in place."""

import io
from collections import defaultdict
from pathlib import Path

from ruamel.yaml import YAML

from .error_types import BrokenRefError
from .frontmatter_parser import split_frontmatter
from .hashing import content_hash
from .model import Lattice
from .resolve import target_content


def reconcile(
    lattice: Lattice, downstream_id: str, *, ref: str | None, reconcile_all: bool
) -> dict[Path, dict[str, str]]:
    """Plan the seen-scalar updates needed to clear drift for the selection.

    Args:
        lattice: The built lattice (its upstream content is the reconcile snapshot).
        downstream_id: The node whose edges to reconcile (ignored if ``reconcile_all``).
        ref: A single upstream ref to narrow to, or None for all of the node's edges.
        reconcile_all: Reconcile every node's STALE or UNRECONCILED edges.

    Returns:
        A mapping of downstream file path to ``{target_ref: new_seen}`` updates. The
        caller applies these via ``apply_reconcile`` and an atomic write (the CLI does).

    Raises:
        BrokenRefError: If a selected edge has no resolvable target.
    """
    node_ids = sorted(lattice.nodes_by_id) if reconcile_all else [downstream_id]
    plan: dict[Path, dict[str, str]] = defaultdict(dict)
    for node_id in node_ids:
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            if ref is not None and edge.target_ref != ref:
                continue
            if edge.target_id is None:
                msg = (
                    f"cannot reconcile broken ref {edge.target_ref!r} on {node_id};"
                    " fix the ref first"
                )
                raise BrokenRefError(msg)
            new_seen = content_hash(target_content(lattice, edge.target_id))
            plan[node.path][edge.target_ref] = new_seen
    return dict(plan)


def apply_reconcile(current_file_text: str, updates: dict[str, str]) -> str:
    """Return ``current_file_text`` with matching edges' seen scalars set.

    Args:
        current_file_text: A fresh read of the downstream file at write time.
        updates: ``{target_ref: new_seen}`` for edges in this file.

    Returns:
        The file text with only the matching ``seen`` scalars changed; the body after
        the closing fence is reattached verbatim from ``current_file_text``.
    """
    raw_meta, body = split_frontmatter(current_file_text)
    if raw_meta is None:
        return current_file_text
    yaml = YAML(typ="rt")
    data = yaml.load(raw_meta)
    for entry in data.get("derives_from", []):
        if entry.get("ref") in updates:
            entry["seen"] = updates[entry["ref"]]
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    return f"---\n{buffer.getvalue()}---\n{body}"
