"""Type-safe constants with runtime validation."""

from typing import Literal, get_args

Layer = Literal["design", "technical", "production"]
VALID_LAYERS: frozenset[str] = frozenset(get_args(Layer))

Authority = Literal["binding", "derived", "exploratory"]
VALID_AUTHORITIES: frozenset[str] = frozenset(get_args(Authority))
AUTHORITY_LADDER: tuple[Authority, ...] = ("exploratory", "derived", "binding")

# What one discovered file's frontmatter turned out to be. "untracked" and "id-less" are the two
# distinct ways a file is left out of the lattice, which a bare "no node" answer conflated: the
# first is prose the engine has nothing to say about, the second is a metadata block that lost or
# never had its `id`. The load cache persists this so a warm run replays the diagnostic a cold run
# emitted.
FrontmatterDisposition = Literal["tracked", "untracked", "id-less"]
VALID_FRONTMATTER_DISPOSITIONS: frozenset[str] = frozenset(get_args(FrontmatterDisposition))

# The exact frontmatter keys that declare lattice intent. An id-less block carrying any of them is
# a typo'd node rather than incidental metadata, so it is a tool error instead of a warning. This
# is an intent set, not "every NodeMeta field except id": `title` and `layer` describe a document
# without wiring it into the graph, so an id-less block carrying only those stays in the warning
# tier. Membership is tested by key presence, never by value truth, because `derives_from: []`,
# `tickets: []`, and `authority: null` all still declare intent.
LATTICE_INTENT_KEYS: frozenset[str] = frozenset({"authority", "derives_from", "tickets"})

LocationKind = Literal["file", "section"]
VALID_LOCATION_KINDS: frozenset[str] = frozenset(get_args(LocationKind))

EdgeState = Literal["OK", "STALE", "UNRECONCILED", "BROKEN"]
# EDGE_STATES is the ordered form, in Literal declaration order. Report output that must be
# deterministic (the check summary breakdown) iterates it; VALID_EDGE_STATES stays the
# membership test and must never drive output order, being a frozenset.
EDGE_STATES: tuple[EdgeState, ...] = get_args(EdgeState)
VALID_EDGE_STATES: frozenset[str] = frozenset(EDGE_STATES)

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

BasicOutputFormat = Literal["human", "json"]
VALID_BASIC_OUTPUT_FORMATS: frozenset[str] = frozenset(get_args(BasicOutputFormat))

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
CACHE_VERSION: int = 4
MAX_STAT_ROOTS: int = 8
CACHE_FILE_NAME: str = "load-cache.json"

# GitHub Actions commit pins, shared by both workflow renderers: the ordinary snippet that
# `init` prints (scaffold.py) and the managed templates (github_ci/render.py). Each action
# owns two halves that must move together, the immutable commit SHA and the release tag it
# corresponds to, because the tag is rendered as a trailing `# vX.Y.Z` comment beside every
# pinned `uses:` line. Both halves live here so both renderers read one owner, but a bump is
# not a single edit: this repository's own `.github/workflows/*.yml`, the golden fixtures under
# `tests/fixtures/managed-workflows/`, and the deliberately spelled-out copies in
# `tests/test_github_ci_render.py` and `tests/cli/test_init.py` each assert the value
# independently, and each one fails until it follows.
# This module is the correct owner because it is the leaf both renderers
# already sit above: scaffold.py imports it, github_ci/render.py imports PYTHON_PIN from
# scaffold.py, so the dependency only ever points this way.
#
# These are the same pins this repository's own workflows run, which
# `tests/test_workflow_pinning.py` enforces: a frozen SHA cannot drift from a floating tag on
# its own, so the shipped pin is kept current by being the pin our own CI depends on.
CHECKOUT_REF: str = "3d3c42e5aac5ba805825da76410c181273ba90b1"  # pragma: allowlist secret
CHECKOUT_VERSION: str = "v7.0.1"
SETUP_UV_REF: str = "20cfd1bf945f4377ade1205e4dbc17946fc9a30d"  # pragma: allowlist secret
SETUP_UV_VERSION: str = "v10.0.1"

# The composed `uses:` fragment both renderers emit verbatim. Owning the assembled form here,
# not just its halves, keeps the `@<sha> # vX.Y.Z` shape in one place rather than hand-spelled
# beside every pinned step.
CHECKOUT_USES: str = f"actions/checkout@{CHECKOUT_REF} # {CHECKOUT_VERSION}"
SETUP_UV_USES: str = f"astral-sh/setup-uv@{SETUP_UV_REF} # {SETUP_UV_VERSION}"

# Reconcile transaction schema plus the shared journal and staged-image naming contract.
RECONCILE_JOURNAL_NAME: str = ".doc-lattice-reconcile.json"
RECONCILE_JOURNAL_VERSION: int = 1
PERSISTENCE_TEMP_SUFFIX: str = ".tmp"
RECONCILE_BEFORE_IMAGE_INFIX: str = ".doc-lattice-before."
RECONCILE_AFTER_IMAGE_INFIX: str = ".doc-lattice-after."
