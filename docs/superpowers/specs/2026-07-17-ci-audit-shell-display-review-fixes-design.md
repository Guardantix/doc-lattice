# CI Audit Shell and Display Review Fixes Design

**Date:** 2026-07-17
**Status:** Approved for autonomous implementation

## Purpose

Close six verified review gaps without weakening the bounded, non-executing scanner or making
valid non-executing workflow steps fail audit:

1. Bash extglob operators are split at `(` before the scanner records expansion provenance.
2. Three-digit ANSI-C octal escapes are not reduced to Bash's eight-bit value before NUL checks.
3. Eager uv help and version options are treated as unknown launcher grammar.
4. Eager reconcile help is reported as a mutating reconcile invocation.
5. The GNU `env` split-string precheck looks through an attached `-a` value.
6. Refresh diffs emit Unicode format and bidirectional controls verbatim.

## Selected approach

### Unsupported extglob syntax

The word parser will recognize an unquoted, unescaped `?(`, `*(`, `+(`, `@(`, or `!(` opener
before `(` reaches command-group handling. Because the scanner does not track shell option state or
parse extglob pattern grammar, it will stop with one explicit incomplete-scan reason rather than
misrepresenting the opener as a literal word followed by a subshell.

Detection belongs in `_parse_word`, where `_ShellWordBuilder.active_syntax` still distinguishes
active operators from quoted or escaped text. Rejecting at `_command_operator_at` would lose that
provenance and create false positives for protected characters.

### ANSI-C octal byte semantics

Three-digit octal escapes will be reduced with `value & 0xFF` before the shared ANSI-C character
validator runs. This models Bash's byte result for values from `\400` through `\777`, including
wrapped ASCII delimiters, and lets the existing NUL rejection catch `\400` as a truncating escape.
Hex and Unicode escapes keep their current rules because they do not use the same three-digit
octal byte conversion.

### Eager uv exits

Static uv option grammar will distinguish options that consume values, ordinary flags, and eager
non-command options. Global `-h`/`--help` and `-V`/`--version` stop before subcommand resolution.
Standalone `uvx` recognizes both help and version aliases, while `uv run` and `uv tool run`
recognize their help aliases. Stop recognition remains inside each option walk, so a help-looking
token consumed as a known value or placed after `--` is not misclassified as eager.

### Eager reconcile help

The reconcile argument walk will treat an unconsumed `--help` as effectively non-mutating, using
the same safe bit currently used for effective `--dry-run`. Known value-taking options still
consume their successor first, and `--` still ends option parsing, so `--config --help` and
`-- --help` remain conservatively mutating classifications.

The helper will be named for effective non-mutation rather than only dry-run, making the boolean's
expanded meaning explicit without changing the scanner's public invocation tuple or audit policy.

### GNU env attached argv0

The split-string short-cluster precheck will stop when it reaches `a`, just as it already stops at
`u` and `C`. All three options consume the remainder of the word as an attached value. The existing
validated env option walker already implements this grammar, so no second parser is introduced.

### Unicode-safe refresh diffs

Diff rendering will preserve ordinary Unicode while rendering every Unicode general-category
`Cf` format character visibly. BMP controls use lowercase `\uNNNN`; supplementary-plane controls
use lowercase `\UNNNNNNNN`. Existing C0, DEL, and C1 controls retain lowercase `\xNN`, and LF plus
the final CR in a CRLF record retain their line-ending behavior.

Escaping rather than rejecting lets a maintainer safely inspect and replace a hostile marked
artifact. General category `Cf` covers both bidirectional controls and other invisible formatting
characters without escaping normal letters, emoji, or combining marks.

## Alternatives rejected

- Rejecting `(` whenever the preceding source byte resembles an extglob operator would also reject
  protected operators because command-operator parsing no longer knows their quote provenance.
- Special-casing only `@(`, `+(`, and `!(` would leave the same parser desynchronization for the
  other Bash extglob operators.
- Rejecting every octal value above 255 would be safe but unnecessarily reject syntax the scanner
  can model exactly with Bash's byte reduction.
- Adding help and version strings to ordinary uv flag sets would skip them and scan impossible
  trailing payloads instead of stopping command resolution.
- Treating every reconcile occurrence of `--help` as eager would be wrong when a value-taking
  option consumes it or `--` makes it positional.
- Escaping only the named bidi ranges would omit other invisible Unicode format controls that can
  conceal or join repository-controlled preview text.

## Test strategy

Each production change begins with a focused regression that is observed failing:

- all five extglob openers fail closed before command grouping, with quoted syntax as a positive
  control;
- `\400` fails with the established ANSI-C NUL diagnostic in executable and subcommand words;
- global uv, standalone uvx, `uv run`, and `uv tool run` help/version aliases stop without a payload
  or incomplete scan, including trailing text that eager handling ignores;
- reconcile help is safe only when it is an unconsumed option before `--`;
- `env -aS doc-lattice linear` classifies the Linear invocation rather than seeing `-S`; and
- representative BMP and supplementary `Cf` controls are absent from rendered diffs and replaced
  by visible escapes.

Focused scanner and filesystem suites follow each minimal implementation. Final verification runs
the full pytest suite, Ruff check and format check, `ty`, typing-boundary validation, version
synchronization, and `git diff --check`. The requirement audit then inspects the complete diff,
commits the implementation, and pushes the current branch without force.
