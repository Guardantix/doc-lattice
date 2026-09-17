"""Cache persistence models plus a pure codec.

This module does not access the filesystem, environment, or stderr.
"""

import hashlib
import os

from pydantic import BaseModel, ConfigDict, Field

from ..constants import FrontmatterDisposition
from ..model import CollisionMember, FileFacts, FileSections, NodeMeta, ParsedMeta, SectionRecord


class StatRecord(BaseModel):
    """One checkout's stat hint for a file: its identity, byte size, and nanosecond mtime.

    ``device`` and ``inode`` bind the hint to the file that was read, so a cache key whose path
    now reaches a different file, such as a retargeted symlink, cannot match on size and mtime
    alone.
    """

    model_config = ConfigDict(extra="forbid")

    device: int
    inode: int
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


class FilePayload(BaseModel):
    """Required file-local content and provenance, whether or not inline metadata exists.

    Nullable metadata is still required: no missing field may silently turn an incomplete
    entry into a file with no inline metadata. Empty bodies likewise retain their provenance
    and section derivation. Nothing here depends on registration or a manifest.
    """

    model_config = ConfigDict(extra="forbid")

    meta: NodeMeta | None
    body: str
    body_first_line: int = Field(ge=1)
    total_lines: int
    sections: list[SectionRecordModel]


class Entry(BaseModel):
    """One cached file: its raw-byte hash, per-root stat hints, file facts, and diagnostics.

    ``disposition`` is required rather than defaulted. A default would let an entry written
    before the field existed decode as an ordinary skip, which is exactly the silent drop this
    field exists to end; ``CACHE_VERSION`` is bumped alongside it so those entries are discarded
    instead of reinterpreted. It records the inline classification so a warm run can replay the
    diagnostic a cold run emitted, and it stores the kind rather than rendered warning text
    because a cache slot is shared across checkouts and the message names the current path.

    ``reused_anchors`` is required for the same reason and records the same kind of fact: the
    parse noticed a frontmatter block defining one anchor name twice, and a warm run has to say
    so too or the diagnostic would exist only on the run that first read the file.

    ``shadowed_envelope`` is the third such field, required on the same grounds: the parse
    noticed that a file tracked under its ``---`` fence also carries a comment envelope in its
    body. It cannot be folded into ``disposition``, which is ``"tracked"`` for exactly these
    files, so it travels beside it.
    """

    model_config = ConfigDict(extra="forbid")

    file_sha256: str
    stats: dict[str, StatRecord]
    payload: FilePayload
    disposition: FrontmatterDisposition
    reused_anchors: bool
    shadowed_envelope: bool


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
        The file identity, byte size, and nanosecond mtime used by the stat tier.
    """
    return StatRecord(device=st.st_dev, inode=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns)


def reconstruct_facts(entry: Entry) -> FileFacts:
    """Rebuild complete file facts without parsing or deriving sections again.

    Args:
        entry: The cached file entry to decode.

    Returns:
        The same path-independent facts a fresh parse produces, including for non-nodes.
    """
    payload = entry.payload
    sections = FileSections(
        total_lines=payload.total_lines,
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
            for r in payload.sections
        ),
    )
    return FileFacts(
        parsed=ParsedMeta(
            meta=payload.meta,
            disposition=entry.disposition,
            reused_anchors=entry.reused_anchors,
            shadowed_envelope=entry.shadowed_envelope,
        ),
        body=payload.body,
        body_first_line=payload.body_first_line,
        sections=sections,
    )


def make_entry(
    data: bytes,
    facts: FileFacts,
    st: os.stat_result,
    current_root: str,
) -> Entry:
    """Replace an entry from a fresh parse with a new hash and current-root stat.

    Args:
        data: The raw file bytes hashed for ``file_sha256``.
        facts: The complete fresh file facts, whether or not inline metadata exists.
        st: The stat captured alongside ``data``, stored as the fresh stat hint.
        current_root: The current project's resolved root used as the sole stat key.

    Returns:
        A replacement cache entry whose stats are reset to the current root.
    """
    payload = FilePayload(
        meta=facts.parsed.meta,
        body=facts.body,
        body_first_line=facts.body_first_line,
        total_lines=facts.sections.total_lines,
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
            for r in facts.sections.sections
        ],
    )
    return Entry(
        file_sha256=hashlib.sha256(data).hexdigest(),
        stats={current_root: stat_record(st)},
        payload=payload,
        disposition=facts.parsed.disposition,
        reused_anchors=facts.parsed.reused_anchors,
        shadowed_envelope=facts.parsed.shadowed_envelope,
    )
