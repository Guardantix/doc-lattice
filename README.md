# doc-lattice

A deterministic, offline traceability engine for design and production documentation.

doc-lattice tracks the dependencies *between* your markdown docs. When a downstream
document derives from an upstream one (an integration guide built on an API design, an
engineering design built on a product brief), it records that link in frontmatter. When
the upstream changes, doc-lattice tells you exactly which downstream docs went stale, and a
CI gate keeps stale work from shipping silently.

It is pure tooling: no network (except the optional `linear` command), no secrets, no LLM,
no database. The dependency graph is derived from your docs on demand, never committed.

## The problem it solves

Docs drift apart. Someone changes the API contract, revises a requirement, or reverses an
architecture decision, and the documents downstream of that decision keep citing the old
version. Nothing breaks loudly; the docs just quietly disagree, and the drift surfaces as a
bug, a re-do, or an argument weeks later.

doc-lattice makes those dependencies explicit and *checkable*. Each downstream doc declares
what it derives from and records a hash of what it last saw. A change upstream that the
downstream hasn't acknowledged is **drift**, and `check` fails CI on it until a human
consciously reconciles the link.

## Where it fits

doc-lattice is domain-agnostic: it needs nothing but markdown files with frontmatter. Three
doc sets it fits naturally:

- **Software product docs.** Product briefs feed engineering designs, which feed runbooks
  and integration guides. When a requirement changes, `impact` lists every downstream doc
  that cited it, and `check` keeps the ones that never acknowledged the change from passing
  CI quietly.
- **Game studio design docs** (the project's original home). Art direction, economy tuning,
  and core-loop docs sit upstream of dozens of character, level, and systems specs. One
  retuned economy value can quietly invalidate a season of downstream work; drift detection
  surfaces that the day it happens instead of weeks later in a playtest.
- **Policy and compliance doc sets.** Procedures and checklists derive from a controls
  document or a policy. An unacknowledged upstream edit there is an audit finding waiting to
  happen; a CI gate turns it into a red build instead.

## How it works

You annotate docs with two things:

- **Stable ids.** Every tracked file declares an `id` in its frontmatter. Sections are addressed
  by their heading's GitHub slug by default; an explicit `{#anchor}` tag on the heading provides
  a stable id independent of heading text. Section ids are file-scoped, so the same anchor in
  two files does not collide with file ids or each other.
- **`derives_from` edges.** A downstream doc lists the upstream ids it depends on. Each edge
  carries a `seen` hash: a fingerprint of the upstream content at the moment the dependency
  was last reconciled.

From those annotations doc-lattice builds a **lattice**: an id-indexed graph of nodes
(your docs) and edges (the `derives_from` links). Every command reads from that one
structure. The `seen` hash is the load-bearing trick: comparing it against the upstream's
*current* content hash is what turns "these docs depend on each other" into "this dependency
is out of date."

### Drift states

`check` classifies every edge into one of four states:

| State | Meaning |
|-------|---------|
| **OK** | `seen` matches the upstream's current content. In sync. |
| **STALE** | The upstream changed since `seen` was locked. The downstream needs review. |
| **UNRECONCILED** | The edge has no `seen` yet. The dependency was declared but never acknowledged. |
| **BROKEN** | The ref points at an id that no longer exists. |

The content hash is `sha256` of a *canonicalized* copy of the text, truncated to 128 bits.
Canonicalization normalizes line endings, strips trailing whitespace per line, and trims
leading and trailing blank lines, so those cosmetic edits never trip drift. Internal
whitespace is preserved, so rewrapping a paragraph (which moves its line breaks) does count
as a change.

### Broken refs and tool errors

A ref that points at nothing is a normal, reportable lattice state: `check` calls it BROKEN
and exits 1. Invalid config or lattice frontmatter, unreadable or non-UTF-8 documents,
containment failures, and incoherent ids are tool errors that exit 2. An index is incoherent
when two files repeat a file id or two headings in one file resolve to the same file-scoped
anchor. Equal anchors in different files, and a file id equal to another file's anchor, remain
distinct `TargetId(file_id, anchor)` keys and do not collide.

A Markdown file without an opening `---` fence is valid untracked prose. Once a file opens YAML
frontmatter with `---`, it must include a closing `---` fence; otherwise every lattice-loading
command names the file, asks for the missing close, and exits 2 instead of omitting the node.

### The authority ladder

Separately from drift, `lint` enforces a structural rule: authority only flows downhill.
Docs can declare an `authority` of `binding`, `derived`, or `exploratory`. A `derives_from`
edge from a more-authoritative doc to a less-authoritative one is an **inversion** (a binding
spec should not derive from an exploratory sketch), and `lint` fails on it. `lint` is pure
structure, independent of drift, and exits 1 on a violation just like `check`.

## A worked example

Two docs. The upstream owns a decision; the downstream depends on it.

`docs/api-design.md`, the upstream:

```markdown
---
id: api-design
layer: design
authority: binding
---
# API Design

## Pagination {#pagination}
List endpoints use cursor pagination: pass the last item's cursor as `after`.
```

`docs/billing-integration-guide.md`, which derives from the pagination decision:

```markdown
---
id: billing-integration-guide
layer: technical
authority: derived
derives_from:
  - ref: api-design#pagination
    seen: 647cc64481bee8d8541ef7d1733b5204
tickets: [ENG-412]
---
# Billing Integration Guide

Invoice listings page through results with the cursor scheme the API design defines.
```

The ref `api-design#pagination` resolves file-scoped: it points at the section in the
`api-design` file whose heading carries the `{#pagination}` marker. Markers are optional; a
heading with no marker is addressed by its GitHub slug instead, and an explicit marker pins
the id so the ref survives a later rewording of the heading. The `seen` hash records the
pagination text the guide was last built against.

Now someone switches the API to page-number pagination. The `{#pagination}` section's
content hash no longer matches `seen`, so:

```console
$ doc-lattice check
STALE         billing-integration-guide -> api-design#pagination

$ doc-lattice impact api-design#pagination
billing-integration-guide  (/work/acme-api/docs/billing-integration-guide.md)  tickets: ENG-412
```

`check` exits 1, so CI is now red. A human reviews the guide against the new pagination
scheme, updates the body if needed, and then locks in the new hash:

```console
$ doc-lattice reconcile billing-integration-guide
reconciled billing-integration-guide.md: api-design#pagination

$ doc-lattice check
OK            billing-integration-guide -> api-design#pagination
```

That edit → `check` → review → `reconcile` loop is the whole workflow. `reconcile` is the
only command that writes to your docs, and it only ever rewrites the `seen` scalar.

## Quick start

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Install and run

Run the released CLI without installing it globally:

```bash
uvx doc-lattice --help
```

Or install it into an isolated tool environment:

```bash
uv tool install doc-lattice
doc-lattice --help
```

`pipx install doc-lattice` provides the same isolated installation. A conventional
`python -m pip install doc-lattice` is also supported when installing into an activated virtual
environment.

### Development

```bash
uv sync --group dev
uv run doc-lattice --help
```

### Test

```bash
uv run --group dev pytest          # full suite (enforces coverage >= 80%)
uv run --group dev ruff check src tests
uv run --group dev ty check src
```

## Commands

| Command | What it does | Exits non-zero |
|---------|--------------|----------------|
| `check [--only STATE ...] [--format human\|json\|github]` | Classify every `derives_from` edge as OK / STALE / UNRECONCILED / BROKEN. | 1 on drift, 2 on tool error |
| `lint [--format human\|json\|github]` | Validate the authority ladder (binding > derived > exploratory) over the edges. | 1 on a violation, 2 on tool error |
| `impact TOKEN [--depth N]` | List every downstream doc affected by a change to TOKEN; `--depth N` bounds the walk to N hops. | 2 on tool error |
| `reconcile [ID] [--ref REF] [--all] [--dry-run] [--recover]` | Durably set `seen` for selected edges as one transaction, preview read-only with `--dry-run`, or recover an interrupted transaction with `--recover`. | 2 on tool error, conflict, lock contention, or persistence/recovery failure |
| `graph [--format mermaid\|dot\|json]` | Emit the edge graph as Mermaid, DOT, or JSON. | 2 on tool error (including an unrecognized `--format`) |
| `linear [TARGET] [--from ID] [--exit-code] [--warn-exit]` | Report tickets shipped against a spec that has since drifted (needs `LINEAR_API_KEY`). | 1 with `--exit-code` on DANGER/BLOCKED (or WARNING too under `--warn-exit`), 2 on tool error |
| `init [--docs-root ...] [--linear-team KEY]` | Scaffold `.doc-lattice.yml` and print `.gitignore`, pre-commit, and CI guidance. | 2 on tool error |

Only `check` and `lint` gate by default, exiting 1 when they find drift or an authority inversion.
`impact`, `reconcile`, `graph`, and `init` are informational and always exit 0 on success (2 only on
a tool error), so wiring `impact` into a CI gate never turns the build red. `linear` also exits 0 by
default; pass `--exit-code` to gate on any DANGER or BLOCKED finding, and add `--warn-exit` to gate on
WARNING as well.

Every command except `init` accepts `--config PATH` (path to `.doc-lattice.yml`; defaults to
the file in the current directory). `check`, `lint`, `impact`, `reconcile`, and `linear` accept
`--json` for machine-readable output. Run `uv run doc-lattice <command> --help` for the full
flag list.

Pass `--indent N` with JSON output on `check`, `lint`, `impact`, or `linear` to pretty-print the
JSON with `N` spaces per level. JSON output is selected by `--json`, or the equivalent
`--format json` on `check` and `lint`; `--indent` without JSON output is a usage error.

Use the global `--no-color` option before the command to disable colored output explicitly, for
example `doc-lattice --no-color check`. Rich also honors the [`NO_COLOR`](https://no-color.org/)
environment variable; `--no-color` is the command-line equivalent. Either one also strips the
styling from help and usage-error text even when a terminal-forcing variable is set.

`check` and `lint` also accept `--format human|json|github`. `human` is the default, and `json`
is equivalent to the existing `--json` alias. `github` emits one escaped GitHub Actions `::error`
workflow command per drift finding or ladder violation, each with a repo-relative file path, so
findings attach inline to the offending doc in the pull-request diff. Output selection never
changes gate exit codes. Do not combine `--json` with `--format github`.

Structured-output compatibility is command-specific. `--json` remains silent throughout 1.x, with
no deprecation warning on stderr. The selector matrix is:

| Release | Commands | Selection |
|---------|----------|-----------|
| 1.x | `check`, `lint` | `--format human\|json\|github`, plus silent `--json` alias |
| 1.x | `graph` | `--format mermaid\|dot\|json`; no `--json` alias |
| 1.x | `impact`, `reconcile`, `linear` | Human default; only silent `--json` selector |
| 1.x | `init` | Deliberately excluded from structured-output selection |
| 2.0 | `check`, `lint` | `--format human\|json\|github`; no `--json` alias |
| 2.0 | `graph` | `--format mermaid\|dot\|json`; no `--json` alias |
| 2.0 | `impact`, `reconcile`, `linear` | `--format human\|json`; no `--json` alias |
| 2.0 | `init` | Remains excluded from structured-output selection |

Where supported, `--indent` continues to require an effective JSON format. The 2.0 changes remove
`--json` from `check`, `lint`, `impact`, `reconcile`, and `linear`; `graph` never accepts that
alias.

`impact` walks the full transitive closure by default. Pass `--depth N` (N >= 1) to bound the
walk to N hops from TOKEN: `--depth 1` lists only the docs that derive directly from it. Human
output is unchanged, and each `--json` entry gains a `"depth"` field carrying the minimum number
of hops at which that doc is reached.

`check` accepts a repeatable `--only STATE` to narrow the display to specific states (case
insensitive, e.g. `--only stale --only broken`); an unrecognized state exits 2 and names the
valid set. Filtering is display-only: the exit code always reflects every edge, so `check --only
OK` on a drifting lattice still exits 1.

### `reconcile` selectors

Normal reconcile needs either a downstream id or `--all` (running it with neither is an error):

- **`reconcile DOWNSTREAM_ID`**: reconcile every drifting edge of one downstream node.
- **`reconcile DOWNSTREAM_ID --ref REF`**: narrow to a single upstream ref on that node, selected
  by resolved identity; refused if it targets a BROKEN edge.
- **`reconcile --all`**: clear every STALE/UNRECONCILED edge in the lattice. Skips BROKEN and
  already-OK edges, and skips a node's broken edge rather than failing the node, so one dangling
  ref never blocks the rest.
- **`reconcile --all --ref REF`**: reconcile matching drifting edges across every downstream
  node. Nonmatching, BROKEN, and already-OK edges are skipped; unlike the single-node form, no
  match is a successful no-op.
- **`reconcile --recover`**: perform recovery or cleanup for an outstanding transaction and exit
  without loading the lattice or planning a new batch. It cannot be combined with a downstream id,
  `--all`, `--ref`, or `--dry-run`; those combinations exit 2. `--json` is supported.

`reconcile` re-reads each downstream file fresh at write time, rewrites only the targeted `seen`
scalar through round-trip YAML (preserving your body, key order, and comments), and retains the
exact source and replacement bytes. A real run stages exact before and after images, publishes a
`prepared` journal, fingerprints each destination immediately before its replacement, and rejects
a changed destination as a conflict. The full batch is rolled back in reverse order if a conflict
or write/durability failure occurs before the committed marker. After every replacement is durable,
the journal becomes `committed`; success output waits until committed cleanup and a clean
advisory-lock release have both completed.

Every reconcile mode holds a nonblocking advisory lock on the existing project-root directory
through preflight, planning, and any recovery or commit. A competing invocation exits 2 with
`another reconcile is in progress; retry after it exits` and does not inspect or alter the active
transaction. The durability guarantee assumes a local filesystem with reliable advisory-lock,
atomic-rename, and directory-sync semantics. Network filesystems such as NFS may weaken or emulate
`flock`, so reconcile on them is outside this durability contract.

A real reconcile checks for recovery immediately after config and lock setup, before loading the
lattice. A `prepared` journal rolls transaction-owned after images back to their exact before
images; unrelated edits are preserved. A `committed` journal keeps the committed destinations and
finishes artifact cleanup. Automatic recovery is reported once on stderr, then the newly requested
reconcile proceeds. This ordering ensures the new plan sees recovered files.

Add `--dry-run` to any normal selector above to preview the plan without writing: it prints
`would reconcile FILE: REF` per edge that would change (`nothing to reconcile` if none would),
and remains byte-, namespace-, and cache-read-only. It does not create, rewrite, recover, or remove
the journal or staged images, and it does not persist the optional load cache. If an outstanding
journal exists, dry-run exits 2, names it, and tells you to run `reconcile --recover` first without
loading the lattice. Combine a safe dry-run with `--json` for a machine-readable plan:
`{"dry_run": true, "reconciled": [{"path": ..., "ref": ..., "new_seen": ...}]}`, sorted by path
then ref. A real run with `--json` emits the same shape with `"dry_run": false`, after the
durable commit, artifact cleanup, and lock release complete. Failed real batches emit no human
`reconciled` lines and no JSON success payload. A source conflict names the changed destination and
says whether rollback completed; an I/O or durability failure names the failed operation and says
whether rollback completed or recovery evidence remains.

### Reconcile recovery and artifacts

The project-root transaction journal is `.doc-lattice-reconcile.json`. Its state is `prepared` or
`committed`, and each entry records project-relative destination, before-image, and after-image
paths plus full SHA-256 fingerprints. Temporary files use these exact patterns:

```gitignore
.doc-lattice-reconcile.json
.doc-lattice-reconcile.json.*.tmp
.*.doc-lattice-before.*.tmp
.*.doc-lattice-after.*.tmp
```

Before and after images are staged beside each destination, so the last two patterns ignore staged
images in nested document directories as well as at the project root. `doc-lattice init` always
prints this block and tells you to append it to `.gitignore`; it never reads, creates, appends to,
or overwrites `.gitignore` itself.

After an interrupted run, use this workflow:

1. Stop any other reconcile and run `doc-lattice reconcile --recover` from the project root. A safe
   rerun of a normal real reconcile also performs this recovery before lattice loading.
2. A valid `prepared` journal reports `rolled back reconcile transaction: JOURNAL`; a valid
   `committed` journal reports `cleaned committed reconcile transaction: JOURNAL`; no journal
   reports `nothing to recover: JOURNAL`. All three outcomes exit 0.
3. For machine-readable recovery, add `--json`. The complete stdout object contains exactly
   `action` and `journal`, with no additional keys, for example
   `{"action": "none", "journal": "PATH"}`. `action` is `none`, `rolled_back`, or
   `cleaned_committed`.

A malformed or unsafe journal exits 2 and is not deleted. Inspect the named journal, destinations,
and staged files; restore each destination or deliberately preserve its current contents; then move
the invalid journal aside only after that manual restoration or preservation and rerun
`doc-lattice reconcile --recover`.

Missing, corrupt, nonregular, or otherwise unauthenticated staged evidence also exits 2 without
unsafe cleanup. Preserve the journal and available staged files, restore or correct the required
evidence named by the diagnostic, or manually preserve the affected destination, then rerun
`doc-lattice reconcile --recover`. Do not delete evidence or guess which image is authoritative
before inspecting its recorded fingerprint. If rollback itself fails, the diagnostic names the
remaining artifacts and the destination that still needs manual attention.

## Frontmatter reference

| Key | Where | Meaning |
|-----|-------|---------|
| `id` | every tracked file | The file's stable id. Required. |
| `title` | optional | Display title. |
| `layer` | optional | `design`, `technical`, or `production`. |
| `authority` | optional | `binding`, `derived`, or `exploratory`. Ranked by `lint`. |
| `derives_from` | downstream files | List of `{ ref, seen }` edges. |
| `derives_from[].ref` | each edge | The upstream id: bare (whole-file target, e.g. `api-design`) or file-scoped (section target, e.g. `api-design#pagination`). |
| `derives_from[].seen` | each edge | The locked upstream hash, or omitted for a never-reconciled (UNRECONCILED) edge. |
| `tickets` | optional | Issue ids associated with the doc (used by `impact` and `linear`). |

Section ids are optional: a heading is addressed by its GitHub slug by default (e.g.
`## Error Handling` resolves to `error-handling`). An explicit marker must be the trailing heading
token and match `{#[A-Za-z0-9][A-Za-z0-9_-]*}`; a whitespace-separated ATX closing sequence may
follow it (e.g. `## Error Handling {#errors} ##`). A valid marker supplies the stable anchor
independent of heading text. Invalid or nontrailing marker-like text is ordinary heading content,
so the heading falls back to its generated GitHub slug. Section refs are file-scoped
(`file#anchor`), so the same anchor in two files does not collide.

Addressable sections intentionally use a narrow Markdown subset: column-zero ATX headings at
levels 1 through 6, including empty headings and optional ATX closing sequences. CommonMark
backtick and tilde fences suppress headings inside them. Setext headings, headings in block quotes
or list items, and indented headings are not addressable. Inline Markdown remains part of the raw
heading text used for slugging. Heading and fence recognition is pinned to
`markdown-it-py==4.2.0`; generated slugs and document-order duplicate suffixes target
`github-slugger@2.0.0` under JavaScript Unicode 17.0. Generated lowercase patches and contextual
casing-property tables bridge the minimum supported Python 3.13 Unicode 15.1 table to that target.

## Configuration

doc-lattice runs zero-config (defaulting to a `docs/` root), or reads `.doc-lattice.yml`
from the current directory:

```yaml
# doc-lattice configuration
docs_roots:
  - docs                  # roots to scan for tracked .md files (default: ["docs"])
# ignore_globs:           # paths to skip within those roots
#   - "**/superpowers/plans/**"
# cache_key: my-docs      # opt-in incremental load cache slot (see Load cache below)
# cache_trust_stat: false # opt-in stat fast tier for read-only commands (accepts the mtime caveat)
# linear_team: ENG        # the Linear team the `linear` query targets
# binding_layers: null    # accepted but inert today; setting it changes nothing (see below)
```

The project root is the resolved parent of the selected config file, including an explicit
`--config PATH`, or the resolved current directory in zero-config mode. Relative `docs_roots`
entries are interpreted from that project root. Every root must resolve inside it; an entry that
escapes via `..`, an absolute path, or a symlink is rejected before any read.

Discovered document symlinks are resolved separately. A symlink whose target stays inside the
project root is allowed, while one targeting anything outside is skipped with a warning. If
multiple roots or symlink aliases resolve to the same document, it is loaded once under the first
unresolved path discovered. Reconcile re-resolves that identity path before writing so a retargeted
symlink cannot escape the project root.

`binding_layers` is accepted in the config for forward compatibility but is inert today: setting it
changes nothing, because no command consults it. Authority ranking currently lives entirely in
`lint` (binding > derived > exploratory); the
[lint design spec](docs/superpowers/specs/2026-06-28-doc-lattice-lint-design.md) preserves
historical design context for that ranking.

### Load cache (opt-in)

Large doc sets (thousands of files) can skip re-parsing unchanged docs with an opt-in cache.
Set `cache_key` to a single safe segment (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`); it names a slot
under your user cache home at `<cache_home>/doc-lattice/<cache_key>/load-cache.json`, where
`<cache_home>` is `$XDG_CACHE_HOME` (when absolute) or `~/.cache`. The cache lives outside every
checkout on purpose: because `.doc-lattice.yml` is committed, every clone and git worktree of the
project shares one warm cache with no per-checkout setup, which an in-repo cache could not do.

By default the cache re-reads and re-hashes each file's bytes every run, so its output is always
byte-identical to an uncached run under any cache state (cold, warm, stale, structurally corrupt, or
wrong version); only timing differs. A structurally corrupt cache (unreadable, non-JSON, wrong
version, or schema-invalid) is discarded wholesale and rebuilt; the cache is a trusted single-writer
file under your own cache home, so it is not hardened against hand-edited tampering that stays
schema-valid. Setting `cache_trust_stat: true` adds a faster tier for read-only commands that trusts
a file whose size and modification time are unchanged, accepting that the file is not opened at all:
a rewrite that preserves both its size and its nanosecond mtime is served stale, and a file made
unreadable (for example a permissions change, which does not alter size or mtime) is served from
cache instead of erroring, each until the file is touched. `reconcile` ignores `cache_trust_stat`
and always verifies content, so it can never write frontmatter from stale data.
`cache_trust_stat: true` requires `cache_key`; otherwise config loading is a tool error and exits 2.
Two projects sharing a `cache_key` stay correct (a content-hash
hit implies identical bytes); the only cost is overwrite churn, so prefer distinct keys. Delete the
cache directory to reset it; a tool-version bump discards it automatically.

Any cache read failure, including an unreadable, invalid, or stale cache file, silently falls back
to rebuilding from documents. A cache write failure emits one stderr diagnostic and is otherwise
ignored: it does not change command results or exit codes.

## Adopting doc-lattice in your docs repo

Bootstrap config and the drift and authority-ladder gates for a repo whose docs you want to
track:

```bash
uvx --python 3.13 --from doc-lattice==1.0.1 doc-lattice init
```

This writes `.doc-lattice.yml` (only if absent) and always prints the reconcile-artifact
`.gitignore` block above, pre-commit hooks, and a GitHub Actions workflow that run
`doc-lattice check` (drift) and `doc-lattice lint` (authority ladder) as your gates. Paste each
where the output says. `init` only prints `.gitignore` guidance and never modifies that file. Pass
`--docs-root` (repeatable) or `--linear-team` to bake those values into the generated config.
The generated gates remain fully offline: they run only `check` and `lint` and do not require or
receive `LINEAR_API_KEY`.

To test an unreleased commit, replace the PyPI requirement with a Git source such as
`--from git+https://github.com/Guardantix/doc-lattice@<commit>`; released configurations should
keep the exact PyPI version pin.

## Linear integration

`doc-lattice linear` is the only network-touching command. It builds a trigger map from the
loaded lattice, then fetches live ticket status over the Linear GraphQL API to report tickets
that shipped against a spec that has since drifted. It reads `LINEAR_API_KEY` from the
environment (export it before running; the error points you to `impact` for the offline view),
and the client is https-only, redirect-refusing, size-capped, and SSRF-hardened. A transient
HTTP 429 or 5xx gets two retries, for three total attempts. Without a usable `Retry-After`, retries
wait 1 second and then 2 seconds. A non-negative integer `Retry-After` is honored up to the
30-second cap; negative, date-form, and invalid values use the fallback delay.

> **Security note:** If `linear` is used in CI, run it only on trusted refs and never in a fork
> pull-request job, whether or not `--exit-code` is used. The command processes
> repository-controlled `tickets` and `linear_team` while `LINEAR_API_KEY` is present. Fork
> pull-request workflows should use the offline `check`, `lint`, and `impact` commands instead.

Canonical ticket ids are uppercase ASCII `TEAM-NUMBER`: `TEAM` starts with an uppercase letter
and continues with uppercase letters or digits, while `NUMBER` is `0` or a decimal with no leading
zeros. One `linear` run accepts at most 500 distinct ticket refs after its positional or `--from`
scope is applied. Set the team the query targets with `linear_team` in `.doc-lattice.yml`, or pass
`--linear-team` to `init`. Every other command runs fully offline.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success; no drift or violations. |
| `1` | The lattice is coherent but a gate failed: drift (`check`), an authority inversion (`lint`), or (with `--exit-code`) a DANGER/BLOCKED `linear` finding. |
| `2` | Tool error: invalid or unclosed frontmatter, invalid config, unreadable or non-UTF-8 input, incoherent ids, containment failure, reconcile conflict/lock contention, or persistence/recovery failure. |

## Troubleshooting

**`LINEAR_API_KEY is not set`.** Only the `linear` command needs a key. Export a Linear API key
(`export LINEAR_API_KEY=lin_api_...`) before running `linear`, or, when live Linear status is
unnecessary, run `impact` instead: `impact` is the fully offline view of the same downstream reach
and needs no key.

**Linear returns HTTP 429 or 5xx.** These are transient. The client makes at most three attempts,
using the 1- and 2-second fallback delays or a capped, non-negative integer `Retry-After`. If it
still fails, the error tells you to wait and re-run; `impact` stays available offline in the
meantime.

**A `linear` finding is BLOCKED `not-found`.** A ticket the Linear filter does not return is treated
as absence, not an error: it grades as a BLOCKED `not-found` finding rather than crashing the
command. Confirm the ticket id exists and that `linear_team` targets the right team.

**`unclosed YAML frontmatter ...` exits 2.** A file beginning with `---` must add another `---`
line after its YAML metadata. The message names the malformed file; a file with no opening fence
remains ordinary untracked Markdown.

**`duplicate id ...` exits 2.** A duplicate id makes the index incoherent, so loading the lattice
fails with exit 2 (a tool error, distinct from the exit 1 that `check` and `lint` use for drift).
The message names both registration sites so you can find the clash: either two files share an
`id`, or two headings in one file resolve to the same anchor through equal markers or a marker/slug
collision. Equal anchors in different files do not collide.

## Documentation

| Document | Purpose |
|----------|---------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and the decision log |
| [CLAUDE.md](CLAUDE.md) | Architecture map and tooling-enforced invariants |
| [roadmap.md](roadmap.md) | Shipped slices and what is deferred |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [RELEASING.md](RELEASING.md) | Release checklist and version-tag procedure |
| [build-log.md](build-log.md) | Historical development timeline |
| [docs/superpowers/specs/](docs/superpowers/specs/) | Historical design context; current code and supported docs supersede conflicts |
| [docs/superpowers/plans/](docs/superpowers/plans/) | Historical implementation context, not current user-facing documentation |

## Project structure

```
doc-lattice/
├── src/doc_lattice/         # the engine: a pure graph/report core behind a thin impure shell
│   ├── markdown_compat.py      # pinned heading and GitHub-slug compatibility adapter
│   ├── _github_slugger_data.py # generated slug and Unicode compatibility data
│   ├── persistence.py          # shared durable single-path filesystem primitives
│   ├── reconcile_transaction.py # reconcile lock, journal, commit, rollback, and recovery
│   └── cache/               # phase-separated incremental load cache
│       ├── schema.py        # filesystem-free models and codec
│       ├── state.py         # filesystem-free run-local state
│       ├── lookup.py        # document reads and stats for cache-tier selection
│       └── store.py         # cache-file reads and atomic writes
├── tests/                   # test suite (mirrors sources; property-based hashing invariants)
├── scripts/                 # CI guards plus slug generation and section benchmark tools
├── docs/superpowers/        # historical design and implementation context
└── pyproject.toml           # project configuration
```

The engine is a pure pipeline (`config -> discovery -> frontmatter parse -> build_lattice`
feeding `{ check, impact, reconcile, graph, lint, linear }`) where all graph and report logic
is filesystem-free. `config`, `discovery`, and `orchestrate` own load-path filesystem work;
`persistence.py` owns shared durable single-path operations; `reconcile_transaction.py` owns the
reconcile lock capability, independent live commit destination preflight, durable batch, rollback,
and recovery; and the `doc_lattice.cli` package orchestrates and reports those operations. Cache I/O
is split between document-reading `cache/lookup.py` and cache-file-owning `cache/store.py`. Only
`linear_client` touches the network. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the decisions behind that split.

## License

MIT. See [LICENSE](LICENSE).
