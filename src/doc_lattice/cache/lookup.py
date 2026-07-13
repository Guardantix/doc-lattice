"""Select a per-file cache tier without mutating cache state.

This module returns hit or miss facts. It reads and stats documents, but never changes cache
state.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from ..discovery import _unreadable, read_doc_bytes_and_stat
from ..model import ParsedDoc
from .schema import Entry, StatRecord, reconstruct_doc, stat_record


@dataclass(frozen=True, slots=True)
class LookupPolicy:
    """The already-effective policy for resolving one file.

    ``trust_stat`` already reflects both configuration and any requirement for verified
    content. The resolver applies it directly without further gating.
    """

    current_root: str
    trust_stat: bool


@dataclass(frozen=True, slots=True)
class CacheHit:
    """A tier hit and any stat fact learned while verifying content.

    ``doc`` is the reconstructed ParsedDoc, or None for a cached non-node. A verify-tier hit
    carries ``refreshed_stat`` from the same file handle as the verified bytes; a stat-tier hit
    leaves it as None.
    """

    doc: ParsedDoc | None
    refreshed_stat: StatRecord | None = None


@dataclass(frozen=True, slots=True)
class CacheMiss:
    """A miss carrying bytes and stat captured from the same open file handle."""

    data: bytes
    stat: os.stat_result


def resolve(entry: Entry | None, path: Path, policy: LookupPolicy) -> CacheHit | CacheMiss:
    """Resolve one discovered file through the stat and verify tiers.

    Args:
        entry: The cached entry for the file, or None when it has not been cached.
        path: The discovered file path on disk.
        policy: The already-effective tier-selection policy for this lookup.

    Returns:
        A CacheHit reusing the cached derivation, or a CacheMiss carrying freshly read bytes
        and their same-handle stat.

    Raises:
        UnreadableDocError: If the file cannot be read or stat-ed.
    """
    if entry is not None and policy.trust_stat:
        hit = _stat_tier(entry, path, policy.current_root)
        if hit is not None:
            return hit
    data, st = read_doc_bytes_and_stat(path)
    if entry is not None and entry.file_sha256 == hashlib.sha256(data).hexdigest():
        return CacheHit(doc=reconstruct_doc(entry, path), refreshed_stat=stat_record(st))
    return CacheMiss(data=data, stat=st)


def _stat_tier(entry: Entry, path: Path, current_root: str) -> CacheHit | None:
    """Return a hit when the current root's stored stat matches the path."""
    record = entry.stats.get(current_root)
    if record is None:
        return None
    try:
        st = path.stat()
    except OSError as exc:
        raise _unreadable(path, exc) from exc
    if record.size != st.st_size or record.mtime_ns != st.st_mtime_ns:
        return None
    return CacheHit(doc=reconstruct_doc(entry, path))
