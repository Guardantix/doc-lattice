"""Type-safe constants with runtime validation."""

from typing import Literal, get_args

Status = Literal["active", "inactive"]
VALID_STATUSES: frozenset[str] = frozenset(get_args(Status))

Layer = Literal["design", "technical", "production"]
VALID_LAYERS: frozenset[str] = frozenset(get_args(Layer))

Authority = Literal["binding", "derived", "exploratory"]
VALID_AUTHORITIES: frozenset[str] = frozenset(get_args(Authority))

LocationKind = Literal["file", "section"]
VALID_LOCATION_KINDS: frozenset[str] = frozenset(get_args(LocationKind))

EdgeState = Literal["OK", "STALE", "UNRECONCILED", "BROKEN"]
VALID_EDGE_STATES: frozenset[str] = frozenset(get_args(EdgeState))
