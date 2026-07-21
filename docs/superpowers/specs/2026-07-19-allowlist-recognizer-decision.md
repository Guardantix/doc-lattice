# Allowlist recognizer decision record (issue #100)

Date: 2026-07-20 (decision recorded; the evaluation spec is dated 2026-07-19)

Status: decision record for the issue #100 allowlist recognizer evaluation. This document is
non-authoritative. Durable user behavior transfers to [README.md](../../../README.md), durable
decisions to [ARCHITECTURE.md](../../../ARCHITECTURE.md), and release history to
[CHANGELOG.md](../../../CHANGELOG.md) when PR B lands.

Issue: <https://github.com/Guardantix/doc-lattice/issues/100>

Spec: [2026-07-19-allowlist-recognizer-design.md](2026-07-19-allowlist-recognizer-design.md)

Baseline for all accounting in this record: commit `00737ca`.

## 1. Verdict

The D3 floor-grammar candidate is REJECTED by predeclared gate 6: candidate indeterminate 3 of
20 against a budget of 2, and newly indeterminate 3 of 20 against a budget of 2 (fixtures 02,
05, and 14: a command substitution with a pipe, a function definition, and a literal `${{ }}`
template). Per the spec's failure path, only durable corpus, harness, results, and decision
evidence merge; the evaluation implementation stays in the merged harness as the dormant
`reachability.py`, `launcher_policy.py`, and `direct_marker_scanner.py` modules, which land in
the branch's implementation commits. The evaluated implementation state is pinned by commit
`17bcaf9`, the branch's final module-changing commit; every gate result in section 2 was
measured at that state, and post-review repairs amended the dormant modules after that pin
(section 5). The evaluation advances the parser-backed candidate (`mvdan/sh` family first),
scoped in section 6.

## 2. Gate results

Every gate ran under both supported Python versions (3.13 and 3.14). Gates 1 through 5, 7, 8,
and the CI half of 9 are pytest-enforced; the wall-clock half of gate 9 is the trusted
fleetyard-only decision gate. Gates 1 through 5, 7, 8, and 9 pass; gate 6 fails by construction,
and that recorded failure is the verdict above.

| Gate | Budget | Measured result | Outcome |
|------|--------|-----------------|---------|
| 1. Corpus relabel | Every one of the 78 acceptance cases conforms to its checkpoint label and expected `BlockScan`. | 78 of 78 conform: 7 must-certify, 65 intentional-exit-2, 6 outside-direct-marker-contract. | Pass |
| 2. Frozen replay inventory | Zero divergence outside categories (a) identical, (b) intentional exit 2, (c) outside direct-marker contract; category (d) empty absent a prelabeled exception. | 613 records (580 inventory, 13 Tier 3A, 20 Tier 3B): identical 266, intentional-exit-2 321, outside-direct-marker 26, unexplained 0, category (d) 0. | Pass |
| 3. Tier 1, managed workflows | The rendered offline template's PR block certifies with its exact invocations; zero diagnostics. | Certified with invocations `(ci, false)`, `(check, false)`, `(lint, false)`; zero diagnostics. | Pass |
| 4. Tier 2, this repository | `.github/workflows/ci.yml` reports zero diagnostics and zero findings. | The `release` job is pruned under D1; every PR-reachable block is not applicable under D2; zero diagnostics. | Pass |
| 5. Tier 3A, documented conformance | 0 unexpected indeterminates over the documented invocation shapes. | 13 of 13 conform (11 certified, 2 uninspectable); 0 unexpected indeterminates. | Pass |
| 6. Tier 3B, empirical envelopes | Candidate indeterminate at most 2 of 20; newly indeterminate at most 2 of 20; false-safe exactly 0; false positives exactly 0. | Candidate indeterminate 3 of 20 and newly indeterminate 3 of 20 (fixtures 02, 05, 14); false-safe 0; false positives 0. Both indeterminate budgets breach. | Fail (recorded rejection) |
| 7. Semantic differential | Every layer agrees against the independent oracle. | Static layer 35 of 35 (`bash -n` plus shfmt structure), span reproduction 36 of 36, probe execution under pinned Bash 5.2.21(1)-release 36 of 36, boundary mutations 50 of 50. | Pass |
| 8. Adversarial and bounds | Cap exhaustion and malformed tails produce deterministic `uninspectable` within bounds. | 9 adversarial inputs, each deterministic across repeated scans and within the work bound. | Pass |
| 9. Complexity and performance | CI half: `work <= min(4,194,304, 4 * input_length + 4,096)` for every input. Trusted half: each version's median at or under the 250 ms ceiling. | CI half: 622 sources (580 inventory, 13 Tier 3A, 20 Tier 3B, 9 adversarial) all within bound. Trusted half (fleetyard, inventory 580): median 29.384 ms on CPython 3.13.14 and 25.302 ms on 3.14.6 against the 250 ms ceiling; candidate-to-baseline ratios 0.501 and 0.448. | Pass |

Benchmark methodological caveat. The fleetyard timings above ran under ambient
interactive-desktop load (one-minute load average roughly 6 to 8.5 on 8 logical cores, from
steady-state Cinnamon, XRDP, and wezterm overhead, not a competing batch workload). That load
biases the timings slower and noisier, the conservative direction. With the medians landing
roughly 8.5x (3.13) and 9.9x (3.14) under the 250 ms ceiling, it could not flip the verdict, but
the absolute numbers should not be read as idle-machine timings.

## 3. Contract removals

The D2 direct-marker gating replaces the previous notion of "direct invocation" with a
"direct-marker contract". Two named contract removals follow. Both are reported separately in
every benchmark result and are never silently reclassified as safe.

1. Marker-free constructed executable names, for example `doc-"lattice" linear` (currently
   accepted, `tests/test_github_ci_shell_scanner.py:31`). This is a deliberate contraction from
   "direct invocation" to the new "direct-marker contract", a compatibility and security
   reduction.
2. Marker-free dynamically selected launcher payloads, for example `uvx "$PKG" ...`. This case
   is already within the issue's declared dynamic and indirect exclusion; the gate makes it
   explicit.

## 4. Predeclaration integrity

The predeclaration checkpoint landed as commit `7d1d2b2` (`test: land issue #100 predeclaration
checkpoint`), PR A's first reviewed commit and the boundary that makes "inputs chosen first"
independently auditable. The checkpoint carries two owner-adjudicated amendments: the
insert-check substitution for the gate 7 boundary mutations, and the replay-inventory recapture
that grew the frozen inventory from 550 to 580 entries.

No checkpoint file changed after the freeze. `test_manifest_matches_artifacts`
([tests/test_github_ci_checkpoint.py](../../../tests/test_github_ci_checkpoint.py)) runs
`scripts/checkpoint_manifest.py --check` against the SHA-256
[MANIFEST.sha256](../../../tests/fixtures/github_ci_checkpoint/MANIFEST.sha256), which is the
auditable proof of that immutability.

## 5. Replacement-surface accounting

Symbol- and diff-based accounting against baseline `00737ca`, not line-range arithmetic. The
three new dormant modules:

| Module | Lines | Public symbols |
|--------|-------|----------------|
| [reachability.py](../../../src/doc_lattice/github_ci/reachability.py) | 98 | 1: `job_is_pr_reachable` |
| [launcher_policy.py](../../../src/doc_lattice/github_ci/launcher_policy.py) | 418 | 3: `ScanWord`, `CandidateResolution`, `resolve_command` |
| [direct_marker_scanner.py](../../../src/doc_lattice/github_ci/direct_marker_scanner.py) | 569 | 2: `scan_execution_source`, `certified_command_words` |

Total: 6 public symbols across 1,085 module lines.

`git diff --stat 00737ca...17bcaf9 -- src/doc_lattice` reports 5 files changed and 1,181
insertions, 0 deletions:

```
 src/doc_lattice/constants.py                       |  24 +
 src/doc_lattice/github_ci/direct_marker_scanner.py | 569 +++++++++++++++++++++
 src/doc_lattice/github_ci/launcher_policy.py       | 418 +++++++++++++++
 src/doc_lattice/github_ci/model.py                 |  72 +++
 src/doc_lattice/github_ci/reachability.py          |  98 ++++
 5 files changed, 1181 insertions(+)
```

`model.py` gains `AuditDiagnostic` and `AuditResult` plus the `BlockScan` result type; `constants.py`
gains the status, reason-category, and diagnostic-code domains under the `Literal` plus
`get_args()` plus `frozenset` pattern. No production line of
`src/doc_lattice/github_ci/shell_scanner.py` changed in PR A; it is absent from the diff and
remains the runtime scanner until PR B.

Known limitation, dormant module. The work-counter helper `_advance_to` in
[direct_marker_scanner.py](../../../src/doc_lattice/github_ci/direct_marker_scanner.py) charges a
multi-character span in one call to `_WorkCounter.charge` but does not inspect that call's
within-budget return value, so a jump that crosses the work limit is not detected at the crossing
offset, only at the next `over()` check. The total charge is still accrued, so the meter cannot
undercount; the gap is confined to the offset at which an overrun would be reported. It is real
but unobserved on the corpus: every corpus source finished with an observed work margin of at
least 4,114 units below the limit, so no source approached the crossing. Recorded here as a
known, bounded limitation of the dormant module so it is not rediscovered from scratch.

Post-review defect repairs. The PR A code review (PR #101) found four defects in the evaluated
dormant modules, each repaired on the branch after the `17bcaf9` pin: a `doc-lattice.exe`
executable head resolved to no candidate and silently dropped its invocation (`935e577`); a
source ending exactly at a dangling `&&` or `||` certified despite Bash rejecting it
(`14a5048`); empty semicolon statements such as `; cmd` and `cmd;;` certified outside the D3
grammar (`6bdf5e3`); and a command-level grammar or policy refusal was reported at a later
lexical offset, against D4's earliest-failure rule (`d143ba7`). The review also found that the
gate 7 harness verified only the pinned Bash version string; `e3e1f27` strengthens it to verify
the binary's recorded SHA-256 as well. The frozen corpus exercises none of the repaired paths,
so every recorded gate result and the verdict are unchanged; the dormant modules at the branch
head differ from the pinned evaluated state by exactly the repair commits recorded in this
section.

A second PR #101 review round found one further dormant-module defect: Bash's `select` and `in`
reserved words were missing from both the spec's D3 keyword enumeration and the implementation's
command-position refusal set, so `select doc-lattice` and `in doc-lattice` certified although
`bash -n` rejects both sources. Commit `80b45dc` adds the two keywords and joins the repair
commits above. The same round hardened the evaluation and packaging surfaces without touching
the dormant modules: the gate 8 statement storm now crosses the statement cap instead of
stopping at the first empty-statement refusal (`c624508`), every gate 8 adversarial case pins
its expected status and reason category so a regressed cap cannot certify silently (`cb8bf2c`),
and the repository-only evaluation suites are excluded from the sdist so the bundled test suite
no longer requires evaluation-host artifacts (`77fa164`). The frozen corpus exercises neither
reserved word in command position, so every recorded gate result and the verdict remain
unchanged.

A third PR #101 review round found three further dormant-module defects. The D1 reachability
splitter treated a `&&` nested inside a parenthesized group as top-level, so
`${{ !(true && github.event_name == 'push' && true) }}` pruned a job that does run on pull
requests; an unquoted parenthesis is now a structural failure and such a condition stays unknown
(`2c7e071`). An empty command closed by a second list operator anchored its refusal at that
second operator instead of the earlier pending operator that already had no right-hand command,
against D4's earliest-offset rule (`a2aa7fe`). A mid-word lexical refusal discarded the partially
read word, so a command-level failure inside it could not anchor earlier, and
`doc-lattice check $X#foo` reported the `#` at offset 20 instead of the unquoted expansion at
offset 18; the partial word is now retained for the earliest-refusal choice (`0cfc554`). The
first repair affects only the never-wired D1 predicate, and the frozen corpus contains no job
`if:` condition, so no corpus outcome depends on it. The two scanner repairs move only the anchor
and category of refusals on sources that were already uninspectable (for example the extglob
replay entry `doc-lattice !(reconcile) --all`, whose anchor moves from the `(` to the
unresolvable `!` word before it); no source's status or invocation evidence changes, so every
recorded gate result and the verdict remain unchanged.

A fourth PR #101 review round found one defect, in the evaluation harness rather than the
dormant modules: the harness's D6 composition judged template marker presence on the
sentinel-substituted text, and the `{0}` scan sentinel `__doc_lattice_script__` itself matches
the direct-marker regex, so every marker-free explicit shell template carrying `{0}` was treated
as marker-bearing, and a `shell: python {0}` step with a marker-free body received an
`UNSUPPORTED_EXECUTION_SEMANTICS` diagnostic against the D6 table's no-marker row. Marker
presence is now judged on the author's template text, with the sentinel substituted only into
the scan input (`0f9f216`); the substitution can only fabricate a marker, never remove one,
because no marker contains the substring `{0}`. The scan input itself is unchanged, the dormant
modules are untouched, and neither workflow evaluated through the harness declares a shell
template, so every recorded gate result and the verdict remain unchanged.

A fifth PR #101 review round found two further dormant-module defects. The launcher policy shared
one value-option set across every uv launcher form, so `uv run --from doc-lattice check` skipped
`--from` and its value as a recognized option and certified the marker-bearing source with no
invocation, although `uv run` does not accept `--from` (only the package-form launchers `uvx` and
`uv tool run` do) and the old scanner reports an unresolved uv launcher option there; the value
options now split by launcher form, and a `--from` under `uv run` refuses before the payload
(`2ce0baa`). The scanner ended a statement at every newline, so a list split after `&&` or `||`,
which `bash -n` accepts and the old scanner resolves through, refused at the pending operator
instead of reading the right-hand command from the following lines; a newline after a pending
operator now continues the list past blank and comment lines, `run`'s dangling check remains the
single refusal for a right-hand command that never arrives, and the spec's D3 grammar text records
the continuation rule (`04bc8b7`). The frozen corpus contains no `uv run --from` form (its
`--from` occurrences are uvx package-form launches) and no list split across a newline, so every
recorded gate result and the verdict remain unchanged.

A sixth PR #101 review round found one further dormant-module defect. The launcher policy treated
a stable option-like word in the `uv tool` run-selector position as a non-candidate, so
`uv tool -q run doc-lattice linear` certified the marker-bearing source with no invocation,
although uv dispatches through options there (`uv tool -q run` reaches the `uv tool run` help) and
contract point 3 refuses any option-like word before the payload is established. The old scanner
shares the drop (`shell_scanner.py:2343` returns no payload for a non-`run` literal selector), so
this is not a parity divergence but a false-safe hole in both; the floor now refuses at the
selector, matching its handling of a uv global option before `run` or `tool` (`31440be`). The
frozen corpus and replay inventory contain no `uv tool` form with a stable option before `run`
(the only non-`run` words in that position are an unstable expansion and a brace expansion, both
already refused), so every recorded gate result and the verdict remain unchanged.

A seventh PR #101 review round, from an automated reviewer, found one further dormant-module
defect. The launcher policy shared its one recognized flag across every uv launcher form, so
`uvx --no-sync doc-lattice linear` and `uv tool run --no-sync doc-lattice linear` skipped
`--no-sync` and certified the marker-bearing source, although only `uv run` accepts `--no-sync`:
uv rejects it for both package-form launchers with an unexpected-argument error before any
dispatch, so the certified invocation never runs. The old scanner shares the false accept
(`shell_scanner.py:214` carries `--no-sync` in the base launcher flags that feed
`_UV_TOOL_RUN_FLAGS` at `shell_scanner.py:222`), so this is a false-safe hole in both rather than
a parity divergence; the flag options now split by launcher form like the fifth round's value
options, and a package-form `--no-sync` refuses before the payload under contract point 3
(`401e1e1`). The frozen corpus and replay inventory carry `--no-sync` only in `uv run` form, so
every recorded gate result and the verdict remain unchanged.

## 6. Parser-backed candidate scoping

Per the issue thread and its review comments, the successor is a doc-lattice-owned static helper
built directly on an exactly pinned `mvdan.cc/sh/v3` syntax API (`LangBash`, source supplied on
stdin, with a doc-lattice-owned IR as the only production protocol). The reflection-derived typed
JSON is acceptable for the differential oracles only, never as the production protocol.

The successor evaluation requires its own spec revision and a fresh predeclaration checkpoint.
The Tier 3B corpus and this harness are reusable, but every expected outcome must be re-derived
under the successor grammar before any successor code runs.

## 7. Evidence

- Archived July 2026 bash-parser benchmark:
  [docs/research/2026-07-bash-parser-benchmark/](../../research/2026-07-bash-parser-benchmark/),
  with per-file SHA-256 hashes and provenance in
  [PROVENANCE.md](../../research/2026-07-bash-parser-benchmark/PROVENANCE.md), labeled "internally
  consistent, not independently reproducible". Audited totals (correct / false-safe /
  indeterminate / false-positive over 234 executions): shfmt 3.13.1 = 69/1/8/0, tree-sitter-bash
  0.25.1 = 58/5/14/1, bashlex 0.18 = 40/12/25/1. These artifacts are evidence for this record and
  are never gate inputs.
- Fleetyard benchmark JSON:
  [py313.json](../../research/recognizer-benchmark/py313.json) and
  [py314.json](../../research/recognizer-benchmark/py314.json), the trusted-half medians, ratios,
  and ceiling recorded in the gate 9 row above.
- Frozen predeclaration checkpoint:
  [tests/fixtures/github_ci_checkpoint/](../../../tests/fixtures/github_ci_checkpoint/), with its
  SHA-256 [MANIFEST.sha256](../../../tests/fixtures/github_ci_checkpoint/MANIFEST.sha256).
- Gate automation:
  [test_github_ci_evaluation_gates.py](../../../tests/test_github_ci_evaluation_gates.py) and
  [test_github_ci_semantic_differential.py](../../../tests/test_github_ci_semantic_differential.py).

## 8. Release freeze

PR A bumps nothing; it lands this record and the archived evidence and changes no version
surface. The next release stays frozen until the successor evaluation lands a decision and PR B
ships the integration, per the spec's Delivery and Release sections.
