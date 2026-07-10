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
| `check [--only STATE ...]` | Classify every `derives_from` edge as OK / STALE / UNRECONCILED / BROKEN. | 1 on drift, 2 on tool error |
| `lint` | Validate the authority ladder (binding > derived > exploratory) over the edges. | 1 on a violation, 2 on tool error |
| `impact TOKEN [--depth N]` | List every downstream doc affected by a change to TOKEN; `--depth N` bounds the walk to N hops. | 2 on tool error |
| `reconcile [ID] [--ref REF] [--all]` | Set `seen` to current upstream hashes for the selected edges (the only command that mutates your tracked docs). | 2 on tool error |
| `graph [--format mermaid\|dot\|json]` | Emit the edge graph as Mermaid, DOT, or JSON. | 2 on tool error (including an unrecognized `--format`) |
| `linear [TARGET] [--from ID] [--exit-code] [--warn-exit]` | Report tickets shipped against a spec that has since drifted (needs `LINEAR_API_KEY`). | 1 with `--exit-code` on DANGER/BLOCKED, 2 on tool error |
| `init [--docs-root ...] [--linear-team KEY]` | Scaffold `.game-lattice.yml` and print pre-commit and CI codegen. | 2 on tool error |

Every command except `init` accepts `--config PATH` (path to `.game-lattice.yml`; defaults to
the file in the current directory). `check`, `lint`, `impact`, and `linear` accept `--json` for
machine-readable output. Run `uv run game-lattice <command> --help` for the full flag list.

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
# linear_team: ENG        # the Linear team the `linear` query targets
# binding_layers: null    # reserved; accepted in config but not yet consulted by lint
```

All `docs_roots` must resolve inside the project root; an entry that escapes via `..`, an
absolute path, or a symlink is rejected before any read.

## Adopting game-lattice in your docs repo

Bootstrap config and the drift and authority-ladder gates for a repo whose docs you want to
track:

```bash
uvx --python 3.13 --from git+https://github.com/Guardantix/game-lattice@v0.6.0 game-lattice init
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
