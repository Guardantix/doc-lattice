# doc-lattice Roadmap

This document tracks future direction only. See [README.md](README.md) for shipped behavior,
[CHANGELOG.md](CHANGELOG.md) for release history and migrations, and
[ARCHITECTURE.md](ARCHITECTURE.md) for accepted decisions. Work items live in the issue
tracker; the GTX identifiers below are pointers, not restatements.

Adoption today is internal only. Several items below deliberately land before any push for
external users, because they are cheap while breaking changes cost nothing and expensive after.

## Now: close out 4.x

- Bump the internal adopters' pinned workflows to 4.1.0, which shipped the accumulated
  `init` changes and reconcile rewriter fixes.
- Make recovery reporting truthful: a partial rollback must be distinguishable from a full
  one, and recovery must never destroy the evidence an operator still needs (GTX-97, with
  test coverage for the realistic crash states, GTX-98). The journal format then moves to
  v2, so a crash journal records when, by what version, and from which selector it was
  written, and a v1 journal stays recoverable across the upgrade (GTX-126).
- Reorder the release pipeline so a failed gate cannot strand an immutable tag, and put the
  unguarded release steps under contract tests (GTX-103).

## Next: the 5.0.0 breaking-change window

Adoption is still internal, so the breaking-change window is open and cheap. The behavior
changes below batch into one 5.0.0 release rather than trickling out across minors. They are
independent of each other; co-membership in the release is not a sequencing constraint.

- Stop silently dropping documents whose frontmatter lacks an `id` (GTX-102).
- Make `check`'s human output problem-only by default, matching `lint`. Default stdout loses
  the per-edge `OK` rows; the verdict line GTX-41 added keeps the full counts visible, so a
  clean run stays explicit rather than silent (GTX-55).

## Harden before external adoption

- Freeze reconcile's supported YAML subset as an accepted decision, then stand up a
  round-trip fuzz gate so rewriter edge cases surface in CI instead of in user documents
  (GTX-101, GTX-100, with the reparse gate itself guarded by GTX-99).
- Bring load-boundary diagnostics up to the standard the transaction layer already sets, and
  close the README gaps around error codes and strict parsing (GTX-112, GTX-113).
- Publish the operational surface an external user would need: security reporting, a
  bad-release response, and the accounts a release depends on (GTX-105); document the
  adopter upgrade path (GTX-104).
- Bound the runtime dependencies and record the pinning rationale (GTX-115); extend the
  guard scripts to cover what their names imply (GTX-114).

## Insure the section-identity pin

Section identity is pinned by design ([ARCHITECTURE.md](ARCHITECTURE.md) AD-13). The exposure
is the maintenance path, not the pin: make the slugger data generator reproducible offline on
a recorded Node version (GTX-106), write the upgrade procedure down (GTX-107), and arm the
generator, parity, and benchmark gates in CI (GTX-108) before an upstream clock forces the
work under duress.

## Simplify: retire the managed CI product

The managed GitHub and Linear CI surface has zero installations, and the hand-installable
snippet already delivers the same gates. Retire the product to a documented recipe in a
staged deprecate-then-remove (GTX-109), then consolidate the persistence primitives its
removal orphans and record the filesystem threat model (GTX-110). [MANAGED_CI.md](MANAGED_CI.md)
remains the owner of whatever survives as the recipe.

## Deferred enhancements

- Display-prefix lint. An optional future enhancement.
- An anchor-inventory view (document, heading, resolved anchor, marker or generated) to let
  users diff section identity across versions. Deferred until a slug-affecting upgrade is
  actually planned; the migration policy itself is part of GTX-107.

## Out of scope by design

- `split` command. Splitting a document remains a manual or agent-driven edit. Stable ids and
  `impact` make the operation safe without dedicated command surface.
- Hardening against a hostile local writer. The containment and locking model defends against
  accidents and concurrent runs; an adversarial co-tenant on the same filesystem is out of
  scope, recorded with the threat model in GTX-110.
- A productized managed CI offering. Superseded by the recipe once GTX-109 lands; the
  earlier scanner half of that ambition already moved out in AD-25.
