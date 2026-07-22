# Successor relabel report for the acceptance corpus

Re-derivation of every acceptance row against the frozen successor tables (S8, gate 1).
Deterministic output; see `scripts/derive_successor_labels.py`.

## Label counts

- must-certify: 53
- intentional-exit-2: 28
- outside-direct-marker-contract: 6

## Applied-delta counts

- parse-ambiguity: 2
- rule1-traverse: 43
- rule4-malformed-tail: 1
- rule5-heredoc-guard: 6
- rule6-carried: 12
- s3.3-word-text: 1
- s6.2-d2-reachability: 1
- s6.5-launcher-parity: 9
- scope-gap: 1
- table-refuse: 11

## Rows changed versus D3/live baseline (67)

| # | description | baseline | successor | invocations | delta |
| - | ----------- | -------- | --------- | ----------- | ----- |
| 0 | ansi-c executable | intentional-exit-2 | must-certify | [["linear", false]] | s3.3-word-text |
| 2 | elif condition | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 3 | while condition | intentional-exit-2 | must-certify | [["check", false]] | rule1-traverse |
| 4 | until condition | intentional-exit-2 | must-certify | [["check", false]] | rule1-traverse |
| 5 | time reserved word | intentional-exit-2 | intentional-exit-2 | [] | table-refuse |
| 6 | coproc reserved word | intentional-exit-2 | intentional-exit-2 | [] | table-refuse |
| 7 | case arm | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 9 | double-quoted substitution | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 10 | assignment-only substitution | intentional-exit-2 | must-certify | [["reconcile", false]] | rule1-traverse |
| 11 | nested substitution | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 12 | locale-quoted substitution | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 13 | escaped substitution literal | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 15 | inner single-quoted substitution literal | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 16 | comment then active command | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 17 | backticks inside substitution comment | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 18 | comment line then active command | intentional-exit-2 | must-certify | [["check", false]] | rule1-traverse |
| 21 | nested legacy substitution | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 22 | legacy substitution comment literal | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 23 | legacy substitution command after comment | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 26 | parameter parenthesis does not close substitution | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 29 | parameter text resembling heredoc | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 30 | parameter arithmetic shift | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 31 | arithmetic expansion shift | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 32 | arithmetic command shift | intentional-exit-2 | intentional-exit-2 | [] | scope-gap |
| 33 | legacy arithmetic shift | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 38 | unbalanced arithmetic command runs a nested subshell | intentional-exit-2 | intentional-exit-2 | [] | parse-ambiguity |
| 44 | plain heredoc body is data | intentional-exit-2 | must-certify | [["check", false]] | rule1-traverse |
| 45 | unquoted heredoc expands modern substitution | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 46 | quote characters do not quote unquoted heredoc body | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 47 | escaped dollar in unquoted heredoc | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 48 | quoted heredoc suppresses modern substitution | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 49 | unquoted heredoc delimiter word removes continuation | intentional-exit-2 | intentional-exit-2 | [] | rule5-heredoc-guard |
| 50 | double-quoted heredoc delimiter word removes continuation | intentional-exit-2 | intentional-exit-2 | [] | rule5-heredoc-guard |
| 51 | single-quoted heredoc delimiter word preserves continuation | intentional-exit-2 | intentional-exit-2 | [] | rule5-heredoc-guard |
| 52 | ansi-quoted heredoc suppresses substitution | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 53 | unquoted heredoc expands backticks | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 54 | quoted heredoc suppresses backticks | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 55 | hash does not comment unquoted heredoc expansion | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 56 | nested unquoted heredoc | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 57 | nested quoted heredoc | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 58 | multiple heredocs retain expansion policy and ordering | intentional-exit-2 | must-certify | [["linear", false], ["lint", false]] | rule1-traverse |
| 59 | unquoted heredoc continuation suppresses physical delimiter | intentional-exit-2 | intentional-exit-2 | [] | rule5-heredoc-guard |
| 60 | unquoted heredoc continuation forms delimiter | intentional-exit-2 | intentional-exit-2 | [] | rule5-heredoc-guard |
| 61 | unquoted heredoc continuation forms command substitution | intentional-exit-2 | intentional-exit-2 | [] | rule5-heredoc-guard |
| 62 | here-string substitution | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 63 | here-string literal | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 64 | input process substitution | intentional-exit-2 | intentional-exit-2 | [] | table-refuse |
| 65 | output process substitution | intentional-exit-2 | intentional-exit-2 | [] | table-refuse |
| 66 | process substitution literal argument | intentional-exit-2 | intentional-exit-2 | [] | table-refuse |
| 67 | named-fd redirection before executable | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 68 | redirection before subcommand | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 69 | redirection before uv payload | intentional-exit-2 | must-certify | [["linear", false]] | rule1-traverse |
| 70 | dry-run is redirection target | intentional-exit-2 | must-certify | [["reconcile", false]] | rule1-traverse |
| 71 | dry-run is here-string redirection word | intentional-exit-2 | must-certify | [["reconcile", false]] | rule1-traverse |
| 74 | substitution in redirection target executes | intentional-exit-2 | must-certify | [["check", false]] | rule1-traverse |
| 75 | multiline double-quoted literal | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 76 | multiline single-quoted literal | intentional-exit-2 | must-certify | [] | rule1-traverse |
| 77 | complete command before malformed substitution | intentional-exit-2 | intentional-exit-2 | [["check", false]] | rule4-malformed-tail |
| 78 | uv tool short option before selector is intentional exit 2 | live-incomplete | intentional-exit-2 | [] | s6.5-launcher-parity |
| 79 | uv tool long option before selector is intentional exit 2 | live-incomplete | intentional-exit-2 | [] | s6.5-launcher-parity |
| 80 | uv tool value option before selector is intentional exit 2 | live-incomplete | intentional-exit-2 | [] | s6.5-launcher-parity |
| 81 | uv tool option before non-run selector is intentional exit 2 | live-incomplete | intentional-exit-2 | [] | s6.5-launcher-parity |
| 82 | uv tool dynamic value option before selector is intentional exit 2 | live-incomplete | intentional-exit-2 | [] | s6.5-launcher-parity |
| 83 | bare uv tool install remains non-candidate | live-certified | must-certify | [] | s6.5-launcher-parity |
| 84 | uvx no-sync is intentional exit 2 | live-incomplete | intentional-exit-2 | [] | s6.5-launcher-parity |
| 85 | uv tool run no-sync is intentional exit 2 | live-incomplete | intentional-exit-2 | [] | s6.5-launcher-parity |
| 86 | uv run no-sync remains certified | live-certified | must-certify | [["linear", false]] | s6.5-launcher-parity |

## Owner-adjudicate rows (12)

These rows are not fully determined by the frozen contracts and carry a
fail-closed proposal for Rick's checkpoint review.

- Row 1 (concatenated quoted words): proposed outside-direct-marker-contract. D2 gates on authored raw text, and the concatenation doc-"lattice" carries no doc[-_.]+lattice marker in that raw text, so the source is dropped at collection (S5.1) before any helper runs. The S6.2 ordering clarification confirms the word-level marker rule never resurrects a source D2 did not batch, so the doc-quote contraction stays a named D2 contract removal ratified in the predecessor evaluation. This restores the D3 checkpoint's not_applicable disposition for this row.
- Row 32 (arithmetic command shift): proposed intentional-exit-2. S3.2 ArithmCmd (( )) is a refuse construct; unsupported-construct is fixed terminal scope in the frozen reason-code table, but the table does not fix which code an ArithmCmd refusal emits, deciding whether the sibling doc-lattice linear is retained. Proposed fail-closed: terminal, no retained invocation.
- Row 37 (unbalanced dollar-arithmetic runs a command-substitution subshell): proposed intentional-exit-2. S3.2 Assign value is refuse and the $(( )) fallback to a command-substitution subshell is parser-behavior dependent; fail-closed intentional-exit-2 drops the old-scanner linear.
- Row 38 (unbalanced arithmetic command runs a nested subshell): proposed intentional-exit-2. Whether mvdan/sh parses (( )) as an ArithmCmd refuse or as a nested subshell traverse is parser-behavior dependent; fail-closed intentional-exit-2 drops the old-scanner linear.
- Row 49 (unquoted heredoc delimiter word removes continuation): proposed intentional-exit-2. S3.4 flags the backslash-newline continuation in the heredoc delimiter word; whether the guard fires is parser-behavior dependent. Proposed fail-closed: guard refusal.
- Row 50 (double-quoted heredoc delimiter word removes continuation): proposed intentional-exit-2. S3.4 flags the backslash-newline continuation in the double-quoted delimiter word; parser-behavior dependent. Proposed fail-closed: guard refusal.
- Row 51 (single-quoted heredoc delimiter word preserves continuation): proposed intentional-exit-2. S3.4 flags the backslash-newline continuation in the single-quoted delimiter word; parser-behavior dependent. Proposed fail-closed: guard refusal.
- Row 60 (unquoted heredoc continuation forms delimiter): proposed intentional-exit-2. S3.4 body backslash-newline continuation that forms the delimiter; guard-versus-certify is parser-behavior dependent. Proposed fail-closed: guard refusal, dropping old linear.
- Row 61 (unquoted heredoc continuation forms command substitution): proposed intentional-exit-2. S3.4 body backslash-newline continuation that forms a command substitution; guard-versus-certify is parser-behavior dependent. Proposed fail-closed: guard refusal, dropping old linear.
- Row 62 (here-string substitution): proposed must-certify. S3.2 traverses the here-string word as a redirect target-word expansion; the command substitution executes and doc-lattice linear certifies. Proposal: here-string word mapped to Redirect target-word-expansion traverse; frozen table lacks an explicit here-string row; ratify the mapping or add the row.
- Row 63 (here-string literal): proposed must-certify. The here-string word is literal; cat is not a candidate and the source certifies empty. Proposal: here-string word mapped to Redirect target-word-expansion traverse; frozen table lacks an explicit here-string row; ratify the mapping or add the row.
- Row 71 (dry-run is here-string redirection word): proposed must-certify. The --dry-run word is the here-string redirect word (target-word expansion), not an argv token; reconcile certifies without dry-run. Proposal: here-string word mapped to Redirect target-word-expansion traverse; frozen table lacks an explicit here-string row; ratify the mapping or add the row.
