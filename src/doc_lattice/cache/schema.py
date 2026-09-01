"""Cache persistence models plus a pure codec.

This module does not access the filesystem, environment, or stderr.
"""

import hashlib
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..constants import FrontmatterDisposition
from ..model import CollisionMember, FileSections, NodeMeta, ParsedDoc, ParsedMeta, SectionRecord


class StatRecord(BaseModel):
    """One checkout's stat hint for a file: byte size and nanosecond mtime."""

    model_config = ConfigDict(extra="forbid")

    size: int
    mtime_ns: int


class CollisionMemberModel(BaseModel):
    """The serialized form of one member of a slug-collision component.

    ``line`` is a file line, envelope included, matching ``model.CollisionMember``.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    line: int


class SectionRecordModel(BaseModel):
    """The serialized form of one anchored section span, its ancestor context, and collisions.

    ``collision`` and ``context`` are both defaulted rather than required, unlike
    ``Entry.disposition``, because a section that is in no component genuinely has no members to
    record and a top-level section genuinely has no ancestors, and neither empty default can be
    read as a silent drop. Entries written before either field existed are discarded by the
    ``CACHE_VERSION`` bump that lands with it, never reinterpreted.
    """

    model_config = ConfigDict(extra="forbid")

    anchor: str
    start: int
    end: int
    collision: list[CollisionMemberModel] | None = None
    context: list[str] = []


class NodePayload(BaseModel):
    """The cached derivation of a lattice node: validated meta, body, and section spans."""

    model_config = ConfigDict(extra="forbid")

    meta: NodeMeta
    body: str
    total_lines: int
    sections: list[SectionRecordModel]


class Entry(BaseModel):
    """One cached file: its content hash, per-root stat hints, node payload, and diagnostics.

    ``disposition`` is required rather than defaulted. A default would let an entry written
    before the field existed decode as an ordinary skip, which is exactly the silent drop this
    field exists to end; ``CACHE_VERSION`` is bumped alongside it so those entries are discarded
    instead of reinterpreted. It records why a file has no ``node`` so a warm run can replay the
    diagnostic a cold run emitted, and it stores the kind rather than rendered warning text
    because a cache slot is shared across checkouts and the message names the current path.

    ``reused_anchors`` is required for the same reason and records the same kind of fact: the
    parse noticed a frontmatter block defining one anchor name twice, and a warm run has to say
    so too or the diagnostic would exist only on the run that first read the file.
    """

    model_config = ConfigDict(extra="forbid")

    file_sha256: str
    stats: dict[str, StatRecord]
    node: NodePayload | None
    disposition: FrontmatterDisposition
    reused_anchors: bool


class CacheFile(BaseModel):
    """The whole versioned cache document."""

    model_config = ConfigDict(extra="forbid")

    version: int
    tool_version: str
    roots: list[str]
    entries: dict[str, Entry]


def stat_record(st: os.stat_result) -> StatRecord:
    """Build a cache stat hint from an already captured file stat.

    Args:
        st: The stat captured alongside the corresponding file bytes.

    Returns:
        The byte size and nanosecond mtime used by the stat tier.
    """
    return StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)


def reconstruct_doc(entry: Entry, path: Path) -> ParsedDoc | None:
    """Rebuild a parsed document from a cached entry.

    Args:
        entry: The cached file entry to decode.
        path: The discovered path to attach to the reconstructed document.

    Returns:
        The reconstructed ParsedDoc, or None for a cached non-node file.
    """
    node = entry.node
    if node is None:
        return None
    sections = FileSections(
        total_lines=node.total_lines,
        sections=tuple(
            SectionRecord(
                anchor=r.anchor,
                start=r.start,
                end=r.end,
                collision=(
                    None
                    if r.collision is None
                    else tuple(CollisionMember(label=m.label, line=m.line) for m in r.collision)
                ),
                context=tuple(r.context),
            )
            for r in node.sections
        ),
    )
    return ParsedDoc(path=path, meta=node.meta, body=node.body, sections=sections)


def make_entry(  # noqa: PLR0913
    data: bytes,
    parsed: ParsedMeta,
    body: str,
    sections: FileSections | None,
    st: os.stat_result,
    current_root: str,
) -> Entry:
    """Replace an entry from a fresh parse with a new hash and current-root stat.

    Args:
        data: The raw file bytes hashed for ``file_sha256``.
        parsed: The fresh parse outcome, whose disposition and diagnostics are recorded whether
            or not it produced a node.
        body: The verbatim body (unused when ``parsed`` carries no node).
        sections: The pre-derived sections (present when ``parsed`` carries a node).
        st: The stat captured alongside ``data``, stored as the fresh stat hint.
        current_root: The current project's resolved root used as the sole stat key.

    Returns:
        A replacement cache entry whose stats are reset to the current root.
    """
    meta = parsed.meta
    node: NodePayload | None = None
    if meta is not None and sections is not None:
        node = NodePayload(
            meta=meta,
            body=body,
            total_lines=sections.total_lines,
            sections=[
                SectionRecordModel(
                    anchor=r.anchor,
                    start=r.start,
                    end=r.end,
                    collision=(
                        None
                        if r.collision is None
                        else [CollisionMemberModel(label=m.label, line=m.line) for m in r.collision]
                    ),
                    context=list(r.context),
                )
                for r in sections.sections
            ],
        )
    return Entry(
        file_sha256=hashlib.sha256(data).hexdigest(),
        stats={current_root: stat_record(st)},
        node=node,
        disposition=parsed.disposition,
        reused_anchors=parsed.reused_anchors,
    )
