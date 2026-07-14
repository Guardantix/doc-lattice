"""Own load-cache file location, loading, and atomic persistence.

This is the only module that touches the cache file. Any read failure yields an empty snapshot
so the load recomputes, while any write failure emits one stderr line and is swallowed.
"""

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .. import __version__
from ..constants import CACHE_FILE_NAME, CACHE_VERSION
from ..error_types import exception_details
from ..persistence import atomic_replace_bytes
from .schema import CacheFile


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    """A loaded cache and its serialized baseline.

    ``cache`` and ``baseline`` are both None when the file is missing, unreadable, invalid, or
    stale. Otherwise, ``baseline`` is the cache's ``model_dump(mode="json")`` representation.
    """

    cache: CacheFile | None
    baseline: dict[str, object] | None


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
        ``<cache_home>/doc-lattice/<cache_key>/load-cache.json``.
    """
    return cache_home(env) / "doc-lattice" / cache_key / CACHE_FILE_NAME


def load(path: Path) -> StoreSnapshot:
    """Load a validated current cache snapshot, treating all failures as empty.

    Missing or unreadable files, invalid UTF-8 or JSON, schema validation failures, and stale
    cache or tool versions all silently return an empty snapshot so the caller can recompute.

    Args:
        path: The load-cache file to read.

    Returns:
        The validated cache and serialized baseline, or an empty snapshot on any read failure.
    """
    empty = StoreSnapshot(cache=None, baseline=None)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return empty
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return empty
    try:
        parsed = CacheFile.model_validate(data)
    except ValidationError:
        return empty
    if parsed.version != CACHE_VERSION or parsed.tool_version != __version__:
        return empty
    return StoreSnapshot(cache=parsed, baseline=parsed.model_dump(mode="json"))


def save_if_changed(path: Path, final: CacheFile, baseline: dict[str, object] | None) -> None:
    """Persist a cache file only when its serialized form changed.

    Args:
        path: The load-cache file to replace.
        final: The final cache state for the completed load.
        baseline: The serialized cache state loaded at the start, or None for an empty load.
    """
    if final.model_dump(mode="json") == baseline:
        return
    _write(path, final)


def _write(path: Path, cache_file: CacheFile) -> None:
    """Atomically replace the cache file, emitting one stderr diagnostic on failure.

    Any OSError is reported on stderr with a single line and swallowed, so a broken cache
    never changes a command's result or exit code.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace_bytes(
            path,
            cache_file.model_dump_json().encode("utf-8"),
            prefix=f"{CACHE_FILE_NAME}.",
        )
    except OSError as exc:
        sys.stderr.write(
            f"doc-lattice: could not write load cache at {path}: {exception_details(exc)}\n"
        )
