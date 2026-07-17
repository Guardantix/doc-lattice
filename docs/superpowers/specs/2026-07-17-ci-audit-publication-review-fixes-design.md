# CI Audit and Publication Review Fixes Design

**Date:** 2026-07-17
**Status:** Approved for autonomous implementation

## Purpose

Close six verified review gaps without weakening the existing fail-closed audit or durable
publication contracts:

1. Computed GitHub expression keys can resolve to a protected Linear secret without spelling its
   name in workflow text.
2. GNU `env` abbreviations and clustered short options can consume a value that the scanner
   mistakes for the executable.
3. Bash `exec` accepts `-a` after other short options and still consumes an `argv[0]` value.
4. Bash `builtin` can invoke the supported `command` and `exec` wrappers.
5. A preflighted `current` artifact can change before a mixed publication batch obtains its lock.
6. Creating `.github` or `.github/workflows` is not durable until the directory containing each
   new entry is synchronized.

## Selected approach

### Secret references

The global audit will fail closed on a `secrets[...]` lookup whose key is not one static quoted
GitHub secret name. Existing static lookups for unrelated secret names remain allowed, while exact
protected names continue to produce the existing `LINEAR_SECRET_REFERENCE` finding. This closes
computed forms such as `secrets[format(...)]` regardless of which workflow event or environment
makes the resulting secret available.

Reserving `environment: doc-lattice-linear` only for the canonical job was considered. It closes
the reported environment-scoped example but leaves the same computed lookup able to reach a
misconfigured repository or organization secret. Classifying the unprovable lookup itself matches
the repository-wide secret-reference policy and avoids depending on one remote storage scope.

### GNU env option grammar

The scanner will parse the command-position effects of the GNU `env` option surface rather than
treating every unknown option as valueless. It will:

- resolve exact and uniquely abbreviated long options;
- consume separate or attached values for `--unset`, `--chdir`, and `--argv0`;
- parse short clusters left to right, where `u`, `C`, and `a` consume the remainder of the word or
  the next word;
- retain the existing fail-closed treatment of `-S` and `--split-string`;
- skip valueless and optional-value signal options without consuming a following word; and
- fail closed on unknown, ambiguous, dynamic, or malformed option grammar.

This follows the current GNU Coreutils `env` contract, including the newer `-a`/`--argv0` form,
instead of fixing only the two reported spellings.

### Bash exec and builtin wrappers

The `exec` parser will walk static short-option clusters. `c` and `l` are valueless; `a` consumes
an attached suffix or the next word as the replacement `argv[0]`. Unknown static options fail
closed, and a dynamic separate `-a` value preserves command-position ambiguity.

The prefix resolver will unwrap `builtin`, including `builtin --`, only when its target is
`builtin`, `command`, or `exec`. A dynamic builtin target is followed speculatively and marked
ambiguous so a reachable `doc-lattice` payload fails closed. Other literal builtins remain outside
the documented direct-command surface.

The incremental prefix tracker will mirror these rules so command-position state and final
resolution cannot diverge.

### Locked batch consistency

When a batch contains at least one create or replace, `apply_changes` will acquire the existing
root-bound publication lock and revalidate every `current` entry before performing any mutation.
Revalidation uses the same descriptor-relative containment, type, inode, size, and bounded-byte
checks as replacement. A changed current entry aborts the batch before a later create or replace,
even when caller input places the current entry after a mutation.

An all-current batch remains a no-op and does not acquire a lock because it publishes no state.
After prevalidation, creates and replacements retain caller input order and the existing
partial-state diagnostic contract.

### Durable ancestor creation

When descriptor-relative ancestor lookup first observes an absent entry, publication will create
or accept a racing creator, validate the resulting real directory, then call `fsync` on the open
parent descriptor before opening and descending into the child. This is repeated for every new
level. The existing leaf-directory synchronization performed by atomic file publication remains
unchanged.

A parent synchronization failure aborts before the artifact file is published and is wrapped in a
stable `ConfigError` with the canonical artifact path and partial-state remediation note.

## Alternatives rejected

- Matching only the reported computed expression would leave equivalent functions, indexing
  expressions, and concatenations unclassified.
- Adding only `--uns` and `-iu` special cases would leave `--chdir`, `--argv0`, and other clusters
  with the same command-position bug.
- Treating `builtin` as a generic inert prefix would misclassify ordinary builtins; emulating
  `eval`, `source`, or arbitrary builtins would exceed the scanner's documented scope.
- Revalidating `current` entries in caller order could write an earlier mutation before discovering
  a stale current entry. A read phase before the write phase gives the batch a coherent lock-time
  baseline.
- Synchronizing only the final workflow directory cannot persist its entry in `.github`, nor the
  `.github` entry in the repository root.

## Test strategy

Each production change begins with a focused regression that is run and observed failing:

- audit tests cover the reported `format(...)` lookup and preserve a static unrelated lookup;
- scanner and pull-request audit tests cover abbreviated/clustered GNU `env`, clustered `exec -a`,
  and `builtin command`/`builtin exec` payloads;
- filesystem tests mutate a current artifact after lock acquisition and place it after a create in
  input order, proving all current entries are checked before writes;
- filesystem syscall-order tests prove every successful ancestor `mkdir` is immediately followed
  by an `fsync` of the same parent descriptor, and a synchronization failure occurs before an
  artifact file is written.

Focused green checks follow each minimal implementation. Final verification runs the full pytest
suite, Ruff check and format check, `ty`, typing-boundary validation, version synchronization, and
`git diff --check` before commit and push.
