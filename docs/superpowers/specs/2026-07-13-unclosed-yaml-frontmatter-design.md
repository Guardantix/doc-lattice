# Fail Closed on Unclosed YAML Frontmatter

## Problem

`split_frontmatter` currently returns `(None, original_text)` both when a Markdown file has no
opening frontmatter fence and when it starts with `---` but has no closing fence. The loader treats
`None` as an ordinary untracked document, so malformed lattice metadata can silently remove a node
and all of its edges from the graph.

The cached loader can preserve that result as a non-node entry. A parser-only fix would therefore
leave cache entries produced by the old behavior capable of hiding the malformed document.

## Design

Make the frontmatter split operation source-aware. A document whose first logical line is not
`---` continues to return `(None, original_text)`. A document with matching opening and closing
fences continues to return its raw YAML and body. A document with an opening fence and no closing
fence raises `UnreadableDocError` with the source path and an instruction to add the closing `---`
fence.

Every production caller passes the source path into the split operation. This keeps the distinction
at the parsing boundary and gives uncached loads, cache misses, and reconcile's fresh-read validation
one error type and message. The existing CLI `ProjectError` boundary maps that exception to exit 2
for every lattice-loading command.

Increment `CACHE_VERSION` so cache files that may contain a fail-open non-node verdict are discarded
before lookup. No schema field changes; the version bump invalidates cached derivations whose
semantics changed.

## Error Handling

The error is an `UnreadableDocError` with code `UNREADABLE_DOC` and this stable shape:

```text
unclosed YAML frontmatter in <path>: add a closing '---' fence
```

The message names both the source and the required correction. YAML syntax and model-validation
errors retain their existing behavior.

## Verification

Regression coverage will prove:

- the parser raises the source-naming project error for an unclosed opening fence;
- a document without an opening fence remains valid untracked Markdown;
- uncached and cache-enabled loads raise the same exception and byte-identical message;
- legacy cache entries are invalidated instead of preserving the fail-open verdict;
- `check`, `lint`, `impact`, `reconcile`, `graph`, and `linear` all exit 2 before doing command work;
- malformed metadata containing an `id` cannot silently omit that node; and
- README documents the missing-close error and exit-code behavior.

## Scope

This change does not alter valid YAML parsing, lattice validation, graph construction, cache schema,
or the documented `cache_trust_stat` caveat for content changed behind an unchanged stat tuple.
