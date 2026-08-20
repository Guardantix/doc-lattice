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
`init` is separate and never loads the lattice. The central
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
rollback, journal and artifact recovery containment and validation, and cleanup.
`cli/git_repository.py` owns the package's only subprocess use, the bounded `git`
invocations ordinary `init` runs to probe the default branch. `datetime_utils.py` is the
narrow impure time boundary and the only caller of the current clock, so reading the time
stays outside the transaction module's pure validation and every timestamp the engine
records enters through one substitutable function. The
`doc_lattice.cli` package owns the application boundary; AD-5 owns the split between its
`cli/commands/reconcile.py` adapter and `reconcile_transaction.py`.
Within the cache package, `cache/schema.py` and
`cache/state.py` are filesystem-free, `cache/store.py` owns cache-file I/O, and
`cache/lookup.py` reads and stats documents to select the verify or stat tier.
`linear_fetch` is impure wiring and `linear_client` is the only module that touches
the network.

The filesystem boundary is scoped to accidents, not adversaries, confirmed 2026-08-13. The guards
that hold are the ones an ordinary mistake runs into: `path_utils.safe_resolve()` resolves a path
through any symlinks and then enforces project-root containment, so a configured or
journal-supplied path cannot land outside the project root, while an in-project symlink stays a
legitimate document location; the narrower no-follow checks in `reconcile_transaction.py` reject a
symlink or nonregular file standing in for the reconcile journal or a staged recovery artifact,
where the recorded name itself is the authority rather than whatever it points at; before and after
images plus their recorded fingerprints and the recovery journal keep an unrelated concurrent edit
from being overwritten or silently rolled back; and the advisory reconcile lock serializes
cooperating doc-lattice processes on one project. What is excluded is a hostile local process
sharing the working tree: it may simply ignore the advisory lock, or win a race between a path
check and the operation that follows it, and no check in this engine is written to survive that.
The exclusion cuts both ways. 'It is not hostile-safe' is not a reason to drop an accident guard,
since every one of them earns its place against the mistakes it does catch, and 'a co-tenant could
defeat it' is not a reason to harden reconcile further, since the threat it would harden against is
the one this decision deliberately places out of scope.
**Consequences:** Every command's logic is unit-tested with no I/O; the network slice
is quarantined to one module. The reconcile lock is the package's only lock capability, so no
nesting is possible.

### AD-3: Untyped-to-typed boundary policy

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Document frontmatter YAML, workflow YAML, and Linear JSON arrive untyped.
**Decision:** `typing.Any`/`typing.cast` are allowed only in boundary modules
(`scripts/check_typing_boundaries.py`); the real boundaries are `frontmatter_parser`
(document frontmatter YAML), `linear_parser` (Linear JSON), and `yaml_boundary` (the shared
ruamel safe-load mechanics the first of those and `config` both read through), which validate
into typed models. Everywhere else
passes typed values. `yaml_boundary` is the narrowest of the three: it returns the loaded
value still untyped and each caller validates it, which is why the untyped return does not
widen the boundary past the module that produces it.
**Consequences:** Untyped data cannot leak past the named boundary modules; CI enforces
it.

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
project root before re-reading exact bytes. `commit_rewrites` independently re-validates containment during
preparation: `_prepare_transaction` calls `_preflight_rewrite_destinations`, which uses
`safe_resolve` to contain every supplied live destination against the canonical project
root before staging.

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

Rollback classifies rather than skips. Each destination is `restored` from its before
image, `already_before` and so needing no mutation, or `unresolved` because it matches
neither recorded image, absence included. Only `unresolved` is a partial rollback, and
a partial rollback is reported as such in every channel: a distinct recovery action,
the unresolved destinations named on stderr, and a nonzero exit from both `--recover`
and automatic pre-run recovery, which stops before lattice loading. A partial rollback
performs no cleanup at all, keeping the exact journal and every remaining stage, since
the journal is the only record binding a destination to its paths and digests and
selective cleanup would add a fallible mutation path without helping correctness. The
in-process abort distinguishes destinations whose replacement it attempted from those
it never reached, counting a destination as possibly applied from before the call
because `replace_staged` renames before it synchronizes; a destination the run never
touched is therefore not an unresolved rollback entry.

Normal real startup recovers a valid outstanding journal before lattice loading, and an
automatic-recovery notice may be emitted on stderr while the lock is held.
`reconcile --recover` performs only that recovery, while dry-run never recovers or
persists anything and refuses an outstanding journal. Invalid or unauthenticated
recovery evidence is retained for explicit manual remediation rather than guessed at
or deleted. Every recovery also scans the project for transaction artifacts no retained
journal accounts for, after journal handling rather than only in the no-journal branch,
so that an interrupted journal publication reports both the recovered journal and its
leaked helper stage in one invocation. Orphans are reported with project-relative paths
and a nonzero exit and are never deleted. The transaction module resolves journal paths
through `safe_resolve` and validates project-relative containment, path roles, artifact
locations and file types, and recorded fingerprints before recovery mutates them.

**Consequences:** A successful reconcile is a durable all-or-nothing batch from the
operator's perspective. A `prepared` journal rolls transaction-owned changes back; a
`committed` journal records that PONR has passed and makes recovery cleanup-only. A
recovery that could not restore everything says so and keeps the evidence needed to
finish by hand, so no exit code or report can claim a rollback that did not happen. The
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

A `docs_roots` entry naming a single `.md` file directly is an exception to "retains the
first unresolved path": its document identity is the fully resolved path, because
`_resolve_roots` stores only resolved roots and a file root has no directory walk left to
reintroduce an unresolved form. A path found by walking a directory root still retains the
first unresolved path encountered during that walk, matching the Decision above. Both are
deliberate: the file-entry case has no unresolved candidate to prefer, while the walked
case keeps the reporting stability the Decision describes.

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
format validation, indentation, exact output, and GitHub annotations. The package also
holds `options.py` (shared Typer option types) and `git_repository.py` (local Git discovery
for `init`). Each module under
`cli/commands/` is a narrow command adapter.

`git_repository.py` owns one discovery contract, and it is a hint rather than a prerequisite.
Default-branch discovery for `init` is best-effort: that command has no
Git prerequisite at all, so a missing executable, a directory outside a worktree, a missing
or dangling `origin/HEAD`, a timeout, and unusable output all yield no candidate and the
adapter falls back to `main`. A second contract, top-level resolution for the managed commands,
sat beside it until AD-32 retired those commands; every failure of that one was a `ConfigError`.
Only a branch name that was actually supplied or discovered and
then fails the module's ASCII branch-name policy raises, because a GitHub `branches:` filter
is a glob pattern rather than a literal and must never receive a pattern. The module stays the
sole owner of the Git subprocess boundary and its timeout: `scaffold.py` remains pure and
receives the resolved name as a required keyword argument, and precedence and narration stay
in the `init` adapter. There are no
mutable module-level consoles and no mutations of Typer color globals.

Both contracts resolve the Git executable before running it, through two independent rejections.
A `PATH` lookup that returns a relative result is refused outright, because the result is the
matched name joined onto the entry it came from, so a relative result means the entry was
relative; that covers `.`, `..`, any deeper ancestor, and the Windows search that prepends the
process directory. An absolute result is then refused when it resolves inside the invocation
directory or the process's own working directory, which catches an absolute entry pointing into
the tree being operated on, including one holding a symlink into it.

The earlier decision to run a bare `git` from the maintainer's `PATH` is withdrawn. It was
defensible while only the managed commands shelled out, since those ran inside a repository the
maintainer already trusts, but `init` runs in freshly cloned ones, and Windows searches
the invoking process's current directory ahead of `PATH`. A repository carrying its own `git.exe`
would have been executed, which SECURITY.md's scope says cannot happen. Resolution failure keeps
the probe's shape rather than introducing a second one: no candidate, and the fallback.

Enumerating untrusted directories is deliberately not how the relative case is handled. A
relative entry can name any ancestor, and no project root has been resolved at the point the
executable is chosen, so rejecting relative results is the only form of the
check that is complete. What remains accepted is an absolute `PATH` entry somewhere in the
project, which is a directory the user explicitly chose to trust and that no repository can put
on `PATH` itself.

`cli/errors.py` owns diagnostic rendering, exit constants, and command-level
`ProjectError` context conversion. `cli/__init__.py` preserves
`doc_lattice.cli:main`, loads the compatibility `app` export lazily, and owns
entry-point exception mapping: `ProjectError` and the supported unexpected errors map
to exit 2, while intended `SystemExit` values propagate unchanged.

AD-5 owns the split between `cli/commands/reconcile.py` and
`reconcile_transaction.py`; this package refactor moved no durable reconcile mutation
into the CLI.
**Consequences:** Invocation state and diagnostics can be tested without shared
console state. Tests under `tests/cli/` mirror the command adapters and add focused
runtime, output, and cross-command contract coverage. Durable reconcile safety keeps
its independent transaction boundary.

### AD-10: Output selector compatibility converges in 2.0

**Date:** 2026-07-14
**Status:** Accepted
**Context:** The 1.x commands exposed structured output through inconsistent selectors: some took
`--format`, some only a silent `--json` alias, and some both. Removing `--json` during 1.x or
warning on stderr would have broken scripts, but carrying both selectors indefinitely would have
preserved an inconsistent interface.
**Decision:** `--json` remained silent and behaviorally compatible throughout 1.x, and 2.0 removed
it everywhere in favor of `--format` alone. The migration notice was documentation-only, because
stderr could not carry a compatibility-safe warning. README owns the current per-command selector
values; CHANGELOG owns the 2.0 migration.
**Consequences:** Selector inconsistency persisted through 1.x as the price of byte-exact
compatibility, and the break waited for a major version. This decision did not freeze every 1.x
output schema.

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
dependency. AD-28 records how the maintenance path stays runnable.

### AD-14: Documentation ownership is one-way

**Date:** 2026-07-14
**Status:** Accepted
**Context:** Repeating current behavior across user docs, contributor guidance, roadmaps,
and completed implementation documents creates conflicting sources of truth.
**Decision:** README.md owns the user contract; ARCHITECTURE.md owns durable decisions and
module boundaries; CLAUDE.md routes contributors and agents and lists enforced repository
rules without restating behavior; CHANGELOG.md owns release history and migrations; and
ROADMAP.md owns future direction. Maintained documents link to the owner instead of copying
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
**Consequences:** This was a documented breaking change in 2.0. Future
configuration keys are not reserved as inert surface without an approved requirement.

### AD-16: GitHub administration remains an external human boundary

**Date:** 2026-07-15
**Status:** Accepted
**Context:** Workflow files are repository-controlled input, so a same-repository pull request can
edit a workflow that receives a broadly scoped secret. The normal package code must not receive
GitHub administration credentials while establishing a safer authorization boundary.
**Decision:** GitHub administration stays outside this package entirely. A human maintainer runs
a reviewed external `gh` sequence to configure and read back a GitHub environment whose deployment
allow list is exactly the `main` branch. The dedicated `DOC_LATTICE_LINEAR_API_KEY` exists only in
that environment, and it is mapped onto `LINEAR_API_KEY` only on the final step of the trusted
Linear job. No package code ever receives a GitHub administration credential, and no workflow that
can reach that secret runs real `reconcile`.

MANAGED_CI.md publishes that sequence, and the workflow it protects, as a hand-installable recipe.
For a time the same boundary was reached through a generator instead: `doc_lattice.github_ci`
rendered and audited two workflows, a bootstrap script, and a scoped `.github/.gitattributes`
policy that preserved LF for the bootstrap after checkout, and CLI adapters resolved the Git
top-level to create, inspect, audit, and refresh those files without loading the lattice or
touching the network. AD-32 records why that half retired. The boundary never depended on it.
**Consequences:** `linear_client` remains the only Python network module. Remote setup is explicit,
reviewable, and separate from the Linear secret entry, and it is now established and re-verified by
hand: the readbacks MANAGED_CI.md's steps 3 and 6 prescribe are the whole of it. Nothing observes
GitHub environment or organization-policy drift, and nothing observes local drift in the two
workflows either, which the retired bootstrap verifier and offline audit each covered a part of.
The trusted job's own repository, ref, and event guards are therefore what refuse a
`pull_request_target` run the environment policy would otherwise authorize, and workflow and
branch governance for trusted `main` remains the residual authorization boundary.

### AD-17 through AD-23: Shell scanner decision history (removed)

**Date:** 2026-08-09
**Status:** Removed

These seven records designed, hardened, and finally froze the CI shell scanner that AD-25
extracted from this package. They governed nothing here after the extraction, so per AD-14 they
were removed rather than maintained as history. Their full text remains in this repository's
version control history through commit `2981248`, and their live successors are maintained in
doc-lattice-shell-lint's own ARCHITECTURE. The numbers stay retired so existing references
resolve unambiguously.

### AD-24: The supported Python floor is 3.13

**Date:** 2026-08-04
**Status:** Accepted
**Context:** `requires-python = ">=3.13"` reads as an ordinary packaging choice, but two shipped
behaviors depend on that exact floor and neither is visible from the packaging metadata.
**Decision:** The supported floor is Python 3.13, and it is load bearing rather than incidental.
`discovery.py` matches `ignore_globs` with `Path.full_match`, which exists only from 3.13. AD-13's
generated slug data bridges the Unicode table the minimum supported runtime ships to the
`github-slugger@2.0.0` JavaScript target, so the floor selects the baseline that generation is
verified against. Changing the floor in either direction therefore requires regenerating that data
and re-running the parity and benchmark verification AD-13 prescribes. README owns the user-facing
`Python 3.13+` requirement.
**Consequences:** A request to lower the floor is a compatibility review rather than a metadata
edit, since section identity is measured against the minimum runtime's Unicode table. Raising it
is equally reviewable, and neither move lands without regenerated, re-verified generated data.

### AD-25: The CI shell scanner is extracted to doc-lattice-shell-lint

**Date:** 2026-08-05
**Status:** Accepted
**Context:** AD-23 froze the scanner as a best-effort accident lint with no remaining roadmap. An
eight-thousand-line traceability engine still carried that fifty-thousand-line frozen subsystem
plus a verification harness every contributor had to reason about, and the size misstated the
project's identity. A frozen subsystem with its own complete verification story is a separable
tool, not a feature of a document traceability engine.
**Decision:** The scanner and its verification harness move verbatim to
`Guardantix/doc-lattice-shell-lint`, extracted at doc-lattice commit `38f00d6` and released
independently on PyPI as `doc-lattice-shell-lint`. The two repositories are fully severed: neither
has a runtime, build, or CI dependency on the other, in either direction.

`ci audit` therefore performed no shell analysis at all. The `PR_LINEAR_INVOCATION` and
`PR_MUTATING_RECONCILE` finding codes are retired, as is the exit-2 unsupported-shell-semantics
outcome. An optional-import integration was rejected because an audit's contract must not vary
with what happens to be installed on the runner; a hard dependency was rejected because it
reimposes the cost the extraction removes; and deletion was rejected because the scanner works
within its frozen scope. Adopters who want the lint run `uvx doc-lattice-shell-lint` as its own
explicit workflow step, which README owns.

The scanner's decision history, AD-17 through AD-23, left this document with the subsystem it
governed; the tombstone above records the removal. Live successors are maintained in
doc-lattice-shell-lint's own ARCHITECTURE, whose SL-1 records this extraction from the other side.
**Consequences:** This package's audit reports structural workflow findings only, which is a gate
becoming more permissive and therefore a major version (3.0.0). A consumer whose workflows passed
a doc-lattice audit that included shell certification must add the standalone step to keep those
findings. Shell-scanner work is now filed, reviewed, and released on the extracted repository at
its own cadence, and a change there can no longer regress this engine.

### AD-26: Reconcile output identity depends on ruamel parser internals

**Date:** 2026-08-11
**Status:** Accepted
**Context:** Reconcile rewrites exact source bytes so a document's body, key order, comments, and
list indentation survive a `seen` update. Locating those bytes means reading ruamel's parser
events and scanner tokens, and the byte offsets on their source marks, rather than dumping a
loaded document back out. Dumping cannot meet the contract that only `seen` changes: a fixed
emitter indentation rewrites whichever accepted list style it was not configured for, and even
an emitter setting selected per document restyles unrelated collections when a file mixes
styles, so only targeted source edits leave every unrelated byte alone. The marks that locate
those edits are implementation details of a dependency the project otherwise
declares by floor alone, and a change to how ruamel accounts for marks would move every span this
module measures.
**Decision:** `reconcile.py` depends on `ruamel.yaml.events`, `ruamel.yaml.tokens`, and
`start_mark.index`/`end_mark.index`/`start_mark.column` semantics as a deliberate compatibility
surface, and the dependency is bounded to a verified range rather than pinned to one release. The
implementation is part of that surface, not only the version: every loader this module builds asks
for the pure Python parser explicitly, because a plain safe loader switches to the C one whenever
the optional `ruamel.yaml.clib` accelerator is installed, which any other package in a user's
environment may pull in, and that parser reports coarser marks and exposes no scanner at all. The
`yaml-compatibility` CI leg runs the suite at the declared floor and
at the resolved ceiling of the range with that accelerator present, so both the range and the
choice of implementation are verified rather than asserted. The installed parser is one axis of
that degradation and the interpreter is the other. ruamel enforces `!!omap` key uniqueness and the
`%YAML` version range with bare `assert` statements rather than raised errors, so `python -O` or
`PYTHONOPTIMIZE` compiles both checks out: a repeated `!!omap` key then loads last-wins instead of
being refused, and an unsupported `%YAML 1.3` surfaces as a `KeyError` that no `AssertionError`
handler catches. Enabled assertions are therefore a condition of
every guard this project layers on ruamel, this engine is supported only in that default mode, and
`yaml_boundary.py` reimplements neither check to escape the dependence, since doing so would put
constructor internals into the one boundary that consumes loaded values alone. Where an event mark
and a token mark disagree, the token is authoritative: a node's own mark starts at an anchor or a
tag, while the scanner's block-mapping start, key indicator, and value indicator give the
indentation, the pair boundary, and the `:` an edit has to align to. A node's properties are part
of that surface too. A parsed node reports an explicit tag as its resolved URI and reports none
for a scalar the loader resolves implicitly, which is how this module recognizes a merge key and a
tagged scalar the way the constructor does rather than by re-resolving either itself. A node
written tag first opens at its anchor rather than at its tag, so the tag token, paired with the
scalar token that follows it, is what bounds an edit that has to overwrite or reproduce one. Each
read builds its own loader, since a shared instance carries document state (`YAML.version`, and
the reader and scanner bound to one document) whose reset behavior is itself version dependent.
The boundary loaders behind `frontmatter_parser.py` and `config.py` may keep shared module-level
instances despite that, because they consume only loaded values and never a source mark. They
still state a parser: `SafeYamlLoader` takes the implementation as a required argument,
`frontmatter_parser.py` asks for the pure one under AD-33 so its accepted document set is fixed,
and `config.py` asks for the platform default one. Those
mechanics are owned by `yaml_boundary.py`: its `SafeYamlLoader` performs the reset, and its
`YAML_LOAD_ERRORS` names the failure family both of those modules and `reconcile.py` catch.
Each caller still constructs its own `SafeYamlLoader`, so the sharing stays within a module rather
than becoming one cross-module instance whose document state a second boundary could observe. A
shared instance does retain state across loads: `YAML.version`, the piece that steers a later
parse, is cleared before each load by discarding the underlying loader whenever a directive set
it, since clearing the attribute alone does not rebuild the versioned resolver at the declared
floor and would leave the previous document's version in force there. The `DocInfo` record each
load appends to `doc_infos` is write-only metadata the loader never consults, one small record
per document for the life of the process, with no effect on any later parse; the cross-document
directive-leakage tests pin that a directive in one document does not steer the next, and the
compatibility leg runs them with and without the accelerator, since the C parser ignores
directives outright and would otherwise pass them vacuously.
Every rewrite is reparsed and compared against the intended document before it is staged, so a
mis-measured span is refused rather than published.
**Consequences:** A ruamel major or minor bump is a compatibility review with the reconcile source
suite as its evidence, in the same spirit as the AD-13 parser pin, not a floor edit, and widening
the declared range means widening the compatibility leg's matrix with it. If a future release
changes mark accounting, the reparse gate turns a silent corruption into a refusal to write, which
is the failure mode this engine prefers.

### AD-27: Runtime dependencies are bounded above, and markdown-it-py stays exact

**Date:** 2026-08-14
**Status:** Accepted
**Context:** Adopters invoke this engine by exact version, and `uvx --from doc-lattice==X` resolves
the dependency closure fresh on every run, so an upstream release rather than a doc-lattice release
decides what an adopter's gate actually executes. `typer`, `rich`, and `pydantic` were declared by
floor alone. Each sits on a user-visible surface: typer owns the command surface and its parsing,
rich renders every human report, and pydantic's validation messages are embedded in that output, so
even a minor changes what users see and a major can break every pinned adopter with nothing in this
repository having changed.
**Decision:** Every runtime dependency carries an upper bound. `typer`, `rich`, and `pydantic` are
capped at their current majors, `<1`, `<16`, and `<3` respectively, so crossing a ceiling is a
deliberate edit here rather than a resolver's choice on an adopter's machine. A bound is the floor
of the treatment, not the whole of it; how far a dependency is verified past its declaration is
decided per dependency by how much of its behavior this engine actually reads.

- `ruamel.yaml` is bounded to a range and additionally verified across it by the
  `yaml-compatibility` CI leg, because AD-26 makes that parser's event and token source marks a
  compatibility surface rather than an implementation detail. A range that is only asserted would
  let a difference at the declared floor ship unseen, since the lock only ever installs the ceiling.
- `markdown-it-py` stays pinned exact at `==4.2.0` rather than ranged. AD-13 makes section identity
  depend on that parser's tokenization, so a range would admit releases under which the same
  heading resolves to a different section ref: a silent identity change rather than a visible
  break. The known cost is a transitive constraint through `rich`, which depends on
  `markdown-it-py` itself, so a future `rich` whose floor moves past 4.2.0 is unresolvable until
  this pin is re-reviewed and section identity re-verified. That is the intended trade, because an
  unresolvable lock fails loudly and a shifted slug does not.
- `typer`, `rich`, and `pydantic` carry bounds only. No leg verifies the span beneath each ceiling,
  because nothing here reads their internals; the bound exists to hold the next major out until
  someone looks at it.

**Consequences:** An upstream major cannot reach a pinned adopter without a doc-lattice release,
and raising a ceiling is a compatibility review with the suite as its evidence.
`tests/test_package_metadata.py` fails when any runtime dependency is declared without an upper
bound, so the policy covers dependencies this record does not name. The standing cost is
maintenance: a new upstream major is unavailable to adopters until this project reviews it and
ships, and the `markdown-it-py` pin can block a `rich` upgrade outright until both are reviewed
together.

### AD-28: Slug regeneration pins the exact Node version and vendors its upstream input

**Date:** 2026-08-15
**Status:** Accepted
**Context:** AD-13 makes Node a maintenance-only dependency for regenerating and verifying
`_github_slugger_data.py`, but left the maintenance path itself unreproducible in two ways. The
generator accepted any Node whose ICU reported JavaScript Unicode 17.0, so the exact generating
runtime was recorded nowhere and could not be re-established later; ICU advancing to Unicode 18
would have turned that check into a hard failure with no pinned runtime to fall back to. Upstream
input arrived through `npm install github-slugger@2.0.0` at generation time, so `--check` needed
the network and the npm registry to still serve that version.
**Decision:** The exact generating runtime is pinned in `.nvmrc` as `v24.13.1`, all three
components. The generator validates that exact version alongside the existing Unicode check and
renders it into the artifact as `GENERATED_NODE_VERSION`. Upstream input is the unmodified npm
registry tarball vendored at `vendor/github-slugger-2.0.0.tgz`, verified against a pinned SHA-512
before extraction and resolved by default; `--package-root` remains an explicit override and the
implicit `npm install` fallback is gone.
- The pin is the full patch version, not the major. nvm resolves a partial `.nvmrc` to the latest
  matching patch, and Node 24.13.1 itself carried an ICU update, so anything looser lets the
  Unicode table move while the pin appears unchanged. Since the artifact now records the version,
  a drifting runtime would also change the artifact bytes.
- The tarball digest, not the regex digest, is the upstream-input identity. The Node evaluator
  imports `index.js`, which imports `regex.js` and implements the lowercase and replace operation,
  so hashing the regex alone never covered everything executed. `UPSTREAM_REGEX_SHA256` is kept as
  the narrower artifact-level record of the behavior behind the stripping pattern.
- The vendored tarball is a repository-only maintenance asset. The sdist include list and the
  wheel package list in `pyproject.toml` both enumerate their contents, and neither names
  `vendor/`, so AD-13's no-Node-runtime boundary holds by construction rather than by convention.
  Upstream's ISC license travels inside the tarball and is copied out beside it.

**Consequences:** Regeneration and `--check` run offline, on a machine that is not the one the
artifact was first generated on, for as long as the vendored bytes and a Node 24.13.1 build remain
obtainable. Node's ICU advancing no longer breaks the maintenance path, because the pinned runtime
is what the generator asks for. The costs are a checked-in binary and a second pin to move: raising
either the Node version or the upstream tarball is now an explicit edit to `.nvmrc`,
`UPSTREAM_NODE_VERSION`, and `VENDORED_TARBALL_SHA512`, followed by the regeneration, parity
verification, and benchmark validation AD-13 already prescribes.

### AD-29: A skipped file's reason is cached data, and is reported from one site

**Date:** 2026-08-15
**Status:** Accepted
**Context:** Parsing answered "is this a node?" with a node or nothing, so a file whose `id` key
was mistyped left the lattice with its declared edges and no diagnostic, indistinguishable from
prose the engine never tracked. Reporting the skip is only half the fix. AD-12 requires the
default tier to match uncached results, but the cached path returns before parsing and
`Entry.node = null` recorded both kinds of skip identically, so a diagnostic raised as a parse
side effect would appear on a cold run and vanish on the warm run that replays it.
**Decision:** Parsing classifies and never reports. It returns a disposition alongside the
optional node, distinguishing a tracked node, untracked content, and a fenced block with no `id`,
and raises instead when such a block declares any key from the exact intent set
(`derives_from`, `authority`, `tickets`) that would take real edges down with it. The cache
stores that disposition as a required `Entry` field, so a hit replays what the miss concluded;
`CACHE_VERSION` rises with it so entries predating the field are discarded rather than defaulted
into the silent skip. What is stored is the kind, never the rendered message: a cache slot is
shared across worktrees, so the text is rendered per run from the path that run discovered.
Every load path (cache-free, cache-miss, and cache-hit) reports through one function in
`orchestrate.py` at the default `stacklevel`, because Python filters repeats by a warning's
raising location, so a second call site would change when the warning is shown.
Raising it through `warnings` rather than calling the per-invocation `CliRuntime.stderr` that
AD-9 makes the owner of CLI diagnostics is deliberate, and this warning family, raised from
`orchestrate.py`, `discovery.py`, and `loader.py`, is the only engine diagnostic emitted outside
that boundary. The reason is that a skip is the only diagnostic here a user may legitimately want
to suppress on an otherwise healthy corpus, and `warnings` is the standard, already-documented
suppression mechanism; `cli/errors.py` has no equivalent.
Presentation is nevertheless AD-9's, because the two are separable: Python decides whether to
ignore, display, or raise a warning before it reaches the replaceable `showwarning` stage, so
`CliRuntime.rendered_warnings()` substitutes only that stage, for the duration of a phase that
can reach a parser. Every such phase is wrapped, not the lattice load alone, because more than one
of them can raise about the same document: a reused YAML anchor is reported for a tracked document
by `orchestrate.py`, which reports it from the shared site after `frontmatter_parser.py` has
intercepted ruamel's own warning per AD-33, while `config.py` and the fresh reread `reconcile`'s
rewrite phase performs after that load has returned both still let ruamel raise it directly.
Leaving any of them out would print one of a run's warnings in Python's default format and the next
in this one. The substitute renders `warning: <message>` through the invocation's stderr `Console`,
discarding the category, filename, line number, and source line Python's default formatter would
have shown, stripping a message that opens with a newline so the prefix never lands on a line of
its own, and restoring the previous callable in a `finally` on both the normal and the exception
path.
Filtering, category matching, and repeat suppression stay engine-owned and unreimplemented, and a
library consumer calling `load_lattice()` directly is untouched. Routing the three `warnings.warn`
sites through the stderr renderer instead would have forfeited that filtering, which README
documents.
An advisory must not be able to end the command that raised it, so a write the stderr stream
refuses is contained for the whole wrapped phase: the render is guarded, and a stream that refused
one warning is not asked again. The guard deliberately stops at the phase boundary, because the
work inside raises `OSError` for real read failures and those must keep propagating. Rich's own
`Console.on_broken_pipe` is unusable here for the same reason: it points `sys.stdout` at
`os.devnull` and raises `SystemExit(1)`, so a dead *stderr* discards the report a succeeding
command is still computing, and it does that before any caller could catch. `CliConsole` overrides
it to re-raise the `BrokenPipeError` instead, which also settles the identical exposure
`cli/errors.py` carries: every CLI write now fails like an ordinary stream write, and the entry
point's existing `OSError` handling governs it. The one write that handling cannot govern is its
own: an exception raised inside an `except` clause is never retried against a sibling clause, so
the entry point suppresses `OSError` and `ValueError`, a closed stream's write error, around
the error report itself and keeps the tool-error exit, since a report to a stderr that refuses
the write cannot be delivered anyway.
`BrokenPipeError` alone is caught ahead of that generic `OSError` handling, and it exits 141
(128+SIGPIPE) silently rather than joining the tool-error mapping: a departed reader is
truncation, not a tool failure, and reusing 2 for it would make the 0/1/2 contract CI relies on
ambiguous between "the tool broke" and "something downstream stopped reading." The handler still
has one write of its own to answer for, though: flushing already-buffered stdout can raise the
same `BrokenPipeError` again, and the interpreter's own shutdown flush of the now-dead stream
would otherwise print exactly the "Exception ignored" noise this handler exists to suppress, so
it points both stdout and stderr at `os.devnull` before returning.
Costs survive it. Under `PYTHONWARNINGS=error` the warning is raised before `showwarning` is
consulted, so no hook can reach it and no substitution made here can present it; that is why the
exit-status guarantee below is stated for ordinary warning configuration. What that cost was is
now narrower than this record originally set down. It escaped the entry point's `ProjectError`
mapping entirely, printing a traceback and exiting 1, the code otherwise reserved for drift, and
**AD-39 amends that half**: the entry point catches `Warning` beside `ProjectError` and reports it
as a coded `WARNING_AS_ERROR` error exiting 2. Nothing here changes -- no emission site, category,
filter, or `showwarning` substitution moves -- because the repair belongs at the boundary rather
than at the sites this record owns. And replacing
`showwarning` takes the warning out of reach of anything that records rather than prints it:
CPython dispatches to the substitute instead of the recording branch, so a `catch_warnings(record=True)`
around a wrapped phase collects nothing and an embedder's `logging.captureWarnings(True)` router is
bypassed for its duration. Declining to substitute when another callable already owns the stage
would fix that, but only by reading the private `warnings._showwarning_orig`, so the cost is
accepted and pinned by a test instead.
`warnings.showwarning` is process-global while each caller restores the snapshot it took, so
scoping the substitution to the synchronous phases one invocation performs narrows but cannot
eliminate what concurrency does to it. The exposure is worse than a window: for two threads whose
phases overlap and finish in entry order, the first restores the original and the second then
restores the first's renderer, leaving the hook pointing at a finished invocation's stderr
indefinitely, not merely for the overlap. Correcting that needs the hook installed once under a
reference count with the active runtime carried in a `ContextVar`, since serializing the swap and
restore alone does not change the ordering that causes it, and holding a lock across whole phases
would serialize every concurrent load in the process. It is not built, because the CLI creates no
threads and one invocation owns its process; a caller driving `CliRuntime` from several threads is
the unsupported case this records rather than solves.

**Consequences:** A typo'd `id` is a tool error naming the file, and unrecognized frontmatter is a
named skip rather than a silent one, at the cost of a new warning for corpora carrying non-lattice
frontmatter under a docs root. Under ordinary warning configuration that skip leaves the exit
status untouched. Suppressing it for one file is `ignore_globs`; `PYTHONWARNINGS` reaches it but
cannot single it out, because that setting matches a literal message prefix rather than a pattern
and `discovery.py`'s symlink-escape warning opens with the same `skipping ` prefix. Diagnostics a
load emits are now cache-visible state: any future one has to be derivable from an `Entry` and
rendered at the shared site, or the warm path will not reproduce it. `title` and `layer` stay in the warning tier deliberately: deriving the fatal set
from `NodeMeta`'s fields instead of declaring it would turn ordinary descriptive frontmatter into
an exit 2.

### AD-30: Only gate-verified bytes may reach a reconcile destination

**Date:** 2026-08-15
**Status:** Accepted
**Context:** AD-5 makes the commit transaction durable but not self-checking: it stages
`Rewrite.after` and publishes those exact bytes without ever reparsing them, so
`reconcile.py::_verify_reconciled_meta` is the last point at which a mis-spliced rewrite can be
refused instead of written durably. The chain that reaches the gate held only by convention.
`Rewrite` is an ordinary frozen dataclass, and the staging and publication helpers are ordinary
functions, so a future producer or sink could route around the gate and the suite would stay
green. Reachability alone is too weak a property to assert: a producer can call the gate and
then build its output from a different buffer, the gate can verify one value while the changed
return is assembled from another, and a new publication route can bypass the `Rewrite` and
after-image-infix anchors entirely.
**Decision:** The invariant is that every byte published over a reconcile destination on the
forward path originates in the value `_verify_reconciled_meta` verified. It is enforced
mechanically by an AST convention test in `tests/test_conventions.py`, keyed to function, callee,
and value provenance rather than to line numbers, which pins: the sole production `Rewrite(...)`
site; the `plan_rewrites` to `apply_reconcile` to `_verify_reconciled_meta` chain; that the
changed-output text derives from the exact argument the gate verified, through frontmatter
envelope reassembly only; that `Rewrite.after` derives from what `apply_reconcile` returned,
through line-ending restoration and UTF-8 encoding only; that every possibly changed return
follows the gate; the single after-image staging site and the bytes it stages; and the single
forward publication sink, with the publication helper reachable from no module but
`reconcile_transaction.py`, whether named directly or through a composite primitive that stages
and publishes one destination in a single call. Each such primitive's present users write their
own artifacts rather than documents and are pinned per primitive, so a new one fails closed.
Positive controls prove the detector rejects each bypass rather than
passing vacuously. The invariant covers transaction after images only. Before images, journal
bytes, recovery artifacts, and the before-image rollback sink are deliberately published without
passing through the gate, and a negative control pins that exemption so the guard does not fail
on correct code.

Twenty-five near-miss shapes are pinned explicitly, because each defeated an earlier draft of the
guard and each was reproduced against it before being closed. The gate must be its own
unconditional top-level statement, since a gate merely contained in an earlier statement can sit
inside a conditional and skip the path a later changed return takes. Both operands of the
line-ending restoration are constrained, since checking only the searched text admits a
replacement that rewrites verified content, and each is one complete ending rather than any run
of ending characters, since replacing one newline with two opens a blank line after every line
the gate verified. The envelope fields a rewrite may reattach are whitelisted, excluding
`raw_meta`, which holds the pre-edit YAML the gate never verified, and the literals it may
contribute are restricted the same way, since text spliced into the reassembly f-string is
published byte for byte. The reassembly is pinned as a complete envelope, each piece reattached
exactly once and in order around the verified metadata, since requiring only that some verified
value appear accepts an assembly that emits nothing but gate-verified bytes while dropping the
fences and the entire body. The literals hold their place in that sequence rather than being
validated and dropped, since a legal line ending in an illegal position is still published
unverified. Producer and publication scans resolve module-qualified references
and every kind of alias, by import, by assignment, by parameter default, or by inheritance, since
`Rewrite as R`, `R = Rewrite`, `def build(..., constructor=Rewrite)`, `class Rogue(Rewrite)`, and
`persistence.replace_staged(...)` are each a complete route past a bare-name scan. The
publication-reach rule reads every mention of the identifier rather than only its imports, and
follows the composite primitive that reaches the helper without naming it. The staging scan reads
the staged operand as well as the prefix, since a site can bind the after-image infix to a local
first, a shape the transaction module already uses. The producer scan also refuses a dataclass
field copy, since `dataclasses.replace(rewrite, after=...)` mints a `Rewrite` carrying ungated
bytes without ever naming the class, and refuses one whose keyword arrives unpacked, since a
`**` argument carries no readable field name and an unreadable copy fails closed rather than
being assumed harmless. The sink must take its image and its destination from the
same journal entry expression, since matching only the two field names lets one entry's staged
image publish over every destination the commit loop visits.

A publication route need not name any pinned helper at all, so the transaction module is also
audited by destination rather than by primitive: nothing there may hand a journal entry's
destination to a callee outside a pinned reader set, or call a method on one. Enumerating write
primitives could not close this, since a new sink can always name a primitive the guard has never
heard of, whereas reaching the destination is the one step it cannot avoid. That audit is scoped
to `reconcile_transaction.py`, the only module owning reconcile destinations. Its exemptions are
bare names resolved in that module rather than terminal attribute names, so an unrelated method
cannot borrow a pinned reader's name, and a name with more than one binding stays tainted if any
of them held a destination, since a rebound name is ambiguous rather than safe.

Recording a destination in an in-memory container reaches no filesystem, so classification and
rollback outcome bookkeeping may accumulate one. That exemption is recognized by shape, not by
callee name: the receiver must be a local provably bound to a collection built in the same
function. Admitting the bare names would let anything answering to `append` take a destination,
and a control pins that it cannot. Storing a destination does not launder it either. A container
that ever received one is tainted, and reading an element back out, by subscript or by iterating
it into a loop variable, is a destination again, so a round trip through a list does not carry a
publication past the audit.

One scoping limit is deliberate rather than closed: `persistence.py` owns the publication helper
and is exempt from both the reach rule and the sink audit, so a forward sink added inside that
module is invisible to the guard. Narrowing it would fire on the module's own correct internal
use of the helper. Publication ownership stays a review obligation there.
**Consequences:** A genuinely new producer, staging site, or publication route fails closed and
forces a conscious audit, instead of silently widening the set of bytes that can reach a
document. The guard is a tripwire on the current AST shape, not a general dataflow analysis:
restructuring these functions is expected to fail it, and the resolution is to re-derive the
invariant, never to loosen the matcher until it passes. What the gate compares stays a behavior
assertion in `tests/test_reconcile.py`; this decision governs only which bytes reach it.

### AD-31: The reconcile rewriter supports a declared frontmatter subset

**Date:** 2026-08-15
**Status:** Accepted
**Context:** `reconcile` is the only command that writes to a user's documents, and AD-26 makes it
edit exact source bytes rather than dump a loaded document back out. That buys byte-level
preservation and costs a bounded input language: every spelling the rewriter can locate an edit in
had to be implemented one at a time, and nothing recorded which ones those are. "Is the rewriter
complete" was therefore an unbounded question about YAML rather than a finite one about a declared
subset, so an unimplemented spelling read as a defect and an incidental one read as a commitment.
The write path also reads its input twice under different rules. A tracked document is loaded and
validated into `NodeMeta` at check time, while `apply_reconcile` rereads the file fresh at write
time and deliberately tolerates shapes that validation rejects, because a concurrent edit between
those two reads is the case it exists to survive. Nothing separated the two, so a shape the write
path merely tolerates was indistinguishable from one the project accepts as input.
**Decision:** The supported subset is declared here, in five layers, because the layers carry
different guarantees and flattening them into one list is what made the boundary unreadable. The
record declares the subset rather than implementing one: it adds no preflight classifier and no
refusal that did not already exist.

**Layer 1: semantic schema.** The validated shape a tracked document must load as is owned by
`NodeMeta` and `RawEdge` in `model.py`, under `strict=True` and `extra="forbid"`. Public `seen` is
`str | None`. AD-35 narrows both string positions further: a `ref` or a `seen` constructing a C0,
DEL, or C1 code point is refused, which takes clip and keep chomping out of the strict column of
the `Entry` `seen` and `ref` rows below without moving the reread column beside them.

**Layer 2: supported spellings on the writable path, by position and by load phase.** One row per
writable position and per dimension, and two columns: what a strict tracked-document load accepts,
and what the fresh reread inside `apply_reconcile` additionally handles. The constructs are not
interchangeable across rows, so they are recorded per row rather than as one flat list. The two
columns are what keep defensive-only recovery shapes out of the publicly accepted subset.

| Position | Dimension | Strict tracked-document load accepts | Fresh reread additionally handles |
|---|---|---|---|
| Root | Carrier shape | A block or flow mapping, or an `!!omap` in either style | Nothing more; a root that loads as neither a mapping nor a null is refused, a plain sequence included, while a null root returns the file unchanged |
| Root | Key spelling | Exactly the `NodeMeta` keys, `id` required. A key may be written plainly, or as an explicit `? key` / `: value` pair. A key spelled through an alias needs one of two forms, the explicit `? *name` pair or `*name : value` with a space before its `:`, because the bare `*name:` form does not scan. Members may arrive through a `<<` merge in either spelling (a plain `<<`, or any key carrying an explicit `!!merge` tag) | Any extra key, which is tolerated and left alone; only `derives_from` is read |
| `derives_from` | Carrier shape | A block or flow sequence, written inline or supplied by a merge | Additionally one reached through an alias, which is not strictly reachable because the anchor needs a carrier key `NodeMeta` forbids |
| Entry | Carrier shape | A block or flow mapping, or an `!!omap` in either style, written inline or as an alias to a node elsewhere | Nothing more |
| Entry | Key spelling | `ref` and an optional `seen` and nothing else. Either key may be written plainly, or as an explicit `? key` / `: value` pair. A key spelled through an alias needs one of two forms, the explicit `? *name` pair or `*name : value` with a space before its `:`, because the bare `*name:` form does not scan. Members may arrive through a merge in either spelling | Any extra key, which is tolerated and preserved |
| Entry | Node properties | An anchor, a tag, or both opening the entry, including on the sequence line above and left of its first key | Nothing more |
| Entry | Member layout | Layouts an appended `seen` has to land after that need no member beyond `ref` and `seen`: the next indented item of the enclosing sequence, a trailing comment, a flow entry written with or without a trailing comma, or a `ref` spanning lines as a block scalar in either style, a multi-line double-quoted scalar, or a multi-line plain scalar | The same landing after an extra member, which the strict load forbids: a trailing block scalar, a multi-line flow collection, or an implicit or explicit null |
| Entry `ref` | Value | A control-free string (AD-35), which takes the same line-break-bearing spellings out of this column as the `seen` row below, interior breaks and trailing ones alike | Nothing more; a non-string `ref` is refused whenever planning is reached, while a control-bearing one is rewritten like any other |
| Entry `seen` | Carrier shape | A scalar or null, never a collection | Nothing more; a collection-valued `seen` at a targeted entry is refused |
| Entry `seen` | Scalar spelling | Any scalar spelling whose constructed value is a control-free string or null (AD-35): plain, single or double quoted, a block scalar with an explicit indentation indicator whose constructed value carries no line break at all: folded in any chomping mode where the value keeps no trailing break, or literal on a single line under the same condition, since chomping governs only the break at the end and a literal style keeps the breaks between its own lines, an explicit `null` or `~`, an empty value, an absent key, an alias to such a value, or an explicit `? seen` left without its `:`, which constructs null. How the key itself may be written is the `Entry` `Key spelling` row above | Any scalar the safe constructor accepts, whatever it constructs to, an explicitly tagged one included, a control-bearing value among them |
| Entry `seen` | Node properties | An anchor, a tag, or both, in either order, on the value's own line or on lines of their own, with the author's comments between a property and the value | Nothing more |

Four behaviors of the loaded shape are recorded with the matrix rather than inside a cell.
`!!omap` is handled wherever the loaded shape is a mapping, at the root and at an entry alike.
A merge is deliberately not followed inside an ordered map, because the loader builds one from its
items rather than through mapping construction. Alias detachment may expand an alias site into a
local mapping, or into a local one-pair item for an ordered map, rather than editing the shared
node behind it. An anchor name may be defined more than once, and the pure Python
parser warns about it: a later definition rebinds the name, so each alias reads the nearest
definition above it and a relocated value lands only on the alias sites still bound to the anchor
it displaces. That acceptance is unconditional, in both columns. It was not always: a plain safe
loader switches to the optional `ruamel.yaml.clib` accelerator wherever it is installed, and that
composer refuses a reused name outright as a duplicate anchor, so the strict tracked-document load
accepted the spelling in one environment and refused it in another while the reread inside
`apply_reconcile`, pure by AD-26, handled it in both. The strict load now asks for the pure parser
explicitly, which settles the spelling as supported rather than parser-conditional; AD-33 records
that decision and the alternative it was chosen over.

**Layer 2a: the envelope.** These are lexical rather than structural, and a declared version has a
constraint the matrix cannot show. The block opens and closes on a line whose stripped text is
exactly `---`, so space on either side of either fence is accepted, leading indentation included.
A leading run of UTF-8 byte-order marks may precede the opening fence; the whole run is stripped
for fence detection and reattached verbatim. A `%YAML` directive is supported, but only alongside
a document-start line that does not strip to `---`, because `frontmatter_parser.py` closes the
block at the first line that does. The directive's own document start therefore has to be spelled
otherwise, and `--- !!map` is the form the suite pins. A declared version governs scalar resolution
on both reads, not merely the strict one: under a declared 1.1 an unquoted `on`, `off`, `yes`, or
`no` constructs a boolean rather than a string, so a root `id` spelled that way fails layer 1
validation and the `Entry` `Scalar spelling` row's "constructed value is a string or null" is read
under 1.1 too. This was parser-conditional until AD-33, which records why it no longer is.

**Layer 3: preservation envelope.** For a document inside layer 2, a rewrite puts back exactly as
they were read: a leading run of byte-order marks, both `---` fences including the space around
them, the newline after the closing fence or its absence at end of file, the body, key order,
comments, indentation, collection style, and a declared `%YAML` version. A file written entirely
in CRLF, entirely in LF, or entirely in lone CR is read and rewritten in that same ending.
Preservation of everything the rewriter does not touch follows from the edits being byte local,
not from a whole-document guarantee.

**Layer 4: allowed mutation footprint.** Beyond the `seen` scalar itself, a rewrite may replace a
`seen` or insert one that is missing; write the `:` an explicit `? seen` key lacks; replace a
tagged `seen` together with its tag; drop the anchor and tag tokens of a replaced `seen` along
with the run of space they leave, and, when the value being replaced is the empty scalar, the line
break opening a line those tokens alone occupied; move a
block scalar's header comment onto the line the new hash is written on; relocate an anchor and its
old value to an alias site another key still reads through; land an edit at an alias site rather
than in a shared node, which may expand that site into a local mapping or a local one-pair item;
and normalize a file that already mixes line endings to LF across the whole document. That last
one is a document-wide mutation rather than preservation, which is why it belongs here and not in
layer 3.

The tag lifecycle differs between the two edits that touch one, so it is stated separately.
Arbitrary tags are not in play at either: planning is reached only after the safe constructor
accepted the document, so a tag no safe constructor knows fails the load first. **Replacement
consumes the tag**, because the tag belongs to the value being replaced and keeping it would
retype the new hash or reject it outright on the next read. **Relocation preserves the displaced
value's type**: an anchored `seen` re-emitted at an untouched alias site keeps its explicit tag
when the value is not a string, bool, or null, and a multi-line tagged scalar is re-emitted quoted
under that same tag so it keeps its type without keeping its lines; a string is re-emitted double
quoted with the tag dropped as redundant, and a bool or null in its own spelling.

**Layer 5: refusal posture, as three distinct behaviors.** The first is guaranteed refusal, and
each guarantee is scoped to how far the reread got, because there is no preflight classifier and a
shape the run never reaches is a no-op rather than an error:

- On any reread: an opening fence with no closing fence, frontmatter that does not parse, and
  frontmatter that loads as anything other than a mapping or a null. A null root, which an empty
  document and an explicit `null` or `~` both produce, is a no-op rather than a refusal.
- Once the root has loaded as a mapping, and before any planning: a `derives_from` that does not
  load as a list.
- Whenever planning is reached, meaning the document carries a `derives_from` list, and
  independent of whether any planned ref still matches: an entry that does not load as a mapping,
  and an entry whose `ref` is not a string.
- When the targeted entry matches an applicable update: a collection-valued `seen`. The same
  shape at an entry no update targets is never inspected, so it neither refuses the run nor is
  rewritten, while the updates the run does target still apply and rewrite the document around
  it, as `test_apply_reconcile_leaves_a_collection_seen_at_an_unmatched_entry_alone` pins.
- When at least one update is applied: self-referential frontmatter, which compares without bound
  and so cannot be verified; a rewrite that fails to reload; and a rewrite that does not reproduce
  the whole planned frontmatter, edges and every other key alike.

Returning the text unchanged is not a refusal and is the correct outcome for a file with no
opening fence, an empty document, a null root, an absent or null `derives_from`, no planned ref
still matching, or a ref already holding its planned hash.

The second is non-contractual defensive recovery that may succeed. The fresh reread tolerates
shapes strict validation rejects and may rewrite them rather than refusing: extra keys at the root
or in an entry survive untouched, and a `seen` whose constructed value is neither a string nor
null, arriving from a concurrent edit, is replaced or relocated under the tag lifecycle above.
That is recovery rather than refusal, and it is not a supported input either. An explicit tag is
not what puts a `seen` in this behavior, since a tagged scalar constructing to a string or null,
such as `!!str 12` or `!!null ~`, satisfies `RawEdge` and sits in layer 2's strict column; what
puts it here is the constructed type, whether it was reached through a tag or not.

The third is non-contractual input protected only by the equivalence gate. Syntax outside layer 2
that the rewriter never touches is unsupported, and is neither classified nor refused up front.
The gate compares loaded values, so it catches a rewrite that fails to parse or that reloads as an
unequal document. It is not type identity, and it promises no universal syntax or type
preservation for out-of-subset input.

**`!!omap` is a commitment, not an accident.** It carries dedicated lookup and planning behavior
for roots and for entries, with block, flow, and alias coverage in the reconcile source suite.
Dropping it later would be a compatibility decision of its own rather than a correction to this
record. Two ordered-map refusals are current bounded-loader behavior rather than guaranteed
refusals: a malformed ordered map, meaning an item that is not a one-pair mapping, and a merge
written inside one. The round-trip fuzz gate in `tests/test_reconcile_fuzz.py` pins both at the
project error type and its code, unconditionally, so either one changing turns CI red. That is
deliberate and it is what pinning means here: the behavior stays changeable, but only by editing
this record and its pin in the same change, never by a loader upgrade moving it quietly. Neither
becomes a refusal a user may rely on.

**Consequences:** "Is the rewriter complete" is now a finite question about this subset. A
spelling outside layer 2 is not a completeness defect, and admitting one is a deliberate widening
with the reconcile source suite as its evidence; removing a supported spelling, `!!omap` included,
is a compatibility decision. The standing maintenance obligation is that a writable-path spelling
the suite deliberately pins has to be named in layer 2 or its envelope, or explicitly classified
as unsupported, or this record silently stops being the declared subset. If deterministic refusal
of out-of-subset input is ever wanted, that is separate work rather than part of this decision.
RECONCILE.md keeps the user-facing operational consequences and links here for the normative
matrix.

### AD-32: The managed GitHub CI product retires to a documented recipe

**Date:** 2026-08-16
**Status:** Accepted
**Context:** AD-16 established `doc_lattice.github_ci` to render, audit, and refresh four
create-only artifacts around an external `gh` bootstrap, so a protected Linear gate could be
installed without this package ever holding GitHub administration credentials. The boundary that
design rests on is the GitHub environment, not the generator that produces files around it. After
the shell scanner left under AD-25, a generator, an offline auditor, a byte-level refresher, and a
bootstrap script remained in service of a product with zero installations, and the check and lint
half of what it produced is already what plain `init` scaffolds.
**Decision:** The managed product retires to a hand-installable recipe published in MANAGED_CI.md.
GTX-109 committed that recipe to the unreleased tree, and GTX-163 removed `init --github`,
`ci audit`, `ci refresh`, and the `github_ci` package during 5.0 development. No deprecation stage
ever shipped: 4.1.0 is the last release that carried the managed product and it carried it live,
so an adopter migrates directly from the 4.1.0 managed setup to the 5.0 recipe.

The recipe supplies the trusted Linear workflow as copyable text, because plain `init` does not
print it, together with the `gh` sequence that creates the `main`-only environment and its
dedicated secret. It keeps the boundary exactly: the environment allow list, the dedicated
`DOC_LATTICE_LINEAR_API_KEY`, final-step-only mapping onto `LINEAR_API_KEY`, the trusted job's
repository, ref, and event guards, the pinned actions, and the least-privilege token. It drops the
machinery that watched that boundary: repository-wide audit, drift detection, byte-level refresh,
the scripted remote readback, the ownership markers, and the script's guarded, resumable setup.

A 4.x deprecation stage was drafted and then overtaken. It would have been help text and
documentation only, because a stderr warning cannot be made compatibility-safe for a script that
already parses those channels, which is the reason AD-10 records; removal landed in the same
unreleased tree, so no release ever carried the notice.

Keeping the product was rejected because no installation justified maintaining four subsystems for
a boundary a documented procedure reaches directly. Removing the commands with no successor was
rejected because they are a published CLI contract. Publishing the recipe while leaving the
commands supported was rejected because it would leave two paths to the same boundary, one
unmaintained.
**Consequences:** The published workflow is security-sensitive project output rather than an
internal template, so SECURITY.md names it in scope and `tests/test_managed_ci_recipe.py` holds its
trigger set, guards, environment binding, secret mapping, and action pins. Those structural checks
were written to outlive the renderer they once cross-checked against, and they have. An installed
managed setup does not break when 5.0 ships: a generated workflow pins the exact version that
produced it and never hears that a later one exists, so an installation goes on running until
someone converts it, and pinning it forward is what fails, because its offline workflow invokes
`ci audit`. Conversion changes no remote state and is a local file-ownership change, which
MANAGED_CI.md owns and CHANGELOG.md announces. Removing the commands is a breaking change to a
published CLI surface and therefore a major version.
AD-16's environment boundary survives this record intact; only its generator side retired.

### AD-33: The strict frontmatter load pins the pure Python parser

**Date:** 2026-08-18
**Status:** Accepted
**Context:** Whether a document counted as tracked depended on which packages a user happened to
have installed beside this engine. `frontmatter_parser.py` performs the strict tracked-document
load through `yaml_boundary.SafeYamlLoader`, which asked ruamel for a plain safe loader and so
took whichever parser ruamel picked. ruamel picks the C one whenever the optional
`ruamel.yaml.clib` accelerator is present, and no lock of this project installs it but any other
package in an environment may pull it in. The two parsers do not accept the same documents. A
frontmatter block defining one anchor name twice is accepted by the pure parser, which warns and
rebinds the name, and refused outright by the C composer as a duplicate anchor. So the same file
was a tracked node on one machine and an unreadable document on another, with nothing in the
project's own declared dependency range having changed, and `check` reached different verdicts for
it. AD-31 recorded the split as observed rather than deciding it, and the suite carried a runtime
capability probe that routed the shape between its strict and reread-only pools so both legs of the
`yaml-compatibility` matrix would pass over the disagreement rather than fail on it.
**Decision:** `SafeYamlLoader` takes the parser implementation as a required keyword argument with
no default, and applies it at every construction, including the replacement `load` builds when a
`%YAML` directive forces the reset. `frontmatter_parser.py` asks for the pure Python parser, so
the set of documents that count as tracked is fixed by this project rather than by an adopter's
environment. `config.py` asks for the default, deliberately: config has no declared spelling subset
and no rewriter reading it back, so a parser disagreement there costs a config author one clear
error rather than changing which documents the lattice holds, and this record leaves its semantics
alone rather than widening the change to a second boundary that did not need it. That is a scope
choice, not a claim that config is parser independent: a `.doc-lattice.yml` defining one anchor
name twice is still a `ConfigError` wherever the accelerator is installed and loads cleanly
wherever it is not.

Reused anchor names are therefore supported, not refused. YAML 1.2.2 permits a non-unique anchor
name and resolves an alias to the most recent preceding definition, `reconcile.py` already
implements and tests that rule on the reread path, and AD-31 layer 2 already listed the spelling.
Refusing it in both parsers was the alternative, and it was rejected because it would settle an
environment split by shrinking a valid, already-modeled input surface down to what the weaker
parser can read. Accepting the split and documenting it was not a candidate: it is the state this
record replaces.

Warning behavior is part of the decision rather than a side effect of it. The pure parser raises
`ReusedAnchorWarning` on the spelling it accepts, and the engine stays loud about the rebinding
rather than swallowing it, at a boundary whose whole job is to report what a document says. What
changes is who reports it. Preserving ruamel's own warning verbatim was the first form of this
decision, on the grounds that it kept the strict load's observable behavior identical to an
accelerator-free environment's. That form is rejected, because the warning is a diagnostic a load
emits and AD-29 already governs those: it has to be derivable from an `Entry` and rendered at the
shared site, or the warm path will not reproduce it. Ruamel's warning is neither. It is raised
from inside the composer, so it names `<unicode string>` and a block-relative line rather than the
document, and a `CacheHit` returns before `parse_meta` runs at all, so a corpus loaded from a warm
cache went silent about a rebound alias while still building the edge it rebound. Under the
accelerator that is a loss of reach as well as of fidelity: a reused anchor used to refuse the load
on every run until it was fixed.

So `parse_meta` captures the warning, returns the fact on `ParsedMeta`, and `orchestrate.py`
reports it from a single site against the path the run discovered, exactly as it reports an id-less
skip. `Entry` carries it as a required field and `CACHE_VERSION` rises with it. Every other warning
raised by a load that returns is re-emitted at its original location, so only this one category is
intercepted. Three costs are real and accepted. The strict load's stderr is no longer
byte-identical to what an accelerator-free environment printed before this record: the text names
the file now, which is the point. It no longer matches the reread inside `apply_reconcile`, which
still lets ruamel's warning escape, because that path builds its own loaders per AD-26 and has
neither a discovered path to name nor a cache entry to write. And the reported category changes
from `ReusedAnchorWarning` to the `UserWarning` every diagnostic this engine raises carries, so an
embedder that escalates `UserWarning` to an error now fails the load on a document the pure parser
merely warned about, and a filter naming ruamel's category no longer reaches this diagnostic.
Carrying ruamel's category here is rejected for the same reason its warning is: nothing ruamel
raised survives a warm cache hit, so a category borrowed from it would claim a provenance the
replay does not have. Targetability is by message prefix under AD-29, which is why the wording of
each site is chosen to be distinct, and the id-less skip and the symlink escape are already plain
`UserWarning`; giving this one site a category the other two lack would fragment that surface
rather than stabilise it.
**Consequences:** Which files count as tracked is user-visible, so this is a breaking change and
lands in a major. An adopter running with the accelerator installed sees a document that used to
fail the load become a tracked node, which can add edges to a report and change a `check` exit
code; an adopter without it sees nothing change at all. The reused anchor name is not the only
spelling that moves there. A `%YAML` directive, which layer 2a declares supported alongside a
document start that does not strip to `---`, took no effect at all under the accelerator, so a
block heading itself `%YAML 1.1` resolved under 1.2 on the strict read while the reread inside
`apply_reconcile` resolved it under 1.1. Pinning the parser settles that disagreement in the
reread's favor, and settling it is user-visible in both directions: `id: on` under a declared 1.1
now resolves to a boolean and fails validation where it used to be the string `on` and made a
tracked node. Every such document was already being reread under 1.1, so the alternative was
leaving the two reads disagreeing about the same bytes. The strict load gives up the accelerator's
speed on frontmatter, which is a per-document cost on a parse of a block that is small by
construction. The `yaml-compatibility` matrix keeps both `clib` legs, and they now assert the same
verdict rather than two: the capability probe and the conditional corpus routing in
`tests/test_reconcile_fuzz.py` are gone, and the reused-anchor shape is a strict-column shape on
every leg. A future divergence between the two parsers reaches only `config.py`, which is the one
boundary still declared as taking ruamel's default. Routing the reused-anchor warning adds a second
cached diagnostic beside AD-29's disposition, so `CACHE_VERSION` rises to 5 and caches written
before it are discarded and rebuilt rather than read as files that reused no anchor.


### AD-34: A path is escaped where the message is built, not where it is printed

**Date:** 2026-08-18
**Status:** Accepted
**Context:** A document path is a repo-controlled string that reaches human-facing output without
passing the frontmatter parser at all. The parser refuses a literal C0 byte in the YAML source
stream, so validated frontmatter values were the vector anyone looked at; a filename never went
through it. Executing `check` under `NO_COLOR=1` against a file named `pwn<ESC>[31m<ESC>[Aevil.md`
put the raw `0x1b` bytes on stderr in both the `UNREADABLE_DOC` line and the id-less skip, and
`ESC[A` moves the cursor up a line, so a crafted filename could overwrite a diagnostic printed
before it. README's `--no-color` section already owns the escape-free output contract, and its
scope already reaches this output, so this was an unmet existing contract rather than a new
extension. That section stays the single statement of what is promised; this record covers only
where the escaping happens and why.

Two renderers print these strings, and both leaked. `cli/errors.py::print_project_error` and the
warning renderer in `cli/runtime.py` each apply `rich.markup.escape`, which neutralizes `[tag]`
markup and does nothing whatever to ANSI, and the Console does not strip control codes for these
strings either. Repairing either renderer would have left the other, every direct console write
that is neither an error nor a warning, and every direct library consumer that formats a
`ProjectError` itself.

`strip_control_chars` in `text_utils.py` was the obvious reuse and is the wrong tool. It *deletes*
controls, so `ESC[31m` and a literal `[31m` render identically, and a display spelling that maps
two distinct filenames onto one string is not a diagnostic anyone can act on. It stays scoped to
the network-sourced Linear data and `init` input it was written for, and its consumers are
unchanged.

**Decision:** Escaping happens at message construction. `path_utils.format_path_for_display`
returns exactly `repr(str(path))` on the active supported interpreter, and every human-facing sink
that names a path calls it while building the message. The raw `Path` remains the value the engine
opens, compares, and writes; the display spelling exists only for text a person reads. Machine
channels keep their own encoders: JSON output and the GitHub annotation `file=` value are
deliberately excluded, because substituting a display spelling into an annotation's path breaks
the attachment semantics GitHub resolves it against.

The spelling is pinned to a single expression rather than a project-owned codec because
`str.__repr__` is already injective, and injectivity is what turns "no two filenames render alike"
into a property the suite can check instead of an argument it has to make. The exact escapes are
CPython's: `\t`, `\n`, and `\r` get their named spellings, every other C0 code point plus DEL and
the C1 range get `\xNN`, literal backslashes double so a filename cannot forge an escape,
undecodable filename bytes render as their `\udcNN` surrogates without raising, and printable
non-ASCII survives verbatim, so `café/naïve.md` reads as `'café/naïve.md'`.

**Consequences:** Every path in human output is now quoted, and the quote character varies:
CPython picks single quotes unless the string holds a single quote and no double quote. That is
accepted rather than normalized, because pinning the delimiter would mean owning a codec, and the
spelling is injective under either choice. The contract is therefore "the active supported
interpreter's `repr(str(path))`", not a byte-identical guarantee across Python implementations;
`pyproject.toml` pins 3.13+ without constraining the implementation, and exact quote selection is
not a portable semantic the language documentation promises. What the suite asserts independently
of the interpreter is what actually matters here: injectivity, and that no C0, DEL, or C1 code
point survives into output.

Two visible outputs move as a result. `impact`'s human report prints its path quoted inside the
parentheses it already used, and `reconcile`'s success line quotes the basename it names; both
README examples are updated in this change rather than deferred, since `CLAUDE.md` requires a
behavior change to update its owner.

The warning message prefixes are untouched. AD-29 records that `PYTHONWARNINGS` targetability and
cold/warm cache parity both rest on the exact opening of each warning, so `skipping ` and `reused
anchor in ` keep their spelling and only the path inside the message changes.

This closes the document-path half of the repo-controlled vector. It is not the whole of it: YAML
decodes a double-quoted `\u001b` into a real ESC, so `id`, `title`, `tickets`, `ref`, and `seen`
can each carry a control character into human output through a validated frontmatter value. That
vector turns on a decision this record does not make, between rejecting at validation and a typed
value display encoding, because those values participate in identity and in structured output;
it is GTX-208's, and AD-35 settles it by refusing such a value at validation rather than adding a
sibling display spelling here. The reconcile transaction and recovery sinks interpolated
destination, journal, and staged-artifact paths the same way this record governs, and stage names
inherit `destination.name` so a hostile document filename propagates into them; GTX-209 routed
those sinks through this helper rather than deciding a spelling of their own, and moved
`path_utils.safe_resolve`'s own containment error with them, because the transaction layer embeds
that message verbatim when a journal records an escaping path. GTX-125 and GTX-209 together close
the document-path vector; nothing here claims the repo-controlled vector is fully closed.

A static guard in `tests/test_conventions.py` enforces the boundary going forward: inside the
modules this record covers, a path-bearing name reaching human-facing text must go through the
helper. The failure mode being guarded is an omitted construction site, which a per-sink
behavioral list cannot catch for a sink that does not exist yet. Three things make it able to see
the sinks it covers. It reads through wrapper calls and whole attribute chains rather than only
the top-level expression, because the sink this change repaired was spelled `{escape(path.name)}`
and a top-level test sees a call there and reports nothing. It scans bare `escape(...)` calls
and not only f-string interpolations, because the reconcile adapter formats its basename once
above the loop that prints it, so the raw path never reaches an f-string and an interpolation-only
scan calls that sink clean however it is spelled. It also scans `", ".join(...)`, because the
transaction layer lists unresolved destinations by joining them above the message that carries
the result, which is a third place a path enters text outside an f-string.

The guard holds no whole-module exemption. GTX-209 retired the `reconcile_transaction.py`,
`persistence.py`, and `path_utils.py` entries, and GTX-212 retired the last three -- the config,
cache, and `init` paths, which had been parked here as strings carrying no repo-controlled
*document* filename. That reasoning was true and beside the point: `--config` and `--docs-root`
are user-supplied, and README promises escape-free human-facing output rather than escape-free
document paths, so the promise was unmet at those sinks. What remains is per-expression and
machine-only: the journal serializer, the staged-artifact filenames, and the recovery payload's
own JSON spelling. Every config, cache, and `init` diagnostic now quotes the path it names, the
way `impact` and `reconcile` already did. Because a name can be a path in one module and
something else in another, the path-bearing name set is the global one widened per module
rather than grown globally.

Retiring the module exemptions is not the same as enforcing the boundary, and GTX-212 closed two
gaps that made the difference. Both scans used to `continue` past a named module, so re-adding
one silently switched the scan off for that file while every assertion still passed; the scan now
reports which modules it read and the production test asserts that set is every source module, so
a reintroduced skip fails rather than hides. And the detector's name vocabulary did not cover the
operands those modules spell, so dropping the entries alone would have caught three of the
thirteen operands that needed the helper: the two locals literally named `path` in `config.py`,
and the cache store's own. The configured `config_path`, `entry`, `project_root`, and `safe`, the
header's aliased `source`, and every `init` operand were invisible to it. An alias defeats it the
same way: binding `where = str(source)` above a message removes the path-bearing name before the
scan reaches it, which is why the config validation header is built in two branches whose guarded
expression is the helper call alone.

What the guard reaches is therefore bounded by the vocabulary rather than by the module list.
Retiring the module exemptions widened where it looks, not what it recognizes: a name that is not
classified for a module is still invisible in it, and a module nobody has audited contributes
whatever its own operands happen to be named. Widening a name globally is the fix where the name
is unambiguous, and it is a scope decision rather than a free one, because it reports every
existing sink spelling that name at once.

GTX-238 is the first widening taken under that reading, and the scope choice rather than the
wrapping is what makes it worth recording. Six lock diagnostics in `reconcile_transaction.py`
interpolated a project root raw in a module the scan already read, so vocabulary was the entire
gap. `project_root` is a `Path` parameter or the path-typed `ProjectConfig.project_root` at
every production spelling, so its meaning does not change by module and it belongs in the global
set; the `config.py` entry that had scoped it is removed with the promotion, which leaves one
statement of the decision rather than two that can drift apart. `requested_root` stays scoped to
the transaction module, since the comparison that audits it as a path exists only there.
`lock.project_root` needs no entry at all, because the detector tests every component of an
attribute chain -- coverage the suite now asserts rather than inherits. The blast radius was
measured before the promotion was taken rather than after: the widened detector reports exactly
the seven operands at those six sites and nothing else, so a global name that could have
reported sinks nobody had audited reported none. Output moves the way GTX-125's and GTX-212's
did, and these lines quote the root they name.

One classification is worth recording because it is not about safety. `init` names the config file
it scaffolds by a compile-time constant, so wrapping it neutralizes nothing today, and quoting it
moves three lines of visible output. It is classified and wrapped anyway, because the alternative
is unenforceable: display exemptions are keyed by (module, qualified function, expression), and
the machine-side staged-file prefix shared both its function and its spelling with the two human
messages beside it, so one exemption would have covered all three. The prefix is built from
`DEFAULT_CONFIG_NAME` directly instead, which leaves every remaining use of that name human-facing
and wrapped, and makes the next widening of it -- to a full path, or to a name supplied by a flag
-- a guard failure rather than a silent pass.


### AD-35: A frontmatter value carrying a control character is refused, not re-spelled

**Date:** 2026-08-19
**Status:** Accepted
**Context:** AD-34 closed the document-path half of the repo-controlled output vector and left the
other half open, because the two halves do not turn on the same question. A path is display-only,
so a display spelling settles it. A frontmatter value is not: `id` and `ref` participate in
identity, since edge resolution and duplicate detection compare them, and all five of `id`,
`title`, `tickets`, `ref`, and `seen` participate in structured output through JSON, the GitHub
annotation encoder, and the `linear` command.

The premise that the frontmatter parser already filters control characters was half true, and the
half that is true is narrower than it first reads. Executing `check` and `graph` under `NO_COLOR=1`
against a block spelling `id: "node\u001b[31m"`, `title: "t\u001b[2J"`,
`tickets: ["GTX-1\u001b[31m"]`, and `derives_from: [{ref: "up\u001b[A"}]` put the raw `0x1b` bytes
on stdout in both. `NodeMeta` and `RawEdge` were otherwise unconstrained strings under
`strict=True`, with the `#` check on `id` as the only value rule.

Exactly which spellings reach a value is worth recording, because "YAML refuses control bytes" is
the belief this vector hid behind, and it is wrong in one place. ESC, DEL, NUL, and the C1
controls are refused as raw bytes by the scanner in every scalar style, so a double-quoted escape
is the only way to write one of those into a value. A literal carriage return or NEL is read as a
line break and folds to a space, so it never reaches a value either. A literal **tab** does: the
scanner admits it inside a double-quoted, single-quoted, or block scalar, where it constructs
`U+0009`. The refused set is therefore reachable by escape for most of its members and by a raw
byte for exactly one, and a rule written only against escaped spellings would have missed the tab.
`tests/test_frontmatter_parser.py` pins this table so it stays a measured fact rather than a
belief, since it is the belief that hid the vector in the first place.

The sinks are the same shape AD-34 records for paths and are more numerous: `report_render.py`
prints `source_id`, `target_ref`, `node.id`, and `tickets`; `render.py` turns ids and titles into
graph labels behind a quote-only grammar escaper per format; the reconcile adapter prints
`target_ref`; and `reconcile.py`'s targeted broken-ref error interpolates `node_id` with no
escaping at all. Every one of them applies `rich.markup.escape` at most, which does nothing to
ANSI, or nothing.

**Decision:** These values are refused at validation rather than re-spelled at display.
`ControlFreeStr` in `model.py` is `str` plus an `AfterValidator`, and `id`, `title`, every
`tickets` element, `ref`, and `seen` are typed with it, so a document spelling any C0 code point
(`U+0000` to `U+001F`), DEL (`U+007F`), or any C1 code point (`U+0080` to `U+009F`) in one of
them is a `FRONTMATTER_ERROR` and never becomes a node. The predicate is `text_utils`'
`is_control_char`, which is the same range `strip_control_chars` has always removed, so the
project has one definition of what a control character is rather than two. Tab, newline, and
carriage return are C0 controls and are included deliberately; see the consequences below.

Refusing is what a display encoding could not do here. It closes the vector at one boundary
instead of at each of eight sinks and every sink added later, it keeps control characters out of
identity rather than only out of the rendering of identity, and it needs no rule about how a
display spelling composes with the DOT and Mermaid grammar escapers, which is a question AD-34
does not have to answer because a path is never a graph label. The alternative was a string-typed
sibling of `format_path_for_display` plus an analogue of AD-34's static construction-site guard;
that guard exists because an omitted sink is the failure mode of a display strategy, and the
strategy chosen here has no sinks to omit.

The diagnostic names the code point and its index rather than echoing the value, because a
message quoting the value would print the very byte the rule refuses. The `ControlFreeStr` rule
runs ahead of the `id` `#` rule, which does quote the id it rejects, so an id carrying both is
reported by the rule that names no value.

That the refusal itself stays clean took one more site than it first read, and the extra one is
recorded here rather than left as a footnote, because the reasoning that missed it is the same
reasoning this record exists to replace. `validation_render` drops pydantic's echoed input, and
the first version of this paragraph concluded from that alone that both halves held. They did not:
safe YAML decodes a double-quoted `\u001b` in a mapping **key** exactly as it does in a value, and
a key rejected by `extra="forbid"` is reported as the pydantic error *location*, which the
renderer joined verbatim. A block spelling `"bad\u001b[31m": 1` therefore put a raw ESC on stderr
through the message refusing the key, at both load boundaries, since `config.py` and
`frontmatter_parser.py` share the renderer. Refusing the key was never the gap; naming it was.
`_format_location_part` spells a location part with `repr` when, and only when, that part carries
a control character, so a rejected key is neutralized while an ordinary location still reads
`derives_from.0.ref` rather than gaining quotes around every segment. Rejecting control-bearing
keys at load instead would have been circular, because the new refusal would still have had to
name the key. The spelling is AD-34's, and injective for AD-34's reason; the difference is that a
path is untrusted whole while a location is untrusted in exactly one part.

That reasoning is not about YAML, and GTX-227 extends it to the third file this project validates
against a strict model: the reconcile journal. `reconcile_transaction.py` reads JSON rather than
YAML, and it validates against versioned wire models rather than one model, but neither difference
reaches the premise. JSON spells `\u001b` in a mapping key exactly as safe YAML does, and every
journal model is `extra="forbid"`, so a hand-edited key arrived on the terminal through
`_invalid_journal_error`, which interpolated `str(ValidationError)` as a clause of one sentence.
The journal boundary therefore shares `validation_render` rather than growing a renderer beside it;
a second copy of this rule would be a second thing to keep right, and it is the duplication the
module's own docstring exists to prevent. Two things do follow from the boundary's shape. The model
is bound at each of the three validation stages -- the lax version probe, v1, and v2 -- because the
accepted-key help is answered from the model that rejected the text, and naming one model for all
three would offer a v1 journal the keys only v2 accepts. And the remediation sentence is appended
as its own line rather than interpolated, because the renderer returns a header plus one line per
error, so a suffix joined to that block would read as part of the last error's message rather than
as a property of the failure. `_invalid_journal_error` is unchanged for the project-constructed
causes it still serves: an `OSError`, a containment or path-role refusal, and the
unsupported-version refusal, none of which carry author text.

Machine channels are excluded on the same reasoning AD-34 records, and the exclusion means
something different under this decision: JSON output and the GitHub annotation encoder are
unchanged for every document that still loads, and a refused document fails uniformly before
format selection rather than reaching one channel and not another. Keeping control-bearing values
reachable through the machine channels was never a requirement; had it been, it would have
preselected display-time encoding.

**Consequences:** This is a breaking change, and it is taken inside 5.0's deliberate window while
adoption is internal (ROADMAP.md). A document accepted today whose `id`, `title`, `tickets`,
`ref`, or `seen` constructs a control character becomes a load error, and because a load error is
reported per document rather than per command, it fails `check`, `lint`, `impact`, `graph`, and
`reconcile` alike.

Including tab and newline is what makes the rule worth having and is also its whole compatibility
cost. A newline is not a terminal escape sequence, so the `--no-color` promise alone would not
have reached it; the output it corrupts is line-oriented, and a `title` or `id` carrying one can
forge a whole report row rather than merely recolor a real one. AD-34's own spelling escapes
`\t`, `\n`, and `\r` in a path for the same reason, and a rule that refused a newline in a
filename while admitting one in an id would be incoherent across two halves of one vector.

Those two are also where the compatibility cost actually falls, and they fall differently. The
tab is the reachable-as-a-raw-byte case recorded above, so a document carrying one carries it
invisibly: nothing on screen distinguishes a tabbed `title` from a spaced one, and CHANGELOG's
migration note therefore gives a byte-level search rather than telling a reader to look. The
newline is reached through a spelling instead, and that spelling is what the next paragraph
covers.

The cost lands on AD-31 layer 2, which this record narrows along two independent axes rather than
the one it first appeared to. That table's strict tracked-document column accepts, for `ref` and
for `seen`, "a block scalar in either style with any chomping or explicit indentation indicator".
The first axis is chomping: clip and keep construct a trailing line break, so those two modes are
no longer in the strict column for those two positions. The second is style, and it holds
whatever the chomping is: a literal block scalar keeps the breaks *between* its own lines, so a
multi-line `|-` constructs an interior newline and is refused as well. Only the folded styles join
their lines with a space. What survives for a value written across lines is therefore `>-` alone,
and `|-` survives only on a single line. Stating this as "strip chomping is unaffected" would have
been wrong, and was: chomping governs the break at the end, never the ones in the middle. The
reread column
beside it does not move at all: `apply_reconcile` still rewrites such a document byte-correctly
and still re-emits the value as an escape, which is what the two columns exist to distinguish.
Only one of the affected spellings had no working use: a `seen` ending in a line break can never
equal a content hash, so such a document was permanently drifting and is now refused with a
diagnostic that says why.

The rest did have one, and the reasoning that said otherwise is recorded because its premise was
sitting inside its own conclusion. It ran: a `ref` ending in a line break can never resolve, since
every id in the index comes from a frontmatter `id` or a heading slug. A heading slug carries no
break, but a frontmatter `id` is a value of exactly the kind under discussion, and `id: |`
constructs one. An upstream naming itself that way and a downstream pointing at it the same way
construct the same string, so the id registered, the ref resolved against it, and the edge
reconciled to OK. Verified by execution against the pre-change tree rather than reasoned about,
which is what the earlier claim needed and did not get. A folded `title` is not the one plausible
casualty, then, but the most likely of several: any of `id`, `title`, `tickets`, and `ref` could
carry a break and work, and CHANGELOG's migration scan covers all five keys rather than the two
that hold hashes and refs.

The load cache needs no separate rule. `NodeMeta` is nested inside a cache entry and an invalid
snapshot is discarded whole, so a slot written before this change cannot replay a control
character into a warm run.

This record did not close the repo-controlled vector on its own, and the part it left open turned
on a decision it does not make. Both halves settled here assume a control-bearing string is either
refused at validation or spelled where a message is built. A YAML **load failure** satisfies
neither: it aborts before validation runs, and its message is built by `ruamel` rather than by this
codebase. `ruamel`'s duplicate-key error echoes the offending key and both of its values back at
the reader, and four sites interpolated that message verbatim: `frontmatter_parser.py`'s and
`config.py`'s parse failures and `reconcile.py`'s two. The value half is what made it more than
cosmetic, since a duplicate key defeats this record's own guarantee by failing the load before the
value rule can run. Closing it meant choosing how to spell an untrusted third-party message whose
own line structure is part of the diagnostic, which is why it was GTX-219's rather than an
extension of this one. AD-37 settles it by spelling the whole message with this record's own
`repr`, accepting a flattened syntax error as the price, so that vector is closed and README's
frontmatter section is no longer scoped to a block that loads.

No static construction-site guard accompanies this record, and none is owed. AD-34 needs one
because a display strategy is only as complete as its sink list; a validation rule has one site,
and the parser matrix over the five value families is what pins it.


### AD-36: Journal provenance is spelled for display, because it is informational

**Date:** 2026-08-19
**Status:** Accepted
**Context:** GTX-126 made the reconcile journal record a `created_at`, a `tool_version`, and a
typed selector, and GTX-196 surfaced them through `reconcile --recover`. That put two new
untrusted strings into human output. A journal is a file a person can edit, so `tool_version` and
the selector's `downstream_id` and `ref` can each carry a terminal control byte the way a document
filename can.

AD-34 and AD-35 answer that question two different ways, and this record is as much about which
of them applies as about the strings themselves. The axis between them is not how untrusted a
value is; both are equally untrusted. It is what else the value does. A path is refused nowhere
because the engine has to open it, so AD-34 spells it at the message. A frontmatter value
participates in identity and in structured output, so AD-35 refuses it at validation and closes
the vector at one boundary rather than at eight sinks.

Provenance sits at the far display-only end of that axis. Recovery never reads it: it is captured
so a report can name what produced a crash journal, and the rollback is described entirely by the
entries and their SHA-256 fingerprints. Two options were weighed and rejected. Resting on AD-35's
upstream refusal does not hold, because that rule runs at frontmatter load and a hand edit reaches
the journal directly, at a different boundary and in a different file. Extending AD-35's refusal
to the journal wire model was the closer call and was rejected on two grounds. It would make an
inert field a reason to strand a rollback the entries still fully describe, and because automatic
pre-run recovery gates every ordinary `reconcile`, refusing would stop the command outright until
an operator restored by hand. And it would not buy what it appears to: the v2 model establishes
required structure and types, not authenticity, so it already accepts an invented `tool_version`
or a selector naming a run that never happened. A rule that refused a control byte while accepting
those is output-driven validation wearing an integrity argument, and it would give journal-sourced
strings two policies in one output block, since GTX-209 already re-spells the journal path, every
unresolved destination, and every orphaned artifact rather than refusing the file that named them.

**Decision:** `tool_version` and the selector strings take AD-34's spelling at the sink that
prints them: `repr`, then `rich.markup.escape`. Both halves are load-bearing and neither
substitutes for the other. `repr` is injective and leaves no C0, DEL, or C1 code point in the
line, for the reason AD-34 records. The Rich escape covers markup, which is printable text that
no control-character rule would catch and that the Console would otherwise interpret. The
journal's own validation is unchanged: no control-free type is added to the wire model.

The quoting `repr` brings does a second job here that it does not do for a path. A selector field
the run left unset prints as a bare `null`, so quoting is what keeps it distinct from a recorded
string spelling that word, which prints as `'null'`.

`created_at` needs no display spelling, because `UtcTimestamp` has already made it a datetime
rather than text by the time anything reports it. It does need a chosen spelling, since the wire
format accepts both `Z` and `+00:00` and retains neither token. `journal_timestamp_text` emits the
`Z` form, and the test pins it against the serializer's own output rather than against a literal,
so a journal and the report about it cannot drift apart.

The machine channel is excluded on the reasoning AD-34 records, and here the exclusion costs
nothing on the terminal-safety axis: `json.dumps` escapes every non-printable code point under its
default settings, so the payload preserves the recorded value without ever emitting a raw control
byte.

**Consequences:** A hand-edited or crafted journal recovers normally and shows its tampering in
the report instead of failing the command. That is the intended trade, and it is the same one
GTX-209 already made for the paths in the same file.

No static construction-site guard accompanies this record either, and for a different reason than
AD-35's. A display strategy is only as complete as its sink list, so AD-34's guard is earned by
having many sinks across many modules; provenance has exactly one renderer in one adapter, and the
behavioral tests over it are what pin the spelling.

This record covers the provenance display path and nothing else. The journal's own load diagnostic
is a separate sink, and it was open when this record was written: `_invalid_journal_error`
interpolated a pydantic `ValidationError` verbatim, and while that formatter renders a rejected
*input value* through `repr`, it renders an error *location* raw, so a control-bearing extra key in
a hand-edited journal reached stderr unspelled. **GTX-227 closes that half**, and it closes it under
AD-35 rather than by extending this record: the journal's wire-model failures now render through
`validation_render`, which is where `_format_location_part` already spells exactly that shape.
Nothing here moves. The wire model still refuses no control byte, provenance is still spelled at the
sink that prints it, and the axis this record states is what sends the two halves to different
answers -- provenance is a value the journal records, while a rejected key is the diagnostic's own
naming of what it refused.


### AD-37: A YAML load failure's message is spelled whole, at the sink that reports it

**Date:** 2026-08-19
**Status:** Accepted
**Context:** AD-34 closed the document-path half of the repo-controlled output vector and AD-35
closed the typed-value half. Both rest on the same premise: a control-bearing string is either
refused at validation or spelled where a message is built. A YAML **load failure** satisfies
neither. It aborts before validation runs, so AD-35's rule never fires, and its message is built
by `ruamel` rather than by this codebase, so there was no construction site of ours to apply
AD-34's spelling at.

The message echoes document content back at the reader. `ruamel`'s duplicate-key constructor error
quotes the offending key and both of its values, and four sites interpolated it verbatim:
`frontmatter_parser.py`'s and `config.py`'s parse failures and `reconcile.py`'s post-edit reparse
gate and load site. Verified by execution: a block spelling `"k\u001b[31m": 1` twice put raw
`0x1b` bytes on stderr through the key, and a block spelling `k: "v\u001b[31mA"` and
`k: "v\u001b[31mB"` put them there through the value. The value half is the one that matters,
because it defeats AD-35's guarantee outright: the rule promises such a value never becomes a node
and never reaches output, and a duplicate key gets it printed by failing the load before the rule
can run. Everything else probed at the same boundaries was already clean, the raw-byte scanner
error included, which names a code point (`unacceptable character #x001b`) rather than echoing the
stream.

Which of the two existing answers applies is settled by the axis AD-36 states, and this record is
its third application. What decides between refusing a string and spelling it is not how untrusted
the string is but what else it does. A load failure's message is display-only: it participates in
no identity, and in no structured output either, because a document that fails to load fails
uniformly before format selection, so no admitted document's JSON or GitHub annotation moves.
Refusing is not available in any case, since the thing that would be refused is the diagnostic
itself.

Two alternatives to spelling the whole message were weighed and rejected. Escaping control
characters while preserving the message's own line breaks keeps an ordinary syntax error readable,
but once `ruamel` has joined its context, marks, problem, and note into one string, a newline it
wrote and a newline decoded out of an echoed value are the same character. AD-35 refuses a newline
in a frontmatter value precisely because it can forge a report row rather than merely recolor one,
and preserving every break here would weaken that invariant at a different boundary. Rebuilding
the message from `MarkedYAMLError`'s structured parts keeps both properties and was rejected on
coupling: each string mark embeds its own source snippet, so a safe reconstruction owns mark and
snippet rendering rather than quoting `problem`, and `YAML_LOAD_ERRORS` includes non-marked
builtins that would need a fallback. That surface is exercised across ruamel 0.18.0 and 0.19.x and
both parser configurations, which is a large compatibility cost for a readability optimization.

**Decision:** `yaml_error_render.format_yaml_error_for_display` returns exactly `repr(str(exc))`,
and all four handlers call it while building their own message. Each caller keeps its
domain-specific header, so what a file is and why it was being read stays this project's prose and
only the third-party detail is spelled. The spelling is AD-34's, pinned to a single expression for
AD-34's reason: `str.__repr__` is already injective, and injectivity is what makes "no two
messages render alike" checkable rather than arguable. It needs no codec of its own, because that
`repr` escapes every C0 code point, DEL, and the C1 range.

The renderer is a module of its own rather than a function on the load boundary. `yaml_boundary.py`
owns the load mechanics and deliberately not the caller's error policy, and how a failure is
reported is policy; `validation_render.py` is the existing precedent for a display module beside
the boundaries that share it. Machine channels are excluded on the reasoning AD-34 and AD-35 both
record, and the exclusion costs nothing here, since a load failure never reaches format selection.

**Consequences:** An ordinary YAML syntax error now reads as one quoted line with its line breaks
written as `\n`, and that cost is paid by the common case to close a rare one. The loss is
positional rather than aesthetic, and worth stating precisely: a marked `ruamel` error renders a
source snippet with a caret under the offending column, and flattening the message leaves that
caret pointing at nothing, so that part of the diagnostic becomes noise. What survives is the
textual `line: N, column: M` coordinates, which still locate the failure, and the message stays
lossless: nothing is dropped, so the original wording is recoverable by reading the escapes.

A static construction-site guard in `tests/test_conventions.py` accompanies this record, and unlike
AD-35's it is owed rather than optional. A display strategy is only as complete as its sink list,
which is AD-34's own rule, and this strategy has sinks. The guard is keyed on every `except` clause
in production code that names the load-failure family, the shared `YAML_LOAD_ERRORS` alias and the
`YAMLError` base type alike and in either the bare or the attribute spelling, requiring either a
call to the renderer inside the handler or membership in a named exemption list. It also requires
that the bound name reach nothing but that call and the chained `raise ... from`, so a handler with
two exits cannot spell one and interpolate the exception raw down the other. Keying it on the
clause rather than on whether the clause binds a name is deliberate: `reconcile.py`'s round-trip
probe binds none and would have been exempted structurally, and so would every future handler that
swallows the family. That probe is the list's single entry, and its own comment records why a
failed probe answers with the quoted form instead of reporting anything. The detector is
self-tested the way AD-34's is.

The suite pins the readability cost as a relation to the caught exception rather than as a literal
string. `ruamel`'s wording differs across the releases and accelerator cells CI runs, so a fixed
expected message would pass in one dependency cell and fail in another, which is why
`tests/test_reconcile_fuzz.py` already pins only this project's own prose. What is asserted is
that the rendered detail is the chosen spelling of whatever was caught, and that no C0, DEL, or C1
code point survives.

This closes the third and last of the repo-controlled strings 5.0's diagnostics group names.
README's frontmatter section is no longer scoped to a block that loads, and AD-35's remainder note
is answered by this record.

### AD-38: The escape-free promise is narrowed, because the annotation channel cannot be spelled

**Date:** 2026-08-19
**Status:** Accepted
**Context:** AD-34, AD-35, and AD-37 closed three repo-controlled strings, and each one left
README's `--no-color` promise stronger than it found it. That promise was written unqualified:
no terminal escape sequence reaches the output under either lever, covering every command's
output. AD-34 had already excluded the GitHub annotation `file=` value from the display spelling,
so the two documents disagreed about the same channel from the moment AD-34 landed, and only the
absence of a case exercising both at once kept the contradiction unstated. Execution during the
GTX-125 return supplied it: a document named `pwn<ESC>[31m<ESC>[Aevil.md`, checked under
`NO_COLOR=1` with `--format github`, emits `::error file=docs/pwn<ESC>[31m<ESC>[Aevil.md,...` and
puts the raw `0x1b` bytes on stdout. The promise says that cannot happen; AD-34 says it must.

Only one of the two can hold, and which one gives is decided by whether a control-free annotation
is reachable at all. It is not. `escape_github_property` substitutes `%`, `:`, `,`, CR, and LF,
and those five are the whole of the workflow-command grammar's property vocabulary: the runner
reverses exactly that set to recover the value it resolves the annotation against. CR and LF are
therefore encoded and round-trip, which is why they are the two control characters this record's
exception does not cover, and `ESC` has no representation that does. A spelling such as
`%1B`, `\x1b`, or AD-34's `repr` reaches GitHub as a literally different filename, which attaches
the annotation to nothing, and deletion is `strip_control_chars` again, non-injective for the
reason AD-34 already rejected it. The channel can carry the byte or lose the attachment; there is
no third spelling, so sanitizing is not a cost to weigh but an option that does not exist.

AD-35's move was weighed and is the one genuine alternative: refuse a document whose *filename*
carries a control character, closing the vector at the load boundary rather than narrowing the
promise. It is rejected on two counts. A filename is not authored content the way a frontmatter
value is, so refusing it withholds the report from a repository for a property of its filesystem
rather than of its lattice, and the repository most needing to be told it contains such a file is
exactly the one that would get no report. AD-34 also settled the axis already, on the ground AD-36
states: a path is display-only, and this record does not reopen it. That the refusal would now be
a new breaking change after 5.0 shipped is the lesser objection and not the deciding one.

**Decision:** README's promise is narrowed to human-facing output and names its one exception
outright. The `file=` value of a `--format github` annotation is excluded, with the reason stated
where a reader meets the promise rather than only in this log: GitHub resolves that value against
the file it attaches to, so the repo-relative filename is spelled by the workflow-command grammar
rather than by the escape-free rule. The exception is therefore narrower than "control characters
reach the annotation" and README states it at that width: CR and LF are encoded to `%0D` and
`%0A`, and every other control character passes through raw, `ESC`, tab, `DEL`, and C1 alike.
Wording it as the whole control range would have been an overstatement in the one document that
owns the public guarantee. Nothing else is excluded. `--format json` keeps the guarantee on its
own merits, because JSON's encoder escapes control characters and the `check` and `lint` payloads
carry no filename, and the narrowing says so rather than sweeping both machine formats out
together. The annotation encoder, AD-34, and
AD-35 are unchanged; this record moves a document, not a byte of output.

**Consequences:** The cost is a genuinely weaker public promise, and it is worth stating plainly
rather than as a technicality. Under `--no-color` or `NO_COLOR`, a repository containing a
document whose filename carries any control character other than CR or LF can still put that
character on stdout through `check --format github` and `lint --format github`. What bounds it is
the code-point range above and the channel: that output is addressed to the Actions log rather
than to an interactive terminal, and a reader piping it to one inherits the same exposure they
would from `cat` on the filename itself. Human output, help,
usage errors, warnings, and diagnostics are unaffected and keep the full guarantee, which is where
AD-34's vector actually ran.

An end-to-end case in `tests/cli/test_contract.py` pins the exception under both levers, so the
exclusion is enforced rather than merely written down, and a later change that starts spelling the
annotation fails the suite instead of silently breaking attachment. It sits beside the byte-level
cases the same harness already runs, which is what makes the pair legible: they assert no escape
byte survives, and this one asserts that here it must.
`tests/test_path_display_contract.py`'s encoder-boundary case is kept and is not redundant with
it, because that case proves what `github_annotation` builds while this one proves what the two
no-color levers do to it, and only the second is a statement about the promise.

That file also pins the exception's width, parametrized over both halves: CR and LF encode to
`%0D` and `%0A`, and `ESC`, tab, `DEL`, and the single-byte C1 CSI pass through raw. The width is
tested rather than only written because the first draft of this record and of README stated the
exception as the whole control range, which is wrong by exactly two code points, and an
end-to-end case built on an `ESC`-bearing filename cannot catch that error: every control it
exercises is on the raw side of the boundary. A public guarantee narrowed too far is its own
defect, so the two code points that keep their encoding are pinned as deliberately as the ones
that do not.

### AD-39: An escalated warning is caught at the CLI boundary, not translated at its sites

**Date:** 2026-08-19
**Status:** Accepted
**Context:** AD-29 raises this engine's skip, reused-anchor, and duplicate-`derives_from`
diagnostics through `warnings` rather than through AD-9's `CliRuntime.stderr`, so that
`PYTHONWARNINGS` can suppress them; README documents that setting as a supported control. It has
a third form. `PYTHONWARNINGS=error` and `-W error` do not suppress or display a warning, they
raise the warning instance as an exception, and CPython does that before the replaceable
`showwarning` stage. `main()` mapped `ProjectError`, `BrokenPipeError`, and
`(OSError, RuntimeError, ValueError)`; `Warning` derives from `Exception` and matches none of
them, so an escalated advisory left the entry point as an unhandled traceback naming this
package's own source and exiting 1, the code `check` reserves for drift. A documented control
therefore produced the one exit status the 0/1/2 contract cannot afford to be ambiguous about,
and printed a maintainer's traceback where every other failure prints `error (CODE): ...`.

The decision is where the repair goes. The alternative considered was a shared project warning
category the boundary could recognize, with each `warnings.warn` site translated to it. It is
wider and still incomplete. Wider, because AD-33 treats the existing categories and filter
behavior as observable, so changing what a site raises changes what an adopter's filters match.
Incomplete, because the sites are not the whole population: `config.py` asks ruamel for the
platform-default parser per AD-33 and `reconcile`'s rewrite reread builds its own loader per
AD-26, and both let ruamel's own `ReusedAnchorWarning` escape unchanged, as AD-29 records. Any
dependency this engine imports can raise a category nobody here declares, and a per-site
translation has to be extended for each new one or silently regress.

**Decision:** `main()` catches the base `Warning` beside its existing clauses and reports it
through `print_project_error` as `EscalatedWarningError`, a `ProjectError` carrying the new
`WARNING_AS_ERROR` code, exiting 2. Nothing about emission moves: no site changes category, no
filter is reimplemented, no `showwarning` substitution is touched, and a library consumer calling
`load_lattice()` directly still gets the raised warning. Ordinary runs never reach the clause,
because an unescalated warning is displayed rather than raised.

Catching the base class is the point rather than a shortcut. It covers this engine's warning
family, the two dependency-raised paths AD-29 names, and every category a dependency may add
later, with one clause and nothing to keep in sync. `print_internal_error` was rejected as the
renderer: it prints an uncoded `internal error` line, and an escalated advisory is a consequence
of the caller's own configuration rather than a defect in this tool, so it belongs on the coded
grammar `print_project_error` owns.

The rendered message leads with the warning's category name, which `CliRuntime._render_warning`
deliberately discards for a displayed warning. The two diagnostics answer different questions. A
displayed advisory is addressed to someone reading about their documents, where the category is
noise; this one is addressed to whoever configured the filter that stopped the run, and the
category is the handle that configuration is written against. The warning's own message follows,
and a note records that a filter, not the document, ended the run.

Import time is guarded too, and it takes two regions rather than one. `_load_app()` moves inside
the main block, since an import-time deprecation from a command adapter is an escalated warning
like any other. That alone is not enough, and the first draft of this record claimed otherwise:
the boundary's own support imports run before that block, and `from .errors import ...` reaches
`cli/runtime.py`, which reaches `config` and `orchestrate` -- 25 engine modules plus ruamel,
markdown-it, rich, and typer, every one of them imported while still unguarded. Those imports
therefore get a guard of their own.

That second guard cannot report through `print_project_error`, because that function is exactly
what failed to import. It writes the same `error (CODE): ...` line to `sys.stderr` directly and
exits 2. `escape` is not applied and does not need to be: it neutralizes Rich markup that Rich
then renders back, so a terminal receives the same bytes either way, and nothing on this path
goes through Rich. `EXIT_TOOL_ERROR` is unavailable for the same reason, so the literal `2` is
used and pinned equal to the constant by a test.

Building that message needs a chain that cannot itself fail, which is why
`escalated_warning_error` lives in `error_types.py` rather than beside the rest of the boundary's
rendering in `cli/errors.py`. `error_types` imports `constants`, and `constants` imports
`typing`. That two-module chain is the residual, and it is accepted rather than closed: a warning
escalated while importing it would still be an unhandled traceback, but neither module imports a
dependency or contains a `warnings.warn` site, so there is nothing there to raise one, and a
guard for it would need a third chain with the same problem one level further down.

**Consequences:** `PYTHONWARNINGS=error` is now a supported way to make any advisory fatal, and
lands on the same contract as every other tool error: exit 2, one `error (CODE): ...` line, no
traceback naming this package. It is not a way to make one advisory fatal on its own. The filters
select which warnings are raised and the first one raised ends the run, so a corpus that trips two
sites reports only whichever the load reaches first. That is visible in the reconcile reread's own
coverage: a document whose frontmatter reuses an anchor raises this engine's advisory during the
strict load, so reaching the reread's `ReusedAnchorWarning` at all takes a filter that ignores the
first and escalates the rest. The suite exercises exactly that pair rather than asserting the
reread is reachable under a bare `error`, which it is not.

The `config.py` path is covered by the same clause, but which diagnostic a reader sees there is a
property of the environment rather than of this engine, because the `platform-default` request
AD-33 records takes the C accelerator wherever it is installed. The two cells of the
`yaml-compatibility` leg do not merely differ in whether a warning is shown; they diverge before
any filter applies. Ruamel's pure composer treats a reused anchor as a warning, so an escalating
filter reaches it and the run reports `WARNING_AS_ERROR`. The accelerator refuses the same
document outright, so the config boundary raises `ConfigError` and no warning is ever reached.
Both cells exit 2 with no traceback, which is exactly why the test branches on the parser in hand
rather than on the exit code: keying on the code would take the accelerator's `CONFIG_ERROR` for
the warning path. Each cell's line is pinned rather than one of them skipped, since what the
accelerator leg proves is that this boundary still lands on the coded contract by its own route.

The cost is that `main()` now maps a category of exception it does not own and cannot enumerate.
A dependency that raises a warning during ordinary operation, under an escalating filter, is
reported as this tool's coded error rather than as that dependency's traceback, which is less
information for someone debugging the dependency. That is accepted: the traceback was never the
contract, the exception chain preserves the original via `raise ... from exc`, and a user who
wants the traceback has the same lever they used to escalate.

The escape-free promise AD-38 narrowed is unaffected in both directions. An engine warning's
message already carries its path through `format_path_for_display` per AD-34, so the escaped
spelling is what reaches this line. A dependency's message is passed through as it was written,
which is what `CliRuntime._render_warning` already does for the same warning when it is displayed
rather than raised; this record moves that text between two sinks and does not widen what reaches
either.
