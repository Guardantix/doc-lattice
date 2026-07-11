"""The opt-in incremental load cache: read, tier selection, and atomic write.

The only module that touches the cache file. It resolves the cache path from an environment
mapping, validates the versioned single-file JSON schema as pydantic models, serves stat-tier
and verify-tier hits, and writes atomically after a successful load. It never raises on its own
behalf: a read failure yields an empty cache and a write failure emits one stderr diagnostic.
"""

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from . import __version__
from .constants import CACHE_FILE_NAME, CACHE_VERSION, MAX_STAT_ROOTS
from .discovery import read_doc_bytes_and_stat
from .model import FileSections, NodeMeta, ParsedDoc, SectionRecord


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


def cache_home(env: Mapping[str, str]) -> Path:
    """Return the base cache directory per the XDG base directory spec.

    Args:
        env: The environment mapping to read ``XDG_CACHE_HOME`` and ``HOME`` from.

    Returns:
        ``$XDG_CACHE_HOME`` when it is set to an absolute path (a relative value is ignored
        per the spec), otherwise ``$HOME/.cache``, falling back to the user home directory.
    """
    xdg = env.get("XDG_CACHE_HOME", "")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg)
    home = env.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".cache"


def cache_path(cache_key: str, env: Mapping[str, str]) -> Path:
    """Return the cache file path for a slot.

    Args:
        cache_key: The validated single-segment cache slot name.
        env: The environment mapping used to resolve the cache home.

    Returns:
        ``<cache_home>/game-lattice/<cache_key>/load-cache.json``.
    """
    return cache_home(env) / "game-lattice" / cache_key / CACHE_FILE_NAME


@dataclass(frozen=True, slots=True)
class CacheHit:
    """A tier hit. ``doc`` is the reconstructed ParsedDoc, or None for a cached non-node."""

    doc: ParsedDoc | None


@dataclass(frozen=True, slots=True)
class CacheMiss:
    """A miss. ``data`` is the raw bytes already read, for the caller to decode and parse.

    ``stat`` is the ``os.stat_result`` captured from the same open handle as ``data``, so a
    caller that later records the miss threads a stat hint that corresponds exactly to the
    hashed bytes rather than one taken from a separate, possibly racy, stat call.
    """

    data: bytes
    stat: os.stat_result


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
        loaded = cls._read(path)
        if loaded is None:
            entries: dict[str, Entry] = {}
            roots: list[str] = []
            original: dict[str, object] | None = None
        else:
            entries = dict(loaded.entries)
            roots = list(loaded.roots)
            original = loaded.model_dump(mode="json")
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
            return CacheHit(doc=self._reconstruct(entry, path))
        return CacheMiss(data=data, stat=st)

    def _stat_tier(self, entry: Entry, path: Path) -> CacheHit | None:
        """Return a CacheHit if the current root's stat hint matches, else None."""
        record = entry.stats.get(self._current_root)
        if record is None:
            return None
        try:
            st = path.stat()
        except OSError as exc:
            from .discovery import _unreadable  # noqa: PLC0415

            raise _unreadable(path, exc) from exc
        if record.size != st.st_size or record.mtime_ns != st.st_mtime_ns:
            return None
        return CacheHit(doc=self._reconstruct(entry, path))

    def _refresh_stat(self, entry: Entry, st: os.stat_result) -> None:
        """Insert or refresh the current root's stat hint on a verify-tier hit.

        ``st`` is the stat already captured alongside the bytes that produced the verify hit
        (see ``read_doc_bytes_and_stat``), so no separate stat call is needed or made here.
        """
        entry.stats[self._current_root] = StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)

    @staticmethod
    def _reconstruct(entry: Entry, path: Path) -> ParsedDoc | None:
        """Rebuild a ParsedDoc from a cached node payload, or None for a cached non-node."""
        node = entry.node
        if node is None:
            return None
        sections = FileSections(
            total_lines=node.total_lines,
            sections=tuple(SectionRecord(r.anchor, r.start, r.end) for r in node.sections),
        )
        return ParsedDoc(path=path, meta=node.meta, body=node.body, sections=sections)

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
        node: NodePayload | None = None
        if meta is not None and sections is not None:
            node = NodePayload(
                meta=meta,
                body=body,
                total_lines=sections.total_lines,
                sections=[
                    SectionRecordModel(anchor=r.anchor, start=r.start, end=r.end)
                    for r in sections.sections
                ],
            )
        stats: dict[str, StatRecord] = {
            self._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)
        }
        self._entries[rel_key] = Entry(
            file_sha256=hashlib.sha256(data).hexdigest(), stats=stats, node=node
        )

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
        if final.model_dump(mode="json") == self._original:
            return
        self._write(final)

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

    def _write(self, cache_file: CacheFile) -> None:
        """Atomically replace the cache file, emitting one stderr diagnostic on failure.

        Writes through a temp file in the same directory, fsyncs, then ``os.replace``. Any
        OSError (unwritable directory, failed write or replace) is reported on stderr with a
        single line and swallowed, so a broken cache never changes a command's result or exit
        code. The temp file is always removed.
        """
        text = cache_file.model_dump_json()
        tmp: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=CACHE_FILE_NAME, suffix=".tmp"
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)  # noqa: PTH105 (tests monkeypatch os.replace directly)
            tmp = None
        except OSError as exc:
            sys.stderr.write(f"game-lattice: could not write load cache at {self._path}: {exc}\n")
        finally:
            if tmp is not None:
                # The cleanup unlink must never escape: a write failure already emitted its one
                # diagnostic, and an OSError here (missing_ok only swallows FileNotFoundError)
                # would propagate past the handler and change the command's exit code, which the
                # cache contract forbids. A leaked temp is reclaimed on the next successful write.
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)

    @staticmethod
    def _read(path: Path) -> CacheFile | None:
        """Return the validated cache file, or None if it is missing, invalid, or stale."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        try:
            parsed = CacheFile.model_validate(data)
        except ValidationError:
            return None
        if parsed.version != CACHE_VERSION or parsed.tool_version != __version__:
            return None
        return parsed
