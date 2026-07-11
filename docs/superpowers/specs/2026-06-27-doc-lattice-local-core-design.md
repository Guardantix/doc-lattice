# doc-lattice Local Core: Design Spec

**Date:** 2026-06-27
**Status:** Design (post design-review pass). Ready for implementation planning.
**Scope:** The deterministic local engine only. No network, no secrets, no LLM in the hot path.
**Source decision record:** `~/.claude/LCARS/decisions/2026-06-27-doc-lattice-doc-traceability.md`

This spec turns the approved decision record into a buildable design for the first
implementation slice. It does not re-open any locked decision from that record. Where the
record left a mechanism open, this spec picks one and states it precisely.

## 1. Scope

In scope (the "heart of the tool" per the decision record):

- Parse lattice frontmatter and addressable sections out of a tracked doc set.
- Build an id index and an edge graph derived on demand (never committed).
- Four commands: `impact`, `check`, `reconcile`, `graph`.

Explicitly out of scope, deferred to later specs (see section 12):

- `linear` command, the GraphQL client, and ticket-status resolution.
- `init` scaffolding plus pre-commit and CI codegen.
- Any gitignored performance cache.
- Authority-ladder validation.
- Any `split` command. Splitting is a manual or Claude-driven edit, by design.

## 2. Data model

Three domain types, all immutable once built:

- `Edge(target_ref: str, target_id: str | None, seen: str | None)`. One `derives_from` entry.
  `target_ref` is the raw string as written. `target_id` is the resolved stable id, or `None`
  when the ref resolves to no id in the index (a broken edge). `seen` is the locked content
  hash, or `None` when the edge has never been reconciled.
- `Node(id, title, layer, authority, path, body, derives_from: list[Edge], tickets: list[str])`.
  One tracked file.
- `Lattice(nodes_by_id, index, dependents)`. The whole derived graph.
  `index` maps every stable id to a `Location`. `dependents` is the precomputed reverse
  adjacency: stable id to the set of source node ids that derive from it.

A `Location` is `(path, kind, span)` where `kind` is `file` or `section` and `span` is the
inclusive line range whose content the hash covers.

### 2.1 Id namespace and ref resolution (locked: Option B)

There is one flat id namespace across the entire lattice.
Every id is unique within it.

- A file becomes a node if and only if it has YAML frontmatter carrying an `id`.
  Files without lattice frontmatter are ignored, even if they sit under a docs root.
- Inside a node body, every heading carrying an explicit `{#anchor}` marker registers
  `anchor` as an id pointing at that section. Headings without an explicit anchor are not
  addressable. Opting a section into the lattice is a deliberate act: add the anchor.
- Uniqueness is enforced across the union of file ids and section anchor ids. A collision
  (two files, two anchors, or a file id equal to an anchor id) raises `DuplicateIdError`,
  lists both locations, and exits with code 2.

A `ref` may be written bare (`accent-color`) or with a display namespace
(`art-direction#accent-color`). Resolution splits on the last `#` and keys only on the
trailing id. The namespace prefix is non-authoritative display sugar and is never used for
resolution. This is what makes edges split-safe: when `accent-color` is later split into its
own file with `id: accent-color`, every inbound ref still resolves, because resolution never
depended on the `art-direction#` prefix. This is the literal realization of decision 10
(ids are the contract, filenames are disposable).

An optional future lint may warn when a display prefix no longer matches the id's current
home. It is not part of this slice.

### 2.2 Edge identity

An edge is identified by the pair `(source_node_id, resolved_target_id)`.
This identity is what `reconcile` selects on and what dedup keys on.
A node that lists the same resolved target twice triggers a warning and the duplicate is
dropped (last write wins on `seen`).

## 3. Architecture

All stages are pure code. No network. No LLM.

```
docs roots --> discover paths --> read + split frontmatter --> parse meta
            --> build nodes --> register ids + anchors --> Lattice (index + reverse adjacency)
                                                              |
        +-----------------------+-----------------------+-----+-------------------+
        v                       v                       v                         v
  impact (reverse walk)   check (hash vs seen)   reconcile (rewrite seen)   graph (emit)
```

### 3.1 Module decomposition

Each module has one purpose, an explicit interface, and is testable in isolation against
synthetic fixtures. Untyped external data (YAML) is converted to typed models only inside
boundary-named modules, because `scripts/check_typing_boundaries.py` permits `Any` and
`cast` only in modules whose name ends in `_parser`, `_boundary`, `_validator`, and so on.

| Module | Purpose | Pure? |
|---|---|---|
| `config.py` | Find and load `.doc-lattice.yml`, validate into a typed `Config` | impure I/O, typed |
| `discovery.py` | Walk docs roots, apply ignore globs, return safe paths; read UTF-8 text | impure I/O |
| `frontmatter_parser.py` | Split a file into `(raw_meta, body)`, parse raw YAML, validate into `NodeMeta` | boundary |
| `sections.py` | Heading TOC plus anchored-section extraction (adapted from `binding_slicer`) | pure |
| `hashing.py` | Canonicalize section or file content, compute the truncated content hash | pure |
| `model.py` | `Edge`, `Node`, `Location`, `Lattice` types | pure |
| `loader.py` | Pure assembly: parsed docs to `Lattice`, with id registration and uniqueness | pure |
| `orchestrate.py` | Thin wiring: `config` plus `discovery` plus parsing, then `loader.build_lattice` | impure |
| `resolve.py` | Resolve a ref to a `Location` plus current content; raise `BrokenRefError` | pure |
| `check.py` | Classify every edge into OK / STALE / UNRECONCILED / BROKEN | pure |
| `impact.py` | Reverse-walk dependents of a target; expand a file id to its anchors | pure |
| `reconcile.py` | Pure rewrite of frontmatter `seen`, plus an impure atomic write | split |
| `render.py` | Emit Mermaid or DOT from a `Lattice` | pure |
| `error_types.py` | Extend the existing `ProjectError` hierarchy | pure |
| `cli.py` | Wire the four commands; rich human output plus `--json` | impure |

The loader is split into pure assembly (`loader.build_lattice(parsed_docs)`) and the impure
orchestrator (`orchestrate.load_lattice(config)`). Graph logic is therefore tested without
touching disk. `reconcile` is likewise split: a pure `apply_reconcile(current_file_text,
updates)` that returns new file text by editing only the `seen` scalars, and an impure caller
that resolves the selection, hashes the upstream targets, re-reads each downstream file fresh
immediately before writing, and writes atomically.

## 4. Loading and indexing

`orchestrate.load_lattice(config)` does the following:

1. Each configured docs root is first resolved against the project root (the directory holding
   `.doc-lattice.yml`, or the current working directory when config is absent) and must stay
   inside it; a root that escapes via `..`, a symlink, or an absolute path outside the project
   is rejected with a `ConfigError` before any file is touched. This stops a repo-controlled
   config from steering reads or `reconcile` writes outside the worktree, which matters because
   `check` runs in CI against potentially untrusted repository contents.
   `discovery.discover_doc_paths(roots, ignore_globs)` then returns every candidate `.md` path,
   each run through `safe_resolve()` against its project-bounded docs root; a path that escapes
   its root (via `..` or a symlink) raises an error and is never read. Ignore globs are an
   optimization and a safety net; correctness does not depend on them, because a file with no
   lattice frontmatter is ignored regardless.
2. Each file is read as UTF-8. A decode failure or `OSError` raises `UnreadableDocError` with
   the path.
3. `frontmatter_parser.split_frontmatter(text)` returns `(raw_meta, body)`. Frontmatter must
   be the first content in the file: an optional UTF-8 BOM, then a line that is exactly `---`,
   the YAML block, then a closing line that is exactly `---`. A file lacking this opening is
   not a node and is skipped. `body` is everything after the closing `---`, preserved verbatim.
4. Raw YAML is parsed with `ruamel.yaml` in a safe mode (no arbitrary tag construction), then
   validated into a typed `NodeMeta` pydantic model with `extra="forbid"`, so a typo in a
   frontmatter key surfaces as a `ConfigError` naming the file. Malformed YAML raises
   `UnreadableDocError` with the path and the parser message.
5. `loader.build_lattice(parsed_docs)` (pure) constructs `Node`s, registers each file `id`,
   scans each body for headings with `{#anchor}` markers and registers those anchor ids, and
   enforces uniqueness across the whole namespace. It then resolves each edge `target_ref`
   against the index, setting `target_id` to the resolved id or leaving it `None` when the ref
   matches nothing. A failed resolution is not a load error: a broken edge is a normal lattice
   state that `check` reports as BROKEN (exit 1), distinct from a `DuplicateIdError` that makes
   the index itself incoherent (exit 2). The reverse adjacency `dependents` is built from
   resolved edges only.

## 5. Section extraction and hashing

### 5.1 Section spans

Adapted from `binding_slicer`. A section anchored at a heading spans from the heading line
through the line before the next heading whose level is less than or equal to that heading's
level, or to end of file. Nested sub-headings and their content are part of the span. A
consequence worth stating: editing a nested anchored sub-section changes the content of its
parent section too, so both go STALE. That is correct propagation, not a bug.

The anchor marker is matched on the explicit `{#id}` form. The tolerant heading
normalization from `binding_slicer` (`{#id}` trailers, `<a>` wrappers, leading bullets and
symbols, numeric prefixes, surrounding emphasis) is reused for display only. Only the
heading-TOC and section-span logic is ported. The prose anchor scanner from `binding_slicer`
(`_find_anchor_matches`) is not ported, so its regex surface does not enter this tool.

### 5.2 What the hash covers

- For a `section` target, the hashed content is the section heading text with the `{#id}`
  marker stripped, plus the section body.
- For a `file` target (a whole node, valid mainly after a split), the hashed content is the
  node body (everything after the closing frontmatter `---`).

### 5.3 Canonicalization and the hash

Canonicalization: line endings normalized to `\n`, trailing whitespace stripped per line,
leading and trailing blank lines trimmed. Internal blank lines are preserved. This removes
editor noise (trailing spaces, final-newline churn, CRLF) without hiding substantive edits.

The `seen` value is `sha256(canonical_bytes_utf8)` truncated to the first 32 hex characters
(128 bits). The decision-record example used a 7-character value illustratively; 128 bits is
collision-safe for any realistic corpus while keeping the stored lockfile value short. Each
`seen` only ever compares one section against its own past self.

`hashing.canonicalize` must be idempotent and stable. Two `hypothesis` properties pin this:

- Canonicalization-equivalent variants hash identically: trailing-whitespace, CRLF, and
  final-newline variants of one content always produce one hash. This is a true universal and
  is asserted over generated inputs.
- Distinct contents hash distinctly: asserted with fixed example pairs, not as a universal
  claim, because a hash collision is possible in principle for any digest. At 128 bits the
  practical collision probability for this corpus is negligible, but the test states only what
  is actually guaranteed.

## 6. Commands

### 6.1 `check`

The CI gate. For every edge in every node: resolve the upstream ref, extract the target
content, hash it, and classify.

| State | Condition |
|---|---|
| OK | upstream resolves and `hash(current) == seen` |
| STALE | upstream resolves and `hash(current) != seen` |
| UNRECONCILED | edge has no `seen` value yet |
| BROKEN | ref resolves to no id in the index |

Exit codes carry meaning so CI can tell apart the cases:

- 0: every edge OK.
- 1: drift found (any STALE, UNRECONCILED, or BROKEN edge).
- 2: tool or config error (missing or invalid config, duplicate id, unreadable doc).

Human output is a rich table grouped by state. `--json` emits the full classified report for
CI or Claude to consume.

### 6.2 `impact <token>`

Discovery. `token` is a bare id or a `namespace#id` ref. It may name a section anchor or a
whole file.

- If `token` resolves to a file id, the target set is that file id plus every anchor id the
  file contains. If it resolves to a section anchor, the target set is that anchor plus every
  anchored ancestor section whose span contains it. This mirrors the hashing model in section
  5.1: editing a nested section also changes every enclosing section's hash, so dependents of
  those ancestors are genuinely affected and must appear in the impact set. Omitting them would
  under-report exactly the edit the Discovery guarantee exists to cover.
- Reverse-walk `dependents`: any source whose edge resolves to a target in the set is a
  direct dependent; follow transitively. A visited set guards against cycles.
- Output per downstream node: `id`, `title`, `path`, and its `tickets`. Tickets are raw
  `PC-*` strings in this slice; live Linear status arrives with the deferred `linear` spec.
- `--json` emits the dependent set for tooling.

### 6.3 `reconcile`

The conscious "I looked, still fine," and the only mutating command.

- Selection: `reconcile <downstream-id>` reconciles every edge on that node, `--ref
  <upstream-ref>` narrows to one edge, and `reconcile --all` clears every STALE and
  UNRECONCILED edge across the lattice. Selection matches on resolved edge identity
  `(source_node_id, resolved_target_id)`, so a `--ref` written either bare or namespaced
  selects the same edge.
- For each selected edge, recompute the upstream hash and set that edge's `seen` to it.
- A BROKEN edge cannot be reconciled. `reconcile` refuses it and reports that the ref must be
  fixed first.
- Rewrite mechanism: reconcile re-reads the target file fresh immediately before writing, then
  edits only the targeted `seen` scalar(s) in place. That fresh content is split into
  frontmatter text and body text; only the frontmatter block is round-tripped through
  `ruamel.yaml`, which preserves key order and comments, so the diff touches only the changed
  `seen` scalars. The body after the closing `---` is never passed through the YAML engine; it
  is reattached verbatim from that same fresh read. Because the edit is applied to the current
  on-disk content rather than a copy captured at lattice-load time, a concurrent body or
  other-frontmatter edit made between load and write is preserved, not clobbered. The result is
  written atomically (temp file in the same directory, then `os.replace`) so an interruption
  cannot corrupt the doc. Advisory locks and a merge step are intentionally out of scope:
  `reconcile` is a single-user local action and CI invokes only the read-only `check`, so the
  residual same-instant double-write race does not justify that machinery.

### 6.4 `graph`

Read-only. Emit Mermaid by default or DOT with `--format dot` to stdout. Nodes are labeled by
title. `derives_from` edges are drawn upstream to downstream. STALE edges are styled
distinctly (dashed) so the graph doubles as a drift snapshot. Only tracked nodes appear.

## 7. Configuration

`.doc-lattice.yml`, read directly by the tool. The deferred `init` command will scaffold it
later; the tool does not depend on `init` having run.

```yaml
docs_roots: [docs]
ignore_globs:
  - "**/superpowers/plans/**"
  - "**/build-log/archive/**"
linear_team: null      # parsed but unused in this slice (forward-compat)
binding_layers: null   # reserved; this slice checks all edges regardless of layer
```

Validated through pydantic with `extra="forbid"`. Located in the current working directory or
repo root; `--config PATH` overrides; absent config falls back to `docs_roots: [docs]` with no
ignore globs. Every `docs_roots` entry must resolve to a path inside the project root (the
config's directory, or cwd when defaulting); roots outside it are rejected with a `ConfigError`.
Confining roots to the project is the MVP behavior; an explicit opt-in for external roots is a
deferred enhancement, not built until a real cross-tree layout needs it. All doc paths pass
through `safe_resolve()`.

## 8. Error handling

Extends the existing `ProjectError` hierarchy with actionable, coded errors that mirror the
gx-linear-skills precedent:

- `ConfigError`: missing or invalid config, or a forbidden or malformed frontmatter key.
- `DuplicateIdError`: two ids collide in the namespace. Lists both locations. Exit 2.
- `BrokenRefError`: a broken edge is recorded at load time as `target_id = None` (not raised)
  and reported as a BROKEN edge by `check`; `reconcile` raises it when asked to reconcile such
  an edge.
- `UnreadableDocError`: non-UTF-8 content, `OSError`, or unparseable YAML. Names the path.

No bare `except Exception`. No `datetime.now()` outside `datetime_utils.py`. Error messages
state the file and the fix.

## 9. Security

The only untrusted input in this slice is local markdown and its YAML frontmatter. There is no
network and no secret material.

- YAML is parsed in a safe mode so no arbitrary Python objects are constructed and YAML
  anchor or alias expansion cannot execute code. Validation into pydantic follows.
- Configured `docs_roots` must resolve inside the project root, so a repo-controlled
  `.doc-lattice.yml` cannot steer reads or `reconcile` writes outside the worktree. This
  matters in CI, where `check` runs against potentially untrusted repository contents.
- Every doc path passes through `safe_resolve()` against its docs root, so traversal and
  symlink escape outside the root are refused before any read.
- Only the heading-TOC and section-span logic from `binding_slicer` is ported; the prose
  anchor-scanning regexes are left behind, keeping the regex surface small and linear.

The repo ships zero secrets. No fixture or config in this public repo carries a real token.
The real security surface (network, credentials) belongs to the deferred `linear` spec and
should get a dedicated security pass there.

## 10. Testing

Test-driven, per the project conventions, with tests mirroring source one to one.

- A synthetic fixture lattice under `tests/fixtures/`: a small multi-file tree with
  `art-direction.md` (sections `{#accent-color}` and `{#motion}`), `pc-design.md` deriving from
  both with `seen` hashes, plus a deliberately STALE edge, an UNRECONCILED edge, and a BROKEN
  ref. Zero secrets, zero network.
- Unit tests per module: `test_frontmatter_parser.py`, `test_sections.py`, `test_hashing.py`,
  `test_resolve.py`, `test_loader.py`, `test_check.py`, `test_impact.py`, `test_reconcile.py`,
  `test_render.py`, `test_config.py`, `test_cli.py`.
- Property tests (`hypothesis`) on `hashing.canonicalize` per section 5.3.
- CLI tests via typer's `CliRunner`, asserting the `check` exit codes 0, 1, and 2 and the
  `--json` shapes.
- A `reconcile` round-trip test: a STALE edge, then `reconcile`, then `check` is clean, and the
  resulting file diff touches only the `seen` scalar.
- A `reconcile` concurrency test: a body edit applied to the target file between lattice load
  and the write is preserved in the output, proving the in-place fresh-read rewrite does not
  reattach a stale body.
- A pure-loader uniqueness test: a fixture with a colliding id raises `DuplicateIdError`.
- An `impact` ancestor-expansion test: `impact <nested-anchor>` includes dependents of the
  enclosing parent section, not just the nested anchor's own dependents.
- A broken-ref reporting test: a fixture edge whose ref resolves to nothing loads without error
  (`target_id = None`), and `check --json` reports it as BROKEN with exit code 1, not a tool
  error.
- A docs-root containment test: a config whose `docs_roots` points outside the project root is
  rejected with a `ConfigError` before any file is read.
- Coverage at or above the existing 80 percent gate. The existing `test_conventions.py` stays
  green.

## 11. Dependencies

Keep `typer`, `rich`, and `pydantic`. Add `ruamel.yaml` as the single YAML library: it reads
frontmatter and config, and it round-trip-writes for `reconcile`, which avoids carrying two
YAML libraries. `hypothesis` is already in the dev group.

## 12. Non-goals and deferral map

| Deferred item | Where it lands |
|---|---|
| `linear` command, GraphQL client, ticket status | next spec |
| `init` scaffolding, pre-commit and CI codegen | later spec |
| Gitignored performance cache | not needed at this corpus size |
| Authority-ladder validation | `authority` is parsed, stored, rendered; not policed here |
| `split` command | none; "Execution has no command" by design |
| Display-prefix lint | optional future enhancement |

## 13. Acceptance: the three-pain scorecard

| Pain | Solved by | Verifiable when |
|---|---|---|
| Discovery | `impact` over the reverse adjacency | a change to one section lists every downstream doc and ticket |
| Execution | stable ids plus `impact`-guided loading | edges survive splitting a file; `impact` points at the exact section |
| Confidence | `check` exit-code gate plus `reconcile` | a stale `seen` fails CI until consciously reconciled |
