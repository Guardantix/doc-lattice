# Issue #100 Predeclaration Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce the frozen, content-hashed predeclaration checkpoint that PR A's evaluation
gates consume, as defined by
[the approved spec](../specs/2026-07-19-allowlist-recognizer-design.md), section
"Predeclaration checkpoint".

**Architecture:** Pure data plus small authoring tools. Every gate input (labels, inventories,
fixtures, mutation sites, probe inputs, limits, protocol) is authored or extracted now, hashed
into a manifest, and enforced immutable by a CI test. No recognizer code exists yet; a second
plan implements the recognizer and gates against these frozen artifacts after this checkpoint
is reviewed and committed.

**Tech Stack:** Python 3.13+ via uv, pytest, `gh` CLI (Tier 3B mining), system Bash 5.2.21,
Docker (container digest pin).

## Global Constraints

- Spec is authoritative: `docs/superpowers/specs/2026-07-19-allowlist-recognizer-design.md`.
  Baseline SHA for everything: `00737ca`.
- Work on branch `feature/issue-100-allowlist-spec`. Commit after every task; Task 8 squashes
  the series into the single reviewed checkpoint commit.
- Run Python through uv with the dev shell quirks neutralized:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest ...`.
- Every new `.py` file: module docstring, Google-style docstrings on public functions, Ruff
  100-char lines, no `typing.Any`, no `typing.cast`, no bare `except Exception`, no
  `datetime.now()` (dates in artifacts are typed literally by the author).
- No em dashes in any drafted content. No Claude attribution in commits.
- Checkpoint artifact directory: `tests/fixtures/github_ci_checkpoint/`. After Task 8 these
  files are immutable for the remainder of PR A.
- Marker regex everywhere: `re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)`.
- The frozen reason-category vocabulary (used by labels, Tier 3A/3B expectations, and
  mutations): `control-character`, `unsupported-operator`, `unsupported-expansion`,
  `unquoted-expansion-in-command-word`, `quote-spans-newline`, `unterminated-quote`,
  `control-flow-keyword`, `assignment-prefix`, `unstable-first-word`, `policy-unresolvable`,
  `cap-exceeded`.
- The frozen status vocabulary: `not_applicable`, `certified`, `uninspectable`.

---

### Task 1: Checkpoint scaffolding, manifest tool, integrity test

**Files:**
- Create: `scripts/checkpoint_manifest.py`
- Create: `tests/fixtures/github_ci_checkpoint/README.md`
- Create: `tests/fixtures/github_ci_checkpoint/limits.json`
- Create: `tests/fixtures/github_ci_checkpoint/benchmark_protocol.md`
- Create: `tests/fixtures/github_ci_checkpoint/category_d_exceptions.json`
- Create: `tests/test_github_ci_checkpoint.py`

**Interfaces:**
- Produces: `scripts/checkpoint_manifest.py` CLI with `--write` and `--check` modes over
  `tests/fixtures/github_ci_checkpoint/`, emitting/verifying `MANIFEST.sha256` (lines of
  `<sha256>  <posix-relpath>`, sorted, all files except the manifest itself). Later tasks
  re-run `--write`; Task 8 freezes the final manifest.
- Produces: `tests/test_github_ci_checkpoint.py::test_manifest_matches_artifacts`, the
  permanent immutability gate.

- [ ] **Step 1: Write the failing integrity test**

```python
"""Integrity gates for the issue #100 predeclaration checkpoint artifacts."""

import json
import subprocess
import sys
from pathlib import Path

CHECKPOINT = Path("tests/fixtures/github_ci_checkpoint")

STATUSES = frozenset({"not_applicable", "certified", "uninspectable"})
REASON_CATEGORIES = frozenset(
    {
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
)


def test_manifest_matches_artifacts():
    result = subprocess.run(
        [sys.executable, "scripts/checkpoint_manifest.py", "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_limits_match_spec():
    limits = json.loads((CHECKPOINT / "limits.json").read_text())
    assert limits["source_cap_chars"] == 1_048_576
    assert limits["invocation_cap"] == 10_000
    assert limits["token_cap"] == 262_144
    assert limits["statement_cap"] == 65_536
    assert limits["work_limit"] == "min(4194304, 4 * source_length + 4096)"
    assert set(limits["charges"]) == {
        "marker_pass",
        "character_examined",
        "token_emitted",
        "statement_closed",
        "policy_step",
    }


def test_category_d_exceptions_is_empty():
    assert json.loads((CHECKPOINT / "category_d_exceptions.json").read_text()) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: FAIL (checkpoint files and script do not exist).

- [ ] **Step 3: Write the manifest tool**

```python
"""Generate or verify the SHA-256 manifest for the issue #100 predeclaration checkpoint.

The manifest freezes every checkpoint artifact. ``--write`` regenerates it during checkpoint
authoring; ``--check`` (used by tests and CI) fails when any artifact drifts from the frozen
hashes, enforcing the spec's immutability rule for the remainder of PR A.
"""

import hashlib
import sys
from pathlib import Path

CHECKPOINT_DIR = Path("tests/fixtures/github_ci_checkpoint")
MANIFEST = CHECKPOINT_DIR / "MANIFEST.sha256"


def _entries() -> list[str]:
    """Return sorted ``<sha256>  <relpath>`` lines for every artifact except the manifest."""
    lines: list[str] = []
    for path in sorted(CHECKPOINT_DIR.rglob("*")):
        if path.is_dir() or path == MANIFEST:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(CHECKPOINT_DIR).as_posix()}")
    return lines


def main() -> int:
    """Write or verify the manifest per the single CLI argument."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    current = "\n".join(_entries()) + "\n"
    if mode == "--write":
        MANIFEST.write_text(current)
        return 0
    if not MANIFEST.exists():
        print("checkpoint manifest missing", file=sys.stderr)
        return 1
    if MANIFEST.read_text() != current:
        print("checkpoint artifacts drifted from MANIFEST.sha256", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Write the data artifacts**

`tests/fixtures/github_ci_checkpoint/limits.json`:

```json
{
  "source_cap_chars": 1048576,
  "invocation_cap": 10000,
  "token_cap": 262144,
  "statement_cap": 65536,
  "work_limit": "min(4194304, 4 * source_length + 4096)",
  "charges": {
    "marker_pass": "source_length, charged once",
    "character_examined": "1 per character the tokenizer examines",
    "token_emitted": "1 per token",
    "statement_closed": "1 per statement",
    "policy_step": "1 per policy step"
  }
}
```

`tests/fixtures/github_ci_checkpoint/category_d_exceptions.json`:

```json
[]
```

`tests/fixtures/github_ci_checkpoint/benchmark_protocol.md`: transcribe spec checkpoint item 7
verbatim (fleetyard-VM, no concurrent workload, CPython 3.13 and 3.14 via uv, timed scope of
one full replay-inventory scan through the harness entry point, 3 discarded warm-up runs, 30
measured repetitions per Python version, median per version, ceiling 250 ms per median,
interleaved current-scanner baseline with reported ratio, exceeding the ceiling rejects the
candidate, trusted fleetyard-only decision gate, never a self-hosted runner).

`tests/fixtures/github_ci_checkpoint/README.md`: one paragraph stating what the checkpoint is,
that files are immutable for the remainder of PR A per the spec, and one line per artifact
file naming which spec checkpoint item it satisfies.

- [ ] **Step 5: Generate the manifest, run the test, commit**

Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`
Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS (3 tests).

```bash
git add scripts/checkpoint_manifest.py tests/fixtures/github_ci_checkpoint tests/test_github_ci_checkpoint.py
git commit -m "test: scaffold issue #100 predeclaration checkpoint"
```

---

### Task 2: Frozen replay inventory

**Files:**
- Create: `scripts/checkpoint_record_scanner_inputs.py`
- Create: `tests/fixtures/github_ci_checkpoint/replay_inventory.json`
- Modify: `tests/test_github_ci_checkpoint.py` (append tests)

**Interfaces:**
- Produces: `replay_inventory.json` with shape
  `{"count": int, "aggregate_sha256": str, "entries": [{"id": "replay-0001", "sha256": str, "source": str}]}`,
  entries sorted by `sha256`, ids assigned in that order. Plan 2's replay gate iterates
  `entries` and re-verifies `count` and `aggregate_sha256`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_github_ci_checkpoint.py`:

```python
def test_replay_inventory_is_internally_consistent():
    import hashlib

    inventory = json.loads((CHECKPOINT / "replay_inventory.json").read_text())
    entries = inventory["entries"]
    assert inventory["count"] == len(entries) > 0
    hashes = [entry["sha256"] for entry in entries]
    assert hashes == sorted(hashes)
    for index, entry in enumerate(entries, start=1):
        assert entry["id"] == f"replay-{index:04d}"
        assert hashlib.sha256(entry["source"].encode()).hexdigest() == entry["sha256"]
    aggregate = hashlib.sha256("\n".join(hashes).encode()).hexdigest()
    assert inventory["aggregate_sha256"] == aggregate


def test_replay_inventory_covers_acceptance_corpus():
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES

    inventory = json.loads((CHECKPOINT / "replay_inventory.json").read_text())
    sources = {entry["source"] for entry in inventory["entries"]}
    missing = [d for d, script, _ in ACCEPTANCE_CASES if script not in sources]
    assert missing == []
```

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: the two new tests FAIL (file missing).

- [ ] **Step 2: Write the recording pytest plugin**

```python
"""Pytest plugin that records every source scanned by the legacy shell scanner suite.

Loaded with ``pytest -p scripts.checkpoint_record_scanner_inputs`` while running
``tests/test_github_ci_shell_scanner.py``. It wraps the public scanner entry point before test
modules import it, deduplicates recorded sources by SHA-256, and writes the frozen replay
inventory on exit when ``CHECKPOINT_REPLAY_OUT`` is set.
"""

import hashlib
import json
import os
from pathlib import Path

from doc_lattice.github_ci import shell_scanner

_RECORDS: dict[str, str] = {}
_ORIGINAL = shell_scanner.direct_doc_lattice_invocations


def _recording(source, *args, **kwargs):
    """Record ``source`` and delegate to the real scanner."""
    _RECORDS[hashlib.sha256(source.encode()).hexdigest()] = source
    return _ORIGINAL(source, *args, **kwargs)


def pytest_configure(config):
    """Install the recording wrapper before test modules import the scanner."""
    shell_scanner.direct_doc_lattice_invocations = _recording


def pytest_unconfigure(config):
    """Write the inventory and restore the original entry point."""
    shell_scanner.direct_doc_lattice_invocations = _ORIGINAL
    out = os.environ.get("CHECKPOINT_REPLAY_OUT")
    if not out:
        return
    hashes = sorted(_RECORDS)
    entries = [
        {"id": f"replay-{index:04d}", "sha256": digest, "source": _RECORDS[digest]}
        for index, digest in enumerate(hashes, start=1)
    ]
    inventory = {
        "count": len(entries),
        "aggregate_sha256": hashlib.sha256("\n".join(hashes).encode()).hexdigest(),
        "entries": entries,
    }
    Path(out).write_text(json.dumps(inventory, indent=2) + "\n")
```

- [ ] **Step 3: Extract the inventory**

Run:

```bash
CHECKPOINT_REPLAY_OUT=tests/fixtures/github_ci_checkpoint/replay_inventory.json \
  env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest \
  tests/test_github_ci_shell_scanner.py -p scripts.checkpoint_record_scanner_inputs -q
```

Expected: existing suite PASSES unchanged; `replay_inventory.json` is written. Inspect it:
entry count must exceed 78 (parameterized inputs beyond `ACCEPTANCE_CASES` are the point).
Record the count for the Task 8 review notes.

- [ ] **Step 4: Refresh manifest, run tests, commit**

Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`
Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS.

```bash
git add scripts/checkpoint_record_scanner_inputs.py tests/fixtures/github_ci_checkpoint tests/test_github_ci_checkpoint.py
git commit -m "test: freeze replay inventory for issue #100 checkpoint"
```

---

### Task 3: The 78 acceptance-case labels

**Files:**
- Create: `tests/fixtures/github_ci_checkpoint/acceptance_labels.json`
- Modify: `tests/test_github_ci_checkpoint.py` (append tests)

**Interfaces:**
- Produces: `acceptance_labels.json` with shape
  `{"cases": [{"description": str, "label": str, "expected_status": str, "expected_invocations": [[str, bool]], "reason_category": str | absent}]}`
  in `ACCEPTANCE_CASES` order. Labels: `must-certify`, `intentional-exit-2`,
  `outside-direct-marker-contract`.

**Derivation algorithm (apply by hand to each of the 78 cases, in order):**

1. Marker test: if `re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)` has no match in
   the script, the label is `outside-direct-marker-contract`, `expected_status`
   `not_applicable`, `expected_invocations` `[]`, no `reason_category`.
2. Otherwise walk the script against the D3 checklist below, left to right. If every statement
   certifies, the label is `must-certify`, `expected_status` `certified`, and
   `expected_invocations` must equal the case's current expected tuple exactly. If your
   hand-derived invocations differ from the current expected column, STOP and report the case
   to the reviewer; that is a spec conflict, not something to reconcile silently.
3. Otherwise the label is `intentional-exit-2`, `expected_status` `uninspectable`,
   `reason_category` = the category of the earliest unsupported construct by offset, and
   `expected_invocations` = invocations from statements fully certified before that offset
   (monotonic evidence; usually `[]`).

**D3 checklist (each item cites its reason category):**

- Carriage return or C0 control other than newline/tab anywhere: `control-character`.
- Unquoted `|` (not `||`), `&` (not `&&`), `<`, `>`, `(`, `)`, backtick, backslash, brace
  outside `${NAME}`, heredoc introducer, redirection: `unsupported-operator`.
- Control-flow keyword in command position (`if then elif else fi while until do done for
  case esac function ! time coproc`): `control-flow-keyword`.
- `$` sequence other than `$?`, `$NAME`, `${NAME}`: `unsupported-expansion`.
- Permitted expansion unquoted in a command word: `unquoted-expansion-in-command-word`.
- Quote spanning a newline: `quote-spans-newline`; unterminated quote: `unterminated-quote`.
- `NAME=value` followed by more words in the same command: `assignment-prefix`.
- First word of a command unstable (contains expansion, unquoted `*` `?` `[` `]` except the
  exact words `[` or `]`, or leading `~`) or otherwise non-literal: `unstable-first-word`.
- Candidate resolution hits an unstable word before its subcommand is established, or an
  unresolvable policy word: `policy-unresolvable`.
- Mid-word `#`: `unsupported-operator`.

**Worked examples (these exact five rows must appear in the file):**

```json
{"description": "ansi-c executable", "label": "intentional-exit-2",
 "expected_status": "uninspectable", "expected_invocations": [],
 "reason_category": "unsupported-expansion"}
{"description": "concatenated quoted words", "label": "outside-direct-marker-contract",
 "expected_status": "not_applicable", "expected_invocations": []}
{"description": "elif condition", "label": "intentional-exit-2",
 "expected_status": "uninspectable", "expected_invocations": [],
 "reason_category": "control-flow-keyword"}
{"description": "runtime-unreachable command remains conservative", "label": "must-certify",
 "expected_status": "certified", "expected_invocations": [["linear", false]]}
{"description": "time reserved word", "label": "intentional-exit-2",
 "expected_status": "uninspectable", "expected_invocations": [],
 "reason_category": "control-flow-keyword"}
```

- [ ] **Step 1: Write the failing validation tests**

Append to `tests/test_github_ci_checkpoint.py`:

```python
def test_acceptance_labels_align_with_corpus():
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES

    labels = json.loads((CHECKPOINT / "acceptance_labels.json").read_text())["cases"]
    assert len(labels) == len(ACCEPTANCE_CASES) == 78
    for row, (description, _script, expected) in zip(labels, ACCEPTANCE_CASES, strict=True):
        assert row["description"] == description
        assert row["label"] in {
            "must-certify",
            "intentional-exit-2",
            "outside-direct-marker-contract",
        }
        assert row["expected_status"] in STATUSES
        if row["label"] == "must-certify":
            assert row["expected_status"] == "certified"
            assert [tuple(i) for i in row["expected_invocations"]] == list(expected)
        if row["label"] == "outside-direct-marker-contract":
            assert row["expected_status"] == "not_applicable"
            assert row["expected_invocations"] == []
        if row["label"] == "intentional-exit-2":
            assert row["expected_status"] == "uninspectable"
            assert row["reason_category"] in REASON_CATEGORIES


def test_acceptance_labels_marker_consistency():
    import re

    from test_github_ci_shell_scanner import ACCEPTANCE_CASES

    marker = re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)
    labels = json.loads((CHECKPOINT / "acceptance_labels.json").read_text())["cases"]
    for row, (_d, script, _e) in zip(labels, ACCEPTANCE_CASES, strict=True):
        has_marker = bool(marker.search(script))
        assert (row["label"] == "outside-direct-marker-contract") == (not has_marker)
```

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: new tests FAIL (file missing).

- [ ] **Step 2: Author all 78 rows by hand using the algorithm**

Work straight down `ACCEPTANCE_CASES` (`tests/test_github_ci_shell_scanner.py:28`). For every
row record the checklist item that fired first (by offset) as `reason_category`. Do not run
any scanner while labeling; this is predeclaration.

- [ ] **Step 3: Run validation, refresh manifest, commit**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS.
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`

```bash
git add tests/fixtures/github_ci_checkpoint tests/test_github_ci_checkpoint.py
git commit -m "test: predeclare 78 acceptance-case labels for issue #100"
```

---

### Task 4: Tier 3A documented-conformance fixtures

**Files:**
- Create: `tests/fixtures/github_ci_checkpoint/tier3a_cases.json`
- Modify: `tests/test_github_ci_checkpoint.py` (append test)

**Interfaces:**
- Produces: `tier3a_cases.json` with shape
  `{"cases": [{"id": str, "origin": str, "source": str, "expected_status": str, "expected_invocations": [[str, bool]], "reason_category": str | absent}]}`.

- [ ] **Step 1: Author the fixture file with exactly these cases**

Sources are `run:` block content. Expected invocations for the two multi-invocation blocks are
recorded from the current scanner (`env -u VIRTUAL_ENV uv run python -c "from
doc_lattice.github_ci.shell_scanner import direct_doc_lattice_invocations as f;
print(f(open('/tmp/block.txt').read()))"`) and then hand-verified against D3; Tier 3A expected
values may consult the current scanner because compatibility on documented shapes is exactly
what this tier freezes (Tier 3B expectations, by contrast, stay independent).

1. `direct-audit`, origin `README managed workflow docs`:
   `doc-lattice ci audit --repository OWNER/REPO`, certified.
2. `direct-check`, origin `README`: `doc-lattice check`, certified, `[["check", false]]`.
3. `direct-lint`, origin `README`: `doc-lattice lint`, certified, `[["lint", false]]`.
4. `uvx-pinned-audit`, origin `README:560 single-line form`:
   `uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice ci audit --repository OWNER/REPO`,
   certified.
5. `uvx-continuation`, origin `README:560 verbatim two-line form`: the same command with the
   trailing backslash and continuation line exactly as printed. Expected `uninspectable`,
   `reason_category` `unsupported-operator`. This is a documented terminal procedure; copied
   into a workflow it fails closed, and this fixture freezes that expectation.
6. `uv-run-help`, origin `README:196`: `uv run doc-lattice --help`, certified (help
   disposition per current policy; record from current scanner).
7. `uv-run-no-sync`, origin `repository ci.yml style`: `uv run --no-sync doc-lattice check`,
   certified, `[["check", false]]`.
8. `uv-tool-run-long-form`, origin `README:640 supported uv forms`:
   `uv tool run doc-lattice check`, certified, `[["check", false]]`.
9. `dynamic-non-policy-arg`, origin `spec Tier 3A shape list`:
   `doc-lattice check --config "$CFG"`, certified, `[["check", false]]`.
10. `conditional-list-and`, origin `spec Tier 3A shape list`:
    `doc-lattice check && doc-lattice lint`, certified,
    `[["check", false], ["lint", false]]`.
11. `conditional-list-or`, origin `spec Tier 3A shape list`: `doc-lattice lint || exit 1`,
    certified, `[["lint", false]]`.
12. `offline-template-block`, origin `render.py offline template`: the full rendered PR block
    (`set +e` through the bracket-test conjunction) with `__PYTHON_PIN__`, `__VERSION__`,
    `__REPOSITORY__` replaced by `3.13`, `2.0.0`, `OWNER/REPO`. Certified; expected
    invocations recorded from the current scanner and hand-verified.
13. `linear-template-quoted-path`, origin `render.py:114`:
    `'"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code'` body form. Expected
    `uninspectable`, `reason_category` `unstable-first-word` (out of PR scan in production,
    frozen here as the documented fail-closed shape).

- [ ] **Step 2: Write the failing validation test, then make it pass**

```python
def test_tier3a_cases_are_well_formed():
    cases = json.loads((CHECKPOINT / "tier3a_cases.json").read_text())["cases"]
    assert [case["id"] for case in cases] == [
        "direct-audit", "direct-check", "direct-lint", "uvx-pinned-audit",
        "uvx-continuation", "uv-run-help", "uv-run-no-sync", "uv-tool-run-long-form",
        "dynamic-non-policy-arg", "conditional-list-and", "conditional-list-or",
        "offline-template-block", "linear-template-quoted-path",
    ]
    for case in cases:
        assert case["expected_status"] in STATUSES
        if case["expected_status"] == "uninspectable":
            assert case["reason_category"] in REASON_CATEGORIES
```

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS after the file is authored.

- [ ] **Step 3: Refresh manifest and commit**

Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`

```bash
git add tests/fixtures/github_ci_checkpoint tests/test_github_ci_checkpoint.py
git commit -m "test: predeclare Tier 3A conformance fixtures for issue #100"
```

---

### Task 5: Tier 3B empirical envelopes

**Files:**
- Create: `tests/fixtures/github_ci_checkpoint/tier3b/fixture-01.yml` ... `fixture-20.yml`
- Create: `tests/fixtures/github_ci_checkpoint/tier3b/provenance.json`
- Modify: `tests/test_github_ci_checkpoint.py` (append test)

**Interfaces:**
- Produces: 20 minimal workflow fixtures, each a complete YAML workflow with a
  `pull_request` trigger and exactly one marker-bearing `run:` block, plus
  `provenance.json` with shape `{"selection_queries": [str], "retrieved": "2026-MM-DD",
  "normalization": str, "fixtures": [{"id": str, "source_url": str, "source_commit": str,
  "query_index": int, "expected_status": str, "expected_invocations": [[str, bool]],
  "reason_category": str | absent}]}`.

- [ ] **Step 1: Record the frozen selection queries in provenance.json first**

```json
"selection_queries": [
  "gh search code --filename '*.yml' --path '.github/workflows' 'uv run' --limit 50",
  "gh search code --filename '*.yml' --path '.github/workflows' 'uvx' --limit 50",
  "gh search code --filename '*.yml' --path '.github/workflows' 'run: pytest' --limit 50"
]
```

Selection rule (record verbatim in `normalization`): iterate each query's results in returned
order; skip files without a `pull_request` or `pull_request_target` trigger, blocks longer
than 40 lines, and repositories already selected; take the first 20 accepted `run:` blocks
overall, at most one per repository. Record each source file's permalink URL and commit SHA
(`gh browse` style permalink from the search result).

- [ ] **Step 2: Mine and normalize the 20 fixtures**

For each accepted block, apply the mechanical substitution (record verbatim in
`normalization`): replace the invoked console-script token with `doc-lattice` and its first
positional argument with `check`; in `uvx`/`uv run`/`uv tool run` forms, replace the payload
distribution or executable word with `doc-lattice` (preserving any `@spec` or `==version`
suffix shape) and the payload's first positional argument with `check`; leave every other
word, option, value, and all surrounding shell structure byte-identical. Wrap the block in
this minimal workflow skeleton:

```yaml
name: tier3b-fixture-NN
on:
  pull_request:
    branches: [main]
jobs:
  job:
    runs-on: ubuntu-latest
    steps:
      - run: |
          <normalized block, indented>
```

- [ ] **Step 3: Assign independent expected outcomes**

For each fixture, hand-derive `expected_status`, `expected_invocations`, and
`reason_category` using only the Task 3 D3 checklist. Do not run the current scanner or any
parser on Tier 3B fixtures; independence of these expectations is a spec requirement. A
second person or a fresh review pass re-derives any fixture whose expectation feels unclear
before freezing.

- [ ] **Step 4: Write the failing validation test, then make it pass**

```python
def test_tier3b_fixtures_are_well_formed():
    import re

    marker = re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)
    tier3b = CHECKPOINT / "tier3b"
    provenance = json.loads((tier3b / "provenance.json").read_text())
    fixtures = provenance["fixtures"]
    assert len(fixtures) == 20
    repos = set()
    for row in fixtures:
        path = tier3b / f"{row['id']}.yml"
        text = path.read_text()
        assert marker.search(text), row["id"]
        assert "pull_request" in text
        assert row["source_url"].startswith("https://")
        assert len(row["source_commit"]) == 40
        repo = "/".join(row["source_url"].split("/")[3:5])
        assert repo not in repos
        repos.add(repo)
        assert row["expected_status"] in STATUSES
        if row["expected_status"] == "uninspectable":
            assert row["reason_category"] in REASON_CATEGORIES
```

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS.

- [ ] **Step 5: Refresh manifest and commit**

Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`

```bash
git add tests/fixtures/github_ci_checkpoint tests/test_github_ci_checkpoint.py
git commit -m "test: freeze Tier 3B empirical envelopes for issue #100"
```

---

### Task 6: Probe inputs and the Bash pin

**Files:**
- Create: `scripts/checkpoint_derive_probe_spans.py`
- Create: `tests/fixtures/github_ci_checkpoint/probes.json`
- Create: `tests/fixtures/github_ci_checkpoint/bash_pin.json`
- Modify: `tests/test_github_ci_checkpoint.py` (append test)

**Interfaces:**
- Produces: `probes.json` with shape `{"env": {str: str}, "synthesis_rules": str,
  "spans": [{"fixture_id": str, "span_id": str, "start": int, "end": int, "text": str,
  "expected_stable_argv_prefix": [str], "expected_verdict": {"subcommand": str,
  "dry_run": bool} | null}]}` covering every `must-certify` acceptance case and every
  certified Tier 3A/3B fixture; `bash_pin.json` with `{"version": str, "container": str,
  "local_binary_sha256": str}`.

- [ ] **Step 1: Write the span-derivation helper**

This is checkpoint authoring tooling, frozen with the checkpoint; the future recognizer must
reproduce these spans, which is an extra consistency check. It intentionally implements only
the D3 statement split, nothing more.

```python
"""Derive candidate-command spans for issue #100 checkpoint probes.

Splits certified fixture sources on the frozen floor-grammar statement boundaries (unquoted
newline, ``;``, ``&&``, ``||``) and emits one span per statement whose first word is a
doc-lattice launcher or executable literal. Output is reviewed by hand and frozen; it is
authoring tooling, not the recognizer.
"""

import json
import re
import sys

MARKER = re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)
LAUNCHERS = ("doc-lattice", "uvx", "uv")


def spans_for(source: str) -> list[dict[str, object]]:
    """Return candidate spans as dicts with start, end, and text."""
    spans: list[dict[str, object]] = []
    start = 0
    index = 0
    quote: str | None = None
    length = len(source)
    while index <= length:
        char = source[index] if index < length else "\n"
        two = source[index : index + 2]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        boundary = char in "\n;" or two in ("&&", "||")
        if boundary or index == length:
            text = source[start:index]
            stripped = text.strip()
            first = stripped.split(" ", 1)[0] if stripped else ""
            if first in LAUNCHERS or MARKER.search(first):
                offset = start + (len(text) - len(text.lstrip()))
                spans.append({"start": offset, "end": offset + len(stripped), "text": stripped})
            index += 2 if two in ("&&", "||") else 1
            start = index
            continue
        index += 1
    return spans


if __name__ == "__main__":
    print(json.dumps(spans_for(sys.stdin.read()), indent=2))
```

- [ ] **Step 2: Author probes.json**

Run the helper over each `must-certify` acceptance script and each certified Tier 3A/3B
fixture block, review every emitted span by hand, then for each span record:

- `expected_stable_argv_prefix`: the words of the span, left to right, stopping before the
  first unstable word (expansion, glob, tilde). Words are dequoted (`"x"` records as `x`).
- `expected_verdict`: `{"subcommand": ..., "dry_run": ...}` for spans the checkpoint labels as
  doc-lattice invocations, `null` for launcher spans that resolve away from doc-lattice.

Set `env` to cover every `$NAME` used in any certified span, with glob-free, space-free
values:

```json
"env": {"CFG": "/tmp/probe-cfg.yml", "OPTION": "--verbose", "RUNNER_TEMP": "/tmp/probe-rt"}
```

Extend this map if the certified Tier 3B fixtures reference additional names; the Plan 2
probe harness fails on any unlisted name.

Set `synthesis_rules` to this exact text: "One probe per span. Probe body is the span text
only. Probes run under the pinned Bash with the env map above and PATH containing only the
recorder stubs for doc-lattice, uvx, and uv. One probe per list arm; arms are never joined.
Original fixture text is never executed."

- [ ] **Step 3: Author bash_pin.json**

Run: `bash --version | head -1` (expect `GNU bash, version 5.2.21(1)-release`).
Run: `sha256sum /bin/bash` and record the digest.
Run: `docker pull ubuntu:24.04 && docker inspect --format='{{index .RepoDigests 0}}' ubuntu:24.04`
and record the pinned reference. If Docker is unavailable on this machine, record the digest
from the GitHub-hosted `ubuntu-24.04` runner image release notes and note the source.

```json
{
  "version": "5.2.21(1)-release",
  "container": "ubuntu:24.04@sha256:<recorded>",
  "local_binary_sha256": "<recorded>"
}
```

- [ ] **Step 4: Validation test, manifest, commit**

```python
def test_probes_and_bash_pin_are_well_formed():
    probes = json.loads((CHECKPOINT / "probes.json").read_text())
    assert probes["spans"], "probe span inventory must not be empty"
    for span in probes["spans"]:
        assert span["end"] > span["start"] >= 0
        assert span["text"]
        assert isinstance(span["expected_stable_argv_prefix"], list)
    pin = json.loads((CHECKPOINT / "bash_pin.json").read_text())
    assert pin["version"] == "5.2.21(1)-release"
    assert pin["container"].startswith("ubuntu:24.04@sha256:")
    assert len(pin["local_binary_sha256"]) == 64
```

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS.
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`

```bash
git add scripts/checkpoint_derive_probe_spans.py tests/fixtures/github_ci_checkpoint tests/test_github_ci_checkpoint.py
git commit -m "test: freeze probe inputs and Bash pin for issue #100 checkpoint"
```

---

### Task 7: Boundary-mutation set

**Files:**
- Create: `tests/fixtures/github_ci_checkpoint/mutations.json`
- Modify: `tests/test_github_ci_checkpoint.py` (append test)

**Interfaces:**
- Produces: `mutations.json` with shape `{"kinds": {str: str},
  "sites": [{"fixture_id": str, "span_id": str, "kind": str, "offset": int,
  "inserted_text": str, "expected_reason_category": str}]}`.

- [ ] **Step 1: Author the mutation kinds (frozen vocabulary)**

```json
"kinds": {
  "backslash-newline": "insert \\\n between the executable and subcommand words",
  "backtick-wrap": "wrap the subcommand word in backticks",
  "unterminated-substitution": "insert $( before the subcommand word",
  "unquoted-expansion": "insert the word $X before the subcommand word",
  "heredoc-introducer": "append <<EOF to the span",
  "pipe-suffix": "append | cat to the span",
  "redirection-suffix": "append > out.txt to the span",
  "subshell-wrap": "wrap the whole span in ( and )",
  "control-flow-prefix": "prefix the span with if ",
  "brace-payload": "replace the subcommand word with {check,lint}"
}
```

- [ ] **Step 2: Author the sites**

For every span in `probes.json` whose fixture is a `must-certify` acceptance case (skip Tier
3B to keep the set reviewable), emit one site per applicable kind. `offset` is computed from
the span fields (for insert-before-subcommand kinds: the offset of the span's second word;
for suffix kinds: `end`; for wrap kinds: `start`). Every site is live executable syntax by
construction because spans exclude comments and quoted contexts. Expected categories:
`backslash-newline`, `backtick-wrap`, `pipe-suffix`, `redirection-suffix`, `subshell-wrap`,
`heredoc-introducer`, `brace-payload` map to `unsupported-operator`;
`unterminated-substitution` maps to `unsupported-expansion`; `unquoted-expansion` maps to
`unquoted-expansion-in-command-word` at every site (an unquoted expansion in a command word is
a D3 grammar refusal regardless of whether a subcommand is established; the spec governs);
`control-flow-prefix` maps to `control-flow-keyword`.

- [ ] **Step 3: Validation test, manifest, commit**

```python
def test_mutation_sites_reference_real_spans():
    probes = json.loads((CHECKPOINT / "probes.json").read_text())
    span_ids = {(s["fixture_id"], s["span_id"]) for s in probes["spans"]}
    mutations = json.loads((CHECKPOINT / "mutations.json").read_text())
    assert mutations["sites"], "mutation set must not be empty"
    for site in mutations["sites"]:
        assert site["kind"] in mutations["kinds"]
        assert (site["fixture_id"], site["span_id"]) in span_ids
        assert site["expected_reason_category"] in REASON_CATEGORIES
        assert site["offset"] >= 0
```

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS.
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`

```bash
git add tests/fixtures/github_ci_checkpoint tests/test_github_ci_checkpoint.py
git commit -m "test: freeze boundary-mutation set for issue #100 checkpoint"
```

---

### Task 8: Freeze, verify, squash to the single checkpoint commit

**Files:**
- Modify: `tests/fixtures/github_ci_checkpoint/MANIFEST.sha256` (final write)

- [ ] **Step 1: Final manifest write and full verification**

Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/checkpoint_manifest.py --write`
Run the complete handoff set:

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest
env -u VIRTUAL_ENV uv run --group dev ruff check src tests scripts
env -u VIRTUAL_ENV uv run --group dev ruff format --check src tests scripts
env -u VIRTUAL_ENV uv run --group dev ty check src
env -u VIRTUAL_ENV uv run --group dev python scripts/check_typing_boundaries.py src
env -u VIRTUAL_ENV uv run --group dev python scripts/check_version_sync.py
```

Expected: all PASS. The full suite must pass unchanged; this plan adds only data, tooling,
and checkpoint tests.

- [ ] **Step 2: Squash the checkpoint series into one reviewed commit**

The spec requires the checkpoint to be PR A's first independently reviewed commit. Squash
Tasks 1 through 8 into a single commit on top of the spec/plan docs commits:

```bash
git log --oneline  # identify the first checkpoint commit (Task 1)
git reset --soft <commit-before-task-1>
git commit -m "test: land issue #100 predeclaration checkpoint"
```

- [ ] **Step 3: Verify the squash kept everything**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_github_ci_checkpoint.py -v`
Expected: PASS. Run `git status` (clean) and `git log --oneline -3` (spec docs, plan doc,
checkpoint commit).

---

## Self-Review

- Spec coverage: checkpoint items 1 (Task 3), 2 (Task 2), 3 (Task 5), 4 (Task 7), 5 (Task 6),
  6 and 7 (Task 1), 8 (Task 1, `category_d_exceptions.json`), 9 (Tasks 1 and 8). Tier 3A
  shapes (Task 4) feed spec gate 5.
- Deliberately out of scope for this plan (Plan 2, after checkpoint review): recognizer
  modules, launcher policy, replay and differential harnesses, tier gates, complexity gate,
  benchmark run, `docs/research/` benchmark archive, decision record, PR A opening.
- The span helper (Task 6) and label algorithm (Task 3) are authoring procedures, frozen with
  their outputs; the recognizer built in Plan 2 must match them, which is a feature, not
  duplication.
