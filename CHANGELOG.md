# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [7.0.0] - 2026-08-31

### Migration

`.doc-lattice.yml` must now declare `lattice_format: 2`. A config file that omits the key is
refused with a pointer to this section; a zero-config run (no `.doc-lattice.yml` anywhere) is
unaffected, since there is no file to declare it in. Three steps:

1. Add `lattice_format: 2` to `.doc-lattice.yml`, as the first key. doc-lattice 7 refuses to run
   without it, and every earlier release refuses to run with it, so the two never operate on the
   same tree by accident. A zero-config project has nothing to add.
2. An edge whose target id sits in a slug-collision component now fails `AMBIGUOUS`, `check`
   exits 1 on it, and `reconcile` refuses to write its `seen`. Fix each one first, by rewording a
   colliding heading or giving the target an explicit `{#anchor}` marker, before running the
   re-bless in step 3: the refusal is run-scoped, so `reconcile --all` writes no `seen` values
   at all while any edge in the run is still ambiguous, not just the ambiguous one.
3. The section content hash now includes the target's ancestor heading chain, so every `seen`
   value on a section that sits under a parent heading mismatches once. Run
   `doc-lattice reconcile --all` to re-bless the lattice. Whole-file refs and top-level
   sections are unaffected.

To make a tracked file's metadata invisible on GitHub, replace its opening `---` with
`<!-- doc-lattice`, replace its closing `---` with `-->`, and rerun `check`. The YAML between
them is unchanged. A file whose id or any other value contains `--` keeps the fence spelling or
renames the value.

## [6.0.0] - 2026-08-25

### Migration

One thing to act on, and it applies only to a caller that reads the printed error code rather than
the message. Every value `init` validates before it writes anything now exits with
`VALIDATION_ERROR` instead of `CONFIG_ERROR`. That covers four inputs, and all four move together:
an unsafe or empty or control-bearing `--docs-root`, a `--linear-team` that is not a Linear team
key or is empty or control-bearing, a `--default-branch` outside the supported branch-name domain,
and a branch name the local `origin/HEAD` probe discovered that fails the same domain. The
probed candidate is not a command-line value, but it is still an input the run validated, and
`CONFIG_ERROR` named a config file `init` has not written at that point and never reads.

Repoint anything matching on the printed code, and anything catching `ConfigError` around
`doc_lattice.cli.git_repository.validate_default_branch` or around `init`'s flag validation, at
`ValidationError`. It is a sibling of `ConfigError`, not a subclass, so an existing
`except ConfigError` stops catching it. Catching `ProjectError` keeps working unchanged. The exit
status is unchanged at 2, the messages are unchanged, and no other command's error-code mapping
moves. See **Changed** below.

A second thing to act on, and only for a caller that *constructs* this package's exception types
rather than catching them. `FrontmatterError` and `UnreadableDocError` now require the failing
document: `FrontmatterError(message, source=path)`, not `FrontmatterError(message)`. Both derive
from a new `DocumentError`, which is where the `source` attribute lives, and `DocumentError`
derives from `ProjectError`. Catching either type, or `ProjectError`, is unaffected; catching
`DocumentError` is the new way to catch exactly the failures that name one document. Error codes,
message text, and exit codes are unchanged. See **Fixed** below and AD-41 in ARCHITECTURE.md.

A third thing to act on, and the only one that changes what a zero-config run does. `init` run
from a subdirectory of a repository where an ancestor directory inside the same repository
already holds `.doc-lattice.yml` now refuses, exiting 2 with `VALIDATION_ERROR`, where it used to
exit 0 having written a second, nested config carrying default settings. It is the nearest such
ancestor that counts, not only the repository root, so an intermediate directory's config refuses
just as a root one does. That nested file was not inert, which is what made the old behavior
worth changing: `check`, `lint`, `impact`, `reconcile`, `graph`, and `linear` all select a default
config from their own current directory and never walk up, so commands run from that subdirectory
would have loaded it while every run from the configured directory carried on reading the
original. The result was a silently divergent second lattice, reported as success. If you have a
script that runs `init` from a subdirectory, either move it to the directory holding the config
or, when it only wanted the printed blocks, switch it to `init --print-only`, which is new in this
release and writes nothing at all.

Two bounds on that refusal are worth knowing before you read an exit 2 as a bug. The search walks
up only as far as the nearest `.git` entry, inclusive, so a config above your checkout does not
block a new project and a submodule or nested repository under a configured root still scaffolds
normally. And it fires only when the current directory has no config of its own: an ordinary
rerun at a configured root still reports that the file already exists, leaves it untouched, and
prints, exactly as before. Nothing that used to be written is now written to a different place;
the only outcome that moved is a write that used to happen and now does not.

This was decided as a compatibility choice rather than falling out of the fix. The alternative
was to resolve the Git root and write there, which would have overturned two documented
behaviors -- `init` keeping its current-directory contract, and having no Git prerequisite -- and
made the write destination depend on whether Git discovery succeeded. The refusal keeps both, so
it needs this note and README rather than an ARCHITECTURE decision. A deliberately nested lattice
stays supported and needs no flag: README prints the exact bytes `init` writes, so it is one
hand-written file, and the diagnostic says so. See **Added** and **Changed** below.

### Added

- `init --print-only` prints the `.gitignore`, pre-commit, and workflow blocks and writes nothing.
  Those three are hand-maintained artifacts an adopter has to re-fetch on every upgrade, and until
  now the only way to obtain them was a command that also scaffolds `.doc-lattice.yml`, which
  coupled a read to a write and made the retrieval depend on which directory it ran in. The mode
  prints byte-for-byte what an ordinary run prints and narrates the same branch and placement
  guidance on stderr, minus the one line reporting a write. It branches ahead of every config
  concern, so it renders no config text, runs no config validation, and is not subject to the
  nested-scaffold refusal above: it succeeds from exactly the subdirectory where an ordinary run
  is now declined. Only `--default-branch` affects its output; combining it with `--docs-root` or
  `--linear-team` is refused as a usage error, uncoded and exiting 2, because those two feed only
  the config this mode does not render and silently ignoring them would report success for a
  request nothing acted on. README's Upgrading section now retrieves through it.
- `markdown_compat.github_heading_ids` returns the addressable id GitHub renders for each heading
  in a document, deduplicated in document order by the pinned `github-slugger@2.0.0` collision
  rule. Neither existing public function answers that: `github_slug` is a base slug with no
  deduplication, so repeated headings collapse onto one id, and `anchor_ids` resolves
  doc-lattice's explicit `{#anchor}` identity, which is a different namespace because GitHub has
  no such syntax and slugs the literal marker-bearing heading instead. The new helper is a thin
  wrapper over the existing deduplicator rather than a second collision algorithm, and
  `anchor_ids` now derives from it, so both namespaces stay pinned to one implementation.
  `markdown_compat.github_ids_for_texts` exposes that same deduplicator over raw heading texts,
  for a caller whose heading grammar is wider than the addressable subset `Heading` describes;
  `github_heading_ids` delegates to it, and the link gate below feeds it the heading texts of its
  own inventory rather than manufacturing `Heading` values or copying the collision rule. The
  golden compatibility fixture gains a `github_ids` column per case, which is where the
  divergence between the two is recorded.
- `scripts/check_doc_links.py` resolves every relative link and heading fragment in the maintained
  root documents, and runs both as a pre-commit hook and as an explicit step in the CI code-quality
  job, which enumerates its checks directly and never invokes pre-commit. CLAUDE.md told
  contributors to run "a relative-link check" that the repository never defined, so every
  contributor invented one and the anchor half was usually skipped; meanwhile the maintained
  documents accumulated 36 deep anchor links that a renamed heading would have broken silently. Link
  sources are the sorted root `*.md` files, a target may be any repository-contained relative path,
  and absolute and external destinations stay out of scope. Destinations come from parsed link
  tokens, so reference-style links are followed and link-like text inside code is not a link.
  Heading fragments resolve against a link-target inventory the gate builds from its own full
  CommonMark parse, deliberately separate from doc-lattice's section identity: it covers every
  heading form GitHub assigns an id to -- setext, ATX indented one to three spaces, and headings
  nested in a list item or a block quote -- so a deep link that renders and resolves on GitHub is
  not failed by a gate reading the narrower addressable subset, and accepting it costs no
  cached-derivation change to what the engine sees as a section. A heading a fence or an indented
  code block swallows is sample text to both inventories, and both deduplicate through the one
  shared collision rule, so a heading both see resolves to the same id. A
  destination written as a raw HTML anchor is reported rather than resolved, since markdown-it
  normalizes a Markdown destination and an attribute value arrives with none of that done, so
  resolving one means owning URL and HTML attribute semantics wider than this gate's contract. An
  anchor inside a `<details>` block is reported at its own line within that block rather than the
  line the block opens on, and an inline one at the line of the block containing it, which is the
  only line the token stream records for it. The check parses the document's HTML rather than
  pattern-matching it, so anchor-shaped text inside a comment, a code fence or a raw-text element
  such as `<script>` is not read as an anchor, and raw-text state carries across the separate tokens
  markdown-it splits an inline element into. Containment is settled twice, once lexically on the
  destination as written and once on where the filesystem sends it, so an in-repository symlink
  keeps working while one leaving the repository is refused before its target is opened. A document
  the run cannot read is reported and stepped over rather than allowed to end the run on a
  traceback: a maintained source that will not decode as UTF-8, or that carries a character
  reference wider than the interpreter's integer-conversion limit, is reported without a line, and a
  link target that will not decode or parse for its heading ids is reported at the link that names
  it. Either way every later link in every later document is still checked, which is the silent
  truncation the gate exists to prevent.
- Dependabot now watches this repository's GitHub Actions pins. `.github/dependabot.yml` checks the
  six actions `.github/workflows/ci.yml` and `.github/workflows/claude.yml` reference once a month
  and opens one pull request per action, because a SHA-pinned action cannot report on its own that
  a newer release exists. `actions/checkout` and `astral-sh/setup-uv` are grouped as `shipped-pins`
  and travel as a single pull request: they are the two pins `init` and MANAGED_CI.md ship to
  adopters, so bumping either is a coupled edit across the workflows, `constants.py`, the published
  recipe, and the spelled-out copies in the suite. Such a pull request arrives red on the
  shipped-pin parity test by design, since Dependabot edits the workflows and nothing else, and
  that red is the notice that the rest of the edit is owed. RELEASING.md owns the procedure for
  finishing it.
- A new `Action runtime audit` workflow reads the annotations GitHub's runner attaches to the jobs
  of a completed CI or Claude Code run and fails when any warning or failure annotation mentions a
  deprecation. That is how a runtime deprecation announces itself, and it covers the actions that
  actually executed, nested composite steps included. `scripts/audit_action_runtimes.py` holds the
  logic and writes a job summary naming the source workflow, the job, and the action and SHA the
  annotation named. It is a notice rather than a gate: a `workflow_run` workflow runs on the
  default branch and does not attach to the run that triggered it, so a red audit blocks nothing
  and someone has to look. Any completed run can be re-read on demand with
  `gh workflow run "Action runtime audit" -f run_id=<run-id>`, and the 5.0.0 release run trips it,
  because at that tag `actions/upload-artifact` and `actions/download-artifact` were pinned to
  releases that target Node.js 20. No shipped pin moved in this change: both mechanisms report, and
  every pin stayed exactly where it was. Those two workflow-only pins have since moved, under
  **Changed** below. AD-42 in ARCHITECTURE.md records the decision, including
  why a manifest-fetching auditor and a tail job inside `ci.yml` were both rejected.
- A new `Action pin correspondence` workflow asks GitHub whether each shipped pin's SHA really is
  the commit its trailing `# vX.Y.Z` comment names. Every other check compares this repository's
  text against another copy of this repository's text, so a SHA paired with the wrong release is
  self-consistent in every file and passes green everywhere, while the comment is what a reader and
  an adopter use to judge what the pin is. `scripts/check_action_pin_correspondence.py` holds the
  logic: it reads the two pairs from `constants.py`, refuses a non-exact comment such as `v7`,
  probes each tag's reference for existence, resolves it through the commits endpoint so an
  annotated tag is peeled to its commit, and writes a job summary. A wrong comment, a deleted or
  retagged upstream release, and a SHA advanced past every tag are all reported as correspondence
  findings, and are kept distinct from infrastructure failures such as a rate limit or an outage,
  which say nothing about the pins. The check runs monthly and on `workflow_dispatch`; it is a
  notice rather than a gate and does not join the protected contexts RELEASING.md records, because
  the answer needs network access and the pull-request suite stays offline. No pin moves in this
  change. AD-43 in ARCHITECTURE.md records the decision and amends AD-42, whose conclusion that a
  wrong comment can only enter by hand edit holds at authorship time alone: a Git tag is mutable,
  and the pinned `actions/checkout` release reports `immutable: false`.
- `scripts/check_migration_rule.py` enforces the release rule that changed adopter output carries a
  `### Migration` subsection, which RELEASING.md step 4 had only ever stated as prose, in the one
  situation where prose is weakest: a release under time pressure. The guard snapshots the surfaces
  that rule names -- the `.gitignore`, pre-commit, and workflow blocks `init` prints, rendered here
  from `doc_lattice.scaffold` across a representative default-branch matrix, and the trusted Linear
  workflow and the two `gh` procedure sections MANAGED_CI.md publishes, extracted from the document
  because AD-32 left no renderer for them -- and compares them against a committed baseline,
  `scripts/migration_baseline.json`. Only the routine per-release version-pin substitution is
  normalized away, since every release performs it and step 4 exempts it by name. A baseline that
  any change may freely rewrite would authenticate nothing, so it is bound twice: its stamp must
  equal the latest released changelog heading, which forces the rollover into the release commit
  that promotes `## [Unreleased]`, and required pull-request CI additionally compares it against the
  base ref, where a content change without a version promotion, or a promoted section missing the
  subsection, fails. Mid-cycle, a change to generated output is authorized by writing the
  subsection, never by advancing the baseline. It runs as a step in the CI code-quality job, on both
  the base-ref path and the plain one the push to `main` takes, and as an `always_run` pre-commit
  hook mirroring the offline half for early feedback. The script's module docstring owns the
  mechanism and states what stays open: edits to the guard itself, and branch shapes outside the
  matrix.

### Changed

- `init` refuses to scaffold a config into a directory that has none when an ancestor inside the
  same repository already holds one, instead of writing a nested config that only commands run
  from that same subdirectory would ever load. The diagnostic names the configuration it found and
  both ways forward: run `init` in that directory, or pass `--print-only` here. It is reported as `VALIDATION_ERROR`, with the other inputs `init`
  checks before writing anything, because the directory is the input in question -- not
  `CONFIG_ERROR`, which would name a file `init` found but still never reads, and not
  `INIT_PERSISTENCE`, which stays exactly "an I/O failure happened". The boundary is a filesystem
  walk for `.git`, testing for existence rather than for a directory so a linked worktree and a
  submodule checkout are recognized by the regular file they carry. It is deliberately not a Git
  query: `init` has no Git prerequisite, and resolving the boundary through Git would re-create
  the resolver AD-32 retired. The consequence is stated rather than fixed -- under `GIT_DIR`,
  `GIT_CEILING_DIRECTORIES`, or `GIT_DISCOVERY_ACROSS_FILESYSTEM` this walk and the default-branch
  probe can disagree about where the repository begins, which costs one refusal too many or one
  too few and never a file written in the wrong place, because the boundary bounds only the
  refusal and never selects a destination. See **Migration** above.
- An entry that walk cannot read is a refusal rather than a guess, reported as
  `INIT_PERSISTENCE` and naming the entry. The predicate this rests on is deliberately not
  `Path.exists`, which is not stable across the supported Python versions: 3.13 re-raises an
  `OSError` outside its ignored set while 3.14 answers False for every one of them, so the same
  run against the same filesystem crashed with an uncoded error on one interpreter and silently
  scaffolded the nested config on the other. Only "not found", "not a directory", and a symlink
  loop mean absence now; anything else means the question cannot be answered, and `init` declines
  to scaffold rather than assume either way. A configuration entry must also be a regular file, so
  a directory carrying that name no longer counts as a config at either end of the walk, and the
  repository marker is tested without following symlinks, so a worktree whose `.git` link has lost
  its target still bounds the search.
- `init`'s printed guidance now names the step it was already ordering against. Its baseline line
  told an adopter to reconcile "before enabling the gates" while nothing in the same output said
  what enabling them is, so a terminal-only adoption was handed an ordering constraint and no act
  to order against. A new line after the baseline states that adding the pre-commit block installs
  no Git hook, gives the `uv tool install pre-commit` and `uv tool run pre-commit install` pair --
  or plain `pre-commit install` where a durable runner already exists -- and carries the ordering
  with it: on an initial adoption only after the baseline and after `check` and `lint` are clean,
  while an established installation enables the gates immediately. It is conditioned on the clone
  not already being gated, because the same narration is emitted by `init --print-only`, the
  documented upgrade path, where replacing a block needs no reactivation. Placement, baseline, and
  activation stay three separate lines on purpose: appending an unconditional "then install" to
  the placement sentence is the shape that would put the ordering failure back. README.md already
  owned every one of these rules and is unchanged.
- The release smoke test now runs `init --print-only` in its throwaway directory and asserts the
  config is absent afterwards, before the ordinary `init` run whose write it then asserts. The
  read-only mode needed an observer, since nothing else in that step reads the directory back, and
  keeping both runs preserves the packaged-scaffolding coverage the throwaway directory has
  carried rather than retiring it under cover of a cleanup.
- The declared floors for `typer` and `pydantic` rise to `>=0.26.0` and `>=2.12.0`, because
  neither of the old ones survived being run. Below typer 0.16.0, click 8.2 breaks typer outright
  and `--help` piped into a departed reader exits 2 instead of 141; from there to 0.25.1 typer
  renders a mistyped flag as `No such option '--json'.` rather than click's `No such option:
  --json`, which is a user-visible difference in what a rejected option prints. `pydantic>=2`
  never resolved at all on Python 3.14, since pydantic pins an exact `pydantic-core` and no
  release before 2.12.0 ships a wheel for that interpreter. Both new floors are the earliest
  release the whole suite passes against on every supported interpreter. An adopter already
  resolving through this project's own lock sees nothing change; one resolving in a constrained
  environment now gets a resolution failure instead of a version nothing ever tested.
- Dependency floors are now a CI contract rather than a claim. The single `rich-floor` leg is
  generalized into a `runtime-floor` matrix that crosses every floor-declared runtime dependency
  this engine reads -- `pydantic`, `rich`, and `typer` -- with both supported interpreters,
  overlaying exactly one floor per cell onto the locked remainder and running the whole suite. One
  floor per cell because a constrained resolver may hold one dependency at its minimum and take
  the rest from a resolution as new as this project's lock; both interpreters because `pydantic`
  resolves through interpreter-specific `pydantic-core` wheels. `rich` keeps its declared floor of
  `13.8.0` and gains coverage: its cell now runs the whole suite on both interpreters rather than
  `tests/cli/` on one. The matrix reports through a fixed `Runtime floor compatibility` context in
  the fail-closed shape GTX-176 established, and `tests/test_release_workflow.py` derives the
  expected cells from `pyproject.toml`, so a ranged dependency added with neither a cell nor a
  recorded exemption fails there rather than shipping an unverified span. AD-27 in
  ARCHITECTURE.md records the amended posture; RELEASING.md owns the branch-protection rollout
  that makes the new context required.
- A reconcile commit that loses its journal to a failed committed marker, and then cannot restore
  the prepared journal either, no longer tells you to run `doc-lattice reconcile --recover`. In
  that one state there is no journal for recovery to read, so the command could only report each
  retained before stage as an opaque orphan and exit 2, with nothing tying a stage back to the
  destination it belongs to. The diagnostic now says the journal is absent, states that every
  destination reached its after image and that no rollback was attempted, and lists each
  destination with the digest it should currently hold, its retained before stage, and that
  stage's digest. Both digests are checks it requires rather than facts it prints: recovery
  would refuse to act on a stage that did not match its recorded digest, and a manual restore
  has no such gate unless the diagnostic states one, so the message asks for the destination and
  the stage to be verified and for anything that does not match to be preserved rather than
  copied.

  A failed reset is not read as an absent journal on its own. The commit classifies the journal
  path before choosing what to say, and only a confirmed-absent journal drops the instruction: an
  exact prepared journal and a visible committed marker each still prescribe `--recover`, which
  acts on either, and so does anything else found there, which recovery authenticates and refuses
  safely when it cannot. Every state where the prepared journal is not the file on disk now
  carries the entry mapping with it, the committed marker excepted, where the transaction is
  durable and nothing should be restored. RECONCILE.md owns the resulting state.

- A rejected `init` input now reports `VALIDATION_ERROR` rather than `CONFIG_ERROR`. `init` writes
  `.doc-lattice.yml` and never reads one, so a bad `--docs-root`, `--linear-team`, or
  `--default-branch` was pointing the user at a file that did not exist yet and had nothing to do
  with the failure. A branch name the local `origin/HEAD` probe discovered and then failed the
  branch-name policy moves with the flag rather than keeping the old code on one side of the same
  validator. This completes the boundary that 5.0 started: `FRONTMATTER_ERROR` took the load
  boundary and `INIT_PERSISTENCE` took `init`'s write boundary, leaving the values `init` checks
  before either as the last ones naming config. See **Migration** above.

- The declared `rich` floor rises from `13` to `13.8.0`. The CLI's broken-pipe policy is built on
  `Console.on_broken_pipe`, which does not exist before that release, so the previous floor
  admitted versions where the policy was silently inert. A new `rich-floor` CI leg installs that
  exact version and runs the CLI suite against it. AD-27 in ARCHITECTURE.md records why `rich`
  now carries a compatibility leg where it previously carried only a bound.

- The `--no-color` / `NO_COLOR` escape-free promise in README.md is narrowed to human-facing
  output and now names its one exception. No behavior changes: this corrects a documented
  guarantee that was always broader than the engine kept. A repository holding a document whose
  *filename* carries a control character other than a carriage return or a line feed has always
  been able to put that character on stdout through `check --format github` and
  `lint --format github`, under either lever, because the annotation's `file=` value is what
  GitHub resolves the attachment against. README promised it could not; AD-34 had excluded that
  channel since 5.0, so the two documents disagreed from the moment AD-34 landed.

  README's promise gave way rather than the encoder, because no sanitized spelling round-trips
  back to the original filename and every candidate detaches the annotation instead. AD-38 in
  ARCHITECTURE.md owns that reasoning and the rejected alternative of refusing such a filename
  outright; README.md owns the narrowed promise and states the exception's exact width. Nothing
  else is excluded, `--format json` included. Human output, help, usage errors, warnings, and
  diagnostics keep the full guarantee. Tests now enforce the exclusion under both levers and pin
  the carriage-return and line-feed boundary rather than leaving either written down.

- `init` now scaffolds a commented `cache_trust_stat: true` line into `.doc-lattice.yml`,
  alongside the `ignore_globs`, `cache_key`, and `linear_team` examples it already wrote. It is
  the one option whose fast path trades a read for trust, so a reader should meet it in the file
  rather than only in the docs. It is scaffolded at `true`, not at the `false` default, so that
  uncommenting it opts in like every other commented line in that file; a scaffolded `false`
  would be a no-op wearing an opt-in's comment. Its comment names the `cache_key` dependency,
  because `cache_trust_stat: true` without `cache_key` is a config error and the scaffolded line
  would otherwise invite one. The written file is otherwise unchanged and still has exactly one active
  key; nothing about config loading moves.

- README.md documents the twelve printed error codes beside the exit-code table, separates the
  uncoded shapes an exit 2 can take, documents the global `--version` option, and states that
  configuration and tracked lattice frontmatter reject unknown keys and do not coerce values.
  None of this is new behavior; all of it was undocumented. The uncoded shapes are given apart
  because they are not one grammar: a usage check a command writes itself and the
  `reconcile --recover` problem report print `error: ...`, an unexpected failure prints
  `internal error: ...`, a usage failure the parser rejects first prints Typer's own `Usage:`
  line and boxed `Error`, and a bare invocation prints help with no diagnostic at all. A caller
  cannot classify every exit 2 by matching `error`, so the document says so rather than implying
  otherwise. The error-code table and the sample `.doc-lattice.yml` are now held to the code by
  tests rather than by review, so neither can drift again: the table's rows must be exactly the
  coded domain `constants.py` declares, and the sample must be byte-for-byte what a flagless
  `init` writes. Tests pin the uncoded shapes too.

- The repository guards now enforce what their names imply, in four places where they did not.
  `scripts/check_version_sync.py` loaded README.md and MANAGED_CI.md alone and treated a document
  with no recognized pin as consistent, so deleting a pinned install ref was invisible and a pin
  added to a new document was unenforced. Release surfaces are now declared rather than
  discovered: `PIN_MANIFEST` names each document with the exact number of recognized pins it
  carries, `HISTORICAL_PIN_DOCS` exempts CHANGELOG.md, whose superseded migration pins are
  preserved on purpose, and a recognized pin in any other maintained document fails as an
  unclassified release surface. The count is exact, not a minimum, because a minimum lets a newly
  added pin mask the deletion of a required occurrence; what it closes is any change that alters
  the number of recognized pins, never one that preserves it, so neither a deletion compensated
  by a new current pin in the same document nor a newly added spelling that `_PINNED_REF` never
  sees is reported. Identifying individual pin sites and widening candidate recognition both stay
  out of scope. Two policy errors are reported alongside the pins: a manifest entry with no
  matching document, so deleting the document does not read as compliance, and a document that is
  declared and exempted at once, whose exemption is applied first and would otherwise silence its
  declared count. "Maintained documents" is the sorted root `*.md` files, the same set
  `scripts/check_doc_links.py` takes as its link sources; the two selections are spelled
  separately, because neither script is importable from the other's process, and a test holds
  them identical.
- `scripts/check_typing_boundaries.py` matched six generic keywords as an inner directory, an
  exact stem, or a `_<keyword>` suffix, so a future `doc_lattice/cache/external.py`,
  `doc_lattice/external/store.py`, or `doc_lattice/cache_store_external.py` would each have opened
  a `typing.Any` escape hatch nobody decided to open. The keyword set is replaced by an exact
  allowlist of the three modules AD-3 actually names, spelled as source-root-relative paths rather
  than stems so a same-named module elsewhere in the tree cannot inherit the exemption. Because
  those paths are relative to the source root, the check now refuses a scan root that does not
  carry them rather than reporting the three exempt modules as violations. The scanned tree is
  unchanged: those three are the only modules that use the escape hatches today.
- CI runs Ruff check and Ruff format check over `src/ tests/ scripts/` and `ty` over
  `src/ scripts/`, where the release-gating scripts were previously unlinted, unformatted-checked,
  and untyped. The tree already passed all three, so this is enforcement coverage rather than a
  cleanup. CLAUDE.md's contributor commands name the same target sets.
- `scaffold.PYTHON_PIN` and the `requires-python` lower bound are held to each other by a parsed
  correspondence test. Both are copies of the floor AD-24 declares load-bearing, and changing
  either alone now fails. The existing scaffold test fails on a lone `PYTHON_PIN` change by
  asserting the rendered `--python 3.13`, which proved nothing about `requires-python`. The parse
  judges two clause shapes, the sole `>=` clause and upper bounds, and refuses a specifier set
  carrying anything else: every other operator can raise the effective floor without touching the
  `>=` clause, so reducing such a set to its `>=` clause would report a floor installers do not
  honor and pass the correspondence against a `PYTHON_PIN` they reject. Three
  machine-consumed copies of the floor stay uncorrelated and are named here so the claim stays
  honest: Ruff's `target-version`, the CI matrix, and the slugger generator's default interpreter.
- CLAUDE.md names `uv run --group dev pre-commit install` as a required contributor setup step and
  says it is per clone. It described what the hooks run without ever saying to install them.
  Adopter-side activation was already documented in README.md and MANAGED_CI.md and is unchanged.
- `actions/upload-artifact` moves to v7.0.1 and `actions/download-artifact` to v8.0.1, both
  SHA-pinned per AD-42, off the releases that target Node.js 20 and onto ones declaring `node24`.
  These are the release path's two workflow-only pins, used by `build-release` and `publish`; they
  are not among the pins `init` and MANAGED_CI.md ship to adopters, so nothing an adopter holds
  moves and the shipped-pin parity test is untouched. The configured inputs are unchanged and
  still contract-tested: upload keeps `name`, `path`, and `if-no-files-found`, download keeps
  `name` and `path`. Upload v7's direct-upload path is opt-in through `archive: false`, so the
  default zipped transfer this pipeline relies on is unchanged, and download v8 now fails a digest
  mismatch that older releases only warned about, which tightens the same-run build-to-publish
  handoff rather than requiring an override. Both jobs are gated on a real version-bump run, so
  neither bumped action executes until this release is cut and the first `Action runtime audit`
  reading of that run is what confirms the deprecation is gone.

### Fixed

- The release pipeline now runs the packaged artifact before it can be published. `build-release`
  installs the wheel it just built and runs MANAGED_CI.md step 1's command against it,
  `init --default-branch main` in a throwaway directory, between Twine validation and the artifact
  upload. Nothing exercised that path before: the pre-tag smoke installs from the Git source and
  omits the flag, and Twine reads distribution metadata without executing any of it, so a
  scaffolding regression that existed only in the packaged artifact would have reached PyPI and
  become a hotfix or a yank rather than a failed run. The check asserts what an adopter following
  the recipe observes -- the config written into a directory that did not have one, and the branch
  readback on stderr and absent from stdout -- and it gates rather than reports: it precedes the
  upload `publish` depends on, so a failure leaves that job nothing to download. The `publish` job
  is unchanged, and deliberately so; GitHub holds it for `pypi` environment approval before any of
  its steps start, so a check placed there would run after the decision it exists to inform.

- `check --format github` and `lint --format github` now annotate the document a run *failed* on,
  not only the ones it classified. A broken frontmatter block, or a document that cannot be read
  or decoded, previously exited 2 with a single stderr line and an empty stdout, so a pull request
  whose gate failed on a bad document showed nothing at all on the diff -- the one place a
  reviewer would look for the file name. Such a failure now emits an `::error file=...` line
  attached to the offending document, resolved against the same annotation root drift findings
  use, so it lands on the right file from a nested working directory too.

  The annotation carries the error code in its title and the same message plus notes that stderr
  carries, and it is written before the stderr diagnostic so a departed stdout reader still
  reaches the silent 141 rather than a tool error with a half-written annotation. If the failing
  document falls outside the annotation base, the run emits the same unattachable warning the
  finding renderers already emit, since that annotation is the only one the run produces. Every other
  renderer is untouched: `--format human` and `--format json` keep their exact stderr text, their
  empty stdout, and exit 2, and a failure with no single document behind it -- a config defect, a
  broken ref, a transaction failure -- emits no annotation under any format. AD-41 in
  ARCHITECTURE.md records the hierarchy this rests on and what it decides for the message-only
  error types, which are unchanged.

- A command's exit code no longer depends on which of its output streams survived. Piping
  `doc-lattice` into a reader that departs early -- `head`, `jq -e`, a shell that closes the pipe
  -- could put five different wrong codes on the wire, and each is now the code the run actually
  earned.

  A dead **stderr** kept its diagnostics undelivered but changed the verdict: a genuinely broken
  lattice reported 141 ("something downstream stopped reading") instead of 2, because every
  command reports through `exit_on_project_error` before raising its exit, and that report ran
  unguarded. A run whose only stderr traffic was an advisory finished all of its stdout work and
  then exited 120 -- CPython's code for a failed shutdown flush -- instead of its own 0 or 1,
  because the bytes the failed write left buffered were flushed again after every handler had
  finished. Both now leave the semantic exit code untouched.

  A dead **stdout** exited 1, the code `check` and `lint` reserve for drift, on two paths.
  `doc-lattice --help` did so because help and usage text are rendered by typer's own consoles,
  which the previous fix could not reach; `--format json` and `init` did so because they write
  without going through Rich, and typer converts that particular failure into exit 1 itself. Both
  now exit 141 silently, as the human-rendered paths already did.

  The fifth path is the entry point's own fallback for an escalated warning raised while importing
  its reporter. It reports through a plain `sys.stderr` write precisely because the reporter is
  what failed, and it now neutralizes that stream itself rather than letting a dead one turn its
  exit 2 into 120.

  AD-40 in ARCHITECTURE.md owns the per-channel policy and amends AD-29, which had recorded the
  previous uniform one and had claimed the tool-error exit was already retained when a report
  could not be delivered.

- The load-cache write warning is now one physical stderr line for every failure it can report.
  Both the module boundary and `_write` promise a single line, and the diagnostic built that line
  by interpolating `exception_details`, which GTX-203 pinned as preserving the line breaks inside
  an exception's message and inside each note. The two statements contradicted each other. The
  call site now flattens the rendered detail onto one line, so a break reaching it from a failing
  replacement -- or from the remediation note the persistence layer attaches when the stage
  cleanup fails as well -- can no longer split the warning in two.

  Nothing observably wrapped before this: an `OSError`'s text is single-line in practice, so the
  contradiction was latent rather than live. `exception_details` is unchanged and stays multi-line
  by design, because the shared project-error renderer builds its output from those breaks
  deliberately. This is the one sink that promises a single physical line, so the flattening is
  local to it and no architecture decision moves.

- Running any command under `PYTHONWARNINGS=error` (or `-W error`) no longer ends in a Python
  traceback naming this package's own source and exiting 1, the code `check` and `lint` reserve
  for drift. That setting does not suppress or display a warning, it raises the warning instance
  as an exception, and no handler at the entry point matched it. README documents
  `PYTHONWARNINGS` as a supported control, so the escalating form has to land on the same
  contract as every other failure, and it now does: one `error (WARNING_AS_ERROR): <Category>:
  <message>` line and exit 2.

  The new `WARNING_AS_ERROR` code joins the printed error-code domain; nothing raises it except
  this mapping. The category name leads the message because it is the handle an escalating filter
  is written against, and a note records that a filter rather than a document ended the run.

  Import time is covered as well, in two regions. The application load is inside the guarded
  block, and the boundary's own support imports -- which reach most of the engine plus ruamel,
  markdown-it, rich, and typer before any renderer exists -- have a guard of their own that
  writes the same line to stderr directly, since the renderer is what would have failed to
  import.

  The repair is at the command-line boundary and touches no warning emission: every category,
  filter, and message is unchanged, ordinary runs are byte-identical, and a library consumer
  calling `load_lattice()` directly still receives the raised warning itself. Catching the base
  `Warning` class is what makes the coverage complete rather than site-by-site -- it reaches this
  engine's four warning sites, the two paths where ruamel raises its own `ReusedAnchorWarning`
  directly (config loading and `reconcile`'s rewrite reread), and any category a dependency adds
  later. AD-39 in ARCHITECTURE.md owns the reasoning and amends AD-29, which had recorded the
  traceback as a measured, accepted cost.

  Escalating remains a way to make any advisory fatal, not a way to make one fatal on its own:
  the filters decide which warnings are raised, and the first one raised is the one that ends the
  run.

- A `--config` path, a cache location, and a `--docs-root` value carrying a terminal control
  character no longer reach human-facing output raw. Running `check --config` against a config
  named with an embedded escape sequence printed those bytes verbatim under `NO_COLOR`, so a
  crafted name could recolor or overwrite the diagnostic printed above it. Eleven message
  construction sites across config loading, the load-cache write warning, and `init` now build
  their path through the same display spelling every document path has used since 5.0.

  Output moves as a result: these paths are quoted now, as `impact` and `reconcile` paths already
  were. `init` reports `wrote '.doc-lattice.yml'` rather than `wrote .doc-lattice.yml`, and the
  same for its already-exists and write-failure lines. Nothing about which files are read or
  written changes -- the raw path is still what the engine opens, and the staged file `init`
  writes keeps its exact name.

- A project root carrying a terminal control character no longer reaches human-facing output raw
  through a `reconcile` lock failure. Six diagnostics in the transaction layer interpolated it
  directly: the two that report an unresolvable or unreadable root, the two that report a lock
  bound to a different root or a replaced root directory, the cleanup failure a clean exit
  raises, and the lock setup failure. Running `reconcile` from a repository whose directory name
  holds an escape sequence put those bytes on stderr, where a cursor-up could overwrite the
  diagnostic printed above. All seven path operands at those six sites now build through the
  same display spelling every other path has used since 5.0.

  Output moves as a result: these lines quote the root they name. The static guard that enforces
  the boundary gained the vocabulary to see them, which is why the sites went unreported while
  the module was already scanned. `project_root` is now recognized in every module rather than
  in `config.py` alone, since it is a path wherever it appears; that widening reports no sink
  beyond these six. `requested_root` is recognized in the transaction module, where the only
  code that audits it lives.

- A hand-edited reconcile journal whose rejected key carries a terminal control character no
  longer puts that character on stderr through the message refusing it. Every journal wire model
  forbids unknown keys, and a rejected key is reported as pydantic's error *location*, which the
  invalid-journal diagnostic interpolated raw. A crafted key could therefore recolor the
  diagnostic or overwrite the line above it under `NO_COLOR`, on any journal that failed
  validation for any reason. Journal validation failures now render through the same module the
  config and frontmatter boundaries have used since 5.0, which spells a control-bearing location
  part and drops pydantic's URL and echoed input.

  Output moves as a result: a journal that fails its wire model now reports as a header naming
  the file, one indented line per error, and the manual-remediation sentence on its own final
  line, rather than as one sentence carrying pydantic's whole rendering. A rejected key is also
  answered with the keys the model that rejected it accepts, so a version 1 journal is never
  offered version 2's `provenance`. Every other invalid-journal diagnostic is unchanged, and what
  the journal format accepts is unchanged. AD-35 in ARCHITECTURE.md owns the extension to this
  boundary and AD-36 records that its own provenance decision does not move.
- `reconcile` no longer hard-wraps its human output at the terminal width. Each per-file record
  (`reconciled 'pc-design.md': art-direction#accent`) and the `nothing to reconcile` all-clear are
  now one record on one line at any width, so a long document name or target ref stays intact for
  a line-oriented pipeline instead of breaking mid-token on a narrow console. They join the
  one-record-per-line contract the `impact`, `check`, `lint`, and `linear stale-shipped`
  renderers already carry, and the one `reconcile --recover` had already opted into for its own
  journal-path lines. Styling is unaffected -- these are CLI adapters covered by the
  console-wide `--no-color` lever -- and `--format json` output is unchanged.
- `init` no longer hard-wraps its three remaining status records at the terminal width. The
  `wrote '.doc-lattice.yml'` and `'.doc-lattice.yml' already exists, leaving it untouched` lines
  and the `workflow triggers on branch <name> (<source>)` line are now one record on one line at
  any width, so a long `--default-branch` value or a long probed `origin/HEAD` target stays
  intact instead of breaking mid-token on a narrow console. The placement guidance printed
  beside them is prose and still wraps, and the baseline guidance had already opted out.
  Styling is unaffected and stdout is unchanged.
- The test suite shipped in the sdist runs from an unpacked sdist again. Five test modules read
  files the sdist deliberately does not carry -- `scripts/audit_action_runtimes.py`,
  `scripts/check_action_pin_correspondence.py`, `.github/workflows`, `RELEASING.md`, and
  `MANAGED_CI.md` -- and were still being shipped, so unpacking the archive and running `pytest`
  stopped with two collection errors (both `run_path` modules, raising `FileNotFoundError` before
  any test ran) and 31 further failures in the other three. The five are now excluded from the
  sdist alongside the nine repository-only modules already listed there, and
  `tests/test_package_metadata.py` fails if the manifest's exclude list and its
  archive-membership denial set ever name different modules. Nothing new is shipped to make these
  tests portable: `MANAGED_CI.md`, `RELEASING.md`, `scripts/`, and `.github/` all stay out of the
  archive, since nothing in the distribution reads them. `tests/workflow_helpers.py` and its test
  stay shipped, because the helper only builds paths at import time and its test drives it
  entirely from `tmp_path`. The installed package is unchanged -- this is a source-archive
  contents fix, and the wheel never carried tests at all.

## [5.0.0] - 2026-08-19

### Migration

Seven things to act on, plus two changes below that need no action in a default environment. The
first is the printed workflow, and it applies to every ordinary and recipe install. The workflow
`doc-lattice init` prints now triggers on a resolved default branch rather than a hard-wired
`main`, so regenerate your user-owned `.github/workflows/doc-lattice.yml` from the target release
and replace the checked-in file, exactly as the ordinary upgrade path in README.md already
describes. Check the branch the run reports on stderr against the branch you actually gate on
before committing. Pass `--default-branch` explicitly to make the upgrade reproducible instead of
dependent on the `origin/HEAD` of whichever checkout you ran it in; a repository already on `main`
that regenerates from a checkout with a healthy `origin/HEAD` gets a byte-identical workflow.
Adopters whose default branch is not `main` were previously running a workflow that installed
cleanly and never triggered, and this is the release that fixes it.

The second is the managed GitHub and Linear CI setup, which this release removes. A managed
installation converts rather than upgrades, and it will not tell you so itself; see **Removed**
below for why, and `MANAGED_CI.md` for the procedure.

The third is the frontmatter error code, and it applies only to a caller that reads the code
rather than the message. A broken frontmatter block now exits with `FRONTMATTER_ERROR` instead
of `CONFIG_ERROR`, so repoint anything matching on the printed code, and anything catching
`ConfigError` around `parse_meta` or `load_lattice`, at `FrontmatterError`. It is a sibling of
`ConfigError`, not a subclass, so an existing `except ConfigError` stops catching it. Catching
`ProjectError` keeps working unchanged. See **Changed** below.

The fourth is the `init` write error code, and like the third it applies only to a caller that
reads the code rather than the message. A filesystem failure while `init` writes
`.doc-lattice.yml` now exits with `INIT_PERSISTENCE` instead of `CONFIG_ERROR`, which sent users
to a config file `init` never reads. That covers a read-only filesystem, a permission-denied
working directory, and the abnormal case where cleaning up the staged file also failed and left
an orphan behind. Repoint anything matching on the printed code, and anything catching
`ConfigError` around `init`, at `InitPersistenceError`. It is a sibling of `ConfigError`, not a
subclass, so an existing `except ConfigError` stops catching it. Catching `ProjectError` keeps
working unchanged. Rerunning `init` in a directory that already has a `.doc-lattice.yml` is
unaffected: the file is still left untouched and the run still exits 0. See **Changed** below.

The fifth needs nothing from you, and is recorded here because a format bump usually would. The
reconcile transaction journal moves to version 2, adding a required `provenance` block and
pretty-printing. Upgrading across an interrupted run is safe in both directions of the crash
window: a version 1 journal left by an earlier release is still recovered normally, whether it is
`prepared` or `committed`, because recovery inspects the declared version before validating the
rest. So you do not need to drain outstanding transactions before upgrading. Only version 2 is
written, so a recovered version 1 journal is removed rather than rewritten, and the first
transaction after the upgrade writes the new format. What you cannot do is downgrade with a
journal outstanding: an earlier release accepts only version 1 and will refuse a version 2 journal
as invalid, so run `doc-lattice reconcile --recover` to completion before rolling back. See the
transaction-artifacts section of `RECONCILE.md` for the field list.

The sixth is the one to check only if you run doc-lattice in an environment that has the optional
`ruamel.yaml.clib` accelerator installed, which no lock of this project produces but another
package may pull in. A frontmatter block defining one anchor name twice used to fail the load
there, and now loads: the document becomes a tracked node, so it can contribute edges to a report
and change a `check` exit code. If you were relying on that failure to keep a document out of the
lattice, exclude it with `ignore_globs` or drop its `id` instead. A frontmatter block that declares
its own `%YAML` version moves too, and in the other direction: the accelerator ignored the
directive outright, so such a block was read under YAML 1.2 no matter what it declared, and it is
now read under the version it declares, exactly as `reconcile` has always reread it. Under a
declared `1.1` an unquoted `on`, `off`, `yes`, or `no` is a boolean rather than a string, so
`id: on` becomes an invalid-frontmatter error instead of a node named `on`; quote the scalar to
keep the old value. Only one spelling of that block is supported, so check yours before assuming it
is affected: the directive has to sit above a document-start line that does not strip to `---`,
such as `--- !!map`, because a plain `---` closes the frontmatter block instead. An environment
without the accelerator, which is what every lock of this project produces, sees no change at all
in what loads.

The seventh is the frontmatter value rule, and it is the fifth thing to act on. `id`, `title`,
every `tickets` entry, `derives_from[].ref`, and `derives_from[].seen` may no longer decode to a
control character: any C0 code point (`U+0000` to `U+001F`), DEL (`U+007F`), or any C1 code point
(`U+0080` to `U+009F`) now fails the load with a `FRONTMATTER_ERROR`. Three spellings reach this,
and only the first is the one the vector was reported for:

1. **An escape in a double-quoted scalar**, which is the only way to write ESC, DEL, NUL, or a
   C1 control into a value at all, since YAML refuses each of those as a raw byte in the file.
   `"\u001b"` is the spelling to picture but not the only one to search for: `\0`, `\a`, `\b`,
   `\t`, `\n`, `\v`, `\f`, `\r`, `\e`, and `\N` each name a refused code point directly, and
   `\xNN` and `\U00000000` reach the same points `\uNNNN` does.
2. **A literal tab**, which is the one control character YAML does admit as a raw byte, inside a
   double-quoted, single-quoted, or block scalar. Nothing about it is visible on screen, so it
   has to be searched for rather than read for.
3. **A newline a block scalar keeps**, whether at the end of the value or inside it. Tab,
   newline, and carriage return are C0 controls, so both spellings are refused. `|` or `>`
   without `-` keeps a *trailing* break, which `|-` and `>-` chomp away. An *interior* break is
   the case to look for after that, because chomping does not touch it: a `|-` spanning two
   lines is already chomped and still constructs a newline between them. The folded styles (`>`
   and `>-`) join adjacent lines with a space, so `>-` is the block spelling that survives this
   rule for a value written across lines, and `|-` survives it only on one line. Folding stops
   at a blank line, which is a paragraph break and constructs a newline `>-` does not chomp, so
   keep a folded value's lines adjacent. Search every one of the five keys, not just the two
   that carry hashes and refs: `id`, `title`, `tickets`, `ref`, and `seen` are all constrained,
   and `id` and `title` are where a multi-line value is most likely to have been written on
   purpose.

The frontmatter reference in README.md carries one scan covering all three. It reads the fence
the way the loader does, so a file saved with a byte-order mark, CRLF endings, or a padded `---`
is scanned rather than skipped, and then it loads the block and inspects the five values rather
than pattern-matching the lines they were written on. That distinction is what makes it exact:
every spelling above is a property of the constructed value, and the ways to write one value are
open-ended, from a `- ref:` behind a sequence dash to a block scalar carrying an anchor to an
explicit `? key` pair. Run it where doc-lattice is installed, since it borrows that
installation's `ruamel.yaml`.

All five had a working use, so treat this as a real scan rather than a formality. A folded
`title` is the obvious one. Less obviously, an `id` written `|` constructs a trailing newline,
and a `ref` written the same way constructs the same string, so the two matched and the edge
resolved and reconciled to OK; verified by execution against the pre-change tree. Only a `seen`
carrying a break was already broken, since it could never equal a content hash. A literal
carriage return or NEL needs no search: YAML reads both as line breaks and folds them to a space
before any value is constructed. See **Changed** below and the frontmatter reference in
README.md.

The eighth is where the error code is printed, and it is the sixth thing to act on. It applies
only to a caller that scrapes stderr; the exit code and the code values themselves are unchanged.
Every project error now carries its code beside the severity instead of after the message, so
`error: docs_roots entry 'notes.txt' ... must be one or the other (CONFIG_ERROR)` becomes
`error (CONFIG_ERROR): docs_roots entry 'notes.txt' ... must be one or the other`. Match the
prefix `error (<CODE>): ` rather than a trailing `(<CODE>)`, and match it on the first line rather
than the last. The move is what makes a multi-line diagnostic readable: the message keeps the line
breaks it is built from, so a trailing code landed at the end of the last detail line and read as
part of that field's parenthetical rather than as the error's code, which the multi-line config
and frontmatter diagnostics above make routine. Single-line diagnostics move with it deliberately,
so that one prefix matches every project error rather than two grammars splitting on whether the
message happens to have a newline. A diagnostic that carries no code is unaffected and still
prints `error: <message>`. That is every usage error, and also the `reconcile --recover` problem
report, which its own command adapter prints and which exits 2 like a project error does. So the
parenthetical now marks exactly the diagnostics that carry a code to match on, and not every
stderr line that ends the run at exit 2.

The ninth is how a YAML parse failure prints, and it is the seventh thing to act on. Like the
eighth it applies only to a caller that scrapes stderr; the exit code and the error codes are
unchanged. A frontmatter block or config file that fails to load at all is still reported with
this project's own header naming the file, and the parser's message that follows it is now quoted
as one line, with its line breaks written as `\n`, instead of printed raw across several. Match
the header rather than the parser's wording, which was never this project's to promise and differs
across `ruamel` releases in any case. The reason is that the parser echoes your document back at
you: a duplicate key is reported by quoting the key and both of its values, so a block spelling a
control character twice put those bytes on your terminal through the diagnostic refusing it. See
**Security** below. What you lose is the parser's caret, which underlined a column in a source
snippet and points at nothing once the message is one line; the `line: N, column: M` coordinates
beside it survive, and nothing is dropped, so the original message is still readable through the
escapes.

Everywhere, in both environments, the load cache is rebuilt once. `CACHE_VERSION` rises to 5 so a
warm run can replay a new diagnostic, and entries written before it are discarded rather than read
as documents that reused no anchor. No action is needed: the next run rebuilds the file.

### Added

- `init` gained `--default-branch NAME`, and the workflow it prints now filters both its `push`
  and `pull_request` triggers on a resolved branch instead of a hard-wired `main`. Precedence is
  explicit flag, then the local `origin/HEAD` remote-tracking ref, then `main`, and the run names
  the branch and its source on stderr so a wrong guess is visible rather than buried in generated
  YAML. The probe is deliberately best-effort: no remote, no `git`, a directory outside a
  worktree, a timeout, or an `origin/HEAD` whose target no longer exists all fall back quietly,
  and ordinary `init` still has no Git requirement. A stale `origin/HEAD` whose target does still
  exist locally cannot be detected without network access, which is the residual the reported
  source line and the explicit flag exist to cover. A branch name that is supplied or detected but
  is not a supported ASCII literal name is rejected with an actionable error rather than rendered,
  because a GitHub `branches:` filter is a glob pattern and would match `*`, `?`, `[`, `]`, and
  `!` as patterns. See the `init` section of README.md.
- A hand-installable recipe for protected Linear reporting in CI, published in `MANAGED_CI.md`.
  It is the successor to the managed GitHub and Linear setup removed below, and it is complete:
  the offline workflow plain `init` scaffolds, the trusted-main Linear workflow as copyable text,
  the exact `gh` sequence that creates the `main`-only environment and its dedicated secret, the
  preconditions and readbacks that gate it, the manual review that replaces the removed offline
  audit, and the upgrade and conversion procedures. It is the only documented path to the
  protected setup. `SECURITY.md` now names that published
  workflow in its in-scope boundary, and tests parse both the documented workflow and the
  documented `gh` procedure, enforcing the trigger set, the whole `if:` guard, the environment
  binding, final-step-only secret mapping, action-pin parity, and the branch policy and host
  pinning the shell steps establish.
- A security policy in `SECURITY.md`, linked from README's documentation table, covering
  supported versions (the latest release only), the private reporting path, what a report should
  carry, response targets, the coordinated-disclosure expectation, and what is in and out of
  scope. GitHub private vulnerability reporting is enabled on the repository, which is the
  channel itself; the file describes it but does not create it.
- A bad-release playbook and an accounts-and-access record in `RELEASING.md`. The playbook is
  stage-aware, because the pipeline crosses two one-way doors: it separates a mechanical failure
  that should be rerun from a bad payload that must not be, covers the window where the
  immutable tag and GitHub Release exist but PyPI publication has not been approved, and gives
  the operator procedure for a PyPI yank along with the caveat that a yank does not stop an
  adopter pinned with `==` or `===`, and the narrow escalation path for an artifact that has to
  stop being downloadable at all. A security response is ordered fix first, then yank, then
  advisory, because a yank reason is public as soon as it is saved. The access record covers
  the Trusted Publisher binding and
  what a rename or transfer breaks, the `CLAUDE_CODE_OAUTH_TOKEN` rotation procedure and its
  triggers, and PyPI operator continuity. A new "Who can release" section separates landing a
  version bump, approving the `pypi` environment, and operating the PyPI project, which are
  three authorities that do not gate each other.
- Internal: a Hypothesis round-trip fuzz gate for the reconcile frontmatter rewriter,
  `tests/test_reconcile_fuzz.py`. The subset AD-31 declares had only hand-written cases as standing
  evidence, so rewriter edge cases were found by review iteration on a change rather than by CI.
  The gate generates documents across that subset and checks each rewrite against an independent
  model-derived oracle, enforcing the record exactly rather than a stricter contract. It is
  derandomized and runs in about ten seconds. See
  [AD-31](ARCHITECTURE.md#ad-31-the-reconcile-rewriter-supports-a-declared-frontmatter-subset).

### Changed

- **BREAKING:** a frontmatter value that decodes to a control character is now refused at load
  instead of being carried into output. `id`, `title`, every `tickets` entry,
  `derives_from[].ref`, and `derives_from[].seen` may hold no C0 code point (`U+0000` to
  `U+001F`), no DEL (`U+007F`), and no C1 code point (`U+0080` to `U+009F`), and a document
  spelling one fails with a `FRONTMATTER_ERROR` naming the key and the code point rather than
  echoing the value. This closes the second half of the vector `--no-color` output already
  promised was closed: YAML refuses a raw control byte in the file, but a double-quoted scalar
  decodes `\u001b` into a real ESC, and `check` and `graph` printed it, so a crafted document
  could recolor or overwrite the lines of a gate's report. Refusing at validation rather than
  escaping at each sink is what keeps control characters out of identity and out of every
  renderer at once, including the two graph grammars, and needs no rule for how a display
  spelling would compose with them.

  Tab, newline, and carriage return are C0 controls and are included: the output being protected
  is line-oriented, so a newline in a value forges a whole report row rather than restyling one.
  That is also the only compatibility cost worth planning for. It narrows the block-scalar
  spellings AD-31 declares supported for `ref` and `seen` in two ways, not one. Clip and keep
  chomping (`|`, `>`, and their `+` forms) construct a trailing line break, so those are refused
  where `|-` and `>-` are not. Independently of chomping, a *literal* block scalar (`|` in any
  chomping mode) keeps the breaks between its own lines, so a multi-line `|-` is refused too;
  only the folded styles join their lines with a space, which leaves `>-` as the block spelling
  that survives across lines and `|-` as one that survives on a single line. The reconcile
  rewriter is unchanged and still round-trips such a document byte for byte;
  only the strict tracked-document load moved. Machine channels are untouched for every document
  that still loads, and a refused document fails before format selection rather than reaching one
  channel and not another. See
  [AD-35](ARCHITECTURE.md#ad-35-a-frontmatter-value-carrying-a-control-character-is-refused-not-re-spelled)
  and the frontmatter reference in README.md.
- Warnings a command emits while loading now render in the CLI's own stderr voice, as
  `warning: <message>`, instead of through Python's default formatter. A skip previously arrived
  behind an absolute path into this package and a `UserWarning` category, with the raising source
  line printed beneath it, while every error in the same command rendered as one clean
  `error: ... (CODE)` line. The id-less-frontmatter skip made that the routine sight rather than
  an edge case. Filtering is unchanged and still documented in README: Python applies
  `PYTHONWARNINGS` before the presentation stage this replaces, so `PYTHONWARNINGS=ignore` and the
  `ignore:skipping` literal-prefix form behave exactly as before, as do category matching and
  repeat suppression. The substitution covers the config read and `reconcile`'s rewrite pass as
  well as the document load, and is scoped to those phases and restored afterwards on both the
  normal and the failing path, so importing `doc_lattice` as a library still gets standard
  `warnings` behavior. One consequence is worth knowing when embedding: for the duration of a
  wrapped phase a warning reaches this renderer rather than a `catch_warnings(record=True)`
  recorder or a `logging.captureWarnings()` router. See
  [AD-29](ARCHITECTURE.md#ad-29-a-skipped-files-reason-is-cached-data-and-is-reported-from-one-site).
- **BREAKING:** frontmatter schema and lattice-intent failures now carry the new
  `FRONTMATTER_ERROR` code instead of `CONFIG_ERROR`, which sent users to the config file for a
  broken document. Both raise sites move together: a `NodeMeta` validation failure, and the guard
  that fires when a block declares `authority`, `derives_from`, or `tickets` with no `id`. Moving
  only the first would have left the reported `idd` plus `derives_from` typo still pointing at
  config, which is the defect the change exists to close. Read, decode, and YAML parse failures
  keep `UNREADABLE_DOC`; that boundary was already coherent. Anything matching on the printed code
  or catching `ConfigError` around `parse_meta` or `load_lattice` needs repointing, since
  `FrontmatterError` is a sibling of `ConfigError`, not a subclass.
- **BREAKING:** a filesystem failure from `init` now carries the new `INIT_PERSISTENCE` code
  instead of `CONFIG_ERROR`. This is the defect the frontmatter code above closed at the load
  boundary, one layer over: `init` never reads `.doc-lattice.yml`, so pointing at config named a
  file with no part in the failure. Both raise sites move together: the `OSError` from the
  scaffold write, which is what a read-only filesystem or an unwritable directory produces, and
  the noted `FileExistsError` that means the staged file outlived a failed cleanup. Moving only
  the first would have left the orphan diagnostic, which names a stray staged path in the working
  directory, still reported as a config error. A bare `FileExistsError` is unchanged and is not
  an error at all: an existing config is left untouched, the guidance still prints, and the run
  still exits 0. `InitPersistenceError` is a sibling of `ConfigError`, not a subclass.
- Config and frontmatter validation errors are now formatted by doc-lattice rather than delegated
  to pydantic's multi-line renderer. Both load boundaries render through one formatter, so they
  cannot drift apart. The message names the file it came from, which the sibling read and parse
  errors already did and these did not, and renders one line per error carrying the full field
  location and pydantic's human message. The `pydantic.dev` URL, the echoed input value, and the
  machine-readable `type` tag are gone; the domain-authored messages themselves are unchanged, so
  a message that deliberately quotes the offending value still does. A key rejected by
  `extra: forbid` also lists the accepted keys, derived from the model so a future field cannot
  leave the list stale, and `binding_layers` additionally gets the 1.x migration sentence, since
  the blanket forbid is the only thing that catches it. An error pydantic reports against no
  field, which is either a validator that runs on the whole model, such as the `cache_trust_stat`
  check, or a file whose top level is not a mapping at all, gets an explicit `<config>` or
  `<frontmatter>` marker rather than an invented field name.
- `--format github` annotations are now rendered relative to `GITHUB_WORKSPACE` when it is set and
  contains the document, rather than always to the invocation working directory. Under GitHub
  Actions that is the repository checkout root, so inline pull-request annotations no longer
  vanish for a run invoked from a subdirectory, where the previous behavior silently degraded to
  an absolute path GitHub cannot attach to a diff. The base is selected before rendering, so a
  workspace set to a directory that does not contain the document still takes the working-
  directory fallback instead of being passed through to an absolute path. With the variable unset
  the behavior is unchanged, including the absolute fallback. The base is deliberately not the
  config file's project root: using it would strip the leading path from a monorepo config under
  `packages/game`. See the `--format` section of README.md.
- `--format github` now warns on stderr when a run emitted an annotation GitHub cannot attach to
  the pull-request diff. The absolute-path fallback above is a correct last resort but a silent
  one, and its symptom is a failing gate with nothing shown on the diff, which is unpleasant to
  debug from a workflow log. The warning fires at most once per run and names the base and every
  document outside it. Stdout stays exactly the workflow commands GitHub parses.
- Error codes are now a declared domain in `constants.py` rather than a string literal per
  exception type. The code a `ProjectError` carries is printed beside every diagnostic and is a
  documented migration surface, so it is typed like the project's other shared string domains,
  and the test suite derives its expectations from the class tree instead of a hand-maintained
  list that had already fallen three types behind. No code value changes.
- Whether a document counts as tracked no longer depends on whether the optional
  `ruamel.yaml.clib` accelerator is installed. The strict tracked-document load asked ruamel for a
  plain safe loader, which silently uses the C parser wherever that accelerator is present. No lock
  of this project installs it, but any other package in an environment may pull it in, and the two
  parsers do not accept the same documents: a frontmatter block defining one anchor name twice is
  accepted by the pure Python parser, which warns and rebinds the name, and refused outright by the
  C composer as a duplicate anchor. The same file was therefore a tracked node on one machine and
  an unreadable document on another, and `check` reached different verdicts for it with nothing in
  this project having changed. The load now asks for the pure Python parser explicitly, at every
  construction including the one a `%YAML` directive forces, so the set of documents that count as
  tracked is fixed here rather than by an adopter's environment. Reused anchor names are supported
  rather than refused, which is what YAML 1.2.2 specifies and what the reconcile rewriter already
  implemented. In an accelerator environment a declared `%YAML` version now takes effect on the
  strict read as well, since the C parser ignored the directive entirely; that is the same
  resolution `reconcile` has always reread such a block under, and it is unchanged without the
  accelerator, where the directive already took effect. Config parsing is deliberately unchanged
  and still takes ruamel's default. See
  [AD-33](ARCHITECTURE.md#ad-33-the-strict-frontmatter-load-pins-the-pure-python-parser).

- A frontmatter block that defines one anchor name twice now says so naming the file, on every
  run. The pure parser raises a `ReusedAnchorWarning` from inside ruamel, which identifies the
  document only as `<unicode string>` and never fires at all when the file is served from a warm
  load cache, so a corpus loaded from cache went quiet about a rebound alias while still building
  the edge it rebound. The warning is now captured and re-reported as
  `reused anchor in <path>: ...` from the same single site that reports an id-less skip, and the
  fact is stored in the load cache so a warm run repeats it. `CACHE_VERSION` rises to 5
  accordingly. Any other warning raised while loading frontmatter is untouched.

- The reconcile transaction journal is now version 2, and records what produced it. A journal
  previously carried only `version`, `state`, and `entries`, so an operator holding one after a
  crash could not tell when it was written, which doc-lattice wrote it, or what command produced
  it. Version 2 adds a required `provenance` block carrying `created_at`, `tool_version`, and a
  typed `selector` (`mode` of `all` or `downstream`, the `downstream_id` when there is one, and
  the `ref` the run narrowed to), and the journal is written pretty-printed so it can be read
  without reformatting. The selector is recorded as typed fields rather than a command line, so
  recovery never parses argv. All three values are captured once when the transaction is prepared
  and copied unchanged into the committed marker, which previously forwarded only the version and
  entries, so a crash journal cannot disagree with itself. Provenance is required and immutable: a
  version 2 journal missing any of it is rejected rather than recovered with blank fields. Version
  1 journals stay recoverable in both states, so the bump strands no interrupted run; see
  **Migration** above.
- **BREAKING:** `reconcile --recover` no longer reports a full rollback it did not perform, and no
  longer deletes the evidence needed to finish one by hand. Rolling back a `prepared` journal
  previously skipped any destination whose contents did not match the recorded after image, then
  cleaned up unconditionally: an unrelated edit or a deletion left the destination unrestored while
  the command printed `rolled back reconcile transaction`, exited 0, and removed the before image
  and journal that were the only way to recover. Each destination is now classified as restored,
  already equal to its before image, or unresolved, where unresolved means matching neither
  recorded image, absence included. Any unresolved entry makes the run a partial rollback: it
  reports the new `partially_rolled_back` action, names every unresolved destination on stderr as a
  project-relative path, retains the journal and every remaining stage without cleaning anything,
  and exits 2. A destination already equal to its before image is still a full rollback. Recovery
  stays idempotent while an entry is unresolved, and a rerun after manual repair completes the
  cleanup normally.
- **BREAKING:** automatic pre-run recovery now stops the command on an incomplete recovery. It
  previously logged any non-`none` action and continued into lattice loading, planning, and commit,
  which planned against a tree that was never fully restored. It now reports the problem on stderr
  and exits 2 before loading anything.
- **BREAKING:** the `reconcile --recover --format json` object gained `restored`, `already_before`,
  `unresolved`, `orphans`, and `scan_errors` alongside `action` and `journal`, and `action` gained
  the `partially_rolled_back` value. Consumers asserting the exact key set need updating; the
  previous two keys are unchanged in name and meaning.
- **BREAKING:** the `reconcile --recover --format json` object gained an eighth key, `provenance`,
  carrying the `created_at`, `tool_version`, and `selector` a version 2 journal records. The key is
  always present. It is `null` for a recovered version 1 journal, which recorded no provenance, and
  for a run that found no journal at all; `action` already separates those two, so no
  `journal_version` key was added. `created_at` is emitted in the spelling the journal is written
  in, ending in `Z`, whichever accepted spelling the file itself used. Consumers asserting the
  exact key set need updating again; the previous seven keys are unchanged in name and meaning.
  The human `--recover` output gained the same three fields, indented under its summary line, with
  `provenance: not recorded by journal version 1` in place of blank fields for a version 1 journal
  and nothing at all when there was no journal. Automatic pre-run recovery is unchanged and still
  reports only its action. The journal's own strings are quoted and escaped in the human lines
  rather than refused at load, so a hand-edited journal still recovers; see AD-36.
- `reconcile --recover` now reports orphaned transaction artifacts that no retained journal
  accounts for, instead of printing `nothing to recover` over a tree that still holds them. The
  scan runs after journal handling in every branch, so a journal publication interrupted between
  linking the journal and removing its helper stage reports both in one invocation, and it covers
  staged images in nested document directories as well as journal temporaries. Orphans are reported
  as project-relative paths with a nonzero exit and are never deleted. With no journal present and
  orphans found, the summary reads `no reconcile journal to recover` rather than
  `nothing to recover`.
- An in-process abort no longer reports `no files were reconciled (rollback complete)` after a
  rollback that may have skipped entries. It distinguishes destinations whose replacement it
  attempted from those it never reached, treating a destination as possibly applied from before the
  call because `replace_staged` renames before it synchronizes. An ordinary pre-replace conflict on
  one file therefore still reports a complete rollback of everything the run touched.
- RECONCILE.md documents the partial-rollback and orphan contracts, and ARCHITECTURE.md AD-5
  records the classification, evidence-retention, and exit-code decisions.
- The frontmatter syntax `reconcile` supports is now declared rather than implied. AD-31 in
  ARCHITECTURE.md records it as five layers: the validated schema, the spellings accepted per
  writable position and per load phase, what a rewrite preserves, what it may mutate beyond the
  `seen` scalar, and when a refusal is guaranteed. RECONCILE.md now links to it for those rules
  and keeps only what they mean for a run, so no contract is stated in both. The subset itself is
  unchanged: the record documents what the rewriter already does, so that a spelling outside it is
  a known boundary rather than an open question. Writing the guarantees down did surface one place
  the code did not honor them, fixed below.
- Regenerating and verifying the generated slug data is now reproducible offline and no longer
  breaks when Node's ICU advances. `scripts/generate_github_slugger_data.py --check` previously
  hard-failed on any Node reporting a JavaScript Unicode version other than 17.0, recorded the
  generating Node nowhere, and shelled to `npm install github-slugger@2.0.0`, so it needed the
  network and would have become unrunnable at Unicode 18. The exact runtime is now pinned in
  `.nvmrc` (`v24.13.1`, all three components, because nvm resolves a partial version to the
  latest matching patch and Node 24.13.1 itself carried an ICU update), validated by the
  generator, and rendered into the artifact as a new `GENERATED_NODE_VERSION` line. Upstream
  input is the unmodified npm tarball vendored at `vendor/github-slugger-2.0.0.tgz`, verified
  against a pinned SHA-512 before extraction and resolved by default; `--package-root` stays as
  an explicit override and the implicit `npm install` fallback is gone. The tarball digest is the
  complete upstream-input identity, since the evaluator runs `index.js`, which imports
  `regex.js`; `UPSTREAM_REGEX_SHA256` is unchanged and still records artifact-level behavior
  provenance. The vendored bytes reproduce the existing artifact exactly, so no slug, section
  ref, or Unicode behavior changes. AD-28 in ARCHITECTURE.md records the reasoning. This is a
  maintenance-path change only: the shipped package still has no Node dependency, and `vendor/`
  is excluded from both the sdist and the wheel.
- **BREAKING:** frontmatter with no `id` is no longer silently dropped. A file whose fenced
  frontmatter declares any of `derives_from`, `authority`, or `tickets` but no `id` is now a tool
  error naming the file and the keys it declared, and exits 2. Any other id-less fenced block is
  still skipped, but now warns on stderr naming the file; the exit status is unchanged. A file with
  no opening `---` fence stays silent, as does a fence holding no YAML mapping. Previously all of
  these were the same silent omission, so a one-character typo in the `id` key removed a document
  **and every edge it declared** from the gate while `check` stayed green and said nothing.
  Migration for the new exit 2: run any lattice command once; the message names the file and the
  key. Restore the `id` (checking it for a typo) or, if the file is genuinely untracked, delete the
  lattice keys. Migration for the new stderr warning: it is expected on a docs root that carries
  frontmatter belonging to another tool, and it does not fail a gate. Drop the file from discovery
  with `ignore_globs` to silence it precisely; `PYTHONWARNINGS` also applies but no available form
  targets this warning alone, since its message field is a literal prefix and the symlink-escape
  warning shares that prefix. Those three keys are an exact set, not "every frontmatter field
  except `id`": a block carrying only `title` or `layer` warns rather than failing. AD-29 in ARCHITECTURE.md records the decision, including why the
  disposition is cached rather than recomputed.
- The load cache records why a file is not a lattice node, not just that it is not one, and
  `CACHE_VERSION` rises to 4 so entries written before the field are discarded rather than read as
  ordinary skips. Without this, the warning above would appear on a cache-free or cold run and
  vanish on a warm one, since a warm run returns before parsing; AD-12 requires the cached and
  uncached tiers to report the same thing. What is stored is the reason, never the rendered
  message, because a cache slot is shared across worktrees and the message names the path the
  current run discovered. No action is needed: the version bump discards the old file and the next
  run rebuilds it.
- **BREAKING:** human-format `check` output now lists problem edges only. The per-edge `OK`
  rows are gone from the default listing, so a problem-free lattice prints its verdict line
  alone. This matches `lint`, which has always printed violations only, and keeps the default
  invocation on a large lattice from burying a one-line verdict under thousands of unread `OK`
  lines. The verdict line added in 4.0.0 is what makes the omission safe rather than lossy: the
  total and every per-state count, `OK` included, stay on it. Exit codes, summary counts, and
  the classification itself are unchanged, as is `--format github`, which already suppressed
  `OK` records. Migration for a consumer that needs the `OK` rows: read `--format json`, which
  still carries one record per edge including `OK` ones, or ask for them explicitly with
  `check --only OK` (repeat `--only` to name every state when the previous full listing is
  wanted). Anything gating on the exit code or grepping for a problem state is unaffected.
- Every runtime dependency now carries an upper bound. `typer`, `rich`, and `pydantic` were
  declared by floor alone and are capped at their current majors (`typer<1`, `rich<16`,
  `pydantic<3`), so an upstream major can no longer change what a pinned
  `uvx --from doc-lattice==X` invocation executes with no doc-lattice release involved. The
  versions this project resolves today are unchanged. AD-27 in ARCHITECTURE.md records the
  reasoning, including why `ruamel.yaml` additionally gets the `yaml-compatibility` CI leg and why
  `markdown-it-py` stays exact-pinned at 4.2.0 despite the transitive constraint that places on
  `rich`.
- The shipped GitHub Actions pins move onto releases that target Node.js 24: `actions/checkout`
  from `v4.4.0` to `v7.0.1`, and `astral-sh/setup-uv` from `v6.8.0` to `v10.0.1`. Both previous
  pins target Node.js 20, which GitHub has deprecated on Actions runners and currently forces onto
  Node.js 24; this moves off that forcing before it stops rather than after. The pins reach
  adopters through the snippet `init` prints and the recipe in MANAGED_CI.md, and are the same
  ones this repository's own workflows run, so all three moved in one change. No input either
  workflow sets changed across the bump. Adopters who copied the recipe and then added inputs of
  their own should note that `setup-uv` changed the `prune-cache` default to `false` in v9.0.0 and
  now disables caching by default on `pull_request_target`, `workflow_run`, and `release` in
  v10.0.0, and that `actions/checkout` v7.0.0 blocks checking out a fork pull request under
  `pull_request_target` and `workflow_run`. The published recipe sets both inputs explicitly and
  uses neither trigger, so it is unaffected.
- `roadmap.md` is renamed to `ROADMAP.md`, matching every other maintained root document. The
  rename is case-only, so a case-insensitive checkout sees no change and a case-sensitive one
  needs the new spelling. README.md, CLAUDE.md, and ARCHITECTURE.md now reference the new name;
  entries in earlier releases keep the spelling that was correct at the time. Its contents were
  also rewritten to track the three release projects rather than the shipped 4.x work.
- **BREAKING:** a project error prints its code beside the severity rather than after the
  message: `error (CONFIG_ERROR): invalid config /p/.doc-lattice.yml:` and its indented detail
  lines, where the code previously trailed the last of those lines. On a multi-line diagnostic
  that trailing position read as the last detail field's second parenthetical rather than as a
  property of the whole error, which the deliberately multi-line config and frontmatter
  diagnostics made routine rather than rare. Placement is owned by `print_project_error`, so it
  moved for every error type at once and no message formatter gained CLI metadata. Single-line
  diagnostics moved with it so one grammar covers both, and a diagnostic that carries no code --
  a usage error, or the `reconcile --recover` problem report -- still prints `error: <message>`.
  See **Migration** above for what a stderr scraper matches instead.

### Removed

- **BREAKING:** the managed GitHub and Linear CI product is gone. `ci audit` and `ci refresh` no
  longer exist, `init` no longer accepts `--github` or `--repository`, and the
  `doc_lattice.github_ci` package and the Git top-level resolver those commands required are
  deleted with them. Nothing generates the four committed artifacts
  (`.github/workflows/doc-lattice.yml`, `.github/workflows/doc-lattice-linear.yml`,
  `.github/doc-lattice-bootstrap.sh`, and `.github/.gitattributes`) any more. Plain `init` is
  unaffected and still prints its three blocks. AD-32 in ARCHITECTURE.md records the decision.

  The replacement is the hand-installable recipe published in `MANAGED_CI.md`, added above, which
  reaches the same protected boundary with workflows you own. It keeps the GitHub environment as
  the authoritative secret boundary, the `main`-only deployment allow list, the trusted job's
  repository, ref, and event guards, and final-step-only mapping of `DOC_LATTICE_LINEAR_API_KEY`
  onto `LINEAR_API_KEY`. It has no repository-wide audit, drift detection, byte-level refresh,
  scripted bootstrap readback, or ownership markers; `MANAGED_CI.md` states that trade in full.

  **If you have a managed installation**, nothing breaks when this release ships, which is the
  trap. A generated workflow pins the exact version that produced it and never hears that a later
  one exists, so the installation goes on running that older release quietly and indefinitely,
  long after it stopped being supported. Pinning it forward is what fails, because the managed
  offline workflow invokes `ci audit`. Convert it instead: replace that offline workflow with the
  one plain `init` scaffolds, adopt the recipe's Linear workflow, leave the protected environment
  and its `DOC_LATTICE_LINEAR_API_KEY` exactly as they are, and retire
  `.github/doc-lattice-bootstrap.sh` together with the `.github/.gitattributes` rule that existed
  only to hold it at LF after checkout. Conversion changes no remote state, and is a local change
  of file ownership from the tool to you. `MANAGED_CI.md` carries the step-by-step procedure, and
  the upgrade path for a recipe installation once you are on it.

### Fixed

- An unknown frontmatter or config key that decodes to a control character is now spelled in the
  diagnostic that rejects it, instead of being echoed into your terminal raw. Safe YAML decodes a
  double-quoted `\u001b` in a mapping *key* exactly as it does in a value, and an unknown key is
  reported by naming the key, so a document spelling `"bad\u001b[31m": 1` put the ESC on stderr
  through the very message refusing it. Only the spelling changed: such a key was already an
  error, and a key carrying no control character still reads exactly as before, so an ordinary
  diagnostic still names `derives_from.0.ref` rather than gaining quotes. This is the key half of
  the value rule under **Changed** above; the two together did not close the whole of what a
  document can print, because a block that fails to parse at all is reported through the YAML
  parser's own message, which echoes the source it choked on. That path is closed by the last
  entry under **Security** below.

- A command whose stderr is closed or full no longer discards its own report. Every console this
  CLI writes through now raises the underlying `BrokenPipeError` on a refused write. Rich's
  default is to point `sys.stdout` at `os.devnull` and raise `SystemExit(1)`, which aimed the
  redirect at file descriptor 1 no matter which stream had failed, so `doc-lattice check 2>` a
  closed pipe printed nothing at all on a healthy corpus and produced an empty document under
  `--format json`. A warning that cannot be written is now dropped rather than allowed to end the
  load, matching the behavior of Python's own warning printer. A benign broken pipe, such as
  `doc-lattice check | head -1` on a healthy corpus, now exits 141 silently instead of the old
  `SystemExit(1)`, which collided with the drift exit code.
- Diagnostics no longer rewrite a colon-delimited word in a path as an emoji. Discovered paths
  reach both the `warning:` and `error:` renderers verbatim, and `docs/a:x:b.md` is a legal
  filename, not a request for an icon.
- The release job validates the changelog section before it pushes the tag, so a release that
  fails its own notes check no longer strands an immutable tag. Extraction ran inside `Publish
  release notes`, which is two steps after `Create and push the tag`: a missing or empty
  `## [X.Y.Z]` section failed the run only after the tag existed and had been pushed, leaving a
  published tag with no GitHub Release and no way forward except cutting another version. The
  implicit `success()` on the dependent jobs did block PyPI publication, so nothing was ever
  published against unvalidated notes; the damage was the stranded tag alone. Generation is now
  its own `Extract release notes` step that runs under the same gate, before both the tag push
  and the network smoke, and writes the file that `gh release create` reads back. Reruns are
  unaffected: the step is gated on `proceed` rather than on `create_tag`, so the resume path that
  finishes a missing GitHub Release still regenerates the notes it needs.
- No command crashes any more on frontmatter whose `!!omap` repeats a key. A block such as
  `extra: !!omap` followed by two items spelling the same key escaped every YAML boundary as an
  uncaught `AssertionError` and printed a traceback, from `check`, `lint`, `impact`, `graph`,
  `linear`, and `reconcile` alike; it is now the ordinary `UNREADABLE_DOC` tool error naming the
  file, and exits 2. It was missed the way any gap that turns on which builtin the safe
  constructor raises is missed: ruamel's `construct_yaml_omap` enforces key uniqueness with a bare
  `assert` rather than raising a `YAMLError`, so `AssertionError` was the one member the shared
  load-error family did not name. No shape that
  loaded before loads differently, and no document that was already refused changes its message.
- `linear stale-shipped` no longer hard-wraps its human output at the terminal width, and no
  longer leaks terminal escapes when styling is off. Each finding and the all-clear line are now
  one record on one line at any width, so a ticket ref, node id, or drifted ref stays intact for
  a line-oriented pipeline instead of breaking mid-token on a narrow console. The renderer also
  opts out of Rich's automatic highlighter, which bolds bare numbers and survives `no_color`, so
  a ticket ref (`GTX-96`) or a numbered id (`adr-001`) no longer emits escapes under
  `--no-color`. This was the one renderer the 4.0.0 `impact` fix and the 4.1.0 `check`, `lint`,
  and `--no-color` fixes did not reach; no renderer now relies on Rich's default
  auto-highlighting. `--format json` output is unchanged.
- A `%YAML 1.1` directive in one document's frontmatter no longer changes how the documents read
  after it in the same run. The shared safe loader cleared `YAML.version` between loads, which is
  enough on `ruamel.yaml` 0.19 but not on 0.18, where the versioned resolver is built once and
  never rebuilt; the declared range admits both. On 0.18 without the optional `ruamel.yaml.clib`
  accelerator that left every later document resolving under 1.1, so an unquoted `on`, `yes`, or
  `no` became a boolean and a leading-zero number became octal, silently and only for documents
  parsed after the one carrying the directive. The loader is now discarded outright whenever a
  directive touched it, which is version independent. The `yaml-compatibility` CI leg gained the
  accelerator-absent half of its matrix, since the C parser ignores directives entirely and so
  hid this on both of the ruamel versions it tested.
- Adopters now have upgrade guidance. README.md gained an Upgrading section split by install
  kind, since ownership differs: the printed pre-commit block is hand-maintained everywhere and
  must be replaced wholesale rather than pin-bumped, an ordinary install replaces its workflow
  from fresh `init` output, and a recipe install additionally replaces its Linear workflow whole
  from MANAGED_CI.md. A managed installation left over from 4.1.0 has no upgrade path and converts
  instead. Every command names the target release explicitly, because `init` prints from the
  running version, so an old binary prints the old blocks.
- Both `reconcile --all` adoption sites now say to commit the annotated input state and start from
  an otherwise clean working tree, so the baseline diff is reviewable and revertible with `git`.
  Neither document previously offered git as the safety net, and reconcile's own failure rollback
  does not reverse a successful but mistaken baseline. README.md owns the rule and MANAGED_CI.md
  links to it at the command site.
- RELEASING.md's checklist now requires a `### Migration` subsection in the changelog section of
  any release that changes output an adopter installs, in shape or behavior, excluding the
  version-pin substitution every release performs. The 4.1.0 section gained that subsection
  retroactively; it is the release whose printed and generated workflow output changed.
- README.md no longer presents the `init` branch probe and its stderr narration as behavior of the
  release it pins. Both the probe and `--default-branch` arrive in 5.0, so an adopter running the
  pinned 4.1.0 command looked for a `workflow triggers on branch ...` line that never appears, and
  had no documented way to notice a workflow gating the wrong branch: that release hard-wires
  `branches: [main]` into both the `push` and the `pull_request` filter of the workflow it prints.
  The Ordinary offline setup section now states the release boundary first, says what a pre-5.0
  install renders and that editing both filters by hand is the only correction available there,
  and marks the probe paragraphs as 5.0 and later. The boundary is worded the way MANAGED_CI.md
  already words the same one for `--default-branch`, so it stays accurate once the pin moves.

### Security

- A document filename can no longer forge or corrupt the CLI's own output. Every human-facing
  path now prints in an unambiguous quoted spelling built at message construction, so terminal
  control bytes in a filename are visible as escapes rather than acted on. Previously a file named
  with an embedded `ESC` put those bytes straight onto stderr through both the typed errors and
  the warnings, and a `ESC[A` in the name moved the cursor up a line and overwrote the diagnostic
  printed before it. Both renderers applied Rich markup escaping, which neutralizes `[tag]` markup
  and does nothing to ANSI. The fix lives in `path_utils.format_path_for_display` and is applied
  where each message is built, so it holds for a direct library consumer that formats a
  `ProjectError` itself, and it covers the success-path console writes (`impact`'s human report,
  `reconcile`'s reconciled lines) as well as the diagnostics. This enforces the `--no-color` /
  `NO_COLOR` promise README already made rather than extending it. AD-34 in ARCHITECTURE.md
  records the raw-path-versus-display-path boundary.

  Two visible outputs change shape: `impact` prints its path quoted inside the parentheses it
  already used, and `reconcile` quotes the basename in its `reconciled` and `would reconcile`
  lines. JSON output and the GitHub annotation `file=` value are deliberately unchanged, because
  they are machine channels with their own encoders. Warning message prefixes are unchanged, so
  `PYTHONWARNINGS` filters targeting them keep working.

  This closes the document-path vector at the load boundary. Control characters written as
  escapes inside typed frontmatter values reach human output by a separate route that is not
  closed here. The reconcile transaction and recovery sinks interpolated their paths the same
  way; the next entry closes those.

- A document filename can no longer forge or corrupt reconcile's transaction, rollback, and
  recovery output either. A reconcile stage is named after the destination it is written beside,
  so a hostile filename propagates into the transaction's own artifact paths, and recovery then
  prints those paths back to the user; that report is what someone reads while deciding how to
  repair a half-applied transaction, which is why it matters more than the equivalent leak at the
  load boundary. Every journal, destination, staged-artifact, and recorded journal-entry path in
  `reconcile_transaction.py`, the shared durable-write helper's orphaned-stage remediation note,
  and reconcile's `--recover` reporting now use the same quoted spelling the previous entry
  introduced. `path_utils.safe_resolve`'s containment error moves with them, because the
  transaction layer embeds it verbatim when a journal records an escaping path.

  Visible output changes shape accordingly: the journal named by every `--recover` summary line,
  each unresolved destination, each orphaned artifact, and the path component of each
  orphan-scan failure are quoted. The shared cleanup note is quoted for every caller of the
  durable-write helper, including `doc-lattice init` and the load cache, whose paths are not
  document paths. The `--format json` recovery payload is byte-identical, wording and array
  ordering included: it is a machine channel, so an orphan-scan failure is now retained as
  structured data and spelled by whichever encoder the run selected rather than being fused into
  prose when it is recorded. No second display spelling is introduced and AD-34 is unchanged.

  The static guard that enforces this no longer exempts `reconcile_transaction.py`,
  `persistence.py`, `path_utils.py`, or reconcile's recovery reporting. What remains exempt is
  per-expression and machine-only: the journal serializer, the staged-artifact filenames, and the
  recovery payload's own JSON spelling.

- A document or config file that fails to parse can no longer put terminal control bytes on your
  screen through the YAML parser's own message. That message is built by `ruamel` rather than by
  this project, and it echoes what it choked on: a duplicate key is reported by quoting the key
  and both of its values, so a block spelling `k: "v\u001b[31mA"` and `k: "v\u001b[31mB"` put raw
  ESC bytes on stderr. It is the vector nothing else in this release reaches: the frontmatter value
  rule under **Changed** never runs, because the load aborts before any value is validated, and the
  quoted path spelling in the two entries above governs filenames rather than a message this
  project did not build. All four sites that interpolate it are covered: the frontmatter and config
  parse failures and reconcile's post-edit reparse gate and load site.

  The message is now spelled with the same `repr` every path and rejected key already uses, applied
  to the whole message because its third-party origin leaves no part of it trustworthy: once
  `ruamel` has joined its pieces into one string, a line break it wrote and one decoded out of your
  document are the same character, and preserving them would let a document forge a diagnostic
  line. So an ordinary syntax error now reads as one quoted line. Each command keeps its own
  header, so what failed and which file it was still reads as before, and the parser's `line: N,
  column: M` coordinates survive; the caret under the offending column does not, because it points
  at nothing once the message is flattened. `--format json` and the GitHub annotation encoder are
  unaffected: a document that fails to load fails before either is selected.
  [AD-37](ARCHITECTURE.md#ad-37-a-yaml-load-failures-message-is-spelled-whole-at-the-sink-that-reports-it)
  records the decision, the two rejected alternatives, and the static guard that keeps every future
  handler of the family on the same spelling. With this, every repo-controlled string that can
  corrupt what doc-lattice prints is closed.

- Every `git` invocation now resolves to an absolute path outside the directory being operated on.
  A `PATH` lookup returning a relative result is refused, which covers every relative entry from
  `.` to any ancestor, and an absolute result resolving inside the invocation directory or the
  process's own working directory is refused too. Earlier versions ran a bare `git`, which on
  Windows searches the invoking process's current directory ahead of `PATH`, so a repository
  carrying its own `git.exe` could have been executed by the managed `ci` commands and
  `init --github`, all of which this release removes.
  `init` gained the default branch probe in this release, which would have widened that to the
  ordinary command run in freshly cloned repositories, so it is fixed before it ships. On POSIX
  the same applies to a relative `PATH` entry. When no trusted `git` is found, the `init` probe
  falls back to `main`, unchanged from any other
  discovery failure. SECURITY.md's scope states the promise this keeps.

## [4.1.0] - 2026-08-14

### Migration

This release changed generated output in both setups, so an adopter that stays on the 4.0.0
artifacts keeps running the older workflow shape. Recorded retroactively: it was added to `main`
after v4.1.0 was published, so it is absent from the immutable v4.1.0 tag and from that release's
GitHub Release notes, which are never rewritten.

- Every install: reprint the pre-commit block with
  `uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice init` and replace your checked-in
  block with it. Do not bump only the version in the two `entry:` lines.
- Ordinary installs: replace `.github/workflows/doc-lattice.yml` with the workflow the same
  `init` run prints. It gained SHA-pinned actions, `permissions: contents: read`,
  `persist-credentials: false`, and `enable-cache: false`, none of which a pin bump introduces.
- Managed installs: run `uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice ci refresh
  --repository OWNER/REPO`, then the same command with `--apply`, and commit the diff. Do not
  hand-edit the four managed artifacts, and do not re-run `init --github`.

### Changed

- `init` now closes with baseline guidance in both the ordinary and the managed branch, telling a
  first-time adopter to run `doc-lattice reconcile --all` once after annotating documents and
  before enabling the gates. The line is scoped to an initial adoption with no established
  baseline, since `init` is rerunnable and `reconcile --all` would otherwise acknowledge drift an
  established adopter has not reviewed, and it does not promise green CI, since BROKEN edges are
  skipped and remain findings. README.md owns the rule; MANAGED_CI.md sequences the command and
  links there.
- The GitHub Actions workflow snippet `init` prints now pins `actions/checkout` and
  `astral-sh/setup-uv` by commit SHA with a trailing version comment, matching the managed
  workflows, instead of using the floating `@v4` and `@v6` tags. The composed `uses:` fragment
  for each action now has a single owner in `constants.py` that both the snippet and the managed
  renderer read, so bumping a pin updates both.
- The printed snippet also adopts the managed workflows' least-privilege posture: the job
  declares `permissions: contents: read`, the checkout step sets `persist-credentials: false`, so
  the job token is not left in `.git/config` while the following step resolves and runs
  third-party packages, and the setup-uv step sets `enable-cache: false`, so no persistent
  cross-run cache another workflow on the repository can populate is restored into the gate job.
- `actions/checkout` moves from `v4.3.1` to `v4.4.0`, the pin this repository's own gates already
  ran, in both the printed snippet and the managed workflows. `tests/test_workflow_pinning.py`
  now holds the two together, so a shipped pin cannot silently fall behind the pin doc-lattice
  itself depends on.

### Fixed

- `--no-color` and `NO_COLOR` now suppress all terminal styling, not only color. Both levers
  previously left Rich's automatic bold highlighting (and any future explicit `[bold]` or
  `[link=...]` markup) in place on any shared-console print site with no local
  `highlight=False`, such as the `check --only` diagnostic. `_create_runtime` now also disables
  the console-wide highlighter and forces `color_system=None` when a lever is set, so `--no-color`
  and `NO_COLOR` produce byte-identical, escape-free output; this is a deliberate extension of the
  `NO_COLOR` baseline, documented in README.md and the `--no-color` option help.
- `reconcile` now preserves the input indentation of frontmatter lists when it updates `seen`, so
  two-space, column-zero, and mixed list styles no longer produce unrelated cosmetic diffs.
- `reconcile` writes a CRLF or lone-CR file back in its own line ending instead of converting the
  whole file to LF, so updating one `seen` no longer restyles every other line of a downstream
  document. A file that already mixes endings has none to preserve and is still written out in LF.
- `reconcile` reparses the rewritten frontmatter before staging it and refuses a rewrite that
  would not reload as the planned frontmatter, edges and every other key alike, so a bad source
  edit can no longer be published durably.
- `reconcile` now reads through ruamel's pure Python parser explicitly. Installing the optional
  `ruamel.yaml.clib` accelerator, which any other package in an environment may pull in, otherwise
  switched the loader to a C parser reporting different source marks, which moved where reconcile
  measured its edits.
- `ci audit` now reads workflows through ruamel's pure Python parser explicitly. With the same
  optional `ruamel.yaml.clib` accelerator installed, the C parser read neither the resolver the
  audit edits nor the version and anchor state it inspects, so a workflow holding an unquoted
  timestamp was refused as an unsupported scalar, an unsupported `%YAML` directive was accepted
  rather than rejected, and a duplicate anchor was reported as generic malformed YAML instead of
  being named.
- A replacement or relocated `seen` value holding a character YAML admits only as an escape, such
  as a C1 control or a next-line, is now written back as one instead of raw, so a document that
  loads cleanly can no longer be refused by the reparse gate on every run.
- `.doc-lattice.yml` holding a tagged scalar its type cannot accept, such as `!!int oops`, now
  reports a config error naming the file instead of exiting with a traceback, matching how the
  same typo has been reported in document frontmatter.
- Reconcile's malformed-frontmatter refusals now name the document they came from, so a failed
  `reconcile --all` says which file to fix.
- `reconcile` quotes a replacement hash that would not reload as a string, keeps a `derives_from`
  list reached through a YAML alias or a merge key editable in the plain `<<`, the explicitly
  tagged, and the aliased spelling of that key alike, along with any entry inheriting `seen`
  through one, reads a merge key as the instruction it is rather than as a member of its own, so
  an entry whose merge key happens to spell `seen` has a hash appended instead of being refused,
  updates a `seen` member whose key is written as an alias instead of
  appending a second one, preserves the type of a relocated non-string `seen` anchor along with
  any explicit tag it carries, and no longer rewrites an alias bound to a later definition of a
  reused anchor name or to an entry the same run already updated.
- `reconcile` indents a newly inserted `seen` to the column the entry's mapping was opened at and
  appends it after a multi-line flow value, so an entry written with an explicit key, an anchor or
  a tag on its list line, or a flow tail stays parseable. Replacing an empty `seen` targets the
  value indicator itself, so a colon inside a comment on the key no longer misplaces the hash,
  and an explicit `seen` key written without any `:` gets one written for it instead of borrowing
  the next pair's. An anchor or a tag written on the line below an empty `seen` goes with the
  value it belonged to, rather than being left behind as source of a value that is no longer
  there, and a comment written between the two stays.
- `reconcile` reads every document through its own loader, so one file's `%YAML` directive can no
  longer change how a later file's hash is quoted, and it quotes a replacement the document's own
  YAML version would otherwise retype. A tagged `seen` scalar is rewritten with its tag dropped,
  matching how an anchored one was already handled.
- `reconcile` puts back the document around the frontmatter it edits, so a file saved with a
  byte-order mark keeps it and a `---` fence written with surrounding space, or with no newline
  after it, is no longer rewritten to this engine's own spelling.
- `reconcile` keeps an entry written as a YAML ordered map (`!!omap`) editable. Such an entry
  loads as a mapping and validates as an edge, but its source is a sequence of one-pair mappings,
  so the targeted pair is edited inside its own mapping and a missing `seen` is appended as an
  item rather than as a key. A whole frontmatter document written as an ordered map is
  reconcilable for the same reason: its `derives_from` is found among the items its source
  spells rather than looked up in a mapping that is not written there.
- `reconcile` keeps a comment written inside the source a replaced `seen` occupies. A block
  scalar's source runs from its header through its contents, so `seen: | # note` used to lose the
  note, which is now rewritten onto the line the new hash is written on. An anchor or a tag is
  dropped when the value it carries is replaced, and a comment written between either one and
  that value, or between the two, used to go with it; each property is now removed on its own, up
  to a comment rather than past one, so the comment stays where its author put it.
- `reconcile` covers the remaining ways one `derives_from` entry can share a node with another: an
  alias to an entry the same run already updated is left alone when that entry is written as an
  ordered map, an ordered map item spelled as an alias takes the pair it stands for rather than
  the shared definition it names being edited, and an entry inheriting `seen` from another through
  a `<<` merge key no longer makes the reparse gate refuse an otherwise correct rewrite. That last
  case holds when the entry inherited from is written as an ordered map, which a merge key reads
  through as the one-pair mappings its source spells rather than as the entry itself.
- Frontmatter tagging a scalar with a type its value cannot build, such as `!!int oops`, is
  reported as unreadable frontmatter naming the file. It previously escaped `check` and `build` as
  an internal `ValueError`, since only `reconcile` recognized that failure.
- `reconcile` refuses a self-referential frontmatter document as a clean error rather than a
  `RecursionError` traceback, quotes a replacement hash a YAML constructor rejects instead of
  letting that error escape, and appends a missing `seen` after an entry key whose value is empty,
  including the explicit `? key` and `:` spelling of that value, rather than splicing it into the
  next entry or between the key and its own value indicator.

## [4.0.0] - 2026-08-10

### Added

- PyPI Trove classifiers and keywords in the package metadata.
- `docs_roots` entries may name individual `.md` files, which are tracked as documents.
- `check --format json` carries a `summary` object of per-state counts alongside `edges`, so a
  consumer can answer "is the tree clean?" without folding every edge record. The addition is
  purely additive: the `edges` key, its ordering, and each edge record are unchanged. The
  format is documented in [README.md](README.md).

### Changed

- **BREAKING:** every human-format `check` run now ends with a verdict line counting the
  classified edges per state, so truncated output can no longer read as clean when it is not.
  Previously a run with nothing to list, such as a clean lattice or `--only OK` on a drifting
  one, printed nothing at all. Migration: a pipeline that gated on empty stdout, or that
  grepped human output for a state name, now matches on every run because the verdict names
  every state including the zero-count ones. Gate on the exit code (1 on drift, unchanged) or
  on `--format json` instead. The line's format is documented in [README.md](README.md).
- `check --only STATE` still narrows only the records that are displayed. The summary counts and
  the exit code both reflect every classified edge. `--format github` output is unchanged and
  carries no summary.
- The managed GitHub and Linear setup guide moved from the README to
  [MANAGED_CI.md](MANAGED_CI.md), and the version-sync guard now checks install pins in both
  documents.
- The reconcile selector, dry-run, durability, and recovery deep dive moved from the README to
  [RECONCILE.md](RECONCILE.md).
- **BREAKING:** an existing `docs_roots` entry that is neither a directory nor a regular `.md`
  file now fails config load with a config error and exit 2. Previously such an entry was
  silently ignored and contributed no documents. Migration: remove the entry, or point it at a
  directory or `.md` file.

### Removed

- Internal: the `doc_lattice.version_check` module. It was maintainer tooling that no CLI command
  or production module imported, yet it shipped as importable package API. Its two functions moved
  into the only scripts that call them: `check_version_consistency` into
  `scripts/check_version_sync.py` and `changelog_section` into `scripts/extract_release_notes.py`.
  Behavior, messages, and the pre-commit and CI gates are unchanged.
- Internal: the `doc_lattice.datetime_utils` module and its `utc_now` helper, which nothing called.
  The convention that `datetime.now()` and `datetime.utcnow()` may appear only in
  `datetime_utils.py` still stands and is still enforced by `tests/test_conventions.py`.

### Fixed

- `impact` no longer hard-wraps its human output at the terminal width. It previously inserted
  real newlines mid-path, breaking the rendered document path into unusable fragments, and it did
  so even when stdout was a pipe or a file rather than a terminal. That made
  `doc-lattice impact X | xargs ...` and similar line-oriented pipelines fail, and it worsened
  with path depth. Each affected node is now one record on one line at any width, so the path
  stays intact and copyable. `--format json` output is unchanged, and no width policy was added
  to the shared CLI console.

## [3.0.0] - 2026-08-05

### Added

- Explicit `init --github --repository OWNER/REPO` generation for create-only managed offline,
  trusted Linear, and human-run GitHub bootstrap artifacts.
- A reviewed two-stage GitHub environment bootstrap that verifies an exact `main` deployment
  policy before a maintainer separately sets the Linear credential.
- Read-only `ci audit` policy checks and interactive `ci refresh` support for managed upgrades,
  repository renames, and transfers.

### Removed

- **BREAKING (3.0):** Removed the CI shell scanner and its verification harness. The scanner moved
  verbatim to [doc-lattice-shell-lint](https://github.com/Guardantix/doc-lattice-shell-lint), which
  is released independently on PyPI. `ci audit` now performs no shell analysis: the
  `PR_LINEAR_INVOCATION` and `PR_MUTATING_RECONCILE` finding codes are retired, as is the exit-2
  unsupported-shell-semantics audit outcome. Migration: run `uvx doc-lattice-shell-lint` as its own
  explicit workflow step to keep that lint. This is a major version because an audit reporting
  fewer finding classes is a gate becoming more permissive, which a consumer could otherwise come
  to depend on silently. See
  [AD-25](ARCHITECTURE.md#ad-25-the-ci-shell-scanner-is-extracted-to-doc-lattice-shell-lint).
- Internal: removed the scanner's contributor gates with it, including the guard inventory checker,
  the frozen-corpus differential, the witness sweep, the differential fuzzer, their fixtures, and
  the CI jobs that ran them. They are maintained in doc-lattice-shell-lint.

### Security

- The generated Linear workflow uses a dedicated environment-only credential, maps it only on the
  final trusted step, and never exposes it to the generated pull-request workflow.
- Existing installations must migrate the repository-scoped `LINEAR_API_KEY` to the protected
  environment and remove unmarked canonical and hand-written Linear workflows before
  `init --github`. See
  [Managed GitHub and Linear setup](MANAGED_CI.md) for the full
  migration and secret-cleanup procedure.

## [2.0.0] - 2026-07-14

### Added

- `reconcile` now commits multi-file updates as one conflict-detecting durable transaction, recovers
  interrupted work automatically before real runs, and provides recovery-only `--recover` mode;
  `init` prints the corresponding transaction-artifact ignore patterns.
- Empty ATX headings such as `#` and `##   ` are now recognized and receive the same empty GitHub
  slug that `github-slugger` generates.
- Document symlinks whose targets remain inside the project root are now supported. Aliases to the
  same resolved document are loaded once, while external targets are skipped with a warning and
  reconcile revalidates containment before writing.

### Changed

- Documentation ownership is consolidated: README.md owns the user contract, ARCHITECTURE.md owns
  durable decisions and module boundaries, CLAUDE.md routes contributors and agents, CHANGELOG.md
  owns history and migrations, and roadmap.md contains future direction only.
- Internal: `doc_lattice.cli` is now a package with a frozen per-invocation runtime, focused
  command adapters, centralized output and error handling, and command-mirrored CLI tests. Runtime
  behavior is unchanged.
- Internal: Markdown heading recognition and GitHub-compatible slug generation now pass through a
  documented adapter pinned to `markdown-it-py==4.2.0` and `github-slugger@2.0.0`. The slug-strip
  and JavaScript Unicode 17 lowercase and contextual-casing compatibility data is generated from
  upstream. Section spans and the cache schema are unchanged, but existing version-2 load caches
  are rebuilt so parser-derived anchors and spans use the new adapter. Rare headings whose casing
  data was absent from Python Unicode 15.1 now receive the upstream-compatible section id.
- Internal: the load cache module is now a phase-separated `doc_lattice/cache/` package
  (schema/codec, store, lookup, run state). No user-facing behavior change; the cache file
  format is unchanged.

### Fixed

- Markdown files that open YAML frontmatter without a closing `---` now fail with a
  source-naming tool error (exit 2) across cached and uncached loads instead of being silently
  omitted from the lattice. Existing version-1 load caches are rebuilt.

### Removed

- **BREAKING (2.0):** Removed the unsupported `binding_layers` configuration key. Migration:
  delete the key from 1.x configs; there is no replacement, and `lint`'s fixed authority ladder
  is unchanged. Strict configuration now rejects the key.
- **BREAKING (2.0):** Removed the silent `--json` alias from `check`, `lint`, `impact`,
  `reconcile`, and `linear`; `impact`, `reconcile`, and `linear` now accept `--format human|json`.
  Migration: replace `--json` with `--format json`. `--indent` now requires an effective
  `--format json`, and the former `--json`/`--format github` conflict rule is gone along with
  the alias.
- Internal: removed the singular `section_span` helper in favor of the existing `section_spans`
  API.
- Deleted completed design specs and implementation plans after recording their durable Linear,
  load-cache, and Markdown compatibility decisions in ARCHITECTURE.md. Also deleted the duplicate
  code-conventions guide and incomplete build log; their owners are CLAUDE.md and CHANGELOG.md.
  Version control retains the implementation history.

## [1.0.1] - 2026-07-13

### Fixed

- `graph` Mermaid output now assigns each node a collision-free identifier. Distinct node ids
  that sanitized to the same Mermaid-safe token previously merged into one graph node.
- Heading anchor parsing now recognizes `{#marker}` only as a trailing heading marker
  (optionally followed by an ATX closing `#` sequence). An anchor-like token in the middle of
  heading text is no longer mistaken for the heading's explicit anchor.
  **Behaviorally breaking, noted retroactively:** this was shipped as a fix but changes section
  identity. A heading whose anchor came from a nontrailing `{#marker}` now resolves to its
  generated slug instead, so any `derives_from` ref pointing at the old `file#marker` target
  resolves to nothing and `check` reports it as BROKEN. Migration: move the marker to the
  supported trailing position to keep the old anchor, or repoint the ref at the heading's
  generated slug. That slug is derived from the whole heading text with the marker still in it,
  so `## Alpha {#mid} beta` becomes `alpha-mid-beta`, not the `alpha-beta` the token's removal
  would suggest. Recorded retroactively: this note was added to `main` long after v1.0.1 was
  published, so it is absent from the immutable v1.0.1 tag and from that release's GitHub
  Release notes, which are never rewritten.

## [1.0.0] - 2026-07-12

### Added

- Publish release wheels and source distributions to PyPI through GitHub Actions Trusted
  Publishing, with no stored PyPI credential.

### Changed

- Generated pre-commit and CI gates install an exact `doc-lattice==1.0.0` PyPI requirement
  instead of cloning and building a tagged Git revision.
- Release retries distinguish the current tagged commit from an ordinary unversioned merge,
  making GitHub Release and PyPI publication safe to resume after a partial failure.
- Source distributions contain only package source, tests, license, README, build metadata, and
  Hatchling's required `.gitignore`.

## [0.9.0] - 2026-07-11

### Changed

- **BREAKING:** the project is renamed from game-lattice to doc-lattice. The engine was never
  game-specific; the name now matches its general purpose. In one release this renames the
  repository (https://github.com/Guardantix/doc-lattice, with GitHub redirects from the old
  URL), the distribution and package (`doc-lattice` / `doc_lattice`), the CLI executable
  (`doc-lattice`), the config file (only `.doc-lattice.yml` is recognized; no fallback), and
  the opt-in load-cache location (`<cache_home>/doc-lattice/`; old cache directories are
  orphaned and safe to delete). Doc sets themselves need no edits; lattice frontmatter
  (`id`, `derives_from`, `authority`, `seen`) is unchanged.

### Migration (v0.8.x to v0.9.0)

Nothing breaks until you bump your pin: checked-in gates pin a tag
(`uvx --from git+.../game-lattice@v0.8.0 game-lattice ...`) and GitHub's rename redirect keeps
that resolving indefinitely. Upgrading the pin to v0.9.0 requires, in one commit:

1. Rename `.game-lattice.yml` to `.doc-lattice.yml` (contents unchanged).
2. Regenerate the checked-in pre-commit hook and CI workflow (re-run `doc-lattice init`
   codegen, or by hand update the repo URL, the `@v0.9.0` pin, and the executable name
   `game-lattice` to `doc-lattice` in each invocation).
3. Any Python code importing `game_lattice` switches to `doc_lattice` (the package is not on
   PyPI and no import consumers are known; listed for completeness).

## [0.8.0] - 2026-07-10

### Added

- Opt-in incremental load cache: set `cache_key` in `.game-lattice.yml` to skip re-parsing
  unchanged docs across runs and git worktrees, with byte-identical output to an uncached run by
  default; `cache_trust_stat: true` adds a faster stat tier for read-only commands under the
  documented mtime caveat, and `reconcile` always verifies content (#28).
- `check`, `lint`, `impact`, and `linear` accept `--indent N` with JSON output (`--json`, or the
  equivalent `--format json` on `check` and `lint`), and the global `--no-color` option and the
  `NO_COLOR` environment variable both explicitly disable colored output, including the styling on
  help and usage-error text even when a terminal-forcing environment variable is set (#20).
- `check --format github` and `lint --format github` emit escaped GitHub Actions error
  annotations with repo-relative file paths so findings attach inline to the offending doc in
  the pull request diff, while preserving the existing gate exit codes; both commands also accept
  `--format human|json`, and `--json` remains the JSON alias (#18).

### Changed

- Moved `check`, `lint`, and `impact` JSON builders beside their result types and centralized their
  human console output in a dedicated report renderer, leaving the CLI as dispatch-only wiring
  (#29).
- Centralized CLI `ProjectError` handling behind the shared tool-error exit path (#30).
- Internal performance: `check` and `reconcile` memoize target-content hashes within each run,
  avoiding repeated section extraction and hashing for edges that share a target (#25).
- Reduced repeated load-path work by counting document lines once, reusing safe YAML loaders, and
  sharing newline normalization between section parsing and hashing (#27).
- Moved reconcile phase-1 rewrite planning into the pure reconcile module via an injected reader
  (#31).

## [0.7.0] - 2026-07-09

### Added

- `check --only STATE` (repeatable) filters human and JSON output by edge state; the exit code
  still reflects every edge (#19).
- `graph --format json` emits a machine-readable node and edge dump that matches the Mermaid and
  DOT edge collapsing (#21).
- `impact --depth N` bounds the reverse walk, and `impact --json` entries now carry a `depth`
  field (#22).
- `reconcile --dry-run` previews the plan without writing, and `reconcile --json` emits a
  machine-readable plan for both dry and real runs (#17).
- The Linear client retries transient HTTP 429 and 5xx failures with bounded backoff, honoring
  `Retry-After` up to a 30 second cap (#24).
- The version-sync guard now also checks README pinned install refs (`game-lattice@vX.Y.Z`)
  against `__version__` (#34).
- The release job now publishes a GitHub Release for each tag it cuts, with the body taken from
  the matching `## [X.Y.Z]` CHANGELOG section; `scripts/extract_release_notes.py` (pure core
  `version_check.changelog_section`) extracts it and fails the release if that section is missing
  or empty (#47).

### Changed

- `graph --format` now rejects unknown formats with exit 2 instead of silently rendering
  Mermaid (#21).
- Ancestor recording in the loader is a single stack pass instead of a quadratic scan (#26).
- CI runs the code-quality job on both Python 3.13 and 3.14 (#33).

### Removed

- Unused `datetime_utils` helpers (`local_now`, `parse_iso`, `format_iso`); `utc_now` remains the
  single sanctioned current-time entry point (#35).

## [0.6.0] - 2026-07-05

### Changed

- Lowered the minimum supported Python from 3.14 to 3.13 (`requires-python = ">=3.13"`). 3.13 was
  already the true floor: the engine's only version-gated dependency is `PurePath.full_match`
  (added in Python 3.13, used by `ignore_globs` matching), so no engine change was required. CI now
  runs the test suite on a 3.13 and 3.14 matrix, and `init`'s generated pre-commit and CI gates pin
  `--python 3.13` so an adopting repo provisions the declared minimum.

## [0.5.0] - 2026-07-01

### Added

- GitHub-native heading-slug fallback for anchor resolution: a `derives_from` section ref now
  resolves against a plain heading with no `{#slug}` marker, computing the same slug GitHub
  renders for that heading (ported verbatim from `github-slugger@2.0.0` for byte-parity). An
  explicit `{#marker}` still resolves and takes precedence, remaining the escape hatch for
  headings whose rendered text diverges from their source (inline links, images).

### Changed

- **Breaking:** section refs must now be namespaced `<file>#<anchor>`; a bare ref resolves only
  to a file id. A bare anchor ref that previously resolved against the flat anchor namespace
  (for example a plain `#accent` matching `art-direction#accent`) now reports `BROKEN` instead.
  Adopters relying on bare-anchor refs must repoint them to the `file#anchor` form.

## [0.4.0] - 2026-06-29

### Added

- Version-consistency guard (`scripts/check_version_sync.py`, pure core `version_check.py`) wired into pre-commit and CI: `__version__`, `pyproject.toml`, and the top `CHANGELOG.md` entry must agree.
- Merge-triggered `release` CI job that creates and verifies the lightweight `vX.Y.Z` tag, smoke-testing `check`, `lint`, and `init` against the pinned ref.

## [0.3.0] - 2026-06-28

### Added

- `lint` command: validates the authority ladder over `derives_from` edges and reports edges it cannot rank.
- Generated pre-commit and CI now run both `game-lattice check` and `game-lattice lint`.

## [0.2.0] - 2026-06-28

### Added

- `init` command: scaffolds `.game-lattice.yml` and prints pre-commit and CI codegen for an adopting repo.
- `RELEASING.md`: release checklist that makes the version tag an atomic part of cutting a release.
- `linear` command: resolve referenced tickets to live Linear status over GraphQL and report tickets shipped against a spec that has since drifted; supports `--from`, `--exit-code`, and `--warn-exit`.

## [0.1.0] - 2026-06-27

### Added

- Initial project scaffolding.
- Local traceability engine that parses lattice frontmatter and anchored `{#anchor}` sections into an id-indexed edge graph, with four offline commands:
  - `check`: classify every `derives_from` edge as OK, STALE, UNRECONCILED, or BROKEN against the upstream content hash; exit 1 on drift, 2 on a tool error.
  - `impact`: list every downstream doc and ticket affected by a change to an id, walking the reverse adjacency with ancestor and enclosing-file expansion.
  - `reconcile`: rewrite the `seen` hash for the selected edges (a downstream id, `--ref`, or `--all`) through round-trip YAML, atomically and only after a fresh re-read so a concurrent edit is preserved.
  - `graph`: emit the edge graph as Mermaid or DOT (`--format`), marking stale edges.
