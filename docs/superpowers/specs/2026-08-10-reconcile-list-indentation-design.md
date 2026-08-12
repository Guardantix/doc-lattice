# Reconcile List Indentation Design

## Goal

Make a reconcile update change only targeted `seen` scalars while preserving the input
indentation of `derives_from` and every unrelated frontmatter collection. Update the reconcile
contract and changelog to state that guarantee.

## Constraints

- Keep `reconcile.py` pure and leave transaction, journal, recovery, hashing, and scaffold behavior
  unchanged.
- Continue to reject malformed fresh frontmatter with the existing actionable errors.
- Preserve comments, key order, the Markdown body, and both column-zero and two-space block-list
  layouts.
- Preserve mixed indentation, such as a two-space `derives_from` list beside a column-zero
  `tickets` list.
- Existing and missing `seen` values must both be supported.

## Considered Approaches

### Fixed round-trip dumper indentation

Configure ruamel with `mapping=2`, `sequence=4`, and `offset=2`. This preserves two-space nested
lists, but rewrites column-zero lists and every unrelated block sequence. It cannot satisfy both
accepted input styles.

### Dynamically selected dumper indentation

Choose the emitter-wide setting from `derives_from.lc.col`. This preserves an isolated
`derives_from` list in either accepted style. It still rewrites unrelated collections when a file
mixes indentation styles, so it violates the contract that only `seen` changes.

### Localized source edits

Keep ruamel parsing for defensive validation, but use the parsed YAML node source marks to locate
targeted `seen` values. Replace an existing scalar's exact source span or insert a missing key into
its containing mapping, then apply edits from the end of the frontmatter toward the beginning.
This is the selected approach because it leaves all unrelated source text outside the edit spans
untouched.

## Design

`apply_reconcile()` will continue to split the document and load frontmatter with a fresh
round-trip YAML parser. The loaded mapping remains the authority for validation, ref matching, and
no-op detection. A composed YAML node tree from the same raw frontmatter supplies source offsets
without introducing filesystem effects.

For each validated `derives_from` entry whose ref appears in the update plan:

- If `seen` already equals the planned hash, record no edit.
- If `seen` exists with a different value, replace only that scalar node's source span with the new
  hash.
- If `seen` is absent from a block mapping, insert `seen: HASH` at the mapping's end using the
  mapping key indentation already present in that entry.
- If `seen` is absent from a flow mapping, insert `, seen: HASH` immediately before its closing
  brace.

The planned hashes are lowercase hexadecimal strings, so plain YAML scalars are unambiguous.
Applying source edits in descending offset order prevents an earlier edit from invalidating later
offsets. After the raw frontmatter is edited, the existing opening fence, closing fence, and body
reattachment path remains unchanged.

Malformed frontmatter continues to fail before any source edit is returned. A document with no
frontmatter, no `derives_from`, no matching ref, or only already-current values remains an exact
no-op.

## Testing

Exact-output tests will cover:

- two-space `derives_from` indentation;
- column-zero `derives_from` indentation;
- mixed `derives_from` and `tickets` block-list indentation;
- existing and missing `seen` values;
- comments, key order, untargeted edges, and body preservation; and
- `plan_rewrites()` output whose only changed line is `seen` after newline normalization.

The focused reconcile, commit, and transaction suites will guard the pure writer and durability
boundary. The full repository test, coverage, lint, format, type, boundary, and version-sync gates
will run before publication.

## Documentation

[RECONCILE.md](../../../RECONCILE.md) will state that round-trip reconcile preserves list
indentation in addition to the body, key order, and comments. [CHANGELOG.md](../../../CHANGELOG.md)
will record the fix under `Unreleased`.
