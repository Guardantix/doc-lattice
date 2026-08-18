# Reconcile: selectors, transactions, and recovery

`reconcile` is the only command that writes to your docs, and it only ever rewrites `seen` values
and the aliases that read them. This document covers the selector forms, the read-only dry-run
preview and its JSON plan, the write and durability mechanics of a real run, automatic and manual
recovery, and the transaction artifacts reconcile leaves behind.

## Selectors

Normal reconcile needs either a downstream id or `--all` (running it with neither is an error):

- **`reconcile DOWNSTREAM_ID`**: reconcile every drifting edge of one downstream node.
- **`reconcile DOWNSTREAM_ID --ref REF`**: narrow to a single upstream ref on that node, selected
  by resolved identity; refused if it targets a BROKEN edge.
- **`reconcile --all`**: clear every STALE/UNRECONCILED edge in the lattice. Skips BROKEN and
  already-OK edges, and skips a node's broken edge rather than failing the node, so one dangling
  ref never blocks the rest.
- **`reconcile --all --ref REF`**: reconcile matching drifting edges across every downstream
  node. Nonmatching, BROKEN, and already-OK edges are skipped; unlike the single-node form, no
  match is a successful no-op.
- **`reconcile --recover`**: perform recovery or cleanup for an outstanding transaction and exit
  without loading the lattice or planning a new batch. It cannot be combined with a downstream id,
  `--all`, `--ref`, or `--dry-run`; those combinations exit 2. `--format json` is supported.

## Dry-run previews

Add `--dry-run` to any normal selector above to preview the plan without writing: it prints
`would reconcile FILE: REF` per edge that would change (`nothing to reconcile` if none would),
and remains byte-, namespace-, and cache-read-only. It does not create, rewrite, recover, or remove
the journal or staged images, and it does not persist the optional load cache. If an outstanding
journal exists, dry-run exits 2, names it, and tells you to run `reconcile --recover` first without
loading the lattice.

Combine a safe dry-run with `--format json` for a machine-readable plan:
`{"dry_run": true, "reconciled": [{"path": ..., "ref": ..., "new_seen": ...}]}`, sorted by path
then ref. A real run with `--format json` emits the same shape with `"dry_run": false`, after the
durable commit, artifact cleanup, and lock release complete. Failed real batches emit no human
`reconciled` lines and no JSON success payload. A source conflict names the changed destination and
says whether rollback completed; an I/O or durability failure names the failed operation and says
whether rollback completed or recovery evidence remains.

## Write mechanics and durability

`reconcile` re-reads each downstream file fresh at write time and edits only the source bytes of
the targeted `seen` scalar, so your body, key order, comments, and list indentation survive
verbatim, and so does the file's line ending, which is what hashing compares. The one exception is
a file that already mixes endings: there is no single ending to restore, so a run that changes
anything in it rewrites the whole document in LF. An entry that inherits `seen` from another
through a `<<` merge key spells none of its own, so updating the entry that does update both.
Every rewrite is verified against the document it was planned to produce before anything is
staged, and one that would not reproduce it is refused rather than written, which fails the whole
batch. A real run then stages exact before and after images, publishes a `prepared` journal,
fingerprints each destination immediately before its replacement, and rejects a changed
destination as a conflict. The full batch is rolled back in reverse order if a conflict or
write/durability failure occurs before the committed marker. A destination counts as possibly
applied from the moment its replacement is attempted, not once that call returns, because the rename
lands before the directory synchronization that can still fail. Destinations the run never reached
are not rollback candidates, so an ordinary pre-replace conflict on one file is a complete rollback
of everything the run did touch rather than a partial one. After every replacement is durable, the
journal becomes `committed`; success output waits until committed cleanup and a clean advisory-lock
release have both completed.

[AD-31](ARCHITECTURE.md#ad-31-the-reconcile-rewriter-supports-a-declared-frontmatter-subset) owns
the normative rules behind that: the frontmatter syntax this rewriter supports, what it puts back
untouched, what a rewrite may mutate beyond the `seen` scalar, when a refusal is guaranteed, and
the compatibility rationale for all four. This section stays with what those rules mean for a run.

Every reconcile mode holds a nonblocking advisory lock on the existing project-root directory
through preflight, planning, and any recovery or commit. A competing invocation exits 2 with
`another reconcile is in progress; retry after it exits` and does not inspect or alter the active
transaction. The durability guarantee assumes a local filesystem with reliable advisory-lock,
atomic-rename, and directory-sync semantics. Network filesystems such as NFS may weaken or emulate
`flock`, so reconcile on them is outside this durability contract.

## Automatic recovery

A real reconcile checks for recovery immediately after config and lock setup, before loading the
lattice. A `prepared` journal rolls transaction-owned after images back to their exact before
images; unrelated edits are preserved. A `committed` journal keeps the committed destinations and
finishes artifact cleanup. Automatic recovery is reported once on stderr, then the newly requested
reconcile proceeds. This ordering ensures the new plan sees recovered files.

An incomplete automatic recovery stops the command instead. If any destination stayed unresolved,
or the run found orphaned artifacts, reconcile reports them on stderr and exits 2 without loading
the lattice, planning, or writing, since planning against a tree that was never fully restored
would reconcile from unrecovered bytes.

## Transaction artifacts

The project-root transaction journal is `.doc-lattice-reconcile.json`. It is written
pretty-printed, so an interrupted run leaves a file you can read without reformatting it. Its
`version` is `2`, its `state` is `prepared` or `committed`, and each entry under `entries` records
project-relative destination, before-image, and after-image paths plus full SHA-256 fingerprints.

A `provenance` object records what produced the transaction. Every field is required, and a
version 2 journal missing any of them is rejected rather than recovered with blanks:

- `created_at`: when preparation began, as an ISO 8601 timestamp string in UTC, for example
  `2026-08-17T12:00:00.123456Z`. The value must match that syntax: a naive timestamp, a nonzero
  offset, and anything spelling a number rather than a date-time are all rejected, so a
  hand-edited value cannot be reinterpreted as some other instant. Both `0` and `"0"` would
  otherwise read as 1970-01-01.
- `tool_version`: the doc-lattice version that wrote the journal.
- `selector`: the selection the batch was planned from, as `mode` (`all` or `downstream`),
  `downstream_id` (the node for a `downstream` selection, `null` for `all`), and `ref` (the single
  upstream ref the run narrowed to, or `null`). `--all` takes precedence over a downstream id, so a
  run given both records `mode: "all"`.

All three are captured once, when the transaction is prepared, and copied unchanged into the
committed marker, so a crash journal never disagrees with itself about when or why it ran.

Version 1 journals, which recorded no provenance, are still recoverable: recovery inspects the
declared version before validating, and both `prepared` and `committed` v1 journals roll back and
clean up exactly as they did before. Only version 2 is ever written, so a recovered v1 journal is
removed rather than upgraded in place. A journal declaring any other version is rejected as
invalid and left for manual inspection.

Temporary files use these exact patterns:

```gitignore
.doc-lattice-reconcile.json
.doc-lattice-reconcile.json.*.tmp
.*.doc-lattice-before.*.tmp
.*.doc-lattice-after.*.tmp
```

Before and after images are staged beside each destination, so the last two patterns ignore staged
images in nested document directories as well as at the project root. `doc-lattice init` always
prints this block and tells you to append it to `.gitignore`; it never reads, creates, appends to,
or overwrites `.gitignore` itself.

## Manual recovery

After an interrupted run, use this workflow:

1. Stop any other reconcile and run `doc-lattice reconcile --recover` from the project root. A safe
   rerun of a normal real reconcile also performs this recovery before lattice loading.
2. A valid `prepared` journal reports `rolled back reconcile transaction: JOURNAL`, or
   `partially rolled back reconcile transaction: JOURNAL` when a destination stayed unresolved; a
   valid `committed` journal reports `cleaned committed reconcile transaction: JOURNAL`; no journal
   reports `nothing to recover: JOURNAL`, or `no reconcile journal to recover: JOURNAL` when the
   scan reported an orphan or a failure. A full rollback, a committed cleanup, and a clean
   no-journal run exit 0. A partial rollback, any orphaned artifact, and any scan failure exit 2,
   whichever journal state the run started from.
3. For machine-readable recovery, add `--format json`. The complete stdout object contains exactly
   `action`, `journal`, `restored`, `already_before`, `unresolved`, `orphans`, and `scan_errors`,
   with no additional keys, for example `{"action": "none", "journal": "PATH", "restored": 0,
   "already_before": 0, "unresolved": [], "orphans": [], "scan_errors": []}`. `action` is `none`,
   `rolled_back`, `partially_rolled_back`, or `cleaned_committed`. `restored` and `already_before`
   count rolled-back destinations; `unresolved`, `orphans`, and `scan_errors` are the sorted
   details behind a nonzero exit.

## Partial rollback

Rolling back a `prepared` journal classifies each destination against the images the transaction
recorded. A destination still holding the after image is **restored** from its before image. A
destination already equal to its before image is **already before**: nothing to do, and a full
rollback all the same. A destination matching neither image, **including one that no longer
exists**, is **unresolved**: the transaction cannot account for its current contents, so recovery
neither guesses nor overwrites.

Any unresolved destination makes the whole run a partial rollback. It reports
`partially rolled back reconcile transaction: JOURNAL` on stdout, names every unresolved
destination on stderr as a project-relative path, and exits 2. A partial rollback deletes nothing:
the journal and every remaining staged image are retained, because the journal is the only record
of which destination, path, and digest belong together. The one image that unavoidably disappears
is a before image consumed by restoring its own destination.

Recovery stays idempotent while a destination is unresolved: rerunning `--recover` reports the same
partial result and changes nothing. Rerunning on its own is therefore not a way out, and until you
take one of the two below, `--recover`, a real reconcile, and a dry run all refuse to proceed,
because the journal is still outstanding.

- **Finish the rollback.** Restore each named destination to its recorded before or after image,
  then rerun `--recover`. Once no entry is unresolved, that run performs the ordinary full cleanup
  and exits 0.
- **Keep the current bytes.** Recovery cannot resolve contents it has no record of, so it will
  never accept them for you. Inspect the journal to confirm what the transaction owned, then move
  the journal and the stages it names aside yourself. With no journal outstanding, reconcile runs
  again normally.

## Orphaned artifacts

Every recovery also scans the project for transaction artifacts matching the patterns above that no
retained journal accounts for. The scan runs after journal handling, not only when no journal was
found, so a publication interrupted between linking the journal and removing its helper stage
reports both the recovered journal and the leaked helper in one invocation. It covers staged images
in nested document directories as well as journal publication temporaries.

Orphans are reported on stderr as project-relative paths and exit 2. Nothing is ever deleted:
inspect each artifact and remove it yourself after confirming it is not a destination. When no
journal is present and orphans are found, the summary line reads `no reconcile journal to recover`
rather than `nothing to recover`, so that a completeness claim is never made about an unclean tree.
If the scan cannot enumerate part of the project, the failure is reported the same way rather than
silently narrowing the search.

A malformed or unsafe journal exits 2 and is not deleted. Inspect the named journal, destinations,
and staged files; restore each destination or deliberately preserve its current contents; then move
the invalid journal aside only after that manual restoration or preservation and rerun
`doc-lattice reconcile --recover`.

Missing, corrupt, nonregular, or otherwise unauthenticated staged evidence also exits 2 without
unsafe cleanup. Preserve the journal and available staged files, restore or correct the required
evidence named by the diagnostic, or manually preserve the affected destination, then rerun
`doc-lattice reconcile --recover`. Do not delete evidence or guess which image is authoritative
before inspecting its recorded fingerprint. If rollback itself fails, the diagnostic names the
remaining artifacts and the destination that still needs manual attention.
