# Cross-command Marker Taint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse a CI `run:` body when authored fragments can compose
`doc[-_.]+lattice` along a modeled content flow and that content reaches an execution sink, while
preserving every phase-1 invocation and refusal outcome.

**Architecture:** Add a pure `shell_taint.py` module that owns symbolic content, the finite
suffix-aware DFA domain, fixed-point flow tables, stream aggregation, descriptor replay, source
selection, sink classification, caps, and the final verdict. Keep `shell_scanner.py` responsible
for parsing only: it attaches immutable typed evidence to monotonic command and scope IDs, reuses
the existing launcher grammar to identify the effective executable, and invokes the taint pass
once after the top-level scan.

**Tech Stack:** Python 3.13/3.14, frozen dataclasses, enums, `re`, `pathlib.PurePosixPath`, pytest,
Hypothesis-free table tests, Bash runtime probes, uv, Ruff, ty

---

## Governing references and file map

- Design authority:
  `docs/superpowers/specs/2026-07-24-cross-command-marker-taint-design.md`
- Issues: `https://github.com/Guardantix/doc-lattice/issues/110` and parent issue
  `https://github.com/Guardantix/doc-lattice/issues/106`
- Baseline: `main` at `93a9ee3`; implementation branch
  `feat/issue-110-cross-command-marker-taint`
- Create: `src/doc_lattice/github_ci/shell_taint.py`
  - Owns content and output expressions, evidence records, DFA transfer summaries, fixed-point
    evaluation, flow tables, descriptor replay, shell source selection, sink evaluation, caps, and
    verdicts.
- Create: `tests/test_github_ci_shell_taint.py`
  - Mirrors the pure module with synthetic evidence and low-cap exhaustion tests.
- Modify: `src/doc_lattice/github_ci/shell_scanner.py`
  - Builds word content, assigns command/scope IDs, records flow evidence, shares effective-head
    resolution with the finding path, and runs the taint pass.
- Modify: `tests/test_github_ci_shell_scanner.py`
  - Owns phase-2 parser integration, the real-Bash exploit fixtures, and phase-1 non-regression
    assertions.
- Modify: `tests/test_github_ci_audit.py`
  - Proves one `run:` body is the certification unit and file handoff fails closed through
    `audit.py`.
- Modify: `tests/cli/test_ci.py`
  - Proves the same handoff exits 2 through the public `ci audit` command.
- Modify: `README.md`
  - Publishes the step-local authored-marker contract and its absence-of-evidence limitations.
- Modify: `ARCHITECTURE.md`
  - Adds AD-18 with the durable boundary and algorithm decisions.
- Read only:
  `.worktrees/successor-evaluation/tests/fixtures/github_ci_successor_checkpoint/corpus/new_fixtures.json`
  - Supplies adversarial predecessor inputs for the final in-session battery.

Do not modify `CHANGELOG.md`, `src/doc_lattice/github_ci/audit.py`,
`tests/fixtures/github_ci_checkpoint/`, the successor-evaluation worktree, or any corpus artifact.
`ci audit` is still unreleased, and `audit.py` already scans each step independently.

## Locked implementation vocabulary

Use these names throughout the implementation and tests. This ledger prevents the scanner and pure
module from developing parallel representations.

```python
# src/doc_lattice/github_ci/shell_taint.py
TAINT_REFUSAL_REASON = "authored marker flow reaches an execution sink"

ContentExpr = (
    LiteralTransfer
    | VariableRef
    | StreamRef
    | ResourceRef
    | Choice
    | Concat
    | OutsideGap
)
OutputExpr = (
    CommandOutput
    | ScopeOutput
    | SequenceOutput
    | ChoiceOutput
    | RepeatOutput
)
RedirectionTarget = (
    StaticResourceTarget
    | DynamicResourceTarget
    | ContentTarget
    | ProcessResourceTarget
    | DescriptorTarget
    | NullTarget
)
```

The scanner imports the evidence and expression types; `shell_taint.py` must never import
`shell_scanner.py`. `ContentExpr` composes only inside one byte stream. `OutputExpr` describes how
command stdout values aggregate inside one stream scope. Descriptor and ephemeral-resource records
route values between those two layers without pretending filenames are file contents.

### Task 1: Build the symbolic content and suffix-aware DFA domain

**Files:**

- Create: `src/doc_lattice/github_ci/shell_taint.py`
- Create: `tests/test_github_ci_shell_taint.py`

- [ ] **Step 1: Write the closed-expression domain tests**

Create `tests/test_github_ci_shell_taint.py` with:

```python
"""Tests for pure authored-marker shell taint analysis."""

import pytest

from doc_lattice.github_ci.shell_taint import (
    Choice,
    Concat,
    LiteralTransfer,
    OutsideGap,
    _evaluate_closed,
    _marker_capable,
    _strip_trailing_newlines,
)


def _can_mark(expression, *, strip: bool = False) -> bool:
    value = _evaluate_closed(expression)
    if strip:
        value = _strip_trailing_newlines(value)
    return _marker_capable(value)


def test_concat_threads_dfa_state_across_fragment_boundaries():
    expression = Concat((LiteralTransfer("doc-"), LiteralTransfer("lattice reconcile")))

    assert _can_mark(expression) is True


def test_choice_joins_alternatives_without_concatenating_them():
    expression = Choice((LiteralTransfer("doc-"), LiteralTransfer("lattice")))

    assert _can_mark(expression) is False


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            Concat(
                (
                    LiteralTransfer("doc-"),
                    OutsideGap(),
                    LiteralTransfer("lattice"),
                )
            ),
            True,
        ),
        (
            Concat(
                (
                    LiteralTransfer("doc"),
                    OutsideGap(),
                    LiteralTransfer("lattice"),
                )
            ),
            False,
        ),
    ],
    ids=["authored-separator", "external-separator"],
)
def test_outside_gap_offers_epsilon_and_non_authored_barrier(expression, expected):
    assert _can_mark(expression) is expected


def test_command_substitution_strips_only_trailing_newlines():
    expression = Concat(
        (
            LiteralTransfer("doc-\n"),
            LiteralTransfer("lattice"),
        )
    )
    substituted = Concat(
        (
            LiteralTransfer("doc-\n"),
        )
    )

    assert _can_mark(expression) is False
    assert _can_mark(
        Concat(
            (
                LiteralTransfer("prefix"),
                LiteralTransfer("doc-\n"),
            )
        )
    ) is False
    assert _marker_capable(
        _evaluate_closed(
            Concat(
                (
                    LiteralTransfer(""),
                    LiteralTransfer("lattice"),
                )
            )
        )
    ) is False
    assert _marker_capable(
        _evaluate_closed(LiteralTransfer("doc-\nlattice"))
    ) is False
    assert _marker_capable(
        _evaluate_closed(LiteralTransfer("doc-lattice"))
    ) is True
    assert _marker_capable(
        _evaluate_closed(
            Concat(
                (
                    LiteralTransfer("doc-"),
                    LiteralTransfer("lattice"),
                )
            )
        )
    ) is True
    stripped = _strip_trailing_newlines(_evaluate_closed(substituted))
    assert _marker_capable(
        {
            left.compose(right)
            for left in stripped
            for right in _evaluate_closed(LiteralTransfer("lattice"))
        }
    ) is True
```

The final assertion pins strip-then-concat. The raw `doc-\n` transfer must not match, but its
command-substitution summary must expose `doc-` before the following authored `lattice`.

- [ ] **Step 2: Run the new domain tests and confirm the module is red**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_taint.py -v
```

Expected: collection fails with `ModuleNotFoundError` for
`doc_lattice.github_ci.shell_taint`.

- [ ] **Step 3: Add the symbolic expression records and normalization helpers**

Create `src/doc_lattice/github_ci/shell_taint.py` with this opening:

```python
"""Pure authored-marker taint analysis for one CI shell run body."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

TAINT_REFUSAL_REASON = "authored marker flow reaches an execution sink"


@dataclass(frozen=True, slots=True)
class LiteralTransfer:
    """Authored literal bytes transferred into one content stream."""

    text: str


@dataclass(frozen=True, slots=True)
class VariableRef:
    """Reference one shell variable's joined authored definitions."""

    name: str


@dataclass(frozen=True, slots=True)
class StreamRef:
    """Reference one stream scope's aggregated stdout."""

    scope_id: int


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Reference one normalized static resource's content."""

    key: str


@dataclass(frozen=True, slots=True)
class Choice:
    """Mutually exclusive in-word or control-flow alternatives."""

    parts: tuple[ContentExpr, ...]


@dataclass(frozen=True, slots=True)
class Concat:
    """Ordered adjacent fragments in one byte stream."""

    parts: tuple[ContentExpr, ...]


@dataclass(frozen=True, slots=True)
class OutsideGap:
    """An external-content boundary with epsilon and opaque-barrier alternatives."""


ContentExpr: TypeAlias = (
    LiteralTransfer
    | VariableRef
    | StreamRef
    | ResourceRef
    | Choice
    | Concat
    | OutsideGap
)


def concat(*parts: ContentExpr) -> ContentExpr:
    """Flatten concatenation and discard authored epsilon fragments."""
    flattened: list[ContentExpr] = []
    for part in parts:
        if isinstance(part, LiteralTransfer) and not part.text:
            continue
        if isinstance(part, Concat):
            flattened.extend(part.parts)
        else:
            flattened.append(part)
    if not flattened:
        return LiteralTransfer("")
    if len(flattened) == 1:
        return flattened[0]
    return Concat(tuple(flattened))


def choice(*parts: ContentExpr) -> ContentExpr:
    """Flatten choices while retaining epsilon as a real alternative."""
    flattened: list[ContentExpr] = []
    for part in parts:
        if isinstance(part, Choice):
            flattened.extend(part.parts)
        else:
            flattened.append(part)
    if len(flattened) == 1:
        return flattened[0]
    return Choice(tuple(flattened))
```

- [ ] **Step 4: Implement the fixed marker DFA transfer**

Append the following domain definitions. State `0` is the DFA start, states `1` through `10`
represent the longest suffix `d`, `do`, `doc`, `doc[-_.]+`, `...l`, through `...lattic`.
Accepting the final `e` records acceptance and returns to state `0`.

```python
_DFA_STATE_COUNT = 11
_DFA_START = 0
_SEPARATORS = frozenset("-_.")


def _ascii_lower(character: str) -> str:
    if "A" <= character <= "Z":
        return chr(ord(character) + ord("a") - ord("A"))
    return character


def _dfa_step(state: int, character: str) -> tuple[int, bool]:
    character = _ascii_lower(character)
    if state == 0:
        return (1, False) if character == "d" else (0, False)
    if state == 1:
        if character == "o":
            return 2, False
        return ((1, False) if character == "d" else (0, False))
    if state == 2:
        if character == "c":
            return 3, False
        return ((1, False) if character == "d" else (0, False))
    if state == 3:
        if character in _SEPARATORS:
            return 4, False
        return ((1, False) if character == "d" else (0, False))
    if state == 4:
        if character in _SEPARATORS:
            return 4, False
        if character == "l":
            return 5, False
        return ((1, False) if character == "d" else (0, False))
    expected = ("a", "t", "t", "i", "c", "e")[state - 5]
    if character == expected:
        if state == 10:
            return 0, True
        return state + 1, False
    return ((1, False) if character == "d" else (0, False))


@dataclass(frozen=True, slots=True)
class _DfaTransfer:
    """Deterministic exit and acceptance result for every possible entry state."""

    entries: tuple[tuple[int, bool], ...]

    @classmethod
    def identity(cls) -> _DfaTransfer:
        return cls(tuple((state, False) for state in range(_DFA_STATE_COUNT)))

    @classmethod
    def barrier(cls) -> _DfaTransfer:
        return cls(tuple((0, False) for _state in range(_DFA_STATE_COUNT)))

    @classmethod
    def literal(cls, text: str) -> _DfaTransfer:
        entries: list[tuple[int, bool]] = []
        for start in range(_DFA_STATE_COUNT):
            state = start
            accepted = False
            for character in text:
                state, crossed_accept = _dfa_step(state, character)
                accepted = accepted or crossed_accept
            entries.append((state, accepted))
        return cls(tuple(entries))

    def compose(self, following: _DfaTransfer) -> _DfaTransfer:
        entries: list[tuple[int, bool]] = []
        for intermediate, accepted_before in self.entries:
            final, accepted_after = following.entries[intermediate]
            entries.append((final, accepted_before or accepted_after))
        return _DfaTransfer(tuple(entries))
```

- [ ] **Step 5: Implement suffix-aware alternatives and closed evaluation**

Append:

```python
@dataclass(frozen=True, slots=True)
class _TransferSummary:
    """Raw and trailing-newline-stripped DFA effects for one alternative."""

    full: _DfaTransfer
    stripped: _DfaTransfer
    newline_only: bool

    @classmethod
    def literal(cls, text: str) -> _TransferSummary:
        return cls(
            full=_DfaTransfer.literal(text),
            stripped=_DfaTransfer.literal(text.rstrip("\n")),
            newline_only=all(character == "\n" for character in text),
        )

    @classmethod
    def barrier(cls) -> _TransferSummary:
        barrier = _DfaTransfer.barrier()
        return cls(full=barrier, stripped=barrier, newline_only=False)

    def compose(self, following: _TransferSummary) -> _TransferSummary:
        stripped = (
            self.stripped
            if following.newline_only
            else self.full.compose(following.stripped)
        )
        return _TransferSummary(
            full=self.full.compose(following.full),
            stripped=stripped,
            newline_only=self.newline_only and following.newline_only,
        )


_ContentValue: TypeAlias = frozenset[_TransferSummary]
_EPSILON = _TransferSummary.literal("")
_OUTSIDE_VALUE: _ContentValue = frozenset({_EPSILON, _TransferSummary.barrier()})


def _join_values(*values: _ContentValue) -> _ContentValue:
    return frozenset(alternative for value in values for alternative in value)


def _compose_values(left: _ContentValue, right: _ContentValue) -> _ContentValue:
    return frozenset(
        before.compose(after) for before in left for after in right
    )


def _strip_trailing_newlines(value: _ContentValue) -> _ContentValue:
    return frozenset(
        _TransferSummary(
            full=alternative.stripped,
            stripped=alternative.stripped,
            newline_only=alternative.newline_only,
        )
        for alternative in value
    )


def _marker_capable(value: _ContentValue) -> bool:
    return any(
        alternative.full.entries[_DFA_START][1] for alternative in value
    )


def _evaluate_closed(expression: ContentExpr) -> _ContentValue:
    if isinstance(expression, LiteralTransfer):
        return frozenset({_TransferSummary.literal(expression.text)})
    if isinstance(expression, OutsideGap):
        return _OUTSIDE_VALUE
    if isinstance(expression, Choice):
        return _join_values(*(_evaluate_closed(part) for part in expression.parts))
    if isinstance(expression, Concat):
        value: _ContentValue = frozenset({_EPSILON})
        for part in expression.parts:
            value = _compose_values(value, _evaluate_closed(part))
        return value
    raise ValueError("closed content expression contains an unresolved reference")
```

This representation is finite and hashable. Keeping `newline_only` per alternative is what makes
`Concat` able to strip through an all-newline suffix without losing the preceding suffix state.

- [ ] **Step 6: Run the domain tests**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_taint.py -v
uv run --no-sync ruff check src/doc_lattice/github_ci/shell_taint.py \
  tests/test_github_ci_shell_taint.py
```

Expected: every test passes and Ruff exits 0.

- [ ] **Step 7: Commit the content domain**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  tests/test_github_ci_shell_taint.py
git commit -m "feat: add shell marker transfer domain"
```

### Task 2: Add bounded reference resolution and fixed-point flow tables

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `tests/test_github_ci_shell_taint.py`

- [ ] **Step 1: Add variable, resource, stream, cycle, and cap tests**

Append to `tests/test_github_ci_shell_taint.py`:

```python
from doc_lattice.github_ci.shell_taint import (  # noqa: E402
    ResourceRef,
    StreamRef,
    TaintLimits,
    VariableRef,
    _FlowDefinitions,
    _FlowWrite,
    _TaintLimitExceeded,
    _solve_flow_definitions,
)


def test_variable_assignment_and_append_compose_in_the_fixed_point():
    definitions = _FlowDefinitions(
        variable_writes=(
            _FlowWrite("X", LiteralTransfer("doc-")),
            _FlowWrite("X", LiteralTransfer("lattice"), append=True),
        )
    )

    solved = _solve_flow_definitions(definitions)

    assert _marker_capable(solved.evaluate(VariableRef("X"))) is True


def test_competing_variable_definitions_join_without_composing():
    definitions = _FlowDefinitions(
        variable_writes=(
            _FlowWrite("X", LiteralTransfer("doc-")),
            _FlowWrite("X", LiteralTransfer("lattice")),
        )
    )

    solved = _solve_flow_definitions(definitions)

    assert _marker_capable(solved.evaluate(VariableRef("X"))) is False


def test_resource_append_and_stream_strip_resolve_through_typed_tables():
    definitions = _FlowDefinitions(
        resource_writes=(
            _FlowWrite("task.sh", LiteralTransfer("doc-")),
            _FlowWrite("task.sh", LiteralTransfer("lattice\n"), append=True),
        ),
        stream_writes=(
            _FlowWrite(
                7,
                Concat(
                    (
                        ResourceRef("task.sh"),
                        LiteralTransfer(""),
                    )
                ),
                strip_trailing_newlines=True,
            ),
        ),
    )

    solved = _solve_flow_definitions(definitions)

    assert _marker_capable(solved.evaluate(ResourceRef("task.sh"))) is True
    assert _marker_capable(solved.evaluate(StreamRef(7))) is True


def test_mutually_referential_variables_converge_by_least_fixed_point():
    definitions = _FlowDefinitions(
        variable_writes=(
            _FlowWrite("X", LiteralTransfer("doc-")),
            _FlowWrite("X", VariableRef("Y")),
            _FlowWrite("Y", VariableRef("X")),
        )
    )

    solved = _solve_flow_definitions(definitions)

    assert _marker_capable(
        solved.evaluate(
            Concat((VariableRef("Y"), LiteralTransfer("lattice")))
        )
    ) is True


@pytest.mark.parametrize(
    ("definitions", "limits", "message"),
    [
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite(
                        "X",
                        Choice(
                            (
                                LiteralTransfer("a"),
                                LiteralTransfer("b"),
                            )
                        ),
                    ),
                )
            ),
            TaintLimits(max_alternatives=1),
            "shell taint alternative limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite(
                        "X",
                        Concat(
                            (
                                LiteralTransfer("a"),
                                LiteralTransfer("b"),
                            )
                        ),
                    ),
                )
            ),
            TaintLimits(max_expression_nodes=2),
            "shell taint expression node limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", LiteralTransfer("a")),
                    _FlowWrite("Y", LiteralTransfer("b")),
                )
            ),
            TaintLimits(max_table_entries=1),
            "shell taint table entry limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", LiteralTransfer("a")),
                    _FlowWrite("Y", VariableRef("X")),
                )
            ),
            TaintLimits(max_edges=1),
            "shell taint edge limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", LiteralTransfer("a")),
                    _FlowWrite("Y", VariableRef("X")),
                )
            ),
            TaintLimits(max_fixed_point_updates=1),
            "shell taint fixed-point update limit exceeded",
        ),
    ],
    ids=["alternatives", "nodes", "tables", "edges", "updates"],
)
def test_every_flow_bound_fails_closed(definitions, limits, message):
    with pytest.raises(_TaintLimitExceeded, match=message):
        _solve_flow_definitions(definitions, limits=limits)
```

- [ ] **Step 2: Run the fixed-point tests and verify the missing API is red**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_taint.py -v
```

Expected: collection fails because `TaintLimits`, `_FlowDefinitions`, `_FlowWrite`, and the solver
do not exist.

- [ ] **Step 3: Add deterministic limits and normalized flow equations**

Append to `shell_taint.py`:

```python
@dataclass(frozen=True, slots=True)
class TaintLimits:
    """Deterministic caps for one taint pass."""

    max_alternatives: int = 256
    max_expression_nodes: int = 100_000
    max_table_entries: int = 10_000
    max_edges: int = 50_000
    max_fixed_point_updates: int = 100_000
    max_brace_expansions: int = 256
    max_brace_depth: int = 16


class _TaintLimitExceeded(RuntimeError):
    """A deterministic taint bound prevented certification."""


@dataclass(frozen=True, slots=True)
class _FlowWrite:
    key: str | int
    expression: ContentExpr
    append: bool = False
    strip_trailing_newlines: bool = False


@dataclass(frozen=True, slots=True)
class _FlowDefinitions:
    variable_writes: tuple[_FlowWrite, ...] = ()
    resource_writes: tuple[_FlowWrite, ...] = ()
    stream_writes: tuple[_FlowWrite, ...] = ()
```

- [ ] **Step 4: Add expression accounting and capped value operations**

Append:

```python
def _expression_nodes(expression: ContentExpr) -> int:
    if isinstance(expression, (Choice, Concat)):
        return 1 + sum(_expression_nodes(part) for part in expression.parts)
    return 1


def _expression_edges(expression: ContentExpr) -> int:
    if isinstance(expression, (VariableRef, ResourceRef, StreamRef)):
        return 1
    if isinstance(expression, (Choice, Concat)):
        return sum(_expression_edges(part) for part in expression.parts)
    return 0


def _cap_value(value: _ContentValue, limits: TaintLimits) -> _ContentValue:
    if len(value) > limits.max_alternatives:
        raise _TaintLimitExceeded("shell taint alternative limit exceeded")
    return value


def _definition_counts(definitions: _FlowDefinitions) -> tuple[int, int, int]:
    writes = (
        definitions.variable_writes
        + definitions.resource_writes
        + definitions.stream_writes
    )
    nodes = sum(_expression_nodes(write.expression) for write in writes)
    edges = len(writes) + sum(_expression_edges(write.expression) for write in writes)
    entries = len(
        {
            ("variable", write.key)
            for write in definitions.variable_writes
        }
        | {
            ("resource", write.key)
            for write in definitions.resource_writes
        }
        | {
            ("stream", write.key)
            for write in definitions.stream_writes
        }
    )
    return nodes, edges, entries
```

- [ ] **Step 5: Implement least-fixed-point reference resolution**

Append this solver. Missing references evaluate to `OUTSIDE`, which discloses external variables,
files, and streams without asserting that they are inert.

```python
@dataclass(frozen=True, slots=True)
class _SolvedFlow:
    variables: dict[str, _ContentValue]
    resources: dict[str, _ContentValue]
    streams: dict[int, _ContentValue]
    limits: TaintLimits

    def evaluate(self, expression: ContentExpr) -> _ContentValue:
        return _evaluate_with_tables(
            expression,
            self.variables,
            self.resources,
            self.streams,
            self.limits,
        )


def _evaluate_with_tables(
    expression: ContentExpr,
    variables: dict[str, _ContentValue],
    resources: dict[str, _ContentValue],
    streams: dict[int, _ContentValue],
    limits: TaintLimits,
) -> _ContentValue:
    if isinstance(expression, LiteralTransfer):
        return frozenset({_TransferSummary.literal(expression.text)})
    if isinstance(expression, OutsideGap):
        return _OUTSIDE_VALUE
    if isinstance(expression, VariableRef):
        return variables.get(expression.name, _OUTSIDE_VALUE)
    if isinstance(expression, ResourceRef):
        return resources.get(expression.key, _OUTSIDE_VALUE)
    if isinstance(expression, StreamRef):
        return streams.get(expression.scope_id, _OUTSIDE_VALUE)
    if isinstance(expression, Choice):
        return _cap_value(
            _join_values(
                *(
                    _evaluate_with_tables(
                        part,
                        variables,
                        resources,
                        streams,
                        limits,
                    )
                    for part in expression.parts
                )
            ),
            limits,
        )
    value: _ContentValue = frozenset({_EPSILON})
    for part in expression.parts:
        value = _cap_value(
            _compose_values(
                value,
                _evaluate_with_tables(
                    part,
                    variables,
                    resources,
                    streams,
                    limits,
                ),
            ),
            limits,
        )
    return value


def _solve_flow_definitions(
    definitions: _FlowDefinitions,
    *,
    limits: TaintLimits = TaintLimits(),
) -> _SolvedFlow:
    nodes, edges, entries = _definition_counts(definitions)
    if nodes > limits.max_expression_nodes:
        raise _TaintLimitExceeded("shell taint expression node limit exceeded")
    if edges > limits.max_edges:
        raise _TaintLimitExceeded("shell taint edge limit exceeded")
    if entries > limits.max_table_entries:
        raise _TaintLimitExceeded("shell taint table entry limit exceeded")

    variables: dict[str, _ContentValue] = {}
    resources: dict[str, _ContentValue] = {}
    streams: dict[int, _ContentValue] = {}
    updates = 0
    changed = True
    while changed:
        changed = False
        for writes, table in (
            (definitions.variable_writes, variables),
            (definitions.resource_writes, resources),
            (definitions.stream_writes, streams),
        ):
            for write in writes:
                value = _evaluate_with_tables(
                    write.expression,
                    variables,
                    resources,
                    streams,
                    limits,
                )
                if write.strip_trailing_newlines:
                    value = _strip_trailing_newlines(value)
                prior = table.get(write.key, frozenset())
                if write.append:
                    base = prior or _OUTSIDE_VALUE
                    value = _cap_value(_compose_values(base, value), limits)
                widened = _cap_value(_join_values(prior, value), limits)
                if widened == prior:
                    continue
                table[write.key] = widened
                updates += 1
                if updates > limits.max_fixed_point_updates:
                    raise _TaintLimitExceeded(
                        "shell taint fixed-point update limit exceeded"
                    )
                changed = True
    return _SolvedFlow(variables, resources, streams, limits)
```

Charge only successful lattice updates. Do not charge raw worklist pops or loop iterations, because
those depend on scheduling order.

- [ ] **Step 6: Run the fixed-point suite**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_taint.py -v
uv run --no-sync ruff check src/doc_lattice/github_ci/shell_taint.py \
  tests/test_github_ci_shell_taint.py
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 7: Commit bounded flow resolution**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  tests/test_github_ci_shell_taint.py
git commit -m "feat: solve bounded shell content flows"
```

### Task 3: Define typed command evidence, descriptor replay, scopes, and sinks

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `tests/test_github_ci_shell_taint.py`

- [ ] **Step 1: Add synthetic command, source-selection, and descriptor tests**

Append these imports and helpers to `tests/test_github_ci_shell_taint.py`:

```python
from doc_lattice.github_ci.shell_taint import (  # noqa: E402
    TAINT_REFUSAL_REASON,
    ChoiceOutput,
    CommandOutput,
    Concat,
    ContentTarget,
    DynamicResourceTarget,
    LiteralTransfer,
    NullTarget,
    OutsideGap,
    ResourceRef,
    SequenceOutput,
    StaticResourceTarget,
    _ArgPort,
    _AssignmentEvidence,
    _CommandEvidence,
    _ExecutableEvidence,
    _PipeEvidence,
    _RedirectionEvent,
    _ShellTaintEvidence,
    _StreamScopeEvidence,
    analyze_marker_taint,
)


def _arg(literal: str, expression=None, *, dynamic: bool = False) -> _ArgPort:
    return _ArgPort(
        literal=literal,
        content=expression or LiteralTransfer(literal),
        dynamic=dynamic,
    )


def _command(
    command_id: int,
    *argv: _ArgPort,
    name: str,
    head_index: int = 0,
    external_lookup: bool = False,
    assignments: tuple[_AssignmentEvidence, ...] = (),
    redirections: tuple[_RedirectionEvent, ...] = (),
) -> _CommandEvidence:
    return _CommandEvidence(
        command_id=command_id,
        output_scope_id=command_id,
        container_scope_id=100,
        argv=tuple(argv),
        assignments=assignments,
        redirections=redirections,
        executable=_ExecutableEvidence(
            argv_index=head_index,
            name=name,
            literal=argv[head_index].literal,
            external_lookup=external_lookup,
        ),
    )
```

Append the focused tests:

```python
def test_eval_sink_reads_joined_variable_definitions():
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("doc-")),
            _AssignmentEvidence("X", LiteralTransfer("lattice"), append=True),
        ),
    )

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
        True,
        TAINT_REFUSAL_REASON,
    )


def test_eval_inserts_spaces_between_argument_ports():
    command = _command(
        1,
        _arg("eval"),
        _arg("doc-"),
        _arg("lattice"),
        name="eval",
    )

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
        False,
        None,
    )


def test_external_lookup_cannot_reach_eval_builtin():
    command = _command(
        1,
        _arg("env"),
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        head_index=1,
        external_lookup=True,
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("doc-")),
            _AssignmentEvidence("X", LiteralTransfer("lattice"), append=True),
        ),
    )

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
        False,
        None,
    )


def test_shell_c_selects_payload_and_not_heredoc_stdin():
    command = _command(
        1,
        _arg("bash"),
        _arg("-c"),
        _arg("echo ok"),
        name="bash",
        redirections=(
            _RedirectionEvent(
                ordinal=0,
                operator="<<",
                descriptor=0,
                target=ContentTarget(LiteralTransfer("doc-lattice reconcile\n")),
            ),
        ),
    )

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
        False,
        None,
    )


def test_dynamic_shell_selector_checks_every_possible_code_port():
    command = _command(
        1,
        _arg("bash"),
        _arg("$OPT", OutsideGap(), dynamic=True),
        _arg(
            "$X",
            Concat((LiteralTransfer("doc-"), LiteralTransfer("lattice"))),
            dynamic=True,
        ),
        name="bash",
    )

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
        True,
        TAINT_REFUSAL_REASON,
    )


def test_file_write_and_script_operand_link_order_insensitively():
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-"),
        _arg("lattice reconcile"),
        name="printf",
        redirections=(
            _RedirectionEvent(
                0,
                ">",
                1,
                StaticResourceTarget("task.sh"),
            ),
        ),
    )
    reader = _command(
        2,
        _arg("bash"),
        _arg("./task.sh"),
        name="bash",
    )

    assert analyze_marker_taint(
        _ShellTaintEvidence(commands=(reader, writer))
    ) == (True, TAINT_REFUSAL_REASON)


def test_last_stdout_binding_wins_but_earlier_truncation_remains_empty():
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-"),
        _arg("lattice reconcile"),
        name="printf",
        redirections=(
            _RedirectionEvent(0, ">", 1, StaticResourceTarget("task.sh")),
            _RedirectionEvent(1, ">", 1, NullTarget()),
        ),
    )
    reader = _command(
        2,
        _arg("bash"),
        _arg("task.sh"),
        name="bash",
    )

    assert analyze_marker_taint(
        _ShellTaintEvidence(commands=(writer, reader))
    ) == (False, None)


def test_last_stdin_binding_overrides_marker_bearing_pipe():
    producer = _command(
        1,
        _arg("printf"),
        _arg("doc-"),
        _arg("lattice reconcile"),
        name="printf",
    )
    consumer = _command(
        2,
        _arg("bash"),
        name="bash",
        redirections=(
            _RedirectionEvent(
                0,
                "<<<",
                0,
                ContentTarget(LiteralTransfer("true\n")),
            ),
        ),
    )

    assert analyze_marker_taint(
        _ShellTaintEvidence(
            commands=(producer, consumer),
            pipes=(_PipeEvidence(1, 2),),
        )
    ) == (False, None)


def test_stream_scope_sequence_composes_but_choice_only_joins():
    first = _command(1, _arg("printf"), _arg("doc-"), name="printf")
    second = _command(2, _arg("printf"), _arg("lattice"), name="printf")
    sequence = _StreamScopeEvidence(
        scope_id=200,
        kind="command_substitution",
        parent_scope_id=100,
        parent_command_id=3,
        output=SequenceOutput((CommandOutput(1), CommandOutput(2))),
    )
    branch = _StreamScopeEvidence(
        scope_id=201,
        kind="command_substitution",
        parent_scope_id=100,
        parent_command_id=4,
        output=ChoiceOutput((CommandOutput(1), CommandOutput(2))),
    )
    sequence_eval = _command(
        3,
        _arg("eval"),
        _arg("$(...)", StreamRef(200), dynamic=True),
        name="eval",
    )
    choice_eval = _command(
        4,
        _arg("eval"),
        _arg("$(...)", StreamRef(201), dynamic=True),
        name="eval",
    )

    assert analyze_marker_taint(
        _ShellTaintEvidence(
            commands=(first, second, sequence_eval),
            scopes=(sequence,),
        )
    ) == (True, TAINT_REFUSAL_REASON)
    assert analyze_marker_taint(
        _ShellTaintEvidence(
            commands=(first, second, choice_eval),
            scopes=(branch,),
        )
    ) == (False, None)


def test_process_and_pipe_evidence_edges_charge_the_declared_cap():
    evidence = _ShellTaintEvidence(
        pipes=(
            _PipeEvidence(1, 2),
            _PipeEvidence(2, 3),
        )
    )

    assert analyze_marker_taint(
        evidence,
        limits=TaintLimits(max_edges=1),
    ) == (True, "shell taint edge limit exceeded")
```

- [ ] **Step 2: Run the evidence tests and verify the missing records are red**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_taint.py -v
```

Expected: collection fails on the first missing evidence import.

- [ ] **Step 3: Add the immutable port, redirect, output, and evidence records**

Append to `shell_taint.py`:

```python
from enum import Enum
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class _ArgPort:
    literal: str
    content: ContentExpr
    dynamic: bool = False
    process_resource_id: int | None = None


@dataclass(frozen=True, slots=True)
class _AssignmentEvidence:
    name: str
    content: ContentExpr
    append: bool = False


@dataclass(frozen=True, slots=True)
class _ExecutableEvidence:
    argv_index: int | None
    name: str | None
    literal: str | None
    external_lookup: bool = False
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class StaticResourceTarget:
    key: str


@dataclass(frozen=True, slots=True)
class DynamicResourceTarget:
    pass


@dataclass(frozen=True, slots=True)
class ContentTarget:
    content: ContentExpr


@dataclass(frozen=True, slots=True)
class ProcessResourceTarget:
    resource_id: int


@dataclass(frozen=True, slots=True)
class DescriptorTarget:
    descriptor: int


@dataclass(frozen=True, slots=True)
class NullTarget:
    pass


RedirectionTarget: TypeAlias = (
    StaticResourceTarget
    | DynamicResourceTarget
    | ContentTarget
    | ProcessResourceTarget
    | DescriptorTarget
    | NullTarget
)


@dataclass(frozen=True, slots=True)
class _RedirectionEvent:
    ordinal: int
    operator: str
    descriptor: int | None
    target: RedirectionTarget


@dataclass(frozen=True, slots=True)
class CommandOutput:
    command_id: int


@dataclass(frozen=True, slots=True)
class SequenceOutput:
    parts: tuple[OutputExpr, ...]


@dataclass(frozen=True, slots=True)
class ChoiceOutput:
    parts: tuple[OutputExpr, ...]


@dataclass(frozen=True, slots=True)
class RepeatOutput:
    part: OutputExpr


OutputExpr: TypeAlias = CommandOutput | SequenceOutput | ChoiceOutput | RepeatOutput


@dataclass(frozen=True, slots=True)
class _CommandEvidence:
    command_id: int
    output_scope_id: int
    container_scope_id: int
    argv: tuple[_ArgPort, ...]
    assignments: tuple[_AssignmentEvidence, ...]
    redirections: tuple[_RedirectionEvent, ...]
    executable: _ExecutableEvidence


@dataclass(frozen=True, slots=True)
class _StreamScopeEvidence:
    scope_id: int
    kind: str
    parent_scope_id: int | None
    parent_command_id: int | None
    output: OutputExpr
    redirections: tuple[_RedirectionEvent, ...] = ()
    loop_bindings: tuple[_AssignmentEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class _PipeEvidence:
    producer_scope_id: int
    consumer_command_id: int


@dataclass(frozen=True, slots=True)
class _ProcessResourceEvidence:
    resource_id: int
    scope_id: int
    direction: str


@dataclass(frozen=True, slots=True)
class _ShellTaintEvidence:
    commands: tuple[_CommandEvidence, ...] = ()
    scopes: tuple[_StreamScopeEvidence, ...] = ()
    pipes: tuple[_PipeEvidence, ...] = ()
    process_resources: tuple[_ProcessResourceEvidence, ...] = ()
```

Use strings only for scope kind and process direction because both are serialized evidence domains,
not behavior-bearing enums. Validate them against explicit frozensets in the analysis constructor.

- [ ] **Step 4: Add static resource normalization**

Append:

```python
def normalize_static_resource(literal: str, *, dynamic: bool) -> str | None:
    """Normalize the minimum task.sh/./task.sh equivalence."""
    if dynamic or not literal:
        return None
    normalized_literal = literal.replace("\\", "/")
    path = PurePosixPath(normalized_literal)
    if ".." in path.parts:
        return None
    absolute = normalized_literal.startswith("/")
    parts = tuple(part for part in path.parts if part not in ("", ".", "/"))
    if not parts:
        return "/" if absolute else "."
    prefix = "/" if absolute else ""
    return prefix + "/".join(parts)
```

Do not resolve `..`, current-directory changes, symlinks, or dynamic paths. A missing key is
external `OUTSIDE`, not a claim that the file is empty.

- [ ] **Step 5: Implement shell source selection**

Append:

```python
_SHELL_HEADS = frozenset(
    {"bash", "sh", "dash", "zsh", "ksh", "rbash", "rzsh", "rksh"}
)
_SHELL_LONG_OPTIONS_WITH_ARGUMENTS = frozenset(
    {"--rcfile", "--init-file", "--emulate"}
)
_SHELL_EAGER_STOP_OPTIONS = frozenset(
    {"--help", "--version", "--dump-strings", "--dump-po-strings"}
)


class _ShellSourceKind(Enum):
    NONE = "none"
    COMMAND = "command"
    SCRIPT = "script"
    STDIN = "stdin"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class _ShellSourceSelection:
    kind: _ShellSourceKind
    argv_index: int | None = None
    candidate_indices: tuple[int, ...] = ()
    include_stdin: bool = False


def _normalized_shell_head(name: str | None) -> str | None:
    if name is None:
        return None
    return name.casefold().removesuffix(".exe")


def _select_shell_source(
    argv: tuple[_ArgPort, ...],
    head_index: int,
) -> _ShellSourceSelection:
    index = head_index + 1
    stdin_selected = False
    while index < len(argv):
        word = argv[index]
        if word.dynamic:
            return _ShellSourceSelection(
                _ShellSourceKind.AMBIGUOUS,
                candidate_indices=tuple(range(index, len(argv))),
                include_stdin=True,
            )
        literal = word.literal
        if literal in _SHELL_EAGER_STOP_OPTIONS:
            return _ShellSourceSelection(_ShellSourceKind.NONE)
        if literal in {"-", "--"}:
            index += 1
            if stdin_selected or index >= len(argv):
                return _ShellSourceSelection(_ShellSourceKind.STDIN)
            return _ShellSourceSelection(_ShellSourceKind.SCRIPT, index)
        if not literal or literal[0] not in "-+":
            if stdin_selected:
                return _ShellSourceSelection(_ShellSourceKind.STDIN)
            return _ShellSourceSelection(_ShellSourceKind.SCRIPT, index)
        if literal.startswith("--"):
            consumes_value = literal in _SHELL_LONG_OPTIONS_WITH_ARGUMENTS
            if consumes_value:
                value_index = index + 1
                if value_index >= len(argv) or argv[value_index].dynamic:
                    return _ShellSourceSelection(
                        _ShellSourceKind.AMBIGUOUS,
                        candidate_indices=tuple(range(index, len(argv))),
                        include_stdin=True,
                    )
                index += 2
            else:
                index += 1
            continue
        cluster = literal[1:]
        if "c" in cluster:
            payload_index = index + 1
            if payload_index >= len(argv):
                return _ShellSourceSelection(_ShellSourceKind.NONE)
            return _ShellSourceSelection(_ShellSourceKind.COMMAND, payload_index)
        if "s" in cluster:
            stdin_selected = True
        consumes_value = bool(cluster) and cluster[-1] in "oO"
        if consumes_value:
            value_index = index + 1
            if value_index >= len(argv) or argv[value_index].dynamic:
                return _ShellSourceSelection(
                    _ShellSourceKind.AMBIGUOUS,
                    candidate_indices=tuple(range(index, len(argv))),
                    include_stdin=True,
                )
            index += 2
        else:
            index += 1
    return _ShellSourceSelection(_ShellSourceKind.STDIN)
```

This classifier selects a content port; it does not decide whether that port is marker-capable.
The ambiguous result deliberately returns every remaining argv port plus stdin.

- [ ] **Step 6: Implement ordered descriptor replay and producer stdout expressions**

Append:

```python
_INPUT_OPERATORS = frozenset({"<", "<<", "<<-", "<<<", "<&", "<>"})
_OUTPUT_OPERATORS = frozenset({">", ">|", ">>", ">&", "<>", "&>", "&>>"})
_APPEND_OPERATORS = frozenset({">>", "&>>"})


def _pipe_inputs(evidence: _ShellTaintEvidence) -> dict[int, ContentExpr]:
    return {
        edge.consumer_command_id: StreamRef(edge.producer_scope_id)
        for edge in evidence.pipes
    }


def _input_expression(
    command: _CommandEvidence,
    pipe_inputs: dict[int, ContentExpr],
    process_resources: dict[int, _ProcessResourceEvidence],
) -> ContentExpr:
    binding: ContentExpr = pipe_inputs.get(command.command_id, OutsideGap())
    for event in sorted(command.redirections, key=lambda item: item.ordinal):
        if event.descriptor != 0 or event.operator not in _INPUT_OPERATORS:
            continue
        target = event.target
        if isinstance(target, StaticResourceTarget):
            binding = ResourceRef(target.key)
        elif isinstance(target, ContentTarget):
            binding = target.content
        elif isinstance(target, ProcessResourceTarget):
            process = process_resources.get(target.resource_id)
            binding = (
                StreamRef(process.scope_id)
                if process is not None and process.direction == "input"
                else OutsideGap()
            )
        elif isinstance(target, NullTarget):
            binding = LiteralTransfer("")
        else:
            binding = OutsideGap()
    return binding


def _producer_stdout(
    command: _CommandEvidence,
    stdin: ContentExpr,
) -> ContentExpr:
    head_index = command.executable.argv_index
    payload_start = (head_index + 1) if head_index is not None else min(1, len(command.argv))
    argv_content = concat(
        *(port.content for port in command.argv[payload_start:])
    )
    return choice(OutsideGap(), argv_content, stdin)


def _static_write_definitions(
    events: tuple[_RedirectionEvent, ...],
    output: ContentExpr,
) -> tuple[_FlowWrite, ...]:
    writes: list[_FlowWrite] = []
    final_output: dict[int, _RedirectionEvent] = {}
    for event in sorted(events, key=lambda item: item.ordinal):
        if event.operator not in _OUTPUT_OPERATORS or event.descriptor is None:
            continue
        if isinstance(event.target, StaticResourceTarget):
            if event.operator not in _APPEND_OPERATORS:
                writes.append(
                    _FlowWrite(event.target.key, LiteralTransfer(""))
                )
            final_output[event.descriptor] = event
        elif isinstance(event.target, (NullTarget, DynamicResourceTarget)):
            final_output[event.descriptor] = event

    for descriptor, event in final_output.items():
        if not isinstance(event.target, StaticResourceTarget):
            continue
        routed = output if descriptor == 1 else OutsideGap()
        writes.append(
            _FlowWrite(
                event.target.key,
                routed,
                append=event.operator in _APPEND_OPERATORS,
            )
        )
    return tuple(writes)
```

Earlier truncating targets receive the authored empty-string side effect. Only the final descriptor
binding receives bytes, and only descriptor 1 carries the generic stdout expression.

- [ ] **Step 7: Lower stream output structure into flow equations**

Append:

```python
@dataclass(slots=True)
class _OutputLowering:
    command_scopes: dict[int, int]
    next_synthetic_scope: int = -1

    def lower(
        self,
        output: OutputExpr,
        stream_writes: list[_FlowWrite],
    ) -> ContentExpr:
        if isinstance(output, CommandOutput):
            return StreamRef(self.command_scopes[output.command_id])
        if isinstance(output, SequenceOutput):
            return concat(
                *(self.lower(part, stream_writes) for part in output.parts)
            )
        if isinstance(output, ChoiceOutput):
            return choice(
                *(self.lower(part, stream_writes) for part in output.parts)
            )
        repeated = self.lower(output.part, stream_writes)
        scope_id = self.next_synthetic_scope
        self.next_synthetic_scope -= 1
        stream_writes.extend(
            (
                _FlowWrite(scope_id, LiteralTransfer("")),
                _FlowWrite(
                    scope_id,
                    Concat((repeated, StreamRef(scope_id))),
                ),
            )
        )
        return StreamRef(scope_id)
```

`RepeatOutput` becomes a reflexive-transitive closure equation. The finite transfer domain and
alternative cap bound it.

- [ ] **Step 8: Build normalized definitions and evaluate execution sinks**

Append:

```python
def _eval_arguments(command: _CommandEvidence) -> ContentExpr:
    head_index = command.executable.argv_index
    if head_index is None:
        return LiteralTransfer("")
    arguments = command.argv[head_index + 1 :]
    parts: list[ContentExpr] = []
    for offset, argument in enumerate(arguments):
        if offset:
            parts.append(LiteralTransfer(" "))
        parts.append(argument.content)
    return concat(*parts)


def _script_port_expression(port: _ArgPort) -> ContentExpr:
    if port.process_resource_id is not None:
        return OutsideGap()
    key = normalize_static_resource(port.literal, dynamic=port.dynamic)
    return ResourceRef(key) if key is not None else OutsideGap()


def _sink_expressions(
    command: _CommandEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    executable = command.executable
    head_index = executable.argv_index
    if head_index is None or executable.name is None:
        return ()
    name = executable.name
    normalized_head = _normalized_shell_head(name)
    if name == "eval" and not executable.external_lookup:
        return (_eval_arguments(command),)
    if name in {"source", "."} and not executable.external_lookup:
        operand_index = head_index + 1
        if operand_index >= len(command.argv):
            return ()
        operand = command.argv[operand_index]
        if operand.process_resource_id is not None:
            process = process_resources.get(operand.process_resource_id)
            if process is not None and process.direction == "input":
                return (StreamRef(process.scope_id),)
        return (_script_port_expression(operand),)
    if normalized_head in _SHELL_HEADS:
        selection = _select_shell_source(command.argv, head_index)
        if selection.kind is _ShellSourceKind.NONE:
            return ()
        if selection.kind is _ShellSourceKind.STDIN:
            return (stdin,)
        if selection.kind in {_ShellSourceKind.COMMAND, _ShellSourceKind.SCRIPT}:
            assert selection.argv_index is not None
            port = command.argv[selection.argv_index]
            if selection.kind is _ShellSourceKind.COMMAND:
                return (port.content,)
            if port.process_resource_id is not None:
                process = process_resources.get(port.process_resource_id)
                if process is not None and process.direction == "input":
                    return (StreamRef(process.scope_id),)
            return (_script_port_expression(port),)
        candidates = tuple(
            command.argv[index].content
            for index in selection.candidate_indices
        )
        return candidates + ((stdin,) if selection.include_stdin else ())
    literal = executable.literal
    if literal is not None and (
        literal.startswith("/") or literal.startswith("./")
    ):
        key = normalize_static_resource(literal, dynamic=False)
        if key is not None:
            return (ResourceRef(key),)
    return ()


def _build_flow_definitions(
    evidence: _ShellTaintEvidence,
) -> tuple[_FlowDefinitions, dict[int, ContentExpr]]:
    commands = {command.command_id: command for command in evidence.commands}
    command_scopes = {
        command.command_id: command.output_scope_id for command in evidence.commands
    }
    process_resources = {
        resource.resource_id: resource for resource in evidence.process_resources
    }
    pipe_inputs = _pipe_inputs(evidence)
    inputs = {
        command.command_id: _input_expression(
            command,
            pipe_inputs,
            process_resources,
        )
        for command in evidence.commands
    }
    variable_writes = [
        _FlowWrite(write.name, write.content, append=write.append)
        for command in evidence.commands
        for write in command.assignments
    ]
    variable_writes.extend(
        _FlowWrite(write.name, write.content, append=write.append)
        for scope in evidence.scopes
        for write in scope.loop_bindings
    )
    stream_writes: list[_FlowWrite] = []
    resource_writes: list[_FlowWrite] = []
    for command in evidence.commands:
        output = _producer_stdout(command, inputs[command.command_id])
        stream_writes.append(_FlowWrite(command.output_scope_id, output))
        resource_writes.extend(
            _static_write_definitions(command.redirections, output)
        )

    lowering = _OutputLowering(command_scopes)
    for scope in evidence.scopes:
        output = lowering.lower(scope.output, stream_writes)
        stream_writes.append(
            _FlowWrite(
                scope.scope_id,
                output,
                strip_trailing_newlines=scope.kind == "command_substitution",
            )
        )
        resource_writes.extend(
            _static_write_definitions(
                scope.redirections,
                StreamRef(scope.scope_id),
            )
        )
    return (
        _FlowDefinitions(
            variable_writes=tuple(variable_writes),
            resource_writes=tuple(resource_writes),
            stream_writes=tuple(stream_writes),
        ),
        inputs,
    )


def analyze_marker_taint(
    evidence: _ShellTaintEvidence,
    *,
    limits: TaintLimits = TaintLimits(),
) -> tuple[bool, str | None]:
    """Return a fail-closed verdict for authored marker flow in one run body."""
    if any(
        scope.kind
        not in {
            "command",
            "command_substitution",
            "process_substitution",
            "subshell_group",
            "pipeline",
        }
        for scope in evidence.scopes
    ):
        return True, "shell taint stream scope cannot be structured"
    evidence_edges = (
        len(evidence.pipes)
        + len(evidence.process_resources)
        + sum(len(command.redirections) for command in evidence.commands)
        + sum(len(scope.redirections) for scope in evidence.scopes)
    )
    if evidence_edges > limits.max_edges:
        return True, "shell taint edge limit exceeded"
    evidence_entries = (
        len(evidence.commands)
        + len(evidence.scopes)
        + len(evidence.process_resources)
    )
    if evidence_entries > limits.max_table_entries:
        return True, "shell taint table entry limit exceeded"
    try:
        definitions, inputs = _build_flow_definitions(evidence)
        solved = _solve_flow_definitions(definitions, limits=limits)
    except _TaintLimitExceeded as error:
        return True, str(error)

    process_resources = {
        resource.resource_id: resource for resource in evidence.process_resources
    }
    for command in evidence.commands:
        for sink in _sink_expressions(
            command,
            inputs[command.command_id],
            process_resources,
        ):
            if _marker_capable(solved.evaluate(sink)):
                return True, TAINT_REFUSAL_REASON
    return False, None
```

Remove the unused local `commands` from `_build_flow_definitions` if Ruff flags it. Keep the
command-scope map, which is required for `CommandOutput` lowering.

- [ ] **Step 9: Run the complete pure suite**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_taint.py -v
uv run --no-sync ruff check src/doc_lattice/github_ci/shell_taint.py \
  tests/test_github_ci_shell_taint.py
uv run --no-sync ty check src/doc_lattice/github_ci/shell_taint.py
```

Expected: all tests pass and both static checks exit 0.

- [ ] **Step 10: Commit typed evidence and sink analysis**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  tests/test_github_ci_shell_taint.py
git commit -m "feat: analyze typed shell execution sinks"
```

### Task 4: Reuse the full launcher resolver for effective-head evidence

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_scanner.py:460-570`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1889-2660`
- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `tests/test_github_ci_shell_scanner.py`
- Test: `tests/test_github_ci_shell_taint.py`

- [ ] **Step 1: Add effective-head resolver tests**

Add `_ExecutableCandidate`, `_ShellWord`, and `_effective_executable_evidence` to the private imports
in `tests/test_github_ci_shell_scanner.py`. Add:

```python
def _static_word(literal: str, *, assignment: bool = False) -> _ShellWord:
    return _ShellWord(literal=literal, shell_assignment=assignment)


@pytest.mark.parametrize(
    ("words", "name", "external_lookup"),
    [
        (
            [_static_word("builtin"), _static_word("eval"), _static_word("$X")],
            "eval",
            False,
        ),
        (
            [_static_word("command"), _static_word("eval"), _static_word("$X")],
            "eval",
            False,
        ),
        (
            [_static_word("env"), _static_word("eval"), _static_word("$X")],
            "eval",
            True,
        ),
        (
            [_static_word("exec"), _static_word("eval"), _static_word("$X")],
            "eval",
            True,
        ),
        (
            [
                _static_word("uv"),
                _static_word("run"),
                _static_word("bash"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            "bash",
            True,
        ),
        (
            [
                _static_word("uvx"),
                _static_word("bash@5.2"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            "bash",
            True,
        ),
        (
            [_static_word("/bin/bash"), _static_word("-c"), _static_word("$X")],
            "bash",
            False,
        ),
        (
            [_static_word("./task.sh")],
            "task.sh",
            False,
        ),
    ],
    ids=[
        "builtin-eval",
        "command-eval",
        "env-eval",
        "exec-eval",
        "uv-run-shell",
        "uvx-versioned-shell",
        "path-shell",
        "direct-path",
    ],
)
def test_effective_executable_evidence_reuses_launcher_grammar(
    words,
    name,
    external_lookup,
):
    evidence = _effective_executable_evidence(words, _ScanBudget())

    assert evidence is not None
    assert evidence.name == name
    assert evidence.external_lookup is external_lookup
```

Add this regression beside it:

```python
def test_effective_executable_evidence_retains_ambiguous_candidates():
    words = [
        _ShellWord(literal="", dynamic=True, unquoted_dynamic=True),
        _static_word("bash"),
        _static_word("-c"),
        _static_word("$X"),
    ]

    evidence = _effective_executable_evidence(words, _ScanBudget())

    assert evidence is not None
    assert evidence.name == "bash"
    assert evidence.ambiguous is True
```

- [ ] **Step 2: Run the resolver tests and verify the helper is red**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_effective_executable_evidence_reuses_launcher_grammar \
  tests/test_github_ci_shell_scanner.py::test_effective_executable_evidence_retains_ambiguous_candidates \
  -v
```

Expected: collection fails because `_ExecutableCandidate` and
`_effective_executable_evidence` do not exist.

- [ ] **Step 3: Let one executable record carry ambiguous alternates**

In `shell_taint.py`, extend `_ExecutableEvidence` with the final field:

```python
    alternates: tuple[_ExecutableEvidence, ...] = ()
```

Replace `_sink_expressions` with a wrapper plus a single-candidate helper:

```python
def _sink_expressions(
    command: _CommandEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    expressions: list[ContentExpr] = []
    candidates = (
        command.executable,
        *command.executable.alternates,
    )
    for executable in candidates:
        expressions.extend(
            _candidate_sink_expressions(
                command,
                executable,
                stdin,
                process_resources,
            )
        )
    return tuple(expressions)


def _candidate_sink_expressions(
    command: _CommandEvidence,
    executable: _ExecutableEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    head_index = executable.argv_index
    if head_index is None or executable.name is None:
        return ()
    name = executable.name
    normalized_head = _normalized_shell_head(name)
    if (
        name == "eval"
        and executable.literal == "eval"
        and not executable.external_lookup
    ):
        return (_eval_arguments_from(command, executable),)
    if (
        name in {"source", "."}
        and executable.literal == name
        and not executable.external_lookup
    ):
        operand_index = head_index + 1
        if operand_index >= len(command.argv):
            return ()
        operand = command.argv[operand_index]
        if operand.process_resource_id is not None:
            process = process_resources.get(operand.process_resource_id)
            if process is not None and process.direction == "input":
                return (StreamRef(process.scope_id),)
        return (_script_port_expression(operand),)
    if normalized_head in _SHELL_HEADS:
        selection = _select_shell_source(command.argv, head_index)
        if selection.kind is _ShellSourceKind.NONE:
            return ()
        if selection.kind is _ShellSourceKind.STDIN:
            return (stdin,)
        if selection.kind in {_ShellSourceKind.COMMAND, _ShellSourceKind.SCRIPT}:
            assert selection.argv_index is not None
            port = command.argv[selection.argv_index]
            if selection.kind is _ShellSourceKind.COMMAND:
                return (port.content,)
            if port.process_resource_id is not None:
                process = process_resources.get(port.process_resource_id)
                if process is not None and process.direction == "input":
                    return (StreamRef(process.scope_id),)
            return (_script_port_expression(port),)
        candidates = tuple(
            command.argv[index].content
            for index in selection.candidate_indices
        )
        return candidates + ((stdin,) if selection.include_stdin else ())
    literal = executable.literal
    if literal is not None and (
        literal.startswith("/") or literal.startswith("./")
    ):
        key = normalize_static_resource(literal, dynamic=False)
        if key is not None:
            return (ResourceRef(key),)
    return ()
```

Rename `_eval_arguments` to `_eval_arguments_from` and make it use the supplied candidate:

```python
def _eval_arguments_from(
    command: _CommandEvidence,
    executable: _ExecutableEvidence,
) -> ContentExpr:
    head_index = executable.argv_index
    if head_index is None:
        return LiteralTransfer("")
    arguments = command.argv[head_index + 1 :]
    parts: list[ContentExpr] = []
    for offset, argument in enumerate(arguments):
        if offset:
            parts.append(LiteralTransfer(" "))
        parts.append(argument.content)
    return concat(*parts)
```

- [ ] **Step 4: Reintroduce candidate recording as resolver output, not policy**

In `shell_scanner.py`, add:

```python
@dataclass(frozen=True, slots=True)
class _ExecutableCandidate:
    """One effective executable position reached by the shared launcher grammar."""

    index: int
    uv_requirement: bool = False
    external_lookup: bool = False
    ambiguous: bool = False
```

Extend `_LauncherResolutionState`:

```python
    executable_positions: list[_ExecutableCandidate] = field(default_factory=list)

    def record_executable(
        self,
        index: int,
        *,
        uv_requirement: bool = False,
        external_lookup: bool = False,
        ambiguous: bool = False,
    ) -> None:
        candidate = _ExecutableCandidate(
            index=index,
            uv_requirement=uv_requirement,
            external_lookup=external_lookup,
            ambiguous=ambiguous,
        )
        if candidate not in self.executable_positions:
            self.executable_positions.append(candidate)
```

Candidate recording is evidence only. Do not restore the deleted dispatcher head sweep, opaque
tail, noexec walk, or dispatcher refusal.

- [ ] **Step 5: Thread provenance through the shell prefix resolver**

Change `_skip_shell_prefixes` to accept the shared resolution state:

```python
def _skip_shell_prefixes(
    words: list[_ShellWord],
    start: int,
    resolution: _LauncherResolutionState,
    *,
    inherited_external_lookup: bool = False,
) -> _ResolvedIndex:
```

Initialize `external_lookup = inherited_external_lookup`. Pass `resolution` and
`external_lookup` into `_skip_shell_builtin_wrapper` and `_skip_builtin_wrapper`. When
`_skip_builtin_wrapper` reaches a non-wrapper target, record the exact target before returning:

```python
    resolution.record_executable(
        index,
        external_lookup=external_lookup,
    )
    return _ResolvedIndex(None)
```

Update the two callers in `_doc_lattice_command_index` and
`_doc_lattice_command_after_prefixes`:

```python
command = _skip_shell_prefixes(words, start, resolution)
```

and:

```python
executable = _skip_shell_prefixes(words, start, resolution)
```

No other caller may invoke `_skip_shell_prefixes` without the active
`_LauncherResolutionState`.

- [ ] **Step 6: Record direct and launcher payload positions**

Add an `external_lookup` keyword to `_doc_lattice_payload_index`:

```python
def _doc_lattice_payload_index(
    words: list[_ShellWord],
    executable_index: int,
    resolution: _LauncherResolutionState,
    *,
    launcher_depth: int = 0,
    external_lookup: bool = False,
) -> _ResolvedIndex:
```

Immediately after `_reject_unsafe_executable_word(executable_word)`, record the direct candidate:

```python
    resolution.record_executable(
        executable_index,
        external_lookup=external_lookup,
    )
```

Pass `external_lookup=command.external_lookup` from
`_doc_lattice_command_index`, and
`external_lookup=executable.external_lookup` from
`_doc_lattice_command_after_prefixes`.

In `_nested_launcher_payload_index`, immediately after validating the static payload and computing
`raw_basename`, record the uv/external candidate:

```python
    resolution.record_executable(
        payload_index,
        uv_requirement=strip_version,
        external_lookup=True,
        ambiguous=payload_resolution.ambiguous,
    )
```

Nested `env`, external `time`, `uv`, and `uvx` remain external lookup. The existing recursion and
option functions remain the only authority for reaching the candidate.

- [ ] **Step 7: Convert the deepest reachable candidate to immutable taint evidence**

Import `_ExecutableEvidence` from `shell_taint.py` into `shell_scanner.py`, then add:

```python
def _candidate_name(
    candidate: _ExecutableCandidate,
    words: list[_ShellWord],
) -> str | None:
    word = words[candidate.index]
    if candidate.uv_requirement:
        return _uv_requirement_executable_name(word.literal)
    return _basename(word.literal)


def _effective_executable_evidence(
    words: list[_ShellWord],
    budget: _ScanBudget,
) -> _ExecutableEvidence | None:
    """Resolve effective heads with the exact invocation launcher grammar."""
    resolution = _LauncherResolutionState(budget)
    result = _doc_lattice_command_index(words, 0, resolution)
    candidates = resolution.executable_positions
    if not candidates and result.index is not None:
        candidates = [
            _ExecutableCandidate(
                result.index,
                external_lookup=result.external_lookup,
                ambiguous=result.ambiguous,
            )
        ]
    if not candidates:
        return None

    def convert(candidate: _ExecutableCandidate) -> _ExecutableEvidence:
        word = words[candidate.index]
        return _ExecutableEvidence(
            argv_index=candidate.index,
            name=_candidate_name(candidate, words),
            literal=word.literal,
            external_lookup=candidate.external_lookup,
            ambiguous=candidate.ambiguous or result.ambiguous,
        )

    converted = tuple(convert(candidate) for candidate in candidates)
    primary = converted[-1]
    return _ExecutableEvidence(
        argv_index=primary.argv_index,
        name=primary.name,
        literal=primary.literal,
        external_lookup=primary.external_lookup,
        ambiguous=primary.ambiguous,
        alternates=converted[:-1],
    )
```

The helper intentionally uses a fresh resolution state. Task 5 will compute invocation
classification and taint executable evidence from one shared state at flush so production does not
double-charge the scan budget.

- [ ] **Step 8: Run resolver and full phase-1 scanner tests**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_effective_executable_evidence_reuses_launcher_grammar \
  tests/test_github_ci_shell_scanner.py::test_effective_executable_evidence_retains_ambiguous_candidates \
  -v
uv run --no-sync pytest tests/test_github_ci_shell_scanner.py
```

Expected: the focused tests pass, and the complete existing scanner suite passes with no changed
invocations or phase-1 incomplete reasons.

- [ ] **Step 9: Commit shared effective-head resolution**

```bash
git add src/doc_lattice/github_ci/shell_scanner.py \
  src/doc_lattice/github_ci/shell_taint.py \
  tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_shell_taint.py
git commit -m "refactor: expose effective shell executable evidence"
```

### Task 5: Emit basic command evidence and run the taint pass once

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:366-440`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:503-648`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1010-1025`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1240-1510`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1648-1710`
- Modify: `tests/test_github_ci_shell_scanner.py`

- [ ] **Step 1: Add basic cross-command variable-flow tests**

Add `TAINT_REFUSAL_REASON` to the imports from `shell_taint.py` in
`tests/test_github_ci_shell_scanner.py`, then add:

```python
def assert_taint_refusal(script: str) -> None:
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == TAINT_REFUSAL_REASON
    with pytest.raises(
        ConfigError,
        match=rf"shell scan incomplete: {TAINT_REFUSAL_REASON}",
    ):
        direct_doc_lattice_invocations(script)


def test_cross_command_assignment_append_reaches_eval_sink():
    assert_taint_refusal(
        """\
X=doc-
X+='lattice reconcile'
eval "$X"
"""
    )


def test_taint_analysis_is_order_insensitive_within_one_run_body():
    assert_taint_refusal(
        """\
eval "$X"
X=doc-
X+='lattice reconcile'
"""
    )


def test_unrelated_variable_definitions_do_not_ambiently_compose():
    script = """\
A=doc-
B=lattice
eval "$A"
"""

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'eval "doc-${EXTERNAL}lattice"',
        'eval "doc${EXTERNAL}lattice"',
    ],
    ids=["authored-separator", "external-separator"],
)
def test_external_gap_distinguishes_authored_and_external_separator(script):
    result = scan_doc_lattice_invocations(script)

    if "doc-" in script:
        assert result.incomplete_reason == TAINT_REFUSAL_REASON
    else:
        assert result.incomplete_reason is None


def test_phase_one_complete_assignment_marker_keeps_phase_one_reason():
    result = scan_doc_lattice_invocations(
        "X='doc-lattice reconcile'; eval \"$X\""
    )

    assert (
        result.incomplete_reason
        == "marker-bearing command is not a certified doc-lattice invocation"
    )


def test_resolved_doc_lattice_finding_survives_taint_pass():
    result = scan_doc_lattice_invocations(
        "X=doc-; X+=lattice; doc-lattice reconcile --dry-run"
    )

    assert result.invocations == RECONCILE_DRY
    assert result.incomplete_reason is None
```

- [ ] **Step 2: Run the new scanner tests and confirm the escapes still certify**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_cross_command_assignment_append_reaches_eval_sink \
  tests/test_github_ci_shell_scanner.py::test_taint_analysis_is_order_insensitive_within_one_run_body \
  tests/test_github_ci_shell_scanner.py::test_unrelated_variable_definitions_do_not_ambiently_compose \
  tests/test_github_ci_shell_scanner.py::test_external_gap_distinguishes_authored_and_external_separator \
  tests/test_github_ci_shell_scanner.py::test_phase_one_complete_assignment_marker_keeps_phase_one_reason \
  tests/test_github_ci_shell_scanner.py::test_resolved_doc_lattice_finding_survives_taint_pass \
  -v
```

Expected: the split assignment/refusal and authored-separator rows fail because the scanner still
returns a complete empty result. The phase-1 reason and resolved finding tests pass.

- [ ] **Step 3: Add the taint-owned word content builder**

Append to `shell_taint.py`:

```python
@dataclass(frozen=True, slots=True)
class _BuiltContent:
    expression: ContentExpr
    assignment_name: str | None = None
    assignment_content: ContentExpr | None = None
    assignment_append: bool = False
    conditional_assignments: tuple[_AssignmentEvidence, ...] = ()
    process_resource_id: int | None = None


@dataclass(slots=True)
class ContentBuilder:
    """Build one word's ordered authored content while the scanner parses it."""

    parts: list[ContentExpr]
    assignment_value_start: int | None = None
    assignment_name: str | None = None
    assignment_append: bool = False
    conditional_assignments: list[_AssignmentEvidence] = field(
        default_factory=list
    )
    process_resource_id: int | None = None

    @classmethod
    def empty(cls) -> ContentBuilder:
        return cls(parts=[])

    def append_literal(self, text: str) -> None:
        if text:
            self.parts.append(LiteralTransfer(text))

    def append_expression(self, expression: ContentExpr) -> None:
        self.parts.append(expression)

    def mark_assignment(self, name: str, *, append: bool) -> None:
        self.assignment_name = name
        self.assignment_append = append
        self.assignment_value_start = len(self.parts)

    def add_conditional_assignment(
        self,
        assignment: _AssignmentEvidence,
    ) -> None:
        self.conditional_assignments.append(assignment)

    def build(self) -> _BuiltContent:
        expression = concat(*self.parts)
        assignment_content: ContentExpr | None = None
        if self.assignment_value_start is not None:
            assignment_content = concat(
                *self.parts[self.assignment_value_start :]
            )
        return _BuiltContent(
            expression=expression,
            assignment_name=self.assignment_name,
            assignment_content=assignment_content,
            assignment_append=self.assignment_append,
            conditional_assignments=tuple(self.conditional_assignments),
            process_resource_id=self.process_resource_id,
        )
```

Import `field` beside `dataclass` from `dataclasses`.

- [ ] **Step 4: Add a mutable evidence accumulator with monotonic IDs**

Append to `shell_taint.py`:

```python
@dataclass(slots=True)
class _EvidenceBuilder:
    """Accumulate parser evidence and freeze it before analysis."""

    commands: list[_CommandEvidence]
    scopes: list[_StreamScopeEvidence]
    pipes: list[_PipeEvidence]
    process_resources: list[_ProcessResourceEvidence]
    next_command_id: int = 1
    next_scope_id: int = 1
    next_process_resource_id: int = 1

    @classmethod
    def empty(cls) -> _EvidenceBuilder:
        return cls(commands=[], scopes=[], pipes=[], process_resources=[])

    def allocate_command(self) -> tuple[int, int]:
        command_id = self.next_command_id
        self.next_command_id += 1
        output_scope_id = self.allocate_scope()
        return command_id, output_scope_id

    def allocate_scope(self) -> int:
        scope_id = self.next_scope_id
        self.next_scope_id += 1
        return scope_id

    def allocate_process_resource(self) -> int:
        resource_id = self.next_process_resource_id
        self.next_process_resource_id += 1
        return resource_id

    def freeze(self) -> _ShellTaintEvidence:
        return _ShellTaintEvidence(
            commands=tuple(self.commands),
            scopes=tuple(self.scopes),
            pipes=tuple(self.pipes),
            process_resources=tuple(self.process_resources),
        )
```

- [ ] **Step 5: Attach symbolic content to finalized shell words**

Import these names into `shell_scanner.py`:

```python
from .shell_taint import (
    TAINT_REFUSAL_REASON,
    ContentExpr,
    ContentBuilder,
    LiteralTransfer,
    OutsideGap,
    VariableRef,
    _ArgPort,
    _AssignmentEvidence,
    _CommandEvidence,
    _EvidenceBuilder,
    _ExecutableEvidence,
    _RedirectionEvent,
    analyze_marker_taint,
)
```

Add these fields to `_ShellWord` after `literal`:

```python
    content: ContentExpr = LiteralTransfer("")
    assignment_name: str | None = None
    assignment_content: ContentExpr | None = None
    assignment_append: bool = False
    conditional_assignments: tuple[_AssignmentEvidence, ...] = ()
    process_resource_id: int | None = None
```

Add this field to `_ShellWordBuilder`:

```python
    content: ContentBuilder = field(default_factory=ContentBuilder.empty)
```

At the beginning of `append_protected`, before provenance flags are changed, append authored text:

```python
        if segment:
            text = "".join(segment) if isinstance(segment, list) else segment
            self.content.append_literal(text)
```

At the beginning of `append_active`:

```python
        self.content.append_literal(character)
```

When `append_active` recognizes a valid assignment at `=`, mark the first value part after the
equals sign:

```python
            self.shell_assignment = bool(
                _SHELL_ASSIGNMENT_NAME_RE.fullmatch(assignment_name)
            )
            if self.shell_assignment:
                self.content.mark_assignment(
                    assignment_name,
                    append=self.assignment_name.endswith("+"),
                )
```

In `build`, call `built_content = self.content.build()` and add:

```python
            content=built_content.expression,
            assignment_name=built_content.assignment_name,
            assignment_content=built_content.assignment_content,
            assignment_append=built_content.assignment_append,
            conditional_assignments=built_content.conditional_assignments,
            process_resource_id=built_content.process_resource_id,
```

The assignment marker must be placed after the literal `=` part is appended, so
`assignment_content` starts at the first RHS fragment and never includes `X+=`.

- [ ] **Step 6: Surface unbraced variable references and external gaps**

Extend `_ShellExpansion`:

```python
@dataclass(frozen=True, slots=True)
class _ShellExpansion:
    end: int
    quoted_zero_field_expansion: bool = False
    content: ContentExpr = OutsideGap()
    conditional_assignments: tuple[_AssignmentEvidence, ...] = ()
```

In `_consume_active_expansion`, replace the unbraced `$` branch with:

```python
        elif self.source[index] == "$":
            end = _consume_parameter_name(self.source, index, limit)
            parameter_end = _parameter_name_end(
                self.source,
                index + 1,
                limit,
            )
            name = self.source[index + 1 : parameter_end]
            content = VariableRef(name) if _is_name(name) else OutsideGap()
            quoted_zero_field_expansion = double_quoted and (
                _is_unbraced_named_parameter(self.source, index, limit)
                or (index + 1 < limit and self.source[index + 1] == "@")
            )
```

Initialize `content: ContentExpr = OutsideGap()` at the start of the function, and return:

```python
        return _ShellExpansion(
            end,
            quoted_zero_field_expansion,
            content,
        )
```

For Task 5, command substitutions, arithmetic, legacy substitutions, and complex braced parameters
remain `OutsideGap`; later tasks replace those specific cases.

- [ ] **Step 7: Preserve expansion content inside quoted and unquoted words**

In `_parse_word`, immediately before each existing
`builder.append_protected("", dynamic=True, ...)` call for an active expansion, add:

```python
                builder.content.append_expression(expansion_end.content)
                for assignment in expansion_end.conditional_assignments:
                    builder.content.add_conditional_assignment(assignment)
```

Change `_parse_double_quoted` to accept a content builder:

```python
def _parse_double_quoted(
    self,
    start: int,
    limit: int,
    depth: int,
    content: ContentBuilder,
) -> tuple[list[str], int, bool, bool]:
```

Pass `builder.content` from both callers. When it consumes an expansion, add:

```python
                content.append_expression(expansion_end.content)
                for assignment in expansion_end.conditional_assignments:
                    content.add_conditional_assignment(assignment)
```

When `_parse_double_quoted` consumes a literal or escaped character, append the exact same decoded
character to `content` at the same point it appends to `characters`. To avoid double-counting,
change the caller's final `builder.append_protected(segment, ...)` to:

```python
                builder.append_protected(
                    "",
                    dynamic=fragment_dynamic,
                    locale_translated=True,
                    quoted_zero_field_expansion=fragment_zero_field,
                )
                builder.characters.extend(segment)
```

for locale-translated quotes, and the same form without `locale_translated=True` for ordinary
double quotes. Set `builder.keyword_eligible = False` and the existing assignment-name provenance
exactly as `append_protected` currently does.

- [ ] **Step 8: Store one command's assignment and argv ports**

Add a temporary empty `redirections` field for Task 6 and a `last_command_id` to
`_CommandScanState`:

```python
    redirections: list[_RedirectionEvent] = field(default_factory=list)
    last_command_id: int | None = None
```

Clear `redirections` in `reset_command`; do not clear `last_command_id` or `heredocs`.

Add these helpers to `_ShellScanner`:

```python
def _assignment_indices(
    words: list[_ShellWord],
    executable: _ExecutableEvidence | None,
) -> frozenset[int]:
    limit = executable.argv_index if executable is not None else len(words)
    return frozenset(
        index
        for index, word in enumerate(words[:limit])
        if word.shell_assignment and word.assignment_name is not None
    )


def _remap_executable(
    executable: _ExecutableEvidence | None,
    argv_indices: dict[int, int],
) -> _ExecutableEvidence:
    if executable is None:
        return _ExecutableEvidence(None, None, None)

    def remap(candidate: _ExecutableEvidence) -> _ExecutableEvidence:
        mapped = (
            argv_indices.get(candidate.argv_index)
            if candidate.argv_index is not None
            else None
        )
        return _ExecutableEvidence(
            argv_index=mapped,
            name=candidate.name,
            literal=candidate.literal,
            external_lookup=candidate.external_lookup,
            ambiguous=candidate.ambiguous,
        )

    primary = remap(executable)
    return _ExecutableEvidence(
        argv_index=primary.argv_index,
        name=primary.name,
        literal=primary.literal,
        external_lookup=primary.external_lookup,
        ambiguous=primary.ambiguous,
        alternates=tuple(remap(item) for item in executable.alternates),
    )
```

Use the primary executable index for assignment-prefix separation. Every supported wrapper and
`env` assignment lies before it; an assignment-shaped word after the executable remains argv.

- [ ] **Step 9: Share one launcher resolution and emit evidence at flush**

Change `_invocation_in_simple_command` to accept a caller-owned resolution:

```python
def _invocation_in_simple_command(
    words: list[_ShellWord],
    budget: _ScanBudget,
    *,
    command_has_marker: bool,
    resolution: _LauncherResolutionState | None = None,
) -> _Invocation | None:
    resolution = resolution or _LauncherResolutionState(budget)
```

Split Task 4's helper into
`_executable_evidence_from_resolution(words, resolution)` plus the test wrapper:

```python
def _effective_executable_evidence(
    words: list[_ShellWord],
    budget: _ScanBudget,
) -> _ExecutableEvidence | None:
    resolution = _LauncherResolutionState(budget)
    _doc_lattice_command_index(words, 0, resolution)
    return _executable_evidence_from_resolution(words, resolution)
```

Extend `_ShellScanner.__init__`:

```python
        taint_builder: _EvidenceBuilder | None = None,
        collect_taint: bool = True,
```

Initialize:

```python
        self.taint_builder = (
            taint_builder
            if taint_builder is not None
            else (_EvidenceBuilder.empty() if collect_taint else None)
        )
        self.owns_taint_builder = taint_builder is None and collect_taint
```

Replace `_flush_command` with:

```python
def _flush_command(self, state: _CommandScanState) -> int | None:
    if not state.words and not state.redirections:
        return None
    resolution = _LauncherResolutionState(self.budget)
    invocation: _Invocation | None = None
    if self.classify_commands and state.words:
        invocation = _invocation_in_simple_command(
            state.words,
            self.budget,
            command_has_marker=state.command_has_marker,
            resolution=resolution,
        )
        if invocation is not None:
            if len(self.invocations) >= _MAX_SHELL_INVOCATIONS:
                raise _ShellScanIncomplete("invocation limit exceeded")
            self.invocations.append(invocation)

    command_id: int | None = None
    if self.taint_builder is not None:
        if state.words and not resolution.executable_positions:
            _doc_lattice_command_index(state.words, 0, resolution)
        executable = _executable_evidence_from_resolution(
            state.words,
            resolution,
        )
        assignment_indices = _assignment_indices(state.words, executable)
        assignments = [
            _AssignmentEvidence(
                name=word.assignment_name,
                content=word.assignment_content or LiteralTransfer(""),
                append=word.assignment_append,
            )
            for index, word in enumerate(state.words)
            if index in assignment_indices and word.assignment_name is not None
        ]
        assignments.extend(
            assignment
            for word in state.words
            for assignment in word.conditional_assignments
        )
        argv: list[_ArgPort] = []
        argv_indices: dict[int, int] = {}
        for index, word in enumerate(state.words):
            if index in assignment_indices:
                continue
            argv_indices[index] = len(argv)
            argv.append(
                _ArgPort(
                    literal=word.literal,
                    content=word.content,
                    dynamic=word.dynamic,
                    process_resource_id=word.process_resource_id,
                )
            )
        command_id, output_scope_id = self.taint_builder.allocate_command()
        self.taint_builder.commands.append(
            _CommandEvidence(
                command_id=command_id,
                output_scope_id=output_scope_id,
                container_scope_id=0,
                argv=tuple(argv),
                assignments=tuple(assignments),
                redirections=(),
                executable=_remap_executable(executable, argv_indices),
            )
        )
        state.last_command_id = command_id
    state.reset_command()
    return command_id
```

Task 7 replaces the temporary `container_scope_id=0`; Task 6 replaces the empty redirection tuple.

- [ ] **Step 10: Run taint only after the top-level scan completes**

Replace `_ShellScanner.scan` with:

```python
def scan(self) -> tuple[_Invocation, ...]:
    self._scan_commands(0, len(self.source), terminator=None, depth=0)
    if self.owns_taint_builder and self.taint_builder is not None:
        refused, reason = analyze_marker_taint(self.taint_builder.freeze())
        if refused:
            raise _ShellScanIncomplete(reason or TAINT_REFUSAL_REASON)
    return tuple(self.invocations)
```

Every child `_ShellScanner` created only to lex a heredoc delimiter must pass
`collect_taint=False`. Every child that scans executable nested syntax will later share the parent
builder; until Task 7 it also passes `collect_taint=False`, preventing a nested pass from running
early.

- [ ] **Step 11: Run focused and complete scanner suites**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_cross_command_assignment_append_reaches_eval_sink \
  tests/test_github_ci_shell_scanner.py::test_taint_analysis_is_order_insensitive_within_one_run_body \
  tests/test_github_ci_shell_scanner.py::test_unrelated_variable_definitions_do_not_ambiently_compose \
  tests/test_github_ci_shell_scanner.py::test_external_gap_distinguishes_authored_and_external_separator \
  tests/test_github_ci_shell_scanner.py::test_phase_one_complete_assignment_marker_keeps_phase_one_reason \
  tests/test_github_ci_shell_scanner.py::test_resolved_doc_lattice_finding_survives_taint_pass \
  -v
uv run --no-sync pytest tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_shell_taint.py
```

Expected: all focused tests and both complete files pass.

- [ ] **Step 12: Commit basic scanner evidence**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_scanner.py
git commit -m "feat: trace cross-command variable marker flow"
```

### Task 6: Record descriptor-aware redirections, heredocs, and static resources

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:503-525`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:982-1140`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1140-1240`
- Modify: `tests/test_github_ci_shell_scanner.py`

- [ ] **Step 1: Add file, heredoc, herestring, and descriptor-order tests**

Append:

```python
@pytest.mark.parametrize(
    "script_operand",
    ["task.sh", "./task.sh"],
    ids=["plain", "dot-normalized"],
)
def test_split_printf_file_handoff_reaches_shell_script_sink(script_operand):
    assert_taint_refusal(
        f"""\
printf '%s%s\\n' 'doc-' 'lattice reconcile' > task.sh
bash {script_operand}
"""
    )


def test_heredoc_passthrough_reaches_written_script_sink():
    assert_taint_refusal(
        """\
cat > task.sh <<'EOF'
doc-lattice reconcile
EOF
bash task.sh
"""
    )


def test_split_herestring_reaches_shell_stdin_sink():
    assert_taint_refusal(
        """\
X=doc-
X+='lattice reconcile'
bash <<< "$X"
"""
    )


def test_static_descriptor_zero_read_reaches_shell_stdin_sink():
    assert_taint_refusal(
        """\
printf '%s%s\\n' doc- 'lattice reconcile' > task.sh
bash < task.sh
"""
    )


def test_nonzero_static_read_is_not_shell_stdin():
    script = """\
printf '%s%s\\n' doc- 'lattice reconcile' > task.sh
bash 3< task.sh
"""

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_final_stdout_binding_routes_bytes_to_task():
    assert_taint_refusal(
        """\
printf '%s%s\\n' doc- 'lattice reconcile' > /dev/null > task.sh
bash task.sh
"""
    )


def test_overwritten_stdout_binding_leaves_only_empty_task():
    script = """\
printf '%s%s\\n' doc- 'lattice reconcile' > task.sh > /dev/null
bash task.sh
"""

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_nonzero_heredoc_is_not_shell_stdin():
    script = """\
bash 3<<'EOF'
doc-lattice reconcile
EOF
"""

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None
```

- [ ] **Step 2: Run the redirection tests and verify the handoffs still certify**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_split_printf_file_handoff_reaches_shell_script_sink \
  tests/test_github_ci_shell_scanner.py::test_heredoc_passthrough_reaches_written_script_sink \
  tests/test_github_ci_shell_scanner.py::test_split_herestring_reaches_shell_stdin_sink \
  tests/test_github_ci_shell_scanner.py::test_static_descriptor_zero_read_reaches_shell_stdin_sink \
  tests/test_github_ci_shell_scanner.py::test_nonzero_static_read_is_not_shell_stdin \
  tests/test_github_ci_shell_scanner.py::test_final_stdout_binding_routes_bytes_to_task \
  tests/test_github_ci_shell_scanner.py::test_overwritten_stdout_binding_leaves_only_empty_task \
  tests/test_github_ci_shell_scanner.py::test_nonzero_heredoc_is_not_shell_stdin \
  -v
```

Expected: the four marker-flow tests and final-binding refusal fail because no redirection evidence
is emitted. The nonzero and overwritten-binding certification rows pass.

- [ ] **Step 3: Make late heredoc content replaceable without mutating frozen evidence**

Import `replace` from `dataclasses` in `shell_taint.py`. Add this method to `_EvidenceBuilder`:

```python
    def attach_redirection_content(
        self,
        command_id: int,
        ordinal: int,
        content: ContentExpr,
    ) -> None:
        for command_index, command in enumerate(self.commands):
            if command.command_id != command_id:
                continue
            events = tuple(
                replace(event, target=ContentTarget(content))
                if event.ordinal == ordinal
                else event
                for event in command.redirections
            )
            self.commands[command_index] = replace(
                command,
                redirections=events,
            )
            return
        raise ValueError("heredoc owner command is missing")
```

The evidence remains immutable at every observation point; only the builder swaps one complete
frozen command record before `freeze()`.

- [ ] **Step 4: Preserve descriptor, ordinal, and owner on pending heredocs**

Replace `_Heredoc` in `shell_scanner.py` with:

```python
@dataclass(slots=True)
class _Heredoc:
    delimiter: str
    strip_tabs: bool
    expand: bool
    descriptor: int | None
    ordinal: int
    owner_id: int | None = None
```

Add to `_CommandScanState`:

```python
    owned_heredoc_count: int = 0
```

Do not reset `owned_heredoc_count` or `heredocs` in `reset_command`.

- [ ] **Step 5: Return a parsed redirection with its effective descriptor**

Add:

```python
@dataclass(frozen=True, slots=True)
class _ParsedRedirection:
    operand_start: int
    operator: str
    descriptor: int | None
```

Replace `_redirection_at` with:

```python
def _redirection_at(
    self,
    index: int,
    limit: int,
) -> _ParsedRedirection | None:
    operator_index = index
    descriptor: int | None = None
    dynamic_descriptor = False
    if self.source[index].isdigit():
        while operator_index < limit and self.source[operator_index].isdigit():
            operator_index += 1
        descriptor = int(self.source[index:operator_index])
    elif self.source[index] == "{":
        dynamic_descriptor = True
        closing = self.source.find("}", index + 1, limit)
        if closing != -1 and _is_name(self.source[index + 1 : closing]):
            operator_index = closing + 1
            descriptor = None
    for operator in _REDIRECTION_OPERATORS:
        if not self.source.startswith(operator, operator_index):
            continue
        if descriptor is None and not dynamic_descriptor:
            descriptor = 0 if operator in {"<", "<<", "<<-", "<<<", "<&", "<>"} else 1
        return _ParsedRedirection(
            operand_start=operator_index + len(operator),
            operator=operator,
            descriptor=descriptor,
        )
    return None
```

- [ ] **Step 6: Convert redirection operands to typed targets**

Import these names into `shell_scanner.py`:

```python
from .shell_taint import (
    ContentTarget,
    DescriptorTarget,
    DynamicResourceTarget,
    NullTarget,
    ProcessResourceTarget,
    StaticResourceTarget,
    _RedirectionEvent,
    concat,
    normalize_static_resource,
)
```

Add:

```python
def _redirection_target(
    word: _ShellWord,
    operator: str,
) -> RedirectionTarget:
    if word.process_resource_id is not None:
        return ProcessResourceTarget(word.process_resource_id)
    if operator in {"<&", ">&"} and word.literal.isdigit() and not word.dynamic:
        return DescriptorTarget(int(word.literal))
    if word.literal == "/dev/null" and not word.dynamic:
        return NullTarget()
    key = normalize_static_resource(word.literal, dynamic=word.dynamic)
    if key is not None:
        return StaticResourceTarget(key)
    return DynamicResourceTarget()
```

Annotate the return as `RedirectionTarget` and import that alias with the other taint evidence
types.

- [ ] **Step 7: Record ordinary, heredoc, and herestring events in parse order**

Change `_consume_redirection` to receive command state and return only the next index:

```python
def _consume_redirection(
    self,
    redirection: _ParsedRedirection,
    limit: int,
    state: _CommandScanState,
    depth: int,
) -> int:
    index = redirection.operand_start
    operator = redirection.operator
    while index < limit and self.source[index] in " \t":
        index += 1
    ordinal = len(state.redirections)
    if operator in {"<<", "<<-"}:
        delimiter, quoted, index = self._parse_heredoc_delimiter(
            index,
            limit,
            depth,
        )
        if delimiter is None:
            return index
        heredoc = _Heredoc(
            delimiter=delimiter,
            strip_tabs=operator == "<<-",
            expand=not quoted,
            descriptor=redirection.descriptor,
            ordinal=ordinal,
        )
        state.heredocs.append(heredoc)
        state.redirections.append(
            _RedirectionEvent(
                ordinal,
                operator,
                redirection.descriptor,
                ContentTarget(LiteralTransfer("")),
            )
        )
        return index
    target, index = self._parse_word(index, limit, depth)
    if operator == "<<<":
        redirection_target = ContentTarget(
            concat(target.content, LiteralTransfer("\n"))
        )
    else:
        redirection_target = _redirection_target(target, operator)
    state.redirections.append(
        _RedirectionEvent(
            ordinal,
            operator,
            redirection.descriptor,
            redirection_target,
        )
    )
    return index
```

Update `_scan_commands`:

```python
            if redirection is not None:
                index = self._consume_redirection(
                    redirection,
                    limit,
                    state,
                    depth,
                )
                continue
```

- [ ] **Step 8: Emit redirections and stamp every newly registered heredoc owner**

In `_flush_command`, replace `redirections=()` with:

```python
                redirections=tuple(state.redirections),
```

After `command_id` is allocated and before `state.reset_command()`:

```python
        for heredoc in state.heredocs[state.owned_heredoc_count :]:
            heredoc.owner_id = command_id
        state.owned_heredoc_count = len(state.heredocs)
```

This count-based attachment is required for:

```bash
cat <<EOF; printf ignored
body
EOF
```

A single “last command” pointer would attach the body to `printf`.

- [ ] **Step 9: Attach the exact heredoc body to its owner after the newline**

In `_consume_heredocs`, once `body_start`, `body_end`, and the delimiter are known, compute:

```python
            raw_body = self.source[body_start:body_end]
            body = (
                _remove_active_line_continuations(raw_body)
                if heredoc.expand
                else raw_body
            )
            if (
                self.taint_builder is not None
                and heredoc.owner_id is not None
            ):
                self.taint_builder.attach_redirection_content(
                    heredoc.owner_id,
                    heredoc.ordinal,
                    LiteralTransfer(body),
                )
```

Keep the existing executable-expansion scan for unquoted heredocs. Task 7 replaces the unquoted
body's literal-only taint expression with expansion-aware content; quoted heredocs stay an exact
`LiteralTransfer`.

- [ ] **Step 10: Run focused redirection and complete scanner tests**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_split_printf_file_handoff_reaches_shell_script_sink \
  tests/test_github_ci_shell_scanner.py::test_heredoc_passthrough_reaches_written_script_sink \
  tests/test_github_ci_shell_scanner.py::test_split_herestring_reaches_shell_stdin_sink \
  tests/test_github_ci_shell_scanner.py::test_static_descriptor_zero_read_reaches_shell_stdin_sink \
  tests/test_github_ci_shell_scanner.py::test_nonzero_static_read_is_not_shell_stdin \
  tests/test_github_ci_shell_scanner.py::test_final_stdout_binding_routes_bytes_to_task \
  tests/test_github_ci_shell_scanner.py::test_overwritten_stdout_binding_leaves_only_empty_task \
  tests/test_github_ci_shell_scanner.py::test_nonzero_heredoc_is_not_shell_stdin \
  -v
uv run --no-sync pytest tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_shell_taint.py
```

Expected: all focused rows and both complete files pass.

- [ ] **Step 11: Commit descriptor-aware resource flow**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_scanner.py
git commit -m "feat: trace shell redirection resource flow"
```

### Task 7: Add stream scopes, pipes, process substitutions, and substitution stripping

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:573-780`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:975-1040`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1220-1640`
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `tests/test_github_ci_shell_taint.py`

- [ ] **Step 1: Add pipe, substitution, group, and newline-strip tests**

Append:

```python
def test_split_pipeline_stdout_reaches_shell_stdin():
    assert_taint_refusal(
        "printf '%s%s\\n' doc- 'lattice reconcile' | bash"
    )


def test_marker_free_unresolved_pipeline_stays_certified():
    result = scan_doc_lattice_invocations(
        "curl https://example.invalid/script | bash"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_later_herestring_rebinds_pipeline_stdin():
    result = scan_doc_lattice_invocations(
        "printf '%s%s\\n' doc- 'lattice reconcile' | bash <<<'true'"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_input_process_substitution_redirection_reaches_stdin():
    assert_taint_refusal(
        "bash < <(printf '%s%s\\n' doc- 'lattice reconcile')"
    )


def test_input_process_substitution_script_operand_reaches_sink():
    assert_taint_refusal(
        "bash <(printf '%s%s\\n' doc- 'lattice reconcile')"
    )


def test_process_substitution_read_by_non_sink_is_not_overconnected():
    result = scan_doc_lattice_invocations(
        "grep x <(printf '%s%s' doc- lattice)"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_output_process_substitution_routes_writer_to_consumer_stdin():
    assert_taint_refusal(
        "printf '%s%s\\n' doc- 'lattice reconcile' > >(bash)"
    )


def test_multi_command_substitution_scope_sequences_stdout():
    assert_taint_refusal(
        """\
eval "$(printf doc-; printf 'lattice reconcile')"
"""
    )


def test_compound_group_stdout_reaches_written_resource():
    assert_taint_refusal(
        """\
{ printf doc-; printf 'lattice reconcile'; } > task.sh
bash task.sh
"""
    )


def test_command_substitution_strips_trailing_newline_before_splice():
    assert_taint_refusal(
        """\
eval "$(cat <<'EOF'
doc-
EOF
)lattice reconcile"
"""
    )
```

- [ ] **Step 2: Run the new stream tests and verify they are red**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_split_pipeline_stdout_reaches_shell_stdin \
  tests/test_github_ci_shell_scanner.py::test_marker_free_unresolved_pipeline_stays_certified \
  tests/test_github_ci_shell_scanner.py::test_later_herestring_rebinds_pipeline_stdin \
  tests/test_github_ci_shell_scanner.py::test_input_process_substitution_redirection_reaches_stdin \
  tests/test_github_ci_shell_scanner.py::test_input_process_substitution_script_operand_reaches_sink \
  tests/test_github_ci_shell_scanner.py::test_process_substitution_read_by_non_sink_is_not_overconnected \
  tests/test_github_ci_shell_scanner.py::test_output_process_substitution_routes_writer_to_consumer_stdin \
  tests/test_github_ci_shell_scanner.py::test_multi_command_substitution_scope_sequences_stdout \
  tests/test_github_ci_shell_scanner.py::test_compound_group_stdout_reaches_written_resource \
  tests/test_github_ci_shell_scanner.py::test_command_substitution_strips_trailing_newline_before_splice \
  -v
```

Expected: all refusal rows fail because nested stdout is still absent from taint evidence. Both
certification rows pass.

- [ ] **Step 3: Add explicit scope references to output structure**

In the locked vocabulary and `OutputExpr` alias, add `ScopeOutput`. Define:

```python
@dataclass(frozen=True, slots=True)
class ScopeOutput:
    scope_id: int


OutputExpr: TypeAlias = (
    CommandOutput
    | ScopeOutput
    | SequenceOutput
    | ChoiceOutput
    | RepeatOutput
)
```

In `_OutputLowering.lower`, immediately after the `CommandOutput` branch, add:

```python
        if isinstance(output, ScopeOutput):
            return StreamRef(output.scope_id)
```

An empty scope is `SequenceOutput(())`, which lowers to authored epsilon. A nested group is
`ScopeOutput(scope_id)`. A command substitution or process substitution is referenced through its
word or typed resource, not appended ambiently to the parent scope.

- [ ] **Step 4: Add scanner-side scope and pipeline frames**

In `shell_scanner.py`, add:

```python
@dataclass(slots=True)
class _ScopeFrame:
    scope_id: int
    kind: str
    parent_scope_id: int | None
    parent_command_id: int | None
    outputs: list[OutputExpr]
    redirections: list[_RedirectionEvent] = field(default_factory=list)
    loop_bindings: list[_AssignmentEvidence] = field(default_factory=list)


@dataclass(slots=True)
class _PipelineFrame:
    scope_id: int
    stages: list[int]
```

Add to `_CommandScanState`:

```python
    pending_pipe_producer: int | None = None
    pipeline: _PipelineFrame | None = None
    pending_compound_scope_id: int | None = None
```

Add to `_ShellScanner.__init__`:

```python
        self.scope_stack: list[_ScopeFrame] = []
```

- [ ] **Step 5: Open and freeze one stream scope around each nested scan**

Add:

```python
def _scan_stream_scope(
    self,
    start: int,
    limit: int,
    *,
    terminator: str | None,
    depth: int,
    kind: str,
) -> tuple[int, int]:
    if self.taint_builder is None:
        end = self._scan_commands(
            start,
            limit,
            terminator=terminator,
            depth=depth,
        )
        return end, 0
    parent_scope_id = (
        self.scope_stack[-1].scope_id if self.scope_stack else None
    )
    scope_id = self.taint_builder.allocate_scope()
    frame = _ScopeFrame(
        scope_id=scope_id,
        kind=kind,
        parent_scope_id=parent_scope_id,
        parent_command_id=None,
        outputs=[],
    )
    self.scope_stack.append(frame)
    end = self._scan_commands(
        start,
        limit,
        terminator=terminator,
        depth=depth,
    )
    finished = self.scope_stack.pop()
    self.taint_builder.scopes.append(
        _StreamScopeEvidence(
            scope_id=finished.scope_id,
            kind=finished.kind,
            parent_scope_id=finished.parent_scope_id,
            parent_command_id=finished.parent_command_id,
            output=SequenceOutput(tuple(finished.outputs)),
            redirections=tuple(finished.redirections),
            loop_bindings=tuple(finished.loop_bindings),
        )
    )
    return end, scope_id
```

Import `CommandOutput`, `OutputExpr`, `ScopeOutput`, `SequenceOutput`, and
`_StreamScopeEvidence`.
Change `scan()` to call `_scan_stream_scope(..., kind="command")`. All recursive executable scopes
must share the same `taint_builder`; lexical-only child scanners still disable collection.
Before each return from `_scan_commands`, call `_finalize_pipeline(state)` after its final
`_flush_command(state)`.

- [ ] **Step 6: Attach flushed commands to their containing scope and pending pipe**

In `_flush_command`, use:

```python
        container_scope_id = (
            self.scope_stack[-1].scope_id if self.scope_stack else 0
        )
```

for `_CommandEvidence.container_scope_id`. After appending the command:

```python
        if self.scope_stack:
            self.scope_stack[-1].outputs.append(CommandOutput(command_id))
        if state.pending_pipe_producer is not None:
            self.taint_builder.pipes.append(
                _PipeEvidence(
                    state.pending_pipe_producer,
                    command_id,
                )
            )
            state.pending_pipe_producer = None
```

The `_PipeEvidence` producer field is a scope ID, so set it from the producer command's
`output_scope_id`, not from the command ID. Add a builder helper:

```python
    def command_output_scope(self, command_id: int) -> int:
        for command in self.commands:
            if command.command_id == command_id:
                return command.output_scope_id
        raise ValueError("pipe producer command is missing")
```

- [ ] **Step 7: Turn `|` into a pending producer edge and pipeline scope**

Add:

```python
def _begin_or_extend_pipeline(
    self,
    state: _CommandScanState,
    producer_command_id: int,
) -> None:
    assert self.taint_builder is not None
    producer_scope = self.taint_builder.command_output_scope(
        producer_command_id
    )
    if state.pipeline is None:
        state.pipeline = _PipelineFrame(
            scope_id=self.taint_builder.allocate_scope(),
            stages=[producer_scope],
        )
    elif not state.pipeline.stages or state.pipeline.stages[-1] != producer_scope:
        state.pipeline.stages.append(producer_scope)
    state.pending_pipe_producer = producer_scope
    if self.scope_stack and self.scope_stack[-1].outputs:
        self.scope_stack[-1].outputs.pop()


def _finalize_pipeline(self, state: _CommandScanState) -> None:
    pipeline = state.pipeline
    if pipeline is None or self.taint_builder is None:
        return
    if state.last_command_id is not None:
        final_scope = self.taint_builder.command_output_scope(
            state.last_command_id
        )
        if not pipeline.stages or pipeline.stages[-1] != final_scope:
            pipeline.stages.append(final_scope)
        if self.scope_stack and self.scope_stack[-1].outputs:
            self.scope_stack[-1].outputs.pop()
    output = (
        ScopeOutput(pipeline.stages[-1])
        if pipeline.stages
        else SequenceOutput(())
    )
    self.taint_builder.scopes.append(
        _StreamScopeEvidence(
            scope_id=pipeline.scope_id,
            kind="pipeline",
            parent_scope_id=(
                self.scope_stack[-1].scope_id
                if self.scope_stack
                else None
            ),
            parent_command_id=None,
            output=output,
        )
    )
    if self.scope_stack:
        self.scope_stack[-1].outputs.append(
            ScopeOutput(pipeline.scope_id)
        )
    state.pipeline = None
```

At a `|`, retain the command ID returned by `_flush_command` and call
`_begin_or_extend_pipeline`. Before `;`, newline, `&&`, `||`, `&`, a scope terminator, or end of
input, call `_finalize_pipeline`. Pipeline output is the final stage only; the producer-to-consumer
byte flow remains the explicit `_PipeEvidence`.

- [ ] **Step 8: Build command-substitution scopes and return `StreamRef` content**

In `_consume_active_expansion`, replace the `$(` branch with:

```python
        elif self.source.startswith("$(", index):
            end, scope_id = self._scan_stream_scope(
                index + 2,
                limit,
                terminator=")",
                depth=depth + 1,
                kind="command_substitution",
            )
            content = (
                StreamRef(scope_id)
                if self.taint_builder is not None
                else OutsideGap()
            )
```

Make the arithmetic-fallback command substitution use the same helper and content. Change legacy
backticks so their child scanner shares `taint_builder`, opens a `command_substitution` scope, and
returns both the closing index and scope ID. Preserve the existing invocation collection and scan
budget in every nested scanner.

At outer command flush, traverse each word content expression for `StreamRef` values and update
those scopes' `parent_command_id`. Add to `_EvidenceBuilder`:

```python
    def attach_scope_parent(self, scope_id: int, command_id: int) -> None:
        for index, scope in enumerate(self.scopes):
            if scope.scope_id == scope_id:
                self.scopes[index] = replace(
                    scope,
                    parent_command_id=command_id,
                )
                return
```

Use a recursive `stream_ref_ids(expression)` helper in `shell_taint.py` that descends only through
`Choice` and `Concat`.

- [ ] **Step 9: Build typed input and output process-substitution resources**

Replace `_consume_process_substitution`'s integer return with:

```python
@dataclass(frozen=True, slots=True)
class _ProcessSubstitution:
    end: int
    resource_id: int
    scope_id: int
    direction: str
```

Its implementation is:

```python
def _consume_process_substitution(
    self,
    index: int,
    limit: int,
    depth: int,
) -> _ProcessSubstitution | None:
    if not (
        self.source.startswith("<(", index)
        or self.source.startswith(">(", index)
    ):
        return None
    direction = "input" if self.source[index] == "<" else "output"
    end, scope_id = self._scan_stream_scope(
        index + 2,
        limit,
        terminator=")",
        depth=depth + 1,
        kind="process_substitution",
    )
    if self.taint_builder is None:
        return _ProcessSubstitution(end, 0, scope_id, direction)
    resource_id = self.taint_builder.allocate_process_resource()
    self.taint_builder.process_resources.append(
        _ProcessResourceEvidence(
            resource_id=resource_id,
            scope_id=scope_id,
            direction=direction,
        )
    )
    return _ProcessSubstitution(
        end,
        resource_id,
        scope_id,
        direction,
    )
```

In `_parse_word`, when this result is consumed, append `OutsideGap()` as the filename's content,
set `builder.content.process_resource_id`, and advance to `process.end`. Do not append
`StreamRef(scope_id)` to the argv content. That separation is what keeps
`grep x <(producer)` from becoming an execution edge.

- [ ] **Step 10: Route output process substitutions into their consumer scope**

In `shell_taint.py`, add:

```python
def _first_commands(output: OutputExpr) -> tuple[int, ...]:
    if isinstance(output, CommandOutput):
        return (output.command_id,)
    if isinstance(output, ScopeOutput):
        return ()
    if isinstance(output, SequenceOutput):
        for part in output.parts:
            commands = _first_commands(part)
            if commands:
                return commands
        return ()
    if isinstance(output, ChoiceOutput):
        return tuple(
            command
            for part in output.parts
            for command in _first_commands(part)
        )
    return _first_commands(output.part)
```

Extend `_pipe_inputs` to inspect the final descriptor-1 binding of every command. When that target
is `ProcessResourceTarget` whose evidence direction is `"output"`, bind every first command in the
target scope to `StreamRef(writer.output_scope_id)`. Keep direct pipeline bindings and process
bindings in one dict; a later descriptor-0 redirection on the consumer still wins in
`_input_expression`.

- [ ] **Step 11: Add subshell and brace-group scopes with redirect ownership**

When `_consume_command_operator` sees `(` as a real subshell or `{` as a standalone group opener,
call `_scan_stream_scope` with `terminator=")"` or `terminator="}"` and
`kind="subshell_group"`. Set `state.pending_compound_scope_id` to the returned scope ID and append
`ScopeOutput(scope_id)` to the parent frame.

Add these `_EvidenceBuilder` methods:

```python
    def attach_scope_redirection(
        self,
        scope_id: int,
        event: _RedirectionEvent,
    ) -> None:
        for index, scope in enumerate(self.scopes):
            if scope.scope_id == scope_id:
                self.scopes[index] = replace(
                    scope,
                    redirections=(*scope.redirections, event),
                )
                return
        raise ValueError("compound redirection owner scope is missing")

    def replace_scope_output(
        self,
        scope_id: int,
        output: OutputExpr,
    ) -> None:
        for index, scope in enumerate(self.scopes):
            if scope.scope_id == scope_id:
                self.scopes[index] = replace(scope, output=output)
                return
        raise ValueError("compound output scope is missing")
```

If a redirection follows a pending compound scope while no new command word has started, attach the
event to that scope. Replay all following redirections left to right. If the final descriptor-1
binding routes away from the parent stream, remove the pending `ScopeOutput` from the parent frame;
the scope's raw stdout remains available to its resource write. Clear
`pending_compound_scope_id` on the next non-redirection word or command boundary.

- [ ] **Step 12: Parse unquoted heredoc content with active expansions**

Add to `_ShellScanner`:

```python
def _heredoc_content_expression(
    self,
    body: str,
    depth: int,
) -> ContentExpr:
    lexer = _ShellScanner(
        body,
        budget=self.budget,
        invocations=self.invocations,
        classify_commands=self.classify_commands,
        taint_builder=self.taint_builder,
    )
    content = ContentBuilder.empty()
    index = 0
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            escaped = body[index + 1]
            if escaped in {"$", "`", "\\"}:
                content.append_literal(escaped)
                index += 2
                continue
        expansion = lexer._consume_active_expansion(
            index,
            len(body),
            depth,
        )
        if expansion is not None:
            content.append_expression(expansion.content)
            index = expansion.end
            continue
        content.append_literal(body[index])
        index += 1
    return content.build().expression
```

In `_consume_heredocs`, use this expression for unquoted bodies and exact
`LiteralTransfer(raw_body)` for quoted bodies. This preserves command-substitution scopes,
variables, escaped dollars/backticks/backslashes, and active line-continuation removal while
keeping quote characters literal in unquoted heredoc bodies.

- [ ] **Step 13: Run focused stream and complete pure/scanner suites**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_split_pipeline_stdout_reaches_shell_stdin \
  tests/test_github_ci_shell_scanner.py::test_marker_free_unresolved_pipeline_stays_certified \
  tests/test_github_ci_shell_scanner.py::test_later_herestring_rebinds_pipeline_stdin \
  tests/test_github_ci_shell_scanner.py::test_input_process_substitution_redirection_reaches_stdin \
  tests/test_github_ci_shell_scanner.py::test_input_process_substitution_script_operand_reaches_sink \
  tests/test_github_ci_shell_scanner.py::test_process_substitution_read_by_non_sink_is_not_overconnected \
  tests/test_github_ci_shell_scanner.py::test_output_process_substitution_routes_writer_to_consumer_stdin \
  tests/test_github_ci_shell_scanner.py::test_multi_command_substitution_scope_sequences_stdout \
  tests/test_github_ci_shell_scanner.py::test_compound_group_stdout_reaches_written_resource \
  tests/test_github_ci_shell_scanner.py::test_command_substitution_strips_trailing_newline_before_splice \
  -v
uv run --no-sync pytest tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py
```

Expected: all focused tests and both complete files pass.

- [ ] **Step 14: Commit stream-scope flow**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py
git commit -m "feat: trace shell stream scope marker flow"
```

### Task 8: Model parameter alternatives and bounded brace argv fan-out

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:366-440`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1240-1510`
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `tests/test_github_ci_shell_taint.py`

- [ ] **Step 1: Add parameter and brace synthesis tests**

Append:

```python
def test_parameter_default_authors_marker_prefix_inside_word():
    assert_taint_refusal(
        'unset X; eval "${X:-doc-}lattice reconcile"'
    )


def test_parameter_assign_default_returns_and_assigns_authored_value():
    assert_taint_refusal(
        """\
unset X
eval "${X:=doc-}lattice"
eval "$X"
"""
    )


def test_parameter_assign_default_taints_later_eval_even_without_immediate_suffix():
    assert_taint_refusal(
        """\
unset X
: "${X:=doc-}"
X+=lattice
eval "$X"
"""
    )


def test_parameter_alternate_uses_epsilon_not_variable_value():
    script = """\
X=doc-
eval "${X:+lattice}"
"""

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_unsupported_parameter_form_surfaces_authored_literal_operand():
    assert_taint_refusal(
        'eval "${X/pattern/doc-}lattice"'
    )


def test_brace_expansion_fans_one_lexical_word_into_ordered_argv():
    assert_taint_refusal("eval doc-{lattice,noop}")


def test_brace_fanout_composes_neighboring_producer_arguments():
    assert_taint_refusal("printf %s {doc-,lattice} | bash")


def test_unrelated_brace_alternatives_do_not_become_choice():
    script = "eval {doc-,lattice}"

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_oversized_static_brace_range_fails_closed():
    script = "eval doc-{1..1000}-lattice"
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert (
        result.incomplete_reason
        == "shell taint brace expansion limit exceeded"
    )


def test_dynamic_brace_operand_fails_closed():
    result = scan_doc_lattice_invocations(
        "eval {doc-,$X}lattice"
    )

    assert (
        result.incomplete_reason
        == "shell taint dynamic brace expansion cannot be bounded"
    )
```

- [ ] **Step 2: Run parameter and brace tests and verify the synthesis gaps**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_parameter_default_authors_marker_prefix_inside_word \
  tests/test_github_ci_shell_scanner.py::test_parameter_assign_default_returns_and_assigns_authored_value \
  tests/test_github_ci_shell_scanner.py::test_parameter_assign_default_taints_later_eval_even_without_immediate_suffix \
  tests/test_github_ci_shell_scanner.py::test_parameter_alternate_uses_epsilon_not_variable_value \
  tests/test_github_ci_shell_scanner.py::test_unsupported_parameter_form_surfaces_authored_literal_operand \
  tests/test_github_ci_shell_scanner.py::test_brace_expansion_fans_one_lexical_word_into_ordered_argv \
  tests/test_github_ci_shell_scanner.py::test_brace_fanout_composes_neighboring_producer_arguments \
  tests/test_github_ci_shell_scanner.py::test_unrelated_brace_alternatives_do_not_become_choice \
  tests/test_github_ci_shell_scanner.py::test_oversized_static_brace_range_fails_closed \
  tests/test_github_ci_shell_scanner.py::test_dynamic_brace_operand_fails_closed \
  -v
```

Expected: all refusal and cap rows fail. The alternate and unrelated-argv certification rows pass.

- [ ] **Step 3: Replace flat word content parts with brace-aware tokens**

In `shell_taint.py`, add:

```python
@dataclass(frozen=True, slots=True)
class _ContentToken:
    expression: ContentExpr
    literal: str
    brace_active: bool


@dataclass(frozen=True, slots=True)
class _WordContentPort:
    literal: str
    content: ContentExpr
```

Replace `ContentBuilder.parts` with:

```python
    tokens: list[_ContentToken]
```

Change `empty()` to `return cls(tokens=[])`. Replace its two append methods:

```python
    def append_literal(
        self,
        text: str,
        *,
        brace_active: bool = False,
    ) -> None:
        if text:
            self.tokens.append(
                _ContentToken(
                    LiteralTransfer(text),
                    text,
                    brace_active,
                )
            )

    def append_expression(self, expression: ContentExpr) -> None:
        self.tokens.append(_ContentToken(expression, "", False))
```

Store `assignment_value_start` as a token index. Update `mark_assignment` and assignment RHS
construction to use `tokens`.

- [ ] **Step 4: Add bounded comma-list and range expansion**

Append:

```python
def _active_character(token: _ContentToken, character: str) -> bool:
    return (
        token.brace_active
        and isinstance(token.expression, LiteralTransfer)
        and token.literal == character
    )


def _brace_alternatives(
    tokens: tuple[_ContentToken, ...],
) -> tuple[tuple[_ContentToken, ...], ...] | None:
    if any(not isinstance(token.expression, LiteralTransfer) for token in tokens):
        raise _TaintLimitExceeded(
            "shell taint dynamic brace expansion cannot be bounded"
        )
    depth = 0
    separators: list[int] = []
    for index, token in enumerate(tokens):
        if _active_character(token, "{"):
            depth += 1
        elif _active_character(token, "}"):
            depth -= 1
        elif depth == 0 and _active_character(token, ","):
            separators.append(index)
    if separators:
        starts = (0, *(index + 1 for index in separators))
        ends = (*separators, len(tokens))
        return tuple(tokens[start:end] for start, end in zip(starts, ends))

    text = "".join(token.literal for token in tokens)
    fields = text.split("..")
    if len(fields) not in {2, 3}:
        return None
    start, stop = fields[:2]
    step_text = fields[2] if len(fields) == 3 else ""
    if start.lstrip("-").isdigit() and stop.lstrip("-").isdigit():
        first = int(start)
        last = int(stop)
        default_step = 1 if first <= last else -1
        step = int(step_text) if step_text else default_step
        if step == 0:
            return None
        end = last + (1 if step > 0 else -1)
        values = tuple(str(value) for value in range(first, end, step))
    elif len(start) == len(stop) == 1 and (
        start.isascii()
        and stop.isascii()
        and start.isalpha()
        and stop.isalpha()
    ):
        first = ord(start)
        last = ord(stop)
        default_step = 1 if first <= last else -1
        step = int(step_text) if step_text else default_step
        if step == 0:
            return None
        end = last + (1 if step > 0 else -1)
        values = tuple(chr(value) for value in range(first, end, step))
    else:
        return None
    return tuple(
        (
            _ContentToken(
                LiteralTransfer(value),
                value,
                True,
            ),
        )
        for value in values
    )
```

Add the recursive expander:

```python
def _expand_braces(
    tokens: tuple[_ContentToken, ...],
    limits: TaintLimits,
    *,
    depth: int = 0,
) -> tuple[tuple[_ContentToken, ...], ...]:
    if depth > limits.max_brace_depth:
        raise _TaintLimitExceeded(
            "shell taint brace expansion depth limit exceeded"
        )
    opening: int | None = None
    nesting = 0
    for index, token in enumerate(tokens):
        if _active_character(token, "{"):
            if opening is None:
                opening = index
            nesting += 1
            continue
        if not _active_character(token, "}") or opening is None:
            continue
        nesting -= 1
        if nesting:
            continue
        inner = tokens[opening + 1 : index]
        alternatives = _brace_alternatives(inner)
        if alternatives is None:
            opening = None
            continue
        results: list[tuple[_ContentToken, ...]] = []
        for alternative in alternatives:
            expanded = _expand_braces(
                tokens[:opening] + alternative + tokens[index + 1 :],
                limits,
                depth=depth + 1,
            )
            results.extend(expanded)
            if len(results) > limits.max_brace_expansions:
                raise _TaintLimitExceeded(
                    "shell taint brace expansion limit exceeded"
                )
        return tuple(results)
    return (tokens,)
```

Every dynamic token inside a recognized brace operand fails closed. Braces with neither a comma
list nor a bounded numeric/ASCII-letter range remain literal and do not fan out.

- [ ] **Step 5: Build ordered argv ports from expanded tokens**

Replace `ContentBuilder.build` with:

```python
    def build(
        self,
        *,
        limits: TaintLimits = TaintLimits(),
    ) -> _BuiltContent:
        expanded = _expand_braces(tuple(self.tokens), limits)
        ports = tuple(
            _WordContentPort(
                literal="".join(token.literal for token in result),
                content=concat(*(token.expression for token in result)),
            )
            for result in expanded
        )
        if not ports:
            ports = (_WordContentPort("", LiteralTransfer("")),)
        assignment_content: ContentExpr | None = None
        if self.assignment_value_start is not None:
            assignment_content = concat(
                *(
                    token.expression
                    for token in self.tokens[self.assignment_value_start :]
                )
            )
        return _BuiltContent(
            expression=ports[0].content,
            argv_ports=ports,
            assignment_name=self.assignment_name,
            assignment_content=assignment_content,
            assignment_append=self.assignment_append,
            conditional_assignments=tuple(self.conditional_assignments),
            process_resource_id=self.process_resource_id,
        )
```

Add to `_BuiltContent`:

```python
    argv_ports: tuple[_WordContentPort, ...] = ()
```

In scanner `append_active`, call:

```python
        self.content.append_literal(character, brace_active=True)
```

All quoted, escaped, expansion-derived, and heredoc fragments retain the default
`brace_active=False`.

- [ ] **Step 6: Store and flatten brace-expanded ports at command flush**

Import `_TaintLimitExceeded` and `_WordContentPort` from `shell_taint.py` into
`shell_scanner.py`.

Add to `_ShellWord`:

```python
    argv_ports: tuple[_WordContentPort, ...] = ()
```

Set `argv_ports=built_content.argv_ports` in `_ShellWordBuilder.build`. Replace Task 5's single
`argv.append` block with:

```python
            ports = word.argv_ports or (
                _WordContentPort(word.literal, word.content),
            )
            argv_indices[index] = len(argv)
            argv.extend(
                _ArgPort(
                    literal=port.literal,
                    content=port.content,
                    dynamic=word.dynamic,
                    process_resource_id=word.process_resource_id,
                )
                for port in ports
            )
```

If a candidate executable word fans out, keep the existing phase-1
`active_argv_expansion` fail-closed behavior. If a shell selector word fans out, mark the effective
executable evidence ambiguous so `_select_shell_source` considers every candidate port.

Catch `_TaintLimitExceeded` around `builder.build()` in `_parse_word` and raise
`_ShellScanIncomplete(str(error))`.

- [ ] **Step 7: Parse simple, default, alternate, and assign-default parameters**

Add:

```python
_PARAMETER_OPERATORS = (":-", ":=", ":+", "-", "=", "+")


def _parameter_operator(
    source: str,
    index: int,
    limit: int,
) -> tuple[str | None, int]:
    for operator in _PARAMETER_OPERATORS:
        if source.startswith(operator, index, limit):
            return operator, index + len(operator)
    return None, index
```

Add a scanner helper that parses a parameter operand until its matching top-level `}` while
preserving nested expansions:

```python
def _parse_parameter_word_content(
    self,
    start: int,
    closing: int,
    depth: int,
    *,
    double_quoted: bool,
) -> ContentExpr:
    content = ContentBuilder.empty()
    index = start
    while index < closing:
        expansion = self._consume_active_expansion(
            index,
            closing,
            depth,
            double_quoted=double_quoted,
        )
        if expansion is not None:
            content.append_expression(expansion.content)
            index = expansion.end
            continue
        character = self.source[index]
        if character == "\\" and index + 1 < closing:
            content.append_literal(self.source[index + 1])
            index += 2
            continue
        content.append_literal(character)
        index += 1
    return content.build().expression
```

Use the existing balanced-parameter loop to locate the matching `}` without rescanning nested
commands. Record the top-level name and operator as they are encountered. At close, return:

```python
        variable = VariableRef(name) if _is_name(name) else OutsideGap()
        if operator in {"-", ":-"}:
            content = choice(variable, operand)
            assignments: tuple[_AssignmentEvidence, ...] = ()
        elif operator in {"+", ":+"}:
            content = choice(LiteralTransfer(""), operand)
            assignments = ()
        elif operator in {"=", ":="}:
            content = choice(variable, operand)
            assignments = (
                _AssignmentEvidence(name, operand),
            )
        elif operator is None and _is_name(name):
            content = variable
            assignments = ()
        else:
            content = concat(OutsideGap(), operand)
            assignments = ()
        return _ShellExpansion(
            index,
            quoted_zero_field_expansion,
            content,
            assignments,
        )
```

For pattern replacement, substring, indirection, and transformation forms, set `operand` to the
ordered authored literal operands plus nested expansion content and use the final
`concat(OutsideGap(), operand)` branch. If nesting or operand alternatives exceed the declared
caps, raise `_ShellScanIncomplete` with the taint limit reason.

- [ ] **Step 8: Run parameter, brace, pure, and scanner suites**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_parameter_default_authors_marker_prefix_inside_word \
  tests/test_github_ci_shell_scanner.py::test_parameter_assign_default_returns_and_assigns_authored_value \
  tests/test_github_ci_shell_scanner.py::test_parameter_assign_default_taints_later_eval_even_without_immediate_suffix \
  tests/test_github_ci_shell_scanner.py::test_parameter_alternate_uses_epsilon_not_variable_value \
  tests/test_github_ci_shell_scanner.py::test_unsupported_parameter_form_surfaces_authored_literal_operand \
  tests/test_github_ci_shell_scanner.py::test_brace_expansion_fans_one_lexical_word_into_ordered_argv \
  tests/test_github_ci_shell_scanner.py::test_brace_fanout_composes_neighboring_producer_arguments \
  tests/test_github_ci_shell_scanner.py::test_unrelated_brace_alternatives_do_not_become_choice \
  tests/test_github_ci_shell_scanner.py::test_oversized_static_brace_range_fails_closed \
  tests/test_github_ci_shell_scanner.py::test_dynamic_brace_operand_fails_closed \
  -v
uv run --no-sync pytest tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py
```

Expected: all focused tests and both complete files pass.

- [ ] **Step 9: Commit in-word synthesis handling**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py
git commit -m "feat: trace parameter and brace marker synthesis"
```

### Task 9: Structure branches, case fallthrough, and loop repetition

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_taint.py`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:510-540`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:600-1025`
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `tests/test_github_ci_shell_taint.py`

- [ ] **Step 1: Add branch, loop-binding, while-test, and case-fallthrough tests**

Append:

```python
def test_mutually_exclusive_if_branches_do_not_concatenate():
    script = """\
if test "$X" = yes; then
  printf doc-
else
  printf lattice
fi | bash
"""

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_for_loop_binding_and_repeat_compose_across_iterations():
    assert_taint_refusal(
        "for X in doc- lattice; do printf %s \"$X\"; done | bash"
    )


def test_select_loop_binding_uses_the_same_iteration_word_flow():
    assert_taint_refusal(
        "select X in doc- lattice; do printf %s \"$X\"; break; done | bash"
    )


def test_while_repeat_includes_initial_and_next_test_list_output():
    assert_taint_refusal(
        """\
i=0
P='#\\n'
while { printf %b "$P"; test "$i" -lt 1; }; do
  printf doc-
  P=lattice
  i=1
done | bash
"""
    )


def test_case_double_semicolon_arms_join_without_composing():
    script = """\
case "$X" in
  a) printf doc- ;;
  b) printf lattice ;;
esac | bash
"""

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_case_ampersand_fallthrough_sequences_following_arm():
    assert_taint_refusal(
        """\
case a in
  a) printf doc- ;&
  *) printf lattice ;;
esac | bash
"""
    )


def test_case_test_ampersand_fallthrough_is_conservatively_sequenced():
    assert_taint_refusal(
        """\
case a in
  a) printf doc- ;;&
  *) printf lattice ;;
esac | bash
"""
    )
```

- [ ] **Step 2: Run structured-control tests and verify the loop/case rows are red**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_mutually_exclusive_if_branches_do_not_concatenate \
  tests/test_github_ci_shell_scanner.py::test_for_loop_binding_and_repeat_compose_across_iterations \
  tests/test_github_ci_shell_scanner.py::test_select_loop_binding_uses_the_same_iteration_word_flow \
  tests/test_github_ci_shell_scanner.py::test_while_repeat_includes_initial_and_next_test_list_output \
  tests/test_github_ci_shell_scanner.py::test_case_double_semicolon_arms_join_without_composing \
  tests/test_github_ci_shell_scanner.py::test_case_ampersand_fallthrough_sequences_following_arm \
  tests/test_github_ci_shell_scanner.py::test_case_test_ampersand_fallthrough_is_conservatively_sequenced \
  -v
```

Expected: branch certification may already pass, while every refusal row fails because the scope is
still a blind sequence or lacks loop binding.

- [ ] **Step 3: Add one scanner control frame with explicit phase buffers**

Import `ChoiceOutput` and `RepeatOutput` with the existing output-expression imports.

Add to `shell_scanner.py`:

```python
@dataclass(slots=True)
class _ControlFrame:
    kind: str
    parent_outputs: list[OutputExpr]
    current_outputs: list[OutputExpr]
    test_outputs: list[OutputExpr] = field(default_factory=list)
    body_outputs: list[OutputExpr] = field(default_factory=list)
    branches: list[OutputExpr] = field(default_factory=list)
    case_arms: list[OutputExpr] = field(default_factory=list)
    case_terminators: list[str] = field(default_factory=list)
    loop_variable: str | None = None
    loop_values: tuple[ContentExpr, ...] = ()
    phase: str = "test"
```

Add `controls: list[_ControlFrame]` to `_ScopeFrame`. Initialize it with `controls=[]` in
`_scan_stream_scope`.

Add:

```python
def _output_target(self) -> list[OutputExpr]:
    frame = self.scope_stack[-1]
    if frame.controls:
        return frame.controls[-1].current_outputs
    return frame.outputs
```

Replace every direct `self.scope_stack[-1].outputs.append(...)` for a flushed command or completed
compound output with `self._output_target().append(...)`.

- [ ] **Step 4: Add exact output constructors for each control family**

Add:

```python
def _sequence_output(parts: list[OutputExpr]) -> SequenceOutput:
    return SequenceOutput(tuple(parts))


def _finish_if(frame: _ControlFrame) -> OutputExpr:
    branch = _sequence_output(frame.current_outputs)
    frame.branches.append(branch)
    return SequenceOutput(
        (
            _sequence_output(frame.test_outputs),
            ChoiceOutput(tuple(frame.branches)),
        )
    )


def _finish_loop(frame: _ControlFrame) -> OutputExpr:
    test = _sequence_output(frame.test_outputs)
    body = _sequence_output(frame.body_outputs)
    if frame.kind in {"for", "select"}:
        return RepeatOutput(body)
    return SequenceOutput(
        (
            test,
            RepeatOutput(SequenceOutput((body, test))),
        )
    )


def _case_chains(frame: _ControlFrame) -> tuple[OutputExpr, ...]:
    chains: list[OutputExpr] = []
    for start in range(len(frame.case_arms)):
        parts = [frame.case_arms[start]]
        index = start
        while (
            index < len(frame.case_terminators)
            and frame.case_terminators[index] in {";&", ";;&"}
            and index + 1 < len(frame.case_arms)
        ):
            index += 1
            parts.append(frame.case_arms[index])
        chains.append(
            parts[0] if len(parts) == 1 else SequenceOutput(tuple(parts))
        )
    return tuple(chains)


def _finish_case(frame: _ControlFrame) -> OutputExpr:
    if frame.current_outputs:
        frame.case_arms.append(
            _sequence_output(frame.current_outputs)
        )
    return ChoiceOutput(_case_chains(frame))
```

An empty test/body/arm is `SequenceOutput(())`, which is authored epsilon. `while` and `until`
produce `test (body test)*`, so the final failed test and every body-to-next-test boundary remain
representable.

- [ ] **Step 5: Open and switch `if`/`elif`/`else` phases at reserved words**

At command position in `_record_word`, before ordinary word recording:

```python
        if (
            not word.dynamic
            and word.keyword_eligible
            and command_position
            and word.literal == "if"
        ):
            parent = self._output_target()
            self.scope_stack[-1].controls.append(
                _ControlFrame("if", parent, [])
            )
```

When the scanner reaches a standalone `then`, move the current test outputs into
`frame.test_outputs` and set `current_outputs=[]`, `phase="then"`. At `elif`, append the completed
then branch, begin an elif test buffer, and at its `then` wrap that test plus branch as one
`SequenceOutput` alternative. At `else`, append the current branch and begin a new branch buffer.
At `fi`, call `_finish_if`, pop the frame, and append the result to `frame.parent_outputs`.

Suppress standalone `then`, `elif`, `else`, and `fi` from `_CommandEvidence`; commands in their
test and branch lists still flush normally and target the active buffer.

- [ ] **Step 6: Record `for`/`select` iteration words as joined variable writes**

When a command-position word is `for` or `select`, parse its header through the terminating `;` or
newline as:

```text
for-or-select NAME [in WORD ...]
```

Require a static shell name and bounded word ports. Create:

```python
_ControlFrame(
    kind=word.literal,
    parent_outputs=self._output_target(),
    current_outputs=[],
    loop_variable=name,
    loop_values=tuple(port.content for port in iteration_ports),
    phase="header",
)
```

Do not emit the header as a producer command. At `do`, set
`current_outputs=body_outputs` and `phase="body"`. At `done`, call `_finish_loop`, pop, and append
the `RepeatOutput` to the parent.

When `done` closes the frame, extend the active `_ScopeFrame.loop_bindings` with:

```python
tuple(
    _AssignmentEvidence(frame.loop_variable, value)
    for value in frame.loop_values
    if frame.loop_variable is not None
)
```

Competing iteration words are separate assignments and therefore join. They never become one
`Concat`.

- [ ] **Step 7: Structure `while` and `until` as test plus repeated body/test**

At a command-position `while` or `until`, open:

```python
_ControlFrame(
    kind=word.literal,
    parent_outputs=self._output_target(),
    current_outputs=[],
    phase="test",
)
```

Keep the reserved word in the current simple command so the existing prefix grammar still finds
doc-lattice invocations inside a test list, but route flushed output into `test_outputs`. At `do`,
switch to `body_outputs`. At `done`, use `_finish_loop`.

This must retain output from compound test lists such as
`while { printf ...; test ...; }; do`; a body-only `RepeatOutput` is not accepted.

- [ ] **Step 8: Turn case arms into Choice or fallthrough Sequence**

Extend `_CaseScanState` with:

```python
    control_depth: int | None = None
```

When `case` enters body phase, open a `_ControlFrame(kind="case", ...)`. On `;;`, `;&`, or `;;&` in
`_advance_case_body`:

```python
        control = self.scope_stack[-1].controls[-1]
        control.case_arms.append(
            _sequence_output(control.current_outputs)
        )
        control.case_terminators.append(operator)
        control.current_outputs = []
```

At `esac`, append the final arm, call `_finish_case`, pop, and append the result to the parent. A
plain `;;` ends a mutually exclusive alternative. `;&` and `;;&` extend a sequence to later arms.
If nesting makes it impossible to associate a terminator with the active case frame, raise:

```python
_ShellScanIncomplete("shell taint case fallthrough cannot be structured")
```

- [ ] **Step 9: Fail closed on unterminated structured frames**

Before freezing a `_ScopeFrame`, require `frame.controls` to be empty:

```python
    if finished.controls:
        raise _ShellScanIncomplete(
            "shell taint compound control flow cannot be structured"
        )
```

This is independent of phase-1 syntax scanning. It prevents an incomplete output structure from
being flattened into a sequence and certified.

- [ ] **Step 10: Run structured-control and complete scanner/pure suites**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_mutually_exclusive_if_branches_do_not_concatenate \
  tests/test_github_ci_shell_scanner.py::test_for_loop_binding_and_repeat_compose_across_iterations \
  tests/test_github_ci_shell_scanner.py::test_select_loop_binding_uses_the_same_iteration_word_flow \
  tests/test_github_ci_shell_scanner.py::test_while_repeat_includes_initial_and_next_test_list_output \
  tests/test_github_ci_shell_scanner.py::test_case_double_semicolon_arms_join_without_composing \
  tests/test_github_ci_shell_scanner.py::test_case_ampersand_fallthrough_sequences_following_arm \
  tests/test_github_ci_shell_scanner.py::test_case_test_ampersand_fallthrough_is_conservatively_sequenced \
  -v
uv run --no-sync pytest tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py
```

Expected: all focused tests and both complete files pass.

- [ ] **Step 11: Commit structured stream aggregation**

```bash
git add src/doc_lattice/github_ci/shell_taint.py \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py
git commit -m "feat: structure shell control flow taint"
```

### Task 10: Pin the full phase-2 matrix and audit exit behavior

**Files:**

- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `tests/test_github_ci_audit.py`
- Modify: `tests/cli/test_ci.py`

- [ ] **Step 1: Add the missing launcher, selector, and certification rows**

Append to `tests/test_github_ci_shell_scanner.py`:

```python
@pytest.mark.parametrize(
    "script",
    [
        """\
X=doc-
X+=lattice
builtin eval "$X"
""",
        """\
X=doc-
X+=lattice
command eval "$X"
""",
        """\
X=doc-
X+=lattice
uv run bash -c "$X"
""",
        """\
X=doc-
X+='lattice reconcile'
bash "$OPT" "$X"
""",
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; "
            "chmod +x task.sh; ./task.sh"
        ),
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; "
            "source ./task.sh"
        ),
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; "
            ". ./task.sh"
        ),
        (
            "printf %s doc- > task.sh; "
            "printf %s lattice >> task.sh; "
            "bash task.sh"
        ),
    ],
    ids=[
        "builtin-eval",
        "command-eval",
        "uv-run-shell",
        "ambiguous-selector",
        "direct-path",
        "source",
        "dot-source",
        "resource-append",
    ],
)
def test_phase_two_launcher_and_selector_sinks_refuse(script):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "echo 'make build' > run.sh; bash run.sh",
        "eval doc- lattice",
        """\
bash -c 'echo ok' <<'EOF'
doc-lattice reconcile
EOF
""",
        """\
X=doc-
X+=lattice
env eval "$X"
""",
        "bash -c 'echo hi' > doc-lattice.log",
        (
            "P=task.sh; "
            "printf '%s%s\\n' doc- 'lattice reconcile' > \"$P\"; "
            "bash task.sh"
        ),
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' 2> task.sh; "
            "bash task.sh"
        ),
    ],
    ids=[
        "marker-free-generated-script",
        "eval-space-barrier",
        "shell-c-ignores-stdin",
        "external-eval-lookup",
        "redirection-name-is-not-content",
        "dynamic-resource-identity",
        "stderr-does-not-carry-stdout-payload",
    ],
)
def test_phase_two_mandatory_certification_rows(script):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_phase_two_refusal_retains_resolved_findings_from_the_same_body():
    result = scan_doc_lattice_invocations(
        """\
doc-lattice check
X=doc-
X+=lattice
eval "$X"
"""
    )

    assert result.invocations == CHECK
    assert result.incomplete_reason == TAINT_REFUSAL_REASON
```

- [ ] **Step 2: Run the missing end-to-end rows**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_phase_two_launcher_and_selector_sinks_refuse \
  tests/test_github_ci_shell_scanner.py::test_phase_two_mandatory_certification_rows \
  tests/test_github_ci_shell_scanner.py::test_phase_two_refusal_retains_resolved_findings_from_the_same_body \
  -v
```

Expected: every row passes. A launcher refusal must use exactly `TAINT_REFUSAL_REASON`; an external
`env eval` lookup must certify.

- [ ] **Step 3: Add the canonical real-Bash refusal fixtures**

Add imports:

```python
import os
import shutil
import subprocess
from pathlib import Path
```

Add this table:

```python
PHASE_TWO_RUNTIME_REFUSALS = [
    (
        "file-handoff",
        "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; bash task.sh",
        {},
    ),
    (
        "dot-file-handoff",
        "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; bash ./task.sh",
        {},
    ),
    (
        "heredoc-handoff",
        "cat > task.sh <<'EOF'\ndoc-lattice reconcile\nEOF\nbash task.sh",
        {},
    ),
    (
        "variable-eval",
        "X=doc-; X+='lattice reconcile'; eval \"$X\"",
        {},
    ),
    (
        "pipeline",
        "printf '%s%s\\n' doc- 'lattice reconcile' | bash",
        {},
    ),
    (
        "herestring",
        "X=doc-; X+='lattice reconcile'; bash <<< \"$X\"",
        {},
    ),
    (
        "input-process-substitution",
        "bash < <(printf '%s%s\\n' doc- 'lattice reconcile')",
        {},
    ),
    (
        "multi-command-substitution",
        "eval \"$(printf doc-; printf 'lattice reconcile')\"",
        {},
    ),
    (
        "compound-group",
        "{ printf doc-; printf 'lattice reconcile'; } > task.sh; bash task.sh",
        {},
    ),
    (
        "parameter-default",
        "unset X; eval \"${X:-doc-}lattice reconcile\"",
        {},
    ),
    (
        "parameter-assign-default",
        "unset X; eval \"${X:=doc-}lattice\"",
        {},
    ),
    (
        "parameter-assigned-later",
        "unset X; : \"${X:=doc-}\"; X+=lattice; eval \"$X\"",
        {},
    ),
    (
        "brace-eval",
        "eval doc-{lattice,noop}",
        {},
    ),
    (
        "brace-pipeline",
        "printf %s {doc-,lattice} | bash",
        {},
    ),
    (
        "for-binding",
        "for X in doc- lattice; do printf %s \"$X\"; done | bash",
        {},
    ),
    (
        "case-fallthrough",
        "case a in a) printf doc- ;& *) printf lattice ;; esac | bash",
        {},
    ),
    (
        "static-stdin-read",
        "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; bash < task.sh",
        {},
    ),
    (
        "builtin-eval",
        "X=doc-; X+=lattice; builtin eval \"$X\"",
        {},
    ),
    (
        "uv-run-shell",
        "X=doc-; X+=lattice; uv run bash -c \"$X\"",
        {},
    ),
    (
        "ambiguous-selector",
        "X=doc-; X+='lattice reconcile'; bash \"$OPT\" \"$X\"",
        {"OPT": "-c"},
    ),
    (
        "substitution-newline-strip",
        "eval \"$(cat <<'EOF'\ndoc-\nEOF\n)lattice reconcile\"",
        {},
    ),
    (
        "while-test-list",
        (
            "i=0; P='#\\n'; "
            "while { printf %b \"$P\"; test \"$i\" -lt 1; }; "
            "do printf doc-; P=lattice; i=1; done | bash"
        ),
        {},
    ),
    (
        "final-descriptor-binding",
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' "
            "> /dev/null > task.sh; bash task.sh"
        ),
        {},
    ),
    (
        "direct-path",
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; "
            "chmod +x task.sh; ./task.sh"
        ),
        {},
    ),
    (
        "source",
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; "
            "source ./task.sh"
        ),
        {},
    ),
    (
        "dot-source",
        (
            "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; "
            ". ./task.sh"
        ),
        {},
    ),
    (
        "resource-append",
        (
            "printf %s doc- > task.sh; "
            "printf %s lattice >> task.sh; "
            "bash task.sh"
        ),
        {},
    ),
]
```

Add the runtime test:

```python
@pytest.mark.parametrize(
    ("_description", "script", "extra_environment"),
    PHASE_TWO_RUNTIME_REFUSALS,
    ids=[row[0] for row in PHASE_TWO_RUNTIME_REFUSALS],
)
def test_phase_two_refusal_fixture_executes_marker_under_real_bash(
    _description,
    script,
    extra_environment,
    tmp_path: Path,
):
    bash = shutil.which("bash")
    assert bash is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = tmp_path / "marker-ran"
    doc_lattice = bin_dir / "doc-lattice"
    doc_lattice.write_text(
        "#!/bin/sh\n: > \"$MARKER_PROBE\"\n",
        encoding="utf-8",
    )
    doc_lattice.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = run ] || exit 64\n"
        "shift\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    environment = {
        **os.environ,
        **extra_environment,
        "MARKER_PROBE": str(probe),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }

    scan = scan_doc_lattice_invocations(script)
    completed = subprocess.run(
        [bash, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert scan.invocations == NONE
    assert scan.incomplete_reason == TAINT_REFUSAL_REASON
    assert probe.exists(), completed.stderr
```

The local `uv` shim proves launcher selection without network access. The fake `doc-lattice`
records execution without loading project configuration or mutating repository state.

- [ ] **Step 4: Run the real-Bash fixture table**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_phase_two_refusal_fixture_executes_marker_under_real_bash \
  -v
```

Expected: every row passes both claims: real Bash reaches the fake executable, and the scanner
returns the exact phase-2 reason.

- [ ] **Step 5: Add audit integration and step-local boundary tests**

Append to `tests/test_github_ci_audit.py`:

```python
def test_global_audit_fails_closed_on_cross_command_file_handoff():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - shell: bash
        run: |
          printf '%s%s\\n' doc- 'lattice reconcile' > task.sh
          bash task.sh
"""
    )

    with pytest.raises(
        ConfigError,
        match=(
            r"shell scan incomplete: authored marker flow reaches "
            r"an execution sink"
        ),
    ):
        audit_global_workflows((document,))


def test_global_audit_does_not_aggregate_taint_across_run_steps():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - shell: bash
        run: printf '%s%s\\n' doc- 'lattice reconcile' > task.sh
      - shell: bash
        run: bash task.sh
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == set()
```

The second test is a disclosed boundary, not a trust assertion about the external file seen by the
second step.

- [ ] **Step 6: Add public CLI exit-2 coverage**

Append to `tests/cli/test_ci.py`:

```python
def test_ci_audit_cross_command_marker_handoff_exits_two(
    tmp_path: Path,
    monkeypatch,
):
    _install(tmp_path)
    workflow = tmp_path / ".github/workflows/cross-command-smuggle.yml"
    workflow.write_text(
        """\
name: cross-command smuggle
on: pull_request
permissions:
  contents: read
jobs:
  smuggle:
    runs-on: ubuntu-latest
    steps:
      - shell: bash
        run: |
          printf '%s%s\\n' doc- 'lattice reconcile' > task.sh
          bash task.sh
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["ci", "audit", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 2
    assert "CONFIG_ERROR" in result.stderr
    assert (
        "shell scan incomplete: authored marker flow reaches an execution sink"
        in result.stderr
    )
```

- [ ] **Step 7: Run focused scanner, audit, and CLI integration**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_phase_two_launcher_and_selector_sinks_refuse \
  tests/test_github_ci_shell_scanner.py::test_phase_two_mandatory_certification_rows \
  tests/test_github_ci_shell_scanner.py::test_phase_two_refusal_fixture_executes_marker_under_real_bash \
  tests/test_github_ci_audit.py::test_global_audit_fails_closed_on_cross_command_file_handoff \
  tests/test_github_ci_audit.py::test_global_audit_does_not_aggregate_taint_across_run_steps \
  tests/cli/test_ci.py::test_ci_audit_cross_command_marker_handoff_exits_two \
  -v
```

Expected: every selected test passes.

- [ ] **Step 8: Run the complete live scanner and audit suites**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_audit.py \
  tests/cli/test_ci.py
```

Expected: all four files pass. Existing phase-1 refusal reasons, resolved findings, and audit policy
findings remain unchanged.

- [ ] **Step 9: Commit the live acceptance matrix**

```bash
git add tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_audit.py \
  tests/cli/test_ci.py
git commit -m "test: pin cross-command marker taint contract"
```

### Task 11: Publish the step-local contract and AD-18

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1662-1687`
- Modify: `README.md:645-670`
- Modify: `ARCHITECTURE.md`
- Include: `docs/superpowers/plans/2026-07-24-cross-command-marker-taint.md`

- [ ] **Step 1: Update scanner documentation to match implemented behavior**

Replace the module docstring with:

```python
"""Bounded scanner for direct doc-lattice invocations and authored marker flow."""
```

Replace the limitation paragraph in `direct_doc_lattice_invocations` with:

```python
    After the command-local resolver pass, a pure bounded taint pass evaluates authored content
    flow within this one shell body. It refuses when authored fragments can compose the ASCII
    doc-lattice marker along a modeled variable, stream, or static-resource edge and that content
    reaches an execution sink. External content is represented as absence of authored evidence,
    never as a claim that the content is inert.

    The scanner intentionally does not aggregate across run steps or model aliases, functions,
    PATH shadowing, external files or environment content, dynamic resource identity, arbitrary
    encoding/transform programs, descriptor aliasing, actions, or reusable workflows.
```

Keep the argument and exception documentation unchanged.

- [ ] **Step 2: Replace the README cross-command limitation with the new contract**

In `README.md`, replace the paragraph beginning “Executable classification is syntactic basename
resolution” and its old cross-command limitation with:

```markdown
Executable classification is syntactic basename resolution, not proof of runtime identity. Within
each individual `run:` body, audit also evaluates authored marker flow after the command-local
resolver pass. Certification means no authored fragments compose the ASCII
`doc[-_.]+lattice` marker along a modeled content flow and reach an execution sink in that body.
Modeled flows include variable assignment and append, producer stdout, pipes, heredocs,
herestrings, command and process substitutions, static file writes and reads, shell script/stdin/
`-c` source selection, `eval`, `source`/`.`, bounded parameter alternatives and brace argv
fan-out, structured stream scopes, loop binding/repetition, and descriptor-aware final bindings.
Resolved doc-lattice invocations and the phase-1 retained-word refusals keep their existing
outcomes.

This contract is step-local and marker-anchored, not a general proof that dynamic shell execution
is safe. Audit does not aggregate across steps, jobs, `uses:` actions, or reusable workflows. It
also has no authored evidence for external environment values, external files, or unresolved
producer output beyond the generic may-output rule; for encoding or transform synthesis such as
`base64 -d`, `tr`, or `sed`; for dynamic path identity, `..`, `cd`, rename, or symlink aliases; or
for file-descriptor duplication and movement. Unsupported parameter transforms surface their
authored literal operands but do not model the variable-derived transform. Function, alias,
`PATH`, and dynamic executable-name shadowing remain the command-identity limitations. These are
absence-of-evidence boundaries, not trust claims: for example `curl ... | bash`,
`eval "$EXTERNAL"`, a marker-free generated script, and `doc${EXTERNAL}lattice` still certify.
Malformed, oversized, cap-exhausting, or otherwise unreliably structured input exits 2.
```

Keep the following secret-access and bootstrap paragraphs unchanged.

- [ ] **Step 3: Add durable architecture decision AD-18**

Append:

```markdown
### AD-18: CI shell certification follows authored marker flow within one run body

**Date:** 2026-07-24
**Status:** Accepted
**Context:** AD-17 rejects a complete retained-word marker under every unresolved command, but a
shell body can author marker fragments in separate commands and later execute their composed
content through a variable, stream, file, or expansion. Treating all unknown dynamic content as
unsafe would replace the marker-anchored policy with a general dynamic-execution ban, while
treating it as inert would assert evidence the scanner does not have.
**Decision:** Certification remains scoped to one `run:` body and anchored to authored
`doc[-_.]+lattice` content. The parser emits immutable command, redirection, process-resource, and
stream-scope evidence with monotonic IDs. Typed ports keep argv, assignments, stdin, stdout, and
static-resource content separate. A pure taint module builds `LiteralTransfer`, variable, stream,
resource, `Choice`, `Concat`, and `OutsideGap` expressions, then evaluates them with a fixed marker
DFA. Sequential adjacency uses relational composition; competing definitions, truncating writes,
and mutually exclusive alternatives use set union, so unrelated fragments never concatenate.
`OutsideGap` contributes epsilon and an opaque non-authored barrier, which permits only
authored-only marker paths.

Stream scopes aggregate command stdout with `Sequence`, `Choice`, and reflexive-transitive
`Repeat`; command substitution alone strips trailing newlines with a finite suffix-aware transfer
summary. Parameter default/alternate forms produce in-word choices, assign-default also emits a
conditional variable definition, and bounded static brace expansion fans one lexical word into
ordered argv ports. `for`/`select` iteration words join into loop-variable evidence;
`while`/`until` repeat the test list around each body iteration; `case` `;&` and `;;&` preserve
fallthrough sequence. Ordered descriptor replay installs pipeline endpoints first and then applies
redirections left to right, so only final descriptor bindings route bytes while earlier truncations
retain their empty-file side effect.

Execution sinks are `eval`, shell `-c`, selected shell stdin, and static script execution through a
shell operand, direct path, `source`, or `.`. Effective-head evidence comes from the complete
existing assignment, keyword, `builtin`, `command`, `exec`, `env`, `time`, `coproc`, `uv run`,
`uvx`, and `uv tool run` grammar. Its `external_lookup` provenance prevents an external `env eval`
from being mistaken for the shell builtin. The shell source selector chooses `-c`, a script
operand, or stdin; if a dynamic selector could choose a marker-capable authored port, it fails
closed.

Variable, resource, and stream references are solved by monotone least fixed point, independent of
source order. Alternative width, expression nodes, table entries, graph edges, brace expansion,
and successful fixed-point updates have deterministic caps; every exhaustion fails closed. The
absence-of-evidence boundary is cross-step/job/action/workflow flow, external values and files
beyond generic may-output, arbitrary encoding/transforms, dynamic resource aliases, unsupported
parameter transforms beyond their authored operands, descriptor aliasing, and AD-17's function,
alias, `PATH`, and dynamic-executable limitations.
**Consequences:** Split variable, pipe, heredoc/herestring, substitution, and static-file handoffs
that execute authored marker content now exit 2, including launcher-wrapped and ambiguous-selector
sinks. Marker-free dynamic execution and a marker whose required character comes only from
external content continue to certify with the boundary disclosed. `audit.py` still invokes the
scanner independently for each step; future job-level aggregation can consume the evidence shape
without changing the parser/analysis ownership boundary.
```

- [ ] **Step 4: Check documentation links, ownership, and formatting**

Run:

```bash
uv run --no-sync python - <<'PY'
import re
from pathlib import Path

paths = (
    Path("README.md"),
    Path("ARCHITECTURE.md"),
    Path("docs/superpowers/plans/2026-07-24-cross-command-marker-taint.md"),
)
pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
for document in paths:
    for target in pattern.findall(document.read_text(encoding="utf-8")):
        clean = target.split("#", 1)[0]
        if not clean or "://" in clean or clean.startswith("mailto:"):
            continue
        resolved = (document.parent / clean).resolve()
        assert resolved.exists(), (document, target)
print("PASS: relative Markdown links resolve")
PY
git diff --check
uv run --no-sync python scripts/check_version_sync.py
git diff --name-only | rg '^CHANGELOG.md$'
```

Expected: the link check prints `PASS`, diff check and version sync exit 0, and the final command
exits 1 with no output, confirming no changelog edit.

- [ ] **Step 5: Commit durable documentation and the implementation plan**

```bash
git add src/doc_lattice/github_ci/shell_scanner.py \
  README.md \
  ARCHITECTURE.md \
  docs/superpowers/plans/2026-07-24-cross-command-marker-taint.md
git commit -m "docs: record cross-command marker taint"
```

### Task 12: Run the read-only corpus battery and full handoff verification

**Files:**

- Read only:
  `.worktrees/successor-evaluation/tests/fixtures/github_ci_successor_checkpoint/corpus/new_fixtures.json`
- Verify: entire repository

- [ ] **Step 1: Re-run the predecessor corpus inputs against live outcomes**

Run:

```bash
uv run --no-sync python - <<'PY'
import json
from pathlib import Path

from doc_lattice.github_ci.shell_scanner import scan_doc_lattice_invocations
from doc_lattice.github_ci.shell_taint import TAINT_REFUSAL_REASON

fixture = Path(
    ".worktrees/successor-evaluation/tests/fixtures/"
    "github_ci_successor_checkpoint/corpus/new_fixtures.json"
)
families = json.loads(fixture.read_text(encoding="utf-8"))["families"]

certified = {
    ("dispatcher", "marker-free-dispatch"): (),
    ("look_alike", "exe-head-certifies"): (("check", False),),
    ("look_alike", "casefold-head-certifies"): (("lint", False),),
}
phase_one_refuse = {
    ("dispatcher", row["id"])
    for row in families["dispatcher"]
    if row["id"] != "marker-free-dispatch"
} | {
    ("look_alike", "underscore-head"),
    ("look_alike", "dotted-wrapper-head"),
}

seen = set()
for family in ("dispatcher", "look_alike"):
    for row in families[family]:
        key = (family, row["id"])
        result = scan_doc_lattice_invocations(row["source"])
        if key in certified:
            assert result.incomplete_reason is None, (key, result)
            assert result.invocations == certified[key], (key, result)
        else:
            assert key in phase_one_refuse, key
            assert result.incomplete_reason is not None, (key, result)
            assert result.invocations == (), (key, result)
        seen.add(key)
assert seen == set(certified) | phase_one_refuse
print(f"PASS: {len(seen)} predecessor corpus inputs retain live outcomes")

phase_two_refuse = {
    "file": "printf '%s%s\\n' doc- lattice > task.sh; bash task.sh",
    "variable": "X=doc-; X+=lattice; eval \"$X\"",
    "pipe": "printf %s doc- lattice | bash",
    "process": "bash < <(printf %s doc- lattice)",
    "substitution": "eval \"$(printf doc-; printf lattice)\"",
    "parameter": "unset X; eval \"${X:-doc-}lattice\"",
    "brace": "printf %s {doc-,lattice} | bash",
    "loop": "for X in doc- lattice; do printf %s \"$X\"; done | bash",
    "fallthrough": (
        "case a in a) printf doc- ;& *) printf lattice ;; esac | bash"
    ),
    "launcher": "X=doc-; X+=lattice; uv run bash -c \"$X\"",
}
phase_two_certify = {
    "external-producer": "curl https://example.invalid | bash",
    "eval-space": "eval doc- lattice",
    "external-separator": "eval \"doc${EXTERNAL}lattice\"",
    "process-nonsink": "grep x <(printf %s doc- lattice)",
    "external-eval": "X=doc-; X+=lattice; env eval \"$X\"",
    "fd-three": (
        "printf %s doc- lattice > task.sh; bash 3< task.sh"
    ),
    "overwritten-output": (
        "printf %s doc- lattice > task.sh > /dev/null; bash task.sh"
    ),
    "overridden-pipe": "printf %s doc- lattice | bash <<<'true'",
}
for case_id, source in phase_two_refuse.items():
    result = scan_doc_lattice_invocations(source)
    assert result.invocations == (), (case_id, result)
    assert result.incomplete_reason == TAINT_REFUSAL_REASON, (case_id, result)
for case_id, source in phase_two_certify.items():
    result = scan_doc_lattice_invocations(source)
    assert result.invocations == (), (case_id, result)
    assert result.incomplete_reason is None, (case_id, result)
print(
    "PASS: "
    f"{len(phase_two_refuse) + len(phase_two_certify)} "
    "phase-2 corpus outcomes match the authored-flow contract"
)
PY
```

Expected:

```text
PASS: 12 predecessor corpus inputs retain live outcomes
PASS: 18 phase-2 corpus outcomes match the authored-flow contract
```

Keep these results in the session. Do not edit the frozen fixture or add derived corpus output.

- [ ] **Step 2: Run focused shell and audit verification**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_taint.py \
  tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_audit.py \
  tests/cli/test_ci.py
```

Expected: all focused implementation and integration tests pass.

- [ ] **Step 3: Run the complete test suite and coverage gate**

Run:

```bash
uv run --no-sync pytest
```

Expected: all tests pass and total branch coverage meets the configured 80 percent threshold.

- [ ] **Step 4: Run lint, format, typing, and repository policy checks**

Run:

```bash
uv run --no-sync ruff check src/ tests/
uv run --no-sync ruff format --check src/ tests/
uv run --no-sync ty check src/
uv run --no-sync python scripts/check_typing_boundaries.py src/
uv run --no-sync python scripts/check_version_sync.py
git diff --check
```

Expected: every command exits 0. The typing-boundary script prints:

```text
PASS: typing.Any/typing.cast restricted to boundary modules
```

- [ ] **Step 5: Confirm the intended file boundary and immutable inputs**

Run:

```bash
uv run --no-sync python - <<'PY'
import subprocess

expected = {
    "ARCHITECTURE.md",
    "README.md",
    "docs/superpowers/plans/2026-07-24-cross-command-marker-taint.md",
    "docs/superpowers/specs/2026-07-24-cross-command-marker-taint-design.md",
    "src/doc_lattice/github_ci/shell_scanner.py",
    "src/doc_lattice/github_ci/shell_taint.py",
    "tests/cli/test_ci.py",
    "tests/test_github_ci_audit.py",
    "tests/test_github_ci_shell_scanner.py",
    "tests/test_github_ci_shell_taint.py",
}
changed = set(
    subprocess.check_output(
        ["git", "diff", "--name-only", "main...HEAD"],
        text=True,
    ).splitlines()
)
assert changed == expected, (changed - expected, expected - changed)
print("PASS: branch changes match the planned file boundary")
PY
git status --short
git diff --name-only main...HEAD -- \
  CHANGELOG.md \
  src/doc_lattice/github_ci/audit.py \
  tests/fixtures/github_ci_checkpoint \
  .worktrees/successor-evaluation
```

Expected: the Python check prints `PASS`; both Git commands produce no output. The successor
worktree, frozen checkpoints, `audit.py`, and `CHANGELOG.md` remain unchanged.

- [ ] **Step 6: Push the completed branch and open a draft pull request**

Run:

```bash
git push -u origin feat/issue-110-cross-command-marker-taint
gh pr create \
  --draft \
  --base main \
  --head feat/issue-110-cross-command-marker-taint \
  --title "feat(ci): detect cross-command marker taint" \
  --body '## Summary

- detect authored `doc[-_.]+lattice` content across modeled flows in one `run:` body
- preserve phase-1 invocation findings and refusal outcomes
- document the step-local evidence boundary in README and AD-18

## Verification

- `uv run --no-sync pytest`
- `uv run --no-sync ruff check src/ tests/`
- `uv run --no-sync ruff format --check src/ tests/`
- `uv run --no-sync ty check src/`
- `uv run --no-sync python scripts/check_typing_boundaries.py src/`
- `uv run --no-sync python scripts/check_version_sync.py`
- predecessor corpus battery from the implementation plan

## Checklist

- [x] Tests pass
- [x] Docs updated
- [x] CHANGELOG intentionally unchanged because `ci audit` is unreleased
- [x] No new convention violations

Closes #110'
gh pr view \
  --json isDraft,baseRefName,headRefName,title,url
```

Expected: the push succeeds, `gh pr create` prints the pull-request URL, and `gh pr view` reports
`isDraft: true`, base `main`, head `feat/issue-110-cross-command-marker-taint`, and title
`feat(ci): detect cross-command marker taint`.
