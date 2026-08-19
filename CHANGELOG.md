# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Migration

Six things to act on, plus two changes below that need no action in a default environment. The
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
message happens to have a newline. A usage error, which has no code, is unaffected and still
prints `error: <message>`, so the parenthetical now marks exactly the diagnostics that carry a
code to match on.

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
  diagnostics moved with it so one grammar covers both, and a usage error, which carries no code,
  still prints `error: <message>`. See **Migration** above for what a stderr scraper matches
  instead.

### Fixed

- An unknown frontmatter or config key that decodes to a control character is now spelled in the
  diagnostic that rejects it, instead of being echoed into your terminal raw. Safe YAML decodes a
  double-quoted `\u001b` in a mapping *key* exactly as it does in a value, and an unknown key is
  reported by naming the key, so a document spelling `"bad\u001b[31m": 1` put the ESC on stderr
  through the very message refusing it. Only the spelling changed: such a key was already an
  error, and a key carrying no control character still reads exactly as before, so an ordinary
  diagnostic still names `derives_from.0.ref` rather than gaining quotes. This is the key half of
  the value rule under **Changed** above; the two together do not close the whole of what a
  document can print, because a block that fails to parse at all is reported through the YAML
  parser's own message, which quotes the source it choked on. That path is still open and is
  tracked separately.

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
