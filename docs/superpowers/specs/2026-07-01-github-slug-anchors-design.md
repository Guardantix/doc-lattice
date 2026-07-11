# doc-lattice GitHub-Slug Anchors: Design Spec

**Date:** 2026-07-01
**Status:** Design (post brainstorm). Ready for implementation planning.
**Scope:** Anchor resolution. Add a GitHub-native heading-slug fallback so a `derives_from` section
ref resolves against a plain heading with no `{#slug}` marker, make section resolution file-scoped,
and keep explicit markers as a precedence-winning escape hatch. No network, no secrets, no new
command, no change to drift or hashing semantics beyond what falls out of resolution. The change is
internal to the load and resolve pipeline plus the report layers that read resolved ids.
**Builds on:** `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md` (the local core,
which defines the flat id index, `split_ref`, `Edge.resolve`, and section spans this slice reworks).
**Issue:** GitHub issue #14, "Resolve derives_from anchors from GitHub-native heading slugs (retire
the literal {#slug} marker)", including the ChatGPT Codex design-review comment.

This spec retires the literal `{#slug}` marker as the *only* way to address an anchored section. Today
a section is addressable solely through an explicit `{#slug}` marker on its heading, which renders
literally on GitHub (`## UI {#ui}` shows `UI {#ui}`), breaks GitHub's own anchor (GitHub slugifies the
whole heading text, marker included, to `ui-ui`), and duplicates information the heading already
carries. This slice adds a heading-slug fallback whose output is byte-parity with GitHub's rendered
anchors, so a natural prose link and a `derives_from` ref resolve to the same place.

## 1. Scope

In scope:

- A pure `github_slug(text)` helper in `sections.py` that reproduces the `github-slugger` package's
  slug output byte-for-byte, plus a document-order de-duping pass that reproduces GitHub's
  `-1`/`-2` suffixing.
- A slug fallback in anchor resolution: a heading with no explicit `{#slug}` marker becomes
  section-addressable by its computed GitHub slug. Explicit markers are still honored and take
  precedence.
- File-scoped section resolution. Section ids resolve as `(file-id, anchor)` against that file's
  headings, not against a flat global id table, so the same slug in two files does not collide.
- A typed resolved-target key (`TargetId`) replacing the flat string id across the lattice maps and
  the report layers, so a file target and a section target cannot be confused and the file prefix on a
  section ref is load-bearing rather than display-only.
- A frontmatter guard rejecting a `#` inside a node `id`, so the file-id half of a section id is
  unambiguous.
- Documentation updates (`CLAUDE.md`, `README.md`) reflecting file-scoped resolution and the
  `split_ref` to `parse_ref` change.

Explicitly out of scope, deferred, or declined (see section 11):

- The downstream Mainspring ref rewrite. The Codex comment notes Mainspring's short markers do not
  equal their heading slugs (`save-format#slot-table` versus the real `save-format#32-slot-table`), so
  the adopter must remove its 7 markers and repoint its 9 section refs to real slugs. That is a
  separate consuming-repo chore, not part of this tool change, and this spec does not pin Mainspring's
  literal strings into doc-lattice's tests.
- An alias system that preserves short semantic refs like `#ui` or `#slot-table` after marker
  removal. Rejected: it is a different feature and would re-couple the tool to a consumer's naming.
- Full inline-markdown rendering before slugging. `github_slug` operates on raw heading source text,
  which matches GitHub for plain-text headings (the norm) but can diverge for headings with inline
  links, images, or complex markup. Those keep the marker escape hatch (section 3).
- Any new command, network call, or change to `check`/`reconcile`/`graph`/`init`/`lint` behavior
  beyond re-typing the resolved id they already consume. The `linear` slice is a deliberate
  exception: its `stale_shipped` trigger builders join `expand_targets` and `anchors_by_path` against
  node ids, so they need active `TargetId` bridging, not passive re-typing (section 6), and are in
  scope as a behavior-preserving change.
- Preserving bare-anchor resolution. A bare ref now resolves only to a file id; a bare anchor that is
  not a file id becomes a normal `BROKEN` edge (section 5).

## 2. Problem and resolution model

Two facts about the current engine set up the change:

- `sections.py` extracts an anchor only from an explicit `{#slug}` marker (`_ANCHOR_RE`); a heading
  without that literal marker has `Heading.anchor is None`.
- `loader.py` registers a `Location(kind="section")` only for headings whose `.anchor is not None`, so
  an anchorless heading is never section-addressable. `model.split_ref` returns only the trailing
  segment after the last `#`, so anchor ids live in a flat global namespace and the file prefix is
  display-only.

The new resolution order for a section ref `<file>#<anchor>` is:

1. If the heading carries an explicit `{#<anchor>}` marker, use it. Full back-compat and an escape
   hatch for a stable id independent of heading text.
2. Otherwise, compute the GitHub slug of each heading's text and match `<anchor>` against it,
   file-scoped.

The key modeling decisions, unchanged from the local core, still hold: a ref that resolves to nothing
is not a load error but a normal `BROKEN` edge (exit 1, drift), distinct from a duplicate id (exit 2,
incoherent index). Marker removal is content-hash-neutral because `section_text` already strips the
marker before returning section content for hashing.

## 3. The GitHub slug algorithm

Add to `sections.py` a pure, stateless helper and a stateful de-duping pass.

`github_slug(text: str) -> str` is a faithful port of the `github-slugger` package's `slug()`
function, not a hand-rolled approximation. The steps `github-slugger` performs, in order:

1. lowercase the text;
2. remove every character in `github-slugger`'s strip set (ASCII punctuation, a wide Unicode
   punctuation and symbol range, and emoji);
3. replace each ASCII space (`U+0020`) with a single `-`.

Byte-parity with GitHub's rendered anchors is the whole point, so the strip regex must be copied
verbatim from a pinned `github-slugger` version, and that version must be recorded in the spec history
and in a comment beside the ported regex. **This slice pins `github-slugger@2.0.0`**: the character
class in `sections.py`'s `_SLUG_STRIP_RE` is a verbatim translation of that version's `regex.js`,
verified codepoint-for-codepoint (0 through 0x10FFFF) against the real package. Two behaviors that a
hand-rolled version tends to get wrong, and that the corpus (section 10) locks, are:

- `github-slugger` does not collapse runs. `a  b` (two spaces) becomes `a--b`, not `a-b`. We match
  that; we do not "improve" it.
- `github-slugger` strips a large character set including `.`, so `3.2 Slot table` becomes
  `32-slot-table` and `5.7 Capability` becomes `57-capability`. Underscores and existing hyphens are
  preserved.

Document-order de-duping is a separate stateful pass that reproduces `github-slugger`'s `Slugger`
occurrence counter: maintain an occurrences map; the first time a base slug appears, emit it and
reserve it; each subsequent appearance emits `base-1`, `base-2`, and so on, reserving each result so a
later identical base cannot reuse it. A second `## Notes` becomes `notes-1`, matching GitHub.

**Known limitation (drives the escape-hatch narrative).** `github_slug` slugs the raw heading source
text, not fully-rendered inline markdown. Plain-text headings, the overwhelming norm for spec section
headings, match GitHub byte-for-byte. A heading with inline markup whose rendered text differs from
its source (for example `## [Foo](url)`, where GitHub anchors on the rendered `Foo` but the source
contains the URL) can diverge. This is an explicit non-goal, not a bug: such a heading keeps an
explicit `{#marker}`, which is precisely the escape hatch's purpose. Retaining the marker feature is
therefore not merely back-compat; it is the pressure-release for headings the slugger cannot match
GitHub on.

## 4. Marker precedence and counter parity

`build_toc` and the `Heading` dataclass are unchanged. `Heading.anchor` still means "the explicit
marker, or `None`", and `Heading.text` still retains the marker. A new pass computes one addressable
id per heading:

```
def anchor_ids(toc: list[Heading]) -> list[str]:
    # one addressable id per heading, in document order
```

For each heading in document order, compute `unique = dedup(github_slug(heading.text))` for **every**
heading, marker headings included, advancing and reserving the occurrences counter. The heading's
addressable id is then:

- `heading.anchor` when a marker is present (the marker wins), or
- `unique` when there is no marker.

Computing and reserving `unique` even for a marker heading is deliberate. `github-slugger` on GitHub
slugs the marker heading from its literal, marker-retaining text (which is exactly what
`Heading.text` holds) and reserves that slug in its counter. By reserving it too, the markerless
headings around a marker heading stay byte-identical to GitHub even in a mixed file. Excluding marker
headings from the counter would diverge by a `-N` suffix in the contrived case where a marker
heading's rendered anchor collides with a markerless heading's slug; reserving avoids that entirely.

The consequence is that **every heading becomes section-addressable**, not just marked ones. That is
the point of retiring the marker: an author cites `save-format#slot-table` against a plain
`## Slot table` with no hand-added marker.

## 5. File-scoped resolution and the typed key

### 5.1 `TargetId`

Add a frozen, hashable value type to `model.py`:

```python
@dataclass(frozen=True, slots=True)
class TargetId:
    """A resolved target: a whole file, or a file-scoped section anchor."""

    file_id: str
    anchor: str | None = None  # None => the whole file; else a section within file_id

    def as_ref(self) -> str:
        """Return the canonical ref string: "file" or "file#anchor"."""
        return self.file_id if self.anchor is None else f"{self.file_id}#{self.anchor}"
```

`TargetId` is the single key type for resolved targets. A file target is `TargetId("save-format")`; a
section target is `TargetId("save-format", "slot-table")`. Because the two halves are separate fields,
a file target and a section target can never be confused, and the `#` separator is not overloaded
inside a key.

### 5.2 `parse_ref` replaces `split_ref`

`split_ref(ref) -> str` is replaced by `parse_ref(ref) -> TargetId`:

- a ref containing `#`: split on the last `#` into `(file_id, anchor)` and return
  `TargetId(file_id, anchor)`;
- a bare ref: return `TargetId(ref)`, a file id.

`parse_ref` is pure string-to-`TargetId` parsing; it does not consult the index. Resolution (whether a
`TargetId` actually exists) stays in `Edge.resolve` and the `index` lookups, exactly as membership was
checked before. This preserves the local core's separation: parsing a ref never fails, and an
unresolvable ref is a `BROKEN` edge, never a load error.

`Edge.target_id` becomes `TargetId | None`. `Edge.resolve(ref, seen, index)` builds
`parse_ref(ref)` and keeps it as `target_id` only when it is present in `index`, else `None`.

### 5.3 Bare refs and back-compat

Section refs must be namespaced `<file>#<anchor>`. A bare ref resolves only to a file id. A bare
anchor that is not also a file id is a normal `BROKEN` edge (exit 1 in `check`), not a load failure.
This drops the local core's flat-namespace behavior where a bare `accent` and a namespaced
`art-direction#accent` resolved to the same id. In practice every real `derives_from` ref is already
written in `<real-file-id>#<anchor>` form, so making the prefix load-bearing does not break them; the
`split_ref` unit tests that asserted the flat behavior are replaced by `parse_ref` tests (section 10).

### 5.4 Lattice maps

The lattice maps re-key on `TargetId`:

- `index: Mapping[TargetId, Location]`
- `dependents: Mapping[TargetId, frozenset[str]]` (values stay source-node-id strings; a
  `derives_from` always belongs to a whole file)
- `ancestors: Mapping[TargetId, tuple[TargetId, ...]]`
- `anchors_by_path: Mapping[Path, frozenset[TargetId]]`
- `file_id_by_path: Mapping[Path, str]` is unchanged (a node id is a plain string).

### 5.5 Node-id guard

`NodeMeta` gains a validator rejecting a `#` in `id`. With the typed key this is defense-in-depth, not
correctness-critical: a `#` in a file id could never silently corrupt a composite key (fields are
separate), but a stray `#` would produce a confusing `BROKEN` instead of a clear error. The validator
fails frontmatter validation (exit 2) and names the file.

## 6. Change surface, module by module

- **sections.py** adds `github_slug` and the `anchor_ids` document-order pass (sections 3, 4). `build_toc`, `Heading`, `section_span`, `section_text` are unchanged.
- **loader.py** registers each heading as `TargetId(file_id, anchor_id)` from `anchor_ids`, so ancestors and `anchors_by_path` now cover every heading, not only marked ones. `_register` keys on `TargetId`, so the same slug in different files yields distinct keys (no `DuplicateIdError`) while a within-file collision (a marker equal to a computed slug, or two equal markers) still errors correctly, naming both sites. `_resolve_edges` dedups on `parse_ref(raw.ref)`.
- **model.py** adds `TargetId`, `parse_ref`, re-types `Edge.target_id`, the `Lattice` maps, and adds the `NodeMeta` id guard.
- **resolve.py** re-types `target_content(lattice, target_id: TargetId)`; the `index.get` and `section_text` calls are otherwise unchanged. `node_for_path` is unchanged.
- **check.py** re-types `EdgeStatus.target_id` to `TargetId | None`; `_classify` reads `edge.target_id` as before.
- **impact.py** parses the token with `parse_ref`, walks with `TargetId`, and bridges a walked source node id back to its file target via `TargetId(source_id)` when continuing the walk. `expand_targets` returns `set[TargetId]`.
- **stale_shipped.py** (the `linear` trigger builders) needs active `TargetId` bridging, not passive re-typing, because it joins `expand_targets`/`anchors_by_path` against node ids. Two call sites break silently otherwise. In `build_audit_trigger`, the filter `tid in lattice.nodes_by_id` over `expand_targets` goes always-False once `tid` is a `TargetId` (the map is string-keyed), so the deliberate add-back of the target's own node is lost and a scoped `linear <target>` audit drops that node's own STALE-shipped tickets; the fix bridges file targets to node ids, for example `{tid.file_id for tid in expand_targets(lattice, target) if tid.anchor is None and tid.file_id in lattice.nodes_by_id}`. In `build_from_trigger`, `closure` becomes `set[TargetId]` and the affected-node add must be `closure.add(TargetId(node.id))`, not the bare string, or an edge deriving from a whole downstream file fails the `edge.target_id in closure` test and `linear --from` under-reports its justifying refs. Trigger-dict keys stay node-id strings and `Finding.drifted_refs` stay raw `target_ref` strings (grouping and display, unaffected).
- **render.py** `_graph_edges` reads `edge.target_id` as a `TargetId`; `stale_edges` becomes `set[tuple[str, TargetId]]`. Node ids in the rendered graph stay file-id strings.
- **reconcile.py** compares edges with `parse_ref(edge.target_ref) != parse_ref(ref)` (`TargetId` equality). The rewrite plan still keys on the literal `target_ref` string, so `apply_reconcile` is unchanged.
- **lint.py** re-types `LadderViolation.target_id` and `SkippedEdge.target_id` to `TargetId`; `_target_authority(lattice, target_id: TargetId)` looks up the index as before.
- **cli.py** serializes `target_id` in `check --json` and `lint --json` via `TargetId.as_ref()`, so the JSON field changes from the old bare id (`slot-table`) to the scoped ref (`save-format#slot-table`), a deliberate and more-informative output change. The `graph` command's stale set holds `TargetId` (internal). No flag or exit-code change.

## 7. Migration and hash-neutrality

Issue #14's acceptance criterion "remove every marker, repoint nothing, `check` stays green" holds
only when a marker string equals its heading's computed slug. The Codex comment shows Mainspring's do
not. This spec splits that into two honest guarantees:

- **(a) Content-hash neutrality, always true.** `section_text` strips the marker before hashing, so
  deleting a marker (`## Slot table {#slot-table}` to `## Slot table`) yields byte-identical section
  text, an identical `seen` hash, and, being an inline edit, shifts no line numbers.
- **(b) Ref-text stability, only when `marker == slug`.** When they differ, the ref string must be
  repointed to the real slug (`save-format#slot-table` to `save-format#32-slot-table`). Because the
  target content is unchanged, the repointed ref's `seen` still matches, so no `reconcile` is needed;
  only a ref-string edit.

Sequencing for an adopter: ship this tool change, cut a release, then remove markers and repoint refs
as a no-reconcile verification pass in the consuming repo.

## 8. Error handling

No new exception types. An unresolvable ref, including a now-unresolvable bare anchor ref, is a normal
`BROKEN` edge (exit 1), not a load failure. A within-file duplicate anchor (a marker equal to a
computed slug, or two equal markers) raises the existing `DuplicateIdError` (exit 2), naming both
registration sites. A `#` in a node `id` fails frontmatter validation (exit 2) and names the file. All
messages continue to name the file and the fix, per the repo's error convention.

## 9. Conventions and invariants

- The pure/impure split is preserved. `sections.py`, `model.py`, `loader.py`, `resolve.py`,
  `check.py`, `lint.py`, `impact.py`, `render.py`, and `reconcile.reconcile` stay pure and
  filesystem-free. `github_slug` and `anchor_ids` are pure.
- The untyped-to-typed boundary is unchanged. `frontmatter_parser` and `linear_parser` remain the only
  boundary modules; the id guard lives at frontmatter validation.
- No `datetime.now()`, no raw string literals duplicating a constant, module docstrings and
  Google-style docstrings on public functions, ruff line length 100, and no em-dashes in drafted
  content, all as before.
- The version-sync invariant is untouched by this slice; whether this ships in its own release is a
  planning decision, not a design one.

## 10. Testing

Test files mirror sources. New and updated coverage:

- **`github_slug` parity corpus** in `test_sections.py`, generic strings (not Mainspring's literals),
  pinned to `github-slugger@2.0.0`, covering: lowercasing, numbered headings
  (`3.2 Slot table` to `32-slot-table`, `5.7 Capability` to `57-capability`), punctuation stripping,
  single-space-to-hyphen and the no-collapse `a  b` to `a--b` behavior, emoji stripping,
  underscore/hyphen preservation, leading/trailing text, and document-order dupes
  (`notes`, `notes-1`, `notes-2`).
- **Precedence and counter parity**: an explicit marker wins over a computed slug; a mixed
  marker-plus-markerless file keeps the markerless headings byte-parity with GitHub (the
  counter-reservation test).
- **Scoping**: cross-file same-slug non-collision; within-file collision (marker equal to a slug, two
  equal markers) raises `DuplicateIdError`.
- **Resolution**: `file#slug` resolves against a markerless heading; a bare `slug` that is not a file
  id resolves to `BROKEN`; `parse_ref` unit tests replace the `split_ref` tests; the `#`-in-id guard
  rejects a bad id.
- **Downstream layers**: `impact`, `render`, and `reconcile` behave under file-scoped anchors; the
  `check --json` and `lint --json` `target_id` field emits the scoped ref form; marker removal is
  hash-neutral.
- **Linear trigger builders**: `stale_shipped.build_audit_trigger` and `build_from_trigger` under
  file-scoped `TargetId`. A scoped `linear <target>` audit still grades the target node's own
  STALE-shipped tickets (the `build_audit_trigger` add-back), and `linear --from` still reports
  justifying refs for edges that derive from a whole downstream file (the `build_from_trigger`
  closure). Both are exercised with file-target and section-target edges, since the file-target path
  is exactly where the bridge is load-bearing and where a naive re-type under-reports.
- **Fixture**: `tests/conftest.py`'s `lattice_dir` is updated so its STALE, UNRECONCILED, and BROKEN
  edges keep the same states under file-scoped resolution, since those states are load-bearing across
  the check, reconcile, and CLI suites. The `FORCE_COLOR` scrubbing already in `conftest` is untouched.

## 11. Non-goals and deferral map

- **Mainspring ref rewrite**: out of scope, a separate consuming-repo chore (section 1).
- **Alias system for short refs**: declined (section 1).
- **Inline-markdown-aware slugging**: declined; the marker escape hatch covers divergent headings
  (section 3).
- **Bare-anchor resolution**: dropped; section refs must be namespaced (section 5.3).
- **Composite-string key**: considered and set aside in favor of the typed `TargetId`, which cannot
  confuse a file target with a section target and makes the `#`-in-id guard defense-in-depth rather
  than correctness-critical.

## 12. Acceptance criteria

- `derives_from: - ref: save-format#slot-table` resolves against a plain `## Slot table` heading with
  no marker.
- An explicit `{#slug}` still resolves and takes precedence over the computed slug.
- The same slug in two different files does not raise `DuplicateIdError`; a within-file collision does.
- Computed slugs equal GitHub's rendered anchors across the covered corpus, including numbered and
  punctuated headings and document-order duplicates.
- Removing a marker leaves the section's content hash unchanged; where the marker did not equal the
  slug, the ref is repointed to the real slug with no `reconcile` needed.
- A bare anchor ref that is not a file id reports `BROKEN`, not a load failure.
- `check`, `impact`, `reconcile`, `graph`, `lint`, and `linear` all read the file-scoped `TargetId`
  correctly, and the two `--json` surfaces emit the scoped ref form.
- A scoped `linear <target>` audit grades the target node's own stale-shipped tickets, and
  `linear --from` reports every justifying ref including whole-file derivations; neither
  under-reports after the `TargetId` migration.
- Full suite green with coverage at or above the enforced 80 percent; ruff, `ty`, the typing-boundary
  check, and the version-sync guard all clean.
