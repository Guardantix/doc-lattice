# Design: the `links` command (GTX-477)

Status: approved design, 2026-09-02. Staging only. Under AD-14 this file is not a contract:
README.md owns the user-facing behavior it describes, ARCHITECTURE.md owns the decisions, and
CHANGELOG.md owns the migration. It is mined into those owners by the implementation and deleted
at release.

Issue: [GTX-477](https://linear.app/guardantix/issue/GTX-477). Two non-binding Codex reviews on
the issue were folded into this design, as were Rick's section-by-section amendments.

## 1. Goal

`scripts/check_doc_links.py` is a complete Markdown link gate that reaches nobody: it is excluded
from the wheel, hardcodes this repository's root, and has no command. This design moves it into
the package as a consumer-facing `doc-lattice links` command over a configured source set, makes
both generated adopter surfaces run it, and makes this repository's own gate run through the
shipped command so there is exactly one implementation.

Out of scope: heading-inventory performance, any change to which sections the engine addresses,
`json` output, and self-adoption of the lattice itself (GTX-168). Both gaps the script already
declares are preserved, not fixed: a raw HTML anchor destination is reported rather than
resolved, and a heading whose text is itself an inline link slugs from raw source on both sides.

## 2. Configuration and selection

### 2.1 The `link_sources` key

`Config` gains `link_sources: list[str] = Field(default_factory=list)`. Omitted and empty are the
same case; an explicit YAML null is a strict-schema error, preserving the existing strict-config
contract. There is no default, no derivation from `docs_roots`, and `ignore_globs` does not
apply.

Reasons, recorded in AD-45:

- `docs_roots` is the tracked lattice corpus. A consumer may gate links in files it does not
  track (colinear's root carries several), so reusing it conflates two corpora and produces an
  honest-looking green over the wrong files.
- `ignore_globs` is anchored to each docs root. Applying it here would re-couple the corpora
  silently. A selector already says exactly what it wants.
- A fallback would let the mandatory hook and workflow pass over zero files. The key fails
  closed instead (2.4).

### 2.2 Selector grammar

Selectors are project-relative, POSIX, and platform-independent:

- `/` is the only separator. A backslash anywhere in a selector is a config error.
- `*`, `?`, and bracket classes match within one segment, case-sensitively by code point, and
  never match `/`. Bracket classes carry `fnmatch` semantics: ranges such as `[a-z]` and
  negation as `[!seq]` are supported, and a `]` first in a class is literal. Each segment is
  matched with `fnmatch.fnmatchcase`, which is what makes the grammar executable without a
  parser of its own.
- A `[` with no closing `]` is a config error, not a literal as `fnmatch` would treat it.
- `**` is accepted only as a whole segment and matches zero or more directories.
- Empty segments, `.` and `..` segments, an absolute or drive prefix, and a trailing slash are
  config errors naming the entry.

Validation is lexical and runs at config load. Nothing is resolved at load, because an escaping
symlink must survive selection and become a finding (2.4), where `_resolve_roots` would reject it
as a config error.

### 2.3 Expansion

`select_link_sources(project_root, selectors)` in `link_check.py` expands selectors with the
module's own no-follow walk over `os.scandir`, not `Path.glob`, whose ordering is unspecified,
whose case behavior varies by platform, whose `recurse_symlinks=False` governs only `**`, and
which suppresses scanning `OSError`s.

- A symlinked directory is never entered, whether reached by `**` or named by a fixed segment.
  Following one would let a link to `/` turn `**` into a filesystem walk.
- A symlinked file is matched lexically and containment-checked afterward.
- A directory the walk cannot scan is a config error carrying the directory and the OS error
  text. A gate that cannot see its inputs must not pass.
- Hidden entries receive no special treatment.

Which filesystem objects can become a source:

- Ordinary directories are traversal nodes and never satisfy a selector by themselves. A selector
  whose last segment matches only directories has matched nothing.
- Regular files and leaf symlinks are candidate matches.
- A contained symlink must ultimately identify a regular file. A dangling symlink, or one whose
  target is a directory or a special file, is `UnreadableDocError`, exit 2.
- A special file (FIFO, socket, device) matched directly is `UnreadableDocError`, exit 2. The
  classification is made by `stat` without opening, because opening a special file can block
  indefinitely.

Ordering and deduplication: union the lexical matches across all selectors, sort by
project-relative POSIX string, then walk that order checking containment. Every escaping spelling
is retained so each bad configured source is reported. Contained paths are deduplicated by
resolved target with the first in sorted order kept. YAML order, overlapping selectors, and
filesystem order therefore cannot change output.

### 2.4 Fail-closed rules

- `link_sources` empty (including a zero-config run): exit 2, a config error naming the key and
  the config file, or stating that no config file was found and `links` requires `link_sources`.
- A selector that matches no lexical path: exit 2, a config error naming that selector. This is
  per selector, not per union, so `["ARCHITECTURE.md", "docs/**/*.md"]` cannot stay green after
  `docs/` disappears. An escaping match counts as a match; it becomes a finding.
- A matched source that resolves outside the project root: an exit-1 finding, reported before
  any read.

## 3. The engine

### 3.1 Module and boundary

`src/doc_lattice/link_check.py` holds the moved checker. It is a read-only filesystem boundary,
named in AD-2 alongside `config`, `discovery`, and `orchestrate`. It uses no `Any` or `cast`, so
the AD-3 allowlist is unchanged.

Public surface:

- `select_link_sources(project_root: Path, selectors: Sequence[str]) -> list[Path]` (2.3).
- `check_links(project_root: Path, sources: Sequence[Path]) -> list[LinkFinding]`.
- `LinkFinding`, frozen with slots: `path: str` (raw, unescaped project-relative POSIX string of
  the source), `line: int | None` (`None` for document-level findings such as an escaping or
  unparseable source), `message: str`.

`check_links` upholds its own contract independently of its usual caller: it sorts a copy of
`sources` without mutating the input and rechecks containment immediately before each read, as
the script does today.

### 3.2 Behavior carried over unchanged

Link extraction, raw HTML anchor collection, destination splitting, the lexical containment pass,
the `?plain=1` view rule, the Markdown target suffix set, and every message text move as they
are. Only the `path[:line]: ` envelope moves to the renderer; messages stay display-safe prose,
already neutralizing embedded paths, fragments, and destinations through
`format_path_for_display`.

Findings are ordered by source path, then line. Same-line order is exactly the script's: link
findings are collected first, then raw-anchor findings, and the list is stable-sorted by line, so
a link and an anchor on one line keep that relative order. Nothing is sorted by message.

### 3.3 Heading inventory

The target inventory is `full_heading_inventory(text)` with each record's `github_id` collected
into a frozenset, memoized per target path for the run. The script's private heading walker and
its second `github_ids_for_texts` pass are deleted. A second parse per target remains; performance
is out of scope. The stale reference to `scripts/check_doc_links.py` in the
`github_ids_for_texts` docstring is updated to name `link_check`.

### 3.4 Failure classes

- Content failures (undecodable bytes, a parser-rejected character reference): an exit-1 finding
  for that document, and the run continues. A target that cannot be decoded is a finding on the
  link.
- Operational filesystem errors (`OSError`) during resolve, stat, or open of a source or a link
  target: `UnreadableDocError`, exit 2. The gate could not inspect its input. A link target that
  simply does not exist remains an exit-1 finding, and a scan failure during selection remains a
  config error (2.3).
- `RuntimeError` from `full_heading_inventory`: not caught. It signals a parser invariant
  failure, not bad content.

## 4. The CLI adapter

### 4.1 Registration and options

`cli/commands/links.py` exports `register_links`, registered eighth in the application factory.
Options: `--config` and `--format human|github` through a dedicated `LinkFormatOpt` whose help
says `human or github`. The format domain is a `Literal` plus derived frozenset in
`constants.py`. There is no `--indent` because there is no `json`.

### 4.2 Flow

1. Load the project through the runtime. `ProjectConfig` gains `config_path: Path | None` so the
   empty-key diagnostic can name the file it read.
2. Empty `link_sources`: config error (2.4).
3. `select_link_sources`; its config errors propagate.
4. `check_links`.
5. Render; exit 1 on any finding, 0 clean.

Exit 2 arrives through the shared `exit_on_project_error` handler, and 141 through the existing
pipe policy.

### 4.3 Human output

Human findings go to stderr, preserving the script's contract. A new exact stderr writer on the
runtime, the analogue of `write_stdout`, writes bytes literally with no Rich markup, so a
markup-shaped filename cannot become styling. It follows the stderr half of the AD-40 pipe
policy: a departed stderr reader neutralizes the stream and the semantic exit code survives. Exit
141 applies only to truncated stdout results.

Line shape, identical to the script: `'path': message` for document-level findings and
`'path':N: message` otherwise, with the path through `format_path_for_display`. Nothing is
printed on success.

### 4.4 GitHub output

Annotations go to stdout through the shared writer, one per finding, severity `error`, title
`doc-lattice links`. `Annotation` gains `line: int | None = None` declared after `severity`, so
the existing positional `severity` call sites keep working, and `github_annotation` emits
`line=N` only when present, so existing callers are byte-identical. A document-level finding
omits the line; GitHub then attaches it at line 1, which is the closest representation workflow
commands allow.
The renderer rejoins `LinkFinding.path` to the project root because the writer takes a `Path`.
The writer's warning about paths GitHub will not attach applies unchanged.

## 5. Generated adopter surfaces and migration

### 5.1 Selector derivation for `init`

A pure scaffold helper turns a project-relative root path and its classification into a
selector: it strips a leading `./` and trailing slashes, normalizes interior `//` and `/./`,
encodes `*` as `[*]`, `?` as `[?]`, and `[` as `[[]` so a literal never becomes a pattern, then
emits the literal itself for a file root and `root/**/*.md` for a directory root or a nonexistent
root. Root `.` generates `**/*.md`. The nonexistent case takes the directory form because the
default root is created after `init`. A root the grammar cannot express, such as one containing
a backslash, is an `init` validation error naming it.

The `init` adapter supplies the path the helper sees. For an existing root it is the resolved,
contained project-relative path, not the literal flag value, because the checker never enters a
symlinked directory (2.3) and a selector written over a symlinked root would fail on every run. A
root that resolves outside the project is an `init` validation error. For a nonexistent root the
lexical path is used. The adapter classifies each root by stat and passes finished selectors to
`render_config`; the scaffold module stays filesystem-free.

### 5.2 `render_config`

Emits `link_sources` after `docs_roots`. The no-flag output carries `docs/**/*.md`, and the
README block that mirrors it byte-for-byte changes with it. README states that the default
scaffold fails closed until that selector matches at least one source.

### 5.3 `render_precommit` and `render_ci`

The pre-commit block gains a third hook after `lint`: id `doc-lattice-links`, the same `uvx`
invocation form, `language: system`, `always_run: true`, `pass_filenames: false`, no `files:`
key, with a comment giving the cross-document reason. The workflow gains a third invocation,
`links --format github`, with its own `rc_links` joined into the final conjunction. `check` and
`lint` keep their plain invocations; widening them is outside this issue.

### 5.4 Migration guard

`compute_surfaces` gains four config surfaces over the pure helper from 5.1: default, file root,
metacharacter root, and multiple roots. The committed 7.0.0 baseline is left untouched. The guard
compares the union of keys, so a surface absent from the baseline registers as a diff without a
crash. The `### Migration` subsection under `## [Unreleased]` authorizes the diff mid-cycle; the
release commit regenerates the baseline. The issue's acceptance bullet says the opposite and is
corrected in Linear at handoff.

### 5.5 CHANGELOG

Under `## [Unreleased]`: `### Added` for the command and key, `### Changed` for the scaffold and
guard enrollment, and `### Migration` naming adopter steps by install kind in this order: upgrade
the installation or pins first, because the strict parser must recognize the new key; add
`link_sources`; then enable the hook and workflow. An adopter who copies the new blocks first
gets exit 2 on every commit. The eventual release is additive for adopters who copy nothing, so a
minor version; that is the release procedure's call.

## 6. Dogfooding and repository wiring

- `.doc-lattice.yml` at the repository root: `lattice_format: 2`, `docs_roots: []`,
  `link_sources: ["*.md"]`. This reproduces the script's root-only selection and is the minimum
  GTX-168 needs, nothing more.
- Pre-commit hook `check-doc-links` becomes `doc-lattice-links`, entry
  `uv run --locked --group dev doc-lattice links`, keeping `always_run` and
  `pass_filenames: false`.
- CI code-quality step becomes `uv run --no-sync doc-lattice links --format github`.
- `scripts/check_doc_links.py` is deleted.
- `check_version_sync.py` keeps its own root-Markdown selection. Its docstring drops the
  equivalence claim and states its own reason: it reads pins only and stays free of the Markdown
  parser. The equivalence test is deleted.

## 7. Tests

- `tests/test_check_doc_links.py` becomes `tests/test_link_check.py`, importing from the
  package. Engine tests pass explicit sources to `check_links`. Selection tests are rewritten
  against `select_link_sources` for the grammar, no-follow, per-selector failure, ordering, and
  dedup rules. This module leaves both sdist exclusion lists.
- New repository-only `tests/test_link_gate_wiring.py`, enrolled in both lists: asserts the
  exact hook and CI invocations (`--format github` only in CI) and runs `links` through the
  real CLI from the repository root asserting exit 0 and empty stdout and stderr.
- `tests/cli/test_links.py`: the consumer-shaped fixture whose sources are not root Markdown;
  the failing witness with one dead fragment and one dead path asserting exit 1 and both
  messages on stderr; the markup-shaped filename and control-byte cases; zero-config and
  empty-key exit 2 messages; per-selector exit 2; unscannable directory exit 2; `--format github`
  with and without a line; GitHub output with a departed stdout reader exits 141; a human
  finding with a departed stderr reader retains exit 1.
- Named witnesses for the engine: source and target open `OSError` become
  `UnreadableDocError`; decode and parse failures remain findings and scanning continues;
  inventory `RuntimeError` propagates; `check_links` sorts without mutating its input and
  rechecks containment; same-line findings keep discovery order.
- `tests/test_scaffold.py` and the init tests: the selector helper (normalization, root `.`,
  escaping, rejected drive and backslash spellings), the new hook and step, the four config
  surfaces. `tests/test_config.py`: lexical validation and null rejection.
  `tests/test_check_migration_rule.py`: the enrolled surfaces. `tests/cli/test_github.py`: the
  optional line.
- `tests/cli/test_contract.py`: `links` joins the shared contract only where it applies. It has
  no `json` or `--indent` domain and loads no lattice, so it stays out of those matrices and the
  lattice-document annotation matrix. Its two-value format and pipe behavior get dedicated
  coverage.

## 8. Documents

README.md (owner of the user contract):

- `links` row in the Commands table and in the `--config` list.
- `### links` subsection placed after the shared CLI and output policy and before
  `### reconcile`: what it validates, the two declared gaps, the fail-closed rules, stderr for
  human findings and stdout for annotations, exit codes.
- `link_sources` in Configuration: grammar, no-follow and case-sensitivity rules, sorting and
  resolved-target deduplication, no `docs_roots` fallback, no `ignore_globs`, the fail-closed
  note about the default scaffold. The mirrored config block updated byte-for-byte.
- "Enabling the gates" describes three hooks; "two hooks" and "both hooks skipped" are corrected
  because `links` is `always_run`. The upgrade text's "two `entry:` lines" becomes three.
- Exit code 1's description adds link findings. The error-code table is unchanged.
- Project structure: add `link_check.py` and reword the `src/doc_lattice` description; the
  `scripts/` comment stays.

ARCHITECTURE.md: the System Overview pipeline gains the non-lattice `links` branch; AD-2 names
`link_check` as a read-only boundary and qualifies the "every command's logic is unit-tested with
no I/O" consequence; AD-45 records the decisions in 2.1, 2.3, 2.4, 3.3, 4.3, and the own-verb
decision (never fold into `check`, whose green is honest because it makes no link claim),
referencing AD-40 for the stderr pipe policy rather than duplicating it.

CLAUDE.md: the two bullets describing the gate are rewritten around the shipped command and
`link_check`; the contributor and Markdown-only verification lists say `uv run doc-lattice links`.
RELEASING.md step 4 lists the config block among the surfaces the guard covers. MANAGED_CI.md,
SECURITY.md, and ROADMAP.md need no edits. Historical CHANGELOG references stay untouched.
`PIN_MANIFEST` stays at its current README count; no pinned snippet is added.

## 9. Plan notes

- `tests/test_readme_contract.py` holds the mirrored generated config block and changes with it.
- `ProjectConfig.config_path` defaults to `None` so existing construction sites keep working.

## 10. Verification at handoff

pytest, Ruff check and format check, `ty`, typing boundaries, version sync, `doc-lattice links`,
migration rule, and `git diff --check`, with the coverage floor held.
