# doc-lattice

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
| `impact TOKEN [--depth N] [--format human\|json]` | List every downstream doc affected by a change to TOKEN; `--depth N` bounds the walk to N hops. | 2 on tool error |
| `reconcile [ID] [--ref REF] [--all] [--dry-run] [--recover] [--format human\|json]` | Durably set `seen` for selected edges as one transaction, preview read-only with `--dry-run`, or recover an interrupted transaction with `--recover`. | 2 on tool error, conflict, lock contention, or persistence/recovery failure |
| `graph [--format mermaid\|dot\|json]` | Emit the edge graph as Mermaid, DOT, or JSON. | 2 on tool error (including an unrecognized `--format`) |
| `linear [TARGET] [--from ID] [--exit-code] [--warn-exit] [--format human\|json]` | Report tickets shipped against a spec that has since drifted (needs `LINEAR_API_KEY`). | 1 with `--exit-code` on DANGER/BLOCKED (or WARNING too under `--warn-exit`), 2 on tool error |
| `init [--docs-root ...] [--linear-team KEY] [--github --repository OWNER/REPO]` | Scaffold `.doc-lattice.yml`; with explicit GitHub mode, create the four managed GitHub artifacts at the Git top-level. | 2 on tool error or unsafe existing artifact |
| `ci audit [--repository OWNER/REPO]` | Audit repository-global workflow prohibitions and the managed GitHub installation without loading the lattice or using the network. | 1 on findings, 2 on unreadable or ambiguous state |
| `ci refresh --repository OWNER/REPO [--apply]` | Preview a managed artifact upgrade or rename, then optionally apply it after exact interactive confirmation. | 1 when a preview has updates, 2 on refusal, unsafe state, or tool error |

`check` and `lint` gate by default, exiting 1 when they find drift or an authority inversion.
`ci audit` uses the same finding code for a coherent policy violation, and a read-only `ci refresh`
preview uses it when an update is available. `impact`, `reconcile`, `graph`, and ordinary `init`
are informational and exit 0 on success, so wiring `impact` into a CI gate never turns the build
red. `linear` also exits 0 by default; pass `--exit-code` to gate on any DANGER or BLOCKED finding,
and add `--warn-exit` to gate on WARNING as well.

The lattice-loading commands `check`, `lint`, `impact`, `reconcile`, `graph`, and `linear` accept
`--config PATH` (path to `.doc-lattice.yml`; defaults to the file in the current directory).
`init`, `ci audit`, and `ci refresh` deliberately do not accept config or load the lattice.
GitHub-mode `init` and both `ci` commands require a Git working tree and resolve its top-level
before inspecting or writing managed files, even when invoked from a subdirectory. Ordinary
`init` retains its current-directory behavior and does not require Git.
`check`, `lint`, `impact`, `reconcile`, and `linear` accept `--format json` for machine-readable
output. Run `uv run doc-lattice <command> --help` for the full flag list.

Pass `--indent N` with JSON output on `check`, `lint`, `impact`, or `linear` to pretty-print the
JSON with `N` spaces per level. JSON output is selected uniformly by `--format json`; `--indent`
without an effective `--format json` is a usage error.

Use the global `--no-color` option before the command to disable colored output explicitly, for
example `doc-lattice --no-color check`. Rich also honors the [`NO_COLOR`](https://no-color.org/)
environment variable; `--no-color` is the command-line equivalent. Either one also strips the
styling from help and usage-error text even when a terminal-forcing variable is set.

`check` and `lint` also accept `--format human|json|github`. `human` is the default. `github`
emits one escaped GitHub Actions `::error` workflow command per drift finding or ladder
violation, each with a repo-relative file path, so findings attach inline to the offending doc
in the pull-request diff. Output selection never changes gate exit codes.

Structured output is always selected with `--format`; only the accepted values vary by command.
`check` and `lint` accept `--format human|json|github`, `graph` accepts `--format
mermaid|dot|json`, `impact`, `reconcile`, and `linear` accept `--format human|json`, and `init`
is deliberately excluded from structured-output selection. Where supported, `--indent` requires an effective `--format json`.
The 1.x silent `--json` alias was removed in 2.0; see [CHANGELOG.md](CHANGELOG.md) for the
migration.

`impact` walks the full transitive closure by default. Pass `--depth N` (N >= 1) to bound the
walk to N hops from TOKEN: `--depth 1` lists only the docs that derive directly from it. Human
output is unchanged, and each JSON entry gains a `"depth"` field carrying the minimum number
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
  `--all`, `--ref`, or `--dry-run`; those combinations exit 2. `--format json` is supported.

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
loading the lattice. Combine a safe dry-run with `--format json` for a machine-readable plan:
`{"dry_run": true, "reconciled": [{"path": ..., "ref": ..., "new_seen": ...}]}`, sorted by path
then ref. A real run with `--format json` emits the same shape with `"dry_run": false`, after the
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
3. For machine-readable recovery, add `--format json`. The complete stdout object contains exactly
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
#   - "**/archive/**"
# cache_key: my-docs      # opt-in incremental load cache slot (see Load cache below)
# cache_trust_stat: false # opt-in stat fast tier for read-only commands (accepts the mtime caveat)
# linear_team: ENG        # the Linear team the `linear` query targets
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

### Ordinary offline setup

Bootstrap config and the drift and authority-ladder gates for a repo whose docs you want to
track:

```bash
uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice init
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

### Managed GitHub and Linear setup

To add protected Linear reporting, a human maintainer generates and reviews four committed,
create-only artifacts:

- `.github/workflows/doc-lattice.yml` runs the offline audit, drift, and authority gates.
- `.github/workflows/doc-lattice-linear.yml` runs the Linear gate only on trusted `main`.
- `.github/doc-lattice-bootstrap.sh` configures and verifies the GitHub environment.
- `.github/.gitattributes` keeps the bootstrap script at LF line endings after checkout.

The bootstrap script is a durable managed artifact, not a disposable installer. Keep it committed
after installation. Bootstrap `verify` checks remote environment policy and secret-name metadata.
`ci audit` checks that the script is present and carries a valid ownership marker, version, and
repository identity, but it does not compare the bootstrap script byte for byte. `ci refresh`
performs the byte-level managed-artifact diff and can recreate a missing script after confirmation.
The scoped attributes file contains `doc-lattice-bootstrap.sh text eol=lf`, so a Windows checkout
with `core.autocrlf=true` does not turn the Git Bash script into unusable CRLF shell syntax. Audit
requires that exact effective rule while accepting either LF or CRLF separators in the attributes
file itself.

The initial script supports GitHub.com repositories whose default branch is exactly `main`. It
requires Bash 3.2 or later and an authenticated GitHub CLI. The authenticated maintainer must be a
repository owner or administrator with authority to manage environments and inspect repository
secret names. Reading organization-plan metadata can require organization-owner or equivalent
`admin:org` authority; unavailable authority fails closed. Run the script on macOS or Linux, or on
Windows through Git Bash or WSL. Native PowerShell is not supported.

Existing adopters need one local preparation before running `init --github`. Earlier ordinary
`init` guidance produced an unmarked `.github/workflows/doc-lattice.yml` when its printed workflow
was installed. In the same reviewed change, inspect that canonical offline target, then remove it
so `init --github` can install the managed replacement, and inspect and remove any old Linear
workflow occupying
`.github/workflows/doc-lattice-linear.yml`. Run `init --github` only after both canonical targets
are absent so the final diff shows the new managed replacements. `ci refresh` cannot adopt an
unmarked file and will fail closed instead of overwriting it.

Canonical target cleanup is only collision handling. Also inventory the repository's workflows and
remove every old hand-written Linear workflow, regardless of path or filename, in the same reviewed
migration change. Do not rely on `ci audit` to discover all legacy workflow indirection: an
arbitrarily named workflow may call a script, local action, reusable workflow, or wrapper that the
direct-command heuristic cannot identify.

Run this human-maintainer sequence from reviewed, trusted project state:

1. Generate and review the local managed artifacts.

   ```bash
   uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice init \
     --github --repository OWNER/REPO
   ```

2. Inspect the remote repository, plan eligibility, environment, and visible secret names.

   ```bash
   bash .github/doc-lattice-bootstrap.sh plan OWNER/REPO
   ```

3. Apply and read back the exact `main`-only environment policy after typing the canonical
   repository identity.

   ```bash
   bash .github/doc-lattice-bootstrap.sh apply OWNER/REPO
   ```

4. Set the dedicated environment secret separately.

   Stop unless `apply` printed the exact success phrase: `environment policy verified`.

   ```bash
   # Continue only after apply prints: environment policy verified
   gh secret set DOC_LATTICE_LINEAR_API_KEY \
     --env doc-lattice-linear --repo OWNER/REPO
   ```

5. Complete secret migration in the same reviewed change. Run either deletion only when `plan` or
   `apply` reported that repository-scoped name.

   ```bash
   gh secret delete LINEAR_API_KEY --repo OWNER/REPO
   gh secret delete DOC_LATTICE_LINEAR_API_KEY --repo OWNER/REPO
   ```

6. Verify both the remote environment state and the committed local workflow policy.

   ```bash
   bash .github/doc-lattice-bootstrap.sh verify OWNER/REPO
   uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice ci audit \
     --repository OWNER/REPO
   ```

Every initial and every later `plan`, `apply`, or `verify` execution must use a bootstrap script
from reviewed trusted project state. Its ownership marker is useful installation metadata, not a
substitute for reviewing the executable shell content before running it.

The secret-setting command is not ready before `apply` re-reads and proves the exact `main`-only
environment policy. `apply` never receives the Linear key. `gh secret set` prompts for the value or
reads it from stdin, so the value is not part of the command arguments. This ordering is a
maintainer procedure; the server-side GitHub environment scope is the authorization control.

Bootstrap `plan` and `verify` exit 0 only when the protected policy, dedicated environment secret,
and repository-secret cleanup are all complete. They exit 1 for coherent but incomplete state and
2 when inspection is unreliable or setup is unsupported. `apply` first prints and fingerprints the
reviewed state, requires an attached stdin TTY and the exact canonical `OWNER/REPO`, then reinspects
before mutation. A first-time `apply` normally exits 1 because the separately entered secret is not
present yet. It exits 0 if setup is already complete and exits 2 on EOF, non-TTY input, confirmation
mismatch, changed state, or another tool error. There is no `--yes`, `--force`, environment
variable, or other noninteractive apply bypass. The same no-bypass rule applies to
`ci refresh --apply`.

GitHub API updates are not transactional, but completed remote state is re-readable and safe
partial setup can be resumed with a fresh `plan` and `apply`. The script does not roll back or
delete preexisting remote state. If an existing environment has broader or ambiguous rules, it
refuses to narrow or claim ownership of that environment and requires manual remediation.

GitHub's [deployment and environment documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
defines environment availability and protection behavior. Public repositories are eligible on
current GitHub plans. Private repositories owned by a user require GitHub Pro; private or internal
organization repositories require GitHub Team or Enterprise. The script fails closed if
visibility, plan eligibility, canonical repository casing, the exact `main` default branch,
repository secret metadata, or environment policy cannot be verified. A repository name, transfer,
or casing change requires:

```bash
doc-lattice ci refresh --repository CANONICAL/NAME
doc-lattice ci refresh --repository CANONICAL/NAME --apply
```

The preview exits 0 when current, 1 after printing an update diff, and 2 for an unreadable,
unmarked, or otherwise unsafe target. The diff renders non-line-ending byte controls as visible
`\xNN` escapes and Unicode format controls as `\uNNNN` or `\UNNNNNNNN` instead of sending
repository-controlled sequences to the terminal. Apply prints the same diff, requires typing the
explicit repository identity exactly, repeats preflight after confirmation, and atomically replaces
only marked canonical artifacts or creates a missing one.
Before publishing a missing artifact, both an initial create and a retry synchronize every
validated ancestor directory entry. Mixed versions after an interruption are safe to preview and
resume. Use this flow for generator upgrades and repository renames, then review and commit the
resulting diff. GitHub generation and refresh accept only final-version syntax such as `2.0.0`:
this rejects pins that can never resolve as final releases, but it does not prove the release is
already published or that an unreleased source checkout matches that release.

When `ci audit` omits `--repository`, it resolves the local `origin` only from GitHub.com SCP
(`git@github.com:OWNER/REPO.git`), `ssh://git@github.com/OWNER/REPO.git`, or
`https://github.com/OWNER/REPO.git` form, with the `.git` suffix optional. Comparisons are ASCII
case-insensitive, and the repository segment is limited to GitHub's 100-character maximum. The
offline audit cannot establish GitHub's canonical display casing. Bootstrap `plan` and `verify`
read the API `full_name` and require its spelling and case to match the generated literal exactly.
Origin lookup runs from the already-resolved Git top-level, so identity resolution and managed-file
inspection always refer to the same worktree.

For an existing installation, rotate or obtain a Linear key out of band. After the pre-generation
workflow replacement described above, set the replacement key only as
`DOC_LATTICE_LINEAR_API_KEY` on the `doc-lattice-linear` environment, and delete every reported
repository-scoped secret under both the legacy `LINEAR_API_KEY` and dedicated names. Rotation is
preferred because the broader key may already have been exposed. Repository administrators cannot
always inspect organization secret visibility, so obtain organization-owner confirmation that
neither name is exposed to this repository, or have the owner remove or exclude it. Setup is not
complete until bootstrap `verify` and local `ci audit` both pass.

In short, ci audit is meaningful only after `init --github`: before adoption, the absent artifacts
intentionally produce exit-1 findings. The audit checks every workflow for
`pull_request_target`, Linear secret references, direct Linear invocations under pull-request
events, and direct mutating reconcile invocations under those events. Least-privilege permissions,
action pins, checkout credentials, caching, triggers, and exact command structure are scoped to the
two canonical managed workflow paths, so an unrelated release workflow may legitimately use
`contents: write`.

Audit recognizes direct Bash and `sh` invocations, including supported `uv run` and `uvx` forms.
For pull-request steps it resolves step, job-default, and workflow-default shell configuration and
scans the selected command template. Unsupported runner defaults or shell semantics exit 2 instead
of being interpreted as Bash. Active brace or glob expansion in an executable or subcommand word,
and unsupported active extglob syntax anywhere in a command, also exit 2 because the resulting argv
cannot be certified statically. ANSI-C quoted words that decode to NUL after Bash's eight-bit octal
conversion exit 2 because Bash discards the suffix after NUL instead of placing that byte in an
argument. Known eager uv help/version options and effective command help stop without executing a
payload and therefore do not produce policy findings. Audit cannot prove that an arbitrary script,
local action, reusable workflow, or renamed wrapper eventually invokes a sensitive command.
A recognized inline dispatcher (`eval`, `source`, or `bash`, `sh`, `dash`, or `zsh` in `-c`
command-string form) cannot have its payload parsed, so when any word of the same command
literally names doc-lattice it exits 2 rather than being certified clean. That includes
dispatchers reached through the recognized wrapper and launcher grammar such as `uv run bash -c`
or `builtin eval`, and a shell head appearing in the arguments of an unrecognized program that may
re-dispatch its argv, as in `nohup bash -c 'doc-lattice ...'` or `xargs bash -c '... doc-lattice
...'`. A uv tool requirement given as a local wheel path resolves by its filename's distribution
name, which uv verifies against the wheel metadata, so `uvx ./bash-1.0.0-py3-none-any.whl -c ...`
refuses exactly like `uvx bash -c ...`; a path or URL requirement whose executable cannot be
derived statically (a source archive, a directory, or a Git or direct URL) also exits 2 whenever
the same command literally names doc-lattice, because the tool it installs may itself be a shell
or doc-lattice. A dispatcher whose payload is assembled rather than written literally as an
argument word stays within the disclosed executable-name limitation even when the assembled text
spells doc-lattice. That covers a variable executable name, a command, process, or arithmetic
substitution that builds the payload, and source fed from standard input by a heredoc,
herestring, or pipe. Provably non-executing shell invocations stay certified: a pure noexec
prefix (`bash -n -c ...`, `-nc`, or `-o noexec` with nothing else before `-c`) and the bash
string-dump modes (`--dump-strings`, `--dump-po-strings`) never run the payload in any
recognized shell. Any mixed form still exits 2, because execution can be re-enabled in
shell-specific ways (`+n`, `+o noexec`, zsh `-o exec` and its spelling aliases), and the short
`-D` dump option stays refused because zsh reads it as `PUSHD_TO_HOME` and executes normally.
An unrecognized wrapper likewise remains a disclosed limitation for the
non-dispatch forms: one that runs a script file (`nohup bash ./task.sh`) or passes doc-lattice
words to a program the scanner does not model (`xargs doc-lattice ...`) is not certified against.
Malformed, oversized, or otherwise unreliably inspectable workflows also exit 2 instead of being
treated as safe.
Whole-context, wildcard, or computed `secrets` access fails closed unless inspection proves it
selects one static unrelated name. A reusable-workflow job's `secrets: inherit` is whole-context
access because it forwards every available caller secret, so it always produces a
`LINEAR_SECRET_REFERENCE` finding. For the bootstrap script, audit validates only presence and
ownership metadata rather than content equality; the adjacent attributes artifact is checked for
its exact effective LF rule. Local audit also cannot see remote environment or organization-policy
drift, so rerun bootstrap `verify` from reviewed trusted state after relevant policy, visibility,
plan, rename, or transfer changes.

The generated environment is the authoritative secret boundary. It allows only the exact `main`
branch, and the dedicated environment-only secret is mapped to `LINEAR_API_KEY` only on the final
step of the trusted workflow. Removing the environment binding removes secret access. Current
ordinary `pull_request`, `pull_request_review`, and `pull_request_review_comment` runs use
`refs/pull/N/merge`, which the environment policy rejects. `pull_request_target` is different: it
uses the default branch ref, so the environment can authorize it while it handles untrusted input.
For that reason audit bans `pull_request_target` repository-wide, and trusted default-branch review
remains a load-bearing control. GitHub's
[December 2025 ref-semantics changelog](https://github.blog/changelog/2025-11-07-actions-pull_request_target-and-environment-branch-protections-changes/)
records this behavior change.

Before December 8, 2025, GitHub evaluated environment branch policy for pull-request-family runs
against the attacker-controlled pull-request head branch. The exact `main`, with no pattern, rule
was load-bearing under those semantics: relaxing it to a pattern such as `release/*` would
authorize attacker-chosen matching head branches. Even the exact name could be attacker-chosen, so
this design does not claim that the rule repairs the older behavior.

Older GitHub Enterprise Server versions are unsupported pending a separate compatibility review.

No generated workflow runs real `reconcile`; the offline workflow does not run even
`reconcile --dry-run` in this release. The exact managed triggers also omit `merge_group`, so merge
queues are unsupported until a generator release adds that event. Both managed workflows disable
persistent cross-run setup-uv and Actions caching; `uv` may still use its ephemeral job-local cache
while one runner job is active. Introducing persistent caching requires a separate security review.
Optional required environment reviewers and disabled administrator bypass can add manual approval
to each Linear run, but they are administered manually outside the initial generated script and
depend on repository visibility and plan support.

The boundary does not protect malicious code already reviewed and admitted to `main`. Other
residual risks include a compromised maintainer workstation or `gh` binary, pinned action, package
artifact, or dependency; a maintainer later broadening the environment; invisible organization
secret policy; and later visibility or billing changes that disable controls. Branch governance,
bootstrap `verify`, local `ci audit`, key rotation, and optional environment review address
different parts of that residual risk rather than replacing the environment boundary.

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
> [managed protected GitHub setup](#managed-github-and-linear-setup). The command processes
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
| `0` | Success; no coherent policy or gate finding, and no refresh update is pending. |
| `1` | Coherent finding: lattice drift, authority or Linear gate failure, GitHub CI policy violation, incomplete bootstrap state, or a managed refresh update. |
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

**`duplicate id ...` exits 2.** A duplicate id makes the index incoherent, so loading the lattice
fails with exit 2 (a tool error, distinct from the exit 1 that `check` and `lint` use for drift).
The message names both registration sites so you can find the clash: either two files share an
`id`, or two headings in one file resolve to the same anchor through equal markers or a marker/slug
collision. Equal anchors in different files do not collide.

## Documentation

| Document | Purpose |
|----------|---------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and the decision log |
| [CLAUDE.md](CLAUDE.md) | Short contributor and agent guide |
| [roadmap.md](roadmap.md) | Future direction |
| [CHANGELOG.md](CHANGELOG.md) | Release history and migrations |
| [RELEASING.md](RELEASING.md) | Release checklist and version-tag procedure |

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
└── pyproject.toml           # project configuration
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for module boundaries and their rationale.

## License

MIT. See [LICENSE](LICENSE).
