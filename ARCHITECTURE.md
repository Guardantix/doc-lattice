# doc-lattice Architecture

## System Overview

doc-lattice is a deterministic, offline traceability engine for design and
production documentation. It reads markdown docs that carry lattice frontmatter and
anchored sections, derives an id-indexed edge graph on demand, and reports staleness
between an upstream source and the downstream docs that derive from it.

The engine is a pure pipeline behind the thin, impure `doc_lattice.cli` package:

    config -> discovery -> frontmatter parse -> loader.build_lattice
        -> { check, impact, reconcile, graph, lint, linear }

`cli/application.py` constructs Typer and registers the commands, `cli/runtime.py`
captures fresh invocation state, and focused adapters under `cli/commands/` connect
Typer to the engine. Shared output policy lives in `cli/output.py`;
`cli/errors.py` supplies diagnostics and command-level error conversion, while
`cli/__init__.py` owns entry-point exception mapping.
`orchestrate.load_lattice(project)` is the single wiring point that runs the pipeline;
`init` is a separate scaffolding command that never loads the lattice. The central
structure is the `Lattice` (model.py), which every lattice-reading command reads.
This file owns the durable module boundaries and load-bearing decisions. CLAUDE.md
routes contributors and agents to those decisions and lists enforced repository rules.

## Decision Log

### AD-1: A broken ref is a lattice state, not a load error

**Date:** 2026-06-27
**Status:** Accepted
**Context:** A `derives_from` ref can point at an id that no longer exists.
**Decision:** An unresolved ref loads cleanly as `target_id=None` and is reported by
`check` as BROKEN (exit 1, drift). Index coherence fails only when two files repeat a
file id or two headings in one file resolve to the same file-scoped anchor; either
case raises `DuplicateIdError` (exit 2). Index keys are `TargetId(file_id, anchor)`,
so equal anchors in different files do not collide, and a file id equal to another
file's anchor does not collide.
**Consequences:** Exit 1 means 'the graph is coherent but drifting' and exit 2 means
'the index is incoherent'. A single broken edge never blocks a node's reconcilable
edges.

### AD-2: Pure core, thin impure shell

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Graph and report logic must be testable against synthetic inputs.
**Decision:** All graph and report logic is filesystem-free and pure. `config`,
`discovery`, and `orchestrate` own load-path filesystem work. `persistence.py` owns
shared low-level durable staging, replace, create-if-absent, fingerprint, sync, and
cleanup primitives. `reconcile_transaction.py` owns the reconcile lock capability and
mechanics, independent live destination preflight for commits, durable commit and
rollback, journal and artifact recovery containment and validation, and cleanup. The
`doc_lattice.cli` package owns the application boundary. Its
`cli/commands/reconcile.py` adapter resolves document identity paths before fresh
reads and orchestrates lock acquisition and lifetime, recovery, loading, planning,
and the transaction commit call. Final outcome reporting, including success output,
occurs only after clean lock release; an automatic-recovery notice may be emitted on
stderr while the lock is held. Within the cache package, `cache/schema.py` and
`cache/state.py` are filesystem-free, `cache/store.py` owns cache-file I/O, and
`cache/lookup.py` reads and stats documents to select the verify or stat tier.
`linear_fetch` is impure wiring and `linear_client` is the only module that touches
the network.
**Consequences:** Every command's logic is unit-tested with no I/O; the network slice
is quarantined to one module.

### AD-3: Untyped-to-typed boundary policy

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Raw YAML and Linear JSON arrive untyped.
**Decision:** `typing.Any`/`typing.cast` are allowed only in boundary modules
(`scripts/check_typing_boundaries.py`); the real boundaries are `frontmatter_parser`
and `linear_parser`, which validate into typed models. Everywhere else passes typed
values.
**Consequences:** Untyped data cannot leak past two named files; CI enforces it.

### AD-4: Canonicalized, truncated content hash

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Drift must be insensitive to cosmetic edits.
**Decision:** Each edge stores a `seen` hash; the live hash is
`sha256(canonicalize(text))` truncated to 32 hex chars (128 bits), where
`canonicalize` normalizes line endings, strips trailing whitespace per line, and
trims leading and trailing blank lines. It preserves internal line breaks and blank
lines.
**Consequences:** Paragraph reflow changes the hash; normalized line endings,
trailing whitespace, and leading or trailing blank lines do not. 128 bits is ample
for a human-scale corpus.

### AD-5: Reconcile is a durable whole-batch transaction

**Date:** 2026-07-13
**Status:** Accepted
**Context:** Reconcile is the only command that mutates tracked documents. It must
reject edits made after validation, prevent concurrent reconciles from interfering,
and leave an interrupted multi-file batch recoverable.
**Decision:** Every reconcile mode acquires a nonblocking advisory lock on the
existing project-root directory through `reconcile_transaction.reconcile_lock`. The
transaction module owns the lock capability and mechanics; the CLI adapter owns its
lifetime across any recovery, lattice loading, planning, fresh reads, and commit call.
For each planned write, the adapter resolves the document identity path against the
project root before re-reading exact bytes. `commit_rewrites` independently calls
`_preflight_rewrite_destinations`, which uses `safe_resolve` to contain every supplied
live destination against the canonical project root before staging.

The transaction retains exact before and after bytes and stages synced before-image
and after-image files beside each destination. Before mutation, it durably publishes
the `prepared` journal at `.doc-lattice-reconcile.json`. Immediately before each
atomic replacement, it compares the destination's full SHA-256 fingerprint with the
validated before bytes; a mismatch is a conflict. Replacement and namespace changes
include file and parent directory synchronization. Any pre-commit conflict or
persistence failure rolls back transaction-owned after images in reverse order while
preserving unrelated edits. At the point of no return (PONR), all destinations are
durable and the journal is durably marked `committed`; recovery then preserves those
destinations and only cleans staged evidence. Success output is emitted only after
committed cleanup and clean lock release.

Normal real startup recovers a valid outstanding journal before lattice loading.
`reconcile --recover` performs only that recovery, while dry-run never recovers or
persists anything and refuses an outstanding journal. Invalid or unauthenticated
recovery evidence is retained for explicit manual remediation rather than guessed at
or deleted. The transaction module resolves journal paths through `safe_resolve` and
validates project-relative containment, path roles, artifact locations and file types,
and recorded fingerprints before recovery mutates them.

**Consequences:** A successful reconcile is a durable all-or-nothing batch from the
operator's perspective. A `prepared` journal rolls transaction-owned changes back; a
`committed` journal records that PONR has passed and makes recovery cleanup-only. The
lock serializes doc-lattice reconcile processes but does not coordinate unrelated
editors. This contract assumes local-filesystem `flock`, atomic rename, and directory
sync behavior; network filesystems are outside it.

Shared write semantics stop at the primitive boundary. Cache persistence uses the
same durable atomic-replace primitive but remains a disposable, best-effort
single-file write whose `OSError` is reported once and swallowed. `init` uses durable
create-if-absent, still refuses to replace an existing config, and never joins a
reconcile journal. It always prints transaction-artifact `.gitignore` guidance but
does not modify `.gitignore`.

### AD-6: lint is a pure structural check, separate from drift

**Date:** 2026-06-28
**Status:** Accepted
**Context:** Authority inversion (a more-authoritative doc deriving from a less
authoritative one) is a structural error, not staleness.
**Decision:** `lint` ranks `derives_from` edges on the binding > derived > exploratory
ladder, flags inversions, reports edges it cannot rank, never mutates, and exits 1 on
a violation (mirroring `check`).
**Consequences:** Structural validity and drift are independent gates.

### AD-7: Tag-gated PyPI distribution

**Date:** 2026-07-12
**Status:** Accepted
**Context:** Releases publish wheels and source distributions to PyPI, with the tag as
the immutable source identity and no stored PyPI credential.
**Decision:** A merge-triggered `release` job validates or creates the `vX.Y.Z` tag.
The dependent, unprivileged `build-release` job checks out that exact tag, builds and
validates the distributions, and transfers them as an artifact. The OIDC-only
`publish` job downloads and publishes that artifact without checking out repository
code.
**Consequences:** Build input is tied to the validated tag, while the credentialed
publisher executes neither repository code nor package build code. See RELEASING.md.

### AD-8: Symlink targets and document identity

**Date:** 2026-07-13
**Status:** Accepted
**Context:** A discovered markdown path may be a symlink, and multiple configured
roots or aliases may reach the same physical document.
**Decision:** Discovery resolves each candidate against the project root for
containment and deduplication, but retains the first unresolved path as the document's
identity. Project-internal targets are allowed; external targets are skipped with a
warning. Before fresh reconcile reads, the `cli/commands/reconcile.py` adapter resolves
the document identity path and requires the current destination to remain inside the
project root. The transaction layer then independently contains each supplied live
commit destination against the canonical project root before staging.
**Consequences:** Internal symlink paths remain stable in reports and cache keys,
aliases load a resolved document only once, external content is never read, and a
symlink retargeted after load cannot redirect a reconcile write outside the project.
Containment is enforced both before fresh reads and again at the durable transaction
boundary.

### AD-9: Per-invocation CLI package boundaries

**Date:** 2026-07-14
**Status:** Accepted
**Context:** The command-line application must isolate repeated invocations while
preserving the installed `doc_lattice.cli:main` entry point and the importable `app`
compatibility surface. Command wiring also needs named ownership boundaries without
moving durable reconcile mutation into the CLI.
**Decision:** `doc_lattice.cli` is a package. `cli/application.py` constructs and
registers Typer; `cli/runtime.py` creates a frozen runtime for each invocation with
stdout, stderr, cwd, and config and lattice loaders; and `cli/output.py` centralizes
format validation, the JSON alias, indentation, exact output, and GitHub annotations.
The seven modules under `cli/commands/` are narrow command adapters. There are no
mutable module-level consoles and no mutations of Typer color globals.

`cli/errors.py` owns diagnostic rendering, exit constants, and command-level
`ProjectError` context conversion. `cli/__init__.py` preserves
`doc_lattice.cli:main`, loads the compatibility `app` export lazily, and owns
entry-point exception mapping: `ProjectError` and the supported unexpected errors map
to exit 2, while intended `SystemExit` values propagate unchanged.

`cli/commands/reconcile.py` resolves selected document identity paths before fresh
reads and orchestrates lock acquisition and lifetime, recovery, lattice loading,
planning, the transaction commit call, and final outcome reporting after lock release.
An automatic-recovery notice may be emitted on stderr while the lock is held. Lock
capability and mechanics, independent live commit destination preflight, durable
mutation and rollback, recovery containment and validation, and cleanup remain in
`reconcile_transaction.py`.
**Consequences:** Invocation state and diagnostics can be tested without shared
console state. Tests under `tests/cli/` mirror the command adapters and add focused
runtime, output, and cross-command contract coverage. Durable reconcile safety keeps
its independent transaction boundary.

### AD-10: Output selector compatibility converges in 2.0

**Date:** 2026-07-14
**Status:** Accepted
**Context:** The 1.x commands exposed structured output through different selectors.
Removing `--json` during 1.x or warning on stderr would have broken scripts, but carrying
both selectors indefinitely would have preserved an inconsistent interface.
**Decision:** `--json` remained silent throughout 1.x and is removed in 2.0. Selector
availability was fixed by command and release as follows:

| Release | Commands | Structured-output selection |
|---------|----------|-----------------------------|
| 1.x | `check`, `lint` | `--format human\|json\|github`, plus silent `--json` alias |
| 1.x | `graph` | `--format mermaid\|dot\|json`; no `--json` alias |
| 1.x | `impact`, `reconcile`, `linear` | Human default; only silent `--json` selector |
| 1.x | `init` | Deliberately no structured-output selector |
| 2.0 | `check`, `lint` | `--format human\|json\|github`; no `--json` alias |
| 2.0 | `graph` | `--format mermaid\|dot\|json`; no `--json` alias |
| 2.0 | `impact`, `reconcile`, `linear` | `--format human\|json`; no `--json` alias |
| 2.0 | `init` | Remains excluded from structured-output selection |

In 2.0, `--json` is therefore removed from `check`, `lint`, `impact`, `reconcile`,
and `linear`; `graph` never accepted that alias. Where supported, `--indent` is valid
only when the effective format is JSON.
**Consequences:** The CLI package refactor preserved current byte-exact output through
1.x. The silent 1.x alias was behaviorally compatible and emitted no deprecation warning.
The cost was that selector inconsistency persisted through 1.x, and the migration notice
was documentation-only because stderr could not carry a compatibility-safe warning. This
decision did not freeze every 1.x output schema.

### AD-11: Linear is a read-only, opt-in network boundary

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Live ticket status is useful for analysis, but the local graph must remain
deterministic and repository-controlled input must not gain an open network capability.
**Decision:** Only the opt-in `linear` command touches the network, and only
`linear_client` performs requests. Its API key comes exclusively from the environment;
the GraphQL endpoint is hardcoded HTTPS and redirects are refused. Repository-controlled
ticket refs are validated, bounded, and queried within the configured team, failing closed
when that scope is invalid. Ticket status is never persisted, and trigger construction,
response parsing, grading, and rendering remain pure.
**Consequences:** Every other command remains offline, and running `linear` requires an
explicit secret-bearing environment. Live status affects only the current report, never
the lattice or later results. Network policy stays concentrated in one auditable module,
while the analysis can be tested without network access.

### AD-12: The load cache is a disposable, opt-in accelerator

**Date:** 2026-07-10
**Status:** Accepted
**Context:** Large doc sets benefit from reuse across runs and worktrees, but caching
must not weaken default correctness or become part of the project state.
**Decision:** A validated, safe `cache_key` opts into a cache slot under the user cache
home, outside checkouts so worktrees can share it. By default, cache hits re-read and hash
document bytes. `cache_trust_stat` explicitly permits read-only commands to trust unchanged
size and modification time, accepting stale content or masked unreadability when both remain
unchanged. Reconcile always verifies bytes. Cache contents are disposable, and cache write
failure may report a diagnostic but cannot change command output or exit status.
**Consequences:** The default tier matches uncached results for caches produced by doc-lattice
and for missing, unreadable, schema-invalid, or version-stale cache files. The same-user cache
is trusted; schema-valid manual tampering is outside the integrity guarantee. The stat tier's
staleness and readability tradeoff applies only when explicitly enabled for read-only commands,
and it cannot influence reconcile writes. Normal cache deletion, read failure, or write failure
affects acceleration rather than command results.

### AD-13: Section identity uses a pinned compatibility adapter

**Date:** 2026-07-13
**Status:** Accepted
**Context:** Section refs need stable GitHub-compatible identities, while general Markdown
parsers and Unicode behavior can change independently across runtimes.
**Decision:** Section discovery intentionally supports a narrow addressable Markdown subset
through a compatibility adapter pinned to exact `markdown-it-py==4.2.0` behavior and a
`github-slugger@2.0.0` target. Generated Unicode data closes the supported Python and
JavaScript runtime gap. Node is required only to regenerate and verify that artifact during
maintenance, never at runtime.
**Consequences:** Supported headings and slugs remain stable across ordinary dependency and
runtime updates, and unsupported Markdown constructs stay deliberately unaddressable. Parser,
slugger, or Unicode target changes require an explicit compatibility review, regeneration,
parity verification, and benchmark validation. The shipped Python package has no Node
dependency.

### AD-14: Documentation ownership is one-way

**Date:** 2026-07-14
**Status:** Accepted
**Context:** Repeating current behavior across user docs, contributor guidance, roadmaps,
and completed implementation documents creates conflicting sources of truth.
**Decision:** README.md owns the user contract; ARCHITECTURE.md owns durable decisions and
module boundaries; CLAUDE.md routes contributors and agents and lists enforced repository
rules without restating behavior; CHANGELOG.md owns release history and migrations; and
roadmap.md owns future direction. Maintained documents link to the owner instead of copying
its content. Completed implementation specs and plans, duplicate convention guides, and
incomplete history logs are deleted after durable content reaches its owner, rather than
maintained or archived in the repository.
**Consequences:** Each fact has one maintained owner, so changes update one source and its
incoming links. Historical implementation detail remains available through version control,
while the maintained documentation stays smaller and current.

### AD-15: Speculative configuration is removed instead of reserved

**Date:** 2026-07-14
**Status:** Accepted
**Context:** `binding_layers` was accepted by strict configuration but had no consumer,
which implied a future contract without an approved requirement or defined behavior.
**Decision:** `binding_layers` is removed for 2.0 rather than implemented. Existing 1.x
configs migrate by deleting the key, with no replacement. Authority behavior remains
`lint`'s fixed binding > derived > exploratory ladder, and strict configuration rejects
the removed key.
**Consequences:** This is a documented breaking change in the next major release. Future
configuration keys are not reserved as inert surface without an approved requirement.

### AD-16: GitHub administration remains an external human boundary

**Date:** 2026-07-15
**Status:** Accepted
**Context:** Workflow files are repository-controlled input, so a same-repository pull request can
edit a workflow that receives a broadly scoped secret. The normal package code must not receive
GitHub administration credentials while establishing a safer authorization boundary.
**Decision:** `doc_lattice.github_ci` renders and audits the managed workflows, bootstrap artifact,
and a scoped `.github/.gitattributes` policy that preserves LF for the bootstrap after checkout.
CLI filesystem adapters resolve the Git top-level, then create, inspect, audit, and refresh those
local files without loading the lattice or accessing the network. A reviewed external `gh` script
is run explicitly by a human maintainer to configure and read back a GitHub environment whose
deployment allow list is exactly the `main` branch. The dedicated
`DOC_LATTICE_LINEAR_API_KEY` exists only in that environment and is mapped to `LINEAR_API_KEY`
only on the final Linear step. Repository-global audit policy bans `pull_request_target` and
whole-context reusable-workflow secret inheritance, and generated workflows never run real
`reconcile`.
**Consequences:** `linear_client` remains the only Python network module. Remote setup is explicit,
reviewable, resumable, and separate from the Linear secret entry. Bootstrap verification covers
remote environment and secret-name metadata. Local audit covers workflow policy and bootstrap
ownership metadata rather than byte equality, plus the exact effective bootstrap LF attribute,
while managed refresh owns byte-level comparison and replacement. None of the local checks can
observe GitHub environment or organization-policy drift, so the bootstrap verifier remains
necessary. Workflow and branch governance for trusted `main` remains the residual authorization
boundary.

### AD-17: CI shell marker certification is inverted

**Date:** 2026-07-23
**Status:** Accepted
**Context:** The retained CI shell scanner previously refused marker-bearing commands only when
an open enumeration recognized a reachable inline dispatcher. Unrecognized wrappers, script-file
forms, query commands, and ordinary-looking heads could therefore carry a doc-lattice marker while
being certified by omission. A trusted inert-head list would have the same flaw because shell
functions, aliases, and `PATH` can shadow bare names.
**Decision:** Each finalized decoded assignment-prefix, argv, or array-assignment element word
records whether it matches the ASCII, case-insensitive `doc[-_.]+lattice` marker, and
simple-command state aggregates that fact.
When the existing resolver does not classify the effective executable as doc-lattice, any retained
marker fails closed. Resolved doc-lattice commands keep the existing launcher, option,
subcommand, and post-resolution behavior, including certified-empty root help/version forms. The
dispatcher head sets, shell `-c`/noexec option walk, executable-candidate recording, and opaque-tail
provenance are removed; `_ResolvedIndex.external_lookup` and uv requirement-name derivation remain
where the invocation-finding path uses them.
**Consequences:** Unknown marker-bearing heads refuse by construction, with no inert-command
allowlist. Marker detection adds one source-cap-bounded scan of each finalized decoded word and an
O(1) aggregate check at command flush. The resolver remains syntactic and does not prove runtime
identity or model function/alias/`PATH` shadowing or cross-command data flow; comments and
discarded redirection operands remain outside retained-word certification. Frozen evaluation
checkpoints stay immutable, while live scanner and audit tests own the changed expectations.

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
summary. A call to a function defined in the same body reproduces that function scope's aggregated
stdout, so wrapping a producer in a function preserves the handoff. A pipeline keeps its
producer-to-consumer edge across the newline that follows `|` and when its consumer is a simple
command inside a control body, while a compound consumer still binds the compound scope. Parameter
default/alternate forms produce in-word choices, assign-default also emits a conditional variable
definition, and every other parameter operator keeps three alternatives: an empty string, the
subject variable unchanged, and the variable beside its authored operand. The empty alternative
means a transform that can erase its value cannot hide a marker across the expansion, and the
ungapped pass-through alternative means a transform whose pattern does not match cannot hide one
either, so `A=doc-; bash -c "${A#zz}lattice"` refuses. Bounded static brace expansion fans one
lexical word into ordered argv ports; because
brace expansion precedes parameter expansion, an authored comma list expands even when a member's
content is dynamic. An array literal holds `concat(choice("", e1), ..., choice("", en))` over its
element contents in literal order, so one expression covers `${A}`, `${A[i]}`, `${A[@]}`,
`${A[*]}`, and their slices: each read independently selects a subset, a single subscript picking
one element and emptying the rest. Element separators are dropped, which over-approximates in the
fail-closed direction, and `A+=(w)` composes through the existing append path. An element spelled
`[subscript]=value` leaves literal order no longer equal to index order while a joined read
concatenates by index, so that spelling fails closed at the scanner. Fields an element produces by
word splitting recombine without their separator, so `X="doc- lattice"; A=($X); eval
"${A[0]}${A[1]}"` still certifies; that is the same splitting boundary this decision already
records for loop headers rather than a new one. `for`/`select` iteration words join into
loop-variable evidence, and a header
this scan cannot enumerate, whether an arithmetic header, implicit positionals, word splitting, or
pathname expansion, contributes an external gap rather than failing the scan; `while`/`until`
repeat the test list around each body iteration; `case` `;&` and `;;&` preserve fallthrough
sequence. Ordered descriptor replay installs pipeline endpoints first and then applies redirections
left to right, so only final descriptor bindings route bytes while earlier truncations retain their
empty-file side effect. A `>&N` source resolves first against the command's own events and then
against the bindings its enclosing compounds installed, innermost first, so
`{ producer >&3; } 3> out.sh` routes the producer's stdout into `out.sh`. Descriptors 1 and 2 are
always inherited from the enclosing shell and need no such binding. Any other descriptor that some
part of the body binds, but that this command's lexical chain cannot supply, is missing evidence
rather than an inherited stream: a bare `exec 3> out.sh` rebinds the shell scope, which the
per-command `redirections` field cannot carry, so a later `>&3` fails closed. A descriptor nothing
in the body binds is a Bash runtime error rather than a flow, so it keeps routing nowhere.

Execution sinks are `eval`, shell `-c`, selected shell stdin, and static script execution through a
shell operand, direct path, `source`, or `.`. A `/dev/stdin`, `/dev/fd/0`, or `/proc/self/fd/0`
script operand resolves to that command's own stdin instead of to a static resource no writer
reaches. Effective-head evidence comes from the complete
existing assignment, keyword, `builtin`, `command`, `exec`, `env`, `time`, `coproc`, `uv run`,
`uvx`, and `uv tool run` grammar. Its `external_lookup` provenance prevents an external `env eval`
from being mistaken for the shell builtin. The shell source selector chooses `-c`, a script
operand, or stdin; if a dynamic selector could choose a marker-capable authored port, it fails
closed.

Exact eval payload interpretation is a bounded sub-analysis, not a shell interpreter. When an
`eval` payload resolves to exact literal text, that text is tokenized and its state effects are
replayed so a later sink observes them. This is what refuses
`X=safe; eval 'X=doc-'; eval "$X"lattice`, where no single payload carries the marker. It models
scalar assignments and assignment prefixes, the `declare`, `export`, `local`, `readonly`, and
`typeset` declaration builtins including `-g` and `-n` namerefs, `unset`, the `builtin` and
`command` wrappers, `if`/`elif`/`else` reachability by literal command status, `case` arm bodies,
subshell groups, and the function effects and call-graph names a payload contributes. Nameref
cycles, nameref targets that are not static variable names, and command prefixes that cannot be
represented fail closed.

The replay is reached from `eval` alone. A `source` or `.` payload does not enter it, so state a
sourced script establishes is not observed by a later sink; that gap is tracked in issue #133. A
shell `-c` payload does not enter it either, which is harmless on its own because a child shell's
assignments do not persist into the parent, and its own parameter references are covered by the
second pass described below. A payload the tokenizer cannot accept contributes no evidence and
does not invalidate previously recovered state, so it certifies rather than refusing; under a
fail-closed contract that is a defect rather than a boundary, and it is tracked in issue #134.

A shell `-c` payload also enters the parameter second pass, not only the state-effect replay. The
child expands the payload itself, so `export A B; bash -c '$A$B'` composes the marker although no
word of the parent command carries it. Those references resolve against this body's whole variable
table rather than the exported subset the child would inherit, which over-approximates and so stays
fail-closed; modeling export status precisely is future work.

The stop-line is deliberate. This sub-analysis interprets exact literal payloads only, never
dynamic ones, and growth beyond the constructs listed above is out of scope: an interpreter chasing
`eval` has no natural terminating point, and this engine is defense in depth behind human review,
not the boundary that contains untrusted code. Values bearing arithmetic expansion and `eval`
nested inside an `eval` payload are therefore not modeled, and an array assignment fails closed
rather than being interpreted.
Verification under real bash on 2026-07-25 confirmed that nested `eval` does persist its
assignments to the current shell, so that is a known false-safe gap rather than evidence of safety.
Closing it belongs in a bounded guard that fails closed on an unmodeled state-carrying construct,
not in more interpretation.

Brace groups and loop bodies are the exception to that stop-line and are modeled, because they
persist their assignments the same way and are cheap to replay. `_STATIC_EVAL_MUTATION_PREFIXES`
carries the `{`, `do`, `while`, and `until` keyword entries that make this work, and those four
entries are load bearing: removing them as apparent dead weight reopens a real false certification.
An array assignment inside a payload was worse than unmodeled, and not for the reason issue #132
first recorded: the payload tokenizer lexes an unquoted `(` as a command separator, so
`eval 'A=(doc-)'` recorded the scalar `A = ""` and scattered the elements into following commands.
That empty value is a positive safety assertion the analysis had not earned. The compound
spelling, a `NAME[subscript]=` element write, and a `declare`, `local`, `readonly`, or `typeset`
carrying `-a` or `-A` now all fail closed, which is the bounded guard this stop-line prescribes. A
quoted or escaped `(` stays inside its word, where it really is one scalar character, and still
certifies.

Variable, resource, and stream reference resolution is a monotone least fixed point and is
independent of source order. Append accumulation is not: a `+=` write seeds from the value present
at its first visit, so write order sequences the result. Both orderings agree with Bash, but a
refactor that canonicalizes write tuples would change verdicts and must not be treated as a safe
rewrite. Alternative width, expression nodes, table entries, graph edges, brace expansion, resolved
exact value length, and successful fixed-point updates have deterministic caps that refuse on
exhaustion, so an eval payload that grows itself cannot exhaust time or memory. The two exact-text
projection caps are the exception: they widen to the top of the content lattice instead of
refusing, as described at the end of this decision.

The absence-of-evidence boundary is cross-step/job/action/workflow flow, external values and files
beyond generic may-output, arbitrary encoding/transforms, dynamic resource aliases, shell-scope
descriptor state carried across commands by a bare `exec`, eval payload constructs outside the
bounded exact-literal set above, and AD-17's alias, `PATH`, and dynamic-executable limitations.
This list is the boundary as designed, not a complete inventory of what the engine currently
misses; open gaps against this decision are tracked in issues #125 through #141. A `read` beyond
the first record of a shared stream is projected as record one, so a marker split across later
records is not yet seen; that under-refusal is issue #121, and the `read -a/-d/-n/-N/-u`
over-refusal it interacts with is issue #119.
**Consequences:** Split variable, pipe, heredoc/herestring, substitution, and static-file handoffs
that execute authored marker content now exit 2. Marker-free dynamic execution and a marker whose
required character comes only from external content continue to certify with the boundary
disclosed. `audit.py` still invokes the scanner independently for each step; future job-level
aggregation can consume the evidence shape without changing the parser/analysis ownership
boundary.

Execution sinks are recognized in three further positions, none of which requires an allowlist of
command names. A command name composed across a variable boundary is itself a sink, because Bash
executes the head after expansion; a head that already resolves to a marker name stays outside this
check because the command-local resolver owns it. A head this scan cannot resolve to an exact name
carries `ambiguous` executable evidence and selects a shell source anyway, rather than dropping the
sink, on the same reasoning AD-17 used to reject an inert-head allowlist. An unrecognized head that
names a shell later in its own argv, such as `timeout`, `nohup`, `nice`, `setsid`, `stdbuf`,
`flock`, or `sudo`, selects from that shell, and a shell whose operand is missing behind such a
launcher takes the launcher's standard input as its payload, which is what `xargs bash -c` does. An
`external_lookup` head keeps its existing treatment and does not select a nested shell, so an
external shadow of `command` or `builtin` is not reinterpreted as a wrapper.

Three constructs rebind state the per-command evidence shape cannot carry, so they fail closed
rather than being modeled. A bare `exec` that rebinds descriptor 0, 1, or 2 changes the enclosing
shell scope for every later command, which the per-command `redirections` field cannot express. A
`read` that uses `-a`, `-d`, `-n`, `-N`, or `-u`, and every `mapfile` or its `readarray` synonym,
either writes an array from a stream the model carries no per-element content for, or reads a
bounded prefix, which can compose a marker the full stream does not contain; widening either to
the whole stream would drop the flow instead of over-approximating it. An array literal supplies
its element contents directly and so is composed rather than refused.
A `set --` or `shift` outside a function body rewrites positional parameters that are bound for
function contexts only. The first two refuse at the scanner and the third refuses in the taint
module. Where an exact projection is merely lost rather than absent, the solver
instead widens to the top of the content lattice, an alternative that accepts from every DFA
entry state, so the value stays visible at each sink and only a body that actually reaches one
is refused.
