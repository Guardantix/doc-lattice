# doc-lattice

[![CI](https://github.com/Guardantix/doc-lattice/actions/workflows/ci.yml/badge.svg)](https://github.com/Guardantix/doc-lattice/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/doc-lattice)](https://pypi.org/project/doc-lattice/)
[![Python versions](https://img.shields.io/pypi/pyversions/doc-lattice)](https://pypi.org/project/doc-lattice/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/Guardantix/doc-lattice/blob/main/LICENSE)

A deterministic, offline traceability engine for design and production documentation.

doc-lattice tracks the dependencies *between* your markdown docs. When a downstream
document derives from an upstream one (an integration guide built on an API design, an
engineering design built on a product brief), it records that link in frontmatter. When
the upstream changes, doc-lattice tells you exactly which downstream docs went stale, and a
CI gate keeps stale work from shipping silently.

It is pure tooling: no network (except the optional `linear` command), stores no secrets, uses no
LLM, and needs no database. The dependency graph is derived from your docs on demand, never
committed.

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

`check` classifies every edge into one of five states:

| State | Meaning |
|-------|---------|
| **OK** | `seen` matches the upstream's current content. In sync. |
| **STALE** | The upstream changed since `seen` was locked. The downstream needs review. |
| **UNRECONCILED** | The edge has no `seen` yet. The dependency was declared but never acknowledged. |
| **BROKEN** | The ref points at an id that no longer exists. |
| **AMBIGUOUS** | The ref resolves, but its target id sits in a slug-collision component, so document order can hand that id to a different heading. `check` exits 1, `reconcile` refuses to write `seen`, and every command names the colliding headings. `lint` reports it without gating on it, so its GitHub annotation for an ambiguous edge is a `::warning` where `check`'s is an `::error`. |

The content hash is `sha256` of a *canonicalized* copy of the text, truncated to 128 bits.
Canonicalization normalizes line endings, strips trailing whitespace per line, and trims
leading and trailing blank lines, so those cosmetic edits never trip drift. Internal
whitespace is preserved, so rewrapping a paragraph (which moves its line breaks) does count
as a change.

The hash input never includes frontmatter. A `file` ref hashes the canonicalized document body;
a `section` ref hashes its ancestor heading chain followed by the section text, so a section that
moves under a different parent heading, or whose ancestor is reworded, goes STALE even when its
own text is untouched; a `file` ref has no ancestor chain and is unaffected. The chain covers
every heading form GitHub assigns an id to, so a setext, indented, or nested parent supplies
context to the sections under it just as a column-zero ATX one does. Because `reconcile`
writes only `seen`, and `seen` lives inside frontmatter, a reconcile write can never change any
target's hash. That is what lets `reconcile --all` converge in one pass over a stable snapshot:
acknowledging one edge cannot invalidate another. Convergence covers the reconcilable edges
only, since `reconcile` skips BROKEN ones and they remain findings after the pass. The write
scope and selector semantics restated here are owned by
[RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md).

### Broken refs and tool errors

A ref that points at nothing is a normal, reportable lattice state: `check` calls it BROKEN
and exits 1. Invalid config or lattice frontmatter, unreadable or non-UTF-8 documents,
containment failures, and incoherent ids are tool errors that exit 2. An index is incoherent
when two files repeat a file id or two headings in one file resolve to the same file-scoped
anchor. Equal anchors in different files, and a file id equal to another file's anchor, remain
distinct `TargetId(file_id, anchor)` keys and do not collide. A **stdout** write refused because
the pipe's reader closed (for example, piping into `head`) exits 141, printing nothing. A refused
**stderr** write is not an outcome: the diagnostic is dropped and the command still exits on what
it found, so `2>/dev/null` and a departing log reader leave every code above unchanged.

A Markdown file without an opening `---` fence is valid untracked prose. Once a file opens YAML
frontmatter with `---`, it must include a closing `---` fence; otherwise every lattice-loading
command names the file, asks for the missing close, and exits 2 instead of omitting the node.

Fenced frontmatter with no `id` is graded by what else it declares, since dropping a node also
drops every edge it declares. A block carrying any of `derives_from`, `authority`, or `tickets`
meant to be a node, so the missing `id` is a tool error that names the file and exits 2 rather
than a silent omission. Any other id-less block is frontmatter this engine does not own, so the
file is skipped with a warning on stderr naming it, and the exit status is unchanged. The two
tiers are described in full under [Frontmatter reference](#frontmatter-reference).

### The authority ladder

Separately from drift, `lint` enforces a structural rule: authority only flows downhill.
Docs can declare an `authority` of `binding`, `derived`, or `exploratory`. A `derives_from`
edge from a more-authoritative doc to a less-authoritative one is an **inversion** (a binding
spec should not derive from an exploratory sketch), and `lint` fails on it. `lint` is pure
structure, independent of drift, and exits 1 on a violation just like `check`.

An edge whose source or target declares no `authority` cannot be ranked. `lint` reports it as
unranked rather than as a violation, so it never fails the gate. Every human run ends with a
coverage line counting violations and unranked edges, and `--format json` carries the same edges
in a `skipped` array with a `source-unannotated` or `target-unannotated` reason.

Ambiguity is the third thing `lint` reports and the one it does not gate on. An ambiguous edge
adds a row above the findings, a count to the end of that coverage line, and an `ambiguous`
array to `--format json`, but never changes the exit code: that stays the ladder's alone, and
`check` is the command that fails a lattice for ambiguity.

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
1 edge: 0 OK, 1 STALE, 0 UNRECONCILED, 0 BROKEN, 0 AMBIGUOUS

$ doc-lattice impact api-design#pagination
billing-integration-guide  ('/work/acme-api/docs/billing-integration-guide.md')  tickets: ENG-412
```

`check` exits 1, so CI is now red. A human reviews the guide against the new pagination
scheme, updates the body if needed, and then locks in the new hash:

```console
$ doc-lattice reconcile billing-integration-guide
reconciled 'billing-integration-guide.md': api-design#pagination

$ doc-lattice check
1 edge: 1 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN, 0 AMBIGUOUS
```

The listing is empty because there is nothing left to act on; the verdict line is what states
the run was clean.

That edit → `check` → review → `reconcile` loop is the whole workflow. `reconcile` is the
only command that writes to your docs, and it only ever rewrites `seen` values and the
aliases that read them.

## Quick start

### Prerequisites

- Python 3.13+ (the floor is load bearing; see
  [AD-24](https://github.com/Guardantix/doc-lattice/blob/main/ARCHITECTURE.md))
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

Contributor commands, gates, and the full verification set live in
[CLAUDE.md](https://github.com/Guardantix/doc-lattice/blob/main/CLAUDE.md).

## Commands

| Command | What it does | Exits non-zero |
|---------|--------------|----------------|
| `check [--only STATE ...] [--format human\|json\|github]` | Classify every `derives_from` edge as OK / STALE / UNRECONCILED / BROKEN / AMBIGUOUS. | 1 on drift, 2 on tool error |
| `lint [--format human\|json\|github]` | Validate the authority ladder (binding > derived > exploratory) over the edges. | 1 on a violation, 2 on tool error |
| `links [--format human\|github]` | Validate every relative link destination and heading fragment in the `link_sources` files. | 1 on a finding, 2 on tool error |
| `impact TOKEN [--depth N] [--format human\|json]` | List every downstream doc affected by a change to TOKEN; `--depth N` bounds the walk to N hops. | 2 on tool error |
| `reconcile [ID] [--ref REF] [--all] [--dry-run] [--recover] [--format human\|json]` | Durably set `seen` for selected edges as one transaction, preview read-only with `--dry-run`, or recover an interrupted transaction with `--recover`. | 2 on tool error, conflict, lock contention, or persistence/recovery failure |
| `graph [--format mermaid\|dot\|json]` | Emit the edge graph as Mermaid, DOT, or JSON. | 2 on tool error (including an unrecognized `--format`) |
| `linear [TARGET] [--from ID] [--exit-code] [--warn-exit] [--format human\|json]` | Report tickets shipped against a spec that has since drifted (needs `LINEAR_API_KEY`). | 1 with `--exit-code` on DANGER/BLOCKED (or WARNING too under `--warn-exit`), 2 on tool error |
| `init [--docs-root ...] [--linear-team KEY] [--default-branch NAME] [--print-only]` | Scaffold `.doc-lattice.yml` and print the `.gitignore`, pre-commit, and GitHub Actions blocks to install by hand. With `--print-only`, print the same three blocks and write nothing. | 2 on tool error |

`check`, `lint`, and `links` gate by default, exiting 1 when they find drift, an authority
inversion, or a dead link.
`impact`, `reconcile`, `graph`, and `init` are informational and exit 0 on success, so wiring
`impact` into a CI gate never turns the build red. `linear` also exits 0 by default; pass
`--exit-code` to gate on any DANGER or BLOCKED finding, and add `--warn-exit` to gate on WARNING
as well.

The lattice-loading commands `check`, `lint`, `impact`, `reconcile`, `graph`, and `linear`, and
the link gate `links`, accept `--config PATH` (path to `.doc-lattice.yml`; defaults to the
file in the current directory).
`init` deliberately does not accept config or load the lattice. It keeps its current-directory
behavior and does not require Git: it reads local Git state only to guess the workflow's default
branch, and falls back when that read finds nothing.
Run `uv run doc-lattice <command> --help` for the full flag list.

The three printed blocks are hand-maintained artifacts you re-fetch on every upgrade, so
`--print-only` gets them without any chance of a write. It prints byte-for-byte what an ordinary
run prints, its only meaningful flag is `--default-branch`, and combining it with `--docs-root` or
`--linear-team` is a usage error rather than a silently ignored request, because those two feed
only the config this mode does not render.

`init` reports its status on stderr: that it wrote `.doc-lattice.yml`, or that the file already
exists and was left untouched, and which branch the printed workflow triggers on. Those three
status records are each one record on one line at any terminal width, the same promise the
`check` verdict line carries below, so a long branch name stays intact for a line-oriented
pipeline instead of wrapping mid-token on a narrow console. The promise covers exactly those
records: the Git precondition, placement, baseline, and activation guidance printed beside them
is prose and wraps normally.

Because `init` writes into the current directory and every lattice-loading command selects its
config from *its* current directory, a run from a subdirectory of a repository whose root is
already configured refuses rather than scaffolding a second, nested config. That nested file
would not be inert: commands run from that subdirectory would load it, while every run from the
configured directory carried on reading the original, which is a silently divergent second
lattice rather than a harmless extra file. The diagnostic names the configuration it found and
points at both ways forward: run `init` in that directory, or pass `--print-only` here. A deliberately nested lattice is still supported
and is one hand-written file, since the exact bytes `init` writes are printed under
[Configuration](#configuration) below. The refusal is bounded to the repository: the search walks
up from the current directory to the nearest `.git` entry, inclusive, so a config above your
checkout never blocks a new project, and a submodule or nested repository under a configured root
scaffolds normally. That boundary is a filesystem check rather than a Git query, because `init`
has no Git prerequisite; under `GIT_DIR`, `GIT_CEILING_DIRECTORIES`, or
`GIT_DISCOVERY_ACROSS_FILESYSTEM` it can therefore disagree with Git's own idea of where the
repository begins. The cost of that is one refusal too many or one too few, never a file written
in the wrong place, because the boundary bounds only the refusal and never selects a destination.
An entry the search cannot read at all is outside that trade: rather than assume it away, `init`
declines with `INIT_PERSISTENCE` and names the entry, since whether you are standing inside an
already-configured lattice is not something to guess at in either direction.

Pass `--indent N` with JSON output on `check`, `lint`, `impact`, or `linear` to pretty-print the
JSON with `N` spaces per level. JSON output is selected uniformly by `--format json`; `--indent`
without an effective `--format json` is a usage error.

`graph --format json`'s `ambiguous_targets` list is lattice-wide: it names every slug-collision
component in the loaded docs, whether or not any edge targets it. `check`, `lint`, `impact`, and
`linear`'s JSON only ever names a collision an edge actually resolved into.

Two options are global rather than per-command, so they go before the command name:

| Global option | What it does |
|---------------|--------------|
| `--version` | Print the installed version to stdout and exit 0, as in `doc-lattice --version`. |
| `--no-color` | Disable all styling for the run, as in `doc-lattice --no-color check`. |

`--version` is eager: it prints and exits before any command runs, so it needs no config file, no
docs root, and no network. That makes it the check to run against a fresh install.

Rich also honors the [`NO_COLOR`](https://no-color.org/) environment variable; `--no-color` is the
command-line equivalent. doc-lattice intentionally extends the `NO_COLOR` baseline: the standard
itself only asks implementers to drop color and leaves bold, underline, and italic styling in
place, but either lever here means no styling at all, so no terminal escape sequence reaches
human-facing output under either one, even when a terminal-forcing variable is set. This covers
every command's human-facing output, not just help and usage-error text: reports, warnings, and
diagnostics alike.

One machine channel is excluded from that guarantee, deliberately and by name: the `file=` value
of a `--format github` annotation. GitHub resolves that value against the file the annotation
attaches to, so the document's repo-relative filename is spelled by the workflow-command grammar
rather than by the rule above. That grammar substitutes only `%`, `:`, `,`, carriage return, and
line feed, so a filename carrying a carriage return or a line feed is encoded and reaches you as
`%0D` or `%0A`. Every other control character passes through raw, `ESC`, tab, `DEL`, and the C1
range included: none of them has a spelling GitHub decodes back to the original filename, and
deleting one would map two differently named documents onto a single annotation. Attachment is
worth more there than styling a channel no terminal renders, so the raw spelling stays. Nothing
else is excluded: `--format json` keeps the guarantee, because JSON's own encoder escapes control
characters, and the `check` and `lint` payloads carry no filename at all.

`check` and `lint` also accept `--format human|json|github`. `human` is the default. `github`
emits one escaped GitHub Actions `::error` workflow command per drift finding or ladder
violation, each with a repo-relative file path, so findings attach inline to the offending doc
in the pull-request diff. Output selection never changes gate exit codes.

A run that fails on one document rather than finding drift is annotated the same way. A broken
frontmatter block, or a document that cannot be read or decoded, ends the run with exit 2 and
emits a single annotation attached to that document, titled with the error code
(`FRONTMATTER_ERROR`, `UNREADABLE_DOC`) and carrying the same text stderr reports. The stderr
diagnostic is unchanged and still printed. A failure that names no single document -- a config
defect, a broken ref, a reconcile transaction failure -- emits no annotation, because there is no
file for GitHub to attach one to. The same unattachable warning applies: if the failing document falls outside the
base, the run says so on stderr, since that annotation is the only one it emits. The other
formats are unaffected: under `--format human` and `--format json` a failing run writes nothing
to stdout and reports only on stderr.

The reported path is relative to `GITHUB_WORKSPACE` when that variable is set and contains the
document, which is the repository checkout root under GitHub Actions; annotations therefore
stay attachable no matter which subdirectory the command runs in. Otherwise the base is the
invocation working directory, and a document outside it is reported by absolute path rather
than failing. Under a workspace the base is never the config file's project root: a config
under `packages/game` still reports `packages/game/docs/down.md`, not `docs/down.md`. Only a
run with no workspace set, from inside `packages/game`, reports `docs/down.md`, because there
the base is that working directory.

When a document falls outside that base, its annotation carries an absolute path, which GitHub
drops instead of attaching. That is reported: the run warns once on stderr, naming the base and
every document outside it, so a gate that fails with nothing shown on the diff is diagnosable
from the workflow log. Stdout stays exactly the workflow commands, so the warning never
interferes with what GitHub parses.

Structured output is always selected with `--format`; the accepted values per command are in the
table above, and `init` is deliberately excluded from structured-output selection.
The 1.x silent `--json` alias was removed in 2.0; see
[CHANGELOG.md](https://github.com/Guardantix/doc-lattice/blob/main/CHANGELOG.md) for the migration.

`impact` walks the full transitive closure by default. Pass `--depth N` (N >= 1) to bound the
walk to N hops from TOKEN: `--depth 1` lists only the docs that derive directly from it. Human
output is unchanged, and each JSON entry gains a `"depth"` field carrying the minimum number
of hops at which that doc is reached.

Human `check` output lists problem edges only. OK edges are classified, counted, and reported
everywhere else, but they are not listed as rows, so the default invocation on a large lattice
shows what needs acting on rather than thousands of `OK` lines. This matches `lint`, which has
always printed violations only, so the two gating commands now ship one output philosophy
instead of opposite ones. The verdict line below is what makes the omission safe rather than
lossy: the totals and every per-state count stay visible, so a clean run is explicit rather than
silent. `--only` overrides the default in both directions, and `--format json` is unaffected.

Every human `check` run ends with a one-line verdict counting the classified edges and breaking
them down per state, for example
`101 edges: 96 OK, 5 STALE, 0 UNRECONCILED, 0 BROKEN, 0 AMBIGUOUS`. Every
state is listed, including the ones with a zero count, so truncated output such as `check | tail`
still states the result rather than trailing off into whichever edges sort last. The line is one
record on one line at any terminal width, so `check | tail -1` always gets the whole verdict.

The line is present on a clean lattice too, where it is the entire output, and a lattice with no
edges at all reports `0 edges: 0 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN, 0 AMBIGUOUS`. Because the
state names are always printed, match on the exit code rather than grepping human output for a
state name.

`--format json` carries the same counts in a `summary` object alongside `edges`, so a wrapper
can answer "is the tree clean?" without folding every edge record. JSON is the complete
structured record and still carries one entry per edge, OK edges included; the problem-only
default is a human-output rule and does not reach it. `--format github` is likewise unchanged:
it emits annotations for problems and stays silent on a clean tree.

`check` accepts a repeatable `--only STATE` to select which states are displayed (case
insensitive, e.g. `--only stale --only broken`); an unrecognized state exits 2 and names the
valid set. Supplying it replaces the defaults in both formats that list records: human output
shows exactly the selected states, so `check --only OK` lists the OK edges the default omits,
and JSON narrows `edges` to them. Filtering is display-only: the exit code and the summary
counts always reflect every edge, so `check --only OK` on a drifting lattice still exits 1 and
still reports the drift in its verdict. One consequence is deliberate: under `--only`, the
`summary` counts do not sum to the number of records in `edges`.

### links

`links` is the Markdown link gate, and it is deliberately not part of `check`. `check` is the
lattice edge gate and its green is honest precisely because it makes no claim about Markdown
links; folding link coverage into it would make that green a lie the first time it was wrong.

Over every file `link_sources` selects, `links` reads each Markdown link destination and resolves
the relative ones against the project root: the target must exist inside the project, and a
`#fragment` on a Markdown target must name a heading GitHub assigns that id to. That inventory is
wider than the addressable subset the lattice tracks, on purpose: setext headings, ATX headings
indented one to three spaces, and headings nested in a list item or a block quote all render and
resolve on GitHub, so failing a link to one would fail a correct link. Absolute and external
destinations, image destinations, and the `?plain=1` source view's line fragments are out of
scope and skipped.

Two gaps are declared rather than closed. A destination written as a raw HTML anchor is reported,
not resolved, because an attribute value arrives without the normalization markdown-it applies to
a Markdown destination; write it as a Markdown link. And a heading whose text is itself an inline
link slugs from raw source on both sides, so `## [Guide](target.md)` answers to `#guidetargetmd`
here where GitHub renders `#guide`.

Containment binds both ends. A selected file that leaves the project root through a symlink is
reported rather than read, and a file that will not decode as UTF-8 or that the parser refuses is
reported and stepped over, so one document cannot end the run. A filesystem the gate cannot
inspect, resolve, scan, or open is a tool error: a gate that cannot see its inputs must not pass.

Human findings go to stderr, one `'path':line: message` per finding in document order with no
line for a finding about the document itself, and nothing is printed on success. `--format
github` writes one annotation per finding to stdout instead, at the finding's line, so a dead link
shows on the pull-request diff. The generated workflow runs that form. Exit 1 on any finding, 2
when `link_sources` is missing or empty, a selector matches nothing, or the filesystem refuses the
gate, and 0 otherwise.

### `reconcile`

A normal reconcile needs either a downstream id or `--all`, and running it with neither is an
error. `reconcile DOWNSTREAM_ID` clears every drifting edge of one node, `--ref REF` narrows that
to a single upstream ref, and `reconcile --all` clears every STALE or UNRECONCILED edge in the
lattice, with or without `--ref`. Add `--dry-run` to any of those to preview the plan read-only,
or run `reconcile --recover` alone to finish an interrupted transaction without loading the
lattice.

A real run applies the whole selected batch as one durable transaction under a nonblocking advisory
lock: it re-reads each file at write time, detects a destination that changed under it as a
conflict, and rolls the batch back if anything fails before the commit. Its temporary artifacts are
a project-root journal plus staged before and after images, covered by the `.gitignore` block that
`doc-lattice init` prints.

Human output is one record per line: each per-file record
(`reconciled 'pc-design.md': art-direction#accent`, or the same line led by `would reconcile`
under `--dry-run`) and the `nothing to reconcile` all-clear stay on one line at any terminal
width, the same promise the `check` verdict line carries above, so a long document name or
target ref reaches a line-oriented pipeline intact instead of wrapping mid-token on a narrow
console.

See [RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md) for
selector details, dry-run and JSON output, the durability contract, and recovery.

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

This table describes the keys, not the envelope they load from. Every example in this README
wraps them in `---` fences, but the identical YAML also loads from an HTML comment, which GitHub
renders as nothing rather than as a table:

```markdown
<!-- doc-lattice
id: architecture
derives_from:
  - ref: readme#configuration
    seen: 647cc64481bee8d8541ef7d1733b5204
-->
```

The comment body is the identical YAML; every key and rule on this page applies to it unchanged.
The opener must be exactly `<!-- doc-lattice` on line 1, at column zero, with no byte-order mark
ahead of it, and the body ends at the first line that is exactly `-->` at column zero. Both
spellings are accepted unconditionally and forever, with no config to choose or forbid either one;
`reconcile` preserves whichever spelling a file already uses and never converts one to the other.
Unlike the fence, the comment spelling has no soft tiers: a comment envelope whose body is not a
mapping carrying `id` is a `FRONTMATTER_ERROR` at exit 2 rather than being skipped, because
`<!-- doc-lattice` names this engine and admits no other reading. The body must also not contain
`--`, since that substring is where the HTML specification and CommonMark versions disagree on
comment termination; a document whose id needs `--` keeps the fence spelling instead. See
[AD-44](https://github.com/Guardantix/doc-lattice/blob/main/ARCHITECTURE.md) for the full grammar.

Tracked lattice frontmatter is strict in both directions, and so is each nested `derives_from`
entry. An unknown key is rejected rather than ignored, and after YAML parsing each value must
already have the schema's exact type, because values are not coerced: a `tickets:` key written
with no value at all parses to null where a list is required, and fails the load with a
`FRONTMATTER_ERROR` and exit 2 rather than being read as an empty list. The strictness is scoped
to blocks the lattice tracks. A fenced block with no `id` that declares none of `authority`,
`derives_from`, or `tickets` is deliberately accepted and skipped, keys and all, so another tool
can own that file; see [Files with no `id`](#files-with-no-id).

`id`, `title`, each `tickets` entry, `derives_from[].ref`, and `derives_from[].seen` must be
single-line text carrying no control character. A value that decodes to any C0 code point
(`U+0000` to `U+001F`, tab, newline, and carriage return included), DEL (`U+007F`), or any C1
code point (`U+0080` to `U+009F`) fails the load with a `FRONTMATTER_ERROR` naming the key and
the code point. Every other character is accepted, accented text, CJK, emoji, and a no-break
space among them. The rule is what carries the `--no-color` promise above into a document's own
text, as the quoted spelling of a path carries it into a filename: a value reaches your terminal,
so it cannot carry bytes your terminal acts on. An unknown key that decodes to one is spelled in
the error rejecting it for the same reason. A block that fails to parse at all never reaches this
rule, and holds the promise a different way: the YAML parser's own message echoes what it choked
on, so it is printed in the same quoted spelling a filename gets, as one line whose line breaks
read as `\n`. That trades the parser's caret, which no longer points at anything once the message
is one line, for coordinates you can still read and a diagnostic that cannot act on your terminal.

Three spellings reach this rule, and two of them are ones you can write without meaning to.

A **literal tab** is the one control byte YAML itself admits as a raw byte, inside a quoted or a
block scalar, and nothing on screen distinguishes it from a run of spaces, so a value that looks
fine can fail. An **escape in a double-quoted scalar** is how everything else in the refused set
reaches a value, since YAML rejects those as raw bytes. `"\u001b"` is the spelling to picture, but
it is far from the only one: `\0`, `\a`, `\b`, `\t`, `\n`, `\v`, `\f`, `\r`, `\e`, and `\N`
each name a refused code point directly, and `\xNN` and `\U00000000` reach the same points as
`\uNNNN` does. A **newline from a block scalar** is the third, and it arrives in two ways that
need different fixes. A block written `|` or `>` keeps a *trailing* break, which `|-` or `>-`
chomps away. A literal block (`|` in any chomping mode) also keeps the breaks *between* its
lines, and no chomping indicator touches those, so a `|-` spanning two lines still fails; only
the folded styles join their lines with a space. Write a multi-line value as `>-`, which is the
spelling that survives this rule, and keep `|-` for a value on one line. Folding has two limits
worth knowing, because a `>-` that hits either keeps an interior break anyway: a blank line
between the lines is a paragraph break, and a line indented past the block keeps its own break.
Keep a folded value's lines adjacent and equally indented.

To find all three, do not pattern-match the YAML. Load it. Every spelling above is a property of
the value a loader constructs, not of the line it is written on, and the ways to write one value
are open-ended: a tab need not share a line with its key, a `derives_from` edge writes `- ref:`
behind a sequence dash, a block-form `tickets` entry is a bare `- "GTX-1"` item with no key at
all, a block scalar puts its value on later lines behind an optional anchor or tag, and an
explicit `? key` pair puts the key on its own line. A scan that reads the fence the way the
loader does and then asks the loader for the values is exact against all of them, and reports
the same field name and code point the error would:

```bash
uv run python - <<'PY'
import pathlib
from ruamel.yaml import YAML

yaml = YAML(typ="safe", pure=True)

def refused(text):
    return sorted({f"U+{ord(c):04X}" for c in text
                   if ord(c) < 0x20 or ord(c) == 0x7F or 0x80 <= ord(c) <= 0x9F})

def fields(meta):
    yield from ((k, meta[k]) for k in ("id", "title") if isinstance(meta.get(k), str))
    yield from ((f"tickets[{i}]", t) for i, t in enumerate(meta.get("tickets") or [])
                if isinstance(t, str))
    yield from ((f"derives_from[{i}].{k}", e[k])
                for i, e in enumerate(meta.get("derives_from") or []) if isinstance(e, dict)
                for k in ("ref", "seen") if isinstance(e.get(k), str))

for path in sorted(pathlib.Path(".").rglob("*.md")):
    lines = path.read_text(errors="replace").lstrip("\ufeff").split("\n")
    if not lines or lines[0].strip() != "---":
        continue
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
    try:
        meta = yaml.load("\n".join(lines[1:end]) + "\n") if end else None
    except Exception as exc:
        print(f"{str(path)!r}: frontmatter did not parse: {str(exc).splitlines()[0]}")
        continue
    if isinstance(meta, dict):
        for name, value in fields(meta):
            if found := refused(value):
                print(f"{str(path)!r}: {name}: {' '.join(found)}")
PY
```

Run it where doc-lattice is installed, since it borrows that installation's `ruamel.yaml` and its
pure parser, which is the one the loader itself uses. Everything it prints is a value that will
not load. `doc-lattice check` is the authority after the upgrade, and reports the first document
it finds, one run at a time.

It prints each path quoted, through the same `repr(str(path))` spelling every command uses, for
the reason the `--no-color` guarantee under [Commands](#commands) rests on: a filename is
repo-controlled text too, and a scan that hunts terminal control characters is a poor place to
print one.

A code point it names is a value that will not load. A `block-scalar` line is a prompt to look
rather than a verdict, since whether a break survives depends on the lines beneath it, which is
also why a correct `>-` is listed. `doc-lattice check` is the authority either way: it names the
key and the code point for the first document it finds, one run at a time.

### Files with no `id`

Only a file declaring an `id` joins the lattice. How the rest are treated depends on what they
wrote, because silently dropping a file also silently drops every edge it declares:

| The file | Treatment |
|----------|-----------|
| Opens neither envelope | Untracked prose. Silent; nothing is reported. |
| Fenced, but the block holds no YAML mapping (empty, a scalar, a list) | The same as untracked prose. Silent. |
| Fenced with an id-less mapping declaring none of `derives_from`, `authority`, or `tickets` | Skipped, with a warning on stderr naming the file. The exit status is unchanged. |
| Fenced with an id-less mapping declaring any of `derives_from`, `authority`, or `tickets` | A tool error naming the file and the keys it declared. Exits 2. |
| Line 1 nearly spells `<!-- doc-lattice` (indented, trailing space, different case) | A tool error naming the file. Exits 2, rather than being read as prose, so a typo cannot silently drop the node. |
| Carries `<!-- doc-lattice` below line 1, outside any code block | Not a node, with a `misplaced doc-lattice envelope` warning on stderr naming the file. The exit status is unchanged. |
| Tracked by a `---` fence *and* carries `<!-- doc-lattice` below it, outside any code block | A node under the fence's metadata, with a `shadowed doc-lattice envelope` warning on stderr naming the file. The envelope is body text and its metadata is ignored. The exit status is unchanged. |
| Opens `<!-- doc-lattice`, but the body is not a mapping carrying `id` | A tool error naming the file. Exits 2; the comment envelope has no untracked or id-less tier. |

The first row is about *both* envelopes: a file with no `---` fence can still be a tracked node
when its first line is exactly `<!-- doc-lattice`. The last three rows are the comment envelope's,
and the [frontmatter reference](#frontmatter-reference) states its grammar in full.

Those three keys are the exact set that wires a file into the graph, so an id-less block carrying
one is a node that lost its `id` (to a typo like `idd:`, or to an edit) rather than metadata
belonging to another tool. `title` and `layer` are not in the set: they describe a document
without wiring it into the graph, so a block carrying only those is a warning, not an error.

The warning exists because a docs root can legitimately hold frontmatter this engine does not own,
such as a tool's own `name`/`description` block. To silence it for one file, prefer `ignore_globs`:
it drops the file from discovery, so nothing else a run reports is affected.

It is also a Python warning, so the
[`PYTHONWARNINGS`](https://docs.python.org/3/using/cmdline.html#envvar-PYTHONWARNINGS) filters
apply, but neither available form targets it precisely. `PYTHONWARNINGS=ignore` silences every
warning the run emits. `PYTHONWARNINGS=ignore:skipping` looks narrower and is not: that field is a
literal message prefix rather than a regular expression, and the warning for a document symlinked
outside the project root opens with the same `skipping ` prefix, so filtering on it hides that one
too. The two envelope warnings are the cases the prefix form does target, because each deliberately
opens with its own word rather than `skipping `: `PYTHONWARNINGS=ignore:misplaced` silences the
misplaced-envelope warning and nothing else, and `PYTHONWARNINGS=ignore:shadowed` does the same
for the shadowed-envelope one. Neither silences the other. The report is identical whether the load was accelerated by the
[load cache](#load-cache-opt-in) or not.

An escalating filter is supported the same way. Under `PYTHONWARNINGS=error` (or `-W error`)
Python raises the warning instead of displaying it, which ends the command, and doc-lattice
reports that as `error (WARNING_AS_ERROR): <Category>: <the warning's message>` and exits 2. The
category name leads the line because it is the handle your filter is written against. This holds
for every warning a run can raise, including the ones its dependencies raise rather than
doc-lattice itself. Escalating is therefore a way to make any advisory fatal, but not a way to
make one fatal on its own: the filters select which warnings are raised, and the first one raised
is the one that ends the run.

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
heading text used for slugging.

Not being addressable does not put a heading outside `AMBIGUOUS`. GitHub assigns ids to every
heading form, including the ones above, and allocates them in document order across all of them,
so a setext or nested heading can take an id an addressable heading would otherwise have had, or
shift the duplicate suffix of one that follows it. Collision detection therefore reads the full
GitHub heading inventory, and an `AMBIGUOUS` finding can name a heading that owns no lattice id of
its own. Such a member is still the thing to change: reword it, or reword the addressable heading
it collides with. Adding a `{#anchor}` marker to it does nothing, because the marker is only read
on an addressable heading; adding one to the *addressable* member does fix the edge, by making
that id reword-stable.

Nor does it put a heading outside a descendant's drift context. The ancestor chain folded into a
section's hash is derived by heading level over that same full inventory, so a section nested
under a setext, indented, or otherwise non-addressable parent still carries that parent as
context and still goes STALE when it is reworded. Every ancestor is rendered in one normalized
ATX spelling whatever form it was written in, so rewriting a parent from setext to ATX at the
same level does not restale the sections under it. Such a heading owns no lattice id either way.
Heading and fence recognition is pinned to
`markdown-it-py==4.2.0`; generated slugs and document-order duplicate suffixes target
`github-slugger@2.0.0` under JavaScript Unicode 17.0. Generated lowercase patches and contextual
casing-property tables bridge the minimum supported Python 3.13 Unicode 15.1 table to that target.

## Publishing on GitHub

For a docs repo that renders on GitHub, use the [comment envelope](#frontmatter-reference) and
omit `{#anchor}` markers: the metadata block disappears from the rendered page, and headings get
their ids from the GitHub slug they already have, with no marker syntax to add or maintain. Two
facts about markers matter if you publish anyway.

`{#anchor}` is doc-lattice syntax, not GitHub's: GitHub has no explicit-anchor grammar, so it
renders a marker as literal heading text rather than parsing it. That means the marker also
changes the GitHub-side fragment of the heading it is on: `## Notes {#n}` renders as the heading
text `Notes {#n}`, whose GitHub-assigned id is `notes-n`, not `notes`. A Markdown link to that
heading, `#notes`, is broken on GitHub even though doc-lattice resolves `file#n` correctly, since
the two sides slug different text. Reserve markers for headings you specifically want reword-stable
ids on, and expect a GitHub deep link into a marked heading to carry the marker's own fragment.

## Configuration

doc-lattice runs zero-config (defaulting to a `docs/` root), or reads `.doc-lattice.yml`
from the current directory:

```yaml
# doc-lattice configuration. See https://github.com/Guardantix/doc-lattice
lattice_format: 2
docs_roots:
  - docs
link_sources:
  - docs/**/*.md
# ignore_globs:
#   - "**/archive/**"
# cache_key: my-project-docs   # opt-in load cache slot under your cache home
# cache_trust_stat: true       # opt-in stat fast tier for read-only commands (needs cache_key)
# linear_team: ENG
```

That block is byte-for-byte what `init` writes with no flags, and a test holds the two together,
so what you read here is what you get rather than a paraphrase that drifts. `lattice_format` is
required in any config file this engine reads, and `2` is the only value it currently accepts; a
config file omitting it, or naming another value, is a config error that exits 2 with a pointer to
CHANGELOG's migration section. A zero-config run (no `.doc-lattice.yml` anywhere) is exempt from
that error, since there is no file to declare the key in -- not because there is nothing to skew
against; see [CHANGELOG.md](https://github.com/Guardantix/doc-lattice/blob/main/CHANGELOG.md)'s
7.0.0 migration for what a zero-config upgrade from 6.x sees instead. `init` writes the key for
you into the config it generates, as the leading line of the active block, which is why it is not
commented out above. It writes only that config: run against a directory that already holds one,
`init` leaves the existing file untouched, as it always has, and reports that the config predates
this engine rather than editing it. Adding the key to a config `init` did not write is yours to
do, per CHANGELOG's migration. The
other commented keys are the optional ones:
`ignore_globs` lists paths to skip within the roots, `cache_key` and `cache_trust_stat` are the
opt-in load cache described below, and `linear_team` names the team the `linear` query targets.
Uncomment what you need; `docs_roots` is the only other active key the generated file writes, and
it defaults to `["docs"]` whenever it is unset -- in a config file that omits it just as in
zero-config mode, so a file carrying only `lattice_format: 2` and `linear_team: ENG` is valid.

Configuration is strict in both directions. An unknown key is rejected rather than ignored, and
after YAML parsing each value must already have the schema's exact type, because values are not
coerced. A scalar where a list is required (`docs_roots: docs`) and a key written with no value
at all (`docs_roots:`, which parses to null) are both config errors that exit 2, not quietly
normalized inputs.

The project root is the resolved parent of the selected config file, including an explicit
`--config PATH`, or the resolved current directory in zero-config mode. Relative `docs_roots`
entries are interpreted from that project root. Every root must resolve inside it; an entry that
escapes via `..`, an absolute path, or a symlink is rejected before any read.

Each `docs_roots` entry names either a directory to scan recursively for `.md` files, or a single
`.md` file to track directly. An entry that does not exist is tolerated and contributes no
documents. An entry that exists but is neither a directory nor a regular `.md` file, such as a
non-markdown file, a socket, or a device, fails config load with a config error and exit 2.
`ignore_globs` patterns match a directory entry's files by their path relative to that root; a
direct file entry has no root-relative path, so it is matched by its basename instead.

Discovered document symlinks are resolved separately. A symlink whose target stays inside the
project root is allowed, while one targeting anything outside is skipped with a warning. If
multiple roots or symlink aliases resolve to the same document, it is loaded once under the first
unresolved path discovered. Reconcile re-resolves that identity path before writing so a retargeted
symlink cannot escape the project root.

`link_sources` is the file set `links` gates, and it is independent of `docs_roots`: the lattice
corpus is the files you track, and the link gate may cover files you do not, or fewer. It is a
list of project-relative selectors in a POSIX grammar that reads the same on every platform. `/`
is the only separator and a backslash is a config error; `*`, `?`, and `[...]` classes match
within one segment, case-sensitively, and never cross `/`; `**` is accepted only as a whole
segment and matches zero or more directories. `docs/**/*.md`, `ARCHITECTURE.md`, and `*.md` are
all selectors. Expansion never enters a symlinked directory, whether `**` reaches it or a segment
names it, and a symlinked file is selected by its spelling and judged for containment afterward.
Matches are unioned across selectors, sorted by their project-relative spelling, and deduplicated
by resolved target, so YAML order and overlapping selectors cannot change the output.

The key fails closed. It has no default and is not derived from `docs_roots`; `ignore_globs` does
not apply to it, since that key is anchored to each docs root and a selector already says what it
wants. With the key omitted or empty, or with any selector that matches no file, `links` exits 2
rather than reporting a clean run over nothing. The generated config writes a selector per docs
root, `docs/**/*.md` for the default, which therefore fails closed until that directory holds at
least one Markdown file.

For 2.0, `binding_layers` is unsupported. Delete it from 1.x configs; there is no replacement.
`lint`'s fixed binding > derived > exploratory authority ladder is unchanged.

### Load cache (opt-in)

Large doc sets (thousands of files) can skip re-parsing unchanged docs with an opt-in cache.
Set `cache_key` to a single safe segment (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`); it names a slot
under your user cache home at `<cache_home>/doc-lattice/<cache_key>/load-cache.json`, where
`<cache_home>` is `$XDG_CACHE_HOME` (when absolute) or `~/.cache`. The cache lives outside every
checkout on purpose: because `.doc-lattice.yml` is committed, every clone and git worktree of the
project shares one warm cache with no per-checkout setup, which an in-repo cache could not do.

By default the cache re-reads and re-hashes each file's bytes every run, so its output is always
byte-identical to an uncached run under any cache state (cold, warm, stale, structurally corrupt, or
wrong version); only timing differs. That covers stderr as well as stdout: an entry records why a
file is not a node, not just that it is not one, so the skip warning above reproduces on a warm run
that never re-reads the file. Because two checkouts can share a slot, an entry stores the reason
rather than the rendered sentence, and each run names the path it discovered. A structurally
corrupt cache (unreadable, non-JSON, wrong version, or schema-invalid) is discarded wholesale and
rebuilt; the cache is a trusted single-writer file under your own cache home, so it is not hardened
against hand-edited tampering that stays schema-valid. Setting `cache_trust_stat: true` adds a
faster tier for read-only commands that trusts a file whose size and modification time are
unchanged, accepting that the file is not opened at all: a rewrite that preserves both its size
and its nanosecond mtime is served stale, and a file made unreadable (for example a permissions
change, which does not alter size or mtime) is served from cache instead of erroring, each until
the file is touched. `reconcile` ignores `cache_trust_stat`
and always verifies content, so it can never write frontmatter from stale data.
`cache_trust_stat: true` requires `cache_key`; otherwise config loading is a tool error and exits 2.
Two projects sharing a `cache_key` stay correct (a content-hash
hit implies identical bytes); the only cost is overwrite churn, so prefer distinct keys. Delete the
cache directory to reset it; a tool-version bump discards it automatically.

Any cache read failure, including an unreadable, invalid, or stale cache file, silently falls back
to rebuilding from documents. A cache write failure emits one stderr diagnostic and is otherwise
ignored: it does not change command results or exit codes.

## Adopting doc-lattice in your docs repo

Both setups below share one rule. For an initial adoption with no established baseline, annotate
your documents, then run `doc-lattice reconcile --all` once before
[enabling the gates](#enabling-the-gates):

```bash
uvx --python 3.13 --from doc-lattice==7.0.0 doc-lattice reconcile --all
```

Commit the annotated input state and start from an otherwise clean working tree before running
this command, so its reconcile-only diff can be reviewed and reverted with `git`. That undo is
separate from reconcile's own failure rollback, which restores an interrupted run but does not
reverse a successful baseline you later decide was wrong; see
[RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md) for what that
rollback does cover.

This acknowledges the current state of every STALE and UNRECONCILED edge, so the gates start
from a known baseline instead of reporting the whole backlog on their first run. Do this only
on a first adoption: `init` is rerunnable against an existing config, and on an established
lattice `reconcile --all` would acknowledge legitimate drift you have not reviewed. It also
does not by itself make CI green, because `reconcile` skips BROKEN edges and those remain
findings.

Run `doc-lattice check` *before* this baseline, not only after it. An `AMBIGUOUS` edge is not
skipped the way a BROKEN one is: `reconcile` refuses the whole run on the first one it meets, so
a single pair of identically-named headings anywhere in the tree makes `reconcile --all` exit 2
and write nothing, including for every unrelated edge, and `--dry-run` fails the same way. `check`
lists them all at once with the colliding headings and their lines. First adoptions are the most
likely to have such a pair, since nothing has forced them to be disambiguated yet. See
[RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md)
for the selector semantics.

### Ordinary offline setup

Bootstrap config and the drift and authority-ladder gates for a repo whose docs you want to
track:

```bash
uvx --python 3.13 --from doc-lattice==7.0.0 doc-lattice init
```

This writes `.doc-lattice.yml` (only if absent) and always prints the reconcile-artifact
`.gitignore` block (see [RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md)),
pre-commit hooks, and a GitHub Actions workflow that run `doc-lattice check` (drift),
`doc-lattice lint` (authority ladder), and `doc-lattice links` (dead links) as your gates.
Paste each where the output says. Pasting the pre-commit block installs no Git hook, so those
three hooks stay inert until you [enable the gates](#enabling-the-gates), which is a separate
step with an ordering constraint on a first adoption. `init` only prints `.gitignore` guidance
and never modifies that file. Pass `--docs-root` (repeatable) or `--linear-team` to bake those
values into the generated config.
The generated gates remain fully offline: they run only `check`, `lint`, and `links` and do not
require or receive `LINEAR_API_KEY`.

Branch resolution reached `init` in 5.0, the same release boundary
[MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md) records for
`--default-branch` and its own recipe. Every supported release resolves the branch, so what
follows is simply what `init` does. A workflow printed by an earlier release hard-wires its
trigger instead, rendering both the `push` and the `pull_request` filter as `branches: [main]`;
[Upgrading](#upgrading) is how you replace one.

Through the two paragraphs after this one, the printed workflow triggers on
one branch that `init` resolves by a fixed precedence: an explicit `--default-branch` wins;
otherwise the local `origin/HEAD` remote-tracking ref is read; otherwise it falls back to `main`.
The run always names the branch it used and where that came from on stderr, for example
`workflow triggers on branch trunk (origin/HEAD)`, so a repository on `master`, `trunk`, or
`develop` does not silently install a workflow that never runs.

Treat the probe as a hint rather than an authority. `origin/HEAD` is cached local state: it is
often absent in fresh or shallow clones, and after an upstream default-branch rename it can still
name the old branch. `init` detects a target that no longer exists and falls back, but a stale
target that is still present locally is indistinguishable from a current one without network
access. Reading the reported source line is how you catch that, and `--default-branch` is how you
fix it. Prefer passing it explicitly whenever you want a reproducible result, such as in an
upgrade you intend to repeat. A missing remote, a missing `git`, or a directory outside a worktree
all fall back quietly; ordinary `init` has no Git requirement, though following what it prints
does. The probe runs `git` only from an absolute path outside the directory being scaffolded, so a
checkout carrying its own `git` falls back rather than running it, as does a `git` reachable only
through a relative `PATH` entry. A branch name that is supplied or
detected but is not a supported literal name is a different case and is rejected with an error:
names are limited to ASCII letters, digits, `.`, `_`, and `-` in `/`-separated parts, because a
GitHub `branches:` filter is a glob pattern rather than a literal and `*`, `?`, `[`, `]`, and `!`
would be matched as patterns. Git's own structural exclusions are rejected too, including `..`
anywhere in the name, a leading or trailing `.` on any part, a `.lock` suffix, and the reserved
name `HEAD`: no branch can carry such a name, so a filter built from one would never match. Only
that exact spelling of `HEAD` is reserved, so `head`, `release/HEAD`, and similar names are
ordinary branch names and are accepted.

Having no Git requirement is a fact about running `init`, not about following its output. `init`
and every lattice-loading command work outside a worktree, and outside one `init` still writes
`.doc-lattice.yml` and prints the recipe in full, because the blocks are rendered rather than read
out of the directory. The one thing any of them takes from local Git state is the workflow's
trigger branch, and outside a worktree the probe finds nothing and falls back exactly as it does
in a checkout with no remote. What the recipe installs is Git and GitHub artifacts throughout:
the `.gitignore` block needs a repository to be ignored in, the workflow needs a GitHub
repository to run in, and [enabling the gates](#enabling-the-gates) needs a clone for
`pre-commit` to write its hook into, so `pre-commit install` exits 1 outside one. Every run
therefore names that precondition on stderr, once, and on a terminal it precedes the blocks; the
blocks themselves stay on stdout, so a redirected run preserves no such ordering. The line is
unconditional and byte-identical under `--print-only`, because the precondition is on the
repository you install the blocks into rather than on the directory the run happened in, and
[`--print-only`](#the-pre-commit-snippet-every-install) deliberately carries no directory
precondition of its own.

`--default-branch` applies only to this printed workflow. The Linear workflow the recipe in
[MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md) publishes is
pinned to the exact `main` branch as a security control, so nothing there takes a branch either.

To test an unreleased commit, replace the PyPI requirement with a Git source such as
`--from git+https://github.com/Guardantix/doc-lattice@<commit>`; released configurations should
keep the exact PyPI version pin.

### Enabling the gates

Pasting the pre-commit block installs no Git hook. It adds three hook definitions to
`.pre-commit-config.yaml`, and nothing reads that file on commit until pre-commit has written
`.git/hooks/pre-commit` in your clone. Until it has, the gates exist in CI only, and every
local commit succeeds regardless of drift, including the one that introduces it. Enabling them is
a separate, explicit act, and nothing earlier in this setup path performs it or provides the
`pre-commit` runner it needs, because the path requires only `uv`:

```bash
uv tool install pre-commit
uv tool run pre-commit install
```

`uv tool install` puts the runner in a persistent environment. `uv tool run` then invokes it
without needing uv's tool bin directory on `PATH`, which in a fresh shell it often is not; uv
warns when that applies. If the machine already has a durable `pre-commit` from another installer,
plain `pre-commit install` does the same job, and there is no reason to force a uv tool
installation over it.

Prefer that pair over `uvx pre-commit install`. Pre-commit records the absolute path of the
interpreter that installed the hook and runs it first, falling back to a `pre-commit` on `PATH`
only when that path is gone. Installed through `uvx`, the recorded interpreter lives in uv's
disposable cache, which uv is free to reclaim. On a machine whose only Python tooling is `uv`
there is then no fallback either, and the hook fails closed: it exits 1 with a `pre-commit` not
found error and blocks every commit until it is reinstalled. The hook records a path rather than
resolving one each time, so re-run the install command after anything that moves or rebuilds the
environment behind it, including renaming a parent directory.

`.git/hooks/` is neither tracked nor cloned, so activation belongs to a clone rather than to the
repository. Committing `.pre-commit-config.yaml` enables nothing for anybody else: each
contributor runs the install command once in their own clone, and again after re-cloning. A fresh
clone of an already-gated repository commits drift locally without complaint, and the offline
workflow is what catches it on the pull request. Such a clone is the established case below, so it
activates immediately and has no ordering to observe.

**On an initial adoption, enable them after the reconcile baseline**, not while pasting the
blocks. `check` exits 1 on unreconciled edges as well as stale ones, and an initial adoption
commits exactly that state, because the baseline above has you commit the annotated input before
`reconcile --all` acknowledges it. Gates enabled during setup therefore refuse the very commit the
baseline depends on. The order is:

1. Paste the three blocks and commit them with the annotated input. The gates are inert, so this
   commit is not gated.
2. Run `doc-lattice check` and fix every `AMBIGUOUS` edge it reports, by rewording one of the
   colliding headings or giving the target an explicit `{#anchor}` marker. `reconcile` refuses
   its entire run while any edge is ambiguous, so skipping this step makes step 3 exit 2 without
   writing anything.
3. Run `doc-lattice reconcile --all`.
4. Run `check`, `lint`, and `links`, and resolve whatever they still report. The baseline clears
   STALE and UNRECONCILED edges and nothing else: `reconcile` skips BROKEN edges by design,
   `check` exits 1 on them all the same, and neither command touches a lint finding or a dead
   link. Anything left standing here is what the gates refuse in step 6.
5. Enable the gates with the two commands above.
6. Stage and commit the reconcile-only diff. All three hooks run on it and pass, which is also
   how you confirm activation worked.

**An established installation enables them immediately.** A conversion, or any repository that
already has a baseline, skips the reconcile step and with it the ordering constraint, so run the
two commands as soon as you find the gates off.

Confirm activation with any commit. The `links` hook carries `always_run: true`, because the break
it catches is cross-document and the file that changed is not the file that ends up wrong, so it
runs on every commit and reports itself as passed or failed either way. The `check` and `lint`
entries carry `files: \.md$`, so a commit staging no Markdown file reports those two as
`Skipped`; that is still a working gate, and the `links` line beside them is the proof.

The hook entries run `uvx --python 3.13 --from doc-lattice==7.0.0`, so the pinned release has to
resolve on every gated commit, out of uv's cache once it is warm and from PyPI when it is not.
These gates are offline in the sense that matters for secrets, meaning they never require or
receive `LINEAR_API_KEY`. That is not the same as running without a network.

### Protected Linear setup in CI

To run `linear` in CI without exposing its API key to every workflow in the repository, install
the hand-installable recipe in
[MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md). You add two
workflows you own, plus a GitHub environment whose deployment allow list is exactly `main` and
which holds one dedicated secret. That secret is mapped onto `LINEAR_API_KEY` only on the final
step of the trusted job. The environment is the boundary; the workflows are ordinary files in your
repository.

The recipe replaced a managed setup that generated and maintained four committed artifacts for
the same boundary, removed in 5.0. The same document carries the procedure for converting an
installation left over from it, which changes no remote state.

See [MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md) for the
recipe, what it does without relative to the managed setup, requirements, conversion, and the
security model.

## Upgrading

Upgrades are hand-applied: nothing in your repository updates itself. `init` prints its blocks
from the version of doc-lattice actually running, so every command below has to name the release
you are moving to, and an old binary prints the old blocks. Substitute the target release for
`NEW_VERSION` throughout. Read that release's section in
[CHANGELOG.md](https://github.com/Guardantix/doc-lattice/blob/main/CHANGELOG.md) first: a release
that changes generated output carries a `### Migration` subsection with the steps specific to it.

Which paths apply depends on how you installed. The pre-commit snippet is yours either way, the
ordinary workflow is yours, and a recipe installation's Linear workflow is yours too. A managed
installation left over from 4.1.0 converts rather than upgrades; see
[Managed installs](#managed-installs).

### The pre-commit snippet (every install)

The pre-commit block is printed guidance rather than a generated file, so no command updates
your `.pre-commit-config.yaml` for you. Print the block from the target release and compare it
against the one you checked in:

```bash
uvx --python 3.13 --from doc-lattice==NEW_VERSION doc-lattice init --print-only
```

Replace your whole block with the printed one instead of hand-editing the pinned version in its
three `entry:` lines. The block carries generated structure beyond those three commands, so
bumping only the pins silently keeps an outdated hook shape.

Replacing the block does not need reactivation, because the installed hook reads
`.pre-commit-config.yaml` on every commit rather than baking it in. An installation that never
activated in the first place still has to; see [Enabling the gates](#enabling-the-gates).

`--print-only` writes nothing at all, so this retrieval carries no directory precondition: run it
from anywhere in your checkout, including a subdirectory. Dropping the flag turns the same command
back into scaffolding, which does have one. Plain `init` resolves `.doc-lattice.yml` against the
current directory rather than the Git root, so run it from the directory that holds your existing
config, normally the repository root. From there the run is safe against an existing install:
`init` writes `.doc-lattice.yml` only when it is absent, and otherwise reports that the config
already exists, leaves it untouched, and prints. From a subdirectory it refuses with exit 2 rather
than scaffolding a nested config; nothing is written either way, and the diagnostic names the
directory to run in.

### Ordinary installs

The same `init --print-only` run also prints the GitHub Actions workflow. Diff it against your
checked-in `.github/workflows/doc-lattice.yml`, replace the file with the printed version, and
commit it together with the refreshed pre-commit block.

The printed workflow's trigger branch is resolved per run, so pass `--default-branch` with the
branch you actually gate on to make the upgrade reproducible rather than dependent on the local
`origin/HEAD` of whichever checkout you ran it in. Either way, check the reported branch on stderr
against your workflow before committing the replacement.

### Recipe installs

Take the pre-commit block and the ordinary workflow from the `init --print-only` run above, passing
`--default-branch main` as the recipe does at install time, since a recipe installation is pinned
to `main` throughout and an omitted flag resolves the trigger against whatever `origin/HEAD` the
checkout you ran it in has cached. Then replace your Linear workflow whole from the target
release's
[MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md) step 2. Do not
bump only its `doc-lattice==` pin: its structure and action pins can change between releases
independently of the version it installs, which is the same reason the ordinary workflow is
replaced whole.

### Managed installs

A managed installation is one generated by the removed `init --github` before 5.0. It has no
upgrade path, because its offline workflow invokes `ci audit`, a command 5.0 removed, so pinning
it forward fails outright. It does not break on its own either: its workflows pin the exact
release that generated them and go on running it, which is why nothing prompts the conversion.

Convert it instead. The procedure is in
[MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md), it changes no
remote state, and it is a local change of file ownership from the tool to you. Once converted,
follow [Recipe installs](#recipe-installs) above for every later upgrade.

## Linear integration

`doc-lattice linear` is the only network-touching command. It builds a trigger map from the
loaded lattice, then fetches live ticket status over the Linear GraphQL API to report tickets
that shipped against a spec that has since drifted. It reads `LINEAR_API_KEY` from the
environment (export it before running; the error points you to `impact` for the offline view),
and the client is https-only, redirect-refusing, size-capped, and SSRF-hardened. A transient
HTTP 429 or 5xx gets two retries, for three total attempts. Without a usable `Retry-After`, retries
wait 1 second and then 2 seconds. A non-negative integer `Retry-After` is honored up to the
30-second cap; negative, date-form, and invalid values use the fallback delay.

> **Security note:** If `linear` is used in CI, use the
> [protected GitHub setup](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md).
> The command processes
> repository-controlled `tickets` and `linear_team` while `LINEAR_API_KEY` is present. Untrusted
> pull-request workflows should use only offline commands.

Canonical ticket ids are uppercase ASCII `TEAM-NUMBER`: `TEAM` starts with an uppercase letter
and continues with uppercase letters or digits, while `NUMBER` is `0` or a decimal with no leading
zeros. One `linear` run accepts at most 500 distinct ticket refs after its positional or `--from`
scope is applied. Set the team the query targets with `linear_team` in `.doc-lattice.yml`, or pass
`--linear-team` to `init`. Every other command runs fully offline.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success; no coherent policy or gate finding. |
| `1` | Coherent finding: lattice drift, an authority inversion, a dead link or fragment, or a Linear gate failure. |
| `2` | Invalid, unreadable, unsafe, ambiguous, or unreliable tool state, including confirmation refusal, persistence or recovery failure, and an advisory a warning filter escalated to an error. |
| `141` | Standard output could not be written because its reader departed. Nothing is printed. Only stdout produces this; a dead stderr leaves the code the run had otherwise earned. |

### Error codes

The exit status says how a run ended; an error code says which contract failed. Every code below
belongs to a typed error and is printed ahead of the message, as `error (CODE): ...` on stderr.

Carrying a code is not the same as exiting 2, and an uncoded run is not one shape. A usage check a
command writes itself, such as `--indent` without `--format json`, and the `reconcile --recover`
problem report print `error: ...`; an unexpected failure prints `internal error: ...`. A usage
failure the parser rejects before any command runs, such as an unknown option or a missing
argument, never reaches that renderer at all: Typer prints its own `Usage:` line and a boxed
`Error` instead, under `--no-color` as well. Invoking `doc-lattice` with no arguments is the one
exit 2 with no diagnostic anywhere; it prints help. So a caller cannot classify every exit-2 run by
matching `error`, and matching a coded one is the only case this document underwrites. The
parenthetical marks exactly the diagnostics that have a code to match on. Match on the code rather
than on message text: messages are diagnostics and are free to change, while this domain is the
documented migration surface.

| Code | Raised when |
|------|-------------|
| `CONFIG_ERROR` | An explicit `--config PATH` names a file that does not exist, or the selected `.doc-lattice.yml` is unreadable, fails to parse as YAML, fails its schema, or names a `docs_roots` entry that escapes the project root or exists as something other than a directory or a regular `.md` file. A `linear_team` *in that file* that is not a valid team key lands here too; the same value passed to `init --linear-team` does not, because `init` writes a config and never reads one. An absent default config is not an error; it is zero-config mode. |
| `VALIDATION_ERROR` | A value parsed cleanly but failed domain validation: an impact token that resolves to no id (from `impact` or from `linear`), a `reconcile` node id that names no node, a `reconcile --ref` matching no edge on the node it named, or any input `init` checks before it writes anything (enumerated below). Command-shape and parser usage failures are *not* this; they stay uncoded. |
| `DUPLICATE_ID` | Two files claim the same `id`, or two headings within one file resolve to the same anchor id. The error names both registration sites. |
| `BROKEN_REF` | An operation that requires a resolved edge was aimed at one that does not resolve, in practice a single-node `reconcile` whose `--ref` names the broken edge. This is *not* the ordinary unresolved ref: that is the coherent `BROKEN` finding `check` reports with exit 1, and a broad `reconcile` skips it rather than failing. |
| `UNREADABLE_DOC` | A discovered document cannot be read or decoded as UTF-8, it opens a `---` frontmatter fence it never closes, or its frontmatter YAML cannot be parsed. A reconcile also raises it when the structure it must rewrite is malformed, and when it refuses a rewrite it cannot verify, having found the result unparseable, self-referential, or not a faithful reproduction. |
| `FRONTMATTER_ERROR` | Tracked lattice frontmatter failed schema validation: an unknown key, a wrong type, a control character in a text value, or a correctly typed value outside its own domain, such as an `id` containing `#` or a `layer` or `authority` that is not one of its supported words. It also covers an id-less block that declared `authority`, `derives_from`, or `tickets` and so named no owner for the edges it declares. |
| `LINEAR_ERROR` | The `linear` command could not obtain a usable response: a missing or rejected `LINEAR_API_KEY`, a transport failure, any HTTP error status (429 and 5xx after the retry budget is exhausted, every other status refused on the first attempt), a refused redirect, GraphQL errors, a missing, oversized, or malformed payload, or more distinct ticket refs than one run accepts. |
| `RECONCILE_IN_PROGRESS` | A reconcile could not establish or keep an exclusive claim on the project root: either another process already holds the lock, so the run refuses rather than writing alongside it, or the directory the run locked is no longer the one at that path, because the project root was renamed, replaced, or removed while the run held it. Every mutating step revalidates the claim first, so that second case refuses before writing anything. |
| `RECONCILE_CONFLICT` | A reconcile destination's bytes changed between validation and the write, so the transaction was refused and rolled back rather than applied over an edit it never read. |
| `RECONCILE_PERSISTENCE` | A reconcile transaction could not be durably written, its rollback could not be completed, or `--recover` could not safely finish an interrupted one. It also covers lock setup failing and a platform with no POSIX advisory locking. Wherever there is a transaction to recover, the message names the recovery step to run; the rest describe the condition without one. |
| `INIT_PERSISTENCE` | `init` could not write `.doc-lattice.yml` into the working directory, or could not read an entry it has to check before writing one. |
| `WARNING_AS_ERROR` | A warning filter (`PYTHONWARNINGS=error`, or `-W error`) turned an advisory into an exception, ending a run that would otherwise have continued past it. |

The `init` inputs checked before anything is written, all of them `VALIDATION_ERROR`, are a
`--docs-root` or `--linear-team` that is empty or carries a control character, a `--docs-root`
that is absolute or contains `..`, a `--linear-team` that is not a Linear team key, a
`--default-branch` outside the supported branch-name domain, including a name the local
`origin/HEAD` probe discovered rather than one you typed, and the invocation directory itself when
it holds no config but an ancestor inside the same repository does. That last one is the directory
read as an input rather than a value you typed, which is why it reports the same code as the
others: it is checked in the same place, before anything is written.

`--print-only` combined with `--docs-root` or `--linear-team` is *not* in that list. It is a
command-shape failure, so like `reconcile --recover` with a selector it prints an uncoded
`error: ...` and exits 2 with no code to match on.

`UNKNOWN` is deliberately absent. It is the base default that every typed error overrides, and no
production path raises an error carrying it, so it is not a diagnostic you can receive.

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

**`frontmatter in ... declares 'derives_from' but has no 'id' key` exits 2.** The file wired itself
into the graph but never named itself, so it and every edge it declares would leave the lattice
together. Almost always a typo in the `id` key (`idd:`, `Id:`) or an edit that dropped it: add the
`id` back, or remove the lattice keys if the file is not meant to be tracked. The same message
lists `authority` and `tickets` when those are what the block declared.

**`must not contain a control character; found U+000A at index ...` exits 2.** A frontmatter
value decoded to a byte a terminal acts on, so the document is refused rather than loaded and
printed. The fix depends on which code point the message names, and the three common ones do not
share one. `U+000A` is a line break a block scalar kept, and chomping removes it only when it is
*trailing*: change `|` to `|-`, or `>` to `>-`. A break *between* the lines of a value survives
that edit, so if the error repeats, fold the value with `>-` and keep its lines adjacent, or put
it on one line. `U+0009` is a tab, which no chomping indicator touches: it is a literal tab or a
`\t` escape in the value, and it has to come out. `U+000D` reaches a value only as a `\r`
escape, since YAML folds a literal carriage return to a space, so it too has to come out rather
than be re-chomped. Anything in the `U+001B` neighborhood is an escape sequence written into the
value on purpose and should come out as well. The message names the key and the offending code
point rather than repeating the value, so reading it never puts the byte back on your terminal.
See the [Frontmatter reference](#frontmatter-reference) for the exact accepted set and for a scan
that finds every spelling that reaches this rule, including the ones nothing on screen shows you.

**`skipping ... its frontmatter declares no 'id'` on stderr.** Not an error: a file with fenced
frontmatter that declares no `id` and no lattice keys is left out of the lattice, and the exit
status is unchanged. Expected when a docs root holds frontmatter belonging to another tool. Exclude
the file with `ignore_globs` to silence it precisely; see
[Files with no `id`](#files-with-no-id) for why the `PYTHONWARNINGS` alternatives are blunter than
they look.

**`error (WARNING_AS_ERROR): ...` exits 2.** A warning filter escalated an advisory into an
exception, so the command stopped where it would otherwise have continued. Something in the
environment is setting `PYTHONWARNINGS=error` or passing `-W error`; the run is reporting the
first warning that filter raised, and the category the line names is what the filter matched. Drop
the escalation, or narrow it so this category is not caught, and the run continues past the
advisory as before. A dependency's own category can appear here as well as doc-lattice's
`UserWarning`, because the escalation applies to every warning a run raises.

**`duplicate id ...` exits 2.** A duplicate id makes the index incoherent, so loading the lattice
fails with exit 2 (a tool error, distinct from the exit 1 that `check` and `lint` use for drift).
The message names both registration sites so you can find the clash: either two files share an
`id`, or two headings in one file resolve to the same anchor through equal markers or a marker/slug
collision. Equal anchors in different files do not collide.

## Documentation

| Document | Purpose |
|----------|---------|
| [ARCHITECTURE.md](https://github.com/Guardantix/doc-lattice/blob/main/ARCHITECTURE.md) | System design and the decision log |
| [MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md) | The protected Linear CI recipe, its security model, and the conversion procedure |
| [RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md) | Reconcile selectors, transaction durability, and recovery |
| [CLAUDE.md](https://github.com/Guardantix/doc-lattice/blob/main/CLAUDE.md) | Short contributor and agent guide |
| [ROADMAP.md](https://github.com/Guardantix/doc-lattice/blob/main/ROADMAP.md) | Future direction |
| [CHANGELOG.md](https://github.com/Guardantix/doc-lattice/blob/main/CHANGELOG.md) | Release history and migrations |
| [RELEASING.md](https://github.com/Guardantix/doc-lattice/blob/main/RELEASING.md) | Release checklist, version-tag procedure, release authority and access, and the bad-release playbook |
| [SECURITY.md](https://github.com/Guardantix/doc-lattice/blob/main/SECURITY.md) | Supported versions, what is in and out of scope, and how to report a vulnerability privately |

## Project structure

```
doc-lattice/
├── src/doc_lattice/         # the engine: a pure graph/report core, the link gate, behind a thin impure shell
│   ├── markdown_compat.py      # pinned heading and GitHub-slug compatibility adapter
│   ├── link_check.py           # the links gate: selector expansion and link resolution (read-only I/O)
│   ├── _github_slugger_data.py # generated slug and Unicode compatibility data
│   ├── persistence.py          # shared durable single-path filesystem primitives
│   ├── reconcile_transaction.py # reconcile lock, journal, commit, rollback, and recovery
│   ├── cli/                    # per-invocation runtime and one adapter per command
│   └── cache/               # phase-separated incremental load cache
│       ├── schema.py        # filesystem-free models and codec
│       ├── state.py         # filesystem-free run-local state
│       ├── lookup.py        # document reads and stats for cache-tier selection
│       └── store.py         # cache-file reads and atomic writes
├── tests/                   # test suite (mirrors sources; property-based hashing invariants)
├── scripts/                 # slug generation, section benchmark, repository and release tools
└── pyproject.toml           # project configuration
```

See [ARCHITECTURE.md](https://github.com/Guardantix/doc-lattice/blob/main/ARCHITECTURE.md) for
module boundaries and their rationale.

## License

MIT. See [LICENSE](https://github.com/Guardantix/doc-lattice/blob/main/LICENSE).
