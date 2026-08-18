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
instances despite that, because they consume only loaded values and never a source mark. Those
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
`orchestrate.py` at the default `stacklevel`, because Python renders a warning with its raising
location and filters repeats by it, so a second call site would change both the line a user sees
and when it is shown.
Reporting it as a Python warning rather than through the per-invocation `CliRuntime.stderr` that
AD-9 makes the owner of CLI diagnostics is deliberate, and it is the one place a diagnostic leaves
that boundary. The reason is that a skip is the only diagnostic here a user may legitimately want
to suppress on an otherwise healthy corpus, and `warnings` is the standard, already-documented
suppression mechanism; `cli/errors.py` has no equivalent. The costs are real and were measured
rather than assumed: the warning is invisible to an in-process `CliRunner` invocation, which is why
its CLI coverage shells out to a subprocess; `--no-color` and `NO_COLOR` do not reach it, and its
rendered form exposes this module's own source location; and under `PYTHONWARNINGS=error` it
escapes the entry point's `ProjectError` mapping entirely, printing a traceback and exiting 1, the
code otherwise reserved for drift. That last one is why the exit-status guarantee below is stated
for ordinary warning configuration. Moving the report back inside AD-9's boundary would fix all
three but forfeit `PYTHONWARNINGS` suppression, so it is a real trade rather than an oversight.

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
`str | None`.

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
| Entry `ref` | Value | A string | Nothing more; a non-string `ref` is refused whenever planning is reached |
| Entry `seen` | Carrier shape | A scalar or null, never a collection | Nothing more; a collection-valued `seen` at a targeted entry is refused |
| Entry `seen` | Scalar spelling | Any scalar spelling whose constructed value is a string or null: plain, single or double quoted, a block scalar in either style with any chomping or explicit indentation indicator, an explicit `null` or `~`, an empty value, an absent key, an alias to such a value, or an explicit `? seen` left without its `:`, which constructs null. How the key itself may be written is the `Entry` `Key spelling` row above | Any scalar the safe constructor accepts, whatever it constructs to, an explicitly tagged one included |
| Entry `seen` | Node properties | An anchor, a tag, or both, in either order, on the value's own line or on lines of their own, with the author's comments between a property and the value | Nothing more |

Four behaviors of the loaded shape are recorded with the matrix rather than inside a cell.
`!!omap` is handled wherever the loaded shape is a mapping, at the root and at an entry alike.
A merge is deliberately not followed inside an ordered map, because the loader builds one from its
items rather than through mapping construction. Alias detachment may expand an alias site into a
local mapping, or into a local one-pair item for an ordered map, rather than editing the shared
node behind it. An anchor name may be defined more than once under the pure Python parser, which
warns about it: a later definition rebinds the name, so each alias reads the nearest definition
above it and a relocated value lands only on the alias sites still bound to the anchor it
displaces. That acceptance is parser-conditional: with the optional `ruamel.yaml.clib` accelerator
installed the strict tracked-document load refuses a reused name outright as a duplicate anchor,
while the reread inside `apply_reconcile`, pure by AD-26, still handles it, so the spelling is
reread-only there.

**Layer 2a: the envelope.** These are lexical rather than structural, and a declared version has a
constraint the matrix cannot show. The block opens and closes on a line whose stripped text is
exactly `---`, so space on either side of either fence is accepted, leading indentation included.
A leading run of UTF-8 byte-order marks may precede the opening fence; the whole run is stripped
for fence detection and reattached verbatim. A `%YAML` directive is supported, but only alongside
a document-start line that does not strip to `---`, because `frontmatter_parser.py` closes the
block at the first line that does. The directive's own document start therefore has to be spelled
otherwise, and `--- !!map` is the form the suite pins.

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
