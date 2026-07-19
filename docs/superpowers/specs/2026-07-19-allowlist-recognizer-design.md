# Allowlist recognizer for the direct-invocation audit (issue #100)

Date: 2026-07-19
Status: approved evaluation spec. This document is non-authoritative: it directs the issue #100
evaluation and the two stacked PRs below. Durable user behavior transfers to
[README.md](../../../README.md), durable decisions to [ARCHITECTURE.md](../../../ARCHITECTURE.md),
and release history to [CHANGELOG.md](../../../CHANGELOG.md) when PR B lands. Repository precedent
removes completed specs once their durable decisions are captured.

Issue: <https://github.com/Guardantix/doc-lattice/issues/100>

## Goal

Replace the generic Bash syntax layer of `src/doc_lattice/github_ci/shell_scanner.py` (2,905
lines) with a conservative allowlist recognizer that certifies only a frozen floor grammar,
plus two audit-contract changes (PR-reachability pruning and direct-marker gating) that keep
the audit usable without growing that grammar. The allowlist is a proof system, not a
compatibility parser. If it misses its predeclared budgets, the evaluation rejects it and
advances the parser-backed candidate (`mvdan/sh` family first, per the issue thread); it does
not grow the grammar ad hoc.

## Contract decisions

### D1. PR-reachability pruning

A new pure predicate evaluates each job-level `if:` condition against every triggered event in
`PR_EVENTS` (intersected with the document's triggers), using three-valued logic (true, false,
unknown). A job is pruned from the PR scan only when its condition is provably false for all of
those events.

- Recognized syntax: an optional `${{ ... }}` wrapper around a conjunction (`&&`) of static
  equality atoms. Initially the only recognized atom is `github.event_name == '<literal>'`
  (either operand order). Only single-quoted expression literals are recognized; GitHub's
  expression syntax rejects double-quoted string literals, while YAML-level quoting of the whole
  scalar is handled by the YAML parser before this predicate sees the text.
- Literal comparison is ASCII-case-insensitive, matching GitHub's documented string-comparison
  behavior.
- Atoms other than the recognized form (inequality, negation, function calls, dynamic values,
  nesting) evaluate to unknown. One conclusively false conjunct proves the conjunction false;
  unknown conjuncts can never make an `&&` expression true.
- Structural failures are not atom-level unknowns: if the condition does not parse as the
  recognized shape (for example it contains `||`, unbalanced quoting, or anything outside a
  top-level `&&` conjunction), the whole condition is unknown and the job stays scanned.
- Scope limits for #100: job-level `if:` only. No step-level, `needs`, branch, or ref pruning.

`workflow_parser.py` already records job conditions (`model.py:139`, `workflow_parser.py:306`);
the audit simply ignores them today (`audit.py:212`).

### D2. Direct-marker gating

Before grammar certification, both execution sources of a step (the effective shell template and
the `run:` body; see `audit.py:265` for why templates can carry invocations) are searched for the
direct marker: an ASCII-case-insensitive `doc[-_.]+lattice` substring match with no word
boundaries. This overapproximates paths, `doc-lattice.exe`, requirement strings, and the PEP 503
spelling variants that uv normalizes to the same distribution (for example `doc_lattice`).

- No marker in either source: the step is "not applicable under the direct-marker contract".
  No grammar check runs and no safety is asserted; the audit still cannot prove that variables,
  aliases, scripts, or constructed words do not invoke the tool.
- A marker anywhere in a source (comments, quoted data, and heredoc text included) requires
  whole-block certification of that source.
- Unsupported shell semantics: if neither source carries a marker, the step is not applicable;
  if either does, the unsupported shell becomes an aggregated diagnostic rather than an
  immediate error.
- No standalone `uv` or `uvx` markers: they would penalize unrelated Python CI, and marker-free
  dynamically selected launcher payloads are excluded below.

Two named contract removals, both documented in the decision record and reported separately in
every benchmark result (never silently reclassified as safe):

1. Marker-free constructed executable names, for example `doc-"lattice" linear` (currently
   accepted, `tests/test_github_ci_shell_scanner.py:31`). This is a deliberate contraction from
   "direct invocation" to the new "direct-marker contract", a compatibility and security
   reduction.
2. Marker-free dynamically selected launcher payloads, for example `uvx "$PKG" ...`. This case
   is already within the issue's declared dynamic/indirect exclusion; the gate makes it
   explicit.

### D3. Frozen floor grammar

The grammar is frozen by this spec, before the 78-case corpus is labeled and before any
recognizer code runs. It certifies exactly the floor evidenced by the generated PR workflow
(`render.py:64`) and the documented invocation shapes, and nothing else.

Certifiable statement forms:

1. Blank lines and comment lines.
2. Simple commands: a sequence of words. The first word, and every launcher, executable,
   subcommand, and policy-significant option word, must be fully literal. Other argument words
   may be literal, quoted literal, a permitted parameter form, or concatenations of those.
3. Assignment statements: `NAME=value` where the value is literal, quoted literal, or a
   permitted parameter form (covers `rc=$?`).
4. Lists: forms 2 and 3 joined by `&&` or `||`, and statements separated by newlines or `;`.
   Both sides of every list are scanned conservatively; no short-circuit reachability reasoning.

Permitted parameter forms are exactly `$NAME`, `${NAME}`, and `$?` (non-executing). Everything
else is unsupported and makes the block uninspectable: parameter operators, arithmetic, command
and process substitution, backticks, pipelines, redirections, heredocs, control flow, function
definitions, subshells, brace groups, backslash escapes and line continuations, and any other
construct not listed above. Malformed syntax is uninspectable by construction.

### D4. Block-level certification with monotonic evidence

The certification unit is one execution source (shell template or `run:` body). The result is a
`BlockScan` value with status `not_applicable`, `certified`, or `uninspectable`, accumulated
invocations, and an optional incomplete reason. Invariants:

- `not_applicable`: no invocations and no reason.
- `certified`: no reason.
- `uninspectable`: reason and source offset required; invocations permitted.

Monotonic-evidence rule: once an invocation is definitely established, later uncertainty must
never erase it. Every proven prohibited invocation becomes its normal audit finding even when
the enclosing block is uninspectable. The recognizer may continue past unsupported syntax only
when a safe command boundary is provably re-established; otherwise it stops while retaining
earlier evidence. Discovery after synchronization loss is never promised. The reported reason is
the earliest unsupported construct by source offset, whether the failure is syntactic or a
policy-layer refusal.

### D5. Aggregation and exit precedence

`audit_repository` returns an `AuditResult(findings, diagnostics)` instead of findings alone
(`audit.py:128`). Aggregation applies after discovery and workflow validation succeed; fatal
filesystem, malformed-YAML, or model-alignment errors still terminate immediately as tool
errors. Within a successful audit:

- Findings and uninspectability diagnostics aggregate across the whole repository. No
  first-failure stop; output is independent of workflow, job, and step ordering. The same
  aggregation applies to definite non-shell findings elsewhere in the audit.
- Each uninspectable source contributes one contextual diagnostic.
- Exit precedence: exit 2 (`EXIT_TOOL_ERROR`) if any diagnostic exists, else exit 1
  (`EXIT_FINDING`) if any finding exists, else exit 0. Findings and diagnostics render
  together, so a user can see both a concrete violation and the uninspectability that
  accompanies it.

## Architecture

New modules, all pure, fully typed, no `typing.Any` or `typing.cast`, each mirrored by a test
module:

- `src/doc_lattice/github_ci/reachability.py` (tests: `tests/test_github_ci_reachability.py`):
  the D1 predicate.
- `src/doc_lattice/github_ci/direct_marker_scanner.py`
  (tests: `tests/test_github_ci_direct_marker_scanner.py`): the marker gate and floor-grammar
  recognizer. One public function, `scan_execution_source(source) -> BlockScan`, called once per
  execution source; `audit.py` supplies source kind and context. Two public entry points would
  invite semantic drift between templates and bodies.
- `src/doc_lattice/github_ci/launcher_policy.py`
  (tests: `tests/test_github_ci_launcher_policy.py`): doc-lattice launcher and option policy
  (`doc-lattice`, `uvx`, `uv run`, wrapper forms, root options, subcommand and effective
  `--dry-run` extraction), re-founded on the word IR below.

Shared word IR: the tokenizer produces span-carrying words that preserve normalized text,
whether a permitted expansion occurred, and source offsets. `launcher_policy.py` consumes this
IR; `direct_marker_scanner.py` imports policy, never the reverse. Offsets let the scanner report
the earliest syntax-or-policy failure. The current policy layer cannot move intact because it
depends on `_ShellWord`, `_ScanBudget`, and ambiguity state (`shell_scanner.py:1643`); it is
adapted, not copied.

Bounds, all explicit and tested: the shared source cap is a character cap (value inherited from
`shell_scanner.py:10`), the invocation cap stays at 10,000, and token and statement collection
are bounded. Scanning is iterative (no recursion) and linear in source length, enforced by a
work counter (see gates).

Model and orchestration: `model.py` gains `AuditDiagnostic` with a fixed diagnostic code, path,
job id, step index, `source_kind` (`shell_template` or `run_body`), reason, and offset,
deterministically sortable, plus `AuditResult`. Status and code domains use the `Literal` plus
`get_args()` plus `frozenset` pattern in `constants.py`. `audit.py` keeps orchestration:
reachability pruning before step iteration, both-source marker gating, repository-wide
aggregation. `cli/commands/ci.py` renders findings plus diagnostics and derives the exit code.

Documentation ownership: PR A lands only this spec's decision record. Accepted
[ARCHITECTURE.md](../../../ARCHITECTURE.md) and [README.md](../../../README.md) text changes in
PR B only, so authoritative docs never describe behavior while the old scanner remains the
runtime.

Replacement-surface accounting: the working estimate (roughly 1,600 syntax-machinery lines
deleted, policy retained and adapted, 600 to 800 new lines) is explicitly provisional. Syntax
and policy are interleaved (for example syntax helpers continue at `shell_scanner.py:2778` while
policy tables sit near the top of the file), so the decision record must report final
symbol-based and diff-based accounting, not line-range arithmetic.

## Evaluation corpora and gates

All gates are pytest-enforced in PR A and run under both supported Python versions (3.13 and
3.14) in CI. Runtime audit behavior is unchanged in PR A.

1. **Corpus relabel.** Every one of the 78 `ACCEPTANCE_CASES`
   (`tests/test_github_ci_shell_scanner.py:28`) gets a predeclared label: `must certify`,
   `intentional exit 2`, or `outside direct-marker contract`, as a checked-in column with the
   expected `BlockScan` outcome under this contract. Labels derive mechanically from the frozen
   D3 grammar and D2 gate and are fixed before the recognizer runs.
2. **Frozen replay inventory.** Before the recognizer runs, every input exercised by the
   existing scanner suite (parameterized and constructed inputs included, not only
   `ACCEPTANCE_CASES`) is extracted into a named, checked-in manifest with stable IDs and an
   asserted count and content hash. The differential replay runs old and new implementations
   over this manifest plus all tiers. Allowed divergence categories are predeclared:
   (a) identical verdicts; (b) `intentional exit 2` (old certified, new uninspectable);
   (c) `outside direct-marker contract` (old verdict, new not-applicable); (d) old incomplete,
   new certified, which must be empty or individually justified. Any divergence outside these
   categories fails the gate; no post-hoc explanation.
3. **Tier 1, managed workflows.** The rendered offline template's PR block certifies with its
   exact invocations; zero diagnostics. (The Linear workflow carries no PR trigger and is out of
   the PR scan by existing document-level gating.)
4. **Tier 2, this repository.** The global-workflow audit of `.github/workflows/ci.yml` reports
   zero diagnostics and zero findings. (A complete repository audit may report unrelated
   managed-installation findings; those are out of this gate's scope.) Expected mechanism: the
   `release` job prunes under D1, and the PR-reachable blocks carry no marker under D2.
5. **Tier 3A, documented conformance.** Fixtures for every distinct marker-bearing invocation
   shape documented by the project (direct, `uvx`, `uv run`, dynamic non-policy arguments,
   conditional lists, YAML-level conditions). Budget: 0 unexpected indeterminates. This is a
   conformance suite, not usability evidence.
6. **Tier 3B, empirical shell envelopes.** 20 minimal workflow fixtures derived from public
   workflows using analogous `uvx`, `uv run`, or ordinary console-script invocations, with the
   surrounding shell structure preserved and a doc-lattice command mechanically substituted. At
   most one fixture per source repository; a provenance manifest records source URL, commit,
   retrieval date, selection query, and normalization performed. Each fixture carries an
   independently assigned expected policy outcome (the old scanner is a baseline, not a semantic
   oracle) and contains one marker-bearing block, so the unit matches audit usability. Budgets,
   predeclared:
   - candidate indeterminate: at most 2 of 20 in total;
   - newly indeterminate relative to the current scanner: at most 2 of 20, with intentional
     exit 2 counting against the budget (failures never leave the denominator);
   - false-safe against the independent expectation: exactly 0.
7. **Adversarial and bounds tests.** Cap exhaustion, oversized sources, pathological token
   streams, and malformed tails produce deterministic `uninspectable` results within bounds.
8. **Complexity and performance.** A work counter instruments the scanner and a gate asserts a
   fixed `work <= k * input_length + c` bound over marker-heavy and worst-case sources (the
   repository audit is mostly marker-gated and therefore not representative). Wall-clock timing
   is recorded evidence, not a gate assertion: predeclared machine and runtime, repetition
   count, statistic, and ceiling, reported in the decision record.
   `scripts/bench_sections.py` is not triggered; no section-identity surface is touched.

## Delivery

Two stacked PRs sharing one implementation.

**PR A, evaluation and decision.**

- Adds `reachability.py`, `direct_marker_scanner.py`, and `launcher_policy.py` as
  production-quality but dormant code; runtime audit behavior is unchanged.
- Adds the frozen corpora, relabeled cases, replay manifest, differential fixtures,
  adversarial, bounds, and complexity tests, and gate automation.
- Runs every predeclared gate and lands the final decision record in this directory, plus an
  archived copy of the July 2026 bash-parser benchmark artifacts under `docs/research/` with
  SHA-256 hashes and provenance, labeled "internally consistent, not independently
  reproducible". Archived artifacts are evidence for the record and never gate inputs.
- A closing comment on issue #100 links the decision record and both PRs.
- If gates fail: only durable corpus, harness, results, and decision evidence merge. The failed
  candidate remains reproducible: either the runnable evaluation implementation stays in the
  merged harness or an immutable patch is preserved and referenced by commit SHA in the
  decision record. The decision record then advances the parser-backed candidate.

**PR B, integration, stacked on PR A.**

- Reuses the exact PR A implementation; no rebuild.
- Wires D1 pruning, D2 gating, `AuditResult` aggregation, mixed-result rendering, and D5 exit
  precedence through `audit.py` and `cli/commands/ci.py`.
- Deletes `shell_scanner.py` and its obsolete tests in the same PR.
- Atomically updates the authoritative docs: one cohesive ARCHITECTURE.md decision (not four
  micro-decisions), README user behavior and limitations including both D2 contract removals,
  and changelog and migration notes.
- States explicit rollback criteria: reverting PR B restores the previous runtime; the PR A
  modules go dormant. Two scanners never coexist in production.

**Release.** PR A bumps nothing. After PR B, the next release is the next major, expected
3.0.0 (accepted-input narrowing plus exit-semantics change); the exact version is confirmed
against release history when PR B lands, following every synchronized step in
[RELEASING.md](../../../RELEASING.md). This satisfies the issue #100 release freeze: the
decision record lands before any version bump.

## Issue #100 definition-of-done mapping

- Allowlist prototype against the acceptance corpus, indeterminate rates on managed plus user
  workflows: PR A gates 1 through 6.
- tree-sitter prototype: only if PR A gates fail; the decision record then scopes the
  parser-backed evaluation (`mvdan/sh` family first, per the issue thread).
- Pass/fail for every current case including malformed input and heredocs: gates 1 and 2.
- Differential comparison with Bash and shfmt: covered by the archived July benchmark for
  parser candidates; the allowlist needs no external oracle because uncertifiable input fails
  closed by construction, and the replay manifest pins behavior against the current scanner.
- Explicit fail-closed behavior: D2 through D5.
- Performance and bounded parsing: gate 8 and the bounds in Architecture.
- Python 3.13 and 3.14 verification: all gates run on both versions.
- Lines removed and remaining policy surface: final symbol- and diff-based accounting in the
  decision record.
- Decision record: PR A. Separately scoped implementation PR with compatibility and rollback
  criteria: PR B.
