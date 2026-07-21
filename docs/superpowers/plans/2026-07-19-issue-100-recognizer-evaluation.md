# Issue #100 Recognizer Evaluation Implementation Plan (Plan 2, PR A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the D3 floor-grammar recognizer and every predeclared evaluation harness and
gate, run the full evaluation against the frozen checkpoint, and land the decision record that
documents the predetermined Tier 3B rejection and advances the mvdan/sh-family candidate.

**Architecture:** Three new pure dormant modules (`reachability.py`,
`direct_marker_scanner.py`, `launcher_policy.py`) plus dormant model/constants types; a test-side
evaluation harness that orchestrates D1 pruning and D6 composition without touching runtime
audit behavior; pytest gates 1 through 9 (CI halves) that assert the recorded evaluation
outcome; a fleetyard-only wall-clock benchmark; the archived July 2026 parser benchmark; and the
final decision record. Per the owner's ruling, gate 6 asserts the candidate's REJECTED verdict
(dual Tier 3B budget breach at 3/20 against both caps of 2), so CI is green while the evaluation
outcome is honestly encoded.

**Tech Stack:** Python 3.13+ via uv, pytest, `shfmt-py==4.0.0` (new dev-group dependency,
gate 7 only), system Bash 5.2.21 (pinned by the checkpoint), `gh` CLI (PR A opening).

## Global Constraints

- Spec is authoritative: `docs/superpowers/specs/2026-07-19-allowlist-recognizer-design.md`.
  Baseline SHA for all accounting: `00737ca`. The predeclaration checkpoint (commit `7d1d2b2`,
  owner-reviewed) is frozen: nothing under `tests/fixtures/github_ci_checkpoint/` may change,
  enforced by `test_manifest_matches_artifacts`. If any checkpoint file must change, STOP; per
  the spec the evaluation restarts from a new checkpoint commit.
- Work on branch `feature/issue-100-allowlist`. Commit after every task. Never modify
  `src/doc_lattice/github_ci/shell_scanner.py` or `tests/test_github_ci_shell_scanner.py`:
  the old scanner is the frozen compatibility baseline for this evaluation.
- Run Python through uv with the dev-shell quirks neutralized:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest ...`.
- Every new `.py` file: module docstring, Google-style docstrings on public functions, Ruff
  100-char lines, no `typing.Any`, no `typing.cast`, no bare `except Exception`, no
  `datetime.now()`. No em dashes in any drafted content. No Claude attribution in commits.
- Marker regex everywhere: `re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)`.
- Status vocabulary: `not_applicable`, `certified`, `uninspectable`. Reason-category
  vocabulary: `control-character`, `unsupported-operator`, `unsupported-expansion`,
  `unquoted-expansion-in-command-word`, `quote-spans-newline`, `unterminated-quote`,
  `control-flow-keyword`, `assignment-prefix`, `unstable-first-word`, `policy-unresolvable`,
  `cap-exceeded`. After Task 1 these exist in `src/doc_lattice/constants.py`; import them, never
  restate raw strings in source modules.
- Bounds (checkpoint `limits.json`, all enforced): source cap 1,048,576 chars; invocation cap
  10,000; token cap 262,144; statement cap 65,536; work limit
  `min(4194304, 4 * source_length + 4096)` with charges: marker pass charges `source_length`
  once, one per character the tokenizer examines, one per token emitted, one per statement
  closed, one per policy step.
- Owner adjudications binding on this plan (from `.superpowers/sdd/progress.md`):
  - Ruling 1a: the D3 candidate is built correct-but-not-polished and the full evaluation runs.
    The Tier 3B gate asserts the RECORDED REJECTED verdict (3/20 total indeterminate and 3/20
    newly indeterminate, both over the predeclared caps of 2); every other gate asserts pass.
    All gates must be green in CI.
  - `ci audit` resolves as the invocation `("ci", False)`.
  - Monotonic evidence: a command whose scan fails mid-command retains NO invocation from that
    command; only cleanly-terminated prior statements contribute invocations.
  - An interior backslash inside a double-quoted string is category `unsupported-operator`.
  - Probes use static-verdict semantics: `expected_verdict` in `probes.json` is the verdict the
    recognizer must assign to the span text.
  - Acceptance case 74 (`runtime-unreachable command remains conservative`) certifies with the
    retained non-dry disposition `[("linear", False)]`; case 78 retains monotonic
    `[("check", False)]`.
- The checkpoint artifacts are the executable oracle. Key facts used across tasks:
  - `acceptance_labels.json`: 78 rows aligned with `ACCEPTANCE_CASES`
    (`tests/test_github_ci_shell_scanner.py:28`); 7 must-certify, 65 intentional-exit-2,
    6 outside-direct-marker-contract.
  - `replay_inventory.json`: 580 entries `{id, sha256, source}` sorted by sha256.
  - `tier3a_cases.json`: 13 cases; `tier3b/`: 20 fixtures + `provenance.json` (17 certified,
    3 uninspectable: fixture-02 `unsupported-expansion`, fixture-05 `unsupported-operator`,
    fixture-14 `unsupported-expansion`).
  - `probes.json`: 36 spans, env map
    `{"CFG": "/tmp/probe-cfg.yml", "OPTION": "--verbose", "RUNNER_TEMP": "/tmp/probe-rt",
    "FLAG": "--dry-run", "name": "pkg", "image": "amd64"}`.
  - `mutations.json`: 10 kinds, 50 sites over must-certify probe spans.
  - `bash_pin.json`: version `5.2.21(1)-release` plus container digest and local binary hash.
  - `category_d_exceptions.json`: `[]` (replay category (d) must be empty).
  - `benchmark_protocol.md`: the fleetyard wall-clock protocol (Task 12).
- Coverage floor 80 percent holds for the full suite. Run focused tests while iterating; the
  full verification set runs in Task 14.

## File Structure

- `src/doc_lattice/constants.py` (modify): new Literal domains (Task 1).
- `src/doc_lattice/github_ci/model.py` (modify): `BlockScan`, `AuditDiagnostic`,
  `diagnostic_sort_key`, `AuditResult` (Task 1).
- `src/doc_lattice/github_ci/reachability.py` (create): D1 predicate (Task 2).
- `src/doc_lattice/github_ci/launcher_policy.py` (create): word IR + policy (Task 3).
- `src/doc_lattice/github_ci/direct_marker_scanner.py` (create): marker gate + tokenizer +
  grammar + `scan_execution_source` (Tasks 4 and 5).
- `tests/github_ci_evaluation_harness.py` (create): checkpoint loaders, old-scanner
  normalization, divergence classifier, D1+D6 evaluation orchestrator (Tasks 6 and 7).
- `tests/test_github_ci_model.py`, `tests/test_constants.py` (modify): Task 1 mirrors.
- `tests/test_github_ci_reachability.py`, `tests/test_github_ci_launcher_policy.py`,
  `tests/test_github_ci_direct_marker_scanner.py` (create): module mirrors (Tasks 2 to 5).
- `tests/test_github_ci_evaluation_gates.py` (create): gates 1 to 6, 8, and the CI half of 9
  (Tasks 5 to 10).
- `tests/test_github_ci_semantic_differential.py` (create): gate 7 (Task 9).
- `scripts/bench_recognizer_replay.py` (create): gate 9 wall-clock benchmark (Task 12).
- `docs/research/` (create): July 2026 parser benchmark archive (Task 11).
- `docs/superpowers/specs/2026-07-19-allowlist-recognizer-decision.md` (create): decision
  record (Task 13).
- `pyproject.toml` (modify): add `shfmt-py==4.0.0` to `[dependency-groups].dev` (Task 9).

---

### Task 1: Dormant contract types (constants + model)

**Files:**
- Modify: `src/doc_lattice/constants.py` (append after the existing domains)
- Modify: `src/doc_lattice/github_ci/model.py` (append)
- Modify: `tests/test_constants.py` (append)
- Modify: `tests/test_github_ci_model.py` (append)

**Interfaces:**
- Produces (constants): `BlockScanStatus`, `VALID_BLOCK_SCAN_STATUSES`, `ScanReasonCategory`,
  `VALID_SCAN_REASON_CATEGORIES`, `AuditSourceKind`, `VALID_AUDIT_SOURCE_KINDS`,
  `AuditDiagnosticCode`, `VALID_AUDIT_DIAGNOSTIC_CODES`.
- Produces (model): `BlockScan(status, invocations, reason_category, reason, offset,
  work_charged)` with D4 invariants enforced in `__post_init__`;
  `AuditDiagnostic(path, job_id, step_index, source_kind, code, reason, offset)`;
  `diagnostic_sort_key(diagnostic) -> tuple`; `AuditResult(findings, diagnostics)`.
  All frozen slotted dataclasses. Every later task consumes these exact names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_constants.py`:

```python
def test_block_scan_domains_are_frozen():
    from doc_lattice.constants import (
        VALID_AUDIT_DIAGNOSTIC_CODES,
        VALID_AUDIT_SOURCE_KINDS,
        VALID_BLOCK_SCAN_STATUSES,
        VALID_SCAN_REASON_CATEGORIES,
    )

    assert VALID_BLOCK_SCAN_STATUSES == {"not_applicable", "certified", "uninspectable"}
    assert VALID_SCAN_REASON_CATEGORIES == {
        "control-character",
        "unsupported-operator",
        "unsupported-expansion",
        "unquoted-expansion-in-command-word",
        "quote-spans-newline",
        "unterminated-quote",
        "control-flow-keyword",
        "assignment-prefix",
        "unstable-first-word",
        "policy-unresolvable",
        "cap-exceeded",
    }
    assert VALID_AUDIT_SOURCE_KINDS == {"shell_template", "run_body"}
    assert VALID_AUDIT_DIAGNOSTIC_CODES == {
        "UNINSPECTABLE_SOURCE",
        "UNSUPPORTED_EXECUTION_SEMANTICS",
    }
```

Append to `tests/test_github_ci_model.py`:

```python
import pytest

from doc_lattice.github_ci.model import (
    AuditDiagnostic,
    AuditResult,
    BlockScan,
    diagnostic_sort_key,
)


def test_block_scan_invariants():
    BlockScan("not_applicable", (), None, None, None, 12)
    BlockScan("certified", (("check", False),), None, None, None, 30)
    BlockScan("uninspectable", (), "unsupported-operator", "unquoted pipe", 4, 9)
    BlockScan(
        "uninspectable", (("check", False),), "unsupported-operator", "unquoted pipe", 20, 40
    )
    with pytest.raises(ValueError):
        BlockScan("not_applicable", (("check", False),), None, None, None, 1)
    with pytest.raises(ValueError):
        BlockScan("not_applicable", (), None, "reason", None, 1)
    with pytest.raises(ValueError):
        BlockScan("certified", (), None, "reason", None, 1)
    with pytest.raises(ValueError):
        BlockScan("certified", (), "unsupported-operator", None, None, 1)
    with pytest.raises(ValueError):
        BlockScan("uninspectable", (), None, None, None, 1)
    with pytest.raises(ValueError):
        BlockScan("uninspectable", (), "unsupported-operator", "reason", None, 1)
    with pytest.raises(ValueError):
        BlockScan("uninspectable", (), None, "reason", 3, 1)


def test_diagnostic_sort_key_orders_missing_offsets_first():
    with_offset = AuditDiagnostic(
        "a.yml", "job", 0, "run_body", "UNINSPECTABLE_SOURCE", "reason", 7
    )
    without_offset = AuditDiagnostic(
        "a.yml", "job", 0, "run_body", "UNINSPECTABLE_SOURCE", "reason", None
    )
    ordered = sorted([with_offset, without_offset], key=diagnostic_sort_key)
    assert ordered == [without_offset, with_offset]


def test_audit_result_holds_both_lists():
    result = AuditResult(findings=(), diagnostics=())
    assert result.findings == ()
    assert result.diagnostics == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_constants.py tests/test_github_ci_model.py -q --no-cov`
Expected: FAIL (ImportError on the new names).

- [ ] **Step 3: Implement the constants**

Append to `src/doc_lattice/constants.py`, matching the file's existing pattern exactly:

```python
BlockScanStatus = Literal["not_applicable", "certified", "uninspectable"]
VALID_BLOCK_SCAN_STATUSES: frozenset[str] = frozenset(get_args(BlockScanStatus))

ScanReasonCategory = Literal[
    "control-character",
    "unsupported-operator",
    "unsupported-expansion",
    "unquoted-expansion-in-command-word",
    "quote-spans-newline",
    "unterminated-quote",
    "control-flow-keyword",
    "assignment-prefix",
    "unstable-first-word",
    "policy-unresolvable",
    "cap-exceeded",
]
VALID_SCAN_REASON_CATEGORIES: frozenset[str] = frozenset(get_args(ScanReasonCategory))

AuditSourceKind = Literal["shell_template", "run_body"]
VALID_AUDIT_SOURCE_KINDS: frozenset[str] = frozenset(get_args(AuditSourceKind))

AuditDiagnosticCode = Literal["UNINSPECTABLE_SOURCE", "UNSUPPORTED_EXECUTION_SEMANTICS"]
VALID_AUDIT_DIAGNOSTIC_CODES: frozenset[str] = frozenset(get_args(AuditDiagnosticCode))
```

- [ ] **Step 4: Implement the model types**

Append to `src/doc_lattice/github_ci/model.py` (extend the existing import from
`doc_lattice.constants` if one exists; otherwise add
`from doc_lattice.constants import AuditDiagnosticCode, AuditSourceKind, BlockScanStatus,
ScanReasonCategory`):

```python
@dataclass(frozen=True, slots=True)
class BlockScan:
    """Certification outcome for one execution source under the D4 contract.

    Invariants (spec D4): not_applicable carries no invocations and no reason; certified
    carries no reason; uninspectable requires reason, reason_category, and offset, and may
    retain invocations proven before the failure (monotonic evidence).
    """

    status: BlockScanStatus
    invocations: tuple[tuple[str, bool], ...]
    reason_category: ScanReasonCategory | None
    reason: str | None
    offset: int | None
    work_charged: int

    def __post_init__(self) -> None:
        """Enforce the D4 status invariants at construction time."""
        if self.status == "not_applicable" and (
            self.invocations or self.reason is not None or self.reason_category is not None
        ):
            raise ValueError("not_applicable blocks carry no invocations and no reason")
        if self.status == "certified" and (
            self.reason is not None or self.reason_category is not None
        ):
            raise ValueError("certified blocks carry no reason")
        if self.status == "uninspectable" and (
            self.reason is None or self.reason_category is None or self.offset is None
        ):
            raise ValueError("uninspectable blocks require reason, category, and offset")


@dataclass(frozen=True, slots=True)
class AuditDiagnostic:
    """One uninspectability or composition diagnostic attributed to an execution source."""

    path: str
    job_id: str
    step_index: int
    source_kind: AuditSourceKind
    code: AuditDiagnosticCode
    reason: str
    offset: int | None


def diagnostic_sort_key(diagnostic: AuditDiagnostic) -> tuple[str, str, int, str, str, int]:
    """Return the D5 deterministic ordering key; diagnostics without offsets sort first."""
    return (
        diagnostic.path,
        diagnostic.job_id,
        diagnostic.step_index,
        diagnostic.source_kind,
        diagnostic.code,
        -1 if diagnostic.offset is None else diagnostic.offset,
    )


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Aggregated repository audit outcome under the D5 contract."""

    findings: tuple[AuditFinding, ...]
    diagnostics: tuple[AuditDiagnostic, ...]
```

- [ ] **Step 5: Run the tests, then commit**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_constants.py tests/test_github_ci_model.py -q --no-cov`
Expected: PASS.

```bash
git add src/doc_lattice/constants.py src/doc_lattice/github_ci/model.py tests/test_constants.py tests/test_github_ci_model.py
git commit -m "feat: add dormant BlockScan and audit aggregation types for issue #100"
```

---

### Task 2: The D1 reachability predicate

**Files:**
- Create: `src/doc_lattice/github_ci/reachability.py`
- Create: `tests/test_github_ci_reachability.py`

**Interfaces:**
- Produces: `job_is_pr_reachable(if_condition: str | None, event_names: frozenset[str]) ->
  bool`. `event_names` is the document's trigger names intersected with `audit.PR_EVENTS`
  (the caller computes the intersection). Returns False only when the condition is provably
  false for every event in `event_names`; empty `event_names` returns False (no PR event
  reaches the job). Task 7's orchestrator consumes this exact signature.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_ci_reachability.py`:

```python
"""Tests for the D1 PR-reachability predicate."""

import pytest

from doc_lattice.github_ci.reachability import job_is_pr_reachable

PR = frozenset({"pull_request", "pull_request_review", "pull_request_review_comment"})


@pytest.mark.parametrize(
    ("condition", "events", "expected"),
    [
        (None, PR, True),
        ("", PR, True),
        ("github.event_name == 'push'", PR, False),
        ("github.event_name == 'pull_request'", PR, True),
        ("'push' == github.event_name", PR, False),
        ("${{ github.event_name == 'push' }}", PR, False),
        ("${{ github.event_name == 'PUSH' }}", PR, False),
        ("GITHUB.EVENT_NAME == 'push'", PR, False),
        (
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
            PR,
            False,
        ),
        ("github.ref == 'refs/heads/main'", PR, True),
        (
            "github.event_name == 'pull_request' && github.ref == 'refs/heads/main'",
            PR,
            True,
        ),
        ("github.event_name == 'push' || github.event_name == 'pull_request'", PR, True),
        ("github.event_name != 'push'", PR, True),
        ("!(github.event_name == 'push')", PR, True),
        ('github.event_name == "push"', PR, True),
        ("github.event_name == 'push' &&", PR, True),
        ("${{ github.event_name == 'push'", PR, True),
        ("github.event_name == 'push'", frozenset({"pull_request"}), False),
        ("github.event_name == 'pull_request'", frozenset(), False),
        (None, frozenset(), False),
    ],
)
def test_job_is_pr_reachable(condition, events, expected):
    assert job_is_pr_reachable(condition, events) is expected
```

Behavior notes encoded above: `||` anywhere, inequality, negation, double-quoted expression
literals, dangling `&&`, and an unterminated `${{` wrapper are all structural or atom-level
unknowns, so the job stays scanned (True). One conclusively false conjunct proves the condition
false for that event; the job is pruned only when that holds for every event. Empty
`event_names` means no PR event triggers the document, so the job is unreachable (False).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_reachability.py -q --no-cov`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the predicate**

Create `src/doc_lattice/github_ci/reachability.py`:

```python
"""Three-valued PR-reachability evaluation of job-level if: conditions (spec D1).

A job is pruned from the PR scan only when its condition is provably false for every
triggered PR event. The only recognized atom is ``github.event_name == '<literal>'`` in
either operand order inside an optional ``${{ ... }}`` wrapper around a top-level ``&&``
conjunction. Everything else evaluates to unknown, and unknown never proves falsity.
"""

import re

_WRAPPER_RE = re.compile(r"^\$\{\{(?P<body>.*)\}\}$", re.DOTALL)
_ATOM_LEFT_RE = re.compile(
    r"^github\.event_name\s*==\s*'(?P<literal>[^']*)'$", re.ASCII | re.IGNORECASE
)
_ATOM_RIGHT_RE = re.compile(
    r"^'(?P<literal>[^']*)'\s*==\s*github\.event_name$", re.ASCII | re.IGNORECASE
)

_TRUE = "true"
_FALSE = "false"
_UNKNOWN = "unknown"


def _atom_literal(atom: str) -> str | None:
    """Return the compared event literal when ``atom`` is the recognized form, else None."""
    match = _ATOM_LEFT_RE.match(atom) or _ATOM_RIGHT_RE.match(atom)
    return match.group("literal") if match else None


def _split_conjunction(body: str) -> list[str] | None:
    """Split on top-level ``&&`` outside single quotes; None on structural failure."""
    if "||" in body:
        return None
    atoms: list[str] = []
    current: list[str] = []
    in_quote = False
    index = 0
    while index < len(body):
        char = body[index]
        if char == "'":
            in_quote = not in_quote
            current.append(char)
            index += 1
            continue
        if not in_quote and body.startswith("&&", index):
            atoms.append("".join(current).strip())
            current = []
            index += 2
            continue
        current.append(char)
        index += 1
    if in_quote:
        return None
    atoms.append("".join(current).strip())
    if any(not atom for atom in atoms):
        return None
    return atoms


def _condition_value(condition: str, event: str) -> str:
    """Evaluate the condition for one event: ``true``, ``false``, or ``unknown``."""
    text = condition.strip()
    wrapped = _WRAPPER_RE.match(text)
    if wrapped:
        text = wrapped.group("body").strip()
    elif "${{" in text:
        return _UNKNOWN
    atoms = _split_conjunction(text)
    if atoms is None:
        return _UNKNOWN
    saw_unknown = False
    for atom in atoms:
        literal = _atom_literal(atom)
        if literal is None:
            saw_unknown = True
            continue
        if literal.lower() != event.lower():
            return _FALSE
    return _UNKNOWN if saw_unknown else _TRUE


def job_is_pr_reachable(if_condition: str | None, event_names: frozenset[str]) -> bool:
    """Return whether a job can run for any triggered PR event (spec D1).

    Args:
        if_condition: The job-level ``if:`` text, or None when absent.
        event_names: The document's trigger names intersected with ``PR_EVENTS``.

    Returns:
        False only when the condition is provably false for every event in ``event_names``,
        or when ``event_names`` is empty. Structural failures and unrecognized atoms keep
        the job reachable.
    """
    if not event_names:
        return False
    if if_condition is None or not if_condition.strip():
        return True
    return any(
        _condition_value(if_condition, event) != _FALSE for event in sorted(event_names)
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_reachability.py -q --no-cov`
Expected: PASS (20 parameter rows).

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/github_ci/reachability.py tests/test_github_ci_reachability.py
git commit -m "feat: add dormant D1 PR-reachability predicate for issue #100"
```

---

### Task 3: Launcher policy on the shared word IR

**Files:**
- Create: `src/doc_lattice/github_ci/launcher_policy.py`
- Create: `tests/test_github_ci_launcher_policy.py`

**Interfaces:**
- Produces: `ScanWord(text: str, start: int, end: int, unstable: bool)`, the shared word IR.
  `text` is the dequoted normalized word text with permitted expansions kept in raw form
  (`"--config=$CFG"` has text `--config=$CFG`); `start`/`end` are source offsets; `unstable`
  is True when the word contains a permitted expansion or an unquoted glob/tilde instability.
  A fully literal word has `unstable=False`.
- Produces: `CandidateResolution(kind, invocation, reason_category, offset)` where `kind` is
  `Literal["resolved", "not_candidate", "refused"]`; `invocation` is `tuple[str, bool] | None`
  (None for resolved dispositions that record no invocation, such as root `--help`);
  `reason_category`/`offset` are set only for `refused`.
- Produces: `resolve_command(words: tuple[ScanWord, ...]) -> CandidateResolution`, called by
  the scanner (Task 4) once per command in a statement list. The scanner guarantees the first
  word is literal (`unstable=False`) before calling; policy never sees an unstable first word.
- `direct_marker_scanner.py` imports this module, never the reverse (spec Architecture).

**Policy contract (adapting `shell_scanner.py` semantics to the floor; the frozen artifacts
are the oracle):**

1. Executable recognition: a literal word whose basename (text after the last `/`) is exactly
   `doc-lattice` starts a direct invocation. Any other non-launcher first word is
   `not_candidate`.
2. Launchers: `uvx` and `uv`. For `uv`, only the literal subcommand forms `uv run` and
   `uv tool run` continue candidate resolution; any other `uv` subcommand is `not_candidate`.
3. Launcher options before the payload, adapted from `shell_scanner.py:2606` and
   `shell_scanner.py:2713`: value-taking options `--python`, `--from`, `--with` (separate
   value word or attached `=value` form) and flag `--no-sync` are skipped. A `--from` value
   or payload word is a doc-lattice distribution when its name before the first character in
   `([<>=!~@;` normalizes to `doc-lattice` under `[-_.]+` separator collapsing (PEP 503,
   `shell_scanner.py:35-37`). Any other word starting with `-` before the payload is
   established, and any unstable word before the payload is established, is `refused` with
   category `policy-unresolvable` at that word's `start`.
4. The payload executable word must itself be literal and resolve to doc-lattice (basename
   rule or distribution rule); a launcher whose payload resolves to anything else is
   `not_candidate` (marker-free dynamic payloads never reach policy; the scanner refuses
   unstable words in command position first).
5. Root options between executable and subcommand, from `shell_scanner.py:261-263`:
   `--no-color` is skipped; `--help` or `--version` resolves immediately with
   `invocation=None`. Any other `-`-prefixed word before the subcommand is `refused` with
   `policy-unresolvable`.
6. Subcommand table: `check` gives `("check", False)`; `lint` gives `("lint", False)`;
   `linear` gives `("linear", False)`; `ci` consumes an optional following literal `audit`
   word and gives `("ci", False)` (owner-adjudicated); `reconcile` gives
   `("reconcile", dry)` where `dry` is True only when a later word in the same command has
   dequoted literal text exactly `--dry-run` before any unstable word appears. Any other
   literal subcommand word is `refused` with `policy-unresolvable` at its offset.
7. D3 policy rule for unstable argv: once the subcommand is established, the first unstable
   word terminates option processing while retaining the disposition established so far
   (`doc-lattice reconcile pc-design "$OPTION" --dry-run` keeps `("reconcile", False)`; the
   trailing `--dry-run` is never credited). Before the subcommand is established, an unstable
   word is `refused` with `policy-unresolvable`.
8. An empty quoted word (`""` or `''`) never matches any launcher, executable, or option
   table (spec D3 Words).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_ci_launcher_policy.py`:

```python
"""Tests for the doc-lattice launcher and option policy over the shared word IR."""

import pytest

from doc_lattice.github_ci.launcher_policy import (
    CandidateResolution,
    ScanWord,
    resolve_command,
)


def words(*specs):
    """Build a ScanWord tuple from (text, unstable) pairs with synthetic offsets."""
    result = []
    offset = 0
    for text, unstable in specs:
        result.append(ScanWord(text, offset, offset + len(text), unstable))
        offset += len(text) + 1
    return tuple(result)


def lit(*texts):
    return words(*[(text, False) for text in texts])


def test_direct_invocations_resolve():
    assert resolve_command(lit("doc-lattice", "check")).invocation == ("check", False)
    assert resolve_command(lit("doc-lattice", "lint")).invocation == ("lint", False)
    assert resolve_command(lit("doc-lattice", "linear")).invocation == ("linear", False)
    assert resolve_command(
        lit("doc-lattice", "ci", "audit", "--repository", "OWNER/REPO")
    ).invocation == ("ci", False)


def test_path_basename_resolves():
    assert resolve_command(lit("/usr/local/bin/doc-lattice", "check")).invocation == (
        "check",
        False,
    )


def test_non_candidate_commands():
    assert resolve_command(lit("exit", "1")).kind == "not_candidate"
    assert resolve_command(lit("uv", "sync")).kind == "not_candidate"
    assert resolve_command(lit("echo", "doc-lattice")).kind == "not_candidate"


def test_launcher_forms_resolve():
    assert resolve_command(
        lit("uvx", "--python", "3.13", "--from", "doc-lattice==2.0.0", "doc-lattice", "ci",
            "audit", "--repository", "OWNER/REPO")
    ).invocation == ("ci", False)
    assert resolve_command(
        lit("uv", "run", "--no-sync", "doc-lattice", "check")
    ).invocation == ("check", False)
    assert resolve_command(
        lit("uv", "tool", "run", "doc-lattice", "check")
    ).invocation == ("check", False)
    assert resolve_command(
        lit("uvx", "--from", "doc-lattice", "--with", "pyodide-build", "doc-lattice",
            "check")
    ).invocation == ("check", False)
    assert resolve_command(lit("uvx", "doc-lattice", "check")).invocation == ("check", False)


def test_root_options():
    assert resolve_command(lit("doc-lattice", "--no-color", "check")).invocation == (
        "check",
        False,
    )
    helped = resolve_command(lit("uv", "run", "doc-lattice", "--help"))
    assert helped.kind == "resolved"
    assert helped.invocation is None
    versioned = resolve_command(lit("doc-lattice", "--version"))
    assert versioned.kind == "resolved"
    assert versioned.invocation is None


def test_dry_run_extraction():
    assert resolve_command(
        lit("doc-lattice", "reconcile", "--dry-run")
    ).invocation == ("reconcile", True)
    assert resolve_command(lit("doc-lattice", "reconcile")).invocation == (
        "reconcile",
        False,
    )
    quoted = resolve_command(lit("doc-lattice", "reconcile", "pc-design", "--dry-run"))
    assert quoted.invocation == ("reconcile", True)


def test_unstable_after_subcommand_terminates_retention():
    resolution = resolve_command(
        words(("doc-lattice", False), ("reconcile", False), ("pc-design", False),
              ("$OPTION", True), ("--dry-run", False))
    )
    assert resolution.invocation == ("reconcile", False)


def test_unstable_before_subcommand_refuses():
    resolution = resolve_command(
        words(("doc-lattice", False), ("$X", True), ("check", False))
    )
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"
    assert resolution.offset == 12


def test_unknown_subcommand_refuses():
    resolution = resolve_command(lit("doc-lattice", "frobnicate"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_unknown_launcher_option_refuses():
    resolution = resolve_command(lit("uvx", "--quiet", "doc-lattice", "check"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_dynamic_payload_before_established_refuses():
    resolution = resolve_command(words(("uvx", False), ("$PKG", True), ("check", False)))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_empty_quoted_word_never_matches():
    resolution = resolve_command(words(("", False), ("check", False)))
    assert resolution.kind == "not_candidate"


def test_distribution_spelling_variants_resolve():
    assert resolve_command(
        lit("uvx", "--from", "doc_lattice==2.0.0", "doc-lattice", "check")
    ).invocation == ("check", False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_launcher_policy.py -q --no-cov`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the module**

Create `src/doc_lattice/github_ci/launcher_policy.py` implementing the eight-point policy
contract above. Structure guidance, not prescription: a frozen `ScanWord` dataclass; a frozen
`CandidateResolution` dataclass with the exact fields from Interfaces; module-level frozen
tables (`_SUBCOMMANDS`, `_ROOT_SKIP_OPTIONS`, `_ROOT_STOP_OPTIONS`, `_LAUNCHER_VALUE_OPTIONS`,
`_LAUNCHER_FLAG_OPTIONS`); one public `resolve_command` walking the words left to right with an
explicit index, consulting `shell_scanner.py:2094-2360` (payload resolution),
`shell_scanner.py:2651-2777` (subcommand and option skipping), and `shell_scanner.py:2957-2988`
(basename and distribution matching) for the semantics being adapted. Import
`ScanReasonCategory` from `doc_lattice.constants`. No imports from `direct_marker_scanner`
(it does not exist yet, and the dependency direction is policy-first forever).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_launcher_policy.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/github_ci/launcher_policy.py tests/test_github_ci_launcher_policy.py
git commit -m "feat: add dormant launcher policy on the shared word IR for issue #100"
```

---

### Task 4: The floor-grammar scanner core

**Files:**
- Create: `src/doc_lattice/github_ci/direct_marker_scanner.py`
- Create: `tests/test_github_ci_direct_marker_scanner.py`

**Interfaces:**
- Consumes: `ScanWord`, `resolve_command` from `launcher_policy` (Task 3); `BlockScan` from
  `model` (Task 1); domains from `doc_lattice.constants`.
- Produces: `scan_execution_source(source: str) -> BlockScan`, the single public entry point
  (spec Architecture: one entry point so templates and bodies can never drift). Also exposes
  `DIRECT_MARKER_RE` (the frozen marker regex) for the harness and later audit wiring.

**Behavior (spec D2/D3/D4; every rule below is normative):**

1. Marker gate first: no `DIRECT_MARKER_RE` match in `source` gives
   `BlockScan("not_applicable", (), None, None, None, work)` where work is the single
   marker-pass charge of `len(source)`. A marker anywhere (comments and quoted data included)
   proceeds to the grammar.
2. Caps before scanning: a source longer than 1,048,576 characters is `uninspectable` with
   category `cap-exceeded`, offset 0. During scanning, exceeding the token cap (262,144),
   statement cap (65,536), invocation cap (10,000), or the work limit
   `min(4194304, 4 * len(source) + 4096)` is `uninspectable` with category `cap-exceeded` at
   the offset where the cap tripped. Work charges: marker pass `len(source)` once, one per
   character examined, one per token emitted, one per statement closed, one per policy step
   (charge one per `resolve_command` word consumed; the scanner passes a charge callback or
   counts policy words after resolution).
3. Source preconditions: any carriage return, or any C0 control character other than newline
   and tab, is `uninspectable` at its offset with category `control-character`. Non-ASCII
   code points are ordinary literal word characters.
4. Tokenization is a single iterative left-to-right pass (no recursion). Statements are
   separated by unquoted newlines or `;`. Within a statement, commands are joined by `&&` or
   `||`, and both sides of every list are scanned (no short-circuit reasoning).
5. Refusals, each at the earliest offending offset, with these exact categories:
   - unquoted `|` (not `||`), `&` (not `&&`), `<`, `>`, `(`, `)`, backtick, backslash
     (including line continuations), unquoted brace outside a permitted `${NAME}`, mid-word
     `#`, heredoc introducers, redirections: `unsupported-operator`;
   - an interior backslash, backtick, or non-permitted `$` sequence inside a double-quoted
     string: `unsupported-operator` for backslash and backtick (owner-adjudicated),
     `unsupported-expansion` for the `$` sequence;
   - any `$` sequence other than `$?`, `$NAME`, `${NAME}` with `NAME` matching
     `[A-Za-z_][A-Za-z0-9_]*`: `unsupported-expansion`;
   - a permitted expansion unquoted in a command word: `unquoted-expansion-in-command-word`
     (in assignment values, unquoted permitted expansions are certifiable);
   - a single- or double-quoted string spanning a newline: `quote-spans-newline`; a quote
     still open at end of source: `unterminated-quote`;
   - a control-flow keyword in command position (`if then elif else fi while until do done
     for case esac function ! time coproc`) or a function definition: `control-flow-keyword`;
   - `NAME=value` followed by more words in the same command: `assignment-prefix`;
   - a command whose first word is unstable or non-literal (contains any expansion, an
     unquoted `*` `?` `[` `]` except a word exactly `[` or `]`, or a leading unquoted `~`):
     `unstable-first-word`;
   - a policy refusal from `resolve_command`: the resolution's own category and offset.
6. Comments: an unquoted `#` at line start or after unquoted whitespace or an operator runs
   to end of line and is certifiable. Blank lines are certifiable.
7. Assignment statements: `NAME=value` as the entire statement, value a literal word, quoted
   literal, or permitted parameter form (quoted or unquoted), certifies.
8. Monotonic evidence (D4 plus owner adjudication): invocations from commands that terminated
   cleanly before the failure offset are retained on the uninspectable result; the failing
   command itself contributes nothing. The recognizer stops at the first refusal (discovery
   after synchronization loss is never promised); `status="uninspectable"` results therefore
   carry reason, category, offset, and any prior invocations.
9. Certified results carry the concatenated invocations of every command, in source order.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_ci_direct_marker_scanner.py`:

```python
"""Unit tests for the D3 floor-grammar scanner."""

from doc_lattice.github_ci.direct_marker_scanner import (
    DIRECT_MARKER_RE,
    scan_execution_source,
)


def test_marker_gate_not_applicable():
    result = scan_execution_source("echo hello\nexit 1\n")
    assert result.status == "not_applicable"
    assert result.invocations == ()
    assert result.work_charged == len("echo hello\nexit 1\n")


def test_marker_matches_spelling_variants():
    for text in ("doc-lattice", "DOC-LATTICE", "doc_lattice", "doc.lattice", "doc-_lattice"):
        assert DIRECT_MARKER_RE.search(text)
    assert not DIRECT_MARKER_RE.search('doc-"lattice"')


def test_simple_command_certifies():
    result = scan_execution_source("doc-lattice check\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_marker_in_comment_scans_whole_block():
    result = scan_execution_source("# doc-lattice notes\necho fine\n")
    assert result.status == "certified"
    assert result.invocations == ()


def test_list_scans_both_sides():
    result = scan_execution_source("doc-lattice check && doc-lattice lint\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_or_list_scans_both_sides():
    result = scan_execution_source("doc-lattice lint || exit 1\n")
    assert result.status == "certified"
    assert result.invocations == (("lint", False),)


def test_assignment_statement_certifies():
    result = scan_execution_source("CFG=doc-lattice.yml\ndoc-lattice check\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_assignment_prefix_refuses():
    result = scan_execution_source("CFG=x doc-lattice check\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "assignment-prefix"


def test_unquoted_pipe_refuses_at_offset():
    source = "doc-lattice check | cat\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == source.index("|")


def test_command_substitution_refuses():
    result = scan_execution_source('OUT=$(doc-lattice check)\n')
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-expansion"


def test_control_flow_refuses():
    result = scan_execution_source("if doc-lattice check; then echo ok; fi\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "control-flow-keyword"
    assert result.offset == 0


def test_quoted_expansion_in_argument_certifies():
    result = scan_execution_source('doc-lattice check --config "$CFG"\n')
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_unquoted_expansion_in_command_word_refuses():
    source = "doc-lattice check $CFG\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "unquoted-expansion-in-command-word"


def test_unstable_first_word_refuses():
    result = scan_execution_source('"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear\n')
    assert result.status == "uninspectable"
    assert result.reason_category == "unstable-first-word"
    assert result.offset == 0


def test_carriage_return_refuses():
    result = scan_execution_source("doc-lattice check\r\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "control-character"


def test_quote_spanning_newline_refuses():
    result = scan_execution_source('doc-lattice check "a\nb"\n')
    assert result.status == "uninspectable"
    assert result.reason_category == "quote-spans-newline"


def test_unterminated_quote_refuses():
    result = scan_execution_source("doc-lattice check 'oops\n")
    assert result.status in {"uninspectable"}
    assert result.reason_category in {"quote-spans-newline", "unterminated-quote"}


def test_monotonic_evidence_retains_prior_statements():
    source = "doc-lattice check\ndoc-lattice lint | cat\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.invocations == (("check", False),)


def test_mid_command_failure_drops_that_invocation():
    source = "doc-lattice lint | cat\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.invocations == ()


def test_source_cap_refuses():
    result = scan_execution_source("doc-lattice " + "a" * 1_100_000)
    assert result.status == "uninspectable"
    assert result.reason_category == "cap-exceeded"
    assert result.offset == 0


def test_work_charged_is_linear():
    source = "doc-lattice check\n" * 100
    result = scan_execution_source(source)
    assert result.status == "certified"
    assert result.work_charged <= min(4_194_304, 4 * len(source) + 4_096)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_direct_marker_scanner.py -q --no-cov`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the scanner**

Create `src/doc_lattice/github_ci/direct_marker_scanner.py` implementing the nine-point
behavior contract. Suggested internal shape (latitude allowed, contract is not negotiable):
a `_WorkCounter` with the limit formula and a `charge(n)` method returning False on
exhaustion; a `_Tokenizer` producing per-statement `tuple[ScanWord, ...]` lists or a typed
refusal `(offset, category, detail)`; a top-level loop assembling `BlockScan`. Track, per
command, whether it terminated cleanly; flush its invocation into the accumulated tuple only
on clean termination (owner adjudication in Global Constraints). The module imports only
`re`, `dataclasses`, `doc_lattice.constants`, `doc_lattice.github_ci.model`, and
`doc_lattice.github_ci.launcher_policy`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_direct_marker_scanner.py tests/test_github_ci_launcher_policy.py -q --no-cov`
Expected: PASS (both files; policy behavior must not regress).

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/github_ci/direct_marker_scanner.py tests/test_github_ci_direct_marker_scanner.py
git commit -m "feat: add dormant D3 floor-grammar scanner for issue #100"
```

---

### Task 5: Gate 1, the 78-label conformance drive

**Files:**
- Create: `tests/test_github_ci_evaluation_gates.py`
- Modify: `src/doc_lattice/github_ci/direct_marker_scanner.py` and
  `src/doc_lattice/github_ci/launcher_policy.py` (conformance fixes only)

**Interfaces:**
- Consumes: `scan_execution_source` (Task 4); `acceptance_labels.json` and
  `ACCEPTANCE_CASES` alignment (checkpoint).
- Produces: the file `tests/test_github_ci_evaluation_gates.py` that later tasks append gates
  to, with module constant `CHECKPOINT = Path("tests/fixtures/github_ci_checkpoint")`.

- [ ] **Step 1: Write the gate test**

Create `tests/test_github_ci_evaluation_gates.py`:

```python
"""Predeclared evaluation gates for the issue #100 recognizer candidate (spec gates 1-6, 8, 9)."""

import json
from pathlib import Path

import pytest

from doc_lattice.github_ci.direct_marker_scanner import scan_execution_source

CHECKPOINT = Path("tests/fixtures/github_ci_checkpoint")

_LABELS = json.loads((CHECKPOINT / "acceptance_labels.json").read_text())["cases"]


def _acceptance_cases():
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES

    return ACCEPTANCE_CASES


@pytest.mark.parametrize("index", range(78), ids=[row["description"] for row in _LABELS])
def test_gate1_acceptance_label_conformance(index):
    row = _LABELS[index]
    description, script, _expected = _acceptance_cases()[index]
    assert row["description"] == description
    result = scan_execution_source(script)
    assert result.status == row["expected_status"], (description, result.reason)
    assert [list(i) for i in result.invocations] == row["expected_invocations"], description
    if row["expected_status"] == "uninspectable":
        assert result.reason_category == row["reason_category"], (
            description,
            result.offset,
            result.reason,
        )
```

- [ ] **Step 2: Run the gate and drive conformance to 78 of 78**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_evaluation_gates.py -q --no-cov`

Iterate on `direct_marker_scanner.py` and `launcher_policy.py` until all 78 rows pass. Rules
for this step:

- The frozen labels are correct; the recognizer moves toward them, never the reverse. The
  labels were derived twice independently (Task 3 of the checkpoint plan, re-verified at its
  final review). If a label appears wrong, STOP and report to the controller with the exact
  case; that is a spec conflict, not an implementation choice.
- Fixes must not violate the Task 4 behavior contract; Task 4 and Task 3 unit tests keep
  passing throughout (`... -m pytest tests/test_github_ci_direct_marker_scanner.py
  tests/test_github_ci_launcher_policy.py tests/test_github_ci_evaluation_gates.py -q
  --no-cov`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_github_ci_evaluation_gates.py src/doc_lattice/github_ci/direct_marker_scanner.py src/doc_lattice/github_ci/launcher_policy.py
git commit -m "test: pass gate 1 acceptance-label conformance for issue #100"
```

---

### Task 6: Evaluation harness core and gate 2 (frozen replay)

**Files:**
- Create: `tests/github_ci_evaluation_harness.py`
- Modify: `tests/test_github_ci_evaluation_gates.py` (append)

**Interfaces:**
- Produces (harness): `CHECKPOINT` path constant; `load_replay_inventory()`,
  `load_tier3a_cases()`, `load_tier3b_provenance()`, `tier3b_run_block(fixture_id)`,
  `load_probes()`, `load_mutations()`, `load_bash_pin()` (thin JSON loaders over the
  checkpoint, each returning the parsed structure);
  `OldResult(certified: bool, invocations: tuple[tuple[str, bool], ...],
  incomplete_reason: str | None)`; `old_scan(source: str) -> OldResult`;
  `classify_divergence(old: OldResult, new: BlockScan) -> str` returning one of
  `"identical"`, `"intentional-exit-2"`, `"outside-direct-marker"`,
  `"old-incomplete-new-certified"`, `"unexplained"`; and
  `replay_records() -> list[dict]`, the normalized gate 2 records over the 580-entry
  inventory plus all tier sources.
- Consumes: `scan_doc_lattice_invocations` (old scanner), `scan_execution_source` (new),
  checkpoint artifacts. Tasks 7 to 13 import from this module.

- [ ] **Step 1: Write the harness**

Create `tests/github_ci_evaluation_harness.py`:

```python
"""Shared harness for the issue #100 recognizer evaluation gates.

Test-side only: this module orchestrates the frozen checkpoint artifacts, the old scanner
baseline, and the candidate recognizer for the predeclared gates. It never touches runtime
audit behavior.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.direct_marker_scanner import scan_execution_source
from doc_lattice.github_ci.model import BlockScan
from doc_lattice.github_ci.shell_scanner import (
    direct_doc_lattice_invocations,
    scan_doc_lattice_invocations,
)
from doc_lattice.github_ci.workflow_parser import parse_workflow

CHECKPOINT = Path("tests/fixtures/github_ci_checkpoint")


def _load(name: str):
    """Parse one checkpoint JSON artifact."""
    return json.loads((CHECKPOINT / name).read_text())


def load_replay_inventory():
    """Return the frozen 580-entry replay inventory."""
    return _load("replay_inventory.json")


def load_tier3a_cases():
    """Return the 13 Tier 3A conformance cases."""
    return _load("tier3a_cases.json")["cases"]


def load_tier3b_provenance():
    """Return the Tier 3B provenance manifest."""
    return _load("tier3b/provenance.json")


def load_probes():
    """Return the frozen probe inventory."""
    return _load("probes.json")


def load_mutations():
    """Return the frozen boundary-mutation set."""
    return _load("mutations.json")


def load_bash_pin():
    """Return the frozen Bash pin."""
    return _load("bash_pin.json")


def tier3b_run_block(fixture_id: str) -> str:
    """Extract the single run: block of one Tier 3B workflow fixture via the real parser."""
    document = parse_workflow(CHECKPOINT / "tier3b" / f"{fixture_id}.yml")
    runs = [step.run for job in document.jobs for step in job.steps if step.run is not None]
    assert len(runs) == 1, fixture_id
    return runs[0]


@dataclass(frozen=True, slots=True)
class OldResult:
    """Normalized old-scanner outcome for one source (raw and adapter layers)."""

    certified: bool
    invocations: tuple[tuple[str, bool], ...]
    incomplete_reason: str | None
    adapter_config_error: bool


def old_scan(source: str) -> OldResult:
    """Run both old entry points and normalize their results (never exception text)."""
    result = scan_doc_lattice_invocations(source)
    try:
        direct_doc_lattice_invocations(source)
        adapter_config_error = False
    except ConfigError:
        adapter_config_error = True
    return OldResult(
        certified=result.incomplete_reason is None,
        invocations=tuple(result.invocations),
        incomplete_reason=result.incomplete_reason,
        adapter_config_error=adapter_config_error,
    )


def classify_divergence(old: OldResult, new: BlockScan) -> str:
    """Classify one old-versus-new pair into the predeclared gate 2 categories."""
    if new.status == "not_applicable":
        return "outside-direct-marker"
    if old.certified and new.status == "certified":
        return "identical" if old.invocations == new.invocations else "unexplained"
    if old.certified and new.status == "uninspectable":
        return "intentional-exit-2"
    if not old.certified and new.status == "uninspectable":
        return "identical"
    if not old.certified and new.status == "certified":
        return "old-incomplete-new-certified"
    return "unexplained"


def _tier_sources() -> list[tuple[str, str]]:
    """Return (case id, source) pairs for every tier source beyond the replay inventory."""
    sources = [(case["id"], case["source"]) for case in load_tier3a_cases()]
    for row in load_tier3b_provenance()["fixtures"]:
        sources.append((row["id"], tier3b_run_block(row["id"])))
    return sources


def replay_records() -> list[dict]:
    """Produce one normalized record per replay-inventory entry and per tier source."""
    records = []
    entries = [(entry["id"], entry["source"]) for entry in load_replay_inventory()["entries"]]
    for case_id, source in entries + _tier_sources():
        old = old_scan(source)
        new = scan_execution_source(source)
        records.append(
            {
                "id": case_id,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "old_certified": old.certified,
                "old_invocations": [list(pair) for pair in old.invocations],
                "old_incomplete_reason": old.incomplete_reason,
                "old_adapter_config_error": old.adapter_config_error,
                "new_status": new.status,
                "new_invocations": [list(pair) for pair in new.invocations],
                "new_reason_category": new.reason_category,
                "new_offset": new.offset,
                "category": classify_divergence(old, new),
            }
        )
    return records
```

- [ ] **Step 2: Append the gate 2 test**

Append to `tests/test_github_ci_evaluation_gates.py`:

```python
def test_gate2_replay_divergences_stay_in_predeclared_categories():
    from github_ci_evaluation_harness import replay_records

    records = replay_records()
    assert len(records) == 580 + 13 + 20
    allowed = {"identical", "intentional-exit-2", "outside-direct-marker"}
    unexplained = [r for r in records if r["category"] == "unexplained"]
    assert unexplained == [], unexplained[:5]
    category_d = [r["id"] for r in records if r["category"] == "old-incomplete-new-certified"]
    prelabeled = json.loads((CHECKPOINT / "category_d_exceptions.json").read_text())
    assert category_d == prelabeled == []
    assert {r["category"] for r in records} <= allowed
```

- [ ] **Step 3: Run the gate, fix conformance divergences only**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_evaluation_gates.py -q --no-cov`

An `unexplained` record means old and new both certify with different invocation tuples, or a
logic hole in the scanner. Fix the recognizer (Tasks 3 to 5 rules apply: labels and prior unit
tests keep passing). Do not touch `classify_divergence` to make records fit; the categories
are predeclared in the spec. If a record cannot be made conformant without violating a frozen
label, STOP and report.

- [ ] **Step 4: Commit**

```bash
git add tests/github_ci_evaluation_harness.py tests/test_github_ci_evaluation_gates.py src/doc_lattice/github_ci
git commit -m "test: pass gate 2 frozen replay for issue #100"
```

---

### Task 7: D1+D6 evaluation orchestrator and gates 3 and 4

**Files:**
- Modify: `tests/github_ci_evaluation_harness.py` (append)
- Modify: `tests/test_github_ci_evaluation_gates.py` (append)

**Interfaces:**
- Produces (harness append): `SourceEvaluation(path: str, job_id: str, step_index: int,
  source_kind: str, scan: BlockScan)`; `WorkflowEvaluation(pruned_jobs: tuple[str, ...],
  evaluations: tuple[SourceEvaluation, ...], diagnostics: tuple[AuditDiagnostic, ...])`;
  `evaluate_workflow(document: WorkflowDocument) -> WorkflowEvaluation` implementing D1
  pruning plus the D6 composition table for the PR scan. This is the evaluation-side
  orchestrator; PR-B-era runtime wiring in `audit.py` is explicitly out of scope.
- Consumes: `job_is_pr_reachable` (Task 2), `scan_execution_source` (Task 4),
  `audit.PR_EVENTS`, `audit._supports_bash_run_body`, `audit._BASH_DEFAULT_RUNNERS`,
  `audit._SCRIPT_PLACEHOLDER`, `audit._SCRIPT_SENTINEL` (reuse of the frozen recognition
  sets is required by spec D6; importing audit privates from the test-side harness is the
  accepted mechanism until PR B wires composition into `audit.py` itself).

- [ ] **Step 1: Append the orchestrator to the harness**

Append to `tests/github_ci_evaluation_harness.py` (add the model and audit imports at the
top of the file: `from doc_lattice.github_ci import audit as audit_module`,
`from doc_lattice.github_ci.direct_marker_scanner import DIRECT_MARKER_RE`,
`from doc_lattice.github_ci.model import AuditDiagnostic, WorkflowDocument`,
`from doc_lattice.github_ci.reachability import job_is_pr_reachable`):

```python
@dataclass(frozen=True, slots=True)
class SourceEvaluation:
    """One scanned execution source and its BlockScan."""

    path: str
    job_id: str
    step_index: int
    source_kind: str
    scan: BlockScan


@dataclass(frozen=True, slots=True)
class WorkflowEvaluation:
    """D1+D6 evaluation outcome for one workflow document."""

    pruned_jobs: tuple[str, ...]
    evaluations: tuple[SourceEvaluation, ...]
    diagnostics: tuple[AuditDiagnostic, ...]


def _body_shell_class(shell: str | None, runs_on: str | None) -> str:
    """Classify a run body's effective shell per D6: BASH, NON_BASH, or UNKNOWN."""
    if shell is not None:
        bash = audit_module._supports_bash_run_body(shell)  # noqa: SLF001 (spec-mandated reuse)
        return "BASH" if bash else "NON_BASH"
    if runs_on is not None and runs_on.casefold() in audit_module._BASH_DEFAULT_RUNNERS:  # noqa: SLF001
        return "BASH"
    return "UNKNOWN"


def evaluate_workflow(document: WorkflowDocument) -> WorkflowEvaluation:
    """Apply D1 pruning and the D6 composition table to one workflow's PR scan."""
    path = str(document.path)
    event_names = frozenset(
        trigger.name for trigger in document.triggers
    ) & audit_module.PR_EVENTS
    pruned: list[str] = []
    evaluations: list[SourceEvaluation] = []
    diagnostics: list[AuditDiagnostic] = []

    def scan_source(job_id: str, step_index: int, kind: str, text: str) -> BlockScan:
        scan = scan_execution_source(text)
        evaluations.append(SourceEvaluation(path, job_id, step_index, kind, scan))
        if scan.status == "uninspectable":
            diagnostics.append(
                AuditDiagnostic(
                    path, job_id, step_index, kind, "UNINSPECTABLE_SOURCE",
                    scan.reason or "", scan.offset,
                )
            )
        return scan

    def semantics_diagnostic(job_id: str, step_index: int, kind: str) -> None:
        diagnostics.append(
            AuditDiagnostic(
                path, job_id, step_index, kind, "UNSUPPORTED_EXECUTION_SEMANTICS",
                "marker-bearing source executes under semantics the audit cannot inspect",
                None,
            )
        )

    for job in document.jobs:
        if not job_is_pr_reachable(job.if_condition, event_names):
            pruned.append(job.job_id)
            continue
        for step in job.steps:
            if step.run is None:
                continue
            shell = step.shell or job.default_shell or document.default_shell
            body_class = _body_shell_class(shell, job.runs_on)
            template = (
                shell.replace(
                    audit_module._SCRIPT_PLACEHOLDER, audit_module._SCRIPT_SENTINEL  # noqa: SLF001
                )
                if shell is not None
                else None
            )
            marker_in_template = bool(template and DIRECT_MARKER_RE.search(template))
            marker_in_body = bool(DIRECT_MARKER_RE.search(step.run))
            if marker_in_template and template is not None:
                scan_source(job.job_id, step.index, "shell_template", template)
                if body_class != "BASH":
                    semantics_diagnostic(job.job_id, step.index, "shell_template")
            if marker_in_body:
                if body_class == "BASH":
                    scan_source(job.job_id, step.index, "run_body", step.run)
                else:
                    semantics_diagnostic(job.job_id, step.index, "run_body")
    return WorkflowEvaluation(tuple(pruned), tuple(evaluations), tuple(diagnostics))
```

- [ ] **Step 2: Append the gate 3 and gate 4 tests**

Append to `tests/test_github_ci_evaluation_gates.py`:

```python
def test_gate3_tier1_offline_template_certifies(tmp_path):
    from github_ci_evaluation_harness import evaluate_workflow, load_tier3a_cases

    from doc_lattice.github_ci.render import render_workflows
    from doc_lattice.github_ci.workflow_parser import parse_workflow

    offline, _linear = render_workflows("OWNER/REPO", "2.0.0")
    target = tmp_path / "offline.yml"
    target.write_text(offline.text)
    document = parse_workflow(target)
    evaluation = evaluate_workflow(document)

    assert evaluation.diagnostics == ()
    scans = [e for e in evaluation.evaluations if e.source_kind == "run_body"]
    assert len(scans) == 1
    assert scans[0].scan.status == "certified"
    assert scans[0].scan.invocations == (("ci", False), ("check", False), ("lint", False))

    frozen = next(
        case for case in load_tier3a_cases() if case["id"] == "offline-template-block"
    )
    runs = [
        step.run for job in document.jobs for step in job.steps if step.run is not None
    ]
    assert runs[0].strip() == frozen["source"].strip()


def test_gate4_tier2_repository_workflow_is_clean():
    from github_ci_evaluation_harness import evaluate_workflow

    from doc_lattice.github_ci.workflow_parser import parse_workflow

    document = parse_workflow(Path(".github/workflows/ci.yml"))
    evaluation = evaluate_workflow(document)

    assert "release" in evaluation.pruned_jobs
    assert evaluation.diagnostics == ()
    assert all(e.scan.status == "not_applicable" for e in evaluation.evaluations)
```

- [ ] **Step 3: Run the gates**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_evaluation_gates.py -q --no-cov`
Expected: PASS. Two interlocks before adapting anything:

- If `render_workflows` returns artifacts in a different order or with a different signature,
  consult `src/doc_lattice/github_ci/render.py:472` and adjust the test call only.
- If gate 3's frozen-block comparison fails, or gate 4 finds the `release` job not pruned or
  any marker-bearing PR-reachable block, that is checkpoint or spec drift, not a test bug:
  STOP and report to the controller.

- [ ] **Step 4: Commit**

```bash
git add tests/github_ci_evaluation_harness.py tests/test_github_ci_evaluation_gates.py
git commit -m "test: pass tier 1 and tier 2 gates for issue #100"
```

---

### Task 8: Gates 5 and 6 (Tier 3A conformance, Tier 3B recorded verdict)

**Files:**
- Modify: `tests/test_github_ci_evaluation_gates.py` (append)

**Interfaces:**
- Consumes: harness loaders and `tier3b_run_block` (Task 6), `old_scan` (Task 6),
  `scan_execution_source` (Task 4).
- Produces: `TIER3B_VERDICT` module constant (`"rejected"`) that Task 13's decision record
  cites, plus the budget arithmetic the record reproduces.

- [ ] **Step 1: Append the gate tests**

Append to `tests/test_github_ci_evaluation_gates.py`:

```python
def _tier3a_ids():
    from github_ci_evaluation_harness import load_tier3a_cases

    return [case["id"] for case in load_tier3a_cases()]


@pytest.mark.parametrize("case_id", _tier3a_ids())
def test_gate5_tier3a_documented_conformance(case_id):
    from github_ci_evaluation_harness import load_tier3a_cases

    case = next(c for c in load_tier3a_cases() if c["id"] == case_id)
    result = scan_execution_source(case["source"])
    assert result.status == case["expected_status"], (case_id, result.reason)
    assert [list(i) for i in result.invocations] == case["expected_invocations"], case_id
    if case["expected_status"] == "uninspectable":
        assert result.reason_category == case["reason_category"], case_id


TIER3B_BUDGET_TOTAL = 2
TIER3B_BUDGET_NEWLY = 2
TIER3B_VERDICT = "rejected"


def test_gate6_tier3b_conformance_and_recorded_verdict():
    from github_ci_evaluation_harness import (
        load_tier3b_provenance,
        old_scan,
        tier3b_run_block,
    )

    fixtures = load_tier3b_provenance()["fixtures"]
    assert len(fixtures) == 20
    indeterminate = []
    newly_indeterminate = []
    false_safe = []
    false_positive = []
    for row in fixtures:
        source = tier3b_run_block(row["id"])
        new = scan_execution_source(source)
        old = old_scan(source)
        assert new.status == row["expected_status"], (row["id"], new.reason)
        assert [list(i) for i in new.invocations] == row["expected_invocations"], row["id"]
        if row["expected_status"] == "uninspectable":
            assert new.reason_category == row["reason_category"], row["id"]
        if new.status == "uninspectable":
            indeterminate.append(row["id"])
            if old.certified:
                newly_indeterminate.append(row["id"])
        if new.status == "certified" and row["expected_status"] == "uninspectable":
            false_safe.append(row["id"])
        if [list(i) for i in new.invocations] != row["expected_invocations"]:
            false_positive.append(row["id"])

    assert sorted(indeterminate) == ["fixture-02", "fixture-05", "fixture-14"]
    assert sorted(newly_indeterminate) == ["fixture-02", "fixture-05", "fixture-14"]
    assert false_safe == []
    assert false_positive == []

    # The owner-adjudicated recorded verdict (ruling 1a): both predeclared budgets breach,
    # so the D3 candidate is rejected by the evaluation. This gate asserts that recorded
    # outcome; a passing budget here would itself be evaluation drift.
    assert len(indeterminate) > TIER3B_BUDGET_TOTAL
    assert len(newly_indeterminate) > TIER3B_BUDGET_NEWLY
    assert TIER3B_VERDICT == "rejected"
```

- [ ] **Step 2: Run the gates**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_evaluation_gates.py -q --no-cov`
Expected: PASS, with gate 6 confirming exactly fixtures 02, 05, and 14 as the structural
uninspectables. Any other fixture set, any false-safe, or any false-positive is a genuine
evaluation surprise: STOP and report before changing anything.

- [ ] **Step 3: Commit**

```bash
git add tests/test_github_ci_evaluation_gates.py
git commit -m "test: record tier 3 conformance and the tier 3B rejection verdict for issue #100"
```

---

### Task 9: Gate 7, the semantic differential

**Files:**
- Modify: `pyproject.toml` (add `"shfmt-py==4.0.0",` to `[dependency-groups].dev`)
- Modify: `src/doc_lattice/github_ci/direct_marker_scanner.py` (add one observability helper)
- Create: `tests/test_github_ci_semantic_differential.py`
- Modify: `tests/test_github_ci_direct_marker_scanner.py` (append helper tests)

**Interfaces:**
- Produces (scanner append): `certified_command_words(source: str) ->
  tuple[tuple[str, ...], ...]`, returning the dequoted word texts of every command (candidate
  or not, one entry per `&&`/`||` arm, in source order) when the source certifies, and `()`
  otherwise. Pure observability for gate 7; no behavior change to `scan_execution_source`.
- Consumes: `load_probes()`, `load_mutations()`, `load_bash_pin()`, `load_tier3a_cases()`,
  `load_tier3b_provenance()`, `tier3b_run_block()` from the harness; `ACCEPTANCE_CASES`;
  the pinned system Bash; the `shfmt` binary provided by `shfmt-py`.

**The certified corpus for this gate:** the 7 must-certify acceptance scripts
(`acceptance_labels.json` label `must-certify`), the 11 certified Tier 3A sources, and the 17
certified Tier 3B run blocks.

- [ ] **Step 1: Add the dev dependency and the observability helper**

Add `"shfmt-py==4.0.0",` to the dev group in `pyproject.toml`, run
`env -u VIRTUAL_ENV uv sync --group dev`, and confirm `env -u VIRTUAL_ENV uv run --group dev
shfmt --version` prints a 3.13.x version. Then implement `certified_command_words` in the
scanner (it can reuse the tokenizer directly) and append to
`tests/test_github_ci_direct_marker_scanner.py`:

```python
def test_certified_command_words_exposes_structure():
    from doc_lattice.github_ci.direct_marker_scanner import certified_command_words

    words = certified_command_words('doc-lattice check --config "$CFG" && doc-lattice lint\n')
    assert words == (("doc-lattice", "check", "--config", "$CFG"), ("doc-lattice", "lint"))
    assert certified_command_words("doc-lattice check | cat\n") == ()
```

- [ ] **Step 2: Write the gate 7 test module**

Create `tests/test_github_ci_semantic_differential.py` with these components (complete the
walker against the observed `shfmt` JSON; node types below are the mvdan/sh v3 typed-JSON
encoding):

```python
"""Gate 7: the three-layer semantic differential for the issue #100 candidate."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from doc_lattice.github_ci.direct_marker_scanner import (
    certified_command_words,
    scan_execution_source,
)

BASH = "/bin/bash"


def _bash_pin_checked():
    from github_ci_evaluation_harness import load_bash_pin

    pin = load_bash_pin()
    version = subprocess.run(
        [BASH, "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    assert pin["version"] in version, (version, pin["version"])
    return pin


def _certified_corpus():
    from github_ci_evaluation_harness import (
        load_tier3a_cases,
        load_tier3b_provenance,
        tier3b_run_block,
    )
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES

    labels = json.loads(
        Path("tests/fixtures/github_ci_checkpoint/acceptance_labels.json").read_text()
    )["cases"]
    corpus = [
        (row["description"], script)
        for row, (_d, script, _e) in zip(labels, ACCEPTANCE_CASES, strict=True)
        if row["label"] == "must-certify"
    ]
    corpus += [
        (case["id"], case["source"])
        for case in load_tier3a_cases()
        if case["expected_status"] == "certified"
    ]
    corpus += [
        (row["id"], tier3b_run_block(row["id"]))
        for row in load_tier3b_provenance()["fixtures"]
        if row["expected_status"] == "certified"
    ]
    assert len(corpus) == 7 + 11 + 17
    return corpus


def test_static_layer_bash_and_shfmt_agree(tmp_path):
    _bash_pin_checked()
    for name, source in _certified_corpus():
        script = tmp_path / "candidate.sh"
        script.write_text(source if source.endswith("\n") else source + "\n")
        bash_check = subprocess.run(
            [BASH, "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert bash_check.returncode == 0, (name, bash_check.stderr)
        shfmt = subprocess.run(
            ["shfmt", "--to-json"],
            input=script.read_text(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert shfmt.returncode == 0, (name, shfmt.stderr)
        shfmt_commands = _shfmt_command_structure(json.loads(shfmt.stdout))
        recognizer_commands = certified_command_words(source)
        assert len(shfmt_commands) == len(recognizer_commands), name
        for (shfmt_count, shfmt_first), rec_words in zip(
            shfmt_commands, recognizer_commands, strict=True
        ):
            assert shfmt_count == len(rec_words), (name, rec_words)
            if shfmt_first is not None:
                assert shfmt_first == rec_words[0], name
```

`_shfmt_command_structure(tree)` walks the typed JSON: the document is `{"Type": "File",
"Stmts": [...]}`; each statement's `Cmd` is a `CallExpr` (a simple command; its `Args` list
gives the word count, and a first arg whose single part is `{"Type": "Lit", "Value": ...}`
gives the literal first word), a `BinaryCmd` with `Op` of `&&`/`||` (recurse into `X` and `Y`
to flatten arms in source order), or a `DeclClause`/assignment-only statement (a `CallExpr`
with `Assigns` and zero `Args`; emit nothing, matching the recognizer, which reports only
commands). If the installed `shfmt` rejects `--to-json`, use `--tojson`; if neither flag
exists, STOP and report the actual `shfmt --help` output. Comment lines produce no
statements on either side.

- [ ] **Step 3: Add the span-consistency and probe-layer tests**

Append to `tests/test_github_ci_semantic_differential.py`:

```python
def _span_sources():
    from github_ci_evaluation_harness import (
        load_probes,
        load_tier3a_cases,
        load_tier3b_provenance,
        tier3b_run_block,
    )
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES

    by_fixture = {}
    for description, script, _expected in ACCEPTANCE_CASES:
        by_fixture[description] = script
    for case in load_tier3a_cases():
        by_fixture[case["id"]] = case["source"]
    for row in load_tier3b_provenance()["fixtures"]:
        by_fixture[row["id"]] = tier3b_run_block(row["id"])
    return load_probes(), by_fixture


def test_probe_spans_are_reproduced_by_the_recognizer():
    probes, by_fixture = _span_sources()
    for span in probes["spans"]:
        result = scan_execution_source(span["text"])
        assert result.status == "certified", (span["span_id"], result.reason)
        commands = certified_command_words(span["text"])
        assert len(commands) == 1, span["span_id"]
        prefix = span["expected_stable_argv_prefix"]
        assert list(commands[0][: len(prefix)]) == prefix, span["span_id"]


def _write_stubs(stub_dir, record_path):
    stub_dir.mkdir()
    for name in ("doc-lattice", "uvx", "uv"):
        stub = stub_dir / name
        stub.write_text(
            '#!/bin/bash\nprintf \'%s\\n\' "===probe===" "$0" "$@" >> "$PROBE_RECORD"\n'
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return record_path


def test_probe_layer_matches_bash_execution(tmp_path):
    _bash_pin_checked()
    probes, _by_fixture = _span_sources()
    stub_dir = tmp_path / "stubs"
    record = _write_stubs(stub_dir, tmp_path / "record.txt")
    for span in probes["spans"]:
        record.write_text("")
        probe = tmp_path / "probe.sh"
        probe.write_text(span["text"] + "\n")
        env = dict(probes["env"])
        env["PATH"] = str(stub_dir)
        env["PROBE_RECORD"] = str(record)
        completed = subprocess.run(
            [BASH, str(probe)], capture_output=True, text=True, env=env, timeout=10,
            check=False,
        )
        assert completed.returncode == 0, (span["span_id"], completed.stderr)
        lines = record.read_text().splitlines()
        assert lines and lines[0] == "===probe===", span["span_id"]
        argv = [Path(lines[1]).name, *lines[2:]]
        prefix = span["expected_stable_argv_prefix"]
        assert argv[: len(prefix)] == prefix, (span["span_id"], argv)

        result = scan_execution_source(span["text"])
        expected = span["expected_verdict"]
        if expected is None:
            assert result.invocations == (), span["span_id"]
        else:
            assert result.invocations == (
                (expected["subcommand"], expected["dry_run"]),
            ), span["span_id"]
```

Probe rules restated from the checkpoint synthesis text: one probe per span, probe body is
the span text only, one probe per list arm, arms never joined, original fixture text never
executed, PATH contains only the three recorder stubs, env is exactly the checkpoint map.

- [ ] **Step 4: Add the boundary-mutation test**

Append to `tests/test_github_ci_semantic_differential.py`:

```python
def test_boundary_mutations_all_refuse_at_their_sites():
    from github_ci_evaluation_harness import load_mutations

    _probes, by_fixture = _span_sources()
    mutations = load_mutations()
    assert len(mutations["sites"]) == 50
    for site in mutations["sites"]:
        source = by_fixture[site["fixture_id"]]
        offset = site["offset"]
        mutated = source[:offset] + site["inserted_text"] + source[offset:]
        result = scan_execution_source(mutated)
        assert result.status == "uninspectable", (site["span_id"], site["kind"])
        assert result.reason_category == site["expected_reason_category"], (
            site["span_id"],
            site["kind"],
            result.reason_category,
        )
        assert offset <= result.offset <= offset + len(site["inserted_text"]), (
            site["span_id"],
            site["kind"],
            result.offset,
        )
```

The checkpoint encodes every mutation, wraps included, as a single-delimiter insertion at a
live offset (validated at the checkpoint's Task 7 review), so uniform string insertion is the
correct application. If any kind systematically fails the offset window or category, STOP and
report; the mutation expectations are frozen.

- [ ] **Step 5: Run gate 7, then commit**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_semantic_differential.py tests/test_github_ci_direct_marker_scanner.py -q --no-cov`
Expected: PASS (static layer over 35 certified fixtures, 36 probe spans, 50 mutation sites).

```bash
git add pyproject.toml uv.lock src/doc_lattice/github_ci/direct_marker_scanner.py tests/test_github_ci_semantic_differential.py tests/test_github_ci_direct_marker_scanner.py
git commit -m "test: pass gate 7 semantic differential for issue #100"
```

---

### Task 10: Gates 8 and 9 (adversarial bounds and the work counter)

**Files:**
- Modify: `tests/test_github_ci_evaluation_gates.py` (append)

**Interfaces:**
- Consumes: `scan_execution_source`, `replay_records` inputs via the harness loaders, and the
  checkpoint `limits.json`.
- Produces: `ADVERSARIAL_SOURCES`, a named list reused by the gate 9 sweep and cited in the
  decision record.

- [ ] **Step 1: Append the gate tests**

Append to `tests/test_github_ci_evaluation_gates.py`:

```python
_LIMITS = json.loads((CHECKPOINT / "limits.json").read_text())

ADVERSARIAL_SOURCES = [
    ("oversized-source", "doc-lattice " + "a" * (_LIMITS["source_cap_chars"] + 1)),
    ("nul-control", "doc-lattice check\x00\n"),
    ("carriage-return", "doc-lattice check\r\n"),
    ("token-storm", "doc-lattice check " + "x " * (_LIMITS["token_cap"] + 8)),
    ("statement-storm", "# doc-lattice\n" + ";" * (_LIMITS["statement_cap"] + 8)),
    (
        "invocation-storm",
        "doc-lattice check\n" * (_LIMITS["invocation_cap"] + 1),
    ),
    ("quote-flood", "doc-lattice check " + "'a' " * 100_000),
    ("malformed-tail", "doc-lattice check\ndoc-lattice lint 'unterminated"),
    ("marker-heavy", "# doc-lattice doc_lattice DOC.LATTICE\n" * 50_000),
]


@pytest.mark.parametrize("name", [name for name, _ in ADVERSARIAL_SOURCES])
def test_gate8_adversarial_inputs_refuse_deterministically(name):
    source = dict(ADVERSARIAL_SOURCES)[name]
    first = scan_execution_source(source)
    second = scan_execution_source(source)
    assert first == second, name
    if first.status == "uninspectable":
        assert first.reason_category is not None
        assert first.offset is not None
    work_bound = min(4_194_304, 4 * len(source) + 4_096)
    assert first.work_charged <= work_bound, (name, first.work_charged)


def test_gate9_work_counter_holds_over_every_input():
    from github_ci_evaluation_harness import (
        load_replay_inventory,
        load_tier3a_cases,
        load_tier3b_provenance,
        tier3b_run_block,
    )

    sources = [entry["source"] for entry in load_replay_inventory()["entries"]]
    sources += [case["source"] for case in load_tier3a_cases()]
    sources += [
        tier3b_run_block(row["id"]) for row in load_tier3b_provenance()["fixtures"]
    ]
    sources += [source for _name, source in ADVERSARIAL_SOURCES]
    for source in sources:
        result = scan_execution_source(source)
        bound = min(4_194_304, 4 * len(source) + 4_096)
        assert result.work_charged <= bound, source[:60]
```

Expected refusal notes: `oversized-source`, `token-storm`, `statement-storm`, and
`invocation-storm` refuse with `cap-exceeded`; `nul-control` and `carriage-return` with
`control-character`; `malformed-tail` with a quote category while retaining the first
statement's `("check", False)` (monotonic evidence); `quote-flood` and `marker-heavy` may
certify or refuse per grammar, but must stay within the work bound either way. Gate 8 asserts
determinism and boundedness, not specific categories, except where the unit tests of Task 4
already pin them.

- [ ] **Step 2: Run the gates, then commit**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_evaluation_gates.py -q --no-cov`
Expected: PASS. If any adversarial input exceeds the work bound, the scanner's charging or
loop structure is wrong; fix the scanner, never the bound (the formula is frozen checkpoint
data).

```bash
git add tests/test_github_ci_evaluation_gates.py
git commit -m "test: pass adversarial bounds and work-counter gates for issue #100"
```

---

### Task 11: Archive the July 2026 parser benchmark under docs/research/

**Files:**
- Create: `docs/research/2026-07-bash-parser-benchmark/` (artifact copies)
- Create: `docs/research/2026-07-bash-parser-benchmark/PROVENANCE.md`

**Interfaces:**
- Consumes: the July 2026 bash-parser benchmark artifact set referenced and audited in the
  issue #100 review comments (primary-source review, methodology, CSV matrix, JSON results).
- Produces: the archived evidence set the decision record (Task 13) cites. Archived
  artifacts are evidence only and never gate inputs (spec Delivery).

- [ ] **Step 1: Locate and fetch the artifact set**

Run `gh issue view 100 --comments` and locate the attachment or link set for the benchmark
artifacts (the comment auditing "234 parser/case executions" identifies the set). Download
every file. OWNER-INPUT DEPENDENCY: if the artifacts cannot be retrieved from the issue
thread, STOP and ask the project owner for the copies; do not reconstruct or approximate
them.

- [ ] **Step 2: Archive with hashes and labels**

Copy the files unmodified into `docs/research/2026-07-bash-parser-benchmark/`. Write
`PROVENANCE.md` containing: one line per file with its SHA-256 (`sha256sum` output), the
source URL of each file, the retrieval date typed literally, the label "internally
consistent, not independently reproducible" (spec wording), and one paragraph noting the
artifacts cover only the external parser candidates and are evidence for the decision
record, never gate inputs.

- [ ] **Step 3: Commit**

```bash
git add docs/research
git commit -m "docs: archive July 2026 bash-parser benchmark evidence for issue #100"
```

---

### Task 12: The fleetyard wall-clock benchmark (gate 9, trusted half)

**Files:**
- Create: `scripts/bench_recognizer_replay.py`
- Create: `docs/research/recognizer-benchmark/py313.json` and `py314.json` (measured output)

**Interfaces:**
- Consumes: the frozen replay inventory; `scan_execution_source`;
  `scan_doc_lattice_invocations` (baseline).
- Produces: per-version JSON results the decision record (Task 13) embeds. This gate is
  fleetyard-only and never CI-enforced (checkpoint item 7); the script is committed, the
  numbers are recorded evidence.

- [ ] **Step 1: Write the benchmark script**

Create `scripts/bench_recognizer_replay.py`:

```python
"""Fleetyard wall-clock benchmark for the issue #100 recognizer candidate (gate 9).

Implements the checkpoint benchmark protocol: interleaved candidate and current-scanner
runs, each timing one full replay-inventory scan through the harness entry point, with 3
discarded warm-up rounds, 30 measured repetitions, the median statistic, and a 250 ms
ceiling per version median. Trusted fleetyard-only decision gate; never CI-enforced and
never run on a self-hosted runner.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from doc_lattice.github_ci.direct_marker_scanner import scan_execution_source
from doc_lattice.github_ci.shell_scanner import scan_doc_lattice_invocations

INVENTORY = Path("tests/fixtures/github_ci_checkpoint/replay_inventory.json")
WARMUPS = 3
REPETITIONS = 30
CEILING_MS = 250.0


def _sources() -> list[str]:
    """Load every frozen replay-inventory source."""
    entries = json.loads(INVENTORY.read_text())["entries"]
    return [entry["source"] for entry in entries]


def _timed_pass(runner, sources) -> float:
    """Run one full inventory scan and return elapsed milliseconds."""
    start = time.perf_counter()
    for source in sources:
        runner(source)
    return (time.perf_counter() - start) * 1000.0


def main() -> int:
    """Run the pinned protocol and report per-version medians and the baseline ratio."""
    parser = argparse.ArgumentParser(description="issue #100 recognizer benchmark")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()
    sources = _sources()
    candidate_ms: list[float] = []
    baseline_ms: list[float] = []
    for _ in range(WARMUPS):
        _timed_pass(scan_execution_source, sources)
        _timed_pass(scan_doc_lattice_invocations, sources)
    for _ in range(REPETITIONS):
        candidate_ms.append(_timed_pass(scan_execution_source, sources))
        baseline_ms.append(_timed_pass(scan_doc_lattice_invocations, sources))
    candidate_median = statistics.median(candidate_ms)
    baseline_median = statistics.median(baseline_ms)
    result = {
        "python": sys.version.split()[0],
        "inventory_count": len(sources),
        "warmups": WARMUPS,
        "repetitions": REPETITIONS,
        "candidate_median_ms": round(candidate_median, 3),
        "baseline_median_ms": round(baseline_median, 3),
        "candidate_to_baseline_ratio": round(candidate_median / baseline_median, 3),
        "ceiling_ms": CEILING_MS,
        "within_ceiling": candidate_median <= CEILING_MS,
    }
    print(json.dumps(result, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["within_ceiling"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the protocol on fleetyard, both Python versions**

Close other workloads first (the protocol requires no concurrent load). Run:

```bash
env -u VIRTUAL_ENV uv run --python 3.13 --group dev python scripts/bench_recognizer_replay.py --json-out docs/research/recognizer-benchmark/py313.json
env -u VIRTUAL_ENV uv run --python 3.14 --group dev python scripts/bench_recognizer_replay.py --json-out docs/research/recognizer-benchmark/py314.json
```

Expected: exit 0 on both (the D3 recognizer is a small linear pass; the 250 ms ceiling is
ample). Record the two JSON files. If either median exceeds the ceiling, the wall-clock gate
also rejects the candidate: keep the honest numbers, note the second rejection ground for the
decision record, and continue (the evaluation verdict is already `rejected`).

- [ ] **Step 3: Commit**

```bash
git add scripts/bench_recognizer_replay.py docs/research/recognizer-benchmark
git commit -m "test: record fleetyard benchmark for the issue #100 candidate"
```

---

### Task 13: The decision record

**Files:**
- Create: `docs/superpowers/specs/2026-07-19-allowlist-recognizer-decision.md`

**Interfaces:**
- Consumes: every gate outcome (Tasks 5 to 12), `replay_records()` category counts, the
  benchmark JSON, the archived evidence (Task 11), and `git diff --stat 00737ca...HEAD`.
- Produces: the spec-mandated decision record that PR A lands and the issue #100 closing
  comment will cite.

- [ ] **Step 1: Gather the measured numbers**

Run and capture:

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python - <<'PY'
import collections, sys
sys.path.insert(0, "tests")
from github_ci_evaluation_harness import replay_records
counts = collections.Counter(record["category"] for record in replay_records())
print(dict(counts))
PY
git diff --stat 00737ca...HEAD -- src/doc_lattice
```

Also count the new modules' lines (`wc -l src/doc_lattice/github_ci/reachability.py
src/doc_lattice/github_ci/launcher_policy.py src/doc_lattice/github_ci/direct_marker_scanner.py`)
and the public symbols added (grep `^def \|^class ` per module).

- [ ] **Step 2: Write the record**

Create `docs/superpowers/specs/2026-07-19-allowlist-recognizer-decision.md` with exactly these
sections, filling every bracketed slot from the measured outputs of Step 1 and the earlier
tasks (no slot may survive into the commit):

1. **Verdict.** The D3 floor-grammar candidate is REJECTED by predeclared gate 6: candidate
   indeterminate 3 of 20 against a budget of 2, and newly indeterminate 3 of 20 against a
   budget of 2 (fixtures 02, 05, 14; command substitution with a pipe, a function
   definition, a literal `${{ }}` template). Per the spec's failure path, only corpus,
   harness, results, and decision evidence merge; the evaluation implementation stays in the
   merged harness as dormant modules, referenced here by commit SHA [sha of the Task 5
   commit]. The evaluation advances the parser-backed candidate (`mvdan/sh` family first).
2. **Gate results table.** One row per gate 1 through 9: gate name, budget, measured result,
   pass or fail. Gates 1 through 5, 7, 8, and 9 pass; gate 6 fails by construction (the
   recorded rejection). Replay divergence counts by category from Step 1; probe spans 36 of
   36; mutation sites 50 of 50; benchmark medians, ratio, and ceiling from Task 12's JSON.
3. **Contract removals.** The two D2 removals, restated with the spec's wording, marked
   "reported separately in every benchmark result" and never silently reclassified as safe.
4. **Predeclaration integrity.** Checkpoint commit [7d1d2b2 or its successor listed by
   `git log --oneline`], its owner-adjudicated amendments (insert-check substitution, replay
   inventory recapture), and the statement that no checkpoint file changed after freeze
   (`test_manifest_matches_artifacts` enforces this).
5. **Replacement-surface accounting.** Symbol- and diff-based accounting against baseline
   `00737ca`: lines and public symbols of the three new modules, the diff stat from Step 1,
   and the note that no production line of `shell_scanner.py` changed in PR A.
6. **Parser-backed candidate scoping.** Per the issue thread and its review comments: a
   doc-lattice-owned static helper built directly on an exactly pinned `mvdan.cc/sh/v3`
   syntax API (`LangBash`, source on stdin, doc-lattice-owned IR as the only protocol; the
   reflection-derived typed JSON is acceptable for differential oracles, never as the
   production protocol). The evaluation requires its own spec revision and a fresh
   predeclaration checkpoint; the Tier 3B corpus and this harness are reusable, but the
   expected outcomes must be re-derived under the successor grammar before any successor
   code runs.
7. **Evidence.** Links to the archived July 2026 benchmark (`docs/research/`, with its
   "internally consistent, not independently reproducible" label), the fleetyard benchmark
   JSON, and the frozen checkpoint.
8. **Release freeze.** PR A bumps nothing; the next release remains frozen until the
   successor evaluation lands a decision and PR B ships (spec Delivery and Release).

- [ ] **Step 3: Version-sync guard and commit**

Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/check_version_sync.py`
Expected: PASS (the record changes no version surface).

```bash
git add docs/superpowers/specs/2026-07-19-allowlist-recognizer-decision.md
git commit -m "docs: land the issue #100 allowlist recognizer decision record"
```

---

### Task 14: Full verification and PR A opening

**Files:**
- No new files; verification, push, and PR creation only.

- [ ] **Step 1: Complete handoff verification**

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest
env -u VIRTUAL_ENV uv run --group dev ruff check src tests scripts
env -u VIRTUAL_ENV uv run --group dev ruff format --check src tests scripts
env -u VIRTUAL_ENV uv run --group dev ty check src
env -u VIRTUAL_ENV uv run --group dev python scripts/check_typing_boundaries.py src
env -u VIRTUAL_ENV uv run --group dev python scripts/check_version_sync.py
git diff --check
```

Expected: all PASS, coverage at or above 80 percent, every gate green including gate 6's
recorded rejection.

- [ ] **Step 2: Push and open PR A as a draft**

```bash
git push -u origin feature/issue-100-allowlist
gh pr create --draft --title "Evaluate allowlist recognizer for issue #100 (PR A: evaluation and decision)" --body "$(cat <<'BODY'
PR A of the issue #100 stacked delivery: the predeclaration checkpoint, the D3 floor-grammar
candidate, every predeclared evaluation gate, and the decision record.

Verdict: the D3 candidate is REJECTED by predeclared Tier 3B budgets (3/20 total
indeterminate and 3/20 newly indeterminate against caps of 2; fixtures 02, 05, 14). All
other gates pass. Per the spec's failure path, the corpus, harnesses, results, and decision
record merge; the evaluation implementation remains as dormant modules; runtime audit
behavior is unchanged. The decision record advances the mvdan/sh-family candidate.

- Spec: docs/superpowers/specs/2026-07-19-allowlist-recognizer-design.md
- Decision record: docs/superpowers/specs/2026-07-19-allowlist-recognizer-decision.md
- Checkpoint: first reviewed commit of this branch, immutable under MANIFEST.sha256
- Evidence: docs/research/ (July 2026 parser benchmark, fleetyard benchmark JSON)

No release: PR A bumps nothing; the release freeze holds until the successor evaluation and
PR B.
BODY
)"
```

- [ ] **Step 3: Report and stop**

Report the PR URL, the final gate summary, and the benchmark numbers to the project owner.
Do NOT merge; PR A review is the owner's gate. The issue #100 closing comment is written
only after both PRs exist (spec Delivery), so it is out of scope here.

---

## Self-Review

- Spec coverage: D1 (Task 2), D2/D3/D4 (Tasks 3 to 5), D5 types (Task 1; rendering and exit
  wiring are PR B), D6 (Task 7 orchestrator), gates 1 (Task 5), 2 (Task 6), 3 and 4
  (Task 7), 5 and 6 (Task 8), 7 (Task 9), 8 and 9 CI half (Task 10), 9 trusted half
  (Task 12), evidence archive (Task 11), decision record (Task 13), PR A delivery (Task 14).
- Deliberately out of scope, per spec and owner rulings: PR B wiring (`audit.py`,
  `cli/commands/ci.py`, rendering, exit precedence), deleting `shell_scanner.py`,
  authoritative doc updates, any release step, the issue closing comment, and the mvdan/sh
  evaluation itself (new spec plus new checkpoint, scoped by the decision record).
- The recognizer is deliberately correct-but-not-polished (owner ruling 1a): unit tasks give
  it exactly the surface the gates need (`scan_execution_source`, `DIRECT_MARKER_RE`,
  `certified_command_words`) and nothing more.
- Type-consistency check: `BlockScan`, `ScanWord`, `CandidateResolution`,
  `resolve_command`, `scan_execution_source`, `certified_command_words`,
  `job_is_pr_reachable`, harness names (`old_scan`, `classify_divergence`,
  `replay_records`, `evaluate_workflow`, `tier3b_run_block`) are used with identical
  spellings across all tasks.
- Frozen-oracle discipline: every task that could be tempted to adjust an expectation
  carries an explicit STOP-and-report instruction instead.
