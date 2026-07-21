# mvdan/sh helper successor evaluation design

Date: 2026-07-21. Status: approved design for the issue #100 successor evaluation cycle.
Baseline: `be4b7b1` (main after PR #103). Predecessor decision:
[2026-07-19-allowlist-recognizer-decision.md](2026-07-19-allowlist-recognizer-decision.md),
whose section 6 scopes this candidate. The release freeze from that record holds throughout
this cycle; the package version stays `2.0.0` until the freeze lifts.

## 1. Goal and non-goals

Evaluate replacing the bespoke Bash syntax layer in
`src/doc_lattice/github_ci/shell_scanner.py` with a doc-lattice-owned static Go helper (a
thin syntax certifier) built directly on an exactly pinned `mvdan.cc/sh/v3` syntax API,
behind a doc-lattice-owned wire protocol. The evaluation ships as one PR mirroring PR #101:
a frozen predeclaration checkpoint, a dormant production-quality candidate, every
predeclared gate, and a decision record. Runtime import graph and CLI behavior remain
unchanged; dormant importable modules ship in no release because the release freeze holds.

Non-goals for this PR: wiring `audit.py`, deleting either old scanner, changing the release
workflow or published artifacts, bumping the version, or lifting the freeze. Those belong to
the integration PR (PR B) or to the retain path, whichever the decision record selects.

Product policy stays in Python. The Go helper certifies syntax and emits semantic facts; it
never resolves launchers, subcommands, `--dry-run`, or markers.

## 2. Component inventory and dormancy boundary

New Go module at `helper/doc-lattice-shell-parser/` (repo root, outside `src/`, so Hatch
packaging cannot ship it and the Python import surface is untouched):

- `go.mod` and `go.sum` with the exact `mvdan.cc/sh/v3` pin.
- `main.go`: stdin/stdout protocol shell.
- `internal/certify/`: the context-carrying walker, certification tables, raw-source guard,
  and owned IR emission.
- Go conformance tests consuming the same checked-in fixture files as the Python side. Test
  binaries build into temporary directories; no binary is ever committed.

Built with `CGO_ENABLED=0`. The helper reads stdin, writes one protocol response, and
requires no repository, network, or environment access.

New Python modules under `src/doc_lattice/github_ci/`, all dormant (reached only from the
evaluation harness and tests; `audit.py` never imports them in this PR):

- `helper_protocol_boundary.py`: versioned wire model and strict decoding. The only place
  `typing.Any` touches helper JSON; the `boundary` suffix keeps it recognized by
  `scripts/check_typing_boundaries.py`.
- `helper_supervisor.py`: process lifecycle, batching, deadlines, caps, and batch-atomicity
  enforcement. Returns a typed, bounded `BatchFailure`; it never constructs D5 diagnostics
  because it lacks attribution context.
- `syntax_certifier.py`: translation only. Protocol events to certified command sites,
  offset conversion and projection, and adaptation to launcher policy.
- `shell_composition.py`: the neutral D6 classification and selection contract (promoted
  from the evaluation harness so successor code neither copies harness logic nor imports
  private `audit.py` details).
- `direct_marker.py`: the relocated `DIRECT_MARKER_RE` and D2 gating helpers.
  `direct_marker_scanner.py` behavior is unchanged except for the mechanical import
  relocation of the predicate it currently defines locally.
- `successor_audit.py`: the dormant collect, batch, aggregate pipeline. Owns attribution,
  maps `BatchFailure` into per-source D5 diagnostics, and performs D4 aggregation. PR B
  disposition: folded into `audit.py` (the durable production name).
- `helper_locator.py`: the dormant package-owned helper lookup, resolving the bundled
  binary from package data for the current platform, never `PATH`. It returns a typed
  outcome: a located absolute path, or a typed `HelperUnavailable` (`missing_binary` or
  `unsupported_platform`) that `successor_audit.py` maps into per-source
  `UNINSPECTABLE_SOURCE` D5 diagnostics with `offset=None` and a stable owned reason,
  exactly like a `BatchFailure`. In this PR it is invoked by the installed
  candidate-wheel smoke tests and by the helper-free sdist degradation harness of
  gate 13.

Direct reuse, unchanged: `reachability.py` (D1), `model.py` (`BlockScan`, D4 invariants,
`AuditDiagnostic`, D5 aggregation), and `launcher_policy.py` behavior and tests, with one
explicit interface revision described in section 5. Successor fixtures live in a fresh
checkpoint (section 8); the frozen D3 checkpoint at `tests/fixtures/github_ci_checkpoint/`
is never mutated.

Ordinary evaluation tests inject the temporary helper binary's absolute path because no
package-owned binary exists in the normal build before PR B; `helper_locator.py` is
exercised by the installed candidate-wheel smoke tests and the sdist degradation harness.

## 3. Go helper contract

### 3.1 Parse strategy

The parser is constructed as `syntax.NewParser(syntax.Variant(syntax.LangBash))` at the
pinned version. `RecoverErrors` is prohibited by predeclaration: a recovered tree is the
clean-looking-wrong-tree channel.

Statement acquisition uses `Parser.StmtsSeq` with this exact consumption rule: accept a
yielded statement only when the statement is non-nil and the error is nil. On the first
non-nil error, stop certifying, discard the co-yielded statement, and record the error
once; then drain the iterator to completion without accepting any later statement and
without breaking the range (the pinned implementation can yield `(statement, error)` and
subsequently `(nil, error)` again, and the Go iterator contract panics if `yield` is
called after it has returned false). Duplicate terminal errors are deduplicated, and
exactly one terminal refusal is emitted at the first error position. Partial evidence is
therefore preserved only across completed top-level statement boundaries. The canonical
fixture `doc-lattice check; echo "$(` (site for statement 1, exactly one terminal refusal,
nothing after, iterator fully drained) tests this drain-and-deduplicate behavior and is a
hard pin-upgrade tripwire: any parser pin change must re-verify it before the pin lands.

Upstream error values (`syntax.ParseError`, `syntax.LangError`) are type-switched only to
recover positions. Upstream error text never crosses the wire; every refusal carries an
owned code and stable reason.

### 3.2 Traversal envelope

A custom context-carrying walker, not `syntax.Walk`, because upstream walk order is AST
order rather than source order. The walker collects bounded events and stable-sorts them by
span with an explicit tie-breaker (event kind, then ordinal).

The certified-construct table is context-sensitive: decisions key on
`(node kind, parent field or semantic role)`, not node kind alone, because a `Word` may be
command argv, inert data, an assignment value, a redirect target, or a heredoc body. The
table explicitly covers process substitutions, parameter and arithmetic operands,
assignment values, loop, case, and condition words, here-string words, and
unquoted-delimiter heredoc bodies. Three dispositions:

- traverse: statement-bearing contexts that are real execution sources (lists, pipelines,
  subshells, command substitutions, function bodies, redirect-target expansions,
  unquoted-delimiter heredoc bodies);
- ignore safely: constructs that provably cannot host an execution source;
- refuse: everything else, including every unknown or future node kind via the exhaustive
  type switch's default arm.

Quoting mechanics, pipeline shape, redirect operators, and heredoc internals stay inside
the visitor. They inform certification; they are not exported.

### 3.3 Event IR

An ordered event stream per source with exactly two event kinds.

`command_site`: ordinal id, span, assignment-prefix facts (name, value-known flag), and
ordered argv word facts. Each word fact carries:

- `text`: the exact final single argv string, provable under every environment (locale
  quotes, tilde expansion, parameters, substitutions, globs, and braces classified
  carefully), or null when not provable. There is no serialized `known` field; `text`
  being non-null is the knownness signal.
- `single`: whether the word is guaranteed to expand to exactly one argv entry. The IR
  invariant `text is not null implies single is true` is validated at the boundary.
- `start_byte` and `end_byte`: UTF-8 byte offsets into the scanned source. Every position
  is validated with `Pos.IsValid()` and `0 <= start <= end <= source byte length`; an
  invalid upstream position becomes a stable refusal, never a crash or a guess.

The helper exports syntax-only facts; it never applies the direct-marker pattern. Marker
provenance is computed in Python (section 6.2) from `text` and from the word's authored
raw-source segment, obtained by projecting the word span onto `raw_text`. The product
regex stays single-owned in `direct_marker.py`, avoiding cross-language regex drift.

`refusal`: owned category code, span, stable reason, and scope class. The frozen
reason-code-to-scope table (checkpoint artifact, shared by Go and Python) assigns each code
one scope:

- terminal: parse errors, raw-source guard hits, source, work, depth, and statement cap
  breaches, and unknown node kinds. A terminal refusal is the last event for its source.
- subtree-local: only predeclared codes whose complete node span is known, whose subtree
  emits no command sites, and whose next sibling is a certified resynchronization point.
  Local refusal spans must not overlap subsequently accepted sites.
- command-local: policy-layer refusals attached after a complete command site is certified
  (assigned in Python; the table still owns the codes).

New codes (at minimum: syntax error, unsupported construct, parser-divergence guard,
dispatcher payload, marker-head look-alike, assignment prefix, unstable first word,
splitting-unsafe word) enter the `ScanReasonCategory` domain in `constants.py` through the
usual `Literal` plus `get_args()` plus `frozenset` pattern, with their D4 mapping frozen in
the checkpoint.

`work_units`: each per-source result reports bounded certifier work units (definitions
frozen in the checkpoint) so `BlockScan.work_charged` stays honest; Python-side policy
charges are added under definitions frozen alongside. Zero-filling the field is prohibited.

### 3.4 Raw-source guard

An AST-anchored guard inside the Go certification layer, compensating for Bash-versus-
parser semantic divergence (syntax certification, not product policy). After a statement
parses, confirmed unquoted-heredoc spans are used to inspect the original bytes for
backslash-newline continuations that change tokenization. A global textual rule is
rejected: broad enough to find heredoc context, it would recreate a shell lexer and
over-refuse comments and quoted data. Guard hits are terminal refusals.

The guard may be developed against declared differential fixtures but is frozen before the
verdict run; any later change invalidates and reruns all affected evidence. The permanent
regression is the heredoc backslash-newline false-safe recorded in
`docs/research/2026-07-bash-parser-benchmark/bash_parser_primary_source_review.md`
(clean-empty certification of that fixture is an automatic evaluation failure).

### 3.5 Bounds, stated in enforceable layers

- Pre-parse per-source byte cap in Go, plus the aggregate request cap checked while
  reading stdin (section 4.4 numbers).
- The inherited Python-side cap is 1,048,576 characters (`shell_scanner.py` measures
  characters, not bytes). The helper's per-source byte cap is 4,194,304 UTF-8 bytes (four
  bytes per character worst case), an explicit compatibility-preserving widening at the
  byte layer while the character cap continues to govern in Python.
- Statement cap while consuming `StmtsSeq`; visitor node, depth, and event caps.
- Parser CPU, stack, and memory containment come from process isolation plus the parent
  deadline, not from visitor caps: the parser exposes no depth or work-budget option, so
  visitor caps cannot honestly be described as bounding construction of one deeply nested
  AST. Memory is governed by bounded input plus a measured peak-RSS ceiling (gate 11); no
  hard OS-level memory-isolation claim is made.

Determinism: no goroutines, no clock, no environment reads; identical input bytes produce
byte-identical output, enforced by a Go conformance test.

## 4. Wire protocol and batch supervision

### 4.1 Framing

One helper process per audit run, one request/response exchange. Request: a single UTF-8
JSON document on stdin with `protocol_version` (integer, exact match) and `sources`, an
array of `{id, source}` with opaque contiguous ids 0..n-1. Python assigns ids after an
explicit sort by the D5 attribution prefix `(path, job_id, step_index, source_kind)` with
the source-kind order frozen in the checkpoint (template before body). The helper never
receives paths, job names, or any repository context; Python keeps the id-to-attribution
map private.

Response: a single JSON document on stdout with `protocol_version`, `helper_version`,
`parser_version`, and `results`, an array of `{id, events, work_units}` in ascending id
order.

### 4.2 Symmetric byte-level strictness

The Go request decoder rejects: unknown or duplicate fields, invalid UTF-8 and lone
surrogates, trailing documents, wrong exact types, unordered or non-contiguous ids, empty
batches, and excessive nesting or element counts. The Python response decoder decodes
strict UTF-8, rejects duplicate keys via `object_pairs_hook`, rejects `NaN` and
`Infinity`, and distinguishes `bool` from `int`. Both directions are covered by raw
negative fixtures checked in beside the JSON Schema, because Go `encoding/json` permits
duplicate names and replaces invalid UTF-8, and Python's default decoder accepts duplicate
names and non-finite numbers; a schema alone cannot catch those cases.

Python-side validation additionally rejects: version mismatch, ids not exactly the request
set, out-of-order results, events out of span order, spans failing the section 3.3 range
rule, and trailing non-whitespace after the document. A valid-looking prefix is never
accepted.

The request encoder is canonical: compact JSON with `ensure_ascii=False`,
`allow_nan=False`, no BOM, and strict surrogate rejection before encoding. All byte caps
are measured over the resulting UTF-8 bytes; this is what makes the per-source and
aggregate caps compose (Python's default ASCII escaping can expand an astral character to
twelve JSON bytes, breaking the four-bytes-per-character worst case). A
max-length four-byte-character source fixture sits beside the raw negative fixtures to
pin the composition.

### 4.3 Identity checks

`parser_version` is derived inside the helper from `runtime/debug.ReadBuildInfo`, not from
an independently maintained constant, and must exactly match the checkpoint pin
(`parser_pin_mismatch` on failure). `helper_version` must exactly match the expected
internal build identity (`helper_identity_mismatch` on failure). That identity is
mechanically enforced, not asserted: it is a build-embedded semantic digest computed over
a frozen digest-input manifest (a checkpoint artifact) that enumerates every
behavior-affecting, non-generated helper source and contract input: the owned protocol
schema, the certification tables, the visitor and IR-encoding sources, the protocol and
framing code including `main.go`, and the compiled limits. The manifest fixes a
deterministic file ordering and lists its exclusions (generated files, tests, build
metadata) explicitly. The Python side expects the same generated value, and CI recomputes
the digest from the manifest so a forgotten manual bump or an unlisted
behavior-affecting file cannot defeat the stale-binary check. It is decoupled from the
package semver. Reported versions are compatibility tripwires, not
integrity proof; the package-owned path (PR B) and recorded artifact hashes remain the
integrity controls.

### 4.4 Supervisor state machine

`helper_supervisor.py` enforces, in order: aggregate request cap before spawning (the
helper independently enforces it while reading stdin); spawn with absolute helper path
(injected in ordinary evaluation tests, resolved by `helper_locator.py` in installed
candidate-wheel smoke tests and in PR B, never `PATH`), a dedicated neutral working
directory, an explicit frozen minimal per-platform environment, `close_fds=True`, and only
the three standard handles; concurrent stdin writing and stdout/stderr draining with byte
caps (never `communicate()`, which buffers and whose timeout does not kill the child); a
monotonic deadline; on breach or cap overflow: kill, bounded drain, mandatory reap.
Results are published only after EOF, exit code 0, and complete validation; otherwise the
entire response is discarded. Exit 0 is required even for all-refused batches; nonzero
always means batch failure.

Numeric caps (checkpoint `limits.json`): aggregate request cap 8,388,608 bytes; stdout
cap 16,777,216 bytes; stderr capture cap 65,536 bytes. The deadline formula: 2,000 ms
base, plus 25 ms per source, plus 1 ms per 4 KiB of aggregate request bytes, ceiling
30,000 ms.

`BatchFailure` taxonomy: `spawn_failure`, `timeout`, `exit_nonzero`,
`output_cap_exceeded`, `malformed_output`, `protocol_violation`, `parser_pin_mismatch`,
`helper_identity_mismatch`, `request_cap_exceeded`, `transport_failure` (broken pipes and
pipe, wait, or kill failures). Bounded stderr is retained as private debugging evidence
only; raw helper stderr never enters `AuditDiagnostic.reason`.

Process containment guarantee, exact: the helper is prohibited from creating child
processes (verified in review and exercised adversarially), and the supervisor forcibly
terminates and reaps the direct helper on every supported platform. POSIX process-group
killing is defense in depth, not a claimed guarantee. No whole-process-tree containment is
claimed on Windows (that would require a Job Object, which this design does not take on).

The security property, phrased precisely: no attribution on the wire and no ambient
repository context; this is not an OS sandbox.

### 4.5 Batch atomicity versus per-source refusal

A source whose events include refusals is an ordinary per-source outcome; siblings are
unaffected and D4 evidence from completed statements survives. The batch fails as a unit
only on the `BatchFailure` taxonomy above. `successor_audit.py` maps a batch failure to
one `UNINSPECTABLE_SOURCE` diagnostic per batched source with full attribution,
`offset=None`, and a stable owned reason: audit exit 2, no old-scanner fallback (the
standing predeclaration; an error-triggered fallback cannot catch clean wrong parses).

## 5. Python successor pipeline

### 5.1 Collect: D1, marker facts, D6

Order per PR-reachable step (D1 via `reachability.py`, unchanged): compute raw
template-and-body marker facts on authored text; apply the D6 table; only then construct
substituted scan inputs. `shell_composition.py` returns a typed outcome containing
candidate scan sources, immediate `UNSUPPORTED_EXECUTION_SEMANTICS` diagnostics, and
not-applicable dispositions. Consequences preserved from the ratified harness behavior: a
marker-bearing non-Bash body is omitted from the batch but still diagnosed; a
marker-bearing template under non-Bash semantics is both batched and diagnosed; zero
candidates can still produce exit-2 diagnostics without spawning a helper. Marker-free
sources are not applicable under the direct-marker contract, never "certified safe."

### 5.2 Certify and adapt

`syntax_certifier.py` adapts and validates the entire batch before any aggregation, so an
invalid span in source N invalidates all results atomically (`protocol_violation`).

Offsets: each collected source carries `raw_text`, `scan_text`, and an offset projection.
Helper byte offsets are converted once to scan-text character indices (off-boundary spans
are a `protocol_violation`), then refusal and policy offsets are projected back to
raw-text indices for D4 attribution. An offset inside a synthetic sentinel maps to the
corresponding `{0}` start; run bodies use the identity projection. D4's coordinate system
therefore remains authored-text character indices, matching every existing Python test.

Word adaptation: word facts become the revised `ScanWord` with `text: str | None` plus
`single`, an explicit interface revision to `launcher_policy.py`. Known words resolve
exactly as today; `text=None` replaces the old dynamic-token handling inside launcher
policy; empty strings are never fabricated.

Pre-policy command matrix (frozen in the checkpoint; these checks currently live in
`direct_marker_scanner.py` and move into the revised launcher-policy entry path as a named
precheck stage ahead of `resolve_command`, keeping `syntax_certifier.py` translation-only):

- assignments with no argv: no command and no refusal;
- assignments plus argv: `assignment-prefix` refusal at the earliest assignment, while the
  certified argv is still resolved so a definite finding is retained;
- first argv word with `text=None`: `unstable-first-word`, no string-based head logic;
- any word with `single=False`: the appropriate expansion or splitting refusal at that
  word;
- IR invariant enforced: `text is not None` implies `single is True`.

A precheck refusal and a policy resolution can both carry information; a precheck refusal
never automatically erases a resolvable invocation.

### 5.3 Aggregate: D4 and D5

Per source, a pure fold over the ordered event stream: command sites go through
`resolve_command`; resolved invocations accumulate monotonically; refusals (helper-emitted
or policy-emitted) merge into the earliest-offset incomplete reason with this frozen tie
rule: earliest offset, then syntax-class refusals over policy-class refusals (preserving
the current lexical-over-policy behavior), then category code as the final deterministic
tie-breaker.

Scope semantics follow the section 3.3 table: terminal refusals end their source at that
offset; subtree-local and command-local refusals leave later certified statements and
their invocations intact; the source ends incomplete with the earliest refusal and all
retained evidence, per the `BlockScan` invariants. Mixed precedence, stated flatly:
retained findings are always reported, any diagnostic still forces exit 2, and a refusal
never poisons sibling sources, other steps, or the document.

### 5.4 PR B integration shape (named now, executed later)

PR B integrates at repository scope: `audit_repository` invokes collect, one batch,
aggregate once after `_bind_inspected_workflow_snapshot`, merges successor findings and
diagnostics with global and managed results, changes `audit_repository` to return
`AuditResult`, and updates the CLI's findings-only rendering and exit behavior in
`cli/commands/ci.py`. `_pr_step_invocations` is deleted as a consequence of that
repository-level integration, along with both old syntax scanners, subject to the decision
record.

## 6. Predeclared contract stances

### 6.1 Dispatcher rule

Dispatcher recognition is frozen separately from marker detection. Recognized dispatcher
heads (basename after path stripping, `.exe` and ASCII-case handling mirroring the head
rules): `eval`, `source`, `.`, and `bash`, `sh`, `dash`, `zsh` when an exact frozen `-c`
option grammar matches (short-option clusters, value-taking options, `--` terminator).
Unknown or dynamic selector syntax in a marker-bearing command refuses rather than
silently missing `-c`.

The rule is argv-wide, named honestly: if any word of a recognized dispatcher command
carries a marker (per 6.2), the command receives a command-local `dispatcher-payload`
refusal; payloads are never parsed recursively. The deliberate false positive is pinned:
in `bash -c 'echo ok' doc-lattice`, the trailing operand becomes `$0` and is not executed
source, yet the rule still refuses. Marker-free dispatch stays outside the
direct-invocation contract exactly as documented today.

### 6.2 Synthetic-safe marker provenance

A word carries a marker when the pattern matches its statically known final text
(excluding synthetic sentinel content) or its authored raw-source segment (the word span
projected onto `raw_text`, decisive when `text` is null). The check runs in the Python
launcher-policy precheck stage using the single-owned pattern in `direct_marker.py`; the
helper contributes only syntax facts (section 3.3). This catches `eval "doc-lattice $X"`
while preventing the `{0}` sentinel (`__doc_lattice_script__`) from manufacturing a
dispatcher or head-look-alike refusal, because projection maps sentinel-interior spans to
the authored `{0}`.

### 6.3 Head look-alike symmetry

Applied only to known, authored head text: a head word matching the marker pattern that
resolves as neither a doc-lattice head nor a recognized launcher receives a
`marker-head-look-alike` refusal instead of `not_candidate`, closing the floor's
asymmetry in the fail-closed direction. A `text=None` head remains `unstable-first-word`.

Frozen policy precedence: exact doc-lattice and launcher resolution, then the dispatcher
rule, then `marker-head-look-alike`, then the existing off-floor wrapper handling, then
`not_candidate`. Both new reason codes enter `ScanReasonCategory`, the stable-reason
table, and the command-local scope table.

### 6.4 Replay comparison and legacy normalization

The replay gate compares, per entry, the tuple (status, retained invocation tuples,
reason category). Offsets are excluded (coordinate systems legitimately differ) but are
covered by the dedicated offset oracle gate. Because the baseline's `incomplete_reason`
is unstructured and the current harness treats both-incomplete pairs as identical, the
fresh checkpoint records a legacy-reason normalization artifact: each baseline entry's
normalized status, invocations, and owner-adjudicated reason category at baseline
`be4b7b1`. Categories are never inferred dynamically from legacy error-message
substrings. The post-#103 appended acceptance rows are included in re-derivation.

### 6.5 Launcher-policy outcome parity

The successor preserves `launcher_policy.py` semantics (immediate fail-closed refusal of
a dynamic `uv tool` selector) and replays every post-#103 case to the same fail-closed
outcome; it does not reproduce the old scanner's bounded probe mechanics. Parity is
pinned to baseline commit `be4b7b1` and the fixture-manifest digest, not to a moving
phrase.

### 6.6 Public surface and accounting

Every new or revised module declares `__all__`, with a CI check that every listed name
exists, declarations are unique, and intended cross-module exports are present. `__all__`
declares the intended public API; it does not replace replacement-surface accounting,
which counts all owned security-sensitive surface (private symbols and lines, Go
identifiers, wire and schema fields, supervisor code, fixtures, packaging scripts, build
matrix, and focused tests, reported per section 9).

### 6.7 Tracking

Issue #100 remains the tracker, retitled for the mvdan/sh successor evaluation, with a
dated status block prepended (rejected allowlist result, this successor cycle, fresh
checkpoint, evaluation PR link) and the original body preserved as history.

## 7. Packaging and platform contract

Claimed matrix (five targets): Linux x86_64, Linux aarch64, macOS x86_64, macOS arm64,
Windows x86_64. The following degradation behavior is the PR B contract (PR A's actual
CLI still uses the old scanner): on unsupported platforms and from the helper-free sdist,
core `check`, `lint`, and `reconcile` remain pure-Python and fully functional, while
`ci audit` fails closed with a clear contextual diagnostic and exit 2. Installation never
downloads anything.

In this PR, normal Hatch wheel and sdist outputs remain helper-free and the existing
release and publish path is untouched. The evaluation workflow separately builds
ephemeral, platform-tagged candidate wheels using the exact wheel layout proposed for
PR B. Gate 13 covers both halves: the five installed candidate wheels (smoke tests
proving packaging, `helper_locator.py` lookup from the installed wheel, and direct
protocol execution, under Python 3.13 and 3.14 on every claimed target) and a helper-free
sdist degradation harness that invokes `helper_locator.py` (never a merely injected
nonexistent path) and asserts its typed `HelperUnavailable` outcome maps into the D5
diagnostics of the dormant pipeline's fail-closed path. The actual
CLI exit-2 smoke is repeated in PR B when the pipeline is wired. Runner labels, target
triples, wheel tags, and build-container digests are frozen in the checkpoint; actual
hosted runner image versions cannot be frozen in advance (images are continuously
deployed) and are recorded in gate evidence from the job logs. Candidate wheels are
retained as short-lived CI artifacts and never published.

## 8. Checkpoint: immutable inputs, separate evidence

Fresh checkpoint at `tests/fixtures/github_ci_successor_checkpoint/`, frozen as the
branch's first post-design, pre-implementation reviewed commit (this design document
precedes it). Its `MANIFEST.sha256` covers checkpoint inputs only and is never repinned.
Contents:

- the re-derived corpus: the frozen 78-row prefix, the post-#103 appended rows, and new
  fixture families (dispatcher grammar, head look-alikes, heredoc guard, malformed tails
  and mixed evidence, offset oracle cases, the `StmtsSeq` canonical fixture), every case
  labeled `must certify`, `intentional exit 2`, or `outside direct-invocation contract`;
- the legacy-reason normalization artifact (6.4) at baseline `be4b7b1`;
- the certified-construct table, reason-code-to-scope table, stable-reason table,
  dispatcher grammar, and pre-policy command matrix;
- the protocol JSON Schema, cross-language conformance fixtures, and raw negative
  fixtures;
- the helper-identity digest-input manifest (section 4.3);
- `limits.json`: every cap and formula in sections 3.5 and 4.4, work-unit definitions,
  and the performance and RSS ceilings of section 9;
- budgets, tripwires, the platform matrix with frozen runner labels, target triples,
  wheel tags, and build-container digests, and exact pins:
  Go toolchain (exact version and per-builder sha256), `mvdan.cc/sh/v3` v3.13.1, bash
  5.2.21 with the container digest and binary sha256, shfmt 3.13.1 with sha256 and exact
  command lines, and pinned CI action revisions.

Gate results live outside the checkpoint and record: the checkpoint digest, the evaluated
implementation commit and tree hash, helper and wheel hashes, actual hosted runner image
versions discovered from the job logs, and tool versions. The evaluated implementation tree is pinned explicitly because the
decision-record commit cannot identify itself. A code change reruns affected gates
against the new tree; any post-freeze checkpoint change creates a new checkpoint revision
and invalidates the entire evaluation.

## 9. Gates, budgets, and tripwires

Zero false-safe outcomes anywhere is the overriding hard gate; false-safe, indeterminate,
and false-positive metrics are always reported separately.

1. Corpus: every labeled case produces its complete predeclared
   (status, invocations, reason-category) tuple.
2. Replay: the full inventory (580-entry baseline plus post-#103 entries) under the 6.4
   tuple comparison; zero divergences outside predeclared categories.
3. Tier 1 (rendered managed workflows): exactly the `(ci, false)`, `(check, false)`, and
   `(lint, false)` findings with zero diagnostics.
4. Tier 2 (checked-in PR workflows): exactly the predeclared pruning and not-applicable
   outcome, zero findings, zero diagnostics.
5. Tier 3A (documented conformance): complete tuples, zero unexpected.
6. Tier 3B (20 provenance-recorded envelope fixtures, re-derived expectations): at most
   2/20 total indeterminate, at most 2/20 newly indeterminate, false-positive exactly
   zero, false-safe exactly zero.
7. Differential oracles: pinned Bash and shfmt on representative and disagreement cases;
   zero false-safe.
8. Offset oracle: multibyte text, parse errors, nested sites, and template positions
   before, inside, and after `{0}`; exact expected raw-text indices.
9. Adversarial supervision: every `BatchFailure` category exercised (crash, timeout,
   oversize, malformed, trailing output, duplicate ids, pin and identity mismatches,
   transport failures), all failing closed as specified.
10. Cross-language conformance: shared fixtures byte-exact through Go marshal and Python
    decode, including the raw negative fixtures.
11. Bounds and memory: adversarial inputs against every cap in `limits.json`, plus a
    measured peak-RSS ceiling of 256 MiB on the adversarial corpus (bounded input plus
    measured ceiling; no OS-level isolation claim).
12. Performance, end-to-end shape: collect through aggregation with one fresh helper
    process and one repository-wide batch per repetition; 50 repetitions per Python
    version (3.13 and 3.14); corpus digest recorded; fleetyard trusted-half median
    ceiling 750 ms with p95 recorded; work-counter CI half against its own predeclared
    ceilings.
13. Wheels and degradation: the section 7 evidence, both halves; the five installed
    candidate wheels (packaging, `helper_locator.py` lookup, direct protocol execution)
    and the helper-free sdist degradation harness. The actual CLI exit-2 smoke repeats
    in PR B.
14. Surface accounting, two measures over a frozen path set: owned surface (full
    successor-owned files, changed symbols in shared modules, and all schema, build, and
    packaging logic) at most 2,200 production lines; net reduction (production size at
    `be4b7b1` minus the projected integrated PR B tree) at least 1,400 lines against the
    3,704-line deletion baseline (`shell_scanner.py` 3,031 plus
    `direct_marker_scanner.py` 673). Tests, fixtures, generated data, `go.mod`,
    `go.sum`, and CI surface are reported separately. The same accounting reruns against
    the actual PR B tree.

Packaging tripwires: one bundled binary per wheel, zero new runtime Python dependencies,
helper binary at most 12 MiB, platform wheel at most 16 MiB, at most five native target
executions in CI (Python 3.13 and 3.14 sequential within each), artifact retention seven
days.

Breaching any tripwire or hard gate selects the retain path: retain the hardened current
scanner, execute the D3 recognizer disposition (remove, or relocate as an explicitly
test-only oracle), publish the retain decision record, and lift the release freeze
without parser integration. The numeric tripwires above are owner-ratified at checkpoint
review before any implementation exists.

## 10. Delivery

Order of work: Go toolchain install on fleetyard (pinned version, checksum-verified
tarball to `/usr/local/go`, LCARS tool-inventory update); checkpoint plan; frozen
checkpoint commit; owner checkpoint review; implementation plan; SDD execution with
per-task reviews (implementation delegated per the standing delegation policy);
implementation review and repairs; final gate run; evidence pin; decision record; final
documentation-only review. Any later candidate-code change returns to the final gate
run. The evaluation CI workflow ships in this PR, publishes nothing, and leaves the
release job untouched.

On a passing decision record, PR B executes section 5.4 with compatibility and rollback
criteria and the next-major version bump. On failure, the retain path of section 9
executes instead. Either way the freeze lifts only through the decision record.
