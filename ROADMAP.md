# doc-lattice Roadmap

This document owns direction: the themes the project intends to pursue, and the things it has
decided not to do. It is deliberately coarse.

See [README.md](README.md) for shipped behavior, [CHANGELOG.md](CHANGELOG.md) for release history
and migrations, and [ARCHITECTURE.md](ARCHITECTURE.md) for accepted decisions.

**This document is not an inventory.** Work items, their status, their sequencing, and their
ownership live in the issue tracker, which is the only place any of that is kept current. Nothing
below names an issue, assigns a version, or claims to be complete. Earlier revisions did all
three, and keeping them honest required sweeping this document against the tracker on a schedule.
That cost more than it returned, and a roadmap that goes stale between sweeps is worse than one
that never promised to be current.

Version numbers are decided at release time from what actually landed, not assigned in advance
here. Adoption is internal only, which keeps breaking changes cheap; several directions below are
worth doing while that is still true, because each is expensive once outside adopters exist.

## Direction

**Finish the documentation-integrity story.** The engine tracks declared derivation edges between
documents and reports when a downstream section goes stale against its upstream. It does not read
ordinary Markdown links as derivation edges; its first-class `links` command checks relative links
and heading anchors separately. Applying both gates to this repository's own maintained documents
is what lets the project stand behind documentation integrity as a whole rather than one half of
it.

**Track our own documents.** The repository does not currently use the engine on its own
maintained documents, which is both a credibility gap and a missed detector: the drift these
documents produce is exactly the class the engine reports. The decision to adopt is made, and the
shipped link command gives self-adoption one coherent gate over both declared derivation edges and
ordinary Markdown links.

**Make adoption legible to a first-time user.** Everything a new adopter meets before the engine
runs, chiefly what `init` writes and prints, should be correct and followable in the context where
it is read. This is small, unglamorous work with an outsized effect on whether the tool is
adoptable by anyone who did not write it.

**Keep release and packaging confidence ahead of the release cadence.** The pipeline should prove
what it ships rather than assume it: that the packaged distribution is the one that was tested,
and that a failed release leaves a state a maintainer can resume from rather than unwind by hand.
This work is invisible when it succeeds and is the reason releases stay boring.

## Deferred

Ideas judged worth keeping but not worth building yet. Nothing here is scheduled, and an entry
staying on this list indefinitely is a normal outcome rather than a backlog failure.

- A display-prefix lint.
- An anchor-inventory view (document, heading, resolved anchor, and whether it came from an
  explicit marker or was generated), letting users diff section identity across versions. Worth
  building when a slug-affecting upgrade is actually planned, and not before.
- Narrowing reconcile's orphan artifact scan, which walks the whole project root on every real
  reconcile. The breadth is deliberate, since an orphan carries no journal naming its location.
  Revisit only if it becomes a real cost on a large repository.

## Out of scope by design

These are settled non-goals. They are recorded here so the question does not get reopened without
new information.

- **A `split` command.** Splitting a document stays a manual or agent-driven edit. Stable ids and
  `impact` already make the operation safe without dedicated command surface.
- **Hardening against a hostile local writer.** The containment and locking model defends against
  accidents, symlink confusion, and concurrent runs. An adversarial co-tenant on the same
  filesystem is out of scope, recorded with the threat model in AD-2 of
  [ARCHITECTURE.md](ARCHITECTURE.md).
- **A productized managed CI offering.** Superseded by the hand-installable recipe that
  [MANAGED_CI.md](MANAGED_CI.md) owns, which first shipped in 5.0.0. The scanner half of that
  earlier ambition moved to its own project, recorded in AD-25 of
  [ARCHITECTURE.md](ARCHITECTURE.md).
