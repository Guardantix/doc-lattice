# doc-lattice Linear Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `doc-lattice linear` command that resolves the `PC-*` tickets referenced in lattice frontmatter to live Linear status and reports shipped-against-stale-spec drift.

**Architecture:** A pure-core, impure-edge slice. The network is sealed into a transport (`linear_client`), a boundary parser (`linear_parser`), and thin wiring (`linear_fetch`); the entire stale-shipped analysis (`stale_shipped`) is a pure join over a `Mapping[identifier, Ticket]`, reusing the existing `check` and `impact` modules unchanged. `linear` is the only network-touching command; `check`, `impact`, `graph`, and `reconcile` stay byte-for-byte offline.

**Tech Stack:** Python 3.14, typer, rich, pydantic v2, stdlib `urllib.request` (no new HTTP dependency), hypothesis (dev). uv for dependency management and execution.

**Binding spec:** `docs/superpowers/specs/2026-06-27-doc-lattice-linear-design.md`. When code and this plan disagree with the spec, the spec wins.

## Global Constraints

- Python `>=3.14`. Dependencies unchanged: no new third-party packages (transport is stdlib `urllib.request`).
- ruff line length 100; every module needs a module docstring; public functions need Google-style docstrings; no em-dashes in any docstring, comment, message, or string literal.
- `typing.Any` and `typing.cast` are allowed ONLY in `linear_parser.py` (name ends in `_parser`). Every other new module must be fully typed with no `Any`/`cast`.
- All custom exceptions extend `ProjectError` and carry a `code`. No bare `except Exception`/`except BaseException`. Error messages name the cause and the fix and never include the API key or `Authorization` header.
- No `datetime.now()`/`utcnow()` anywhere in this slice.
- Use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py`; import constants, do not duplicate their string values as raw literals.
- The repo ships zero secrets. Every test mocks the transport: no real network, no real `LINEAR_API_KEY`, only synthetic data.
- Per-task test runs use `--no-cov` (the always-on coverage gate fails on partial selections). The final task runs the full suite, which enforces coverage `>=80%`.
- Commit after every task. The pre-commit hook runs ruff (with `--fix`), ruff-format, `ty`, the typing-boundary check, and detect-secrets; if a hook auto-fixes a file, re-stage and re-commit. Work on a feature branch, never commit to `main`.

---

## File Structure

New source modules under `src/doc_lattice/`:

| Module | Responsibility | Pure? |
|---|---|---|
| `text_utils.py` | `strip_control_chars(text)`: remove ASCII control bytes; shared by the parser and the renderer | pure |
| `tickets.py` | `TicketState`, `TicketRef`, `Ticket` (pydantic, control-stripping), `Finding` (frozen dataclass) | pure |
| `linear_query.py` | `partition_identifiers`, `chunk_identifiers`, `build_query`; the identifier cap and ASCII/team validation | pure |
| `linear_client.py` | Transport: stdlib POST, https-only guard, no-redirect opener, lazy key, timeout, capped read | impure I/O |
| `linear_parser.py` | Boundary: JSON envelope to a `Ticket` map keyed by queried id, plus the unresolved set | boundary |
| `linear_fetch.py` | Wiring: partition, chunk, query, client, parser into `(ticket_map, rejected)`; empty-set skip | impure |
| `stale_shipped.py` | Pure join: build the trigger map (audit or `--from`) and grade tickets into ordered `Finding`s | pure |
| `linear_render.py` | `render_safe` helper, the severity-grouped human table, and the `--json` payload | pure |

Edits to existing modules: `constants.py` (three literals), `error_types.py` (`LinearError`), `cli.py` (the `linear` command). `model.py`, `check.py`, `impact.py`, `graph`, `reconcile.py`, `orchestrate.py` are not modified.

Tests mirror sources one to one: `tests/test_<module>.py` for each new module, plus additions to `tests/test_constants.py`, `tests/test_error_types.py`, and `tests/test_cli.py`.

---

## Task 1: Constants and the LinearError type

**Files:**
- Modify: `src/doc_lattice/constants.py`
- Modify: `src/doc_lattice/error_types.py`
- Test: `tests/test_constants.py`, `tests/test_error_types.py`

**Interfaces:**
- Produces: `LinearStateType`, `Severity`, `BlockedReason` literals and `VALID_LINEAR_STATE_TYPES`, `VALID_SEVERITIES`, `VALID_BLOCKED_REASONS` frozensets in `constants.py`; `LinearError(ProjectError)` with `code="LINEAR_ERROR"` in `error_types.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_constants.py` (add the new names to the existing import from `doc_lattice.constants`):

```python
from doc_lattice.constants import (
    VALID_BLOCKED_REASONS,
    VALID_LINEAR_STATE_TYPES,
    VALID_SEVERITIES,
    BlockedReason,
    LinearStateType,
    Severity,
)


def test_linear_state_types_match_literal():
    assert frozenset(get_args(LinearStateType)) == VALID_LINEAR_STATE_TYPES
    assert {"triage", "backlog", "unstarted", "started", "completed", "canceled"} == set(
        VALID_LINEAR_STATE_TYPES
    )


def test_severities_match_literal():
    assert frozenset(get_args(Severity)) == VALID_SEVERITIES
    assert {"DANGER", "WARNING", "INFO", "BLOCKED"} == set(VALID_SEVERITIES)


def test_blocked_reasons_match_literal():
    assert frozenset(get_args(BlockedReason)) == VALID_BLOCKED_REASONS
    assert {"malformed", "not-found", "cross-team"} == set(VALID_BLOCKED_REASONS)
```

Append to `tests/test_error_types.py` (add `LinearError` to the import):

```python
from doc_lattice.error_types import LinearError


def test_linear_error_inherits_and_has_code():
    err = LinearError("network down")
    assert isinstance(err, ProjectError)
    assert err.code == "LINEAR_ERROR"
    assert str(err) == "network down"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_constants.py tests/test_error_types.py -v --no-cov`
Expected: FAIL with ImportError (the new names do not exist yet).

- [ ] **Step 3: Add the constants and the error**

Append to `src/doc_lattice/constants.py`:

```python
LinearStateType = Literal["triage", "backlog", "unstarted", "started", "completed", "canceled"]
VALID_LINEAR_STATE_TYPES: frozenset[str] = frozenset(get_args(LinearStateType))

Severity = Literal["DANGER", "WARNING", "INFO", "BLOCKED"]
VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))

BlockedReason = Literal["malformed", "not-found", "cross-team"]
VALID_BLOCKED_REASONS: frozenset[str] = frozenset(get_args(BlockedReason))
```

Append to `src/doc_lattice/error_types.py`:

```python
class LinearError(ProjectError):
    """A Linear network, credential, or response error."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="LINEAR_ERROR")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_constants.py tests/test_error_types.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/constants.py src/doc_lattice/error_types.py tests/test_constants.py tests/test_error_types.py
git commit -m "feat: add linear constants and LinearError"
```

---

## Task 2: Control-character stripping helper

**Files:**
- Create: `src/doc_lattice/text_utils.py`
- Test: `tests/test_text_utils.py`

**Interfaces:**
- Produces: `strip_control_chars(text: str) -> str`. Removes every ASCII control byte (code point `< 0x20` or `== 0x7F`). Used by `tickets.py` validators and `linear_render.render_safe`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_text_utils.py`:

```python
"""Tests for text_utils."""

from hypothesis import given
from hypothesis import strategies as st

from doc_lattice.text_utils import strip_control_chars


def test_strips_escape_and_controls():
    assert strip_control_chars("a\x1b[31mb\x07c\x7f") == "a[31mbc"


def test_keeps_ordinary_text():
    assert strip_control_chars("PC-228 Done") == "PC-228 Done"


@given(st.text())
def test_output_has_no_control_bytes(text: str):
    cleaned = strip_control_chars(text)
    assert all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in cleaned)


@given(st.text())
def test_is_idempotent(text: str):
    once = strip_control_chars(text)
    assert strip_control_chars(once) == once
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_text_utils.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the helper**

Create `src/doc_lattice/text_utils.py`:

```python
"""Small pure text helpers shared across the linear slice."""


def strip_control_chars(text: str) -> str:
    """Remove ASCII control bytes so untrusted strings cannot corrupt terminal output.

    Args:
        text: Any string, possibly from a repo or a network response.

    Returns:
        The text with every code point below ``0x20`` or equal to ``0x7F`` removed.
        Ordinary printable characters, including non-ASCII letters, are preserved.
    """
    return "".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7F)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_text_utils.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/text_utils.py tests/test_text_utils.py
git commit -m "feat: add strip_control_chars text helper"
```

---

## Task 3: Ticket domain types

**Files:**
- Create: `src/doc_lattice/tickets.py`
- Test: `tests/test_tickets.py`

**Interfaces:**
- Consumes: `strip_control_chars` from `text_utils`; `LinearStateType`, `Severity`, `BlockedReason` from `constants`.
- Produces:
  - `TicketState(name: str, type: LinearStateType)` (pydantic, frozen).
  - `TicketRef(identifier: str, title: str | None, state: TicketState)` (pydantic, frozen).
  - `Ticket(identifier: str, title: str | None, url: str, state: TicketState, parent: TicketRef | None, children: tuple[TicketRef, ...])` (pydantic, frozen, `extra="ignore"`). All string fields are control-stripped on construction.
  - `Finding(severity: Severity, node_id: str, node_title: str | None, node_path: Path, drifted_refs: tuple[str, ...], ticket_ref: str, reason: BlockedReason | None, ticket: Ticket | None)` (frozen dataclass).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tickets.py`:

```python
"""Tests for ticket domain types."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from doc_lattice.tickets import Finding, Ticket, TicketRef, TicketState


def _state(type_: str = "completed") -> TicketState:
    return TicketState(name="Done", type=type_)


def test_ticket_construction():
    ticket = Ticket(
        identifier="PC-228",
        title="Accent tokens",
        url="https://linear.app/acme/issue/PC-228",
        state=_state(),
        parent=None,
        children=(),
    )
    assert ticket.state.type == "completed"
    assert ticket.children == ()


def test_string_fields_are_control_stripped():
    ticket = Ticket(
        identifier="PC-228",
        title="a\x1bb",
        url="https://x/\x07PC-228",
        state=TicketState(name="In\x7fReview", type="started"),
        parent=None,
        children=(),
    )
    assert ticket.title == "ab"
    assert ticket.url == "https://x/PC-228"
    assert ticket.state.name == "InReview"


def test_invalid_state_type_rejected():
    with pytest.raises(ValidationError):
        TicketState(name="Weird", type="archived")


def test_graded_finding_has_ticket_and_no_reason():
    ticket = Ticket(
        identifier="PC-228", title=None, url="https://x", state=_state(), parent=None, children=()
    )
    finding = Finding(
        severity="DANGER",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#accent",),
        ticket_ref="PC-228",
        reason=None,
        ticket=ticket,
    )
    assert finding.reason is None
    assert finding.ticket is ticket


def test_blocked_finding_has_reason_and_no_ticket():
    finding = Finding(
        severity="BLOCKED",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#motion",),
        ticket_ref="PC-999",
        reason="not-found",
        ticket=None,
    )
    assert finding.reason == "not-found"
    assert finding.ticket is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_tickets.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the types**

Create `src/doc_lattice/tickets.py`:

```python
"""Domain types for resolved Linear tickets and the findings they produce."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

from .constants import BlockedReason, LinearStateType, Severity
from .text_utils import strip_control_chars

CleanStr = Annotated[str, AfterValidator(strip_control_chars)]
CleanOptStr = Annotated[
    str | None, AfterValidator(lambda v: strip_control_chars(v) if v is not None else None)
]


class TicketState(BaseModel):
    """A Linear workflow state. ``type`` drives grading; ``name`` is the display label."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: CleanStr
    type: LinearStateType


class TicketRef(BaseModel):
    """A lightweight reference to a parent or child ticket, for context only."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: CleanStr
    title: CleanOptStr = None
    state: TicketState


class Ticket(BaseModel):
    """One resolved Linear issue. All string fields are control-stripped on construction."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: CleanStr
    title: CleanOptStr = None
    url: CleanStr
    state: TicketState
    parent: TicketRef | None = None
    children: tuple[TicketRef, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    """One reportable result.

    For a graded finding (DANGER, WARNING, INFO), ``ticket`` is the resolved issue and
    ``reason`` is None. For a BLOCKED finding, ``ticket`` is None and ``reason`` says why the
    ref could not be resolved. The two fields are mutually exclusive.
    """

    severity: Severity
    node_id: str
    node_title: str | None
    node_path: Path
    drifted_refs: tuple[str, ...]
    ticket_ref: str
    reason: BlockedReason | None
    ticket: Ticket | None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_tickets.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/tickets.py tests/test_tickets.py
git commit -m "feat: add ticket and finding domain types"
```

---

## Task 4: Query builder, identifier partition, and chunking

**Files:**
- Create: `src/doc_lattice/linear_query.py`
- Test: `tests/test_linear_query.py`

**Interfaces:**
- Consumes: `BlockedReason` from `constants`; `LinearError` and `ConfigError` from `error_types`.
- Produces:
  - `BATCH_SIZE = 50`, `MAX_IDENTIFIERS = 500` module constants.
  - `partition_identifiers(identifiers: Iterable[str], linear_team: str | None) -> tuple[list[str], dict[str, BlockedReason]]`. Dedupes input order-preservingly, raises `LinearError` if the distinct count exceeds `MAX_IDENTIFIERS`, validates `linear_team` shape (raising `ConfigError` on a malformed team key), and returns the valid identifiers plus a `rejected` map tagging each refused ref `"malformed"` or `"cross-team"`.
  - `chunk_identifiers(identifiers: Sequence[str], size: int = BATCH_SIZE) -> list[list[str]]`.
  - `QueryPlan(document: str, variables: dict[str, str], alias_to_id: dict[str, str])` (frozen dataclass).
  - `build_query(identifiers: Sequence[str]) -> QueryPlan`. Builds one GraphQL `query` document with one index-aliased `issue(id:)` field per identifier, ids passed as variables, and the alias-to-id map.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linear_query.py`:

```python
"""Tests for the linear query builder and identifier partition."""

import pytest

from doc_lattice.error_types import ConfigError, LinearError
from doc_lattice.linear_query import (
    BATCH_SIZE,
    MAX_IDENTIFIERS,
    build_query,
    chunk_identifiers,
    partition_identifiers,
)


def test_partition_splits_valid_and_malformed():
    valid, rejected = partition_identifiers(["PC-228", "not-a-ticket", "", "  "], None)
    assert valid == ["PC-228"]
    assert rejected == {"not-a-ticket": "malformed", "": "malformed", "  ": "malformed"}


def test_partition_dedupes_preserving_order():
    valid, _ = partition_identifiers(["PC-2", "PC-1", "PC-2"], None)
    assert valid == ["PC-2", "PC-1"]


def test_partition_rejects_non_ascii_digits():
    # Arabic-Indic digits must not pass the ASCII identifier guard.
    valid, rejected = partition_identifiers(["PC-٢٣"], None)
    assert valid == []
    assert rejected["PC-٢٣"] == "malformed"


def test_partition_tags_cross_team_when_team_set():
    valid, rejected = partition_identifiers(["PC-1", "SEC-9"], "PC")
    assert valid == ["PC-1"]
    assert rejected == {"SEC-9": "cross-team"}


def test_partition_queries_off_team_when_no_team_set():
    valid, rejected = partition_identifiers(["SEC-9"], None)
    assert valid == ["SEC-9"]
    assert rejected == {}


def test_partition_rejects_malformed_team_key():
    with pytest.raises(ConfigError):
        partition_identifiers(["PC-1"], "p c")


def test_partition_cap_raises_one_over_but_not_at_cap():
    at_cap = [f"PC-{i}" for i in range(MAX_IDENTIFIERS)]
    valid, _ = partition_identifiers(at_cap, None)
    assert len(valid) == MAX_IDENTIFIERS
    over = [f"PC-{i}" for i in range(MAX_IDENTIFIERS + 1)]
    with pytest.raises(LinearError):
        partition_identifiers(over, None)


def test_chunking_boundary():
    exactly = [f"PC-{i}" for i in range(BATCH_SIZE)]
    assert len(chunk_identifiers(exactly)) == 1
    one_more = [f"PC-{i}" for i in range(BATCH_SIZE + 1)]
    assert len(chunk_identifiers(one_more)) == 2


def test_build_query_is_read_only_and_parameterized():
    plan = build_query(["PC-1", "PC-2"])
    assert "query" in plan.document
    assert "mutation" not in plan.document
    # Identifiers travel as variables, never interpolated into the document text.
    assert "PC-1" not in plan.document
    assert set(plan.variables.values()) == {"PC-1", "PC-2"}
    assert set(plan.alias_to_id.values()) == {"PC-1", "PC-2"}
    assert len(set(plan.alias_to_id)) == 2  # aliases are unique
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_linear_query.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the query builder**

Create `src/doc_lattice/linear_query.py`:

```python
"""Pure construction of the batched Linear GraphQL query and identifier partition."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .constants import BlockedReason
from .error_types import ConfigError, LinearError

BATCH_SIZE = 50
MAX_IDENTIFIERS = 500

_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$", re.ASCII)
_TEAM_RE = re.compile(r"^[A-Z][A-Z0-9]*$", re.ASCII)

_TICKET_FRAGMENT = """
fragment T on Issue {
  identifier
  title
  url
  state { name type }
  parent { identifier title state { name type } }
  children(first: 50) { nodes { identifier title state { name type } } }
}
"""


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A built query: the document, its variables, and the alias-to-identifier map."""

    document: str
    variables: dict[str, str]
    alias_to_id: dict[str, str]


def partition_identifiers(
    identifiers: Iterable[str], linear_team: str | None
) -> tuple[list[str], dict[str, BlockedReason]]:
    """Split identifiers into the set to query and the refs refused before any fetch.

    Args:
        identifiers: Raw ``tickets:`` values, possibly with duplicates.
        linear_team: The configured team key, or None for no team boundary.

    Returns:
        A tuple of the valid identifiers (deduplicated, order preserved) and a map of each
        refused ref to ``"malformed"`` or ``"cross-team"``.

    Raises:
        ConfigError: If ``linear_team`` is set but is not a valid team key.
        LinearError: If the distinct identifier count exceeds ``MAX_IDENTIFIERS``.
    """
    if linear_team is not None and not _TEAM_RE.match(linear_team):
        msg = f"linear_team {linear_team!r} is not a valid team key; fix .doc-lattice.yml"
        raise ConfigError(msg)
    distinct = list(dict.fromkeys(identifiers))
    if len(distinct) > MAX_IDENTIFIERS:
        msg = (
            f"too many referenced tickets ({len(distinct)} > {MAX_IDENTIFIERS}); "
            "narrow the scope with a positional target or --from"
        )
        raise LinearError(msg)
    valid: list[str] = []
    rejected: dict[str, BlockedReason] = {}
    for ref in distinct:
        if not _IDENTIFIER_RE.match(ref):
            rejected[ref] = "malformed"
        elif linear_team is not None and ref.split("-", 1)[0] != linear_team:
            rejected[ref] = "cross-team"
        else:
            valid.append(ref)
    return valid, rejected


def chunk_identifiers(identifiers: Sequence[str], size: int = BATCH_SIZE) -> list[list[str]]:
    """Split identifiers into batches of at most ``size``.

    Args:
        identifiers: The valid identifiers to query.
        size: The maximum batch size.

    Returns:
        A list of batches; empty input yields an empty list.
    """
    return [list(identifiers[i : i + size]) for i in range(0, len(identifiers), size)]


def build_query(identifiers: Sequence[str]) -> QueryPlan:
    """Build one aliased batched query for the given identifiers.

    Args:
        identifiers: The identifiers in a single batch (at most ``BATCH_SIZE``).

    Returns:
        A QueryPlan whose document fetches each identifier under an index alias, passes the
        identifiers as variables, and records the alias-to-identifier map for keying results.
    """
    var_decls: list[str] = []
    fields: list[str] = []
    variables: dict[str, str] = {}
    alias_to_id: dict[str, str] = {}
    for index, identifier in enumerate(identifiers):
        var = f"id{index}"
        alias = f"i{index}"
        var_decls.append(f"${var}: String!")
        fields.append(f"  {alias}: issue(id: ${var}) {{ ...T }}")
        variables[var] = identifier
        alias_to_id[alias] = identifier
    document = "query Batch(" + ", ".join(var_decls) + ") {\n" + "\n".join(fields) + "\n}\n"
    document += _TICKET_FRAGMENT
    return QueryPlan(document=document, variables=variables, alias_to_id=alias_to_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_linear_query.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/linear_query.py tests/test_linear_query.py
git commit -m "feat: add linear query builder and identifier partition"
```

---

## Task 5: Transport client

**Files:**
- Create: `src/doc_lattice/linear_client.py`
- Test: `tests/test_linear_client.py`

**Interfaces:**
- Consumes: `LinearError` from `error_types`.
- Produces:
  - `LINEAR_GRAPHQL_URL`, `DEFAULT_TIMEOUT`, `MAX_RESPONSE_BYTES` constants.
  - `LinearClient(url=LINEAR_GRAPHQL_URL, timeout=DEFAULT_TIMEOUT, opener=None)`. The scheme guard rejects any non-`https` URL with `LinearError`. The default opener refuses redirects.
  - `LinearClient.execute(document: str, variables: dict[str, str]) -> str`. Reads `LINEAR_API_KEY` lazily, POSTs, returns the raw response text. Raises `LinearError` on a missing key, an HTTP or URL error, or an oversized response. Interprets no GraphQL content.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linear_client.py`:

```python
"""Tests for the Linear transport client (mocked, no real network)."""

import io
import urllib.error

import pytest

from doc_lattice.error_types import LinearError
from doc_lattice.linear_client import LinearClient


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


class _FakeOpener:
    def __init__(self, body: bytes = b'{"data":{}}'):
        self.body = body
        self.captured = None
        self.timeout = None

    def open(self, req, timeout=None):
        self.captured = req
        self.timeout = timeout
        return _FakeResp(self.body)


def test_rejects_non_https_url():
    with pytest.raises(LinearError):
        LinearClient(url="http://api.linear.app/graphql")


def test_execute_sends_authorized_post(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")
    opener = _FakeOpener(b'{"data":{"i0":null}}')
    client = LinearClient(opener=opener)
    body = client.execute("query {}", {"id0": "PC-1"})
    assert body == '{"data":{"i0":null}}'
    assert opener.captured.get_method() == "POST"
    assert opener.captured.headers["Authorization"] == "secret-key"
    assert opener.captured.headers["Content-type"] == "application/json"
    assert opener.timeout == client._timeout


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(LinearError) as exc:
        LinearClient(opener=_FakeOpener()).execute("query {}", {})
    assert "secret-key" not in str(exc.value)


def test_http_error_maps_to_linear_error_without_key(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")

    class _Boom:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)

    with pytest.raises(LinearError) as exc:
        LinearClient(opener=_Boom()).execute("query {}", {})
    assert "429" in str(exc.value)
    assert "secret-key" not in str(exc.value)


def test_url_error_maps_to_linear_error(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")

    class _Boom:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("name resolution failed")

    with pytest.raises(LinearError):
        LinearClient(opener=_Boom()).execute("query {}", {})


def test_oversized_response_raises(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "secret-key")
    from doc_lattice.linear_client import MAX_RESPONSE_BYTES

    opener = _FakeOpener(b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(LinearError):
        LinearClient(opener=opener).execute("query {}", {})


def test_no_redirect_handler_returns_none():
    from doc_lattice.linear_client import _NoRedirect

    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://evil") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_linear_client.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the transport**

Create `src/doc_lattice/linear_client.py`:

```python
"""Synchronous Linear GraphQL transport over stdlib urllib, hardened against SSRF."""

import json
import os
import urllib.error
import urllib.request

from . import __version__
from .error_types import LinearError

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
DEFAULT_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses every redirect.

    ``urllib`` follows 3xx by default, which would let a hostile or intercepted response
    steer the credentialed client at an internal address. Returning None turns a redirect
    into an HTTPError that the client maps to a LinearError.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, ARG002
        return None


class LinearClient:
    """A GraphQL client that reads ``LINEAR_API_KEY`` lazily and never follows redirects."""

    def __init__(
        self,
        url: str = LINEAR_GRAPHQL_URL,
        timeout: float = DEFAULT_TIMEOUT,
        opener: object | None = None,
    ) -> None:
        """Build a client.

        Args:
            url: The GraphQL endpoint. Must be ``https`` so the key never crosses the wire
                in cleartext.
            timeout: Per-request timeout in seconds.
            opener: An opener with an ``open(req, timeout=...)`` method, for tests. The
                default refuses redirects.

        Raises:
            LinearError: If ``url`` is not an ``https`` URL.
        """
        if not url.startswith("https://"):
            msg = f"LinearClient refuses non-https URL {url!r}; the API key rides in a header"
            raise LinearError(msg)
        self._url = url
        self._timeout = timeout
        self._opener = opener if opener is not None else urllib.request.build_opener(_NoRedirect)

    def execute(self, document: str, variables: dict[str, str]) -> str:
        """POST a GraphQL document and return the raw response text.

        Args:
            document: The GraphQL query document.
            variables: The query variables.

        Returns:
            The decoded response body. GraphQL ``errors`` and ``data`` are interpreted by the
            parser, not here.

        Raises:
            LinearError: On a missing key, an HTTP or URL error, or an oversized response. The
                message never includes the key or the Authorization header.
        """
        api_key = os.environ.get("LINEAR_API_KEY", "").strip()
        if not api_key:
            msg = "LINEAR_API_KEY is not set; export it, or run impact for the offline view"
            raise LinearError(msg)
        payload = json.dumps({"query": document, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - https scheme enforced in __init__
            self._url,
            data=payload,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "User-Agent": f"doc-lattice/{__version__}",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise LinearError(f"Linear HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LinearError(f"Linear network error: {exc.reason}") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise LinearError("Linear response exceeded the size cap; refusing to parse it")
        return body.decode("utf-8", errors="replace")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_linear_client.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/linear_client.py tests/test_linear_client.py
git commit -m "feat: add hardened linear transport client"
```

---

## Task 6: Response parser (boundary)

**Files:**
- Create: `src/doc_lattice/linear_parser.py`
- Test: `tests/test_linear_parser.py`

**Interfaces:**
- Consumes: `Ticket`, `TicketRef`, `TicketState` from `tickets`; `LinearError` from `error_types`.
- Produces: `parse_tickets(response_text: str, alias_to_id: Mapping[str, str]) -> tuple[dict[str, Ticket], set[str]]`. Returns the ticket map keyed by the queried identifier (not the echoed one) and the set of identifiers Linear returned `null` for. Raises `LinearError` on invalid JSON, a GraphQL `errors` array, a missing `data` object, or a malformed issue node.
- This is the only new module permitted `Any`/`cast`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linear_parser.py`:

```python
"""Tests for the Linear response parser boundary."""

import json

import pytest

from doc_lattice.error_types import LinearError
from doc_lattice.linear_parser import parse_tickets


def _issue(identifier="PC-1", state_type="completed"):
    return {
        "identifier": identifier,
        "title": "Accent",
        "url": "https://linear.app/acme/issue/" + identifier,
        "state": {"name": "Done", "type": state_type},
        "parent": None,
        "children": {"nodes": [{"identifier": "PC-9", "title": "Sub", "state": {"name": "Doing", "type": "started"}}]},
    }


def test_parses_ticket_with_children():
    text = json.dumps({"data": {"i0": _issue()}})
    tickets, unresolved = parse_tickets(text, {"i0": "PC-1"})
    assert unresolved == set()
    ticket = tickets["PC-1"]
    assert ticket.url.endswith("PC-1")
    assert ticket.children[0].identifier == "PC-9"


def test_keys_by_queried_id_not_echo():
    # Linear echoes a lowercased identifier; the map must still be keyed by what we queried.
    echo = _issue(identifier="pc-1")
    text = json.dumps({"data": {"i0": echo}})
    tickets, _ = parse_tickets(text, {"i0": "PC-1"})
    assert "PC-1" in tickets
    assert "pc-1" not in tickets


def test_null_issue_is_unresolved():
    text = json.dumps({"data": {"i0": None}})
    tickets, unresolved = parse_tickets(text, {"i0": "PC-404"})
    assert tickets == {}
    assert unresolved == {"PC-404"}


def test_unknown_extra_field_ignored():
    issue = _issue()
    issue["surprise"] = "new linear field"
    text = json.dumps({"data": {"i0": issue}})
    tickets, _ = parse_tickets(text, {"i0": "PC-1"})
    assert tickets["PC-1"].identifier == "PC-1"


def test_control_chars_stripped_from_url_and_identifier():
    issue = _issue()
    issue["url"] = "https://x/\x1bPC-1"
    text = json.dumps({"data": {"i0": issue}})
    tickets, _ = parse_tickets(text, {"i0": "PC-1"})
    assert "\x1b" not in tickets["PC-1"].url


def test_graphql_errors_raise():
    text = json.dumps({"errors": [{"message": "rate limited"}]})
    with pytest.raises(LinearError):
        parse_tickets(text, {"i0": "PC-1"})


def test_missing_data_raises():
    with pytest.raises(LinearError):
        parse_tickets(json.dumps({"meta": 1}), {"i0": "PC-1"})


def test_invalid_json_raises():
    with pytest.raises(LinearError):
        parse_tickets("not json", {"i0": "PC-1"})


def test_malformed_issue_raises():
    text = json.dumps({"data": {"i0": {"identifier": "PC-1"}}})  # missing url/state
    with pytest.raises(LinearError):
        parse_tickets(text, {"i0": "PC-1"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_linear_parser.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the parser**

Create `src/doc_lattice/linear_parser.py`:

```python
"""Boundary: validate a raw Linear GraphQL response into typed tickets.

This is the only linear module permitted ``Any`` and ``cast``: it converts the untyped JSON
envelope into typed ``Ticket`` models keyed by the identifier that was queried.
"""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .error_types import LinearError
from .tickets import Ticket, TicketRef, TicketState


def parse_tickets(
    response_text: str, alias_to_id: Mapping[str, str]
) -> tuple[dict[str, Ticket], set[str]]:
    """Parse a response into a ticket map keyed by queried id, plus the unresolved ids.

    Args:
        response_text: The raw response body from the transport.
        alias_to_id: The query's alias-to-identifier map.

    Returns:
        A tuple of the resolved tickets keyed by the queried identifier and the set of
        identifiers Linear returned ``null`` for.

    Raises:
        LinearError: On invalid JSON, a GraphQL ``errors`` array, a missing ``data`` object,
            or a malformed issue node.
    """
    try:
        parsed: Any = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LinearError(f"Linear response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LinearError("Linear response was not a JSON object")
    if parsed.get("errors"):
        messages = "; ".join(
            str(e.get("message", "<no message>")) for e in parsed["errors"] if isinstance(e, dict)
        )
        raise LinearError(f"Linear returned GraphQL errors: {messages}")
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise LinearError("Linear response is missing its data object")

    tickets: dict[str, Ticket] = {}
    unresolved: set[str] = set()
    for alias, identifier in alias_to_id.items():
        node = data.get(alias)
        if node is None:
            unresolved.add(identifier)
            continue
        tickets[identifier] = _ticket_from_node(node)
    return tickets, unresolved


def _state(raw: Any) -> TicketState:
    return TicketState(name=raw["name"], type=raw["type"])


def _ref(raw: Any) -> TicketRef:
    return TicketRef(identifier=raw["identifier"], title=raw.get("title"), state=_state(raw["state"]))


def _ticket_from_node(node: Any) -> Ticket:
    """Build a typed Ticket from one issue node, ignoring fields we did not request.

    Raises:
        LinearError: If a required field is missing or the node is malformed.
    """
    try:
        children_nodes = (node.get("children") or {}).get("nodes") or []
        parent_raw = node.get("parent")
        return Ticket(
            identifier=node["identifier"],
            title=node.get("title"),
            url=node["url"],
            state=_state(node["state"]),
            parent=_ref(parent_raw) if parent_raw else None,
            children=tuple(_ref(child) for child in children_nodes),
        )
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise LinearError(f"Linear issue node was malformed: {exc}") from exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_linear_parser.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Verify the typing boundary still holds, then commit**

Run: `uv run --group dev python scripts/check_typing_boundaries.py src`
Expected: passes (`Any` appears only in `linear_parser.py`).

```bash
git add src/doc_lattice/linear_parser.py tests/test_linear_parser.py
git commit -m "feat: add linear response parser boundary"
```

---

## Task 7: Fetch wiring

**Files:**
- Create: `src/doc_lattice/linear_fetch.py`
- Test: `tests/test_linear_fetch.py`

**Interfaces:**
- Consumes: `partition_identifiers`, `chunk_identifiers`, `build_query`, `BATCH_SIZE` from `linear_query`; `LinearClient` from `linear_client`; `parse_tickets` from `linear_parser`; `Ticket` from `tickets`; `BlockedReason` from `constants`.
- Produces: `fetch_tickets(identifiers: Iterable[str], linear_team: str | None, client: LinearClient | None = None) -> tuple[dict[str, Ticket], dict[str, BlockedReason]]`. Partitions, and if no valid identifier remains returns `({}, rejected)` without constructing a client or reading the key. Otherwise fetches each batch and merges. The optional `client` is a test seam.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linear_fetch.py`:

```python
"""Tests for the linear fetch wiring (mocked client, no real network)."""

import json

from doc_lattice.linear_fetch import fetch_tickets


class _RecordingClient:
    def __init__(self, body_for):
        self.body_for = body_for
        self.calls = 0

    def execute(self, document, variables):
        self.calls += 1
        return self.body_for(variables)


def _issue(identifier):
    return {
        "identifier": identifier,
        "title": "t",
        "url": "https://x/" + identifier,
        "state": {"name": "Done", "type": "completed"},
        "parent": None,
        "children": {"nodes": []},
    }


def test_empty_identifiers_skip_network(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    def explode(*_a, **_k):
        raise AssertionError("must not construct a client")

    monkeypatch.setattr("doc_lattice.linear_fetch.LinearClient", explode)
    tickets, rejected = fetch_tickets(["not-a-ticket"], None)
    assert tickets == {}
    assert rejected == {"not-a-ticket": "malformed"}


def test_dedup_and_keying():
    client = _RecordingClient(lambda v: json.dumps(
        {"data": {f"i{i}": _issue(ident) for i, ident in enumerate(v.values())}}
    ))
    tickets, rejected = fetch_tickets(["PC-1", "PC-1", "PC-2"], None, client=client)
    assert set(tickets) == {"PC-1", "PC-2"}
    assert rejected == {}


def test_chunks_merge(monkeypatch):
    monkeypatch.setattr("doc_lattice.linear_fetch.BATCH_SIZE", 1)
    client = _RecordingClient(lambda v: json.dumps(
        {"data": {f"i{i}": _issue(ident) for i, ident in enumerate(v.values())}}
    ))
    tickets, _ = fetch_tickets(["PC-1", "PC-2"], None, client=client)
    assert set(tickets) == {"PC-1", "PC-2"}
    assert client.calls == 2  # one request per chunk


def test_unresolved_is_absent_from_map():
    client = _RecordingClient(lambda v: json.dumps({"data": {"i0": None}}))
    tickets, _ = fetch_tickets(["PC-404"], None, client=client)
    assert tickets == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_linear_fetch.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the fetch wiring**

Create `src/doc_lattice/linear_fetch.py`:

```python
"""Impure wiring: turn referenced identifiers into a resolved ticket map."""

from collections.abc import Iterable

from .constants import BlockedReason
from .linear_client import LinearClient
from .linear_parser import parse_tickets
from .linear_query import BATCH_SIZE, build_query, chunk_identifiers, partition_identifiers
from .tickets import Ticket


def fetch_tickets(
    identifiers: Iterable[str],
    linear_team: str | None,
    client: LinearClient | None = None,
) -> tuple[dict[str, Ticket], dict[str, BlockedReason]]:
    """Resolve referenced identifiers against Linear.

    Args:
        identifiers: Raw ``tickets:`` values collected from trigger nodes.
        linear_team: The configured team key, or None.
        client: An injected client for tests; the default is a real ``LinearClient``.

    Returns:
        A tuple of the resolved tickets keyed by queried identifier and the ``rejected`` map
        of refs refused before any fetch. When no valid identifier remains, the network is
        not touched and ``LINEAR_API_KEY`` is not read.
    """
    valid, rejected = partition_identifiers(identifiers, linear_team)
    if not valid:
        return {}, rejected
    live = client if client is not None else LinearClient()
    tickets: dict[str, Ticket] = {}
    for chunk in chunk_identifiers(valid, BATCH_SIZE):
        plan = build_query(chunk)
        body = live.execute(plan.document, plan.variables)
        chunk_tickets, _unresolved = parse_tickets(body, plan.alias_to_id)
        tickets.update(chunk_tickets)
    return tickets, rejected
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_linear_fetch.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/linear_fetch.py tests/test_linear_fetch.py
git commit -m "feat: add linear fetch wiring with empty-set skip"
```

---

## Task 8: The stale-shipped join

**Files:**
- Create: `src/doc_lattice/stale_shipped.py`
- Test: `tests/test_stale_shipped.py`

**Interfaces:**
- Consumes: `Lattice`, `Node` from `model`; `check_lattice` from `check`; `impact`, `expand_targets` from `impact`; `Ticket`, `Finding` from `tickets`; `Severity`, `BlockedReason` from `constants`.
- Produces:
  - `build_audit_trigger(lattice: Lattice, target: str | None) -> dict[str, tuple[str, ...]]`.
  - `build_from_trigger(lattice: Lattice, from_id: str) -> dict[str, tuple[str, ...]]`.
  - `stale_shipped(lattice: Lattice, trigger: Mapping[str, tuple[str, ...]], tickets: Mapping[str, Ticket], rejected: Mapping[str, BlockedReason]) -> list[Finding]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stale_shipped.py`:

```python
"""Tests for the pure stale-shipped join and trigger builders."""

from pathlib import Path

import pytest

from doc_lattice.loader import build_lattice
from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge
from doc_lattice.stale_shipped import build_audit_trigger, build_from_trigger, stale_shipped
from doc_lattice.tickets import Ticket, TicketState


def _ticket(identifier: str, state_type: str) -> Ticket:
    return Ticket(
        identifier=identifier,
        title="t",
        url="https://x/" + identifier,
        state=TicketState(name=state_type, type=state_type),
        parent=None,
        children=(),
    )


def _node(id_: str, body: str, *, derives=None, tickets=()) -> ParsedDoc:
    meta = NodeMeta(
        id=id_,
        derives_from=[RawEdge(ref=r, seen=s) for r, s in (derives or [])],
        tickets=list(tickets),
    )
    return ParsedDoc(path=Path(f"docs/{id_}.md"), meta=meta, body=body)


def _two_node_lattice(seen: str | None, tickets=("PC-1",)):
    up = _node("up", "# Up {#sec}\nbody v2\n")
    down = _node("down", "# Down\nb\n", derives=[("up#sec", seen)], tickets=tickets)
    return build_lattice([up, down])


@pytest.mark.parametrize(
    "state_type,severity",
    [
        ("completed", "DANGER"),
        ("started", "WARNING"),
        ("unstarted", "INFO"),
        ("backlog", "INFO"),
    ],
)
def test_grading_by_state_type(state_type, severity):
    lattice = _two_node_lattice(seen="staleseenstaleseenstaleseenstale")
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", state_type)}, {})
    assert [f.severity for f in findings] == [severity]


@pytest.mark.parametrize("state_type", ["canceled", "triage"])
def test_terminal_states_omitted(state_type):
    lattice = _two_node_lattice(seen="staleseenstaleseenstaleseenstale")
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", state_type)}, {})
    assert findings == []


def test_unresolved_is_blocked_not_found():
    lattice = _two_node_lattice(seen="staleseenstaleseenstaleseenstale")
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {}, {})
    assert findings[0].severity == "BLOCKED"
    assert findings[0].reason == "not-found"


def test_rejected_reason_is_carried():
    lattice = _two_node_lattice(seen="staleseenstaleseenstaleseenstale", tickets=("SEC-9",))
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {}, {"SEC-9": "cross-team"})
    assert findings[0].reason == "cross-team"


def test_node_with_no_tickets_yields_nothing():
    lattice = _two_node_lattice(seen="staleseenstaleseenstaleseenstale", tickets=())
    trigger = build_audit_trigger(lattice, None)
    assert stale_shipped(lattice, trigger, {}, {}) == []


def test_duplicate_ref_collapses():
    lattice = _two_node_lattice(seen="staleseenstaleseenstaleseenstale", tickets=("PC-1", "PC-1"))
    trigger = build_audit_trigger(lattice, None)
    findings = stale_shipped(lattice, trigger, {"PC-1": _ticket("PC-1", "completed")}, {})
    assert len(findings) == 1


def test_ok_edge_is_not_a_trigger():
    # seen=None is UNRECONCILED, not STALE, so it is not a trigger in audit mode.
    up = _node("up", "# Up {#sec}\nbody\n")
    down = _node("down", "# Down\nb\n", derives=[("up#sec", None)], tickets=("PC-1",))
    lattice = build_lattice([up, down])
    trigger = build_audit_trigger(lattice, None)
    assert trigger == {}


def test_from_mode_transitive_dependent_has_refs():
    # up <- mid <- leaf. A change to up must give leaf non-empty drifted_refs.
    up = _node("up", "# Up {#sec}\nbody\n")
    mid = _node("mid", "# Mid {#midsec}\nbody\n", derives=[("up#sec", None)])
    leaf = _node("leaf", "# Leaf\nb\n", derives=[("mid#midsec", None)], tickets=("PC-1",))
    lattice = build_lattice([up, mid, leaf])
    trigger = build_from_trigger(lattice, "up#sec")
    assert "leaf" in trigger
    assert trigger["leaf"]  # non-empty justifying refs


def test_ordering_is_severity_then_node_then_ref():
    up = _node("up", "# Up {#sec}\nbody v2\n")
    a = _node("a", "# A\nb\n", derives=[("up#sec", "staleseenstaleseenstaleseenstale")], tickets=("PC-2", "PC-1"))
    lattice = build_lattice([up, a])
    trigger = build_audit_trigger(lattice, None)
    tickets = {"PC-1": _ticket("PC-1", "completed"), "PC-2": _ticket("PC-2", "started")}
    findings = stale_shipped(lattice, trigger, tickets, {})
    # DANGER (PC-1) sorts before WARNING (PC-2).
    assert [(f.severity, f.ticket_ref) for f in findings] == [("DANGER", "PC-1"), ("WARNING", "PC-2")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_stale_shipped.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the join**

Create `src/doc_lattice/stale_shipped.py`:

```python
"""Pure join: grade a trigger node's tickets into ordered stale-shipped findings."""

from collections.abc import Mapping

from .check import check_lattice
from .constants import BlockedReason, Severity
from .impact import expand_targets, impact
from .model import Lattice
from .tickets import Finding, Ticket

_STATE_SEVERITY: dict[str, Severity] = {
    "completed": "DANGER",
    "started": "WARNING",
    "unstarted": "INFO",
    "backlog": "INFO",
}
_SEVERITY_RANK: dict[Severity, int] = {"DANGER": 0, "BLOCKED": 1, "WARNING": 2, "INFO": 3}


def build_audit_trigger(lattice: Lattice, target: str | None) -> dict[str, tuple[str, ...]]:
    """Map each currently-STALE node to its stale upstream refs.

    Args:
        lattice: The built lattice.
        target: An optional id; when given, the trigger is narrowed to STALE nodes that also
            fall in the impact set of ``target``.

    Returns:
        A map of downstream node id to the tuple of its STALE ``target_ref`` values.

    Raises:
        ValidationError: If ``target`` is given but resolves to no id.
    """
    grouped: dict[str, list[str]] = {}
    for status in check_lattice(lattice):
        if status.state == "STALE":
            grouped.setdefault(status.source_id, []).append(status.target_ref)
    trigger = {node_id: tuple(refs) for node_id, refs in grouped.items()}
    if target is not None:
        affected = {node.id for node in impact(lattice, target)}
        trigger = {node_id: refs for node_id, refs in trigger.items() if node_id in affected}
    return trigger


def build_from_trigger(lattice: Lattice, from_id: str) -> dict[str, tuple[str, ...]]:
    """Map each node downstream of ``from_id`` to the refs that connect it to the change.

    Args:
        lattice: The built lattice.
        from_id: The id about to change.

    Returns:
        A map of affected node id to the tuple of its ``target_ref`` values whose resolved
        target lies in the transitive impacted-id closure of ``from_id``.

    Raises:
        ValidationError: If ``from_id`` resolves to no id.
    """
    affected = impact(lattice, from_id)
    closure: set[str] = set(expand_targets(lattice, from_id))
    for node in affected:
        closure.add(node.id)
        closure |= lattice.anchors_by_path.get(node.path, frozenset())
    trigger: dict[str, tuple[str, ...]] = {}
    for node in affected:
        refs = tuple(
            edge.target_ref for edge in node.derives_from if edge.target_id in closure
        )
        if refs:
            trigger[node.id] = refs
    return trigger


def stale_shipped(
    lattice: Lattice,
    trigger: Mapping[str, tuple[str, ...]],
    tickets: Mapping[str, Ticket],
    rejected: Mapping[str, BlockedReason],
) -> list[Finding]:
    """Grade each trigger node's tickets into deterministically ordered findings.

    Args:
        lattice: The built lattice.
        trigger: Node id to its justifying drifted refs.
        tickets: Resolved tickets keyed by queried identifier.
        rejected: Refs refused before fetch, mapped to ``"malformed"`` or ``"cross-team"``.

    Returns:
        Findings ordered by severity rank (DANGER, BLOCKED, WARNING, INFO), then node id,
        then ticket ref. A node with no tickets, or only terminal-state tickets, yields none.
    """
    findings: list[Finding] = []
    for node_id, drifted_refs in trigger.items():
        node = lattice.nodes_by_id[node_id]
        seen_refs: set[str] = set()
        for ref in node.tickets:
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            ticket = tickets.get(ref)
            if ticket is not None:
                severity = _STATE_SEVERITY.get(ticket.state.type)
                if severity is None:
                    continue
                findings.append(
                    Finding(
                        severity=severity,
                        node_id=node_id,
                        node_title=node.title,
                        node_path=node.path,
                        drifted_refs=drifted_refs,
                        ticket_ref=ref,
                        reason=None,
                        ticket=ticket,
                    )
                )
            else:
                findings.append(
                    Finding(
                        severity="BLOCKED",
                        node_id=node_id,
                        node_title=node.title,
                        node_path=node.path,
                        drifted_refs=drifted_refs,
                        ticket_ref=ref,
                        reason=rejected.get(ref, "not-found"),
                        ticket=None,
                    )
                )
    findings.sort(key=lambda f: (_SEVERITY_RANK[f.severity], f.node_id, f.ticket_ref))
    return findings
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_stale_shipped.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/stale_shipped.py tests/test_stale_shipped.py
git commit -m "feat: add stale-shipped join and trigger builders"
```

---

## Task 9: Renderer

**Files:**
- Create: `src/doc_lattice/linear_render.py`
- Test: `tests/test_linear_render.py`

**Interfaces:**
- Consumes: `Finding` from `tickets`; `strip_control_chars` from `text_utils`; `rich.console.Console`, `rich.markup.escape`.
- Produces:
  - `render_safe(text: str) -> str`: `escape(strip_control_chars(text))`.
  - `findings_json(findings: Sequence[Finding]) -> dict`: the `--json` payload per spec 4.1.
  - `render_findings(console: Console, findings: Sequence[Finding]) -> None`: the severity-grouped human table.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linear_render.py`:

```python
"""Tests for the linear renderer."""

import io
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from rich.console import Console

from doc_lattice.linear_render import findings_json, render_findings, render_safe
from doc_lattice.tickets import Finding, Ticket, TicketState


def _ticket():
    return Ticket(
        identifier="PC-1",
        title="Accent",
        url="https://x/PC-1",
        state=TicketState(name="Done", type="completed"),
        parent=None,
        children=(),
    )


def _danger():
    return Finding(
        severity="DANGER",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#accent",),
        ticket_ref="PC-1",
        reason=None,
        ticket=_ticket(),
    )


def _blocked():
    return Finding(
        severity="BLOCKED",
        node_id="pc-design",
        node_title="PC Design",
        node_path=Path("docs/pc-design.md"),
        drifted_refs=("art-direction#motion",),
        ticket_ref="PC-999",
        reason="not-found",
        ticket=None,
    )


def test_json_shape_for_graded_and_blocked():
    payload = findings_json([_danger(), _blocked()])
    assert list(payload) == ["findings"]
    danger, blocked = payload["findings"]
    assert danger["ticket"]["state"]["type"] == "completed"
    assert danger["ticket_ref"] == "PC-1"
    assert blocked["ticket"] is None
    assert blocked["reason"] == "not-found"


@given(st.text())
def test_render_safe_is_idempotent_and_control_free(text: str):
    once = render_safe(text)
    assert render_safe(once) == once
    assert all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in once)


def test_render_table_escapes_and_shows_severity():
    finding = Finding(
        severity="DANGER",
        node_id="node[/]",
        node_title=None,
        node_path=Path("docs/x.md"),
        drifted_refs=("ref\x1bx",),
        ticket_ref="PC-1",
        reason=None,
        ticket=_ticket(),
    )
    console = Console(file=io.StringIO(), width=200)
    render_findings(console, [finding])
    out = console.file.getvalue()
    assert "DANGER" in out
    assert "\x1b" not in out  # control byte stripped
    assert "node[/]" in out  # markup-escaped, rendered literally
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_linear_render.py -v --no-cov`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement the renderer**

Create `src/doc_lattice/linear_render.py`:

```python
"""Render stale-shipped findings as a severity-grouped table or a JSON payload."""

from collections.abc import Sequence

from rich.console import Console
from rich.markup import escape

from .tickets import Finding, Ticket, TicketRef
from .text_utils import strip_control_chars


def render_safe(text: str) -> str:
    """Make any external string safe to print: strip control bytes, then escape markup.

    Args:
        text: A string from a repo or a Linear response.

    Returns:
        The string with control bytes removed and rich markup escaped.
    """
    return escape(strip_control_chars(text))


def _ref_json(ref: TicketRef) -> dict:
    return {
        "identifier": ref.identifier,
        "title": ref.title,
        "state": {"name": ref.state.name, "type": ref.state.type},
    }


def _ticket_json(ticket: Ticket) -> dict:
    return {
        "identifier": ticket.identifier,
        "title": ticket.title,
        "url": ticket.url,
        "state": {"name": ticket.state.name, "type": ticket.state.type},
        "parent": _ref_json(ticket.parent) if ticket.parent is not None else None,
        "children": [_ref_json(child) for child in ticket.children],
    }


def findings_json(findings: Sequence[Finding]) -> dict:
    """Build the ``--json`` payload.

    Args:
        findings: The ordered findings.

    Returns:
        An object with a single ``findings`` key, each entry matching the spec 4.1 shape.
    """
    return {
        "findings": [
            {
                "severity": finding.severity,
                "node_id": finding.node_id,
                "node_title": finding.node_title,
                "node_path": str(finding.node_path),
                "drifted_refs": list(finding.drifted_refs),
                "ticket_ref": finding.ticket_ref,
                "reason": finding.reason,
                "ticket": _ticket_json(finding.ticket) if finding.ticket is not None else None,
            }
            for finding in findings
        ]
    }


def render_findings(console: Console, findings: Sequence[Finding]) -> None:
    """Print the findings grouped by severity, escaping every external string.

    Args:
        console: The output console.
        findings: The ordered findings.
    """
    if not findings:
        console.print("no stale-shipped findings")
        return
    colors = {"DANGER": "red", "BLOCKED": "magenta", "WARNING": "yellow", "INFO": "cyan"}
    for finding in findings:
        color = colors[finding.severity]
        refs = ", ".join(render_safe(ref) for ref in finding.drifted_refs)
        if finding.ticket is not None:
            detail = f"{render_safe(finding.ticket_ref)} [{render_safe(finding.ticket.state.name)}]"
        else:
            detail = f"{render_safe(finding.ticket_ref)} ({finding.reason})"
        console.print(
            f"[{color}]{finding.severity:<8}[/{color}] "
            f"{render_safe(finding.node_id)}  {detail}  drift: {refs}"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_linear_render.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/linear_render.py tests/test_linear_render.py
git commit -m "feat: add linear findings renderer"
```

---

## Task 10: The `linear` CLI command

**Files:**
- Modify: `src/doc_lattice/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config` from `config`; `load_lattice` from `orchestrate`; `build_audit_trigger`, `build_from_trigger`, `stale_shipped` from `stale_shipped`; `fetch_tickets` from `linear_fetch`; `findings_json`, `render_findings` from `linear_render`; existing `ProjectError`, `_out`, `_err`.
- Produces: a `linear` typer command. Exit 0 by default (even with findings); exit 1 under `--exit-code` when any DANGER or BLOCKED finding exists (or WARNING too under `--warn-exit`); exit 2 on any `ProjectError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def _fake_fetch(tickets):
    def fetch(_identifiers, _team, client=None):
        return tickets, {}

    return fetch


def test_linear_audit_json_reports_danger(lattice_dir, monkeypatch):
    from doc_lattice.tickets import Ticket, TicketState

    ticket = Ticket(
        identifier="PC-228", title="t", url="https://x/PC-228",
        state=TicketState(name="Done", type="completed"), parent=None, children=(),
    )
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    danger = [f for f in payload["findings"] if f["severity"] == "DANGER"]
    assert danger and danger[0]["ticket_ref"] == "PC-228"


def test_linear_exit_code_gates_on_danger(lattice_dir, monkeypatch):
    from doc_lattice.tickets import Ticket, TicketState

    ticket = Ticket(
        identifier="PC-228", title="t", url="https://x/PC-228",
        state=TicketState(name="Done", type="completed"), parent=None, children=(),
    )
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({"PC-228": ticket}))
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["linear"]).exit_code == 0
    assert runner.invoke(app, ["linear", "--exit-code"]).exit_code == 1


def test_linear_blocked_ticket_fails_gate(lattice_dir, monkeypatch):
    # The completed ticket is replaced by a typo: gate must still fail (fail-closed).
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--exit-code", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["findings"][0]["severity"] == "BLOCKED"


def test_linear_no_tickets_needs_no_key(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A {#s}\nb\n", encoding="utf-8")
    (docs / "b.md").write_text(
        "---\nid: b\nderives_from:\n  - ref: a#s\n    seen: staleseenstaleseenstaleseenstale\n"
        "---\n# B\nb\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["linear", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []


def test_linear_from_and_target_conflict_exits_2(lattice_dir, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "accent", "--from", "accent"])
    assert result.exit_code == 2


def test_linear_unknown_from_exits_2(lattice_dir, monkeypatch):
    monkeypatch.setattr(cli_mod, "fetch_tickets", _fake_fetch({}))
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["linear", "--from", "nonexistent"])
    assert result.exit_code == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cli.py -k linear -v --no-cov`
Expected: FAIL (the `linear` command does not exist).

- [ ] **Step 3: Implement the command**

Add these imports near the other `from .` imports at the top of `src/doc_lattice/cli.py`:

```python
from .linear_fetch import fetch_tickets
from .linear_render import findings_json, render_findings
from .stale_shipped import build_audit_trigger, build_from_trigger, stale_shipped
```

Add the command (place it after the `graph` command, before `_atomic_write`):

```python
@app.command()
def linear(
    target: Annotated[str, typer.Argument(help="Narrow the audit to this id's subtree.")] = "",
    from_id: Annotated[
        str | None, typer.Option("--from", help="Forward-looking: impact-walk from this id.")
    ] = None,
    exit_code: Annotated[
        bool, typer.Option("--exit-code", help="Exit 1 on any DANGER or BLOCKED finding.")
    ] = False,
    warn_exit: Annotated[
        bool, typer.Option("--warn-exit", help="With --exit-code, also exit 1 on WARNING.")
    ] = False,
    config: ConfigOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Report tickets shipped against a spec that has since drifted."""
    if from_id is not None and target:
        _err.print("[red]error[/red]: pass a positional target or --from, not both")
        raise typer.Exit(2)
    try:
        project = load_config(config, Path.cwd())
        lattice = load_lattice(project)
        if from_id is not None:
            trigger = build_from_trigger(lattice, from_id)
        else:
            trigger = build_audit_trigger(lattice, target or None)
        refs = {ref for node_id in trigger for ref in lattice.nodes_by_id[node_id].tickets}
        tickets, rejected = fetch_tickets(refs, project.config.linear_team)
        findings = stale_shipped(lattice, trigger, tickets, rejected)
    except ProjectError as exc:
        _err.print(f"[red]error[/red]: {exc} ({exc.code})")
        raise typer.Exit(2) from exc
    if json_out:
        typer.echo(json.dumps(findings_json(findings)))
    else:
        render_findings(_out, findings)
    if exit_code:
        gate = {"DANGER", "BLOCKED"} | ({"WARNING"} if warn_exit else set())
        if any(finding.severity in gate for finding in findings):
            raise typer.Exit(1)
    raise typer.Exit(0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cli.py -k linear -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/cli.py tests/test_cli.py
git commit -m "feat: add linear command wiring stale-shipped report"
```

---

## Task 11: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full suite with coverage**

Run: `uv run --group dev pytest`
Expected: PASS, coverage `>=80%`, `test_conventions.py` green.

- [ ] **Step 2: Run lint, format check, type check, and the boundary check**

Run:
```bash
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
```
Expected: all pass. If ruff-format reports changes, run `uv run --group dev ruff format src tests`, re-stage, and continue.

- [ ] **Step 3: Smoke-test the CLI help**

Run: `uv run doc-lattice linear --help`
Expected: the command's help, listing `--from`, `--exit-code`, `--warn-exit`, `--json`, `--config`.

- [ ] **Step 4: Commit any formatting fixups**

```bash
git add -A
git commit -m "chore: lint and format fixups for linear slice" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:** Every spec section maps to a task. Scope check (1) the determinism boundary holds because `check`/`impact`/`graph`/`reconcile` are untouched and `stale_shipped` only reads them (Task 8). Section 3 command surface and exit codes are Task 10. Section 4 data model is Task 3, the 4.1 JSON shape is Task 9. Section 5 join, ordering, and both trigger modes are Task 8. Section 6 fetch, query, transport, and parser are Tasks 4 through 7. Section 7 team scoping (fail-closed cross-team) is in the partition (Task 4) and the join (Task 8). Section 8 error codes are Task 10. Section 9 security controls are spread across the partition (ASCII validation, team key, cap, no regex interpolation, Task 4), the transport (https-only, no-redirect, capped read, lazy key, Task 5), the parser and tickets (control stripping, Tasks 3 and 6), and the renderer (render_safe, Task 9). Section 10 testing is satisfied per task, with the property tests in Tasks 2 and 9 and the full-suite gate in Task 11. Section 11 dependencies stay unchanged (stdlib transport).

**Type consistency:** `fetch_tickets`, `partition_identifiers`, `build_query`, `parse_tickets`, `build_audit_trigger`, `build_from_trigger`, `stale_shipped`, `findings_json`, and `render_findings` use identical signatures wherever a later task consumes an earlier one. `Finding` carries `ticket_ref`, `reason`, and an optional `ticket` consistently across Tasks 3, 8, 9, and 10. The ticket map is keyed by the queried identifier in both the parser (Task 6) and the join's lookup (Task 8).
