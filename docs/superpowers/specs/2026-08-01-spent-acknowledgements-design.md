# Spent corpus-differential acknowledgements fail nobody; live ones still refuse

**Date:** 2026-08-01
**Status:** Approved (approach chosen in session; amends AD-22's staleness rule)

## Problem

AD-22's comparison fails whenever an acknowledgement matches no divergence at full scale. The
acknowledging pull request cannot remove its own entries, because its own comparison needs them to
match. So the entries land in the base, and the next pull request touching any replayed input
inherits a red required check and a ~four-minute replay to delete someone else's one-line entry.
The burden lands on an unrelated author by construction.

Reverting to auto-expiry is off the table: commit 96e1155 deliberately replaced silent expiry with
a hard failure because an entry left on file is a standing authorization. A post-merge cleanup job
was considered and rejected: it needs a new single-revision prune mode (the tool refuses to compare
a revision with itself), it creates a bot-authored write path into a security fixture, and it does
not close the window anyway, since pull requests opened before the cleanup merges still fail.

## Decision

Narrow the failure using evidence the comparison already holds. For an unmatched entry
`(digest, A -> B)`, the base record's verdict for `digest` separates two situations:

- **Spent.** The base scores the digest at anything other than `A` (usually `B`: the transition
  landed; possibly a third verdict that superseded it). The entry is provably inert against this
  base: it can only match a divergence whose base verdict is `A`, and this base never produces one.
  Spent entries are reported, dropped by `--write-acknowledgements`, and do not fail the run.
- **Live or unverifiable.** The base still scores the digest at `A` (the exact move it authorizes
  is still available and nobody made it: a genuine standing authorization), or the base record does
  not score the digest at all (the corpus no longer draws the script, so nothing can be proven).
  These fail the comparison exactly as every unmatched entry does today.

Why this is not a weakening: the state AD-22 feared is an old entry becoming an excuse again. That
requires the base to return to `A`, and a reversion is itself a divergence that must be reviewed
and acknowledged. The moment such a reversion lands, the old entry flips from spent to live and the
very next comparison refuses it, earlier than the old rule caught the danger. The refusal now fires
exactly when the authorization is exercisable, on the pull request responsible for that state,
instead of firing once, on the wrong author, when it provably is not.

Burden placement under the new rule:

- Acknowledging PR: entries match its divergences; green.
- Next unrelated PR: entries are spent; green, spent count reported.
- Next verdict-moving PR using `--write-acknowledgements`: spent entries dropped for free.
- Grammar change that stops drawing an acknowledged script: entries become unverifiable and fail
  that PR, which is the change that made them unauditable; its `--write-acknowledgements` run
  drops them.
- Reversion of an acknowledged transition: the reversion divergence itself needs acknowledging;
  once landed, the old entry is live and the next comparison refuses it.

## Mechanics

In `scripts/corpus_differential.py`:

- New pure function splitting unmatched entries into `(spent, live)` given a
  `dict[digest, verdict]` built from the base record's cases. `live` includes both the
  still-at-`A` case and the digest-not-scored case.
- `report()` takes the base verdict mapping as a new required parameter and returns
  `(unacknowledged, spent, live)`. It prints spent entries as informational rows, distinct from
  the failing kind, with the base verdict shown; live entries keep the standing-authorization
  framing. Under `--allow-shrunk-corpus` the caller passes `None` for the mapping and `report()`
  prints unmatched entries undifferentiated, as today, returning them all as spent so nothing
  fails; classification is not performed at all on a shrunk draw.
- `_compare_command()` fails on `live` (not on `spent`) at full scale. Under
  `--allow-shrunk-corpus` behavior is unchanged: no unmatched entry is fatal, none is dropped on
  write, and no spent/live judgment is made, because absence from a shrunk draw proves nothing.
- `acknowledgement_document()` semantics unchanged: at full scale every unmatched entry (spent and
  live alike) is dropped, since removing an authorization is always safe; under a shrunk corpus
  every unmatched entry is kept.
- Exit status stays `EXIT_DIVERGED` for the live failure.

## Documentation

- AD-22: rewrite the "It does not expire on its own" paragraph to carry the spent/live split and
  the reactivation argument. The four disclosed limits are untouched.
- CLAUDE.md: replace the paragraph saying the next pull request has to drop the entry.
- Module docstring: update the stale-handling sentence.

## Tests

In `tests/test_corpus_differential.py` (update existing stale tests, add):

- Spent entry, base at candidate verdict: exit OK, reported as spent, dropped on write.
- Superseded entry, base at a third verdict: treated as spent.
- Live entry, base still at the entry's base verdict: `EXIT_DIVERGED`, message names the standing
  authorization.
- Unverifiable entry, digest absent from the base record: `EXIT_DIVERGED`.
- Shrunk corpus: no unmatched entry fatal, write keeps them (existing behavior pinned).
- Full-scale write drops spent and live alike.

`tests/test_workflow_corpus_differential.py` is expected to be untouched; verify.
