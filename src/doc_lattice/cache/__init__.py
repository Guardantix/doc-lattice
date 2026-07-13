"""The opt-in incremental load cache state and tier selection."""

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from .. import __version__
from ..constants import CACHE_VERSION, MAX_STAT_ROOTS
from ..discovery import read_doc_bytes_and_stat
from ..model import FileSections, NodeMeta
from .lookup import CacheHit, CacheMiss
from .schema import (  # noqa: F401 (models re-exported for doc_lattice.cache importers)
    CacheFile,
    Entry,
    NodePayload,
    SectionRecordModel,
    StatRecord,
    make_entry,
    reconstruct_doc,
    stat_record,
)
from .store import (  # noqa: F401 (cache_home re-exported for doc_lattice.cache importers)
    cache_home,
    cache_path,
    load,
    save_if_changed,
)


class LoadCache:
    """Mutable in-memory cache state for one load run, backed by a single JSON file.

    Constructed by ``open``, mutated per discovered file by ``lookup`` and ``record_miss``,
    and persisted by ``finalize``. Never raises on its own behalf.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        path: Path,
        current_root: str,
        trust_stat: bool,
        require_verified: bool,
        entries: dict[str, Entry],
        roots: list[str],
        original: dict[str, object] | None,
    ) -> None:
        self._path = path
        self._current_root = current_root
        self._trust_stat = trust_stat and not require_verified
        self._entries = entries
        self._roots = roots
        self._original = original

    @property
    def is_empty(self) -> bool:
        """True when no valid cache file was loaded and no entries are held."""
        return not self._entries and self._original is None

    @classmethod
    def open(
        cls,
        *,
        cache_key: str,
        project_root: Path,
        env: Mapping[str, str],
        trust_stat: bool,
        require_verified: bool,
    ) -> "LoadCache":
        """Read and validate the cache file, returning an empty cache on any failure.

        The current project root is recorded as this run's stat key (its realpath). A missing,
        unreadable, invalid, wrong-version, or wrong-tool-version file is treated as empty:
        everything recomputes and the file is rewritten by ``finalize``.

        Args:
            cache_key: The validated cache slot name.
            project_root: The project root, used as this run's per-root stat key.
            env: The environment mapping for cache-path resolution.
            trust_stat: Whether the stat fast tier is enabled by config.
            require_verified: Whether this call forces the verify tier (the reconcile path).

        Returns:
            A LoadCache holding the loaded (or empty) state.
        """
        path = cache_path(cache_key, env)
        current_root = str(project_root.resolve())
        snapshot = load(path)
        if snapshot.cache is None:
            entries: dict[str, Entry] = {}
            roots: list[str] = []
            original: dict[str, object] | None = None
        else:
            entries = dict(snapshot.cache.entries)
            roots = list(snapshot.cache.roots)
            original = snapshot.baseline
        return cls(
            path=path,
            current_root=current_root,
            trust_stat=trust_stat,
            require_verified=require_verified,
            entries=entries,
            roots=roots,
            original=original,
        )

    def lookup(self, rel_key: str, path: Path) -> CacheHit | CacheMiss:
        """Resolve one discovered file through the stat and verify tiers.

        Args:
            rel_key: The file's POSIX path relative to the project root (the entry key).
            path: The absolute path to the file on disk.

        Returns:
            A CacheHit reusing the cached derivation, or a CacheMiss carrying the freshly
            read bytes when the entry is absent, drifted, or unverifiable.

        Raises:
            UnreadableDocError: If the file cannot be read or stat-ed, with the same message
                an uncached read would produce.
        """
        entry = self._entries.get(rel_key)
        if entry is not None and self._trust_stat:
            hit = self._stat_tier(entry, path)
            if hit is not None:
                return hit
        data, st = read_doc_bytes_and_stat(path)
        if entry is not None and entry.file_sha256 == hashlib.sha256(data).hexdigest():
            self._refresh_stat(entry, st)
            return CacheHit(doc=reconstruct_doc(entry, path))
        return CacheMiss(data=data, stat=st)

    def _stat_tier(self, entry: Entry, path: Path) -> CacheHit | None:
        """Return a CacheHit if the current root's stat hint matches, else None."""
        record = entry.stats.get(self._current_root)
        if record is None:
            return None
        try:
            st = path.stat()
        except OSError as exc:
            from ..discovery import _unreadable  # noqa: PLC0415

            raise _unreadable(path, exc) from exc
        if record.size != st.st_size or record.mtime_ns != st.st_mtime_ns:
            return None
        return CacheHit(doc=reconstruct_doc(entry, path))

    def _refresh_stat(self, entry: Entry, st: os.stat_result) -> None:
        """Insert or refresh the current root's stat hint on a verify-tier hit.

        ``st`` is the stat already captured alongside the bytes that produced the verify hit
        (see ``read_doc_bytes_and_stat``), so no separate stat call is needed or made here.
        """
        entry.stats[self._current_root] = stat_record(st)

    def record_miss(  # noqa: PLR0913
        self,
        rel_key: str,
        data: bytes,
        meta: NodeMeta | None,
        body: str,
        sections: FileSections | None,
        st: os.stat_result,
    ) -> None:
        """Replace an entry from a fresh parse: new hash, stats reset to the current root.

        Args:
            rel_key: The entry key (POSIX path relative to the project root).
            data: The raw file bytes hashed for ``file_sha256``.
            meta: The validated NodeMeta, or None for a discovered non-node file.
            body: The verbatim body (unused when ``meta`` is None).
            sections: The pre-derived sections (present when ``meta`` is not None).
            st: The stat captured alongside ``data`` (see ``read_doc_bytes_and_stat``), stored
                as the fresh stat hint for the current root.
        """
        self._entries[rel_key] = make_entry(data, meta, body, sections, st, self._current_root)

    def finalize(self, discovered: set[str]) -> None:
        """Reclaim, bound, and persist the cache after a successful load.

        Moves the current root to the ledger tail, withdraws its claim on files it did not
        discover this run, evicts over-cap head roots and scrubs their stats, drops entries no
        live root claims, and writes atomically only if the serialized cache changed.

        Args:
            discovered: The set of entry keys (POSIX paths relative to the project root) seen
                this run.
        """
        self._touch_current_root()
        self._withdraw_undiscovered_claims(discovered)
        self._evict_over_cap_roots()
        self._drop_unclaimed_entries()
        final = CacheFile(
            version=CACHE_VERSION,
            tool_version=__version__,
            roots=self._roots,
            entries=self._entries,
        )
        save_if_changed(self._path, final, self._original)

    def _touch_current_root(self) -> None:
        """Move the current root to the ledger tail (most recently used)."""
        if self._current_root in self._roots:
            self._roots.remove(self._current_root)
        self._roots.append(self._current_root)

    def _withdraw_undiscovered_claims(self, discovered: set[str]) -> None:
        """Remove the current root's stat key from every entry it did not discover this run."""
        for rel_key, entry in self._entries.items():
            if rel_key not in discovered:
                entry.stats.pop(self._current_root, None)

    def _evict_over_cap_roots(self) -> None:
        """Evict head roots beyond MAX_STAT_ROOTS and scrub their keys from every entry."""
        if len(self._roots) <= MAX_STAT_ROOTS:
            return
        evicted = set(self._roots[:-MAX_STAT_ROOTS])
        self._roots = self._roots[-MAX_STAT_ROOTS:]
        for entry in self._entries.values():
            for root in evicted:
                entry.stats.pop(root, None)

    def _drop_unclaimed_entries(self) -> None:
        """Drop any entry whose stats map is empty (no live root claims it)."""
        self._entries = {key: entry for key, entry in self._entries.items() if entry.stats}
