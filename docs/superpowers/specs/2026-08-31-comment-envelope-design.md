# Design: GitHub-invisible metadata envelope (comment spelling)

Date: 2026-08-31
Status: approved design, pre-implementation; adversarial design review closed by decision
2026-08-31 after four rounds (the PR #250 stopping-rule pattern). Further adversarial passes
target the implementation, where findings get reproductions instead of prose.
Related: GTX-168 (self-adoption), GTX-451 (colinear anchor convention), AD-13, AD-15, AD-31,
AD-33, AD-35

## Problem

Every tracked file must carry YAML frontmatter (`---` fences, `id:` key, optionally
`derives_from` with machine-written `seen` hashes). GitHub renders that frontmatter as a table
at the top of the rendered file, which for a README means the repository landing page. Both
current adopters publish or plan to publish on GitHub, and the visible metadata is the stated
blocker for doc-lattice adopting itself (GTX-168) and for colinear's public docs (GTX-451).

Section identity is already GitHub-friendly: explicit `{#anchor}` markers are optional, and an
unmarked column-zero ATX heading is addressed by its computed GitHub slug (`anchor_ids` in
`markdown_compat.py`). No engine change is needed on the section side. The decision there is a
convention, recorded in this design: GitHub-published repos should use auto-slug identity and
omit `{#anchor}` markers. A reword then changes the id: when the slug is unique, dependent
edges go BROKEN and the change is loud; within a slug-collision component, document-order
deduplication can rebind the id to a different heading silently, which is why this design adds
ambiguous-target detection (below). The marker syntax stays supported for adopters that want
reword-stable ids.

## Decision

Accept a second spelling of the metadata block: the identical YAML, wrapped in an HTML comment
instead of `---` fences.

```markdown
<!-- doc-lattice
id: architecture
derives_from:
  - ref: readme#configuration
    seen: 647cc64481bee8d8541ef7d1733b5204
-->
```

HTML comments render as nothing on GitHub, so tracked documents look clean. The YAML inside is
byte-for-byte the same language: `NodeMeta`, `RawEdge`, edge resolution, validation tiers, and
every module downstream of parsing are untouched.

Both spellings are accepted unconditionally and forever. There is no config knob (AD-15:
speculative configuration is removed, not reserved; a selector here would be speculative since
nothing needs to forbid either spelling). Existing `---` lattices (Mainspring) keep their
spelling untouched, but are affected by the v7 semantics below: the context-inclusive hash
invalidates existing `seen` values once, and edges into collision components newly fail as
`AMBIGUOUS`. This design therefore ships in the v7 release, not as an additive minor.

## Envelope grammar

A file is metadata-bearing when its first line is one of:

- A line stripping to exactly `---`, after an optional BOM (the existing fence rule in
  `frontmatter_parser.py`, unchanged): the YAML-fence spelling.
- Exactly `<!-- doc-lattice` at column zero, no leading or trailing whitespace beyond the line
  terminator: the comment spelling.

The comment opener is deliberately stricter than the fence rule: CommonMark treats a
4-space-indented opener as an indented code block, which would render the "invisible" envelope
as literal text while doc-lattice tracked the file. The opener therefore admits no whitespace
variance at all, and every accepted byte form (including the BOM case) is pinned by a
renderer-parity test rather than inherited from the fence grammar by analogy. The BOM parity
question is resolved: on markdown-it-py 4.2.0 a BOM-prefixed envelope parses as a paragraph,
not an html_block, so the comment spelling admits no BOM at all; the opener must be the first
bytes of the file, and a BOM before it lands in the opener near-miss error. The optional-BOM
rule remains fence-only.

Byte-exactness must not create a silent near-miss: a first line whose *trimmed* form equals
the sentinel but whose spelling is not exact (trailing whitespace, indentation, case variance)
is an actionable opener-format error, exit-2, never ordinary untracked prose. The check runs
before untracked classification, so a whitespace typo cannot make the intended node vanish
from the lattice under a green gate.

For the comment spelling:

- The envelope body is every line up to the first line that is exactly `-->` at column zero.
- An opening line with no closing line is a hard error, matching the unclosed-fence error.
- The body is handed to the same strict pure-Python ruamel load (AD-33) and `NodeMeta`
  validation (AD-35) as fence frontmatter.
- Classification is deliberately *not* the fence's tier ladder. The fence has innocent
  readings (Jekyll frontmatter, a thematic break), so a non-mapping or id-less fenced block
  degrades softly. `<!-- doc-lattice` has exactly one reading: it declares lattice intent by
  name. The comment spelling therefore fails closed: any body that is not a mapping carrying
  `id` (empty, scalar, list, mapping without `id`) is an exit-2 `FRONTMATTER_ERROR` with an
  actionable message. There is no untracked or id-less-warning tier for the comment spelling.

### The `--` refusal

The envelope body must not contain the substring `--`. A body containing it is refused with an
actionable error naming the offending line, in the AD-35 refuse-don't-respell spirit.

Rationale: `--` inside HTML comments is where the HTML spec and CommonMark versions disagree,
and the failure mode is silent and user-facing: a legal-but-unlucky id such as `foo--bar` could
terminate or invalidate the comment in some renderer and turn the "invisible" envelope into
rendered text. Refusing the substring outright is stricter than GitHub requires today and keeps
the invisibility guarantee independent of renderer behavior. Note the refusal is scoped to the
comment spelling; the fence spelling accepts `--` as it always has, so converting a file that
uses such an id means renaming the id or keeping the fence spelling.

The refusal is enforced on output as well as input. It is not argued by construction from
"reconcile only writes hex `seen` values": the rewriter can re-spell content beyond the value
it targets (adversarial review produced a YAML alias-relocation candidate where an escaped
`"--"` value could be re-emitted as a literal `--`). Instead, the post-edit
verification carries the envelope kind, and for the comment spelling the raw `--` validator
runs against the rewritten envelope body before the transaction stages the write; a violation
refuses the rewrite with an actionable message. A regression test pins the alias-relocation
candidate either way its verdict lands, so the class is closed by a gate rather than by
reasoning about the rewriter.

### Renderer parity

Parity tests pin what markdown-it-py 4.2.0 does with the envelope: it must parse as an inert
`html_block` that never perturbs heading extraction, section spans, or the drift hash. Body
handling matches fence frontmatter handling exactly (whatever the current pipeline does with
the fenced block relative to section derivation, the comment envelope does the same), so
`SectionRecord` offsets stay consistent between spellings.

## Precedence and misplacement

Both spellings claim line 1, so a file cannot syntactically carry both. Two guard rules close
the gaps:

- A file whose line 1 opens one spelling and whose later content contains the other is not an
  error: the later block is ordinary content. (A tracked file may legitimately quote either
  syntax in examples; doc-lattice's own README does.)
- A line stripping to `<!-- doc-lattice` found anywhere other than line 1, in a file that is
  otherwise untracked, produces a misplacement warning (the analogue of the id-less warning
  tier). This closes the "I put the envelope after the H1 and the file silently vanished from
  the lattice" hole. Line 1 itself needs no warning tier: an exact opener is tracked, and a
  near-miss opener is the exit-2 opener-format error above. The warning does not fire for
  files already tracked via line 1, since their later occurrences are the quoted-example case
  above. To keep the warning from firing
  on an untracked file that merely quotes the syntax in a code block (README-style examples),
  detection is two-stage: a cheap substring pre-check, and on a hit the file is parsed with
  the already-pinned markdown-it adapter and the warning fires only for occurrences outside
  code blocks. The parse cost lands only on files containing the sentinel, which is rare.
- The misplacement outcome is cached data, not a parse-time side effect: it becomes a distinct
  `FrontmatterDisposition` value carried through the cache schema, so the warning replays on
  verify-tier and stat-tier cache hits exactly as it fires on a cold run (the AD-29 pattern;
  the `CACHE_VERSION` bump this design already requires covers the schema change). Tests pin
  warning parity across cold, verify-hit, and stat-hit runs.

## Ambiguous-target detection

Auto-slug identity has one silent failure mode, surfaced in adversarial review: slug
deduplication is document-order, so with two `## Old Title` headings (`old-title`,
`old-title-1`), rewording the first hands the bare `old-title` id to the second. An edge on
`file#old-title` still resolves, so it is not BROKEN; it reads STALE, or even OK when both
section bodies are byte-identical, and reconcile could bless the rebound dependency.

The countermeasure detects the precondition deterministically:

- Ambiguity is derived from the slugger's full allocation trace, not from base slugs or final
  ids alone. Two failure shapes force this. Dedup suffixes chain across bases (`Notes`,
  `Notes`, `Notes-1`, `Notes-1-1` generate `notes`, `notes-1`, `notes-1-1`, `notes-1-1-1`,
  where a reword of the first heading shifts all four). And dedup *probes* occupied ids it
  never emits: in `Notes`, `Other`, `Notes`, the third heading probes `notes-1` while only
  base-requesting `notes`, so a rule built on requests alone frees exactly the id that
  rebinds when `Other` is later renamed to `Notes-1`. The rule is therefore: during
  allocation, every candidate id a heading examines (its base and each dedup suffix tried)
  links that heading to the id's current holder; the connected components of that graph are
  collision components, and every generated id in a component is ambiguous. An id set by an
  explicit `{#anchor}` marker is never ambiguous; being reword-stable is what the marker is
  for.
- Collision tracing runs over the *full GitHub heading inventory*, not just the addressable
  subset. GitHub allocates ids to setext, indented, quoted, and nested headings the engine
  does not address, so a mixed-form collision (setext `Overview` followed by `# Overview`)
  makes the lattice id and the GitHub fragment diverge, and that divergence only ever arises
  through such a cross-inventory collision. The full-inventory parse and the shared slugger
  already exist for `scripts/check_doc_links.py`; a cross-inventory collision pulls the
  addressable member into an `AMBIGUOUS` component and fails closed. Addressability itself
  stays column-zero ATX (the two-inventory separation is preserved); running *allocation*
  over the full inventory is the GTX-277 follow-up, deliberately not in v7.
- An edge whose resolved target id is ambiguous is a first-class edge state, `AMBIGUOUS`, not
  an advisory: `check` exits 1 on it exactly as it does for BROKEN, `lint` reports it, and
  every graph-consuming command (`impact`, `graph`, `linear`) carries it in human and JSON
  output, naming the colliding headings. The same condition gets the same verdict everywhere;
  a warning-only design would leave CI green on a lattice the tool itself refuses to
  reconcile.
- `reconcile` refuses to write `seen` for an `AMBIGUOUS` edge, with an actionable message:
  disambiguate by rewording one of the colliding headings or by giving the target an explicit
  marker. Refusing keeps the tool from blessing a dependency the declaration cannot
  unambiguously name (the AD-35 refuse-don't-guess spirit).
- This covers both ends of the hazard window: declaring an edge into an existing collision
  component goes red immediately, and a later edit that creates the collision goes red on the
  next run. The transient case, where a collision appears and dissolves inside a single change
  so no run ever sees it, is closed by the context-inclusive target hash below.

Detection must survive cache replay with byte-identical output (AD-12: a cache hit must match
the uncached result exactly, and diagnostics that name colliding headings cannot be
reconstructed from a boolean). `SectionRecordModel` therefore persists collision-component
provenance computed at derivation time: the component's member headings as safe display
labels plus their locations. Parity tests compare exact human and JSON output across cold,
verify-hit, and stat-hit runs, not flag equality. Covered by the same `CACHE_VERSION` bump.

### Safe heading display

Naming colliding headings puts raw heading text, which is unrestricted Markdown body content,
into terminal, CI-log, DOT, and Mermaid sinks for the first time; today's outputs print only
slugs, and AD-35's control-character refusal covers frontmatter values only. One shared
safe-display representation strips or escapes C0, C1, and DEL controls before each sink's own
quoting (Rich markup escaping, DOT and Mermaid string quoting), and every sink that names
headings uses it: `check`, `lint`, `impact`, `graph`, `linear`. The persisted provenance
stores the already-sanitized labels, so cached and uncached paths cannot diverge on
sanitization. Regression cases cover OSC, CSI, Rich markup, DOT, and Mermaid per sink.

### Context-inclusive target hash

The section drift hash today starts at the section's own heading, so two byte-identical
sections under different parents (templated `### Setup` under `## Product A` and
`## Product B`) hash identically. That defeats the STALE net in the transient-collision case:
add B's `Setup` and rename A's in the same change, and `#setup` transfers between products
with no run ever seeing a collision, the old `seen` still matching, and drift analysis
following the wrong product from then on.

The fix makes context part of target identity: a section target's hash input is the ancestor
heading chain (raw inline heading source, the same source the slugger reads) prepended to the
section content. A section that moves under a different parent, or whose ancestor is
reworded, goes STALE even when its own bytes did not change; that is correct, because the
context is part of what the downstream document derived from. Whole-file targets are
unaffected.

With probe-complete components, the `AMBIGUOUS` state, and the context-inclusive hash
together, silent rebinding requires a target with identical body *and* identical ancestor
chain, and identical ancestor chains containing identical headings collide, which the
component machinery turns red. The class is closed up to sections that are genuinely
interchangeable.

## Reconcile rewriter

The split result from `split_frontmatter_parts` is extended with an envelope kind
(`fence` | `comment`) and inner-YAML byte offsets; its three call sites (two in
`orchestrate.py`, one in `reconcile.py`) adopt the extended result. The byte-exact rewriter
continues to operate purely on the inner YAML text and
re-emits the file's original delimiters byte-for-byte:

- Reconcile preserves whichever spelling a file uses. It never converts one spelling to the
  other, in either direction.
- AD-31's layer model gains the comment envelope at Layer 2a as a second declared envelope; the
  inner layers (semantic schema, supported YAML spellings, occurrence addressing) are shared
  between spellings by construction.

This is the risk-bearing chunk (`reconcile.py` plus `reconcile_transaction.py`, roughly 3.8k
lines with about 9k lines of tests), so implementation sequences it after the parser work, with
the reconcile fixture suites run at each step.

## Cache

`CACHE_VERSION` bumps 5 to 6. Which files are tracked is a cached derivation
(`FrontmatterDisposition`, `NodePayload.meta`) and its input grammar changes. The disposition
domain gains the misplacement value described above so the warning survives cache replay, and
`SectionRecordModel` gains the collision-component provenance (sanitized member labels and
locations). Envelope kind is not cached: it is re-derived from the file at rewrite time, and
reconcile reads the file it rewrites anyway.

## Version-skew guard

The format and hash changes must fail loud under version skew, because both adopters run
engine versions pinned in more than one place (workflows are still pinned `==4.0.0` today): a
v6 engine reading a converted repo would classify every comment envelope as prose and report
green with nodes missing, and an older reconciler could rewrite v7-scheme hashes with
old-scheme values.

The guard is a required config key. v7 refuses to operate against a `.doc-lattice.yml` that
does not declare `lattice_format: 2`, with an actionable error pointing at the migration
procedure; the migration adds the key. A run with no config file at all is exempt (there is
no file to declare the key in, and ad hoc zero-config runs have no installation to skew
against); the requirement binds whenever a config file is read. Older engines reject the key for free: `Config` is
`strict=True, extra="forbid"`, so every pre-v7 engine hard-errors on the unknown key before
loading or reconciling anything. Both directions fail loud with zero cooperation from code
that predates the feature. `scaffold` writes the key into new lattices. The hash algorithm is
not separately namespaced; the config boundary is the guard, and a second version channel
would be redundant surface (AD-15 spirit). This is not speculative configuration: the key is
required, live, and read on every run.

## Contract surface

- ARCHITECTURE.md: a new AD records this decision (GitHub-invisible metadata, dual spelling
  forever, no spelling-selector config, the `--` refusal enforced on input and rewritten
  output, the fail-closed classification of the branded envelope and its near-miss openers,
  the auto-slug convention for GitHub-published repos, probe-complete ambiguous-target
  detection with the `AMBIGUOUS` edge state and its reconcile refusal, full-inventory
  collision tracing with ATX-only addressability preserved, the context-inclusive target
  hash, the `lattice_format` version-skew guard, and the safe heading-display
  representation). AD-31 is amended to declare the second envelope.
- README.md: the frontmatter reference documents the second spelling next to the first. The
  edge-status table gains the `AMBIGUOUS` row with its exit-code and JSON contract. A new
  "Publishing on GitHub" section recommends the comment envelope plus auto-slug refs, and
  surfaces two facts that today live only in code comments: `{#anchor}` renders as literal
  heading text on GitHub, and the marker changes the GitHub-side fragment of that heading.
- README.md (config reference): the required `lattice_format: 2` key.
- CHANGELOG.md: ships in v7 (7.0.0), not as an additive minor, with a migration section
  covering three steps: add `lattice_format: 2` to `.doc-lattice.yml` (v7 refuses to run
  without it; older engines refuse to run with it), the context-inclusive hash makes every
  existing `seen` value on a section under a parent heading mismatch once (run `reconcile` to
  re-bless the lattice; whole-file and top-level-section targets keep their hash), and edges
  into collision components newly fail `AMBIGUOUS` (add a marker or reword a colliding
  heading). The comment-envelope
  conversion note rides along: wrap the existing frontmatter YAML lines in `<!-- doc-lattice`
  and `-->`, delete the `---` fences, rerun `check`.
- Version sync: `scripts/check_version_sync.py` for the release change, as always.

## Testing

- `tests/test_frontmatter_parser.py`: comment-spelling cases for the exact opener (tracked,
  unclosed envelope, BOM, CRLF if the fence suite covers it), the fail-closed errors
  (non-mapping body, mapping without `id`, empty body), the opener-format error for every
  near-miss spelling (trailing whitespace, indentation, case variance), the `--` refusal, and
  the misplacement warning.
- `tests/test_markdown_compat.py` / `tests/test_sections.py`: renderer-parity pins (inert
  `html_block`, heading extraction and spans unaffected, drift hash unaffected).
- Reconcile suites: spelling preservation on `seen` writes, byte-exactness of re-emitted
  delimiters, the transaction fixtures over comment-spelling files, the post-rewrite `--`
  validator refusing a violating write, and the alias-relocation regression case.
- Ambiguous-target detection: `AMBIGUOUS` on declaring an edge into an existing collision
  component and when a later heading creates the collision; the chained-suffix rebind case
  (`Notes`/`Notes`/`Notes-1`/`Notes-1-1` with a reword of the first heading); the
  probe-completeness case (`Notes`/`Other`/`Notes` with `Other` renamed to `Notes-1`);
  marker-set ids exempt; the cross-inventory case (setext `Overview` then `# Overview` goes
  `AMBIGUOUS`); the reconcile refusal and its message; exit-code and JSON contract for
  `check`, `lint`, `impact`, `graph`, and `linear`; and exact human and JSON output parity
  across cold, verify-hit, and stat-hit runs.
- Safe heading display: OSC, CSI, Rich-markup, DOT, and Mermaid regression cases for every
  sink that names headings.
- Version-skew guard: v7 refuses a config without `lattice_format: 2` with the migration
  pointer, accepts it with the key, `scaffold` writes it, and a pre-v7 `Config` model
  (reconstructed in-test) rejects the key.
- Context-inclusive hash: the templated-parents regression (identical `### Setup` under two
  products, one renamed in the same change, edge goes STALE rather than silently
  transferring), an ancestor reword STALEing a child-targeted edge, whole-file targets
  unaffected, and the one-time migration behavior (pre-v7 `seen` values read as STALE and
  re-bless cleanly).
- `tests/test_cache_schema.py` / cache tests: version bump discards prior caches.
- `tests/test_readme_contract.py`: whatever it pins about the frontmatter table gains the
  second spelling.
- The `lattice_dir` conftest fixture is explicitly out of scope (per GTX-168's scope note);
  comment-spelling fixtures are new files, not edits to it.

## Rollout (follow-on work, not this change)

- colinear: reconcile GTX-451 to auto-slug links and comment envelopes.
- doc-lattice: GTX-168 self-adoption proceeds with a clean landing page; its README-rendering
  acceptance criterion is answered by construction.

## Out of scope

- Path-derived ids for id-less files (considered, deferred: only helps edge-free upstream
  files and composes cleanly later if wanted).
- Sidecar manifest (rejected: loses locality, centralizes reconcile writes into one
  merge-conflict magnet, rewrites AD-31 wholesale).
- Any widening of the addressable heading subset, slugging of rendered text (GTX-272), or
  Markdown link validation. The relative-link gate gap GTX-168 documents stays open either way.
- Conversion tooling. Migration is mechanical; the CHANGELOG note covers it.
