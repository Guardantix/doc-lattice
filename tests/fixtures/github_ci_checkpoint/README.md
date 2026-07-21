# Issue #100 predeclaration checkpoint

This directory holds the frozen evaluation inputs for the issue #100 allowlist recognizer
evaluation, as defined by the "Predeclaration checkpoint" section of
[the allowlist recognizer design spec](../../../docs/superpowers/specs/2026-07-19-allowlist-recognizer-design.md).
Per that spec, checkpoint files are immutable for the remainder of PR A once the checkpoint
commit lands; if any artifact must change, the evaluation restarts from a new checkpoint
commit and prior results are discarded. `MANIFEST.sha256` (generated and verified by
`scripts/checkpoint_manifest.py`) is the auditable proof of that immutability.

- `limits.json` satisfies checkpoint item 6, the numeric caps and the work limit with its
  charge definitions.
- `benchmark_protocol.md` satisfies checkpoint item 7, the benchmark protocol.
- `category_d_exceptions.json` satisfies checkpoint item 8, the prelabeled exceptions for
  replay divergence category (d), empty absent a prelabeled entry.
- `MANIFEST.sha256` satisfies checkpoint item 9, the SHA-256 manifest over items 1 through 8.
