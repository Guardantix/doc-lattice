# Project-root symlink containment design

**Date:** 2026-07-13

## Problem

`discover_doc_paths` documents project-root containment for discovered Markdown files, but it
currently calls `safe_resolve(path, root)` inside the configured docs-root loop. A symlink under a
docs root is therefore skipped when its target is elsewhere in the same project. The warning says
the path escaped the project root even though it escaped only the docs root.

This can silently omit a valid project document and leave downstream ids or section references
unresolved.

## Design

Change `discover_doc_paths` to require the project's root explicitly:

```python
def discover_doc_paths(
    roots: Sequence[Path],
    ignore_globs: Sequence[str],
    project_root: Path,
) -> list[Path]:
```

For each candidate, discovery will continue to evaluate ignore globs against the candidate path
relative to the configured docs root. It will validate the candidate's resolved target with
`safe_resolve(path, project_root)`. The function will keep returning the symlink path rather than
the resolved target, preserving root-relative ignore behavior, cache keys, diagnostics, and file
identity as seen by callers.

Both the cached and uncached lattice loaders will pass `project.project_root`. No fallback or
inferred common ancestor will be provided because the project boundary is security-sensitive and
already available at every production call site.

## Security and errors

Configured docs roots remain validated against the project root during config loading. A document
symlink whose target remains inside that same boundary is accepted; one whose target resolves
outside it is still skipped before reading and emits the existing project-root escape warning.

This preserves the repository boundary that prevents untrusted configuration or Markdown paths
from steering reads and reconcile writes outside the project while removing the unintended
per-docs-root restriction.

## Testing

Regression coverage will prove that:

- Discovery includes a symlink under a docs root whose target is elsewhere inside the project.
- Discovery still warns and skips a symlink whose target is outside the project.
- Ignore globs remain relative to the configured docs root.
- Both cached and uncached lattice loading include a valid in-project symlinked document.

Implementation will follow red-green TDD: add the failing regression first, make the smallest API
and caller changes, then run the focused tests and the repository's full verification suite.
