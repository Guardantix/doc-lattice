"""Manage pure, filesystem-free run-local entries, root claims, LRU, and eviction."""

from .. import __version__
from ..constants import CACHE_VERSION, MAX_STAT_ROOTS
from .schema import CacheFile, Entry, StatRecord


class RunState:
    """Manage cache entries and per-root claims during one filesystem-free run."""

    def __init__(self, *, entries: dict[str, Entry], roots: list[str], current_root: str) -> None:
        """Initialize mutable state owned by one cache run.

        Args:
            entries: The run-local mapping of relative keys to cached entries.
            roots: The run-local root ledger in least-to-most-recent order.
            current_root: The root whose discoveries and claims this run updates.
        """
        self._entries = entries
        self._roots = roots
        self._current_root = current_root
        self._discovered: set[str] = set()

    @classmethod
    def begin(cls, cache: CacheFile | None, current_root: str) -> "RunState":
        """Begin run-local state from an optional cache snapshot.

        Args:
            cache: The validated cache snapshot, or None when no cache is available.
            current_root: The root whose discoveries and claims this run updates.

        Returns:
            Independent entry-mapping and root-ledger containers for the new run.
        """
        if cache is None:
            return cls(entries={}, roots=[], current_root=current_root)
        return cls(
            entries=dict(cache.entries),
            roots=list(cache.roots),
            current_root=current_root,
        )

    def entry(self, rel_key: str) -> Entry | None:
        """Return the cached entry for a relative key, if present.

        Args:
            rel_key: The root-relative cache key to look up.

        Returns:
            The matching entry, or None when the key is not cached.
        """
        return self._entries.get(rel_key)

    def claim(self, rel_key: str, refreshed_stat: StatRecord | None = None) -> None:
        """Mark an existing entry discovered and optionally refresh its root claim.

        Args:
            rel_key: The root-relative key of the discovered entry.
            refreshed_stat: A replacement stat hint for the current root, if refreshed.
        """
        self._discovered.add(rel_key)
        if refreshed_stat is not None:
            self._entries[rel_key].stats[self._current_root] = refreshed_stat

    def replace(self, rel_key: str, entry: Entry) -> None:
        """Replace an entry and mark its key discovered in this run.

        Args:
            rel_key: The root-relative key of the discovered entry.
            entry: The replacement cache entry.
        """
        self._discovered.add(rel_key)
        self._entries[rel_key] = entry

    def complete(self) -> CacheFile:
        """Finalize claims, LRU eviction, and unclaimed-entry cleanup.

        Returns:
            The current-version cache snapshot produced by this run.
        """
        self._touch_current_root()
        self._withdraw_undiscovered_claims()
        self._evict_over_cap_roots()
        self._drop_unclaimed_entries()
        return CacheFile(
            version=CACHE_VERSION,
            tool_version=__version__,
            roots=self._roots,
            entries=self._entries,
        )

    def _touch_current_root(self) -> None:
        """Move the current root to the most-recent ledger position."""
        if self._current_root in self._roots:
            self._roots.remove(self._current_root)
        self._roots.append(self._current_root)

    def _withdraw_undiscovered_claims(self) -> None:
        """Remove current-root claims from entries not discovered this run."""
        for rel_key, entry in self._entries.items():
            if rel_key not in self._discovered:
                entry.stats.pop(self._current_root, None)

    def _evict_over_cap_roots(self) -> None:
        """Evict excess ledger roots and scrub their claims from every entry."""
        if len(self._roots) <= MAX_STAT_ROOTS:
            return
        evicted = set(self._roots[:-MAX_STAT_ROOTS])
        self._roots = self._roots[-MAX_STAT_ROOTS:]
        for entry in self._entries.values():
            for root in evicted:
                entry.stats.pop(root, None)

    def _drop_unclaimed_entries(self) -> None:
        """Discard entries with no remaining root claims."""
        self._entries = {rel_key: entry for rel_key, entry in self._entries.items() if entry.stats}
