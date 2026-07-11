# game-lattice

A deterministic, offline traceability engine for game design and production documentation.

game-lattice tracks the dependencies *between* your markdown docs. When a downstream
document derives from an upstream one (a player-character spec built on the art direction,
a level design built on the core loop), it records that link in frontmatter. When the
upstream changes, game-lattice tells you exactly which downstream docs went stale, and a
CI gate keeps stale work from shipping silently.

It is pure tooling: no network (except the optional `linear` command), no secrets, no LLM,
no database. The dependency graph is derived from your docs on demand, never committed.

## The problem it solves

Design docs drift apart. Someone retunes the economy, edits the art direction, or rewrites
the core loop, and the dozen documents downstream of that decision keep citing the old
version. Nothing breaks loudly; the docs just quietly disagree, and the drift surfaces as a
bug, a re-do, or an argument weeks later.

game-lattice makes those dependencies explicit and *checkable*. Each downstream doc declares
what it derives from and records a hash of what it last saw. A change upstream that the
downstream hasn't acknowledged is **drift**, and `check` fails CI on it until a human
consciously reconciles the link.

## How it works

You annotate docs with two things:

- **Stable ids.** Every tracked file declares an `id` in its frontmatter. Sections are addressed
  by their heading's GitHub slug by default; an explicit `{#anchor}` tag on the heading provides
  a stable id independent of heading text. Section ids are file-scoped, so the same anchor in
  two files does not collide with file ids or each other.
- **`derives_from` edges.** A downstream doc lists the upstream ids it depends on. Each edge
  carries a `seen` hash: a fingerprint of the upstream content at the moment the dependency
  was last reconciled.

From those annotations game-lattice builds a **lattice**: an id-indexed graph of nodes
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

### A broken ref is a state, not a crash

The only thing that makes loading the lattice *fail* is a duplicate id, which makes the
index incoherent (exit 2). A ref that points at nothing is a normal, reportable lattice
state: `check` calls it BROKEN and exits 1. That is the core distinction the tool is built
on: **exit 1 means "the graph is coherent but drifting," exit 2 means "the index itself is
broken."**

### The authority ladder

Separately from drift, `lint` enforces a structural rule: authority only flows downhill.
Docs can declare an `authority` of `binding`, `derived`, or `exploratory`. A `derives_from`
edge from a more-authoritative doc to a less-authoritative one is an **inversion** (a binding
spec should not derive from an exploratory sketch), and `lint` fails on it. `lint` is pure
structure, independent of drift, and exits 1 on a violation just like `check`.

## A worked example

Two docs. The upstream owns a decision; the downstream depends on it.

`docs/art-direction.md`, the upstream:

```markdown
---
id: art-direction
layer: design
authority: binding
---
# Art Direction

## Accent Color {#accent}
Warm amber, used for every interactive highlight.
```

`docs/pc-design.md`, which derives from the accent decision:

```markdown
---
id: pc-design
layer: design
authority: derived
derives_from:
  - ref: art-direction#accent
    seen: 7f3a9c2e1b8d4f6a0c5e9d2b7a1f4e8c
tickets: [PC-228]
---
# Player Character Design

The PC's UI highlights use the accent color.
```

The ref `art-direction#accent` resolves file-scoped: it points at the section in the `art-direction`
file whose heading carries the `{#accent}` marker. Markers are optional; a heading with no marker is
addressed by its GitHub slug instead, and the `{#accent}` marker here pins a short stable id
regardless of the heading's wording. The `seen` hash records the accent text pc-design was last built
against.

Now someone changes the accent to "cool teal." The `{#accent}` section's content hash no
longer matches `seen`, so:

```console
$ game-lattice check
STALE         pc-design -> art-direction#accent

$ game-lattice impact art-direction#accent
pc-design  (docs/pc-design.md)  tickets: PC-228
```

`check` exits 1, so CI is now red. A human reviews pc-design against the new accent, updates
the body if needed, and then locks in the new hash:

```console
$ game-lattice reconcile pc-design
reconciled pc-design.md: art-direction#accent

$ game-lattice check
OK            pc-design -> art-direction#accent
```

That edit → `check` → review → `reconcile` loop is the whole workflow. `reconcile` is the
only command that writes to your docs, and it only ever rewrites the `seen` scalar.

## Quick start

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Install

```bash
uv sync --group dev
```

### Run

```bash
uv run game-lattice --help
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
| `reconcile [ID] [--ref REF] [--all] [--dry-run]` | Set `seen` to current upstream hashes for the selected edges (the only command that mutates your tracked docs); `--dry-run` previews the plan without writing. | 2 on tool error |
| `graph [--format mermaid\|dot\|json]` | Emit the edge graph as Mermaid, DOT, or JSON. | 2 on tool error (including an unrecognized `--format`) |
| `linear [TARGET] [--from ID] [--exit-code] [--warn-exit]` | Report tickets shipped against a spec that has since drifted (needs `LINEAR_API_KEY`). | 1 with `--exit-code` on DANGER/BLOCKED (or WARNING too under `--warn-exit`), 2 on tool error |
| `init [--docs-root ...] [--linear-team KEY]` | Scaffold `.game-lattice.yml` and print pre-commit and CI codegen. | 2 on tool error |

Only `check` and `lint` gate by default, exiting 1 when they find drift or an authority inversion.
`impact`, `reconcile`, `graph`, and `init` are informational and always exit 0 on success (2 only on
a tool error), so wiring `impact` into a CI gate never turns the build red. `linear` also exits 0 by
default; pass `--exit-code` to gate on any DANGER or BLOCKED finding, and add `--warn-exit` to gate on
WARNING as well.

Every command except `init` accepts `--config PATH` (path to `.game-lattice.yml`; defaults to
the file in the current directory). `check`, `lint`, `impact`, `reconcile`, and `linear` accept
`--json` for machine-readable output. Run `uv run game-lattice <command> --help` for the full
flag list.

Pass `--indent N` with JSON output on `check`, `lint`, `impact`, or `linear` to pretty-print the
JSON with `N` spaces per level. JSON output is selected by `--json`, or the equivalent
`--format json` on `check` and `lint`; `--indent` without JSON output is a usage error.

Use the global `--no-color` option before the command to disable colored output explicitly, for
example `game-lattice --no-color check`. Rich also honors the [`NO_COLOR`](https://no-color.org/)
environment variable; `--no-color` is the command-line equivalent. Either one also strips the
styling from help and usage-error text even when a terminal-forcing variable is set.

`check` and `lint` also accept `--format human|json|github`. `human` is the default, and `json`
is equivalent to the existing `--json` alias. `github` emits one escaped GitHub Actions `::error`
workflow command per drift finding or ladder violation, each with a repo-relative file path, so
findings attach inline to the offending doc in the pull-request diff. Output selection never
changes gate exit codes. Do not combine `--json` with `--format github`.

`impact` walks the full transitive closure by default. Pass `--depth N` (N >= 1) to bound the
walk to N hops from TOKEN: `--depth 1` lists only the docs that derive directly from it. Human
output is unchanged, and each `--json` entry gains a `"depth"` field carrying the minimum number
of hops at which that doc is reached.

`check` accepts a repeatable `--only STATE` to narrow the display to specific states (case
insensitive, e.g. `--only stale --only broken`); an unrecognized state exits 2 and names the
valid set. Filtering is display-only: the exit code always reflects every edge, so `check --only
OK` on a drifting lattice still exits 1.

### `reconcile` selectors

`reconcile` needs either a downstream id or `--all` (running it with neither is an error):

- **`reconcile DOWNSTREAM_ID`**: reconcile every drifting edge of one downstream node.
- **`reconcile DOWNSTREAM_ID --ref REF`**: narrow to a single upstream ref on that node, selected
  by resolved identity; refused if it targets a BROKEN edge.
- **`reconcile --all`**: clear every STALE/UNRECONCILED edge in the lattice. Skips BROKEN and
  already-OK edges, and skips a node's broken edge rather than failing the node, so one dangling
  ref never blocks the rest.

`reconcile` re-reads each downstream file fresh at write time, rewrites only the targeted `seen`
scalar through round-trip YAML (preserving your body, key order, and comments), and writes
atomically, so a concurrent edit is never clobbered.

Add `--dry-run` to any of the selectors above to preview the plan without writing: it prints
`would reconcile FILE: REF` per edge that would change (`nothing to reconcile` if none would),
and leaves every file byte-identical. Combine with `--json` for a machine-readable plan:
`{"dry_run": true, "reconciled": [{"path": ..., "ref": ..., "new_seen": ...}]}`, sorted by path
then ref. A real run with `--json` emits the same shape with `"dry_run": false`, after the
writes complete.

## Frontmatter reference

| Key | Where | Meaning |
|-----|-------|---------|
| `id` | every tracked file | The file's stable id. Required. |
| `title` | optional | Display title. |
| `layer` | optional | `design`, `technical`, or `production`. |
| `authority` | optional | `binding`, `derived`, or `exploratory`. Ranked by `lint`. |
| `derives_from` | downstream files | List of `{ ref, seen }` edges. |
| `derives_from[].ref` | each edge | The upstream id: bare (whole-file target, e.g. `art-direction`) or file-scoped (section target, e.g. `art-direction#accent`). |
| `derives_from[].seen` | each edge | The locked upstream hash, or omitted for a never-reconciled (UNRECONCILED) edge. |
| `tickets` | optional | Issue ids associated with the doc (used by `impact` and `linear`). |

Section ids are optional: a heading is addressed by its GitHub slug by default (e.g. `## Accent Color`
resolves to `accent-color`), and an explicit `{#anchor}` marker on the heading wins as an escape hatch for
a stable id independent of heading text (e.g. `## Accent Color {#accent-hue}`). Section refs are
file-scoped (`file#anchor`), so the same anchor in two files does not collide.

## Configuration

game-lattice runs zero-config (defaulting to a `docs/` root), or reads `.game-lattice.yml`
from the current directory:

```yaml
# game-lattice configuration
docs_roots:
  - docs                  # roots to scan for tracked .md files (default: ["docs"])
# ignore_globs:           # paths to skip within those roots
#   - "**/superpowers/plans/**"
# cache_key: my-docs      # opt-in incremental load cache slot (see Load cache below)
# cache_trust_stat: false # opt-in stat fast tier for read-only commands (accepts the mtime caveat)
# linear_team: ENG        # the Linear team the `linear` query targets
# binding_layers: null    # accepted but inert today; setting it changes nothing (see below)
```

All `docs_roots` must resolve inside the project root; an entry that escapes via `..`, an
absolute path, or a symlink is rejected before any read.

`binding_layers` is accepted in the config for forward compatibility but is inert today: setting it
changes nothing, because no command consults it. Authority ranking currently lives entirely in
`lint` (binding > derived > exploratory); see the
[lint design spec](docs/superpowers/specs/2026-06-28-game-lattice-lint-design.md) for where that
ranking is defined.

### Load cache (opt-in)

Large doc sets (thousands of files) can skip re-parsing unchanged docs with an opt-in cache.
Set `cache_key` to a single safe segment (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`); it names a slot
under your user cache home at `<cache_home>/game-lattice/<cache_key>/load-cache.json`, where
`<cache_home>` is `$XDG_CACHE_HOME` (when absolute) or `~/.cache`. The cache lives outside every
checkout on purpose: because `.game-lattice.yml` is committed, every clone and git worktree of the
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
and always verifies content, so it can never
write frontmatter from stale data. Two projects sharing a `cache_key` stay correct (a content-hash
hit implies identical bytes); the only cost is overwrite churn, so prefer distinct keys. Delete the
cache directory to reset it; a tool-version bump discards it automatically.

## Adopting game-lattice in your docs repo

Bootstrap config and the drift and authority-ladder gates for a repo whose docs you want to
track:

```bash
uvx --python 3.13 --from git+https://github.com/Guardantix/game-lattice@v0.7.0 game-lattice init
```

This writes `.game-lattice.yml` (only if absent) and prints pre-commit hooks and a GitHub
Actions workflow that run `game-lattice check` (drift) and `game-lattice lint` (authority
ladder) as your gates. Paste each where the output says. Pass `--docs-root` (repeatable) or
`--linear-team` to bake those values into the generated config.

## Linear integration

`game-lattice linear` is the only network-touching command. It builds a trigger map from the
loaded lattice, then fetches live ticket status over the Linear GraphQL API to report tickets
that shipped against a spec that has since drifted. It reads `LINEAR_API_KEY` from the
environment (export it before running; the error points you to `impact` for the offline view),
and the client is https-only, redirect-refusing, size-capped, and SSRF-hardened. A transient
HTTP 429 or 5xx is retried up to three times with a short backoff (honoring `Retry-After` when
present, capped) before failing, so a passing rate limit does not fail a CI run. Set the team
the query targets with `linear_team` in `.game-lattice.yml`, or pass `--linear-team` to `init`.
Every other command runs fully offline.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success; no drift or violations. |
| `1` | The lattice is coherent but a gate failed: drift (`check`), an authority inversion (`lint`), or (with `--exit-code`) a DANGER/BLOCKED `linear` finding. |
| `2` | Tool error: the index is incoherent (e.g. a duplicate id), config is invalid, or a path escapes the project root. |

## Troubleshooting

**`LINEAR_API_KEY is not set`.** Only the `linear` command needs a key. Export a Linear API key
(`export LINEAR_API_KEY=lin_api_...`) before running `linear`, or run `impact` instead: `impact` is
the fully offline view of the same downstream reach and needs no key.

**Linear returns HTTP 429 or 5xx.** These are transient. The client already retries a bounded
number of times with a short backoff (honoring `Retry-After` when present, capped), so a passing
rate limit does not fail the run on its own. If it still fails after those retries, the error tells
you to wait and re-run; `impact` stays available offline in the meantime.

**A `linear` finding is BLOCKED `not-found`.** A ticket the Linear filter does not return is treated
as absence, not an error: it grades as a BLOCKED `not-found` finding rather than crashing the
command. Confirm the ticket id exists and that `linear_team` targets the right team.

**`duplicate id ...` exits 2.** A duplicate id makes the index incoherent, so loading the lattice
fails with exit 2 (a tool error, distinct from the exit 1 that `check` and `lint` use for drift).
The message names both registration sites so you can find the clash: either two files share an `id`,
or a heading's `{#marker}` collides with another heading's computed slug in the same file.

## Documentation

| Document | Purpose |
|----------|---------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and the decision log |
| [CLAUDE.md](CLAUDE.md) | Architecture map and tooling-enforced invariants |
| [roadmap.md](roadmap.md) | Shipped slices and what is deferred |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [RELEASING.md](RELEASING.md) | Release checklist and version-tag procedure |
| [build-log.md](build-log.md) | Development timeline |
| [docs/superpowers/specs/](docs/superpowers/specs/) | The binding design specs (source of truth) |

## Project structure

```
game-lattice/
├── src/game_lattice/    # the engine: a pure graph/report core behind a thin impure shell
├── tests/               # test suite (mirrors sources; property-based hashing invariants)
├── scripts/             # CI guards (typing boundary, version sync)
├── docs/superpowers/    # design specs and plans
└── pyproject.toml       # project configuration
```

The engine is a pure pipeline (`config -> discovery -> frontmatter parse -> build_lattice`
feeding `{ check, impact, reconcile, graph, lint, linear }`) where all graph and report logic
is filesystem-free. Only `config`, `discovery`, `orchestrate`, and `cli` touch the disk, and
only `linear_client` touches the network. See [ARCHITECTURE.md](ARCHITECTURE.md) for the
decisions behind that split.

## License

MIT. See [LICENSE](LICENSE).
