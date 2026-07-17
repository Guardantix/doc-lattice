# CI Audit, Terminal, and Durability Review Fixes Design

**Date:** 2026-07-17
**Status:** Approved for autonomous implementation

## Purpose

Close four verified review gaps without weakening the existing fail-closed audit, readable refresh
preview, or durable publication contracts:

1. An actively brace- or glob-expanded executable word can run `doc-lattice` while the scanner
   reports a complete result with no invocation.
2. Bash truncates an ANSI-C quoted word at a decoded NUL, while the scanner retains the NUL and
   suffix and can therefore misclassify the executable or subcommand.
3. Refresh diffs emit repository-controlled terminal control characters verbatim.
4. A create retry does not synchronize an ancestor entry left behind by a prior failed parent
   `fsync`.

## Selected approach

### Executable-word expansion

The scanner will reject active brace or glob expansion whenever the affected word occupies an
executable slot in the supported direct-command grammar. This includes a top-level command,
supported Bash wrappers and coprocesses, and `uv run` or `uvx` payloads. Expanded arguments after a
literal subcommand remain allowed because they cannot change which command or subcommand runs.

The check will use the `_ShellWord.active_argv_expansion` provenance already produced by the
bounded lexer. Resolver layers may continue speculatively past that word so an already-supported
later payload retains its more specific fail-closed diagnostic. If executable resolution otherwise
ends without a payload, the resolver rejects any active expansion it traversed instead of
discarding the ambiguity.

### ANSI-C quoted NUL

The ANSI-C escape decoder will reject any decoded numeric or control escape whose value is zero.
The rejection occurs before the decoded character is appended to a shell word, so octal, `\x`,
`\u`, `\U`, and `\c@` spellings receive one consistent incomplete-scan result in executable,
subcommand, option, and argument positions.

This matches Bash's inability to place NUL in an argument. Retaining `chr(0)` is not a conservative
model because Bash executes the prefix before the NUL and discards the suffix.

### Terminal-safe refresh diffs

Diff rendering will replace every C0 control except LF, as well as DEL and every C1 control, with a
visible lowercase `\xNN` escape. A CR immediately before a record's final LF is the sole additional
exception so CRLF-versus-LF differences keep their existing exact representation; an embedded CR
is escaped.
The transformation applies to both prior repository content and desired artifact content before
the CLI prints the diff.

Escaping is preferred to rejecting the preview. A maintainer must be able to inspect and replace a
malicious marked artifact safely, and visible escapes preserve the location and byte-level meaning
of the unexpected content without letting the terminal interpret it.

### Durable create retry

Every ancestor traversed for an artifact create will synchronize its open parent descriptor after
the child entry has been validated, whether the child was created by the current attempt or already
exists. A create retry cannot distinguish an old durable directory from one left by an interrupted
attempt, so resynchronizing is the only local operation that proves the entry is durably linked
before descent and leaf publication.

Replacement and current-artifact validation continue to use `create=False` and do not add parent
syncs. A synchronization failure still aborts before the artifact write with the canonical path and
partial-publication remediation note.

## Alternatives rejected

- Matching only `{doc-lattice,}` would leave the same bypass behind `command`, `exec`, `builtin`,
  `time`, `coproc`, `uv run`, and `uvx`.
- Comparing only the decoded executable basename would not model brace expansion cardinality and
  would still miss a protected subcommand or launcher payload.
- Stripping NUL and its suffix in the scanner would emulate one Bash effect but silently accept a
  source construct that cannot be represented faithfully in an operating-system argument.
- Rejecting control-bearing artifacts would make safe preview and managed replacement of a hostile
  repository file impossible.
- Removing a directory after failed synchronization could delete a racing creator's directory or
  other content. Remembering directories only within one process would not help a later retry.

## Test strategy

Each production change begins with a focused regression that is run and observed failing:

- scanner tests cover expanded executable words at direct, Bash-wrapper, coprocess, `uv run`, and
  `uvx` positions while preserving expanded post-subcommand arguments;
- ANSI-C tests cover `\0`, `\x00`, `\u0000`, `\U00000000`, and `\c@` in executable and protected
  subcommand words;
- filesystem diff tests cover the complete C0/DEL/C1 set, embedded CR, and preservation of the
  existing CRLF behavior; and
- a two-attempt create test leaves `.github` behind after a synthetic root-directory `fsync`
  failure, then proves the retry synchronizes the root entry before publishing the artifact.

Focused green checks follow each minimal implementation. Final verification runs the full pytest
suite, Ruff check and format check, `ty`, typing-boundary validation, version synchronization, and
`git diff --check` before the implementation commit and push.
