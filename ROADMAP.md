# doc-lattice Roadmap

This document tracks future direction only. See [README.md](README.md) for shipped behavior,
[CHANGELOG.md](CHANGELOG.md) for release history and migrations, and
[ARCHITECTURE.md](ARCHITECTURE.md) for accepted decisions. Work items live in the issue
tracker; the GTX identifiers below are pointers, not restatements.

Adoption today is internal only. Several items below deliberately land before any push for
external users, because they are cheap while breaking changes cost nothing and expensive after.

Each release section below maps to exactly one project in the issue tracker: `## 6.0` to
`doc-lattice v5/v6`, `## 6.x` to `doc-lattice v6.x`, and `## 7.0` to `doc-lattice v7`. Every open
issue belongs to exactly one of those three projects, and every one of them is named in this
document. `## Deferred enhancements` and `## Out of scope by design` are not projects: an item
parked there keeps whatever project its release section would have given it, which is why GTX-129
is a `doc-lattice v6.x` issue listed outside the 6.x section.

The ordering within a section is a real dependency order, recorded as blocker relations on the
issues themselves, so the tracker's unblocked queue and this document cannot drift apart. An item
with no stated dependency has none, and a dependency whose other end has shipped or been canceled
is satisfied history rather than current order, so it is not restated here.

Neither of those is a hand assertion. Both are swept against the live tracker: the identifiers
named here against its open set, and every stated order against the blocker relations in both
directions. That open set is every non-terminal issue carrying this repository's tracker label,
which is what an item acquires on leaving Triage, at the same time it is given a project. Last
swept 2026-08-20 against 28 open issues, with the evidence recorded on GTX-213.

## 6.0: close the behavior window 5.0 left open

5.0.0 shipped ahead of this section rather than at the end of it: six admitted items were still
open at the tag. Two of them, GTX-212 and GTX-214, have landed on main since and wait in
`## [Unreleased]`, where [CHANGELOG.md](CHANGELOG.md) records what they changed. GTX-212 is
breaking, so the window is open again as a fact rather than by choice, and the next version is
6.0.0.

Admission is unchanged and still narrow on purpose: an item belongs here only if it is breaking,
or if it has to be true at the moment a major ships. Everything else waits for 6.x, which is what
keeps this release from staying open indefinitely. Applying that rule admitted nine here rather
than three, six of them moved back in from the minor train. Two more were admitted on 2026-08-22:
GTX-153 moved forward from 7.0 once its direction settled on a breaking refusal rather than a
Git-root resolution, and GTX-279 was filed because the README pass had already landed when the
window was held open.

Seven of those nine have since landed and wait in `## [Unreleased]` beside GTX-212 and GTX-214:
reconcile's lock diagnostics (GTX-238, which GTX-212's own review spawned into this section),
`PYTHONWARNINGS=error` (GTX-202), the broken stderr pipe (GTX-201), the load-cache write warning
(GTX-221), the journal's own validation diagnostic (GTX-227), and the codes `init` reports for a
value it rejects before writing anything (GTX-216), and the README pass itself (GTX-113, PR
#301). One was canceled rather than worked:
reconcile's post-load re-parse warning (GTX-200), which the PR that spawned it had already fixed.
Three are still open -- GTX-153 and GTX-279 below, and GTX-213, the sweep this document's
inventory now rests on, which closes with that sweep and so is not carried below as pending work.

- Decouple fetching `init`'s printed snippets from its directory-sensitive config write, and stop
  it scaffolding a nested `.doc-lattice.yml` when run from a subdirectory (GTX-153). The
  print-only half is additive. The other half is what admits the issue here: ordinary `init` run
  from a subdirectory of a repository whose root already holds a config refuses instead of
  writing, and a run that used to exit 0 and write now exits 2, which is a zero-config behavior
  change and so belongs in a major. The refusal keeps `init`'s current-directory contract and
  resolves no Git root, so it needs no ARCHITECTURE decision, only a CHANGELOG migration note
  and the README changes the issue lists. The issue's thread records the direction.
- Make README describe what 6.0 actually prints and enforces at the tag (GTX-279). GTX-113 did
  this once, and PR #301 landed it, but the window was then held open and behavior has landed
  since, so the same rule applies again: the README pass cannot land ahead of the behavior it
  documents, and this one lands last, after GTX-153 above and anything else admitted before the
  tag. It audits the `## [Unreleased]` entries merged after GTX-113 rather than repeating it.

## 6.x: make the honor-system rules mechanical

A train of minors, not a single release, and deliberately empty of breaking change. Everything
here is either a rule the project already asserts in prose and does not enforce, or a follow-on
that 5.0 and 6.0 unblocked. Several items here do change what a run prints -- reconcile's record
lines (GTX-120), `init`'s guidance (GTX-188), a new frontmatter annotation (GTX-204), how often a
ruamel warning repeats (GTX-206) -- but each is additive or cosmetic and none moves a contract
6.0's README pass will have documented. Anything that moves one belongs in 6.0 by the admission
rule above, which is how the two sections partition. GTX-237 is the one item here whose decision
could reach that bar, and it moves if it does.

Nothing in this section gates 6.0, so these ship in whatever order the blocker graph permits. One
ordering edge does leave the section: GTX-130 blocks GTX-168 in 7.0, for the reason recorded
there.

Start at GTX-176. It is two points, and it is what makes every other CI change in this section
cheap.

- Collapse the eight matrix-generated required status checks behind three stable aggregator
  contexts (GTX-176), so branch protection stops carrying CI matrix values literally. Three items
  here are sequenced directly behind it: GTX-114, GTX-108, and GTX-119.
- Give the guard scripts the reach their names and CLAUDE.md imply (GTX-114), then guard the
  Migration-subsection release rule the same way (GTX-150) rather than leaving it as prose that
  release pressure can silently skip.
- Arm the section-identity gates in CI and record a bench baseline (GTX-108), then write the
  ordered upgrade tracks into AD-13 against the resulting reality (GTX-107). Arming first is what
  makes the procedure short: folding the compat constants into the cache staleness check removes a
  caveat the written procedure would otherwise have to teach.
- Make the `Runtime floor compatibility` context required on `main` (GTX-119). The matrix that
  verifies every declared dependency floor now runs on every pull request, but branch protection
  is a separate control plane, so until that rule is updated a red floor cell can still merge.
  RELEASING.md owns the rollout and the readback that proves it landed.
- Ship the relative-link and anchor check CLAUDE.md already requires (GTX-130), so a renamed
  heading breaks CI instead of silently breaking the deep anchor links the maintained documents
  now carry.
- Close the reconcile follow-ons the v2 journal unblocks: an operator remediation that works for
  the lost-journal double failure (GTX-146), and the provenance-guard simplification (GTX-127)
  with the sink-guard scope question sequenced behind it (GTX-128).
- Simplify the recipe now that 5.0 has rewritten it: a first-class Linear credential check, so
  an installation stops reading that answer out of a feature meant for something else (GTX-172),
  and step 6's run identification (GTX-171). GTX-175 named the gate-activation step in README and
  MANAGED_CI but left what `init` itself prints untouched, so the CLI still orders against an act
  its own output never defines (GTX-188).
- Close the diagnostics follow-ons the 5.0 and 6.0 sink passes spawned, none sequenced against
  another: carry the failing document's path on `FrontmatterError` so a frontmatter failure can be
  annotated on the file that caused it instead of exiting with an unattached stderr line
  (GTX-204); restore the per-site deduplication a non-reused-anchor ruamel warning loses on the
  frontmatter load path, where it costs one identical line per document (GTX-206); and decide
  whether a caught exception's own path rendering is owed a display spelling or whether the
  interpreter's is a depended-on assumption this project should record (GTX-237).
- Single-file work with no sequencing at all: reconcile's record lines (GTX-120), release-smoke
  contract coverage for `init` (GTX-142), fuzz-gate oracle precision (GTX-160), confirming that an
  administrator receives security alerts (GTX-151), and correcting CLAUDE.md's time-boundary rule,
  which tells a contributor to recreate a module that is already there (GTX-233).

## 7.0: the next behavior window

Opened when there is enough batched behavior change to justify a major, not on a schedule. One
item sits here today. It is large, it carries an unresolved decision rather than a known fix, and
it should not be allowed to hold 6.0 open. GTX-153 sat beside it until 2026-08-22, when its
direction settled and it moved into 6.0 above.

- Decide whether doc-lattice tracks its own maintained documents as a lattice (GTX-168). The
  decision is the deliverable and is recorded as an AD either way. GTX-130 lands first, because
  the decline path hands the link-checking question back to it.

## Deferred enhancements

- Display-prefix lint. An optional future enhancement.
- An anchor-inventory view (document, heading, resolved anchor, marker or generated) to let
  users diff section identity across versions. Deferred until a slug-affecting upgrade is
  actually planned; the migration policy itself is part of GTX-107.
- Narrowing reconcile's orphan artifact scan, which walks the whole project root on every real
  reconcile (GTX-129). The breadth is deliberate, because an orphan carries no journal naming its
  location. Revisit only if it shows up on a large repository.

## Out of scope by design

- `split` command. Splitting a document remains a manual or agent-driven edit. Stable ids and
  `impact` make the operation safe without dedicated command surface.
- Hardening against a hostile local writer. The containment and locking model defends against
  accidents, symlink confusion, and concurrent runs; an adversarial co-tenant on the same
  filesystem is out of scope, recorded with the threat model in AD-2 of
  [ARCHITECTURE.md](ARCHITECTURE.md).
- A productized managed CI offering. Superseded by the recipe GTX-109 wrote, which first shipped
  in 5.0.0 and which [MANAGED_CI.md](MANAGED_CI.md) owns; the earlier scanner half of that ambition
  moved out in AD-25.
