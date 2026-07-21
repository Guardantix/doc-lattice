# Provenance: July 2026 bash-parser benchmark

## Archived files and hashes

SHA-256 (`sha256sum` output) for each archived artifact:

```
9003578888d50bb0a195a9e5e942a9d27ffd48233ad9bc646c1065790c80409d  bash_parser_primary_source_review.md
ebab53f46380e4778c083a998fe4310a86d19817869c3d39c8805f9485a62cb5  bash_parser_review_methodology.md
6ad4ff27b893e385210fdfcf208152c56315f2b1fa8237da61c6bf94c48229e9  bash_parser_acceptance_matrix.csv
faa0cb44ec4301ef75b7717cc0b5d2c4938b91691290f990a376218be8bb7320  bash_parser_acceptance_results.json
```

## Source and retrieval

No URL exists for these files: they were never attached to the issue #100 thread. They were
supplied directly by the project owner on 2026-07-20 as the artifact set referenced and
audited in issue #100 comment
<https://github.com/Guardantix/doc-lattice/issues/100#issuecomment-5014847709>.

Retrieval date: 2026-07-20.

This directory pins `-text` in a local `.gitattributes` so Git preserves the artifacts
byte-for-byte, including the CSV's CRLF row terminators, against the repository's global
`eol=lf` normalization. This directory is also excluded from the repository's
`end-of-file-fixer` pre-commit hook, because the JSON artifact ends without a trailing newline
and archived evidence must not be normalized.

Before archiving, the four supplied files were checked against the SHA-256 values above (a
match confirms the bundle is the audited set, not a substitute) and the acceptance tallies
were independently recomputed from `bash_parser_acceptance_matrix.csv` and
`bash_parser_acceptance_results.json`. Both sources agree with each other and reproduce the
published totals across all 234 parser/case executions (78 cases times 3 parsers) exactly, as
correct/false-safe/indeterminate/false-positive: `shfmt` 3.13.1 = 69/1/8/0,
`tree-sitter-bash` 0.25.1 = 58/5/14/1, `bashlex` 0.18 = 40/12/25/1. This confirms the archived
copies are the same audited set described in the comment above and are internally consistent,
not independently reproducible: nobody outside the original benchmark run can re-execute it
from these files alone, because the adapter implementations, benchmark runner, exact parser
command lines, raw AST fixtures, dependency locks, and environment manifest are not part of
the artifact set.

## Scope

These artifacts cover only the external parser candidates evaluated for issue #100. They are
evidence for the decision record and are never gate inputs: no code path in this repository
reads or depends on any file in this directory.
