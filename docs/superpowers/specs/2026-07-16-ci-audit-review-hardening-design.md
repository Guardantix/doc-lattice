# CI Audit Review Hardening Design

**Date:** 2026-07-16
**Status:** Approved for implementation

## Purpose

Close five verified correctness and safety gaps in GitHub CI audit and managed-artifact refresh:

1. A bare Bash line continuation can create an empty scanner word and hide a sensitive
   `doc-lattice` subcommand.
2. GNU `env` split-string options can construct a `doc-lattice` command that the scanner ignores.
3. Local repository inference reads only one `remote.origin.url` even when Git has several.
4. Workflow discovery interpolates repository-controlled filenames into some early diagnostics.
5. Refresh can publish over a concurrent change after its final byte comparison.

## Selected approach

### Scanner normalization and unsupported command constructors

The shell scanner will treat an unquoted backslash-newline followed by indentation as a lexical
separator when it occurs between shell words. This preserves ordinary continuation semantics while
preventing an indentation-only word from becoming the apparent subcommand.

`env -S` and `env --split-string` invoke GNU coreutils' separate argv mini-language. The scanner
will deliberately fail closed for every static spelling of that option rather than implement a
partial emulator whose unrecognized escape or quoting behavior could reintroduce a bypass. Thus a
workflow containing an `env` split-string wrapper cannot receive a successful audit result.

### Repository identity

The CLI will ask Git for every local `remote.origin.url` value using `git config --get-all` and
continue only when the decoded output contains exactly one nonempty URL. Explicit
`--repository OWNER/REPO` remains unchanged and never calls Git.

### Safe diagnostic rendering

A focused GitHub-CI display helper will JSON-escape repository-relative paths without their outer
quotes. Workflow parsing, CLI findings, and filesystem discovery will use this helper. Discovery
will retain a raw path for filesystem operations and model values while using only its escaped form
in every diagnostic, including races and decode failures.

### Refresh publication lock

`apply_changes` will acquire a nonblocking advisory lock on the repository root before mutating
managed artifacts and keep it through each final re-read and atomic replacement. The existing
reconcile mutation path uses the same directory-lock mechanism, so cooperating doc-lattice writers
cannot interleave a post-check edit with publication. Lock setup, contention, and unavailable
platform support fail closed instead of silently publishing without the protocol.

Portable `os.replace` has no compare-and-swap operation against arbitrary, uncooperative direct
filesystem writes. The lock therefore provides the strongest portable transaction boundary: all
doc-lattice mutators must participate, and the final comparison occurs inside that boundary.

## Alternatives rejected

- Emulating all GNU `env -S` grammar would add a second shell-like parser and risk divergent,
  fail-open behavior. Refusing that unsupported constructor is safer and smaller.
- A second unchecked byte comparison before `os.replace` preserves the same time-of-check to
  time-of-use window and is insufficient.
- Escaping only the cited symlink diagnostic would leave later discovery error paths vulnerable to
  the same terminal-control injection.

## Test strategy

Each change starts with a focused failing regression test:

- scanner and audit tests cover indented continuations and each split-string spelling;
- CLI tests use a real local Git configuration with multiple `origin` URLs;
- discovery tests exercise control characters in symlink, non-regular, oversized, read-race, and
  decode diagnostics;
- filesystem tests prove that an apply-time mutation lock spans the final compare and publish,
  rejects contention without changing user bytes, and preserves existing preflight checks.

After each minimal implementation step, run its focused test without coverage aggregation. Final
verification runs the complete test suite and all repository-required lint, format, type, boundary,
and version checks before commit and push.
