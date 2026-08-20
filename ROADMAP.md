# doc-lattice Roadmap

This document tracks future direction only. See [README.md](README.md) for shipped behavior,
[CHANGELOG.md](CHANGELOG.md) for release history and migrations, and
[ARCHITECTURE.md](ARCHITECTURE.md) for accepted decisions. Work items live in the issue
tracker; the GTX identifiers below are pointers, not restatements.

Adoption today is internal only. Several items below deliberately land before any push for
external users, because they are cheap while breaking changes cost nothing and expensive after.

Each release section below is one project in the issue tracker, and every open issue belongs to
exactly one of them. The ordering within a section is a real dependency order, recorded as blocker
relations on the issues themselves, so the tracker's unblocked queue and this document cannot
drift apart. An item with no stated dependency has none.

## 6.0: close the behavior window 5.0 left open

5.0.0 shipped ahead of this section rather than at the end of it: six admitted items were still
open at the tag. Two of them, GTX-212 and GTX-214, have landed on main since and wait in
`## [Unreleased]`, where [CHANGELOG.md](CHANGELOG.md) records what they changed. GTX-212 is
breaking, so the window is open again as a fact rather than by choice, and the next version is
6.0.0.

Admission is unchanged and still narrow on purpose: an item belongs here only if it is breaking,
or if it has to be true at the moment a major ships. Everything else waits for 6.x, which is what
keeps this release from staying open indefinitely. Applying that rule moved six issues back in
from the minor train, so nine are open here rather than three. Start with the two groups below that
GTX-113 waits on.

- Report a bad `--docs-root` or `--linear-team` from `init` as `VALIDATION_ERROR` rather than
  `CONFIG_ERROR` (GTX-216). It is the last raise site in that file naming a command-line value
  with a code that sends the user to a config file `init` has not written yet and never reads,
  and it is here for the reason GTX-112 and GTX-198 were: GTX-113 rewrites README against the
  codes the release actually prints, and moving a documented code afterwards would be a break in
  a minor.
- Close the four output sinks 5.0's group did not reach: reconcile's post-load re-parse warning
  (GTX-200), the load-cache write warning (GTX-221), the journal's own validation diagnostic
  (GTX-227), and reconcile's lock diagnostics (GTX-238). They moved here from the minor train
  because each changes the spelling of something the engine prints, which is the ground that
  admitted GTX-212. AD-36 leaves GTX-227's sink to that issue rather than widening its own scope,
  so it is sequenced by a recorded decision rather than by preference. None of the four sequences
  against another. Landing them here is what keeps the promise GTX-214 narrowed true at every sink
  the README pass is about to document.
- Settle the two edge cases where what breaks is the run's own contract rather than a spelling: a
  broken stderr pipe (GTX-201), and `PYTHONWARNINGS=error` (GTX-202). They moved with the four
  above, on a stricter reading of the same rule, since an exit status is as much a contract as a
  printed string. Neither sequences against the other.
- Make README describe what 6.0 actually prints and enforces (GTX-113). This still lands last.
  Seven open issues now block it -- GTX-216 and the six above -- on the rule that the README pass
  cannot land ahead of the behavior it documents; every other blocker either shipped in 5.0.0 or is
  already in `## [Unreleased]`.
- Bring this document and the release dependency graph back into agreement with what is open
  (GTX-213), so the preamble's claim that every open issue sits in exactly one release project is
  something a sweep confirms rather than something a hand pass asserted. The 5.0.0 boundary is
  what showed the claim was unchecked: it moved without this section moving with it.

## 6.x: make the honor-system rules mechanical

A train of minors, not a single release, and deliberately empty of user-visible behavior change.
Everything here is either a rule the project already asserts in prose and does not enforce, or a
follow-on that 5.0 unblocked. Nothing in this section gates the next major, so these ship in
whatever order the blocker graph permits.

Start at GTX-176. It is two points, and it is what makes every other CI change in this section
cheap.

- Collapse the eight matrix-generated required status checks behind three stable aggregator
  contexts (GTX-176), so branch protection stops carrying CI matrix values literally. Every other
  item here that adds or reshapes a CI job is sequenced behind it.
- Give the guard scripts the reach their names and CLAUDE.md imply (GTX-114), then guard the
  Migration-subsection release rule the same way (GTX-150) rather than leaving it as prose that
  release pressure can silently skip.
- Arm the section-identity gates in CI and record a bench baseline (GTX-108), then write the
  ordered upgrade tracks into AD-13 against the resulting reality (GTX-107). Arming first is what
  makes the procedure short: folding the compat constants into the cache staleness check removes a
  caveat the written procedure would otherwise have to teach.
- Verify or raise the declared floors for `typer`, `rich`, and `pydantic` (GTX-119). This is the
  one dependency posture AD-27 accepts unverified while AD-26 refuses to accept it for
  `ruamel.yaml`, and the two records should agree or say why they differ.
- Ship the relative-link and anchor check CLAUDE.md already requires (GTX-130), so a renamed
  heading breaks CI instead of silently breaking the deep anchor links the maintained documents
  now carry.
- Close the reconcile follow-ons the v2 journal unblocks: an operator remediation that works for
  the lost-journal double failure (GTX-146), and the provenance-guard simplification (GTX-127)
  with the sink-guard scope question sequenced behind it (GTX-128).
- Decide how a stale or deprecated action pin gets noticed at all (GTX-181) before building the
  narrower check that resolves each pinned SHA against its version comment (GTX-180), since the
  chosen mechanism may subsume it.
- Simplify the recipe now that 5.0 has rewritten it: a first-class Linear credential check, so
  an installation stops reading that answer out of a feature meant for something else (GTX-172),
  and step 6's run identification (GTX-171).
- Single-file work with no sequencing at all: reconcile's record lines (GTX-120), release-smoke
  contract coverage for `init` (GTX-142), fuzz-gate oracle precision (GTX-160), and confirming
  that an administrator receives security alerts (GTX-151).

## 7.0: the next behavior window

Opened when there is enough batched behavior change to justify a major, not on a schedule. Two
items sit here today. Each is large, each carries an unresolved decision rather than a known fix,
and neither should be allowed to hold 6.0 open.

- Decouple fetching `init`'s printed snippets from its directory-sensitive config write, and stop
  it scaffolding a nested `.doc-lattice.yml` when run from a subdirectory (GTX-153). The
  print-only half is additive and could be split forward into 6.x if adopters need it sooner;
  resolving the Git root in the ordinary branch changes zero-config behavior and needs this
  window.
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
