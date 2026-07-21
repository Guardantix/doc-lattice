# Allowlist recognizer for the direct-invocation audit (issue #100)

Date: 2026-07-19 (revised same day after review rounds 1 and 2)
Status: approved evaluation spec. This document is non-authoritative: it directs the issue #100
evaluation and the two stacked PRs below. Durable user behavior transfers to
[README.md](../../../README.md), durable decisions to [ARCHITECTURE.md](../../../ARCHITECTURE.md),
and release history to [CHANGELOG.md](../../../CHANGELOG.md) when PR B lands. Repository precedent
removes completed specs once their durable decisions are captured.

Issue: <https://github.com/Guardantix/doc-lattice/issues/100>

Baseline for all measurements and accounting in this spec: commit `00737ca`. At that commit
`src/doc_lattice/github_ci/shell_scanner.py` is 2,997 lines and
`tests/test_github_ci_shell_scanner.py` is 2,116 lines. All source references below are against
this baseline.

## Goal

Replace the generic Bash syntax layer of `shell_scanner.py` with a conservative allowlist
recognizer that certifies only a frozen floor grammar, plus audit-contract changes
(PR-reachability pruning, direct-marker gating, and effective-shell composition) that keep the
audit usable without growing that grammar. The allowlist is a proof system, not a compatibility
parser. If it misses its predeclared budgets, the evaluation rejects it and advances the
parser-backed candidate (`mvdan/sh` family first, per the issue thread); it does not grow the
grammar ad hoc.

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
the `run:` body) are searched for the direct marker: an ASCII-case-insensitive `doc[-_.]+lattice`
substring match with no word boundaries, compiled with `re.ASCII | re.IGNORECASE` (Python's
`re.IGNORECASE` alone also matches Unicode dotted and dotless I variants). This overapproximates
paths, `doc-lattice.exe`, requirement strings, and the PEP 503 spelling variants that uv
normalizes to the same distribution (for example `doc_lattice`).

- No marker in either source: the step is "not applicable under the direct-marker contract".
  No grammar check runs and no safety is asserted; the audit still cannot prove that variables,
  aliases, scripts, or constructed words do not invoke the tool.
- A marker anywhere in a source (comments, quoted data, and heredoc text included) requires
  certification of the marker-bearing sources per the D6 composition table.
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
(`render.py:64`) and the documented invocation shapes, and nothing else. The lexical productions
below are the frozen definition; the 78 labels derive mechanically from them.

**Source preconditions.** The source must be at most 1,048,576 characters (inherited from
`shell_scanner.py:10`). Any carriage return, or any C0 control character other than newline and
tab, makes the source uninspectable. Non-ASCII code points are ordinary literal word characters;
every metacharacter and marker in this grammar is ASCII.

**Tokens and operators.** Statements are separated by unquoted newlines or `;`. Within a
statement, commands are joined by `&&` or `||`. A newline after `&&` or `||` (optionally preceded
by whitespace or a comment) does not end the statement; the list continues across blank and
comment lines to the next command, matching Bash's linebreak rule, and a source that ends with the
operator still pending is uninspectable at the operator. Any unquoted occurrence of the following
makes the block uninspectable at that offset: `|` (other than in `||`), `&` (other than in `&&`),
`<`, `>`, `(`, `)`, backtick, backslash (including line continuations), any unquoted brace outside
a permitted `${NAME}` form, `#` inside a word (see comments), and `$` not forming a permitted
parameter form. The recognizer never decides whether braces constitute brace expansion; any
live unquoted brace refuses.
Heredoc introducers, redirections, control-flow keywords in command position (`if`, `then`,
`elif`, `else`, `fi`, `while`, `until`, `do`, `done`, `for`, `case`, `esac`, `function`, `!`,
`time`, `coproc`), and function definitions are uninspectable.

**Words.** A word is a maximal run of: unquoted literal characters (excluding the
metacharacters above, whitespace, and quotes); single-quoted strings (no expansions inside, may
not span a newline); double-quoted strings (literal characters plus permitted parameter forms;
may not span a newline; an interior backslash, backtick, or non-permitted `$` sequence is
uninspectable); and permitted parameter forms. Empty quoted words (`""`, `''`) are ordinary
literal words with empty text; they never match a launcher or candidate table.

**Permitted parameter forms.** Exactly `$?`, `$NAME`, and `${NAME}` with `NAME` matching
`[A-Za-z_][A-Za-z0-9_]*`. Every other `$` sequence (positional and special parameters,
parameter operators, arithmetic, command substitution) is uninspectable. In command words,
permitted forms are certifiable only inside double quotes; an unquoted expansion in a command
word is uninspectable because word splitting and globbing can change the argv shape. In
assignment values, permitted forms are certifiable quoted or unquoted (assignments do not
field-split).

**Unstable words.** A word containing an unquoted `*`, `?`, `[`, or `]`, or starting with an
unquoted `~`, is glob- or tilde-unstable, except that a word consisting exactly of `[` or `]`
is literal (bracket-test evidence, `render.py:75`). A word containing a permitted expansion is
expansion-unstable. Unstable words are permitted in non-candidate argument positions. A command
whose first word is unstable or non-literal is uninspectable.

**Comments.** An unquoted `#` at the start of a line, or immediately after unquoted whitespace
or an operator, begins a comment that runs to end of line and is certifiable. A `#` appearing
inside a word does not start a comment and is uninspectable (divergence from Bash word rules is
not risked either way: Bash also treats mid-word `#` as literal, but the floor refuses rather
than reasons).

**Statement forms.**

1. Blank lines and comment lines.
2. Simple commands: a sequence of words. The first word, and every launcher, executable,
   subcommand, and policy-significant option word, must be fully literal. Other argument words
   may be literal, quoted literal, permitted parameter forms inside double quotes, unstable
   words, or concatenations of those.
3. Assignment statements: `NAME=value` (NAME per the identifier rule) as the entire statement,
   where the value is a literal word, quoted literal, or permitted parameter form. An
   assignment prefix followed by further words in the same command (`FOO=bar cmd ...`) is
   uninspectable.
4. Lists: forms 2 and 3 joined by `&&` or `||`. Both sides of every list are scanned
   conservatively; no short-circuit reachability reasoning. A list may continue across newlines
   after its operator, including past blank and comment lines.

**Policy rule for unstable argv.** When candidate resolution (launcher, executable, subcommand,
or option processing) encounters an unstable word at an argv-sensitive position, resolution
terminates while retaining the disposition established so far; later words are never credited.
This mirrors current behavior (`shell_scanner.py:1700`, exercised at
`tests/test_github_ci_shell_scanner.py:681`): `doc-lattice reconcile pc-design "$OPTION"
--dry-run` keeps its mutating verdict because the trailing `--dry-run` is not provably an
effective option. If the unstable word appears before the subcommand is established, the
invocation is unresolvable and the block is uninspectable at that offset.

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
filesystem, malformed-YAML, or model-alignment errors still terminate immediately through the
existing project-error path (`cli/commands/ci.py:62`). Within a successful audit:

- Findings and uninspectability diagnostics aggregate across the whole repository. No
  first-failure stop; output is independent of workflow, job, and step ordering. The same
  aggregation applies to definite non-shell findings elsewhere in the audit.
- Each uninspectable source contributes one contextual diagnostic.
- Exit precedence: exit 2 (`EXIT_TOOL_ERROR`) if any diagnostic exists, else exit 1
  (`EXIT_FINDING`) if any finding exists, else exit 0.

Rendering contract: findings and diagnostics both render to stdout through
`runtime.write_stdout`. Findings render first, in their existing sorted order and existing
format (`{path}: {code}: {message}`). Diagnostics render after all findings, sorted by
(path, job id, step index, source kind, code, offset), in the format
`{path} job {job_id!r} step {step_index} [{source_kind}] {code}: {reason}`. The
`doc-lattice ci audit: ok` line renders only when both lists are empty. Fatal errors keep the
current stderr project-error path.

### D6. Effective-shell composition

Whether a `run:` body may be interpreted as Bash depends on the step's effective shell. Body
shell classes, reusing the frozen recognition sets already in the audit
(`_supports_bash_run_body`, `_BASH_DEFAULT_RUNNERS`, `audit.py:283`):

- **BASH**: an explicitly declared supported bash-family shell, or no declared shell on a
  runner in the bash-default set.
- **NON_BASH**: any other explicitly declared shell (for example `pwsh`, `python`, `cmd`).
- **UNKNOWN**: no declared shell and the runner is not recognized as bash-default.

The shell template is always runner-parsed command-line text: when present it is scanned with
the recognizer regardless of body class, with the `{0}` placeholder replaced by the existing
literal sentinel. A non-BASH body is never scanned with the recognizer; resembling a Bash
simple command certifies nothing.

Composition table (T = marker in template, B = marker in body):

| T | B | Body class | Behavior |
|---|---|------------|----------|
| no | no | any | Step not applicable, including under NON_BASH and UNKNOWN shells. |
| no | yes | BASH | Scan body; template not applicable. |
| no | yes | NON_BASH or UNKNOWN | No body scan. One diagnostic, code `UNSUPPORTED_EXECUTION_SEMANTICS`, source kind `run_body`, no offset. |
| yes | no | BASH | Scan template; body not applicable. |
| yes | no | NON_BASH or UNKNOWN | Scan template; template findings retained. One `UNSUPPORTED_EXECUTION_SEMANTICS` diagnostic attributed to `shell_template` (the marker-bearing source executes under semantics the audit cannot inspect). The marker-free body remains not applicable and is never claimed to require inspection. |
| yes | yes | BASH | Scan both sources independently; results aggregate per D4/D5. |
| yes | yes | NON_BASH or UNKNOWN | Scan template; findings retained. Per-source diagnostics: one `UNSUPPORTED_EXECUTION_SEMANTICS` for `shell_template` and one for `run_body`. |

An uncertifiable template additionally contributes its own diagnostic with source kind
`shell_template` and an offset. Template evidence is always retained (monotonic rule); the
current behavior of returning template invocations while discarding the body context
(`audit.py:246`) is replaced by this table.

## Architecture

New modules, all pure, fully typed, no `typing.Any` or `typing.cast`, each mirrored by a test
module:

- `src/doc_lattice/github_ci/reachability.py` (tests: `tests/test_github_ci_reachability.py`):
  the D1 predicate.
- `src/doc_lattice/github_ci/direct_marker_scanner.py`
  (tests: `tests/test_github_ci_direct_marker_scanner.py`): the marker gate and floor-grammar
  recognizer. One public function, `scan_execution_source(source) -> BlockScan`, called once per
  execution source; `audit.py` supplies source kind and context per the D6 table. Two public
  entry points would invite semantic drift between templates and bodies.
- `src/doc_lattice/github_ci/launcher_policy.py`
  (tests: `tests/test_github_ci_launcher_policy.py`): doc-lattice launcher and option policy
  (`doc-lattice`, `uvx`, `uv run`, wrapper forms, root options, subcommand and effective
  `--dry-run` extraction), re-founded on the word IR below and implementing the D3 policy rule
  for unstable argv.

Shared word IR: the tokenizer produces span-carrying words that preserve normalized text,
whether the word is unstable (expansion, glob, or tilde), and source offsets.
`launcher_policy.py` consumes this IR; `direct_marker_scanner.py` imports policy, never the
reverse. Offsets let the scanner report the earliest syntax-or-policy failure. The current
policy layer cannot move intact because it depends on `_ShellWord`, `_ScanBudget`, and
ambiguity state (`shell_scanner.py:1643`); it is adapted, not copied.

Bounds, all explicit, predeclared, and tested:

- source cap: 1,048,576 characters (inherited, `shell_scanner.py:10`);
- invocation cap: 10,000 (inherited, `shell_scanner.py:13`);
- token cap: 262,144; statement cap: 65,536 (new; exceeding either is uninspectable);
- work limit: `min(4,194,304, 4 * source_length + 4,096)` conceptual charges; exceeding it is
  uninspectable. The constants are baseline-inspired, not inherited: the old scanner's
  `_MAX_SHELL_SCAN_STEPS` (4,194,304, `shell_scanner.py:11`) uses different charge units.
  Charges are defined as: the marker pass charges `source_length` once; the tokenizer charges
  one per character examined; and one charge per token emitted, per statement closed, and per
  policy step taken.

Scanning is iterative (no recursion) and linear in source length, enforced by the work counter
(see gate 9).

Model and orchestration: `model.py` gains `AuditDiagnostic` with a fixed diagnostic code, path,
job id, step index, `source_kind` (`shell_template` or `run_body`), reason, and optional offset
(required for scan-level diagnostics, absent for D6 composition diagnostics), deterministically
sortable, plus `AuditResult`. Status and code domains use the `Literal` plus `get_args()` plus
`frozenset` pattern in `constants.py`. `audit.py` keeps orchestration: reachability pruning
before step iteration, D6 composition, repository-wide aggregation. `cli/commands/ci.py`
renders per the D5 rendering contract and derives the exit code.

Documentation ownership: PR A lands only this spec's decision record. Accepted
[ARCHITECTURE.md](../../../ARCHITECTURE.md) and [README.md](../../../README.md) text changes in
PR B only, so authoritative docs never describe behavior while the old scanner remains the
runtime.

Replacement-surface accounting: frozen against baseline `00737ca` (2,997 production lines,
2,116 focused test lines). The working estimate (roughly 1,600 syntax-machinery lines deleted,
policy retained and adapted, 600 to 800 new lines) is explicitly provisional. Syntax and policy
are interleaved (for example syntax helpers continue at `shell_scanner.py:2778` while policy
tables sit near the top of the file), so the decision record must report final symbol-based and
diff-based accounting against the baseline, not line-range arithmetic.

## Predeclaration checkpoint

PR A's first reviewed commit is a predeclaration checkpoint containing every frozen evaluation
input, landing before any recognizer implementation commit. Same-PR prose claiming inputs were
chosen first is not independently auditable; the commit boundary and a content-hash manifest
are. The checkpoint contains:

1. The labels for the first 78 `ACCEPTANCE_CASES` present at the checkpoint (`must certify`,
   `intentional exit 2`, `outside direct-marker contract`) with expected `BlockScan` outcomes,
   derived from D2/D3. Later live-baseline regressions may append cases without changing this
   frozen positional prefix.
2. The frozen replay-inventory manifest (gate 2) with stable IDs, count, and content hash.
3. The Tier 3B fixtures, their provenance manifest, the exact selection query, and each
   fixture's independently assigned expected policy outcome.
4. The grammar-boundary mutation set for the semantic differential (gate 7), each mutation
   carrying a predeclared active insertion site (an offset in live executable syntax, not
   inside a comment, quoted literal, or other context where the construct is inert) and its
   expected outcome.
5. The gate 7 probe inputs: the per-fixture inventory of certified candidate-command spans,
   the probe synthesis rules, the predefined environment values, and the Bash pin (exact
   version plus binary or container digest).
6. The numeric caps and the work limit `min(4,194,304, 4 * source_length + 4,096)` with its
   charge definitions (baseline-inspired constants; see Architecture).
7. The benchmark protocol: fleetyard-VM (Linux Mint 22.3) with no concurrent workload,
   CPython 3.13 and 3.14 via uv, timed scope of one full replay-inventory scan through the
   harness entry point, 3 discarded warm-up runs, 30 measured repetitions per Python version,
   median statistic per version, ceiling 250 ms for each version's median, candidate runs
   interleaved with current-scanner baseline runs and the ratio reported. Exceeding the
   ceiling rejects the candidate. This is a trusted fleetyard-only decision gate recorded in
   the decision record; it is not CI-enforced, and the workstation is never attached as a
   self-hosted runner. Deterministic work and corpus gates remain CI-enforced.
8. Any prelabeled exceptions for replay divergence category (d); absent an entry here, the
   category must be empty.
9. A SHA-256 manifest over items 1 through 8.

Checkpoint files are immutable for the remainder of PR A. If any must change, the evaluation
restarts from a new checkpoint commit and prior results are discarded.

## Evaluation corpora and gates

All gates are pytest-enforced in PR A and run under both supported Python versions (3.13 and
3.14) in CI, except the wall-clock half of gate 9, which is the trusted fleetyard-only
decision gate defined in checkpoint item 7. Runtime audit behavior is unchanged in PR A.

1. **Corpus relabel.** Every one of the first 78 checkpoint-frozen `ACCEPTANCE_CASES`
   (`tests/test_github_ci_shell_scanner.py:28`) gets its checkpoint label as a checked-in
   column with the expected `BlockScan` outcome under this contract.
2. **Frozen replay inventory.** Every input exercised by the existing scanner suite
   (parameterized and constructed inputs included, not only `ACCEPTANCE_CASES`) is extracted
   into the checkpoint manifest. The differential replay runs old and new implementations over
   this manifest plus all tiers, producing one normalized record per case:
   `{id, source hash, old raw result (ordered (subcommand, dry_run) tuples, incomplete-reason
   category or none), old adapter outcome (tuples or config-error), new BlockScan (status,
   ordered tuples, reason code, offset)}`. Comparison happens on the normalized records, never
   on raw exception text. Allowed divergence categories, predeclared: (a) identical normalized
   verdicts; (b) `intentional exit 2` (old certified, new uninspectable); (c) `outside
   direct-marker contract` (old verdict, new not-applicable); (d) old incomplete, new
   certified, which must be empty unless prelabeled in the checkpoint. Any divergence outside
   these categories fails the gate; no post-hoc classification.
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
   most one fixture per source repository; provenance per the checkpoint. Each fixture carries
   an independently assigned expected policy outcome (the old scanner is a baseline, not a
   semantic oracle) and contains one marker-bearing block, so the unit matches audit usability.
   Budgets, predeclared:
   - candidate indeterminate: at most 2 of 20 in total;
   - newly indeterminate relative to the current scanner: at most 2 of 20, with intentional
     exit 2 counting against the budget (failures never leave the denominator);
   - false-safe against the independent expectation: exactly 0;
   - false positives against the independent expectation, reported separately: exactly 0
     (shared old/new false positives would otherwise pass every compatibility gate).
7. **Semantic differential (independent oracle).** Fail-closed-by-construction is a design
   intent, not a verified property: a recognizer bug could return `certified` wrongly, and the
   archived July benchmark neither contains this implementation nor is independently
   reproducible. Therefore a checked-in, candidate-specific differential harness runs three
   layers. Original fixture text, including every Tier 3B public-derived envelope, is never
   executed: PATH isolation is not a sandbox (builtins precede PATH lookup and
   slash-containing commands bypass it), so no whole-fixture execution claim is made at all.
   - **Static layer, whole fixtures, no execution.** Every certifiable fixture must pass
     `bash -n` under the pinned Bash (exact version plus binary or container digest from the
     checkpoint) and must parse cleanly under `shfmt-py==4.0.0` (bundled shfmt 3.13.1,
     matching the archived benchmark pins, dev-group dependency only) with matching command
     and word-boundary structure.
   - **Probe layer, per command.** For each certified candidate-command span predeclared in
     the checkpoint, the harness synthesizes a recorder probe containing exactly that one
     simple command: one probe per `&&`/`||` arm, evaluated independently, because the
     recognizer scans both sides while Bash short-circuits (the corpus's
     "runtime-unreachable command remains conservative" case,
     `tests/test_github_ci_shell_scanner.py:55`, is exactly this divergence). Probes execute
     under the pinned Bash with argv-recording stubs and the checkpoint's predefined
     environment values; only stub-resolved candidate executables ever run. The comparison is
     the literal stable argv prefix through the first unstable word plus the resulting policy
     verdict, never complete expanded argv: unstable words deliberately stay abstract in the
     recognizer while Bash expands them to concrete values.
   - **Boundary mutations.** Every mutation in the checkpoint's set, applied at its
     predeclared active insertion site, must yield `uninspectable` at that site. Sites are
     predeclared precisely because mutations inside comments or quoted contexts can be inert
     rather than uninspectable.
   Any mismatch is a gate failure. The current-scanner replay (gate 2) remains a compatibility
   check, not a semantic oracle; this gate is the semantic one.
8. **Adversarial and bounds tests.** Cap exhaustion (source, work, token, statement,
   invocation), oversized sources, pathological token streams, and malformed tails produce
   deterministic `uninspectable` results within bounds.
9. **Complexity and performance.** The work counter increments per character examined and per
   token, statement, and policy step emitted; a gate asserts
   `work <= min(4,194,304, 4 * input_length + 4,096)` for every input in the replay
   inventory, tiers, and adversarial suite, including
   marker-heavy and worst-case sources (the repository audit is mostly marker-gated and
   therefore not representative). The work-counter assertion is the CI-enforced half of this
   gate; wall-clock timing follows the checkpoint benchmark protocol as a trusted
   fleetyard-only decision gate, and exceeding the predeclared ceiling rejects the candidate.

## Delivery

Two stacked PRs sharing one implementation.

**PR A, evaluation and decision.**

- First commit: the predeclaration checkpoint (see above).
- Adds `reachability.py`, `direct_marker_scanner.py`, and `launcher_policy.py` as
  production-quality but dormant code; runtime audit behavior is unchanged.
- Adds the replay harness, differential harness, adversarial, bounds, and complexity tests, and
  gate automation.
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
- Wires D1 pruning, D2 gating, D6 composition, `AuditResult` aggregation, the D5 rendering
  contract, and exit precedence through `audit.py` and `cli/commands/ci.py`.
- Deletes `shell_scanner.py` and its obsolete tests in the same PR.
- Atomically updates the authoritative docs: one cohesive ARCHITECTURE.md decision (not a set
  of micro-decisions), README user behavior and limitations including both D2 contract
  removals, and changelog and migration notes.
- States explicit rollback criteria: reverting PR B restores the previous runtime; the PR A
  modules go dormant. Two scanners never coexist in production.

**Verification.** Both PRs run the complete handoff verification from
[CLAUDE.md](../../../CLAUDE.md): full pytest (coverage floor 80 percent), `ruff check`,
`ruff format --check`, `ty check src`, `scripts/check_typing_boundaries.py`, and
`scripts/check_version_sync.py`, in addition to the PR A gates. The slugger generator check and
`scripts/bench_sections.py` are not triggered: no adapter, dependency, Unicode, or
generated-data surface for section identity is touched.

**Release.** PR A bumps nothing. After PR B, the next release is the next major, expected
3.0.0 (accepted-input narrowing plus exit-semantics change); the exact version is confirmed
against release history when PR B lands, following every synchronized step in
[RELEASING.md](../../../RELEASING.md). This satisfies the issue #100 release freeze: the
decision record lands before any version bump.

## Issue #100 definition-of-done mapping

- Allowlist prototype against the acceptance corpus, indeterminate rates on managed plus user
  workflows: gates 1 through 6.
- tree-sitter prototype: only if PR A gates fail; the decision record then scopes the
  parser-backed evaluation (`mvdan/sh` family first, per the issue thread).
- Pass/fail for every current case including malformed input and heredocs: gates 1 and 2.
- Differential comparison with Bash and shfmt: gate 7, candidate-specific and checked in. The
  archived July benchmark covers only the external parser candidates and is evidence, not a
  gate input.
- Explicit fail-closed behavior: D2 through D6.
- Performance and bounded parsing: gates 8 and 9 and the bounds in Architecture.
- Python 3.13 and 3.14 verification: all gates run on both versions.
- Lines removed and remaining policy surface: final symbol- and diff-based accounting against
  baseline `00737ca` in the decision record.
- Decision record: PR A. Separately scoped implementation PR with compatibility and rollback
  criteria: PR B.
