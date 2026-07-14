"""Type-safe constants with runtime validation."""

from typing import Literal, get_args

Status = Literal["active", "inactive"]
VALID_STATUSES: frozenset[str] = frozenset(get_args(Status))

Layer = Literal["design", "technical", "production"]
VALID_LAYERS: frozenset[str] = frozenset(get_args(Layer))

Authority = Literal["binding", "derived", "exploratory"]
VALID_AUTHORITIES: frozenset[str] = frozenset(get_args(Authority))
AUTHORITY_LADDER: tuple[Authority, ...] = ("exploratory", "derived", "binding")

LocationKind = Literal["file", "section"]
VALID_LOCATION_KINDS: frozenset[str] = frozenset(get_args(LocationKind))

EdgeState = Literal["OK", "STALE", "UNRECONCILED", "BROKEN"]
VALID_EDGE_STATES: frozenset[str] = frozenset(get_args(EdgeState))

LinearStateType = Literal[
    "triage", "backlog", "unstarted", "started", "completed", "canceled", "duplicate"
]
VALID_LINEAR_STATE_TYPES: frozenset[str] = frozenset(get_args(LinearStateType))

Severity = Literal["DANGER", "WARNING", "INFO", "BLOCKED"]
VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))

BlockedReason = Literal["malformed", "not-found", "cross-team"]
VALID_BLOCKED_REASONS: frozenset[str] = frozenset(get_args(BlockedReason))

SkipReason = Literal["source-unannotated", "target-unannotated"]
VALID_SKIP_REASONS: frozenset[str] = frozenset(get_args(SkipReason))

GraphFormat = Literal["mermaid", "dot", "json"]
VALID_GRAPH_FORMATS: frozenset[str] = frozenset(get_args(GraphFormat))

ReportFormat = Literal["human", "json", "github"]
VALID_REPORT_FORMATS: frozenset[str] = frozenset(get_args(ReportFormat))

# Control-range boundaries for text sanitization. C0 (below 0x20) and DEL (0x7F) are the
# ASCII controls; C1 (0x80 to 0x9F) are 8-bit controls that still drive terminals (for
# example 0x9B is a single-byte CSI introducer, 0x85 is NEL), so they are stripped too.
ASCII_PRINTABLE_MIN: int = 0x20
ASCII_DELETE: int = 0x7F
C1_CONTROL_MIN: int = 0x80
C1_CONTROL_MAX: int = 0x9F

# Load cache (opt-in incremental cache). CACHE_VERSION bumps on an intentional schema or
# cached-derivation semantics change; a tool-version mismatch already discards the file across
# releases.
# MAX_STAT_ROOTS bounds the per-root stat ledger. CACHE_FILE_NAME is the single JSON document under
# the cache slot.
CACHE_VERSION: int = 3
MAX_STAT_ROOTS: int = 8
CACHE_FILE_NAME: str = "load-cache.json"

# Reconcile transaction schema plus the shared journal and staged-image naming contract.
RECONCILE_JOURNAL_NAME: str = ".doc-lattice-reconcile.json"
RECONCILE_JOURNAL_VERSION: int = 1
PERSISTENCE_TEMP_SUFFIX: str = ".tmp"
RECONCILE_BEFORE_IMAGE_INFIX: str = ".doc-lattice-before."
RECONCILE_AFTER_IMAGE_INFIX: str = ".doc-lattice-after."
