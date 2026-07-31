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
command inside a control body, while a compound consumer still binds the compound scope. A command
naming a static resource operand this body writes, such as `cat s.sh`, or reading a process
substitution such as `cat <(...)`, reproduces that resource's or substitution's content the same
way redirected stdin does; all three are the same generic unknown-command's-stdout assumption, so
the redirection, named-operand, and process-substitution forms of the same handoff are inside the
modeled may-output boundary together rather than reading as different boundaries. Only content this
body never writes stays outside it as absence of evidence. Parameter
default/alternate forms produce in-word choices, assign-default also emits a conditional variable
definition, and every other parameter operator keeps three alternatives: an empty string, the
subject variable unchanged, and the variable beside its authored operand. The empty alternative
means a transform that can erase its value cannot hide a marker across the expansion, and the
ungapped pass-through alternative means a transform whose pattern does not match cannot hide one
either, so `A=doc-; bash -c "${A#zz}lattice"` refuses. That argument covers a transform HIDING a
marker its subject already carries, and it does not extend to one FORMING a marker by deleting
characters: the transformed text is not among the three alternatives, so `X=docX-;
"${X/X/}lattice"` certifies while Bash composes the marker. The two directions are easy to
conflate because prefix removal happens to be safe for an incidental reason, the untransformed
value already composing, which is why the `${A#zz}` example above refuses while the `${X%zz}`
mirror image does not. Issue #172 tracks the formation direction across the substitution, removal,
and substring operators in both passes. Bounded static brace expansion fans one
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
`{ producer >&3; } 3> out.sh` routes the producer's stdout into `out.sh`. A stdout writer is a
simple command or a compound, since a compound carries redirections and aggregated stdout of its
own, so `{ producer; } > >(consumer)` binds the compound's stdout to the substitution's consumer
exactly as the ungrouped `producer > >(consumer)` binds one command's. A process substitution is a
descriptor target like any other on that chain, so `{ producer >&3; } 3> >(consumer)` reaches the
consumer as well. Descriptors 1 and 2 are always inherited from the enclosing shell and need no
such binding. Any other descriptor that some part of the body binds, but that this command's
lexical chain cannot supply, is missing evidence rather than an inherited stream: a bare
`exec 3> out.sh` rebinds the shell scope, which the per-command `redirections` field cannot carry,
so a later `>&3` fails closed. A descriptor nothing in the body binds is a Bash runtime error
rather than a flow, so it keeps routing nowhere.

Execution sinks are `eval`, shell `-c`, selected shell stdin, a registered `trap` action, and
static script execution through a shell operand, direct path, `source`, or `.`. A `trap` action is
shell source the shell reads and executes when the signal arrives, so deferring its expansion to
that moment is what hid it rather than any difference in what it runs: as a value the ordinary
single-quoted action is inert text, so it takes the same second parse an `eval` payload takes and
`A=doc-; trap '${A}lattice reconcile' EXIT` refuses. The three spellings that register nothing are
excluded rather than over-refused: `-l` and `-p` print instead of registering, and a literal `-`
restores the default disposition. Whether a signal ever arrives is not modeled, so a trap on a
signal this body never sends still refuses. The command-name word is a sink on the same footing,
because Bash executes it after expansion. That holds whether the head resolves to a name, resolves
ambiguously, or resolves to nothing at all: a command no argv word of which carries literal text,
such as `A=doc-; B=lattice; "$A$B"`, gives the resolver no text to read, and reading that as "runs
nothing" rather than "runs something unknown" left it with no sink and certified the shortest
evasion available. An unresolved head is therefore routed exactly as an ambiguous one is, which is
AD-17's refusal to read an unresolved head as inert applied on the sink side. A command with no
argv word at all, such as a bare assignment, still runs nothing and contributes no sink. A
`/dev/stdin`, `/dev/fd/0`, or `/proc/self/fd/0`
script operand resolves to that command's own stdin instead of to a static resource no writer
reaches. Effective-head evidence comes from the complete
existing assignment, keyword, `builtin`, `command`, `exec`, `env`, `time`, `coproc`, `uv run`,
`uvx`, and `uv tool run` grammar. Its `external_lookup` provenance prevents an external `env eval`
from being mistaken for the shell builtin. The shell source selector chooses `-c`, a script
operand, or stdin; if a dynamic selector could choose a marker-capable authored port, it fails
closed. An `--rcfile`/`--init-file` value is read in addition to whichever of those three the
grammar selects, because Bash reads that file before the selected source; skipping it as an inert
option argument left no sink for it at all and certified
`bash --rcfile env.sh -ic :`. The interactive gate Bash applies to an rcfile is deliberately not
modeled, since recognizing it would mean narrowing a refusal on `-i` evidence the body itself
supplies, which AD-17's founding principle rules out. `--emulate` stays excluded because its value
is a mode name rather than a path.

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

A recovered plain assignment, one that is not a `local`/`declare`/`typeset` genuinely scoped inside
a function context and not a nameref alias, is lowered into the same flow-definition graph an
authored assignment uses, so every sink observes it, not only a later `eval`:
`eval 'X=doc-'; bash -c "$X"'lattice check'` refuses on this path even though no `eval` reads `$X`.
A `declare`/`typeset` an eval payload recovers outside a function context is an ordinary global
assignment in Bash and is lowered the same way. The same declaration builtins recovered genuinely
inside a function context, and nameref-aliasing assignments, stay on the exact-table-only path
above and so are visible only to a later `eval`; that narrower scope is a documented, deliberate
residual, not a regression. A payload the `eval` itself builds from dynamic content, such as a
command substitution or an untracked `declare`, is issue #114's complement: this sub-analysis never
recovers a mutation from it at all, so neither the exact-table path nor this lowering has anything
to act on.

An output redirection a payload performs is lowered the same way, into the resource-write graph an
authored redirection uses, at the enclosing `eval`'s own position in body order so truncation and
append accumulation stay sequenced. Without it a write inside a payload registered nothing at all,
so the file it wrote never entered the resource table and both the `source` guard below and an
ordinary script sink read a key the model believed was never written, certifying
`eval 'printf X=doc- > s.sh'; source s.sh; eval "${X}lattice"` (issue #146). The payload command's
standard output is modeled the way an unknown authored command's is: an external gap, its own argv
content, and the content of any static resource it names as an operand, so the `cat s.sh > t.sh`
handoff is inside the same may-output boundary by either spelling. Three limbs the authored model
carries are absent here, each an over-approximation rather than a dropped flow: `printf` format
exactness, real standard input, and called-function stdout. A branch whose literal status is False
contributes no write, matching the reachability rule the assignment replay already applies, and a
redirection whose target this analysis cannot name resolves to no static key at all, which is the
same dynamic-resource-identity gap issue #151 tracks for the authored route. This lowering does not
build the resource-shaped exact-literal state table the next paragraph rules out; it records what a
payload writes, not what sourcing that file would then assign.

The replay is reached from `eval` alone. A `source` or `.` payload's state effects are
architecturally unreachable to it: recovering them exactly would need a second, resource-shaped
exact-literal table threaded through the same bootstrap phase that builds the variable table, which
does not exist and is not a scoped fix to add. Instead, every route that names a file this body
writes and then has it read as shell source fails closed narrowly, only when the target is a
resource this same body writes and that resource's own content could plausibly carry a marker
fragment. Five routes carry the rule: `source`/`.`, a shell's glob script operand, a shell's exact
script operand, an `--rcfile`/`--init-file` value, and a `BASH_ENV` value.

`BASH_ENV` is the one route argv structurally cannot show. A non-interactive Bash child reads that
file before its selected program, so `export BASH_ENV=env.sh; bash -c :` executed a tracked file's
marker while the argv-driven source selector had nothing to select. Both value spellings are read:
an exported variable, found by unscoped name across every scope because the child inherits whichever
is live, and the per-command prefix assignment, which is scoped to the command and never reaches the
body-wide table. Export status is deliberately not modeled, so a `BASH_ENV` this body sets without
exporting is read too, which over-approximates in the fail-closed direction. This route adds
`_marker_capable` to the child-run content test above, because no sink expression reads this file:
without it, a file composing the whole marker with no assignment in it would have nothing to refuse
on. The POSIX `ENV` spelling of the same channel reaches no guard and is issue #173. It is not the
one-name widening it appears to be, because the two variables have opposite interactivity gates:
Bash reads `BASH_ENV` when non-interactive, a POSIX shell reads `ENV` when interactive, and `bash`
not in POSIX mode reads `ENV` never. Refusing on the name alone would therefore refuse bodies that
read nothing, which is a measurable cost rather than a free widening, and gating on `-i` instead
would contradict the `--rcfile` reasoning above; that issue records the choice as open.

The startup files a shell reads IMPLICITLY are outside this decision's scope and are disclosed here
rather than dropped. An interactive or login shell reads `~/.bashrc`, `~/.bash_profile`,
`~/.profile`, and their system-wide counterparts without any argv word or tracked variable naming
them, so `printf '%s\n' 'A=doc-' '"${A}lattice" reconcile' > "$HOME/.bashrc"; bash -ic :` executes
the marker and certifies, while `--rcfile`, `BASH_ENV`, and `source` on the identical file all
refuse. Every other route in this family resolves a file the body spells, as an operand, an option
value, or a variable's value; an implicit startup file is spelled nowhere, so recognizing one means
synthesizing paths from shell convention, and that set has no natural endpoint across login versus
interactive shells and the shells beyond Bash. That places it on the boundary-extension side of the
AD-19 line rather than the model-integrity side, where a guard fails to reach a route it mirrors.
Issue #178 tracks it, and proposes closing this whole channel family, `BASH_ENV` and `ENV` included,
with one decision over a written-down set of names rather than one name per report.

The content test differs by whether the file's state returns to this body, and the difference is
load bearing rather than cosmetic. For `source`/`.` it merges into the current shell, so the test
asks both whether the file's raw content could continue a partial match begun OUTSIDE the file,
checked across every DFA entry state, and, separately, whether any `NAME=VALUE`-shaped line's
isolated value could, so a suffix fragment written as `Y=lattice` is caught the same way a
variable-borne one already is. For a file a CHILD shell runs, the raw half is dropped and only the
assignment half applies: a child's variables never return here, so the composition to detect is one
the file performs within itself, and ordinary script text answers the raw question yes
(`make build` ends in `d` and so advances the scan from the idle entry state), which refused a
mandatory certification row. Content that is already the whole marker still refuses on every route,
through the value the operand contributes to the sink path. A target this body never writes, or one
it writes with content that cannot advance the marker, stays outside the analysis's scope and
certifies, so the ordinary `echo "REGION=us-east-1" > env.sh; source env.sh` idiom is unaffected.

Without the exact-script-operand route, `printf '%s\n' 'A=doc-' '"${A}lattice" reconcile' > env.sh;
bash env.sh` certified while `source env.sh` and `bash e*.sh` refused the identical file. One class
stays open on the child-run routes as a consequence of the narrower content test and is recorded
rather than silently dropped: a file supplying part of the marker literally and the rest from an
EXPORTED parent variable composes across the one boundary a child does inherit, and neither the
guard nor the value route second-parses a script operand's content to see it. A `source`/`.` target
that resolves to a dynamic filename or to a process-substitution operand is untouched by this rule
and remains open; that gap, and true exact-literal replay for `source` matching `eval`'s, are
tracked in issue #133.

A tracked file can also compose out of text its CALLER supplies rather than text it contains, since
every word after the operand becomes the child's `$1`, `$2`, and so on, so
`printf '%s\n' '"$1$2" reconcile' > s.sh; bash s.sh doc- lattice` executes the marker with no
assignment anywhere in the file for the content test to find. Reparsing the file with its argv bound
is not available, because that parse reads a program as text and a script operand's content arrives
as a `ResourceRef` the eval layer folds into an opaque token (issue #159), so this fails closed on a
conjunction: the target names a resource this body writes, that file's content reads a
caller-supplied positional, and substituting the arguments into the file's own text composes the
marker. The third condition substitutes rather than asking whether an argument is marker-fragment
capable, which is the trap the content test documents above: `build` ends in `d` and so advances the
scan from the idle entry state, which would refuse `bash s.sh build`. Each reference is replaced by
a choice over the individual arguments together with their joined concatenation, because the true
binding is one member of that choice at every position and this analysis does not solve which. The
joined member is load bearing twice over: it keeps the rule monotone with the single-string model
that preceded it, so no refusal that model earned is lost, and it covers an argument word that
splits into several the child then concatenates.

Substituting one joined string at every reference, which is what this rule did when it was first
written, was recorded here as what made it sound without solving which argument lands where. That
was wrong in the under-refusal direction, and a file selecting a non-adjacent subset is the
counterexample: `printf '%s\n' '"$1$3" reconcile' > s.sh; bash s.sh doc- SAFE lattice` composes the
marker while the joined string `doc-SAFElattice` and its doubling do not contain it. The choice
formulation is the fix, and it stays linear rather than enumerating assignments of arguments to
references. The over-approximation the doubling provides is kept rather than traded away, so a file
reading its arguments in the other order, such as `"$2$1"`, still refuses.

This rule reaches every route the content test reaches, through one shared enumeration rather than
each route's own guard. Written into the exact script operand's guard alone, it left the `source`,
glob-operand, variable-operand, `--rcfile`/`--init-file`, and `BASH_ENV` spellings of the identical
read certifying (issue #175), which is the per-route guard shape recorded in issue #176. Whether
`$0` counts is the one thing that varies, and it varies by what binds it: a script operand's `$0` is
the script's own path and a `source` builtin leaves the caller's `$0` untouched, so both exclude it,
while a startup file read by a `bash -c` child sees the caller's first operand there and includes
it. One residual is disclosed rather than closed: on `source`, where the file's state merges back
into this shell, binding a marker FRAGMENT to a variable the parent then completes is enough, and
asking the whole-marker question of the substituted text does not see it (issue #177).

A shell `-c` payload does not enter the body-wide lowering either, because a child shell's
assignments genuinely do not persist into the parent. They do persist into the rest of that
payload, though, so the same extractor recovers them for the payload's own second pass alone,
where they join whatever the name held on entry rather than replacing it: the pass collapses the
payload to a single position, so a use that precedes its assignment must keep reading the inherited
value. Without that, `bash -c 'A=doc-; "$A"lattice reconcile'` certified while the `eval` spelling
of the same body refused. A payload whose own state this extractor cannot represent, such as one
containing an array assignment or a nested `eval`, fails closed, matching how the `eval` route
already treats both; that check runs only after the ordinary parse has declined, so it decides no
body the ordinary parse already refuses. A payload the tokenizer cannot accept is missing evidence
rather than
absent evidence, so it fails closed instead of leaving previously recovered state uninvalidated
(issue #134). A backslash-newline line continuation is removed before the metadata walk and the
`shlex` pass so the two stay in lockstep, matching Bash's own line-continuation handling, since that
form is common and benign enough to model rather than refuse by construction.

A shell `-c` payload also enters the parameter second pass, not only the state-effect replay. The
child expands the payload itself, so `export A B; bash -c '$A$B'` composes the marker although no
word of the parent command carries it. Those references resolve against this body's whole variable
table rather than the exported subset the child would inherit, which over-approximates and so stays
fail-closed; modeling export status precisely is future work. Every modeled dispatch spelling
reaches that pass, not only a shell that is the command's own head, so a launcher-spelled or
unresolved-head shell is second-parsed too; where the argv shape is uncertain the pass reads each
retained candidate word rather than committing to one, mirroring the choice the sink selector
builds over the same set. A script operand stays with the sink path, which already models a file's
content directly, plus the state guard above. The operands after the payload word are bound as the
child's positional parameters, so `bash -c '$0$1 reconcile' doc- lattice` composes the marker out of
words that are plain argv in the parent. Each operand is evaluated in the parent environment, which
is where those words actually expand. The same positionals reached from a shell function's call
site are not bound, because those arguments are applied by substitution over flow effects rather
than stored as variables; that gap is tracked in issue #160.

A standard-input program enters the same second pass, reached by its own route because the program
is a stream rather than an argv word. `bash -s -- doc- lattice` binds its trailing operands as
positionals too, and the starting offset differs by dispatch form: a `-c` payload's operands begin
at `$0`, while `-s` leaves `$0` as the shell's own name and begins at `$1`, both verified under real
Bash 5.2. Binding a `-s` list from `$0` would shift every parameter by one and miss the composition.
The pass runs whether or not operands accompany the program, because an exported parent variable is
visible to that child as well. It can only read a program it can see as text, so the heredoc and
herestring spellings refuse while a program arriving through a pipe or a redirection does not: the
eval-syntax walk folds `ResourceRef` and `StreamRef` into the same opaque token it uses for
`OutsideGap`, which is issue #159 reached by this route rather than by `eval`.

The stop-line is deliberate. This sub-analysis interprets exact literal payloads only, never
dynamic ones, and growth beyond the constructs listed above is out of scope: an interpreter chasing
`eval` has no natural terminating point, and this engine is defense in depth behind human review,
not the boundary that contains untrusted code. Values bearing arithmetic expansion are therefore
not modeled, and an array assignment fails closed rather than being interpreted. An `eval` nested
inside an `eval` payload is the bounded guard this stop-line prescribes rather than an unmodeled
gap: verification under real bash confirmed that a nested `eval` does persist its assignments to
the current shell, so the replay raises `shell nested eval state cannot be represented` instead of
leaving that state unrepresented (issue #114), without recursively interpreting the inner payload
or gating the guard on its content.

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
misses; open gaps against this decision are tracked in issues #125 through #141, and this
enumeration itself is not exhaustive. Two further gaps outside these categories: `lastpipe` state
reached through a function call site or a loop back edge is widened only for a pipeline whose last
stage is a `read` writing from stdin, not for every conditional-on-lastpipe context such as a
trailing `eval` (issue #118), and one descriptor bound to two output process substitutions keeps
only the last binding, so the earlier consumer Bash chains behind it, as in
`producer > >(first) > >(second)`, reads nothing (issue #187). A `read` beyond the first record of
a shared stream is projected as record one, so a marker split across later records is not yet seen;
that under-refusal is issue #121, and the `read -a/-d/-n/-N/-u` over-refusal it interacts with is
issue #119.
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
launcher takes the launcher's standard input as its payload, which is what `xargs bash -c` does. A
dynamic word between the head and that shell is skipped rather than ending the search, because
selecting some sink is always at least as conservative as selecting none. An `external_lookup` head
declines to select a nested shell only when the head is itself one of `builtin`, `command`, or
`exec`, so an external shadow of a wrapper builtin is not reinterpreted as a wrapper while an
ordinary external head such as `timeout` still selects the shell later in its own argv.

A `source`, `.`, or shell script operand whose value comes from a variable is matched against every
resource this body writes. Such an operand resolves to no static key, so the exact target guard and
the glob target guard both skipped it and `F=t.sh; source "$F"` certified a file the body itself
wrote the marker into.

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

### AD-19: A reported false certification is triaged, not automatically fixed

**Date:** 2026-07-27
**Status:** Accepted
**Context:** AD-18 states a stop-line for the eval sub-analysis, on the grounds that an interpreter
chasing `eval` has no natural terminating point and this engine is defense in depth behind human
review rather than the boundary that contains untrusted code. Review practice did not inherit that
reasoning. Each review round on PR #112 reported every false certification at the same severity and
each was fixed on arrival, so the branch absorbed four rounds in one day and the loop had no
terminating condition: certifying non-execution over Bash plus everything on `PATH` is undecidable
in general, and the next shelf up is already reachable. Verified under real Bash at f866617,
`python3 -c 'import os; os.system("doc" + "-lattice reconcile")'` certifies and executes the
marker, while the same body with the marker spelled literally refuses on AD-17's retained-word
rule. The difference is not a modeling gap in the shell analysis; the composition happens inside
another language's string semantics, and closing it means interpreting that language. Without a
stated disposition rule, a finding inside a boundary AD-18 already discloses is indistinguishable
from a defect in the implementation of that boundary.
**Decision:** A reported false certification is verified first and classified second, and only one
class is fixed on sight.

Verification is unchanged and non-negotiable: reproduce under real Bash with a `doc-lattice` shim on
`PATH` using the differential oracle in `scripts/fuzz_shell_taint.py`, and build a control that
isolates the claimed cause from the sink, the carrier, and the composition. An unreproduced report
is not triaged at all.

A model-integrity finding is a construct the analysis claims to handle that silently produces no
evidence: a lowering site that yields nothing where the model asserts something, a sink position
that registers no sink, a guard whose route recognizes a narrower set than the route it mirrors, or
a documented invariant the code does not hold. These are defects in this engine rather than
limitations of it, their number is bounded by the size of this codebase, and they are dangerous
precisely because they are silent. They are fixed, with over-refusal measured against the frozen
references rather than asserted.

A boundary-extension finding is a channel or subsystem the analysis never modeled: another
interpreter, a launcher's own input grammar, a shell startup channel, an expansion primitive with no
lowering. These are unbounded in number, and each fix widens the surface that must then be
maintained and re-measured. The default disposition is to file the issue with its verified repro and
control, pin the behavior as certifying in the test suite so it cannot regress unnoticed, and
disclose the class in AD-18. One is modeled only when the spelling is plausible in a benign workflow
or the guard collapses into an existing change point at measured-zero over-refusal cost.

Interpreters other than the shell are categorically out, by the same reasoning AD-18 applies to
`eval`. No finding of that shape is fixed, whatever its reported severity.

Two rules bind both classes. A finding that falsifies a claim in a decision record corrects that
record even when the code is left alone, because a decision record that overclaims is itself a
defect. A fix is never accepted on the strength of its own test alone: over-refusal is measured
against the frozen adversarial corpus and the seeded fuzz corpora named in AD-18, so the cost of
each widening is known rather than assumed.
**Consequences:** Review findings become classified inputs rather than obligations, and the review
loop terminates. The engine's disclosed boundary can grow deliberately instead of by whichever
channel a reviewer happened to probe. The cost is explicit: pinned certifying bodies are known
evasions that remain open, so the boundary AD-18 discloses is load bearing and this analysis stays
defense in depth behind human review of workflow changes, never the sole control.

### AD-20: A fail-closed guard is identified by its origin, and classified by executable evidence

**Date:** 2026-07-27
**Status:** Accepted
**Context:** The fail-closed guards in `shell_taint.py` and `shell_scanner.py` are the security
contract of the CI shell scanner, and most of them never executed in the test suite. The risk is
inverted: because both layers funnelled every refusal into a bare string, nothing proved a guard's
*condition* fires. An inverted or shadowed condition routes input down the certify path with the
whole suite still green. Reason strings could not close the gap either. Measured at `763f43d`, 55
distinct reasons covered 126 raise sites in the taint layer and 23 reasons were raised from more
than one site, 15 of those with mixed coverage, so an assertion on refusal text could not say which
site fired. Identity was discarded independently in four places: the `analyze_marker_taint` catch,
the scanner's re-wrap of the taint verdict, the brace-error chain through `_BuiltContent` and
`_ShellWord`, and the `_parse_word` `finish()` re-wrap. Separately, limits stopped short of the
guards that enforce them: default `TaintLimits()` signatures and silent fresh-default call paths
meant a shrunk full-pipeline budget never reached several bounds, so no cheap test could exercise
them.
**Decision:** Every fail-closed guard has one *origin*, the site that detects the condition, and
that origin constructs an immutable `GuardRefusal(origin_id, reason)`. Both arguments are direct
string literals. The reason text is normalized out of a fingerprint so wording can change without
churn, but an executable expression is rejected rather than normalized: evaluating
`compute_reason()` can fail before the refusal exists and silently withdraw the guard.

Identity is origin identity, never transport identity. Refusal exceptions accept only a direct
`GuardRefusal` or a declared refusal transport, never a guard-free verdict such as `Certified`;
handlers, deferred fields and result projections carry the same refusal object through.
`analyze_marker_taint` returns only a direct discriminated `Certified | MarkerDetected |
GuardRefusal` construction or a declared transport, and the scan boundary similarly returns a
`ShellScanResult`. Every return expression is validated, not only tuples and text, so a name,
concatenation or arbitrary call cannot replace construction of the inventoried verdict.
`ShellScanResult` stores that verdict as its one authoritative field with `incomplete_reason` and
`guard_id` derived from it, so operator text and guard identity cannot drift. `MarkerDetected`
stays guard-identity-free: it reports the analysis's conclusion about the script rather than a
bound that stopped the analysis. The `ConfigError` wording operators see is unchanged.

Guard identifiers are semantic and stable. They encode neither line numbers nor user-facing text,
so rewording a refusal message is not an identity change. One identifier names one origin: a second
site constructing an identifier that is already classified would inherit evidence it does not have,
so the inventory rejects it. Every rule that recognizes a construction recognizes it by name
whether it is spelled bare or through the module that defines it, and the names it answers to
include every alias bound to the constructor, whether that binding names it bare or
through the module that defines it, because an aliased construction in a verdict return is a
well-formed verdict that no carrier rule would reject. Those names are resolved across the guard
package rather than within one module. Resolution follows a binding whose value spells a name it
already holds, so what it finds depends on what it starts from, and starting from the canonical
spelling alone stopped at the module boundary: a guarded module binding `GuardFactory =
GuardRefusal` publishes a spelling that a consumer importing only `GuardFactory` names in no other
way, so the consumer was discovered by nothing and the fail-closed origin it constructed through
the alias was inventoried by nothing either. Each constructor family is therefore resolved to a
fixed point over every module in the package before any rule reads it. Closing the seed rather than
adding a rule is what closes both halves at once: every rule keeps reading names as it already did,
so a re-export reached by import, by attribute or as text is recognized exactly as its canonical
spelling is, and an alias of an alias needs no case of its own. A binding whose value the module
computes publishes no alias, and needs to publish none, since the reflective lookup that computes
it is rejected on sight where it is spelled. The shipped package publishes no re-export, so the
closure equals the canonical set and the widening moves no record. A binding is followed whichever form it
takes: `GR, E = GuardRefusal, _TaintLimitExceeded` is one statement naming both the refusal
constructor and its transport, and `def helper(factory=TaintLimits)` binds a constructor for every
call that omits the argument. Reading only the single-value assignment form let the destructured
spelling escape every rule at once, taking with it the record, the shape violation and the discovery
of the module holding it, while the defaulted form silently minted production-scale limits below the
public boundary. A starred target collects a list rather than one of the values and names no
constructor.

A call in a guarded module must name its target as a bare name or attribute. A target computed in
call position, such as `globals()["Guard" + "Refusal"](...)`, can invoke the refusal constructor
without spelling a constructor reference any rule can inspect and is rejected by the shape gate.
This is deliberately a syntactic boundary, not a claim to prove arbitrary callable provenance: it
does not trace a computed value first assigned to a plain alias. A future need for a computed
callee must be modeled explicitly, and no guarded module carries one today.

Requiring a named callee settled what a call spells, not which name inside it is the target. The
callee's last component is what every construction rule resolves, which is what lets
`shell_guards.GuardRefusal(...)` read as the construction it is, so a constructor spelled earlier in
the same chain is not the call's target at all. `GuardRefusal.__call__("taint.new", "reason")` mints
a real refusal while every rule reads a call to `__call__`: the origin extractor records nothing,
the shape gate sees no refusal, and the reference rule accepted it because a named callee was
followable in full, including the constructor buried inside it. Handed to a declared transport it
added a fail-closed guard that no record describes, with the candidate gate and the base-owned
comparison both at exit 0, and it needs no alias, no import and no reflective lookup to spell. So
only the callee itself is followable, and a constructor in a non-final position of it is rejected.
The neighbouring routes were already closed, `getattr(GuardRefusal, "__call__")(...)` as a computed
target and `GuardRefusal.__new__(GuardRefusal, ...)` as a constructor named in an argument, which is
what makes this the last spelling that reached a constructor without naming it as one.

A `type` statement binds a name the same way. The `TypeAliasType` it binds is not callable, so
`type Alias = GuardRefusal` is not a constructor binding as an assignment is, but `Alias.__value__`
hands the constructor straight back and reached it through a name no rule tracked. That alias is
therefore registered like any other, which puts the unwrapping dunder in a non-final position and
rejects it by the same rule rather than by a case of its own. Only a value read as a bare reference
registers, so the verdict union `type ScanVerdict = Certified | MarkerDetected | GuardRefusal` binds
no constructor spelling and stays the ordinary type alias it is. Neither rule carries an allowance:
across the three guarded modules, no call spells a tracked constructor in a non-final callee
position, and the shipped tree's 194 fingerprints stay byte-identical.

Closing the computed callee left the same construction spelled across two named calls, which is why
a guarded module resolves no name at runtime at all. `factory = getattr(shell_guards,
"GuardRefusal")` followed by `factory(...)` names both callees plainly, so the call boundary accepts
it, and it names the constructor only by a string, so no alias is registered and the refusal it
mints is classified by nothing. Following the lookup's result would mean tracking a value the module
never names, since the string is free to be computed exactly as `"Guard" + "Refusal"` is. So the
lookup itself is rejected: every reference to a name whose purpose is resolving another name is a
violation, whether it is called, aliased or passed on. The family covers attribute and namespace
lookup, source evaluation, import by name and the attributes that hand back a namespace or walk the
class graph to one. It excludes `compile`, which constructs nothing without `eval` or `exec`,
`hasattr`, which yields a bool, and `super`, which resolves along declared bases and is how the
refusal transports call their base initializer. No shipped guarded module names any of the family, so
the rule carries no allowance; a future need for one must be modeled rather than allowed. The
boundary is again syntactic: it rejects the reflective spellings a guarded module could carry, and
does not claim to enumerate every route to a namespace that a deserializer or a foreign-function
call could take. The family covers the descriptor spelling of a redirection as well as the builtin,
since `type.__setattr__(_EvalDiscoveryBudget, "charge_work", stub)` replaces the method holding a
frozen guard exactly as `setattr` does while spelling neither builtin.

Rejecting those spellings left the plainest one open, so a write that replaces a definition is
rejected whether or not it resolves a name at runtime. `_EvalDiscoveryBudget.charge_work = stub`
withdraws the same guard while resolving every name statically, and all three spellings passed every
gate with the shipped tree's 194 fingerprints byte-identical, because a record describes the source
of the definition it was extracted from rather than what that definition's name holds when a caller
reaches it. Following the write instead would mean deciding what the replacement computes, which is
the value-provenance problem the computed-callee boundary already declines. The base is resolved
through the alias closure, so a definition renamed by an assignment is the same target, and an
attribute chain is read at its root.

That rule read the base and so reached only a replacement spelled through a name, which left the
whole class open one level up: rather than replace a definition's attribute, replace the definition.
`_static_eval_descriptor = lambda digits: 0` withdraws a module-level guard outright, and 9 of the
29 module-level functions holding frozen guards, holding 13 of them, rebind this way with every
record in both guarded modules byte-identical and both gates green. A second `def` of the name does
the same while the first definition, which the record was extracted from, stays exactly as it was.
And `sys.modules[__name__].f = stub` is the attribute write again with the base unresolvable by
construction. All three were measured against the shipped tree, and the second and third passed the
base-owned comparison at exit 0.

So a name a definition binds is written nowhere else. The bare-name half is read per scope, and that
resolution is exact rather than an approximation, because Python decides a bare name's binding scope
statically: an assignment inside a function makes the name local and leaves an enclosing definition
alone. Read across the module it would instead report 73 ordinary locals in `shell_taint.py` that
shadow the name of some unrelated nested helper. Every binding form counts, since each replaces what
a later call reaches: an assignment or deletion, an import alias, an `except` clause, a match
capture, a parameter, and a second `def` or `class` of a name already defined. A `global` or
`nonlocal` declaration naming a definition is rejected on sight, since it is what would carry a
rebinding out of the scope that otherwise contains it; the five the guarded modules declare name
ordinary closure variables. The attribute half now reads the attribute name as well as the base,
which is the only thing left when a receiver is unresolvable by construction. Every one of these
carries no allowance: the guarded modules spell no collision in any scope, no declaration naming a
definition, and 165 attribute writes of which none names a definition. `self.work += amount` remains
ordinary state on a parameter, and stays outside the rule because `work` is not a definition's name.

Every one of those rules governs where a constructor may be *named*, and the exemption for naming
one as a type was reasoned about as a property of the position rather than of the spelling. A type
position accepts the whole expression grammar, so `def _helper(value: _stash(GuardRefusal))` hands
the constructor to `_stash` and `except _stash(GuardRefusal):` runs the same call every time the
handler is tested, exactly as `type X = {"g": GuardRefusal}["g"]` hands it to a subscript. Only the
`type` statement was reduced that way, so the constructor could be captured at a site no rule read,
minted through the plain alias the computed-callee boundary declines to trace, and delivered at a
declared transport: hosted in `_validate_acyclic_graph`, that added a fail-closed guard no record
describes with the candidate gate at exit 0 and the base-owned comparison reporting only two
unrelated fingerprints the edit incidentally moved. So every declaration is reduced by the same
rule, and a constructor spelled beside a hiding form is rejected rather than the hiding subtree
alone, on the deny-by-default reading the provenance rules already carry.

What an exempt declaration leaves behind is then the second half, because a module that evaluates
its annotations stores the class itself: `_ShellWord.__annotations__["brace_expansion_error"]` hands
back `GuardRefusal | None` off an annotation the scanner has every reason to spell. Enumerating the
surfaces that read one back does not close that. `__annotations__`, `__annotate__`, `get_type_hints`
and `get_annotations` join the reflective family, but `dataclasses.fields(_ShellWord)[0].type` and
`inspect.signature(_helper).parameters["refusal"].annotation` reach the same object through names
whose subject is a definition rather than its annotations, and `fields` is a name `shell_taint.py`
already binds for an unrelated local. So the rule is about what a guarded module stores rather than
about who reads it: every guarded module defers its annotations, which leaves all of those surfaces
handing back the annotation's source text, and turning text into the object it names requires
`eval`, `exec` or the evaluators above, each already rejected. The one module that evaluated them,
`shell_scanner.py`, now defers them with no record moved. Neither half carries an allowance: across
the three guarded modules no declaration names a tracked constructor inside a hiding form, and none
names an annotation reader.

The base-owned closure and comparison derive
the set of guard-protocol modules recursively from the candidate guard package rather than from the
base revision's hand-maintained list. Discovery over-approximates: a module belongs to the guarded
surface when it *mentions* a refusal, transport or result constructor anywhere, in any position,
resolvable or not, or imports one, or defines a verdict-producing function. A whole string literal
equal to one of those names is a mention too, because `getattr(sg, "GuardRefusal")(...)` is the same
construction one obfuscation deeper and spells no name node at all. A malformed origin
hidden behind an indirect call therefore still brings its module into shape validation.
Matching text is still matching a spelling the author controls, and `getattr(sg, "Guard" +
"Refusal")` holds the name in no single node, so a module is also swept in when it *imports the
module defining the protocol*, by either half of the import grammar and by relative spellings.
Reaching the constructor requires importing what holds it, and unlike a name an import target
cannot be computed, so recognizing it carries the whole computed-name family rather than one
obfuscation at a time. This import is the one route that must be recognized; sweeping in every
importer of a sibling module would put the entire package on the guarded surface. A candidate
can add a guarded module, classify its origins and add it to its own allowlist without being
rejected as stale by an older base checker. The candidate-owned coverage rule still compares that
discovered set with `GUARDED_MODULES`, because limits and threshold rules use that allowlist. Shape
validation reads the union of the discovered surface and the allowlist directly, and recursive
discovery treats a module one directory down exactly like one beside it. Over-approximating sweeps
in the module that defines the protocol, which names its own refusal constructor in the verdict
alias: it sits in the allowlist, contributes no origin record, and declares its `ScanLimits` field
defaults as a boundary, since the layer defaults are declared where the limits classes are.

One immutable `ScanLimits` is constructed at the public boundary and threaded through the scanner,
content construction and the taint pass. `_ScanBudget` owns it and derives its counters from it, so
the children that already share the budget share exactly one limits value. `limits` is required on
every internal limit-aware helper. A limits construction is recognized through import aliases and
module rebindings as well as its canonical spelling, including a constructor reference passed to a
dataclass `field(default_factory=...)`; either form creates fresh production limits at runtime.
Boundary exemptions name exact scopes. A nested helper inside `analyze_marker_taint` and a method of
`_ScanBudget` are internal consumers, not descendants that inherit authority to mint fresh limits.
Every threshold a guard references is either a field of that value or an inventoried fixed semantic
bound with a recorded rationale, the latter reserved for quantities that are directly authorable
rather than resource budgets. A threshold is recognized structurally rather than by naming
convention: any numeric constant a guard references, whether the module binds it, its function
assigns it, or a positional-only, positional or keyword-only parameter defaults to it, and any bare
numeric magnitude it compares against, arithmetic in the operand included, so
`depth - 4096 > 0` is the same uninventoried cap as `depth > 4096`. Arithmetic counts on the
binding side too: `_MAX_ITEMS = 50 * 2` caps a guard exactly as the bare literal does, and
recognizing only bare literals let the computed spelling escape both halves at once, since a
module-bound name is also exempt from the naming-convention check. A local assignment or parameter
default is covered for the same reason, having neither a module binding nor a convention to fall
back on: one made entirely from numeric arithmetic fixes a magnitude, and so does one that scales a
runtime value by a literal, since `cap = 512 * factor` bounds the scan 512-fold however `factor` is
derived. A dynamic cursor such as `index = start + 2` displaces a position rather than fixing a
magnitude and is not a local cap. A binding that holds its magnitude somewhere other than the whole
expression is read branch by branch and element by element, since the later comparison spells only
the name: each arm of `cap = 100 if strict else 200` fixes the bound whenever it is the arm taken,
and `CAPS = (1, 100)` caps the scan at 100 whichever element a subscript reads. On the comparison
side the same two forms are descended, along with the value of a subscript but never its slice, so
`(100, 200)[flag]` is the cap it is while `words[2]` stays a position in a fixed grammar. A name already resolved to a magnitude stays a threshold wherever
it is spelled, including as the receiver of an accessor, while a callee itself is machinery.
Every rule above reads a magnitude out of numeric literals and the arithmetic over them, so a cap
fixed at authoring time by way of a *nonnumeric* literal was invisible to all of them: `cap =
int("100")` and `cap = ord("d")` each bound `len(items) > cap` at 100 while carrying no numeric
literal for either the binding rule or the bare-literal rule to find. An ordering comparison's
operand is therefore rejected when its value is fixed when written but cannot be read as a number,
whether the operand spells it directly, a module or local binding does, a parameter default does, a
chain of plain aliases does, or a class field the receiver closure reaches does. A value is fixed
when written if every leaf of the expression is a literal, a call included, since `int("100")` and
`"abcdef".index("f")` evaluate to the same number on every run. A comprehension and a lambda are
each fixed on those same terms once the names they bind themselves are counted as accounted for:
reading a comprehension's iteration target or a lambda's parameter as a free name made every such
form unfixable, so `cap = len([None for _ in "abcd"])` fixed a cap of 4 and was classified as
runtime data on the strength of the `_` it binds itself. The binding is scoped to the body it
serves, never to the whole expression, since a comprehension's outermost iterable and a lambda's
parameter defaults are evaluated where the expression is written: `[x for x in "ab"]` is fixed while
`[x for x in x]` reads the enclosing `x`, and a name bound by one comprehension does not vouch for a
free name beside it. A callee spelled inline rather than named, as an immediately-invoked lambda is,
is classified like any other operand instead of ending the search. Such an operand is rejected rather
than resolved: resolving it would mean folding an open set of conversions, and every conversion left
out is another spelling that ships uninventoried, so a cap is spelled plainly or taken from the
scan's limits. Only ordering comparisons are read, for the reason given below for imported bounds,
and only the expressions fixing the operand's own spelling, never the whole writer closure, since that
closure holds the string and container bindings deciding identity rather than magnitude and reading
those as caps reported `state = "body"` as an unprovenanced bound. No shipped guard compares such a
value, so the rule carries no allowance.
The search covers the writers feeding a guard's condition as well as the
condition itself, and the preceding controls that decide whether the origin is reached plus their
writers—the same closures the fingerprint records. A comparison computed one statement
earlier caps the scan exactly as an inline one does: `too_many = len(items) > 100` followed by
`if too_many` leaves
the magnitude nowhere the condition can see it; likewise, `if len(items) <= 100: return` makes that
fixed cap decide whether an unconditional origin is reached. Zero and one are exempt because they
spell emptiness and arity rather than a magnitude, which is also what keeps a counter seeded at zero
from reading as a threshold, and a subscript index is a position rather than a magnitude.
A comparison is recognized by what it does rather than by its syntax. `operator.gt(count, 100)` and
`count.__gt__(100)` bound a resource exactly as `count > 100` does, and reading only `ast.Compare`
nodes left both the literal and any imported bound in them reachable from no comparison at all. A
call to any of the six comparison names is read as the comparison it spells, whichever module it is
imported from, with the receiver supplying the left operand in the method form. A call whose
arguments cannot be resolved to that pair, because it is starred, keyword-bearing or of another
arity, is not read as one.
The same holds for a call that performs the ordering and leaves only an identity question behind.
`max(len(items), cap) != cap` refuses above `cap`, and so do `min(len(items), cap) == cap` and
`len(items) not in range(cap)`, while the operator each one ends in is an equality or a membership
test. Reading operators alone therefore let a cap ship past every threshold rule at once: the
literal rule reads comparison operands and `range(100)` is a call rather than an operand, and the
imported-bound and opaque-magnitude rules read ordering operators that these spellings never write.
An extremum call over two or more operands, or over a literal container of them, is read as the
ordering it performs, and a membership test whose container is a range is read as the bound that
range spells, through any sequence rebuild wrapped around it since none of those changes which
values it holds. Only the positional arguments name the values these callees order, so a starred
argument and a lone non-literal argument leave them unresolvable while a keyword does not: `key`,
`default` and `reverse` change how the ordering runs or which end of it is returned, never which
operands it is over, and a keyword can introduce no positional argument at all. This is where the
rule parts from the spelled comparison above, whose callees accept no keyword, so declining to read
one there costs nothing. Declining to read one here let `max(len(items), cap, key=lambda v: v) !=
cap` ship the same cap the call without the keyword was rejected for.
A call ordering runtime data spells no bound, so `len(items) in range(len(other))` and
`len(items) > max(limits.max_items, limits.max_words)` stay clean. No shipped guard encodes a bound
this way, so the widening carries no allowance.
The line stops at comparison. A guard that caps by *truncating* its data rather than by ordering a
magnitude, as `list(items[:100]) != list(items)` does, is not read as a threshold, because a slice
bound is a grammar offset far more often than a budget: the shipped guarded modules spell 61
authoring-fixed slice bounds, every one of them a lookahead of three characters or fewer, and seven
of those sit outside the zero-and-one exemption. Reading slice bounds as caps would therefore report
seven of the scanner's own grammar offsets as unprovenanced bounds while the same rule caught no
budget, so that family needs evidence separating an offset from a budget before it can be gated
rather than a widening of this rule. That gap is tracked in issue #183.
Module and function-local imports participate structurally too. A guard-visible imported value on
the value side of an *ordering* comparison is a named threshold whether imported directly, aliased,
module-qualified, forwarded through an assignment, or bound as the default of a parameter the
comparison reads, since a default forwards a value into the guard exactly as an assignment does. Its guard-visible spelling must be inventoried
as a fixed semantic bound or it is rejected. Equality and membership ask which value something is
rather than how much of it there is, so an imported sentinel, enum member or frozenset compared
that way is not a threshold and no fixed-bound rationale is invented for it. The comparisons read
are the condition, the reachability controls and the values their writers forward into them: a
writer is in the closure because the guard reads what it binds, not because every comparison it
spells decides the guard. Imports outside the relevant comparison dataflow do not count. An import
used solely as a callable, callback or other machinery in the measured-side dependency graph is not
a threshold. The opposite operand is treated as measured data only when the approved operand
resolves to the scan's limits by binding, through a limits-annotated or required `limits` parameter,
a limits construction, or a binding of one of those; an unrelated object that happens to carry a
`limits` attribute approves nothing.

Classification is executable data, not prose. A guard is classified only by carrying evidence: a
reachable guard carries an authored script that drives the public scan path and returns that exact
identifier; an unreachable one carries a written rationale, a boundary script that drives the same
validation to its nearest reachable state, and a required predicate over the evidence that script
builds. A row asserting that a test exists somewhere is not classification, and neither is a
boundary row whose evidence predicate is satisfied by any input at all, which is why that predicate
has no default and why it is additionally required to be false for the evidence an empty script
builds. Having no default only stops the predicate being omitted; a predicate that holds for the
builder's floor of one root scope and nothing else reports nothing the boundary script constructed,
and is vacuous whether it is spelled as a constant or as a test the floor happens to satisfy.

Discriminating against that floor is still not enough, because it says nothing about *which* data the
predicate discriminates on. A predicate must additionally read something the guard's own condition
reads, and which attributes those are is derived from the guard rather than asserted by the row.
`taint.evidence.unknown-output-node` is the measured case: it falls through the arms of an exhaustive
walk, so `boundary_script="echo hi"` with `boundary_evidence=lambda evidence: bool(evidence.commands)`
satisfies every other assertion the row can be held to, stopping short of the guard, building a
command where the empty control builds none, evaluating the condition line and not reaching the
refusal, while saying nothing whatever about an unhandled output node. The derived set is what decides
the refusal: the origin statement, the tests and enclosing loop iterables governing it, the writer
closure of what those read, and a handler guard's own `try` body. Reading the value a transported
refusal hands down is what recovers the structure behind it, which is how the parent-cycle guard
resolves to `parent_scope_id` rather than to nothing. Only for a guard reached purely by falling
through earlier arms do the preceding controls stand in, because they include everything every earlier
guard in the same function inspected, and using them first would let a predicate borrow relevance from
an unrelated neighbour. This is a floor and not a proof: a predicate can still be weak about the right
data. What it rules out is a predicate about the wrong data. A guard whose condition reads no
attribute at all, or only a limits field, cannot be witnessed this way and the rule says so rather
than accepting the first predicate offered; a resource bound belongs to a reachable witness under
shrunk limits.

Being a rule over inert source, that floor also counts a read the predicate never performs:
`bool(e.commands) if True else any(s.parent_scope_id for s in e.scopes)` mentions the parent-scope
guard's leaf in a branch that never runs, while the half that executes only tests commands. Pruning
dead branches statically would be another round-per-spelling series, so the suite executes every
predicate against its own boundary evidence under a recording wrapper and holds the reads that
actually ran to the same derived set, because dead code contributes nothing at runtime however it is
spelled.

A boundary row is additionally held to executing the guard's own governing construct while not
reaching its refusal, and that construct is whichever one actually decides the guard: an `if` or
`while` test, a `for` header, a `match` arm, or, for a guard in an `except` handler, the operation
in the `try` body whose failure is the only condition it has. Recognizing only `if` and otherwise
falling back to the enclosing function's first executable line would name a line that runs for any
script entering the function, so the trace assertion would hold without the guard's own condition
ever being evaluated. Three frozen origins sit in `except` handlers today, where the handler's own
line is unsatisfiable as a witness because entering the handler is what runs the refusal.

A refusal handed to a declared transport is witnessed in the transport, on both lines. Its
construction is an argument, so reaching the call runs it whether or not the transport goes on to
raise: taking that line as the refusal line makes the assertion that a boundary does not reach it
unsatisfiable, which would have left the two cycle guards unclassifiable as invariants while looking
like an ordinary failure to find a boundary script. The transport's own test is the condition, and its
`raise` is the refusal, which is the same code the record already folds in for the reason given
above.

Anything unclassified is frozen rollout debt that may only shrink. Source origins must partition
exactly into the classification registry and the frozen debt snapshot, and debt is frozen as
canonical origin *records* rather than bare identifiers, so an unclassified guard cannot be moved
or semantically edited while keeping its entry. A record covers the guarding condition, whether it
is spelled as an `if`, a `while` or a `match` arm, and, within the enclosing function's own
execution, the statements that write what that condition reads, in-place mutation of an
accumulator and configuration of an object it reads included,
because inverting or removing an accumulation disables a guard as completely as inverting its test.
An accumulation the statement itself drives counts wherever it is spelled, including in the body of
a generator that statement consumes; only a genuinely deferred body, such as a generator merely
bound to a name or an uncalled lambda's, stays outside.
That selection is transitive: a condition usually reads a name two or more assignments away from
the input deciding it, and stopping at the direct writer leaves everything behind it uncovered.
`scanner.env-option.static-split-string` tests a `kind` written from an `option` resolved one call
earlier, so a one-hop rule lets that call become a constant, withdrawing the guard, with the record
byte-identical.
An enclosing `for` governs a guard in its body as completely as a test does while exposing no test
of its own, so a record also covers every enclosing loop's header and the closure of what writes
its iterable. `taint.eval-payload.metadata-exhausted` is the concrete case: it sits under an `if`
inside `for lexeme in tokens`, and emptying `tokens` makes it unreachable while leaving its
condition, and everything that condition reads, untouched. A `while` needs no such treatment
because its test is already a guarding test.
A record also covers the body of any declared transport the origin hands its refusal to, since the
parameterized cycle detector owns the condition that decides its callers' refusals while minting no
identifier of its own.

A condition says what a guard refuses once control arrives and nothing about whether control
arrives, so every record covers in addition the control flow deciding the origin is reached at all:
each branch test, loop header and diverting statement in its function that can execute before it,
plus the transitive writer closure of every value those controls read. Diverting execution around a
guard withdraws it as completely as inverting its condition, whether the edit changes the control
syntax itself or a statement that produces a value the control reads. The case for a guard that has
a test is `scanner.env-option.static-split-string`: it sits behind an earlier
`if not literal.startswith("--")` that returns, and dropping the `not` sends every long option down
the short-option path so the guard can never fire. `scanner.descriptor.unparsable` is the case for
one that has none: it fires only because the `return int(digits)` in its own `try` body can raise,
and rewriting that to `return 0` withdraws it. Both survived byte-identically before this flow
entered the record.

Only what can run before the origin is taken. Lexical order settles that everywhere except inside a
loop, where a statement after the origin runs again ahead of the next iteration, so an enclosing
loop's whole body is taken back. A statement that can only run later cannot decide whether the
origin was reached, and excluding it is what keeps a subsequent edit in the same function from
churning the record. For the same reason a diverting statement is taken whole, because the value it
returns decides reachability, while a branch is reduced to its test so an edit inside one branch's
body does not churn a record outside it.

A `with` is one of those controls, reduced to its items on the same terms a `try` is reduced to its
handlers. The context manager decides what becomes of an exception raised in the body, so
`with suppress(Exception):` wrapped around an origin swallows the refusal it raises exactly as a
bare handler would. Recognizing every compound statement except this one left that spelling
withdrawing `taint.eval-discovery.work-limit` with all 194 fingerprints in the tree byte-identical
and every other gate silent. The item is taken whole rather than only its context expression, so a
name rebound through `as` moves the record too, and the statement reaches the callee closure like
any other control, so `with quiet():` is not withdrawable by editing what `quiet` returns while its
header stands. The guarded modules spell no `with` statement, so the rule moves no record.

A guard reached through an `except` handler needs more than that flow, because the handled
exception types are all a `try` contributes to it: what decides whether the handler runs is the
operation in the `try` body and the object state that operation reads. Every handler origin records
that body and the function-local and referenced-module closure of what writes what the body reads,
even when the refusal is nested beneath an additional test inside the handler.
`taint.eval-payload.lex-error` is the concrete case: reconfiguring the `shlex` lexer so an
unterminated quote tokenizes cleanly, or rewriting the tokenizing call to one that cannot raise,
each withdraws the guard, and both left every fingerprint in the tree unchanged. Object
configuration is what closes the first: an attribute write is a write of its receiver, exactly as a
subscript write is, but only where the closure reads that receiver as a value in its own right.
Matching it against every spelling read instead would fold every unrelated `self.other = ...` into
the record of any guard whose condition mentions `self.anything`, and churn is what forces the
regeneration this decision exists to prevent.

The executable scope stops at the function deliberately, and at any function or class body nested
inside it, but the writer fixpoint also follows the specific module-scope assignments, rebindings
and imports that the condition, reachability controls or guarded operation actually read. Their
transitive module dependencies are part of the record because changing a referenced cap or imported
binding changes the guard without changing its function. Python lexical binding rules remain the
boundary: a parameter, assignment, import, exception-handler name or match capture shadows a module
spelling throughout its function, so an unrelated module binding with the same name is not
selected. Lambda parameters and comprehension targets shadow only within their expression scopes;
free names in those expressions still reach the referenced module binding. Unrelated module
bindings, whole bodies of other functions, and nested function and class bodies remain outside the
record.

The class a guard's method is written in is not one of those outside scopes, and stopping at the
function and the module skipped it entirely. An attribute read off the receiver is fixed by the
class body declaring it and by every write the class makes to it, and a class body appears in
neither scope the two fixpoints walk: it is a scope, so the module fixpoint does not descend into
it, and it is not the method's body. `taint.eval-discovery.work-limit` is the concrete case, where
`work: int = 0` in `_EvalDiscoveryBudget` seeds the counter the condition tests, and reseeding it to
`-100` delays the guard by 100 charges with every fingerprint in the tree unchanged.
`scanner.budget.step-limit` is the same defect one scope over, its counter seeded by
`_ScanBudget.__post_init__` rather than by the field default, so a record covers the enclosing class
body and the guard's sibling methods as well. Each of those scopes runs its own fixpoint, seeded
with the attribute spelled the way that scope spells it, and a sibling's receiver name is read from
its own signature rather than assumed. What those fixpoints read that no scope of theirs binds seeds
the module fixpoint, so a field defaulted to a module constant reaches that constant. Only the
attributes the guard's dataflow reads are followed, never the receiver itself, for the reason object
configuration is matched that way: folding in every sibling's write to an unrelated attribute would
churn the record of every guard in the class on any edit inside it. Measured across both modules the
widened closure moves 35 of the 194 records and leaves an unrelated edit to a scanner method, a new
field or a new constructor attribute moving none. The boundary is the class the method is written
in: a field inherited from a base class, one a caller assigns onto the instance from outside, and a
write reached through a second name aliasing the same instance are all outside it, and no shipped
guard reads one.

A writer hashes the spelling of a call and nothing the call computes, so a guard whose dataflow
reads a callee's return value is withdrawn by editing that callee with the whole closure left
byte-identical. `scanner.env-option.static-split-string` is the concrete case: the `kind` it tests
comes from an `option` that `_resolve_env_long_option` returns, and rewriting that return to a
constant leaves every fingerprint in the tree unchanged and the base-owned comparison green. A
record therefore also covers every module-level function whose return value the condition, the
reachability controls, the guarded operation, or the parameter defaults feeding any of them read,
followed transitively through those callees' own returns.

What it covers of a callee is what decides its return value, not its body: each value-bearing
`return`, the closure of what writes what that return reads, the defaults that dataflow reads, and
the control flow deciding that return is the one reached. Everything else in the callee is
machinery, because a statement no return reads changes no caller's value. Rewording a refusal
inside a callee therefore moves no record, while editing what a return reads, or the flow selecting
which return runs, moves one.

That bound is on which edits churn a record, not on how many records a helper is tied to, and this
decision raises the coupling deliberately. Fifty origins read what `choice` returns, so editing what
it returns now moves seventeen of the sixty-five frozen records at once, which is the signal the
gate owes its reader rather than a defect in it. Measured across both guarded modules, 299 callees
are reached and 89 percent of their statements are return-deciding, so the exclusion is narrow by
volume and precise by kind: it buys correctness against unrelated edits, not a smaller surface.

Only a bare-name call is followed: an attribute call names a member of a value the parse cannot
resolve. Which definition a bare name reaches is decided by Python's lexical chain, so a lexically
nested helper is followed on the same footing as a module-level one, and any other binding of the
name, such as a parameter or an assignment, shadows both exactly as it shadows a read spelling.
`_contextualize_evidence` is why this matters: it reaches its guards through fifty nested helpers,
and returning an empty tuple from the nested `positional_call_arguments` withdraws every guard that
inspects the arguments it yields, three of them frozen, while leaving every fingerprint in the module
byte-identical. Callee blocks are ordered by qualified name rather than bare name, since two nested
helpers can share one, and by name rather than position so moving a helper within its scope moves no
record. A value the caller passes down rather than reads back stays outside for want of a resolvable
target, as it does for the writer fixpoint.

A statement is not the only thing that binds a name a guard reads. A parameter default binds one
too, for every call that omits the argument, and the signature carrying it sits outside the body
that fixpoint walks, so a record also covers the defaults bound to the parameters its dataflow
reads. `taint.eval-discovery.work-limit` is the concrete case: `charge_work(self, amount: int = 1)`
accumulates `amount` into the work its condition tests, and defaulting it to `0` stops every
zero-argument caller charging anything, withdrawing the guard with the whole closure unchanged. The
same edit to `analyze_marker_taint`'s default `TaintLimits()` widens the budget behind thirteen
origins at once. Only the defaults the dataflow reads are taken, so an unrelated one in the same
signature churns nothing. What a default reads seeds the module fixpoint rather than the local one,
because a default is evaluated in the defining scope: a same-named local inside the body shadows it
nowhere, and the module binding behind it stays in the record. A selected import contributes its source module and the aliases that
closure actually reads, not the whole statement, so adding an unrelated name to a shared
`from ... import (...)` line moves no record. An unbounded closure over those would churn every
frozen record on unrelated edits; a record that churns constantly has to be regenerated, which is
the laundering path this decision closes. A caller passing a different value into that function is
outside the boundary and is not covered.

Every closure above describes the guard's own function, and none of it moves when the call that
reaches that function is withdrawn. `scanner.control-flow.unfinished-case` is the concrete case:
`_finish_case` holds it and has exactly one call site, and replacing that call with `return`
withdraws the guard with its fingerprint byte-identical and the base-owned comparison green. A
record therefore also covers the controls at every resolvable call site of its own function: the
caller's qualified name, the condition the call sits under, the call statement's shape, and the
flow diverting around it, which is the same treatment the origin statement gets one level down.
Inverting `if frame is not None` at the call site, deleting the call, or returning ahead of it all
move the record. A function with no resolvable call site records that fact rather than nothing, so
acquiring one moves the record too.

That closure is one level deep by construction. Following callers transitively would pull the
reachability closure of the public entry point into every record in both modules, since all paths
converge there, and a record that churns on an unrelated edit has to be regenerated, which is the
laundering path this decision closes. Withdrawal further up the chain is covered instead as a
separate property that needs no digest and so costs a frozen record no churn at all: every origin
must sit in a function some public entry point of its own module can reach. Orphaning a function
at any depth is reported there.

The two resolutions differ deliberately, because a wrong edge costs opposite things on each side.
The reachability graph resolves an edge by callee name alone, across every definition carrying that
name, and reads a construction as an edge to the `__init__` and `__post_init__` it runs, which is
how the `__post_init__` holding
`taint.eval-syntax.cleared-projection-without-widening` is reached at all. Over-approximating there
only ever withholds a report. A fingerprint cannot afford that, so a call site resolves only when
the definition is unambiguous: a bare name to a module-level or lexically enclosing function, and an
attribute to a method either through `self` inside its own class or through any receiver when
exactly one function in the module carries the name. That last form is what ties
`taint.eval-discovery.work-limit` to the `budget.charge_work(...)` that reaches it.

A receiver this parse cannot resolve, spelling a name two definitions share, is not guessed at, but
neither is it dropped: it is recorded as a call that might reach the definition, in its own block.
Dropping it certified a withdrawal. With a frozen guard `A.check`, a benign `B.check` and an entry
point calling both, deleting only `a.check()` left the record byte-identical while the surviving
`b.check()` kept the name-resolved reachability graph reporting `A.check` as reached, so both
base-owned checks accepted the removal. The cost is coupling to a call the guard does not reach, and
it is bounded by the collision: it applies only where a module gives one name to more than one
definition, and renaming either removes it. `_ScanBudget.step` is the only origin-holding function in
the tree with such a collision, and it is classified rather than frozen, so this coverage costs the
frozen set no churn at all. Over-approximating in the record is the fail-closed direction here, where
under-approximating certified a withdrawn guard.

Entry points are derived from the candidate tree, as the public module-level functions of a guarded
module, rather than read from an allowlist. An allowlist in the base revision's copy would describe
the base's source and would reject a legitimate rename with no fix available inside the same change,
which is the same reason closure names none. Making a withdrawn guard's function public to satisfy
the rule is not a way through, because the rename moves the record's qualified name and the
base-relative comparison reports that as new debt.

Every shape named so far describes what the guard's function does once it is entered, which left
open whether that function is what a caller reaches at all. A decorator decides exactly that: it
runs before any caller, and it may return something other than the function it wraps. Adding
`@functools.cache`, a bare `@noop` returning a stub, a called `@noop(1)` or an unresolvable
`@mod.attr` each withdrew a guard while its origin, condition, writers, callee closure and caller
graph stayed byte-identical. A record therefore covers the decorators of the guard's own definition
and of every definition holding it, since a decorator on the enclosing class or outer function
replaces the guard just as completely. Both halves of a decorator are recorded, for the reason
calls are: the spelling alone leaves `@noop` withdrawable by editing what `noop` returns, so a
decorator that resolves to a definition is followed into its return-deciding statements exactly as a
callee is. Every bare name in the decorator expression is resolved rather than only a callee in call
position, because `@noop` names the wrapper while `@noop(1)` names a factory whose return is the
wrapper, and one rule covering both is worth following a name that merely appears as an argument. An
attribute-spelled decorator resolves to nothing and contributes its spelling alone, the boundary
call resolution already draws. The component is variadic like every other, so a guard wrapped by
nothing hashes what it hashed before the rule existed: no shipped guard's function carries a
decorator, and the three records that moved are the ones whose methods sit in `@dataclass` classes.

Two withdrawals remain outside both rules, and are recorded rather than implied: inverting a
condition two or more levels above the guard's own function, and, for a function whose name another
definition in the module shares, orphaning the chain that reaches it while a call to that other
definition keeps the name-resolved graph satisfied. Both leave the function statically reachable and
every recorded shape unchanged, the second because the call sites themselves are untouched. Closing
the first needs the transitive caller hashing this decision rejects on churn grounds; closing the
second needs receiver type resolution the parse does not have, and tightening the graph instead would
trade a withheld report for a false one against callback dispatch. What stands against both is
classification: a reachable witness proves dynamic reachability by executing the guard.

Withdrawal by dead code above the guard's function belongs to that family, and its crude form is
closed syntactically rather than left to classification. An unconditional `return None` at the top
of `_ShellScanner.scan` makes every scanner guard dead; measured against a candidate tree it kept
all 194 origins, moved no fingerprint and passed every other gate. That edit is visible in the
syntax alone, as a statement sitting after one that leaves its own block, so no statement in a
guarded module may be unreachable in that sense. The rule reads blocks structurally rather than
naming the constructs that carry them, so a module body, an `if` arm, a loop body and its `else`, a
`try` body, handler, `else` and `finally`, a `with` body and a `match` case are all held to it, and
a rule with a blind spot per construct is what a naming-based version would have been.

That rule evaluates no condition, and the targeted conditional form is therefore the residual:
`if option.startswith("--split-string="): return <refusal>` above a frozen guard needs constant
folding to see statically, and hashing transitive callers to reach it is the churn this decision
rejects. The residual's exposure is exactly coextensive with the frozen-debt window. A classified
guard is not exposed to it, because its witness executes the guard through the public path, so the
same edit becomes a failing test rather than a green gate. The residual therefore shrinks
one-for-one as debt is classified, and vanishes when the debt snapshot is empty, which is the
closure target this decision already names. The dynamic control while the window is open is the
recurring checkpoint-corpus differential proposed in issue #182: it replays the authored corpus
against base and candidate and reports every verdict divergence, so it sees an over-refusal as
readily as a certification, which the taint fuzzer's seed-gated false-certification counter
deliberately does not.

Because a tree-local check cannot enforce that debt only shrinks, the comparison against the
protected base runs the base revision's own copy of the checker with the candidate tree as inert
input, on pushes as well as pull requests. That checker reads only the identifiers the candidate
freezes and re-derives their records itself, which is both why the candidate's own fingerprints are
never trusted and how a candidate can migrate the record schema and the fingerprint derivation in
one change: only the identifier field is fixed across schemas. A base whose object cannot be read is
a failure rather than a base that predates the gate, because a skip there would pass grown debt with
a green job.

That base-owned run enforces closure and monotonicity only. The refusal-shape, limits and threshold
rules read allowlists that describe the source they shipped with, so running them from the base
would reject a candidate that legitimately adds a boundary, a declared transport or an inventoried
bound, with no fix available inside the same change; the candidate's own copy enforces them in the
test suite. Closure names no allowlist, so running it from the base is what makes it unweakenable.

Every candidate artifact that base-owned run touches is read by identity alone, for the same reason
and on the same contract: the debt snapshot by its identifier field, and the witness registry by
each entry's `origin_id`. Decoding either against the base copy's own record schema or field lists
would turn a `SCHEMA_VERSION` bump, or a witness strengthened with a new evidence field, into an
uncaught failure of the base-owned job that no edit inside that change can fix, and on a push to
main it would take the release job down with it. Their full canonical shape is the candidate's own
copy to enforce, alongside the three allowlist-bearing rules. For the same reason each gate in a run
is scored independently: several of them raise for a condition another reports cleanly, and building
one failure list eagerly replaced the operator's report with a traceback naming no gate.
Guard *withdrawal* is covered there too: the base's classified inventory is read alongside its debt
snapshot, because deleting an origin together with its witness row leaves the partition exact and
the debt comparison with nothing to inspect. A withdrawal is accepted only when the candidate's
retirement ledger records the identifier and the reason, so removing a fail-closed guard is a
declared, reviewable edit rather than a silent one. Closure holds that ledger disjoint from the
tree's guard origins, because a row naming a guard that is still live changes no comparison and so
would be rejected by nothing when written, leaving a later change free to delete that guard and
have the removal absorbed by a row it did not add.

The magnitude rule, the constructor-reference rule and the relevance floor share one polarity,
worth recording as a decision rather than leaving implicit across three separate commits.
`_is_magnitude_binding`, `_unanalyzable_constructor_references` and the derivation-layer relevance
rule each enumerate what the inventory can follow and reject the rest, rather than enumerating
known bypasses and accepting whatever such a list does not name. An unrecognized spelling is
therefore a loud false positive: the gate refuses to classify it and spells the refusal out
plainly in its report, never a silent bypass that certifies as though the gate had never seen it.
Extending any of the three to cover another benign grammar is done by pinning the new spelling to
its in-tree position, the way displacement, subscript and slice reads, positional-pinned integer
and ANSI-reader base arguments, all-literal arity membership, modulo parity, dict-value escape
tables and a `type` statement's lazily evaluated value are each pinned today, never by loosening a
shape match to admit a family of spellings at once.

Module discovery is the fourth rule under that polarity and the last one to reach it. It decides
which modules the other three run over, so while it recognized participation by *call* every
deny-by-default gate sat behind an accept-by-default gate: a module whose only construction was
spelled through a form no rule can follow was never discovered, and the constructor-reference rule
that rejects exactly that spelling never ran over it. Discovery now over-approximates by mention and
by import instead, and the strict gates decide inside whatever it sweeps in. A module swept in by an
incidental mention is not thereby exempt: it must then satisfy those gates, and that can require
candidate-owned allowlist entries rather than nothing at all. Shape validation and reachability have
nothing to say about a module that constructs no refusal, but coverage and the limits rules are why
the protocol-defining module carries a `GUARDED_MODULES` entry and a `LIMITS_BOUNDARIES` entry today.

The relevance floor carries the same polarity one level down. A boundary-evidence predicate must
read a leaf attribute of the first non-empty layer `guard_condition_reads` derives for its origin,
the transported, condition or closure layer in that order, falling back to the reachability
controls only when all three are empty, and a predicate over anything else is rejected however
plausible it reads. What stays out of that floor's mechanical reach is a predicate that reads the
right leaf and is still weak about it: a predicate over `scope_id` reads a leaf of the
parent-cycle guard's deciding layer while asserting nothing about whether the parent edges it
names actually cycle. That residual is not something the source can close; it is owned by human
review of the invariant row's rationale, per AD-19.

Codex review round 4 on PR #179 is the motivating evidence for recording the polarity here rather
than leaving it as an unstated pattern across commits. Rounds 1 through 3 had each closed a batch
of individual spellings, 8, 2 and 7 findings in turn, each admitted into the existing rules one
spelling at a time. Round 4 found three bypasses that were, each one, a new spelling of a class the
gate already policed rather than a new class: a `BoolOp`-bound threshold, `(strict and 100) or
200`, that the magnitude rule's shape match did not reach; a conditional constructor alias,
`TaintLimits if use_default else injected`, that the constructor-reference rule's shape match did
not reach; and a container-attribute predicate, `bool(e.commands) and bool(e.scopes)`, that read
the scope-parent-cycle guard's iterated container instead of its deciding layer. A round-per-spelling
series has no terminating condition; enumerating what the inventory can follow, and rejecting
everything else, closes the series instead of extending it by one more name each round. Measured
across the three commits that carried this decision, `a7c1c72`, `f1e7e51` and `77c10df`, the
shipped inventory did not move: zero fingerprint churn, `SCHEMA_VERSION` unchanged at 12, and the
frozen debt snapshot byte-identical.
**Consequences:** A refusal observed at the public boundary names the guard that produced it, so a
test can pin a specific site instead of a shared message. New guards cannot arrive unclassified,
and an untested guard cannot be quietly reclassified or laundered onto a different site. Shrunk
limits reach the bounds they name, so a resource guard is witnessed by a small script rather than
an enormous one. The cost is a second gate to maintain and a debt snapshot that must be regenerated
whenever a guard legitimately moves; the closure target is an empty snapshot, which this decision
does not by itself reach.
