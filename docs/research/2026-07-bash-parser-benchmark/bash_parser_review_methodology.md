# Bash parser acceptance benchmark: methodology and raw-result guide

## Scope

The attached acceptance corpus contains **78 named cases**. The benchmark is a
corpus-scoped AST extraction test, not a claim that any parser by itself understands
`doc-lattice` semantics.

For each parser, the adapter:

1. Parses without executing shell input.
2. Walks the parser's machine-readable AST.
3. Extracts literal `doc-lattice` invocations required by the corpus.
4. Marks dynamic words as uncertifiable rather than guessing.
5. Compares the ordered `(subcommand, dry_run)` tuples with the expected result.

## Outcome definitions

- **correct** — observed tuples exactly match the expected tuples.
- **false-safe** — at least one expected invocation is missed while the parser reports a usable parse.
- **indeterminate** — the parser reports malformed/recovered input, raises a parser error, emits invalid JSON, or exceeds a watchdog.
- **false-positive** — an invocation is reported where none is expected.

The primary safety comparison requested is `false-safe + indeterminate`.

## Pins and execution bounds

- Python: 3.13.5
- tree-sitter: 0.25.2
- tree-sitter-bash: 0.25.1
- shfmt-py: 4.0.0
- bundled shfmt: 3.13.1
- bashlex: 0.18
- Outer watchdog: 2.0 seconds per parser/case
- shfmt inner subprocess timeout: 1.0 second
- Isolation: forked worker process per parser/case
- Outer watchdog timeouts observed: 0

## Results

| Parser | Correct | False-safe | Indeterminate | False-positive | False-safe + indeterminate |
|---|---:|---:|---:|---:|---:|
| tree-sitter | 58 | 5 | 14 | 1 | 19 |
| shfmt | 69 | 1 | 8 | 0 | 9 |
| bashlex | 40 | 12 | 25 | 1 | 37 |

Relative to tree-sitter, shfmt reduced false-safe outcomes from 5 to 1, indeterminate
outcomes from 14 to 8, and the combined safety-relevant total from 19 to 9.

## shfmt non-correct cases

- **unbalanced dollar-arithmetic runs a command-substitution subshell** — indeterminate: `1:18: not a valid arithmetic operator: `linear``
- **unbalanced arithmetic command runs a nested subshell** — indeterminate: `1:15: not a valid arithmetic operator: `linear``
- **unbalanced dollar-arithmetic subshell without an invocation** — indeterminate: `1:18: not a valid arithmetic operator: `INNER``
- **unterminated dollar-arithmetic yields no command** — indeterminate: `1:6: reached EOF without matching `$((` with `))``
- **single-quoted heredoc delimiter word preserves continuation** — indeterminate: `1:5: unclosed here-document "E\\\nOF"`
- **unquoted heredoc continuation suppresses physical delimiter** — indeterminate: `1:5: unclosed here-document `EOF``
- **unquoted heredoc continuation forms delimiter** — indeterminate: `1:5: unclosed here-document `EOF``
- **unquoted heredoc continuation forms command substitution** — false-safe
- **complete command before malformed substitution** — indeterminate: `1:26: reached EOF without matching `$(` with `)``

## tree-sitter non-correct cases

- **nested legacy substitution** — false-safe
- **parameter arithmetic shift** — indeterminate: `ERROR or missing node`
- **unbalanced dollar-arithmetic runs a command-substitution subshell** — indeterminate: `ERROR or missing node`
- **unbalanced arithmetic command runs a nested subshell** — indeterminate: `ERROR or missing node`
- **unbalanced dollar-arithmetic subshell without an invocation** — indeterminate: `ERROR or missing node`
- **unterminated dollar-arithmetic yields no command** — indeterminate: `ERROR or missing node`
- **escaped dollar in unquoted heredoc** — indeterminate: `ERROR or missing node`
- **unquoted heredoc delimiter word removes continuation** — indeterminate: `ERROR or missing node`
- **double-quoted heredoc delimiter word removes continuation** — indeterminate: `ERROR or missing node`
- **single-quoted heredoc delimiter word preserves continuation** — indeterminate: `ERROR or missing node`
- **ansi-quoted heredoc suppresses substitution** — indeterminate: `ERROR or missing node`
- **unquoted heredoc expands backticks** — false-safe
- **multiple heredocs retain expansion policy and ordering** — indeterminate: `ERROR or missing node`
- **unquoted heredoc continuation suppresses physical delimiter** — false-positive
- **unquoted heredoc continuation forms delimiter** — indeterminate: `ERROR or missing node`
- **unquoted heredoc continuation forms command substitution** — false-safe
- **named-fd redirection before executable** — indeterminate: `ERROR or missing node`
- **redirection before subcommand** — false-safe
- **redirection before uv payload** — false-safe
- **complete command before malformed substitution** — indeterminate: `ERROR or missing node`

## bashlex non-correct cases

- **ansi-c executable** — false-safe
- **time reserved word** — indeterminate: `NotImplementedError: type = {time command}, token = {time}`
- **coproc reserved word** — indeterminate: `NotImplementedError: type = {coproc}, token = {coproc}, parts = {[WordNode(parts=[] pos=(7, 18) word='doc-lattice'), WordNode(parts=[] pos=(19, 25) word='linear')]}`
- **case arm** — indeterminate: `NotImplementedError: type = {pattern}, token = {x}`
- **comment then active command** — false-safe
- **backticks inside substitution comment** — false-positive
- **nested legacy substitution** — indeterminate: `ParsingError: unexpected EOF (position 13)`
- **legacy substitution command after comment** — false-safe
- **parameter default substitution** — false-safe
- **nested parameter substitution** — false-safe
- **hash inside parameter expansion is not a comment** — false-safe
- **arithmetic expansion shift** — indeterminate: `NotImplementedError: arithmetic expansion`
- **arithmetic command shift** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted '2') (position 34)`
- **legacy arithmetic shift** — indeterminate: `NotImplementedError: arithmetic substitution`
- **modern substitution in arithmetic** — indeterminate: `NotImplementedError: arithmetic expansion`
- **legacy substitution in arithmetic** — indeterminate: `NotImplementedError: arithmetic expansion`
- **substitution in legacy arithmetic** — indeterminate: `NotImplementedError: arithmetic substitution`
- **unbalanced dollar-arithmetic runs a command-substitution subshell** — indeterminate: `NotImplementedError: arithmetic expansion`
- **unbalanced dollar-arithmetic subshell without an invocation** — indeterminate: `NotImplementedError: arithmetic expansion`
- **balanced dollar-arithmetic is not a command** — indeterminate: `NotImplementedError: arithmetic expansion`
- **balanced dollar-arithmetic assignment is not a command** — indeterminate: `NotImplementedError: arithmetic expansion`
- **nested balanced dollar-arithmetic is not a command** — indeterminate: `NotImplementedError: arithmetic expansion`
- **unterminated dollar-arithmetic yields no command** — indeterminate: `MatchedPairError: unexpected EOF while looking for matching ')' (position 13)`
- **unquoted heredoc expands modern substitution** — false-safe
- **quote characters do not quote unquoted heredoc body** — false-safe
- **quoted heredoc suppresses modern substitution** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted "'EOF'") (position 38)`
- **double-quoted heredoc delimiter word removes continuation** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted '"EOF"') (position 46)`
- **single-quoted heredoc delimiter word preserves continuation** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted "'E\\\nOF'") (position 46)`
- **ansi-quoted heredoc suppresses substitution** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted "$'EOF'") (position 39)`
- **unquoted heredoc expands backticks** — false-safe
- **quoted heredoc suppresses backticks** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted "'EOF'") (position 37)`
- **hash does not comment unquoted heredoc expansion** — false-safe
- **nested quoted heredoc** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted "'EOF'") (position 41)`
- **multiple heredocs retain expansion policy and ordering** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted "'B'") (position 88)`
- **unquoted heredoc continuation suppresses physical delimiter** — indeterminate: `ParsingError: here-document at line 0 delimited by end-of-file (wanted 'EOF') (position 40)`
- **unquoted heredoc continuation forms command substitution** — false-safe
- **named-fd redirection before executable** — false-safe
- **complete command before malformed substitution** — indeterminate: `MatchedPairError: unexpected EOF while looking for matching ')' (position 27)`

## Reproducibility notes

The CSV contains one row per acceptance case and columns for the script, expected tuples,
observed tuples, parser status, diagnostic, outcome, and worker timing. The JSON preserves
the same information without CSV escaping and records the timeout strategy.

Candidates that failed a hard gate before executable comparison—such as licensing,
absence of a Python-distributable implementation, or lack of a machine-readable Bash
AST—were reviewed but were not assigned synthetic acceptance scores.
