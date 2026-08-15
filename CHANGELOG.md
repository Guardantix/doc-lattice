# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

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

### Fixed

- No command crashes any more on frontmatter whose `!!omap` repeats a key. A block such as
  `extra: !!omap` followed by two items spelling the same key escaped every YAML boundary as an
  uncaught `AssertionError` and printed a traceback, from `check`, `lint`, `impact`, `graph`,
  `linear`, and `reconcile` alike; it is now the ordinary `UNREADABLE_DOC` tool error naming the
  file, and exits 2. This is the same gap as the `!!bool` `KeyError` above and was missed the same
  way: ruamel's safe `construct_yaml_omap` enforces key uniqueness with a bare `assert` rather
  than raising a `YAMLError`, so `AssertionError` was the one member the shared load-error family
  did not name. `github_ci`'s workflow parser already caught it and is unaffected. No shape that
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
- The `ci` commands no longer crash on a workflow whose scalar carries a YAML tag its type
  rejects. A value such as `runs-on: !!bool nope` in a repository's own
  `.github/workflows/*.yml` escaped the workflow parser as an uncaught `KeyError` and printed a
  traceback; it is now reported as a malformed-YAML `ConfigError` naming the file, like every
  other unparseable workflow. The gap was tag-dependent and so easy to miss: the safe
  constructor raises whichever builtin the target type rejected the value with, and only the
  `ValueError` that `!!int` and `!!float` raise was handled, while the `KeyError` from `!!bool`
  was not. Duplicate-key and reused-anchor workflows keep their own more specific messages.
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

## [4.1.0] - 2026-08-14

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
