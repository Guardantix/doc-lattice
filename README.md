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

The hash input never includes frontmatter. A `file` ref hashes the canonicalized document body;
a `section` ref hashes only the canonicalized text of the target section. Because `reconcile`
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
distinct `TargetId(file_id, anchor)` keys and do not collide. A write refused because the pipe's
reader closed (for example, piping into `head`) exits 141, printing nothing.

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
1 edge: 0 OK, 1 STALE, 0 UNRECONCILED, 0 BROKEN

$ doc-lattice impact api-design#pagination
billing-integration-guide  ('/work/acme-api/docs/billing-integration-guide.md')  tickets: ENG-412
```

`check` exits 1, so CI is now red. A human reviews the guide against the new pagination
scheme, updates the body if needed, and then locks in the new hash:

```console
$ doc-lattice reconcile billing-integration-guide
reconciled 'billing-integration-guide.md': api-design#pagination

$ doc-lattice check
1 edge: 1 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN
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
| `check [--only STATE ...] [--format human\|json\|github]` | Classify every `derives_from` edge as OK / STALE / UNRECONCILED / BROKEN. | 1 on drift, 2 on tool error |
| `lint [--format human\|json\|github]` | Validate the authority ladder (binding > derived > exploratory) over the edges. | 1 on a violation, 2 on tool error |
| `impact TOKEN [--depth N] [--format human\|json]` | List every downstream doc affected by a change to TOKEN; `--depth N` bounds the walk to N hops. | 2 on tool error |
| `reconcile [ID] [--ref REF] [--all] [--dry-run] [--recover] [--format human\|json]` | Durably set `seen` for selected edges as one transaction, preview read-only with `--dry-run`, or recover an interrupted transaction with `--recover`. | 2 on tool error, conflict, lock contention, or persistence/recovery failure |
| `graph [--format mermaid\|dot\|json]` | Emit the edge graph as Mermaid, DOT, or JSON. | 2 on tool error (including an unrecognized `--format`) |
| `linear [TARGET] [--from ID] [--exit-code] [--warn-exit] [--format human\|json]` | Report tickets shipped against a spec that has since drifted (needs `LINEAR_API_KEY`). | 1 with `--exit-code` on DANGER/BLOCKED (or WARNING too under `--warn-exit`), 2 on tool error |
| `init [--docs-root ...] [--linear-team KEY] [--default-branch NAME]` | Scaffold `.doc-lattice.yml` and print the `.gitignore`, pre-commit, and GitHub Actions blocks to install by hand. | 2 on tool error |

`check` and `lint` gate by default, exiting 1 when they find drift or an authority inversion.
`impact`, `reconcile`, `graph`, and `init` are informational and exit 0 on success, so wiring
`impact` into a CI gate never turns the build red. `linear` also exits 0 by default; pass
`--exit-code` to gate on any DANGER or BLOCKED finding, and add `--warn-exit` to gate on WARNING
as well.

The lattice-loading commands `check`, `lint`, `impact`, `reconcile`, `graph`, and `linear` accept
`--config PATH` (path to `.doc-lattice.yml`; defaults to the file in the current directory).
`init` deliberately does not accept config or load the lattice. It keeps its current-directory
behavior and does not require Git: it reads local Git state only to guess the workflow's default
branch, and falls back when that read finds nothing.
Run `uv run doc-lattice <command> --help` for the full flag list.

Pass `--indent N` with JSON output on `check`, `lint`, `impact`, or `linear` to pretty-print the
JSON with `N` spaces per level. JSON output is selected uniformly by `--format json`; `--indent`
without an effective `--format json` is a usage error.

Use the global `--no-color` option before the command to disable colored output explicitly, for
example `doc-lattice --no-color check`. Rich also honors the [`NO_COLOR`](https://no-color.org/)
environment variable; `--no-color` is the command-line equivalent. doc-lattice intentionally
extends the `NO_COLOR` baseline: the standard itself only asks implementers to drop color and
leaves bold, underline, and italic styling in place, but either lever here means no styling at
all, so no terminal escape sequence reaches the output under either one, even when a
terminal-forcing variable is set. This covers every command's output, not just help and
usage-error text.

`check` and `lint` also accept `--format human|json|github`. `human` is the default. `github`
emits one escaped GitHub Actions `::error` workflow command per drift finding or ladder
violation, each with a repo-relative file path, so findings attach inline to the offending doc
in the pull-request diff. Output selection never changes gate exit codes.

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
them down per state, for example `101 edges: 96 OK, 5 STALE, 0 UNRECONCILED, 0 BROKEN`. Every
state is listed, including the ones with a zero count, so truncated output such as `check | tail`
still states the result rather than trailing off into whichever edges sort last. The line is one
record on one line at any terminal width, so `check | tail -1` always gets the whole verdict.

The line is present on a clean lattice too, where it is the entire output, and a lattice with no
edges at all reports `0 edges: 0 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN`. Because the state names
are always printed, match on the exit code rather than grepping human output for a state name.

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

`id`, `title`, each `tickets` entry, `derives_from[].ref`, and `derives_from[].seen` must be
single-line text carrying no control character. A value that decodes to any C0 code point
(`U+0000` to `U+001F`, tab, newline, and carriage return included), DEL (`U+007F`), or any C1
code point (`U+0080` to `U+009F`) fails the load with a `FRONTMATTER_ERROR` naming the key and
the code point. Every other character is accepted, accented text, CJK, emoji, and a no-break
space among them. The rule is what carries the `--no-color` promise above into a document's own
text, as the quoted spelling of a path carries it into a filename: a value reaches your terminal,
so it cannot carry bytes your terminal acts on. An unknown key that decodes to one is spelled in
the error rejecting it for the same reason. What the rule is scoped to is a block that loads: a
block that fails to parse at all is reported through the YAML parser's own message, which quotes
the source line it choked on, and that path does not yet hold the promise.

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
        print(f"{path}: frontmatter did not parse: {str(exc).splitlines()[0]}")
        continue
    if isinstance(meta, dict):
        for name, value in fields(meta):
            if found := refused(value):
                print(f"{path}: {name}: {' '.join(found)}")
PY
```

Run it where doc-lattice is installed, since it borrows that installation's `ruamel.yaml` and its
pure parser, which is the one the loader itself uses. Everything it prints is a value that will
not load. `doc-lattice check` is the authority after the upgrade, and reports the first document
it finds, one run at a time.

A code point it names is a value that will not load. A `block-scalar` line is a prompt to look
rather than a verdict, since whether a break survives depends on the lines beneath it, which is
also why a correct `>-` is listed. `doc-lattice check` is the authority either way: it names the
key and the code point for the first document it finds, one run at a time.

### Files with no `id`

Only a file declaring an `id` joins the lattice. How the rest are treated depends on what they
wrote, because silently dropping a file also silently drops every edge it declares:

| The file | Treatment |
|----------|-----------|
| No opening `---` fence | Untracked prose. Silent; nothing is reported. |
| Fenced, but the block holds no YAML mapping (empty, a scalar, a list) | The same as untracked prose. Silent. |
| Fenced with an id-less mapping declaring none of `derives_from`, `authority`, or `tickets` | Skipped, with a warning on stderr naming the file. The exit status is unchanged. |
| Fenced with an id-less mapping declaring any of `derives_from`, `authority`, or `tickets` | A tool error naming the file and the keys it declared. Exits 2. |

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
too. The report is identical whether the load was accelerated by the
[load cache](#load-cache-opt-in) or not.

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
  - docs                  # directories to scan, or individual .md files (default: ["docs"])
# ignore_globs:           # paths to skip within those roots
#   - "**/archive/**"
# cache_key: my-docs      # opt-in incremental load cache slot (see Load cache below)
# cache_trust_stat: false # opt-in stat fast tier for read-only commands (accepts the mtime caveat)
# linear_team: ENG        # the Linear team the `linear` query targets
```

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
uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice reconcile --all
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
findings. See [RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md)
for the selector semantics.

### Ordinary offline setup

Bootstrap config and the drift and authority-ladder gates for a repo whose docs you want to
track:

```bash
uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice init
```

This writes `.doc-lattice.yml` (only if absent) and always prints the reconcile-artifact
`.gitignore` block (see [RECONCILE.md](https://github.com/Guardantix/doc-lattice/blob/main/RECONCILE.md)),
pre-commit hooks, and a GitHub Actions workflow that run `doc-lattice check` (drift) and
`doc-lattice lint` (authority ladder) as your gates. Paste each
where the output says. Pasting the pre-commit block installs no Git hook, so those two hooks stay
inert until you [enable the gates](#enabling-the-gates), which is a separate step with an ordering
constraint on a first adoption. `init` only prints `.gitignore` guidance and never modifies that
file. Pass
`--docs-root` (repeatable) or `--linear-team` to bake those values into the generated config.
The generated gates remain fully offline: they run only `check` and `lint` and do not require or
receive `LINEAR_API_KEY`.

Branch resolution reaches `init` in 5.0, the same release boundary
[MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md) records for
`--default-branch` and its own recipe. Before that release the printed workflow hard-wires its
trigger: both the `push` and the `pull_request` filter render as `branches: [main]`, `init` never
inspects `origin/HEAD`, the run names no branch on stderr, and passing `--default-branch` exits 2
with `No such option: --default-branch`. On any such release, compare both filters against the
branch you actually gate on before saving the file and edit both when it is not `main`. That hand
edit is the only correction available there, and a repository on `master`, `trunk`, or `develop`
that skips it installs a workflow that never runs.

Starting in 5.0, and through the two paragraphs after this one, the printed workflow triggers on
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
all fall back quietly; ordinary `init` has no Git requirement. The probe runs `git` only from an
absolute path outside the directory being scaffolded, so a checkout carrying its own `git` falls
back rather than running it, as does a `git` reachable only through a relative `PATH` entry. A branch name that is supplied or
detected but is not a supported literal name is a different case and is rejected with an error:
names are limited to ASCII letters, digits, `.`, `_`, and `-` in `/`-separated parts, because a
GitHub `branches:` filter is a glob pattern rather than a literal and `*`, `?`, `[`, `]`, and `!`
would be matched as patterns. Git's own structural exclusions are rejected too, including `..`
anywhere in the name, a leading or trailing `.` on any part, a `.lock` suffix, and the reserved
name `HEAD`: no branch can carry such a name, so a filter built from one would never match. Only
that exact spelling of `HEAD` is reserved, so `head`, `release/HEAD`, and similar names are
ordinary branch names and are accepted.

`--default-branch` applies only to this printed workflow. The Linear workflow the recipe in
[MANAGED_CI.md](https://github.com/Guardantix/doc-lattice/blob/main/MANAGED_CI.md) publishes is
pinned to the exact `main` branch as a security control, so nothing there takes a branch either.

To test an unreleased commit, replace the PyPI requirement with a Git source such as
`--from git+https://github.com/Guardantix/doc-lattice@<commit>`; released configurations should
keep the exact PyPI version pin.

### Enabling the gates

Pasting the pre-commit block installs no Git hook. It adds two hook definitions to
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
2. Run `doc-lattice reconcile --all`.
3. Run `check` and `lint`, and resolve whatever they still report. The baseline clears STALE and
   UNRECONCILED edges and nothing else: `reconcile` skips BROKEN edges by design, `check` exits 1
   on them all the same, and neither command touches a lint finding. Anything left standing here
   is what the gate refuses in step 5.
4. Enable the gates with the two commands above.
5. Stage and commit the reconcile-only diff. Both hooks run on it and pass, which is also how you
   confirm activation worked.

**An established installation enables them immediately.** A conversion, or any repository that
already has a baseline, skips the reconcile step and with it the ordering constraint, so run the
two commands as soon as you find the gates off.

Confirm activation with a commit that stages at least one Markdown file. Both generated hook
entries carry `files: \.md$`, so an empty or non-Markdown commit runs pre-commit and reports both
doc-lattice hooks as `Skipped`. That looks like a working gate and proves nothing about it.

The hook entries run `uvx --python 3.13 --from doc-lattice==4.1.0`, so the pinned release has to
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
uvx --python 3.13 --from doc-lattice==NEW_VERSION doc-lattice init
```

Replace your whole block with the printed one instead of hand-editing the pinned version in its
two `entry:` lines. The block carries generated structure beyond those two commands, so bumping
only the pins silently keeps an outdated hook shape.

Replacing the block does not need reactivation, because the installed hook reads
`.pre-commit-config.yaml` on every commit rather than baking it in. An installation that never
activated in the first place still has to; see [Enabling the gates](#enabling-the-gates).

Run this from the directory that holds your existing `.doc-lattice.yml`, normally the repository
root. Plain `init` resolves that file against the current directory rather than the Git root, so
a run from a subdirectory does not see your config and scaffolds a second, nested one carrying
default settings. From the right directory the run is safe against an existing install: `init`
writes `.doc-lattice.yml` only when it is absent, and otherwise reports that the config already
exists, leaves it untouched, and prints.

### Ordinary installs

The same `init` run also prints the GitHub Actions workflow. Diff it against your checked-in
`.github/workflows/doc-lattice.yml`, replace the file with the printed version, and commit it
together with the refreshed pre-commit block.

The printed workflow's trigger branch is resolved per run, so pass `--default-branch` with the
branch you actually gate on to make the upgrade reproducible rather than dependent on the local
`origin/HEAD` of whichever checkout you ran it in. Either way, check the reported branch on stderr
against your workflow before committing the replacement.

### Recipe installs

Take the pre-commit block and the ordinary workflow from the plain `init` run above, passing
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
| `1` | Coherent finding: lattice drift, an authority inversion, or a Linear gate failure. |
| `2` | Invalid, unreadable, unsafe, ambiguous, or unreliable tool state, including confirmation refusal and persistence or recovery failure. |

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
├── src/doc_lattice/         # the engine: a pure graph/report core behind a thin impure shell
│   ├── markdown_compat.py      # pinned heading and GitHub-slug compatibility adapter
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
