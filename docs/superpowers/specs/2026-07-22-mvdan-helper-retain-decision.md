# mvdan/sh helper successor decision record (issue #100)

Date: 2026-07-22

Status: decision record for the issue #100 mvdan/sh helper successor evaluation. This document
is non-authoritative history. Durable decisions transfer to
[ARCHITECTURE.md](../../../ARCHITECTURE.md) and release history to
[CHANGELOG.md](../../../CHANGELOG.md).

Issue: <https://github.com/Guardantix/doc-lattice/issues/100>

Spec: `docs/superpowers/specs/2026-07-21-mvdan-helper-evaluation-design.md` on the evaluation
branch (see section 7; the spec never merged to `main` because the candidate is not adopted).

Predecessor record:
[2026-07-19-allowlist-recognizer-decision.md](2026-07-19-allowlist-recognizer-decision.md),
whose section 6 scoped this candidate and whose release freeze this record lifts.

Baseline for all accounting in this record: commit `be4b7b1` (main after PR #103). Evidence
branch: `successor-evaluation`, head `c8e36fe`; helper implementation end state `c00c821`.

## 1. Verdict

The mvdan/sh helper successor candidate is REJECTED, and the RETAIN path of the evaluation
spec (section 9) is selected: retain the current scanner as hardened through PR #103, remove
the rejected D3 recognizer, and lift the release freeze without parser integration.

The deciding evidence is the spec's gate 14 surface accounting, measured before the Python
engine was built: the completed Go helper alone occupies 3,941 handwritten production lines
against the owner-ratified owned-surface tripwire of at most 2,200, a breach of at least
1,741 lines with zero of the planned Python modules written. The same measurement makes the
net-reduction tripwire (at least +1,400 lines against the 3,704-line deletion baseline)
unreachable: deleting both old scanners while adding only the existing Go yields a net of
about -237, and every additional engine line moves it further negative. The spec is explicit
that breaching any tripwire or hard gate selects the retain path, and the tripwire numbers
were owner-ratified at checkpoint review before implementation began.

The evaluation therefore terminated after the helper construction phase (Plan A) and before
the Python engine phase (Plan B, planned but not executed). Gates 1 through 13 were not
reached and are recorded as such in section 4; no further construction could have changed the
gate 14 outcome.

## 2. Gate 14 accounting (formal evidence)

Measurement: physical lines (`wc -l`), the same measure that produced the frozen 3,704-line
deletion baseline (`shell_scanner.py` 3,031 plus `direct_marker_scanner.py` 673). All counts
taken on the evidence branch at `c00c821` (identical at `c8e36fe`, which adds only a
documentation commit).

Owned production surface (frozen definition: full successor-owned files, changed symbols in
shared modules, and all schema, build, and packaging logic):

| Component | Lines |
|-----------|-------|
| `helper/doc-lattice-shell-parser/walk.go` | 1,858 |
| `helper/doc-lattice-shell-parser/emit.go` | 1,289 |
| `helper/doc-lattice-shell-parser/wire.go` | 510 |
| `helper/doc-lattice-shell-parser/guard.go` | 131 |
| `helper/doc-lattice-shell-parser/main.go` | 60 |
| `helper/doc-lattice-shell-parser/parse.go` | 52 |
| `helper/doc-lattice-shell-parser/identity.go` | 41 |
| Handwritten helper Go subtotal | 3,941 |
| `helper/doc-lattice-shell-parser/gen_tables.go` (generator program) | 324 |
| `helper/doc-lattice-shell-parser/gen_limits.go` (generator program) | 165 |
| `scripts/check_helper_digest.py` (build identity logic) | 195 |
| `scripts/build_successor_helper.sh` (build logic) | 43 |
| Owned surface including build logic | 4,668 |
| Changed symbols in shared Python modules | 0 (none built) |

Against the 2,200-line tripwire, the most charitable classification (handwritten Go only,
excluding all build logic) breaches by 1,741 lines. The conclusion is insensitive to every
classification choice above.

Separate reporting (frozen list: tests, fixtures, generated data, go.mod, go.sum, CI
surface): generated Go 135 lines (`limits_gen.go` 17, `tables_gen.go` 118); Go tests 6,158
lines; `go.mod` 5 and `go.sum` 2; checkpoint fixtures under
`tests/fixtures/github_ci_successor_checkpoint/`; checkpoint-authoring tooling 1,739 lines
(`derive_successor_labels.py` 1,002, `normalize_legacy_reasons.py` 415,
`generate_protocol_negatives.py` 288, `successor_checkpoint_manifest.py` 34); CI surface
none (the evaluation workflow was never built).

Net reduction: production size at `be4b7b1` minus the projected integrated PR B tree. PR B
would delete 3,704 lines and add at least the 3,941 Go lines plus the Python engine (eight
modules per spec section 2, unbuilt). Best case is therefore approximately -237 against the
required minimum of +1,400.

Reproduction, from the evidence branch:

```bash
cd helper/doc-lattice-shell-parser
wc -l main.go wire.go parse.go walk.go emit.go guard.go identity.go
wc -l gen_limits.go gen_tables.go limits_gen.go tables_gen.go
wc -l *_test.go | tail -1
```

## 3. Why the breach is structural, not recoverable

The weight sits in `emit.go` and `walk.go`, which implement the spec's S3.3 word-fact
contract: `text` (the exact final argv string, provable under every environment) and `single`
(guaranteed one-field expansion). Proving those facts required hand-written classification of
extglob scanning contexts, ANSI-C quoting, brace expansion analysis, tilde activity,
parameter-expansion cardinality, and quoting composition. The bespoke shell-semantics burden
the evaluation intended to delete moved across languages instead of disappearing.

No in-cycle redesign escapes the box: even halving the walker and emitter (implausible while
keeping provable word facts) leaves the Go surface near the cap before any of the required
Python engine exists, and weakening the word facts so the helper emits less would push
refusals into cases the frozen corpus requires to certify, breaking the Tier 3B budget that
was already ratified at 2-of-20 with zero headroom. Either change is a spec and checkpoint
revision, which under the checkpoint's immutability contract (spec section 8) is a new
evaluation, not a continuation of this one.

## 4. Gate status

| Gate | Status |
|------|--------|
| 1-13 (corpus, replay, tiers, oracles, offsets, supervision, conformance, bounds, performance, wheels) | Not reached; the evaluation terminated at the gate 14 pre-construction breach |
| 14. Surface accounting | FAIL: owned surface >= 3,941 vs <= 2,200; net reduction <= -237 vs >= +1,400 |

Honest reporting note: during helper construction, the Go-side conformance, negative,
boundary, and determinism tests against the frozen protocol fixtures all passed (Go coverage
85.9 percent, race-clean), and the 4 MiB boundary input processed in 0.20 s within 46.5 MiB
peak RSS. Those are engineering verification results from Plan A, not gate results; the
cross-language halves of gates 9 and 10 never ran because the Python decoder was never built.

## 5. Checkpoint integrity disclosure

The predeclaration checkpoint was ratified and frozen at `84c7f4f` after three adversarial
review rounds. During helper construction, the checkpoint received two owner-authorized
post-ratification revisions, both recorded in the checkpoint `README.md` revision log on the
evidence branch: (1) the Task 7 parser-alignment revision reclassifying three
heredoc-continuation acceptance rows from `parser-divergence-guard` to `syntax-error` because
the pinned parser yields no AST for them, and (2) the Task 10 conformance-alignment revision
correcting two empty-event fixture inputs and the canonical refusal span. Under the spec's
section 8 contract each is a new checkpoint revision. This record therefore describes the
evaluation as ending against that revised checkpoint state, not against an untouched
pre-implementation freeze. Neither revision affects the gate 14 arithmetic.

## 6. Retain path execution

Per spec section 9, executed by the pull request that carries this record:

1. **Retain** `shell_scanner.py` as the production scanner, hardened through PR #103. No
   runtime behavior changes in this PR.
2. **D3 recognizer disposition: remove.** The rejected recognizer pair
   (`direct_marker_scanner.py` 673 lines, `launcher_policy.py` 457 lines, both dormant and
   never released; v2.0.0 predates their merge) is deleted rather than relocated as a
   test-only oracle, because (a) it duplicates launcher and subcommand policy that
   `shell_scanner.py` owns, a proven drift hazard (review rounds 5 through 7 and PR #103 all
   patched parity holes in both implementations), and (b) it diverges from the production
   scanner by design, so it cannot act as a plain regression oracle without permanently
   maintaining the evaluation's divergence-classification machinery. Removed with it: the
   mirror test suites, `test_github_ci_evaluation_gates.py`,
   `test_github_ci_semantic_differential.py`, `github_ci_evaluation_harness.py`,
   `scripts/bench_recognizer_replay.py`, and the corresponding sdist-exclusion entries. The
   scanner-versus-policy parity test in `test_github_ci_shell_scanner.py` is converted to
   direct expected-outcome assertions so the issue #102 fixtures stay pinned against the
   production scanner.
3. **Retained deliberately:** `reachability.py` and its tests (candidate-independent,
   contract-ratified D1 logic with no duplicate implementation and no drift hazard), the D4
   and D5 model types (`BlockScan`, `AuditDiagnostic`, `AuditResult`) and their constants
   domains (ratified contracts shared with future audit hardening), and the frozen D3
   checkpoint fixtures with their integrity test (the corpus history; the live scanner's own
   acceptance corpus remains in `test_github_ci_shell_scanner.py`).
4. **Release freeze lifted.** The freeze declared by the predecessor record ends when this
   record merges. Versioning of the accumulated unreleased work follows the normal release
   procedure; this record selects no version.
5. **Evidence preserved, never merged.** The `successor-evaluation` branch (head `c8e36fe`)
   is pushed to origin as research evidence: the spec, the ratified checkpoint with its
   manifest, the complete Go helper with its tests, the executed Plan A document, and the
   written but unexecuted Plan B document. Its production surface must not merge.
6. **Issue #100 closes** with this record's merge, retitled to reflect the completed
   evaluation and its retain outcome.

## 7. Evidence

- Evidence branch: `successor-evaluation` at `c8e36fe` (helper end state `c00c821`,
  checkpoint ratification `84c7f4f`, spec `f3865f8` and amendments).
- Ratified checkpoint and manifest:
  `tests/fixtures/github_ci_successor_checkpoint/MANIFEST.sha256` on the evidence branch.
- Plan documents on the evidence branch: `docs/superpowers/plans/2026-07-21-successor-checkpoint.md`
  (executed), `docs/superpowers/plans/2026-07-22-successor-helper-go.md` (executed),
  `docs/superpowers/plans/2026-07-22-successor-engine.md` (written, not executed).
- Section 2 accounting commands, reproducible on the evidence branch.
- Third-party structural review (2026-07-22) that surfaced the gate 14 pre-construction
  breach; verified independently before this record was written.

## 8. Process notes

- Surface accounting must be a construction-time check in any future evaluation, not an
  evidence-phase gate. Plan A's verification measured the binary-size tripwire but never ran
  the line accounting, and the plan split deferred gate 14 to the final phase; the breach was
  measurable the moment the helper existed.
- The predeclaration discipline otherwise worked as designed: because the tripwire numbers
  were ratified before implementation, the verdict here is arithmetic, not judgment, and
  terminating early spends no further effort on a decided outcome.
