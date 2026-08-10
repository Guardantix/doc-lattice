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
`init` and the `ci` commands are separate: they never load the lattice, and `ci` reads
and refreshes the managed GitHub artifacts AD-16 describes. The central
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
`github_ci/filesystem.py` owns managed-artifact filesystem work over those primitives: a
separate nonblocking advisory lock on the repository root, and a descriptor-relative
parent walk that opens every ancestor with no-follow flags and hands `persistence.py` a
directory descriptor rather than a pathname, so publication keeps writing into the
directory it validated even if that directory's pathname is renamed or recreated mid-run.
The `ci` command adapters together with `cli/git_repository.py` own the package's only
subprocess use, bounded `git` invocations that resolve the working-tree top level and read
the local origin. The rest of `github_ci`, its render, audit, identity, and parser
modules, is filesystem-free; see AD-16 for the policy those adapters enforce. The
`doc_lattice.cli` package owns the application boundary; AD-5 owns the split between its
`cli/commands/reconcile.py` adapter and `reconcile_transaction.py`.
Within the cache package, `cache/schema.py` and
`cache/state.py` are filesystem-free, `cache/store.py` owns cache-file I/O, and
`cache/lookup.py` reads and stats documents to select the verify or stat tier.
`linear_fetch` is impure wiring and `linear_client` is the only module that touches
the network.
**Consequences:** Every command's logic is unit-tested with no I/O; the network slice
is quarantined to one module. Two lock capabilities exist and never nest, since neither
publication path acquires the reconcile lock; reconcile and managed-artifact publication
do not coordinate with each other.

### AD-3: Untyped-to-typed boundary policy

**Date:** 2026-06-27
**Status:** Accepted
**Context:** Document frontmatter YAML, workflow YAML, and Linear JSON arrive untyped.
**Decision:** `typing.Any`/`typing.cast` are allowed only in boundary modules
(`scripts/check_typing_boundaries.py`); the real boundaries are `frontmatter_parser`
(document frontmatter YAML), `linear_parser` (Linear JSON), and
`github_ci/workflow_parser` (workflow YAML), which validate into typed models.
Everywhere else passes typed values.
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

Normal real startup recovers a valid outstanding journal before lattice loading, and an
automatic-recovery notice may be emitted on stderr while the lock is held.
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
holds `options.py` (shared Typer option types) and `git_repository.py` (Git top-level
resolution for the `ci` and GitHub-mode `init` adapters). Each module under
`cli/commands/` is a narrow command adapter. There are no
mutable module-level consoles and no mutations of Typer color globals.

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
**Consequences:** This was a documented breaking change in 2.0. Future
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

`ci audit` therefore performs no shell analysis at all. The `PR_LINEAR_INVOCATION` and
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
