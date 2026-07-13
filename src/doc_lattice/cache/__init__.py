"""The opt-in incremental load cache, split by lifecycle phase.

``schema`` holds the persistence models and codec, ``store`` owns the cache file on disk,
``lookup`` resolves one discovered doc to a hit or a miss without mutating anything, and
``state`` holds the run-local document between load and save. This module re-exports the
public surface; ``orchestrate._load_cached`` is the only production wiring point and owns the
transaction boundary (persist only after ``build_lattice`` succeeds).
"""

from .lookup import CacheHit, CacheMiss, LookupPolicy
from .schema import CacheFile, Entry, NodePayload, SectionRecordModel, StatRecord, make_entry
from .state import RunState
from .store import StoreSnapshot, cache_home, cache_path

__all__ = [
    "CacheFile",
    "CacheHit",
    "CacheMiss",
    "Entry",
    "LookupPolicy",
    "NodePayload",
    "RunState",
    "SectionRecordModel",
    "StatRecord",
    "StoreSnapshot",
    "cache_home",
    "cache_path",
    "make_entry",
]
