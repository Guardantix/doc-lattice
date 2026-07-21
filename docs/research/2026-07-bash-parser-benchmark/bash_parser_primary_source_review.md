# Primary-Source Review of Non-Executing Bash Parsers for Python 3.13+

## Executive Summary

**Recommended candidate: pinned `shfmt` via `shfmt-py`.**

Among the reviewed candidates, pinned shfmt was the only option that both:

1. Cleared the practical hard gates for a Python 3.13+ package on mainstream Linux, macOS, and Windows targets; and
2. Improved both the **false-safe** and **indeterminate** rates over the pinned `tree-sitter-bash` baseline on the attached 78-case acceptance corpus.

The recommended architecture is not “trust shfmt alone.” It is:

> **Pinned shfmt as the primary parser, wrapped in a strict subprocess timeout and schema validation, with the existing bounded conservative scanner retained as a fail-closed fallback.**

No other reviewed candidate both cleared the gates and improved the measured safety rate.

---

## Scope and Hard Gates

The review covered:

- Pinned shfmt via `shfmt-py`
- `tree-sitter-bash`
- `brush-parser`
- `bashlex`
- ShellCheck
- Oils/OSH
- Flash
- GitHub’s Bash Command Parser Specification

The required hard gates were:

1. **Permissive licensing**
2. **Installability on Linux, macOS, and Windows**
3. **Machine-readable AST**
4. **Explicit malformed-input signaling**
5. **Bounded execution**
6. **Active maintenance**

The review also examined:

- Python wheel and platform coverage
- AST stability
- Fuzzing
- Timeout strategy
- Supply-chain ownership
- Every named acceptance case in the attached corpus
- Whether each candidate improved the false-safe or indeterminate rate over tree-sitter

Only candidates that improved the safety metrics over tree-sitter were eligible for ranking.

---

## Final Ranking

### 1. Pinned shfmt via `shfmt-py`

**Disposition: Recommended**

Pinned shfmt is the only ranked candidate.

It offers:

- A non-executing parser
- Machine-readable JSON AST output
- Explicit nonzero failure on malformed input
- A simple subprocess boundary that can be hard-killed on timeout
- Current upstream maintenance
- Cross-platform binaries distributed through a Python package
- The strongest measured result on the acceptance corpus

Its principal weaknesses are:

- Separate ownership of the Python wrapper and upstream parser
- Build/install-time binary acquisition in the wrapper implementation
- No Windows ARM64 wheel
- A version-specific AST schema rather than a formally stable external AST contract
- One observed false-safe involving heredoc backslash-newline formation of command substitution

### Unranked candidates

No other candidate both cleared the hard gates and improved on tree-sitter.

---

## Acceptance-Corpus Benchmark

The evaluation checkpoint freezes the first **78 named `ACCEPTANCE_CASES`** covering:

- Literal executable identity
- Concatenated quoting
- Control-flow conditions and branches
- Modern and legacy command substitutions
- Nested substitutions
- Parameter expansion
- Arithmetic contexts
- Malformed arithmetic boundaries
- Heredoc quoting and expansion policy
- Heredoc continuations
- Here-strings
- Process substitution
- Redirection placement
- Multiline quoted literals
- Commands preceding malformed fragments

### Outcome definitions

- **Correct** — the observed ordered invocation tuples exactly matched the expected tuples.
- **False-safe** — at least one expected executable invocation was missed while the parser still yielded a usable result.
- **Indeterminate** — parsing failed, recovery nodes made the result uncertifiable, JSON was invalid, or the parser exceeded a bound.
- **False-positive** — an invocation was reported where none was expected.

The principal safety comparison is:

```text
false-safe + indeterminate
```

### Final reproducible results

| Parser | Correct | False-safe | Indeterminate | False-positive | False-safe + indeterminate |
|---|---:|---:|---:|---:|---:|
| **shfmt 3.13.1** | **69** | **1** | **8** | **0** | **9 / 78 (11.5%)** |
| tree-sitter-bash 0.25.1 | 58 | 5 | 14 | 1 | 19 / 78 (24.4%) |
| bashlex 0.18 | 40 | 12 | 25 | 1 | 37 / 78 (47.4%) |

Relative to tree-sitter, shfmt reduced:

- False-safe outcomes from **5 to 1**
- Indeterminate outcomes from **14 to 8**
- Combined safety-relevant failures from **19 to 9**
- False positives from **1 to 0**

### Bounded execution

Each parser/case execution ran in a separate worker with:

- **2-second outer watchdog** per parser/case
- **1-second inner subprocess timeout** for shfmt
- No shell execution of the input
- No `shell=True`
- No outer-watchdog timeouts across 234 parser/case executions

This indicates that the observed differences came from parser behavior rather than benchmark hangs.

---

## Candidate Review

## 1. shfmt via `shfmt-py`

### Gate assessment

| Gate | Result |
|---|---|
| Permissive license | Pass |
| Linux installability | Pass |
| macOS installability | Pass |
| Windows installability | Pass for x86-64; no Windows ARM64 wheel |
| Machine-readable AST | Pass |
| Explicit malformed-input signal | Pass |
| Bounded execution | Pass through subprocess timeout |
| Active maintenance | Pass |
| Improves over tree-sitter | Pass |

### Licensing

- Upstream `mvdan/sh` is BSD-3-Clause licensed.
- `shfmt-py` is MIT licensed.

Both are permissive.

### Python and platform packaging

The reviewed package pin was:

```text
shfmt-py==4.0.0
```

It bundles or acquires:

```text
shfmt 3.13.1
```

The wrapper source maps supported platform/architecture pairs to exact upstream binaries and SHA-256 digests. Covered targets include:

- Linux x86-64
- Linux ARM64
- Linux ARM variants
- macOS x86-64
- macOS ARM64
- Windows x86-64
- Windows x86

There is no native Windows ARM64 artifact in the reviewed mapping.

### Supply-chain ownership

The Python package and parser are maintained by different owners:

- `shfmt-py`: independent Python wrapper
- `mvdan/sh`: upstream Go parser and shfmt executable

The wrapper downloads official upstream release binaries and verifies pinned SHA-256 hashes before installation. That is materially better than an unverified download, but it still creates two trust relationships:

1. The PyPI wrapper maintainer
2. The upstream shfmt release owner

For higher-assurance environments, mirror or rebuild the exact binary and publish an internally controlled wheel.

### AST characteristics

shfmt exposes a JSON syntax tree suitable for machine consumption.

The schema should be treated as **version-pinned**, not permanently stable. Application code should:

- Pin the exact shfmt version
- Validate expected top-level fields
- Reject unknown node kinds
- Maintain golden AST fixtures
- Re-run the full corpus for every version change

### Malformed input

Malformed shell input produces explicit parser errors and a nonzero process status. This is preferable to an editor-oriented recovered tree for security certification.

A parser error must remain **indeterminate**. It must not be silently reinterpreted as “no invocation.”

### Fuzzing

Upstream `mvdan/sh` uses Go fuzzing for parser-related code. This is relevant evidence of parser hardening, though fuzzing cannot establish semantic equivalence with every Bash edge case.

### Timeout strategy

shfmt is naturally isolated behind a process boundary.

Recommended controls:

```text
- subprocess argument list only
- shell=False
- 0.5–1.0 second wall-clock timeout
- stdout byte limit
- stderr byte limit
- input byte limit
- recursion/nesting pre-budget
- kill process on timeout
- treat timeout as indeterminate
```

### Acceptance-corpus result

```text
69 correct
1 false-safe
8 indeterminate
0 false-positive
```

### Sole false-safe

The one observed false-safe was:

```bash
cat <<EOF
$\
(doc-lattice linear)
EOF
```

Bash removes the backslash-newline in the unquoted heredoc body, forming:

```bash
$(doc-lattice linear)
```

The shfmt-derived adapter did not expose that invocation correctly.

This case must remain as a permanent regression test or be handled by a targeted conservative pre-scan.

### Interpretation

shfmt is substantially safer than tree-sitter on this corpus, but it is not a complete proof of Bash execution semantics. It should be used as the strongest parser signal in a layered fail-closed system.

---

## 2. tree-sitter-bash

### Gate assessment

| Gate | Result |
|---|---|
| Permissive license | Pass |
| Linux installability | Pass |
| macOS installability | Pass |
| Windows installability | Pass |
| Machine-readable AST | Pass |
| Explicit malformed-input signal | Conditional |
| Bounded execution | Pass |
| Active maintenance | Pass |
| Improves over tree-sitter baseline | Not applicable; baseline |

### Packaging strength

Tree-sitter has the strongest native Python wheel coverage of the reviewed parser libraries.

The reviewed versions were:

```text
tree-sitter==0.25.2
tree-sitter-bash==0.25.1
```

Its ABI3 wheels cover a broad matrix including:

- Windows x86-64
- Windows ARM64
- Linux glibc
- Linux musl
- Linux x86-64
- Linux ARM64
- macOS x86-64
- macOS ARM64

### AST behavior

Tree-sitter is designed to produce useful trees even when source text is incomplete or malformed. That is ideal for editors but risky for allow/deny certification.

A secure adapter must recursively detect and reject:

- `ERROR` nodes
- Missing nodes
- Recovered structures
- Unexpected node kinds
- Ambiguous command-name construction

Simply receiving a tree cannot be treated as successful parsing.

### AST stability

The generated grammar exposes a large version-specific node vocabulary. Node kinds and grammar structure are generated from the pinned grammar.

Therefore:

> Treat the AST as stable only for the exact grammar version covered by tests.

Do not assume compatibility across grammar upgrades.

### Timeout strategy

The Python parser supports time-bounded parsing. A timeout should produce an indeterminate result, and the parser should be reset before reuse.

An even stronger isolation model is one parser operation per worker process.

### Acceptance-corpus result

```text
58 correct
5 false-safe
14 indeterminate
1 false-positive
```

### Interpretation

Tree-sitter remains a strong packaging and editor-parsing choice. It is not the best primary security parser for this scanner because recovered-tree semantics created substantially more false-safe and indeterminate outcomes than shfmt.

---

## 3. brush-parser

### Gate assessment

| Gate | Result |
|---|---|
| Permissive license | Pass |
| Linux installability | Pass as Rust |
| macOS installability | Pass as Rust |
| Windows installability | Partial |
| Machine-readable AST | Pass |
| Explicit malformed-input signal | Pass |
| Bounded execution | Requires wrapper/process policy |
| Active maintenance | Pass |
| Python 3.13 package gate | Fail |
| Ranked | No |

### Strengths

Brush is the strongest future-binding candidate.

It provides:

- MIT licensing
- A public typed AST
- Explicit `ParseError` and tokenizer errors
- Source spans
- Optional Serde serialization
- Optional `arbitrary` derivations
- Parser and arithmetic fuzz targets
- Active Bash compatibility work

The AST derives serialization when the Serde feature is enabled, making it suitable for a machine-readable boundary.

### Fuzzing

Brush has dedicated fuzz targets, including parser fuzzing and arithmetic fuzzing.

Its parser fuzz harness compares parser acceptance to:

```text
bash --noprofile --norc -O extglob -n -t
```

The Bash oracle is bounded at 15 seconds.

However, the harness skips several difficult categories, including heredocs. Heredoc behavior is central to this acceptance corpus, so the fuzz evidence is useful but incomplete for this exact scanner.

### Packaging failure

`brush-parser` is a Rust crate, not a maintained Python 3.13 package.

Using it would require the project to own:

- PyO3 or equivalent bindings
- Linux wheels
- musl wheels if required
- macOS Intel and ARM64 wheels
- Windows x86-64 wheels
- Windows ARM64 decisions
- Rust toolchain pinning
- ABI testing
- Release signing
- AST compatibility policy

That violates the current installability/deployment gate for an ordinary Python dependency.

### AST stability

The crate remains in the `0.x` version range. Its public AST is technically accessible, but downstream code should not assume minor-version schema stability without a project-specific compatibility layer.

### Interpretation

Brush merits a prototype only if the project is willing to become the maintainer of a first-party Python binding and wheel pipeline. It is not currently rankable for the requested package.

---

## 4. bashlex

### Gate assessment

| Gate | Result |
|---|---|
| Permissive license | Fail |
| Cross-platform Python installability | Pass |
| Machine-readable AST | Pass |
| Explicit malformed-input signal | Pass |
| Bounded execution | Requires worker process |
| Active maintenance | Fail |
| Improves over tree-sitter | Fail |

### Licensing

bashlex is GPLv3-or-later, so it fails the permissive-license gate.

### Packaging

It is pure Python and therefore easy to install across Linux, macOS, and Windows.

### Maintenance

The reviewed release was:

```text
bashlex 0.18
```

The release cadence is stale relative to the other candidates.

### Acceptance-corpus result

```text
40 correct
12 false-safe
25 indeterminate
1 false-positive
```

It performed materially worse than tree-sitter.

### Interpretation

bashlex is unsuitable for this package due to licensing, maintenance, and measured parser coverage.

---

## 5. ShellCheck

### Gate assessment

| Gate | Result |
|---|---|
| Permissive license | Fail |
| Cross-platform installability | Generally available |
| Machine-readable complete AST | Fail as supported interface |
| Explicit malformed-input signal | Diagnostics available |
| Bounded execution | Pass through subprocess timeout |
| Active maintenance | Pass |
| Ranked | No |

### Licensing

ShellCheck is GPLv3, failing the permissive-license gate.

### AST interface

ShellCheck has a sophisticated internal parser, but its supported JSON output is a diagnostics format, not a stable full Bash AST contract intended for external parser consumers.

Depending on internal Haskell modules would create an unsupported API dependency and require a custom service or native binding.

### Interpretation

ShellCheck remains highly valuable as an auxiliary linter and differential-testing oracle. It is not a suitable parser dependency under the stated gates.

---

## 6. Oils/OSH

### Gate assessment

| Gate | Result |
|---|---|
| Permissive license | Pass |
| Linux installability | Pass |
| macOS installability | Pass |
| Native Windows installability | Fail/unsupported |
| Machine-readable AST | Internal implementation available |
| Explicit malformed-input signal | Pass |
| Bounded execution | Pass through process isolation |
| Active maintenance | Pass |
| Python package gate | Fail |
| Ranked | No |

### Strengths

Oils is Apache-2.0 licensed and has:

- A rich parser
- Generated ASDL syntax types
- Extensive shell compatibility tests
- Active development
- Explicit syntax-error handling

### Deployment problem

The project’s source implementation is written in Python and translated to C++ for deployed binaries. The repository development build is not presented as a reusable, supported Python 3.13 parser package.

There is no simple, maintained native-Windows Python wheel story.

### API stability

Its AST is an internal implementation interface. Adopting it would couple the package to generated internal types and project build machinery.

### Interpretation

Oils is an excellent reference implementation and differential-testing oracle, but not a practical dependency for this Python package.

---

## 7. Flash

### Gate assessment

| Gate | Result |
|---|---|
| Permissive license | Fail |
| Cross-platform installability | Incomplete |
| Machine-readable AST | Rust AST |
| Explicit malformed-input signal | Available |
| Bounded execution | Requires wrapper/process policy |
| Active maintenance | Pass |
| Bash completeness | Fail |
| Python package gate | Fail |
| Ranked | No |

### Licensing

Flash is GPLv3, failing the permissive-license gate.

### Completeness

The project describes itself as experimental and documents unsupported Bash functionality.

### Packaging

It is Rust-based and does not provide maintained Python wheels.

### Interpretation

Flash fails licensing, maturity, completeness, and Python deployment requirements.

---

## 8. GitHub Bash Command Parser Specification

### Gate assessment

| Gate | Result |
|---|---|
| Permissive specification | Available |
| Installable parser implementation | Fail |
| Machine-readable Bash AST | Fail |
| Explicit malformed-input signaling | Fail by design |
| Bounded execution | Implementation-dependent |
| Active maintenance | Pass |
| Ranked | No |

### Nature of the specification

GitHub’s specification defines lightweight operations such as:

- Splitting on pipeline operators
- Extracting a command name
- Extracting command names from a pipeline

Its conformance vectors are machine-readable and useful.

However, it is not a Bash grammar and does not define a complete AST.

### Malformed-input behavior

The verification vectors explicitly require malformed constructs to remain non-throwing. That is appropriate for a resilient recognizer but does not satisfy the hard gate requiring explicit malformed-input signaling.

### Interpretation

Use the vectors as an auxiliary recognizer test suite. Do not treat the specification as a replacement Bash parser.

---

## Comparative Gate Matrix

| Candidate | Permissive | Linux/macOS/Windows | Machine AST | Malformed signal | Boundable | Active | Beats tree-sitter | Final disposition |
|---|---|---|---|---|---|---|---|---|
| **shfmt via shfmt-py** | Yes | Yes, except Win ARM64 | Yes | Yes | Yes | Yes | **Yes** | **Ranked #1** |
| tree-sitter-bash | Yes | Yes | Yes | Recovery requires strict adapter | Yes | Yes | Baseline | Retain as reference/fallback only |
| brush-parser | Yes | Not as Python wheels | Yes | Yes | Yes with wrapper | Yes | Not benchmarked after gate failure | Future binding candidate |
| bashlex | No | Yes | Yes | Yes | Yes with worker | Weak/stale | No | Reject |
| ShellCheck | No | Generally | No supported full AST | Diagnostics | Yes | Yes | Not applicable | Reject |
| Oils/OSH | Yes | No supported native-Windows Python package | Internal | Yes | Yes | Yes | Not applicable | Reject for deployment |
| Flash | No | Incomplete Python story | Rust AST | Yes | Yes | Yes | Not applicable | Reject |
| GitHub specification | N/A | No parser package | No | No | Implementation-specific | Yes | Not applicable | Auxiliary spec only |

---

## Recommended Architecture

```text
Input size and nesting budget
            |
            v
Pinned shfmt subprocess
- exact package and binary versions
- argument list only
- shell=False
- strict wall-clock timeout
- stdout/stderr byte limits
            |
            +--> valid known AST schema
            |        |
            |        v
            |    AST command analysis
            |
            +--> timeout / parse error / invalid JSON /
                 unknown node / truncated output
                         |
                         v
             Existing bounded conservative scanner
                         |
                         +--> safely resolved: return result
                         |
                         +--> unresolved: fail closed
```

### Required implementation controls

1. Pin:

   ```text
   shfmt-py==4.0.0
   shfmt==3.13.1
   ```

2. Verify package hashes in the lock file or installer.

3. At runtime, assert the exact shfmt version.

4. Invoke with an argument list and `shell=False`.

5. Apply a hard subprocess timeout.

6. Apply maximum input, stdout, and stderr sizes.

7. Treat every parser error as indeterminate.

8. Reject unknown or unexpected AST node kinds.

9. Maintain golden AST fixtures tied to the exact parser pin.

10. Retain the frozen 78-case checkpoint prefix and every appended live regression as a release
    gate.

11. Add a permanent targeted regression for:

    ```bash
    cat <<EOF
    $\
    (doc-lattice linear)
    EOF
    ```

12. Never silently substitute a recovered tree after shfmt rejects malformed input.

13. Retain the existing bounded scanner for fail-closed fallback and semantic checks that are outside the parser’s responsibility.

---

## Supply-Chain Recommendation

For ordinary use, `shfmt-py` provides a practical packaging shape.

For a higher-assurance package, prefer a first-party distribution process:

1. Pin the upstream `mvdan/sh` source tag and commit.
2. Build shfmt in controlled CI.
3. Produce platform-specific artifacts.
4. Generate an SBOM.
5. Sign or attest the artifacts.
6. Embed the binary into project-owned wheels.
7. Publish through trusted publishing.
8. Verify the embedded binary hash at runtime or installation.
9. Keep upstream and wrapper upgrades separate and reviewable.

This removes the independent wrapper maintainer from the critical binary acquisition path.

---

## AST Compatibility Policy

Regardless of parser choice, the package should define its own narrow intermediate representation rather than expose third-party AST nodes throughout the codebase.

For example:

```text
Program
  commands[]
    command_kind
    literal_argv[]
    redirections[]
    substitutions[]
    source_span
    parser_certainty
```

The parser adapter should convert from the pinned third-party AST into this internal representation.

Benefits:

- Parser upgrades are isolated
- Unknown nodes can fail closed
- Golden fixtures remain small
- Multiple parsers can be differentially compared
- Scanner policy is not coupled to an upstream schema

---

## Testing and Fuzzing Strategy

### Corpus tests

Run the frozen 78-case prefix plus every appended live regression on each change to:

- Parser version
- Python wrapper version
- AST adapter
- Scanner
- Timeout values
- JSON decoder
- Internal representation

### Differential testing

Compare acceptance and AST-derived command findings across:

- shfmt
- tree-sitter-bash
- Bash `-n` where available as a syntax oracle
- Brush in development CI
- Oils in periodic compatibility CI
- ShellCheck diagnostics as an auxiliary signal

A disagreement should produce a minimized regression case.

### Fuzz properties

Useful properties include:

1. The parser must never execute input.
2. Parsing must terminate within the configured bound.
3. A parser failure must never become “safe.”
4. Unknown nodes must become indeterminate.
5. Adding comments must not create commands.
6. Protecting text with single quotes must not expose commands.
7. Quoted heredoc delimiters must suppress heredoc expansion.
8. Unquoted heredoc continuation must be modeled conservatively.
9. Complete commands before malformed trailing fragments must not be silently discarded if the scanner’s policy is to preserve them.
10. Parser disagreement must fail closed.

---

## Final Decision

Adopt:

```text
shfmt-py==4.0.0
```

with:

```text
shfmt 3.13.1
```

as the primary parser.

Do not adopt shfmt as a standalone safety oracle. Use a layered design with:

- Strict subprocess bounds
- Exact version and schema pinning
- AST validation
- A project-owned intermediate representation
- The current bounded conservative scanner as fallback
- Fail-closed handling for every unresolved condition

No other reviewed candidate both clears the stated hard gates and improves the measured false-safe or indeterminate rate over tree-sitter.

---

## Primary Sources

- [mvdan/sh repository](https://github.com/mvdan/sh)
- [mvdan/sh syntax package documentation](https://pkg.go.dev/mvdan.cc/sh/v3/syntax)
- [shfmt-py repository](https://github.com/MaxWinterstein/shfmt-py)
- [shfmt-py package index](https://pypi.org/project/shfmt-py/)
- [tree-sitter repository](https://github.com/tree-sitter/tree-sitter)
- [tree-sitter Python parser documentation](https://tree-sitter.github.io/py-tree-sitter/classes/tree_sitter.Parser.html)
- [tree-sitter-bash repository](https://github.com/tree-sitter/tree-sitter-bash)
- [tree-sitter-bash package index](https://pypi.org/project/tree-sitter-bash/)
- [Brush repository](https://github.com/reubeno/brush)
- [brush-parser crate](https://crates.io/crates/brush-parser)
- [bashlex repository](https://github.com/idank/bashlex)
- [bashlex package index](https://pypi.org/project/bashlex/)
- [ShellCheck repository](https://github.com/koalaman/shellcheck)
- [Oils repository](https://github.com/oils-for-unix/oils)
- [Flash repository](https://github.com/raphamorim/flash)
- [GitHub Bash Command Parser Specification](https://github.github.com/gh-aw/specs/bash-command-parser-specification/)

---

## Associated Evidence Artifacts

The benchmark was also exported as:

- `bash_parser_acceptance_matrix.csv`
- `bash_parser_acceptance_results.json`
- `bash_parser_review_methodology.md`

These preserve the per-case expected values, observed values, classifications, errors, and timing data.
