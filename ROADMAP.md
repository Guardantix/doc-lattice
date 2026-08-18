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

## 5.0: retire the managed CI product and close the behavior window

The release in flight. `## [Unreleased]` already carries breaking changes, so the next version is
5.0.0. Admission is narrow on purpose: an item belongs here only if it is breaking, or if it has
to be true at the moment a major ships. Everything else waits for 5.x, which is what keeps this
release from staying open indefinitely.

Nothing in the release sequences behind a single item any more. GTX-163 has removed the managed
GitHub and Linear CI code and repaired every document that described it, GTX-126 has moved the
reconcile journal to v2, and GTX-110 has retired the orphaned dirfd persistence family and
recorded the filesystem threat model in AD-2. Start with the diagnostics group below, since the
error codes it settles are what GTX-113 waits on.

- Bring the diagnostics an external user meets first up to the standard the transaction layer
  already sets. The load boundary introduces a frontmatter-specific error code and so has to land
  in this window (GTX-112). Warning presentation matters now that GTX-102 makes the id-less skip
  fire on every adopter run rather than in an edge case (GTX-124). Control bytes in a document
  filename are the one repo-controlled string reaching a diagnostic without passing the parser,
  and the fix belongs at message construction so it survives whichever renderer prints it
  (GTX-125).
- Stop a document's tracked status depending on whether the optional `ruamel.yaml.clib`
  accelerator happens to be installed (GTX-148). Which files count as tracked is user-visible, so
  settling it is a breaking change either way it is settled.
- Make README describe what 5.0 actually prints and enforces (GTX-113). This lands last, once the
  error codes and the documentation owners above have stopped moving.
- Confirm on a published artifact that step 1 of [MANAGED_CI.md](MANAGED_CI.md) exits 0 against
  the 5.0 pin (GTX-169). GTX-164's walk found it exits 2 on every release available at the time,
  and the recipe is the sole installation path, so a failure here is release-blocking rather than
  a documentation nit.

## 5.x: make the honor-system rules mechanical

A train of minors, not a single release, and deliberately empty of user-visible behavior change.
Everything here is either a rule the project already asserts in prose and does not enforce, or a
follow-on that 5.0 unblocks. Nothing in this section gates the next major, so these ship in
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
- Simplify the recipe once 5.0 has rewritten it: a first-class Linear credential check, so an
  installation stops reading that answer out of a feature meant for something else (GTX-172), and
  step 6's run identification (GTX-171).
- Single-file work with no sequencing at all: reconcile's record lines (GTX-120), release-smoke
  contract coverage for `init` (GTX-142), fuzz-gate oracle precision (GTX-160), and confirming
  that an administrator receives security alerts (GTX-151).

## 6.0: the next behavior window

Opened when there is enough batched behavior change to justify a major, not on a schedule. Two
items sit here today. Each is large, each carries an unresolved decision rather than a known fix,
and neither should be allowed to hold 5.0 open.

- Decouple fetching `init`'s printed snippets from its directory-sensitive config write, and stop
  it scaffolding a nested `.doc-lattice.yml` when run from a subdirectory (GTX-153). The
  print-only half is additive and could be split forward into 5.x if adopters need it sooner;
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
- A productized managed CI offering. Superseded by the recipe GTX-109 wrote, which first ships
  in 5.0 and which [MANAGED_CI.md](MANAGED_CI.md) owns; the earlier scanner half of that ambition
  moved out in AD-25.
