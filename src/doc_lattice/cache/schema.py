"""Cache persistence models plus a pure codec.

This module does not access the filesystem, environment, or stderr.
"""

import hashlib
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..model import FileSections, NodeMeta, ParsedDoc, SectionRecord


class StatRecord(BaseModel):
    """One checkout's stat hint for a file: byte size and nanosecond mtime."""

    model_config = ConfigDict(extra="forbid")

    size: int
    mtime_ns: int


class SectionRecordModel(BaseModel):
    """The serialized form of one anchored section span."""

    model_config = ConfigDict(extra="forbid")

    anchor: str
    start: int
    end: int


class NodePayload(BaseModel):
    """The cached derivation of a lattice node: validated meta, body, and section spans."""

    model_config = ConfigDict(extra="forbid")

    meta: NodeMeta
    body: str
    total_lines: int
    sections: list[SectionRecordModel]


class Entry(BaseModel):
    """One cached file: its content hash, per-root stat hints, and node payload (or null)."""

    model_config = ConfigDict(extra="forbid")

    file_sha256: str
    stats: dict[str, StatRecord]
    node: NodePayload | None


class CacheFile(BaseModel):
    """The whole cache document, version 1."""

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
        sections=tuple(SectionRecord(r.anchor, r.start, r.end) for r in node.sections),
    )
    return ParsedDoc(path=path, meta=node.meta, body=node.body, sections=sections)


def make_entry(  # noqa: PLR0913
    data: bytes,
    meta: NodeMeta | None,
    body: str,
    sections: FileSections | None,
    st: os.stat_result,
    current_root: str,
) -> Entry:
    """Replace an entry from a fresh parse with a new hash and current-root stat.

    Args:
        data: The raw file bytes hashed for ``file_sha256``.
        meta: The validated NodeMeta, or None for a discovered non-node file.
        body: The verbatim body (unused when ``meta`` is None).
        sections: The pre-derived sections (present when ``meta`` is not None).
        st: The stat captured alongside ``data``, stored as the fresh stat hint.
        current_root: The current project's resolved root used as the sole stat key.

    Returns:
        A replacement cache entry whose stats are reset to the current root.
    """
    has_node_payload = meta is not None and sections is not None
    node: NodePayload | None = None
    if has_node_payload:
        node = NodePayload(
            meta=meta,
            body=body,
            total_lines=sections.total_lines,
            sections=[
                SectionRecordModel(anchor=r.anchor, start=r.start, end=r.end)
                for r in sections.sections
            ],
        )
    return Entry(
        file_sha256=hashlib.sha256(data).hexdigest(),
        stats={current_root: stat_record(st)},
        node=node,
    )
