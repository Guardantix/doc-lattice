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

Two boundary rules bound that inventory claim, and each is a rule rather than a fact about a
current issue. An issue still in Triage has no project yet; it acquires one on leaving, at the
same moment it is given this repository's tracker label, so a projectless Triage issue is outside
this document until it lands in a project. And Linear's `Deferred` workflow state is a
backlog-type state, so a Deferred issue is non-terminal and counts against the open set this
document names. That tracker state and `## Deferred enhancements` below are different things: the
section is a prose parking area, not a state. GTX-129, today's only Deferred-state issue, happens
to sit in both, but either could hold without the other.

The ordering within a section is a real dependency order, recorded as blocker relations on the
issues themselves, so the tracker's unblocked queue and this document cannot drift apart. An item
with no stated dependency has none, and a dependency whose other end has shipped or been canceled
is satisfied history rather than current order, so it is not restated here.

Neither of those is a hand assertion. Both are swept against the live tracker: the identifiers
named here against its open set, and every stated order against the blocker relations in both
directions. That open set is every non-terminal issue carrying this repository's tracker label,
under the two boundary rules above. Last swept 2026-08-25 against 40 open issues, with the
evidence recorded on GTX-304 and its PR.

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
window was held open. Three more were admitted on 2026-08-23, each under the second half of the
rule. The pre-publication smoke of the built distribution (GTX-239) and the artifact-action bumps
off their Node.js 20 releases (GTX-263) both change what the release run itself executes, so both
had to be on main before the 6.0 run rather than after it. The doc-link gate's false rejection of
deep links that render and resolve on GitHub (GTX-277) sat directly under the two documentation
passes that close this window, either of which it could have falsely blocked.

Eleven of those fourteen have since landed and wait in `## [Unreleased]` beside GTX-212 and
GTX-214: reconcile's lock diagnostics (GTX-238, which GTX-212's own review spawned into this
section), `PYTHONWARNINGS=error` (GTX-202), the broken stderr pipe (GTX-201), the load-cache
write warning (GTX-221), the journal's own validation diagnostic (GTX-227), the codes `init`
reports for a value it rejects before writing anything (GTX-216), the README pass itself
(GTX-113, PR #301), `init --print-only` with the nested-scaffold refusal beside it (GTX-153), the
pre-publication smoke of the built wheel (GTX-239), the doc-link gate's acceptance of every
heading form GitHub links to (GTX-277), and the artifact-action bumps themselves (GTX-263, PRs
#309 and #312). One was canceled rather than worked: reconcile's post-load re-parse warning
(GTX-200), which the PR that spawned it had already fixed. GTX-263 is the one landed change whose
issue stays open: `build-release` and `publish` execute only on a real version-bump run, so the
6.0 release run is the first thing that can prove the bumped pins, and the issue has moved to
`doc-lattice v6.x`, where the 6.x section names it as that validation checkpoint. GTX-213, the
sweep the previous revision of this inventory rested on, closed with that sweep on 2026-08-20.

That accounting leaves two open items, and they are the whole remaining release sequence: GTX-304,
filed 2026-08-24 as GTX-213's successor and the sweep this revision of the document rests on,
then GTX-279, then the tag, with nothing else admitted in between. GTX-304 blocks GTX-279 on the
tracker, so the order is a recorded relation rather than this sentence alone, and GTX-304 closes
with its own sweep and so is not carried below as pending work.

- Make README describe what 6.0 actually prints and enforces at the tag (GTX-279). GTX-113 did
  this once, and PR #301 landed it, but the window was then held open and behavior has landed
  since, so the same rule applies again: the README pass cannot land ahead of the behavior it
  documents, and this one lands last, behind the GTX-304 sweep whose settled inventory it reads.
  It audits the `## [Unreleased]` entries merged after GTX-113 rather than repeating it,
  GTX-153's `--print-only` and nested-scaffold refusal included.

## 6.x: make the honor-system rules mechanical

A train of minors, not a single release, and deliberately empty of breaking change. Everything
here is either a rule the project already asserts in prose and does not enforce, or a follow-on
that 5.0 and 6.0 unblocked. Several items here do change what a run prints -- how often a ruamel
warning repeats (GTX-206), which document three reconcile diagnostics name (GTX-257), what `init`
prints outside a Git worktree (GTX-298) -- but each is additive or cosmetic and none moves a
contract 6.0's README pass will have documented. Anything that moves one belongs in 6.0 by the
admission rule above, which is how the two sections partition. GTX-237 is the one item here whose
decision could reach that bar, and it moves if it does.

Nothing in this section gates 6.0, and no ordering edge leaves it. The order inside it is the
blocker graph's: GTX-308, GTX-309, and GTX-310 each block GTX-107, GTX-127 blocks GTX-128,
GTX-263 blocks GTX-267, and everything else ships in whatever order it lands.

- Arm the section-identity gates. GTX-108 is the umbrella here and holds no work of its own: its
  original acceptance bars could not be met by the mechanisms it named, so it was split on
  2026-08-24 into the slug-generator check inside the required `Code quality` context (GTX-308),
  a bench baseline with a named reference environment (GTX-309), and a section-derivation
  fingerprint in the load cache so an adapter edit cannot serve stale anchors (GTX-310). The
  three are unsequenced against each other, and each blocks GTX-107, which then writes the
  ordered upgrade tracks into AD-13 against the resulting reality. Arming first is what makes the
  procedure short: a derivation fingerprint in the cache staleness check removes a caveat the
  written procedure would otherwise have to teach.
- Close or record the remaining slugging gap: `github_heading_ids` slugs raw inline source where
  GitHub slugs rendered text (GTX-272).
- Finish the CI rollouts that are already half true: complete the `Runtime floor compatibility`
  requirement on `main`'s protected checks (GTX-289), run the shipped test suite from an unpacked
  sdist so an exclusion gap cannot ship silently (GTX-292), lint `scripts/` in CI (GTX-271),
  decide whether `scripts/` joins the measured coverage source (GTX-268), and give the two CI
  scripts' failure-guarded reporting contract a single owner (GTX-301).
- Close the release-machinery follow-ons the 6.0 admissions left behind: the validation
  checkpoint for the bumped artifact actions (GTX-263), open because the 6.0 release run is the
  first run that can exercise them, then the decision it blocks on whether the workflow-only
  action pins carry exact version comments (GTX-267); the release smoke's check and lint
  assertions, which today accept a command that only names the CLI (GTX-269); and RELEASING.md's
  local verification smoke, weaker than what the pipeline itself has proved since GTX-239
  (GTX-306).
- Widen the doc-link gate where it still trails GitHub's renderer: headings written as raw HTML
  (GTX-305) and relative image destinations, which break the same way links did (GTX-270).
- Close the reconcile follow-ons the v2 journal unblocked: the provenance-guard simplification
  (GTX-127) with the sink-guard scope question sequenced behind it (GTX-128), fuzz-gate oracle
  precision (GTX-160), and the three reconcile diagnostics that do not name the document they now
  carry (GTX-257).
- Simplify the recipe now that 5.0 has rewritten it: a first-class Linear credential check, so an
  installation stops reading that answer out of a feature meant for something else (GTX-172), and
  step 6's run identification (GTX-171).
- Make `init`'s printed guidance hold together on its own: it restates README's adoption commands
  with nothing enforcing the match (GTX-297), and it prints a full adoption recipe outside a Git
  worktree, where none of it can be followed (GTX-298).
- Close the diagnostics follow-ons the sink passes spawned, none sequenced against another:
  restore the per-site deduplication a non-reused-anchor ruamel warning loses on the frontmatter
  load path (GTX-206); give the one dependency-authored warning message that reaches human output
  the escape-free spelling AD-34 requires (GTX-242); require every production Rich `print()` call
  to state `soft_wrap` explicitly (GTX-288); and decide whether a caught exception's own path
  rendering is a display spelling this project owes or a depended-on CPython convention it should
  record (GTX-237).
- Correlate or retire the declared bounds still policed by hand: the Python floor's remaining
  machine-consumed copies (GTX-311), the yaml-compatibility ceiling cell against the declared
  ruamel.yaml upper bound (GTX-290), the test guard describing a yaml matrix AD-33 removed
  (GTX-241), the Google-style docstring rule, asserted but unenforced (GTX-299), and CLAUDE.md's
  unconditional custom-exception rule against the justified `PipeClosed` exception it already
  lives with (GTX-244).
- Decide whether this document's inventory and ordering claims get the mechanical guard GTX-304's
  sweep supplied the motivating evidence for (GTX-245), and confirm that an administrator
  receives security alert notifications (GTX-151).

## 7.0: the next behavior window

Opened when there is enough batched behavior change to justify a major, not on a schedule. One
item sits here today. It is large, it carries an unresolved decision rather than a known fix, and
it should not be allowed to hold 6.0 open. GTX-153 sat beside it until 2026-08-22, when its
direction settled and it moved into 6.0 above.

- Decide whether doc-lattice tracks its own maintained documents as a lattice (GTX-168). The
  decision is the deliverable and is recorded as an AD either way. The relative-link and anchor
  gate the decline path hands the link-checking question back to landed with GTX-130, so the
  decision no longer waits on anything.

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
