# Inverted Marker Certification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every retained-word `doc[-_.]+lattice` marker fail closed unless the existing shell resolver classifies the effective executable as doc-lattice, while preserving the existing invocation-finding grammar.

**Architecture:** Compute an exact ASCII marker fact once when each decoded shell word is finalized, OR it into simple-command state, and pass that O(1) aggregate to the unresolved-executable fallback. Rebaseline all live expectations under that predicate, then delete the dispatcher-only head enumeration, option walker, and resolver provenance while retaining `_ResolvedIndex.external_lookup`, uv requirement-name derivation, and all finding-path behavior.

**Tech Stack:** Python 3.13/3.14, dataclasses, `re`, pytest, Typer, uv, Ruff, ty

---

## Governing references and file map

- Design authority: `docs/superpowers/specs/2026-07-23-inverted-marker-certification-design.md`
- Issue: `https://github.com/Guardantix/doc-lattice/issues/106`
- Baseline branch: `fix/issue-106-inverted-marker-certification`
- Runtime implementation: `src/doc_lattice/github_ci/shell_scanner.py`
- Audit sentinel commentary: `src/doc_lattice/github_ci/audit.py`
- Scanner unit and contract tests: `tests/test_github_ci_shell_scanner.py`
- End-to-end audit tests: `tests/test_github_ci_audit.py`
- User contract: `README.md`
- Durable decision record: `ARCHITECTURE.md`
- Read-only adversarial inputs: `.worktrees/successor-evaluation/tests/fixtures/github_ci_successor_checkpoint/corpus/new_fixtures.json`

Do not modify `CHANGELOG.md`, `tests/fixtures/github_ci_checkpoint/`, the successor-evaluation
worktree, or any corpus artifact. Phase-2 cross-command data flow, heredoc/herestring body taint,
pipeline relationships, and substitution provenance are outside this plan.

### Task 1: Record exact marker facts on words and simple commands

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_scanner.py:385-456`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:525-539`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:804-829`
- Test: `tests/test_github_ci_shell_scanner.py`

- [ ] **Step 1: Import command state and add focused marker-accounting tests**

Add `_CommandScanState` to the private imports in
`tests/test_github_ci_shell_scanner.py`, then add these tests beside the existing scan-budget tests:

```python
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('doc-"lattice"', True),
        ("DOC_LATTICE", True),
        ("doc...lattice", True),
        ("doc-lattıce", False),
    ],
    ids=["composed-fragments", "ascii-casefold", "repeated-separators", "dotless-i"],
)
def test_shell_word_marker_fact_matches_composed_ascii_regex(source, expected):
    scanner = _ShellScanner(source, classify_commands=False)

    word, end = scanner._parse_word(0, len(source), 0)

    assert end == len(source)
    assert word.has_doc_lattice_marker is expected


def test_command_marker_fact_aggregates_and_resets():
    source = "doc-lattice"
    scanner = _ShellScanner(source, classify_commands=False)
    state = _CommandScanState(words=[], heredocs=[], cases=[])
    word, end = scanner._parse_word(0, len(source), 0)

    scanner._record_word(state, word)

    assert end == len(source)
    assert state.command_has_marker is True
    state.reset_command()
    assert state.words == []
    assert state.command_has_marker is False


@pytest.mark.parametrize("suffix", ["", "doc-lattice"], ids=["marker-free", "marker-bearing"])
def test_long_finalized_word_marker_scan_does_not_charge_step_budget(suffix):
    source = "'" + ("x" * 100_000) + suffix + "'"
    budget = _ScanBudget(3)
    scanner = _ShellScanner(source, budget=budget, classify_commands=False)

    scanner.scan()

    assert budget.remaining_steps == 1
```

- [ ] **Step 2: Run the new tests and confirm the missing facts are red**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_shell_word_marker_fact_matches_composed_ascii_regex \
  tests/test_github_ci_shell_scanner.py::test_command_marker_fact_aggregates_and_resets \
  tests/test_github_ci_shell_scanner.py::test_long_finalized_word_marker_scan_does_not_charge_step_budget \
  -v
```

Expected: the first two tests fail because `has_doc_lattice_marker` and
`command_has_marker` do not exist; the long-word budget characterization passes.

- [ ] **Step 3: Compute the marker on the finalized composed literal**

Replace `_ShellWord` and `_ShellWordBuilder.build` with the following definitions, preserving the
other builder methods:

```python
@dataclass(frozen=True, slots=True)
class _ShellWord:
    literal: str
    has_doc_lattice_marker: bool = False
    dynamic: bool = False
    locale_translated: bool = False
    unquoted_dynamic: bool = False
    quoted_zero_field_expansion: bool = False
    active_argv_expansion: bool = False
    shell_assignment: bool = False
    keyword_eligible: bool = True
```

```python
def build(self) -> _ShellWord:
    """Build the immutable decoded word and its expansion provenance."""
    literal = "".join(self.characters)
    return _ShellWord(
        literal=literal,
        has_doc_lattice_marker=_DISPATCHER_MARKER_RE.search(literal) is not None,
        dynamic=self.dynamic,
        locale_translated=self.locale_translated,
        unquoted_dynamic=self.unquoted_dynamic,
        quoted_zero_field_expansion=self.quoted_zero_field_expansion,
        active_argv_expansion=_has_active_argv_expansion("".join(self.active_syntax)),
        shell_assignment=self.shell_assignment,
        keyword_eligible=self.keyword_eligible,
    )
```

This must search the composed `literal`, not individual quote fragments and not the raw authored
source.

- [ ] **Step 4: Aggregate and reset the command marker fact**

Add the field and reset to `_CommandScanState`:

```python
@dataclass(slots=True)
class _CommandScanState:
    words: list[_ShellWord]
    heredocs: list[_Heredoc]
    cases: list["_CaseScanState"]
    prefix_mode: str = "normal"
    prefix_pending: int = 0
    at_command_position: bool = True
    command_has_marker: bool = False

    def reset_command(self) -> None:
        """Clear the accumulated simple command and its incremental prefix-scan state."""
        self.words.clear()
        self.prefix_mode = "normal"
        self.prefix_pending = 0
        self.at_command_position = True
        self.command_has_marker = False
```

At the start of `_ShellScanner._record_word`, before prefix or case processing can return or raise,
OR the finalized word fact into command state:

```python
def _record_word(self, state: _CommandScanState, word: _ShellWord) -> None:
    state.command_has_marker = state.command_has_marker or word.has_doc_lattice_marker
    command_position = state.at_command_position
```

Leave redirection operands untouched: `_consume_redirection` must continue parsing and discarding
them without `_record_word`.

- [ ] **Step 5: Run the focused and complete scanner suites**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_shell_word_marker_fact_matches_composed_ascii_regex \
  tests/test_github_ci_shell_scanner.py::test_command_marker_fact_aggregates_and_resets \
  tests/test_github_ci_shell_scanner.py::test_long_finalized_word_marker_scan_does_not_charge_step_budget \
  -v
uv run --no-sync pytest tests/test_github_ci_shell_scanner.py
```

Expected: all selected tests pass, then the complete scanner test file passes with the old
dispatcher behavior still intact.

- [ ] **Step 6: Commit marker accounting**

```bash
git add src/doc_lattice/github_ci/shell_scanner.py tests/test_github_ci_shell_scanner.py
git commit -m "refactor: track shell command markers"
```

### Task 2: Invert the unresolved-executable fallback and rebaseline every live expectation

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1685-1774`
- Modify: `tests/test_github_ci_shell_scanner.py`
- Modify: `tests/test_github_ci_audit.py:1207-1235`

- [ ] **Step 1: Add one reusable refusal assertion**

Add this helper immediately after the scanner-test constants:

```python
def assert_marker_refusal(script: str) -> None:
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None
    with pytest.raises(ConfigError, match=r"shell scan incomplete"):
        direct_doc_lattice_invocations(script)
```

- [ ] **Step 2: Add the governing regressions**

Add these regressions near the marker case tables:

```python
@pytest.mark.parametrize(
    "script",
    [
        "echo doc-lattice reconcile",
        "find . -name 'doc-lattice*'",
        "command -v doc-lattice",
    ],
    ids=["unknown-head", "find-operand", "command-query"],
)
def test_marker_bearing_non_invocation_fails_closed(script):
    assert_marker_refusal(script)


def test_function_shadow_form_fails_closed():
    script = """\
echo() { eval "$CMD"; }
CMD='doc-lattice reconcile' echo done
"""

    assert_marker_refusal(script)


@pytest.mark.parametrize(
    "script",
    ['echo doc-"lattice"', "echo DOC_LATTICE", "echo doc...lattice"],
    ids=["composed-fragments", "ascii-casefold", "repeated-separators"],
)
def test_composed_ascii_marker_under_unknown_head_fails_closed(script):
    assert_marker_refusal(script)


def test_non_ascii_near_marker_under_unknown_head_stays_certified():
    assert direct_doc_lattice_invocations("echo doc-lattıce") == NONE


def test_marker_bearing_non_invocation_reason_names_certification_failure():
    result = scan_doc_lattice_invocations("echo doc-lattice reconcile")

    assert (
        result.incomplete_reason
        == "marker-bearing command is not a certified doc-lattice invocation"
    )


def test_command_marker_state_resets_between_simple_commands():
    result = scan_doc_lattice_invocations("doc-lattice --help; echo ok")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_redirection_target_marker_is_out_of_scope():
    result = scan_doc_lattice_invocations("bash -c 'echo hi' > doc-lattice.log")

    assert result.invocations == NONE
    assert result.incomplete_reason is None
```

- [ ] **Step 3: Rename the existing refusal table around the new predicate**

Rename `DISPATCHER_FAIL_CLOSED_CASES` to `MARKER_REFUSE_CASES` without changing its existing 96
tuples. Replace its issue-105 comment with:

```python
# A retained doc-lattice marker under any command the resolver does not classify as doc-lattice
# fails closed. The original issue #105 dispatcher rows remain here as empirical regression
# knowledge, but dispatcher reachability is no longer the certification boundary.
```

- [ ] **Step 4: Append the script, wrapper, query, and uv rows to the refusal table**

Append these first 23 formerly-certified tuples to `MARKER_REFUSE_CASES`:

```python
    ("external script file named for doc-lattice", "bash ./doc-lattice-runner.sh"),
    ("non-dispatcher head echoes marker text", "echo doc-lattice reconcile"),
    ("command wrapper external script file", "command bash ./doc-lattice-runner.sh"),
    ("env wrapper external script file", "env bash ./doc-lattice-runner.sh"),
    ("command query never executes marker", "command -v doc-lattice"),
    ("emulate mode then external script file", "zsh --emulate sh ./doc-lattice-runner.sh"),
    ("windows launcher external script file", "bash.exe ./doc-lattice-runner.sh"),
    ("uv run external script file", "uv run bash ./doc-lattice-runner.sh"),
    ("builtin non-dispatcher target", "builtin echo doc-lattice"),
    ("builtin shell target is not a builtin", "builtin bash -c 'doc-lattice reconcile'"),
    ("braced quoted option value stays resolvable", 'bash -o "${X}" ./doc-lattice-runner.sh'),
    ("rbash external script file", "rbash ./doc-lattice-runner.sh"),
    ("assignment marker without dispatcher head", "CMD='doc-lattice reconcile' echo done"),
    ("lone dash ends options before operand", "bash - -c 'doc-lattice reconcile'"),
    ("requirement-suffixed plain head is not a dispatcher", "bash@1.0 -c 'doc-lattice reconcile'"),
    ("uvx requirement external script file", "uvx bash@1.0 ./doc-lattice-runner.sh"),
    (
        "uvx direct requirement URL filename does not override declared name",
        "uvx 'not-bash @ file:///tmp/bash-1.0-py3-none-any.whl' "
        "-c 'doc-lattice reconcile'",
    ),
    ("wrapper before external script file", "nohup bash ./doc-lattice-runner.sh"),
    ("find dot operand is not a dispatcher", "find . -name 'doc-lattice*'"),
    ("wrapper argv marker without shell head", "xargs doc-lattice-formatter --all"),
    ("marker argument after script operand", "bash ./run.sh doc-lattice"),
    (
        "versioned env requirement never resolves its arguments",
        "uvx env@1.0 doc-lattice reconcile",
    ),
    (
        "versioned time requirement never resolves its arguments",
        "uv tool run time@2.0 doc-lattice reconcile",
    ),
```

- [ ] **Step 5: Append the remaining 29 retired shell and plain-head rows**

Continue the same `MARKER_REFUSE_CASES` list with:

```python
    ("exec wrapper before plain eval head", "exec eval 'doc-lattice reconcile'"),
    ("env wrapper before plain source head", "env source ./doc-lattice-env.sh"),
    ("external time before plain eval head", "command time -p eval 'doc-lattice reconcile'"),
    ("uv run before plain eval head", "uv run eval 'doc-lattice reconcile'"),
    ("eager help stop before inline command", "bash --help -c 'doc-lattice reconcile'"),
    ("eager version stop before inline command", "zsh --version -c 'doc-lattice reconcile'"),
    ("path-qualified eval is a path execution", "./eval 'doc-lattice reconcile'"),
    ("path-qualified source is a path execution", "./source ./doc-lattice-env.sh"),
    ("path-qualified dot is a path execution", "./. ./doc-lattice-env.sh"),
    ("command wrapper before path-qualified eval", "command ./eval 'doc-lattice reconcile'"),
    ("uppercase plain head is not the builtin", "EVAL 'doc-lattice reconcile'"),
    ("suffixed plain head is not the builtin", "eval.exe 'doc-lattice reconcile'"),
    ("syntax check noexec before inline command", "bash -n -c 'doc-lattice reconcile'"),
    ("dash noexec before inline command", "dash -n -c 'doc-lattice reconcile'"),
    ("noexec cluster inline command", "sh -nc 'doc-lattice reconcile'"),
    ("reversed noexec cluster inline command", "bash -cn 'doc-lattice reconcile'"),
    ("set option noexec before inline command", "bash -o noexec -c 'doc-lattice reconcile'"),
    ("stacked noexec setters before inline command", "bash -o noexec -n -c 'doc-lattice lint'"),
    ("dump strings mode before inline command", "bash --dump-strings -c 'doc-lattice reconcile'"),
    ("dump po strings mode before inline command", "bash --dump-po-strings -c 'doc-lattice lint'"),
    ("noexec setter after inline selection", "bash -n -c -n 'doc-lattice reconcile'"),
    ("set option noexec after inline selection", "bash -n -c -o noexec 'doc-lattice lint'"),
    ("exec wrapper before builtin dispatcher target", "exec builtin eval 'doc-lattice reconcile'"),
    ("exec wrapper before coprocess dispatcher", "exec coproc eval 'doc-lattice reconcile'"),
    ("exec wrapper before coproc word", "exec coproc bash -c 'doc-lattice reconcile'"),
    ("env wrapper before coproc word", "env coproc bash -c 'doc-lattice reconcile'"),
    ("quoted coproc word after exec", "exec 'coproc' bash -c 'doc-lattice reconcile'"),
    (
        "local wheel non-shell requirement",
        "uvx ./innocent-1.0.0-py3-none-any.whl -c 'doc-lattice reconcile'",
    ),
    (
        "wheel requirement before external script operand",
        "uvx ./bash-1.0.0-py3-none-any.whl ./doc-lattice-runner.sh",
    ),
```

Rename the parametrized test to
`test_marker_bearing_non_invocation_case_fails_closed`, point it at
`MARKER_REFUSE_CASES`, and replace its body with:

```python
def test_marker_bearing_non_invocation_case_fails_closed(_description, script):
    assert_marker_refusal(script)
```

- [ ] **Step 6: Rebuild the certification table around resolver success**

Replace `DISPATCHER_CERTIFY_CASES` with this table. Its first five rows are the complete set of
legacy rows that stay certified; the remaining rows make the resolver-success side of the
two-sided contract explicit:

```python
MARKER_CERTIFY_CASES = [
    (
        "marker only in trailing comment",
        "bash -c 'echo hello'  # doc-lattice check runs here",
        NONE,
    ),
    ("marker-free inline command", "bash -c 'echo hello world'", NONE),
    ("Unicode dotless i is not an ASCII marker", "bash -c 'doc-lattıce reconcile'", NONE),
    ("dispatcher head with no argv", "eval", NONE),
    ("directory requirement without marker", "uvx ./tools/shellkit -c 'echo hello'", NONE),
    ("resolved direct invocation", "doc-lattice linear", LINEAR),
    (
        "resolved wheel requirement invocation",
        "uvx ./dist/doc_lattice-2.0.0-py3-none-any.whl reconcile",
        RECONCILE,
    ),
    (
        "resolved nested wheel launcher invocation",
        "uvx ./uv-0.8.0-py3-none-any.whl run doc-lattice linear",
        LINEAR,
    ),
    ("resolved root help", "doc-lattice --help", NONE),
]
```

Replace its parametrized test with:

```python
@pytest.mark.parametrize(
    ("_description", "script", "expected"),
    MARKER_CERTIFY_CASES,
    ids=[case[0] for case in MARKER_CERTIFY_CASES],
)
def test_resolved_or_marker_free_command_stays_certified(_description, script, expected):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == expected
```

Delete `test_inline_dispatch_reason_names_the_dispatcher`; the new exact-reason regression from
Step 1 replaces it.

#### Exhaustive 64-node rebaseline ledger outside the legacy tables

Use `INCOMPLETE` plus `assert_marker_refusal` for each node in this ledger. Rows explicitly marked
“keep” must retain their current certified result.

| Existing test or table | Exact rows that become REFUSE | Rows to keep |
|---|---|---|
| `ACCEPTANCE_CASES` | `escaped substitution literal`; `single-quoted substitution literal`; `inner single-quoted substitution literal`; `single-quoted parameter expansion literal`; `process substitution literal argument`; `multiline double-quoted literal`; `multiline single-quoted literal`; `bare uv tool install remains non-candidate` | All other rows keep their current outcomes |
| `test_direct_doc_lattice_invocations_handles_documented_forms` | `echo 'doc-lattice linear'`; `printf "%s\\n" "doc-lattice reconcile --all"` | Resolved doc-lattice rows keep their invocation tuples |
| `test_direct_doc_lattice_invocations_certifies_exec_coproc_word` | `exec coproc doc-lattice reconcile` | Rename to state that the retained marker fails closed |
| `test_direct_doc_lattice_invocations_preserves_lone_carriage_returns` | `carriage-return-remains-command-text` / `echo\r doc-lattice linear` | `hash-remains-word-text` keeps `LINEAR` |
| `test_direct_doc_lattice_invocations_ignores_indirect_or_similarly_named_commands` | All eight sources: `other-doc-lattice linear`; `doc-lattice-helper linear`; `$RUNNER_TEMP/doc-lattice-helper linear`; `echo doc-lattice linear`; `printf doc-lattice reconcile`; `runner doc-lattice linear`; `+=x doc-lattice linear`; `FLAGS++=x doc-lattice linear` | No rows |
| `test_direct_doc_lattice_invocations_does_not_widen_dynamic_or_nonexecuting_forms` | `{doc-lattice linear` | `doc-lattice --version linear`; `doc-lattice --no-color --version linear` stay certified-empty because the executable resolves |
| `test_direct_doc_lattice_invocations_ignores_unsupported_builtin_targets` | `builtin doc-lattice linear`; `builtin env doc-lattice linear` | No rows |
| `test_direct_doc_lattice_does_not_treat_quoted_single_field_expansion_as_erasable` | All five sources: `"$(true)" doc-lattice linear`; `"$*" doc-lattice linear`; `"${items[*]}" doc-lattice linear`; the `braced-nameref` row; the `static-literal-with-nameref` row | No rows |
| `test_direct_doc_lattice_invocations_does_not_treat_dynamic_assignment_name_as_prefix` | `quoted-name-fragment`; `unquoted-name-fragment` | No rows |
| `test_direct_doc_lattice_invocations_honors_env_option_terminator` | `env -- -S doc-lattice linear` | No rows |
| `test_direct_doc_lattice_invocations_ignores_nonexecuting_command_forms` | All 22 current rows: `command -v doc-lattice linear`; `command -V doc-lattice linear`; `command -pv doc-lattice linear`; `uv run --module doc-lattice linear`; `uv run --module=doc-lattice linear`; `uv run -m doc-lattice linear`; `uv run -mdoc-lattice linear`; `uv run --script doc-lattice linear`; `uv run -s doc-lattice linear`; `uv run --gui-script doc-lattice linear`; `uv --help run doc-lattice linear`; `uv -h run doc-lattice linear`; `uv --version run doc-lattice linear`; `uv -V run doc-lattice linear`; `uvx --help doc-lattice linear`; `uvx -h doc-lattice linear`; `uvx --version doc-lattice linear`; `uvx -V doc-lattice linear`; `uv run --help doc-lattice linear`; `uv run -h doc-lattice linear`; `uv tool run --help doc-lattice linear`; `uv tool run -h doc-lattice linear` | No rows |
| `test_uv_tool_bare_non_run_subcommand_stays_not_candidate` | `install doc-lattice` | `list` stays certified-empty |
| `test_scanner_issue_102_fixtures_stay_fail_closed` | Change the `uv tool install doc-lattice` tuple’s `complete` field from `True` to `False` | All other tuple values stay unchanged |
| `test_direct_doc_lattice_invocations_ignores_uv_non_launcher_forms` | `uv pip install doc-lattice`; `uvx other-doc-lattice==2.0.0 linear`; `uvx doc-lattice-tools>=2.0.0 linear`; `uv run doc-lattice@2.0.0 linear`; `uv run command doc-lattice linear` | `uv sync` stays certified-empty |
| Literal-backtick tests | Both rows in `test_direct_doc_lattice_invocations_ignores_literal_backticks`; `test_direct_doc_lattice_invocations_ignores_literal_backticks_in_substitution`; `test_direct_doc_lattice_invocations_tracks_parameter_expansion_in_substitution` | Active backtick substitutions keep their detected invocations |

- [ ] **Step 7: Rebaseline tables that already carry an expected outcome**

`ACCEPTANCE_CASES` already handles `INCOMPLETE`; only change the eight row values. Add the same
sentinel branch to the two other mixed tests:

```python
def test_direct_doc_lattice_invocations_handles_documented_forms(script, expected):
    if expected is INCOMPLETE:
        assert_marker_refusal(script)
        return
    assert direct_doc_lattice_invocations(script) == expected


def test_direct_doc_lattice_invocations_preserves_lone_carriage_returns(script, expected):
    if expected is INCOMPLETE:
        assert_marker_refusal(script)
        return
    assert direct_doc_lattice_invocations(script) == expected
```

- [ ] **Step 8: Convert every all-refuse test to the shared assertion**

For each all-refuse parametrized function in the ledger, replace its certified-empty assertion
with the exact statement:

```python
assert_marker_refusal(script)
```

Use these exact behavior-based names:

| Current function | Replacement function |
|---|---|
| `test_direct_doc_lattice_invocations_certifies_exec_coproc_word` | `test_direct_doc_lattice_invocations_fails_closed_on_exec_coproc_marker` |
| `test_direct_doc_lattice_invocations_ignores_indirect_or_similarly_named_commands` | `test_direct_doc_lattice_invocations_fails_closed_on_unresolved_marker_commands` |
| `test_direct_doc_lattice_invocations_ignores_unsupported_builtin_targets` | `test_direct_doc_lattice_invocations_fails_closed_on_unsupported_builtin_marker` |
| `test_direct_doc_lattice_does_not_treat_quoted_single_field_expansion_as_erasable` | `test_direct_doc_lattice_fails_closed_on_quoted_dynamic_head_with_marker` |
| `test_direct_doc_lattice_invocations_does_not_treat_dynamic_assignment_name_as_prefix` | `test_direct_doc_lattice_invocations_fails_closed_on_dynamic_assignment_name_before_marker` |
| `test_direct_doc_lattice_invocations_honors_env_option_terminator` | `test_direct_doc_lattice_invocations_fails_closed_on_env_terminator_before_marker` |
| `test_direct_doc_lattice_invocations_ignores_nonexecuting_command_forms` | `test_direct_doc_lattice_invocations_fails_closed_on_nonexecuting_marker_forms` |
| `test_direct_doc_lattice_invocations_ignores_literal_backticks` | `test_direct_doc_lattice_invocations_fails_closed_on_literal_backtick_marker` |
| `test_direct_doc_lattice_invocations_ignores_literal_backticks_in_substitution` | `test_direct_doc_lattice_invocations_fails_closed_on_literal_backtick_marker_in_substitution` |
| `test_direct_doc_lattice_invocations_tracks_parameter_expansion_in_substitution` | `test_direct_doc_lattice_invocations_fails_closed_on_literal_marker_after_parameter_expansion` |

Use these complete standalone test bodies:

```python
def test_direct_doc_lattice_invocations_fails_closed_on_exec_coproc_marker():
    assert_marker_refusal("exec coproc doc-lattice reconcile")


def test_direct_doc_lattice_invocations_fails_closed_on_env_terminator_before_marker():
    assert_marker_refusal("env -- -S doc-lattice linear")


def test_direct_doc_lattice_invocations_fails_closed_on_literal_backtick_marker_in_substitution():
    script = '''echo "$(printf '%s' '`doc-lattice linear`')"'''

    assert_marker_refusal(script)


def test_direct_doc_lattice_invocations_fails_closed_on_literal_marker_after_parameter_expansion():
    script = '''echo "$(printf %s ${x:-)}; printf '%s' '`doc-lattice linear`')"'''

    assert_marker_refusal(script)
```

- [ ] **Step 9: Split the remaining mixed tables into explicit outcomes**

Split mixed tables that lack an `expected` column:

- keep the two resolved `doc-lattice --version` forms in a certified-empty parametrized test and
  make `{doc-lattice linear` the body of
  `test_direct_doc_lattice_invocations_fails_closed_on_unresolved_braced_marker_head`;
- keep `uv sync` in a certified-empty test and move the other five uv rows to
  `test_direct_doc_lattice_invocations_fails_closed_on_marker_bearing_uv_non_launcher_forms`;
- parameterize `uv tool install doc-lattice` and `uv tool list` with `expected_complete=False/True`
  under `test_uv_tool_bare_non_run_subcommand_applies_marker_fallback` so both outcomes remain
  explicit.

Use these complete replacements:

```python
@pytest.mark.parametrize(
    "script",
    ["doc-lattice --version linear", "doc-lattice --no-color --version linear"],
)
def test_direct_doc_lattice_invocations_keeps_resolved_nonexecuting_forms(script):
    assert direct_doc_lattice_invocations(script) == NONE


def test_direct_doc_lattice_invocations_fails_closed_on_unresolved_braced_marker_head():
    assert_marker_refusal("{doc-lattice linear")


def test_direct_doc_lattice_invocations_keeps_marker_free_uv_non_launcher_form():
    assert direct_doc_lattice_invocations("uv sync") == NONE


@pytest.mark.parametrize(
    "script",
    [
        "uv pip install doc-lattice",
        "uvx other-doc-lattice==2.0.0 linear",
        "uvx doc-lattice-tools>=2.0.0 linear",
        "uv run doc-lattice@2.0.0 linear",
        "uv run command doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_marker_bearing_uv_non_launcher_forms(
    script,
):
    assert_marker_refusal(script)


@pytest.mark.parametrize(
    ("subcommand", "expected_complete"),
    [("install doc-lattice", False), ("list", True)],
    ids=["marker-bearing-install", "marker-free-list"],
)
def test_uv_tool_bare_non_run_subcommand_applies_marker_fallback(
    subcommand,
    expected_complete,
):
    script = f"uv tool {subcommand}"
    if not expected_complete:
        assert_marker_refusal(script)
        return

    result = scan_doc_lattice_invocations(script)
    assert result.incomplete_reason is None
    assert result.invocations == NONE
    assert direct_doc_lattice_invocations(script) == NONE
```

- [ ] **Step 10: Expand the end-to-end audit refusal test**

Replace the inline-dispatch parametrization and test in `tests/test_github_ci_audit.py` with:

```python
@pytest.mark.parametrize(
    "run_body",
    [
        "bash -c 'doc-lattice reconcile'",
        'eval "doc-lattice $CMD"',
        "sh -lc 'doc-lattice reconcile --all'",
        "source ./scripts/doc-lattice-env.sh",
        "uv run bash -c 'doc-lattice reconcile'",
        "echo doc-lattice reconcile",
        "find . -name 'doc-lattice*'",
        "command -v doc-lattice",
    ],
    ids=[
        "bash-c",
        "eval",
        "sh-cluster",
        "source",
        "uv-run-bash-c",
        "unknown-head",
        "find-operand",
        "command-query",
    ],
)
def test_global_audit_fails_closed_on_marker_bearing_non_invocation(run_body: str):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - shell: bash
        run: {run_body}
"""
    )

    with pytest.raises(
        ConfigError,
        match=(
            r"shell scan incomplete.*marker-bearing command is not a certified "
            r"doc-lattice invocation"
        ),
    ):
        audit_global_workflows((document,))
```

- [ ] **Step 11: Run the governing tests against the old fallback**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_audit.py::test_global_audit_fails_closed_on_marker_bearing_non_invocation \
  -q
```

Expected: FAIL. Newly rebaselined unknown-head, script-file, query, uv, dynamic-head, and literal-data
rows still return complete; the exact-reason test either gets no reason or the retired inline
dispatcher reason. Resolved direct doc-lattice rows remain green.

- [ ] **Step 12: Pass the O(1) command fact into the unresolved fallback**

Change `_ShellScanner._flush_command` to:

```python
def _flush_command(self, state: _CommandScanState) -> None:
    if not state.words:
        return
    if self.classify_commands:
        invocation = _invocation_in_simple_command(
            state.words,
            self.budget,
            command_has_marker=state.command_has_marker,
        )
        if invocation is not None:
            if len(self.invocations) >= _MAX_SHELL_INVOCATIONS:
                raise _ShellScanIncomplete("invocation limit exceeded")
            self.invocations.append(invocation)
    state.reset_command()
```

Change the invocation classifier and add the new helper:

```python
def _invocation_in_simple_command(
    words: list[_ShellWord],
    budget: _ScanBudget,
    *,
    command_has_marker: bool,
) -> _Invocation | None:
    resolution = _LauncherResolutionState(budget)
    executable = _doc_lattice_command_index(words, 0, resolution)
    if executable.index is None:
        _reject_marker_bearing_non_invocation(command_has_marker)
        return None
```

Keep the remainder of `_invocation_in_simple_command` byte-for-byte unchanged, then define:

```python
def _reject_marker_bearing_non_invocation(command_has_marker: bool) -> None:
    """Fail closed when an unresolved command contains a retained doc-lattice marker."""
    if command_has_marker:
        raise _ShellScanIncomplete(
            "marker-bearing command is not a certified doc-lattice invocation"
        )
```

Do not call the old dispatcher helper from this path.

- [ ] **Step 13: Rewrite the public scanner docstring around the new predicate**

Replace the dispatcher-specific paragraphs in `direct_doc_lattice_invocations` with:

```python
"""Return conservative direct doc-lattice commands from literal Bash syntax.

The scanner is bounded, recursive, and non-executing. Existing resolver grammar classifies
literal doc-lattice executable positions and preserves its invocation and post-resolution
fail-closed behavior. If that resolver does not classify the executable, any retained
assignment-prefix or argv word matching the ASCII doc-lattice marker fails closed rather than
being certified as a non-invocation.

The scanner intentionally does not resolve aliases, functions, PATH shadowing, variables used
as executable names, external wrapper scripts, actions, reusable workflows, or cross-command
data flow. Comments and discarded redirection operands are not retained command words.

Args:
    script: Literal Bash source to scan.
    context: Optional caller-supplied prefix (for example a workflow path) that identifies
        the source when the scan cannot complete. When given it is prepended to the raised
        fail-closed error so the operator can locate the offending script.

Raises:
    ConfigError: If the bounded scanner cannot certify the source.
"""
```

- [ ] **Step 14: Run the rebaselined scanner and audit tests**

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_audit.py::test_global_audit_fails_closed_on_marker_bearing_non_invocation
```

Expected: all tests pass. The exact reason is
`marker-bearing command is not a certified doc-lattice invocation`; bare `doc-lattice`,
`doc-lattice --help`, and `doc-lattice --version` remain certified-empty.

- [ ] **Step 15: Commit the behavior and live rebaseline**

```bash
git add \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_scanner.py \
  tests/test_github_ci_audit.py
git commit -m "fix: invert shell marker certification"
```

### Task 3: Delete dispatcher-only machinery without changing the finding path

**Files:**

- Modify: `src/doc_lattice/github_ci/shell_scanner.py:290-310`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:575-612`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:1775-2063`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:2198-2329`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:2519-2674`
- Modify: `src/doc_lattice/github_ci/shell_scanner.py:2961-3040`
- Modify: `tests/test_github_ci_shell_scanner.py`

- [ ] **Step 1: Run the finding-path pins before structural cleanup**

Task 2’s `MARKER_CERTIFY_CASES` now pins both
`uvx ./dist/doc_lattice-2.0.0-py3-none-any.whl reconcile` and
`uvx ./uv-0.8.0-py3-none-any.whl run doc-lattice linear`. Keep those rows and the existing bare
root/help/version test unchanged.

Run:

```bash
uv run --no-sync pytest \
  tests/test_github_ci_shell_scanner.py::test_resolved_or_marker_free_command_stays_certified \
  tests/test_github_ci_shell_scanner.py::test_direct_doc_lattice_allows_static_missing_or_nonexecuting_subcommand \
  -v
```

Expected: PASS before cleanup. These are characterization guards for the finding path.

- [ ] **Step 2: Remove dispatcher head and shell-option constants**

Keep `_DISPATCHER_MARKER_RE`, but replace its comment with:

```python
# Retained-word certification marker. It follows Python distribution separator spelling and is
# deliberately ASCII case-insensitive, so doc-lattice/doc_lattice/doc.lattice variants match
# while Unicode case-fold lookalikes do not.
```

Delete these constants:

```text
_PLAIN_DISPATCHER_HEADS
_SHELL_DISPATCHER_HEADS
_SHELL_LONG_OPTIONS_WITH_ARGUMENTS
_SHELL_EAGER_STOP_OPTIONS
```

- [ ] **Step 3: Remove executable-candidate state**

Delete `_ExecutableCandidate`. Reduce `_LauncherResolutionState` to:

```python
@dataclass(slots=True)
class _LauncherResolutionState:
    """Shared budget and memoized states for one simple command's launcher grammar."""

    budget: _ScanBudget
    cache: dict[tuple[str, int, int], _ResolvedIndex] = field(default_factory=dict)

    def step(self) -> None:
        """Charge speculative launcher work to the shell scanner's declared budget."""
        self.budget.step()
```

- [ ] **Step 4: Delete the dispatcher head and option walkers**

Delete the complete old block from `_reject_marker_bearing_dispatcher` through
`_shell_option_consumes_value`. Keep `_word_may_change_option_value_shape`; env and uv option
resolution still call it.

- [ ] **Step 5: Remove candidate-provenance parameters from prefix resolution**

Change `_skip_shell_prefixes` to:

```python
def _skip_shell_prefixes(
    words: list[_ShellWord],
    start: int,
    *,
    external_lookup: bool = False,
) -> _ResolvedIndex:
```

Inside it, call the wrapper without deleted provenance:

```python
wrapper = _skip_shell_builtin_wrapper(words, index)
```

Replace `_skip_shell_builtin_wrapper` and `_skip_builtin_wrapper` with:

```python
def _skip_shell_builtin_wrapper(
    words: list[_ShellWord],
    index: int,
) -> _ResolvedIndex:
    """Resolve one supported Bash wrapper beginning at ``index``."""
    literal = words[index].literal
    if literal == "builtin":
        return _skip_builtin_wrapper(words, index + 1)
    if literal == "command":
        return _skip_command_builtin(words, index + 1)
    return _skip_exec_wrapper(words, index + 1)


def _skip_builtin_wrapper(
    words: list[_ShellWord],
    start: int,
) -> _ResolvedIndex:
    """Expose a supported literal Bash builtin target or one ambiguous successor."""
    index = start
    if index < len(words) and not words[index].dynamic and words[index].literal == "--":
        index += 1
    if index >= len(words):
        return _ResolvedIndex(index)
    target = words[index]
    if _command_boundary_word_may_disappear(target) or target.dynamic:
        return _ResolvedIndex(index + 1, ambiguous=True)
    if target.literal not in {"builtin", "command", "exec"}:
        return _ResolvedIndex(None)
    return _ResolvedIndex(index)
```

Do not weaken `_ResolvedIndex.external_lookup`; the coproc gate still consumes it.

- [ ] **Step 6: Remove dispatcher recording from payload resolution**

In `_doc_lattice_command_index`, use:

```python
command = _skip_shell_prefixes(words, start)
```

Call `_doc_lattice_payload_index` without `external_lookup` in both
`_doc_lattice_command_index` and `_doc_lattice_command_after_prefixes`:

```python
payload_index = _doc_lattice_payload_index(words, command_index, resolution)
```

```python
payload = _doc_lattice_payload_index(words, executable.index, resolution)
```

Keep `external_lookup` on `_doc_lattice_command_after_prefixes` and pass it only into
`_skip_shell_prefixes`; this preserves coproc PATH-exec provenance.

Replace `_doc_lattice_payload_index` with:

```python
def _doc_lattice_payload_index(
    words: list[_ShellWord],
    executable_index: int,
    resolution: _LauncherResolutionState,
    *,
    launcher_depth: int = 0,
) -> _ResolvedIndex:
    if executable_index >= len(words):
        return _ResolvedIndex(None)
    executable_word = words[executable_index]
    _reject_unsafe_executable_word(executable_word)
    if _is_doc_lattice_executable(executable_word):
        return _ResolvedIndex(executable_index)
    if not executable_word.dynamic:
        executable = _basename(executable_word.literal)
        if executable in {"env", "time"}:
            return _nested_launcher_payload_index(
                words,
                _ResolvedIndex(executable_index),
                strip_version=False,
                launcher_depth=launcher_depth,
                resolution=resolution,
            )
        if executable == "uvx":
            return _uvx_payload_index(
                words,
                executable_index + 1,
                launcher_depth=launcher_depth,
                resolution=resolution,
            )
        if executable == "uv":
            return _uv_payload_index(
                words,
                executable_index + 1,
                launcher_depth=launcher_depth,
                resolution=resolution,
            )
    return _ResolvedIndex(None)
```

In `_nested_launcher_payload_index`, delete the `_ExecutableCandidate` append and the comment that
justifies it. Keep this derivation exactly:

```python
raw_basename = _basename(payload.literal)
basename = _uv_requirement_executable_name(payload.literal) if strip_version else raw_basename
```

Replace the final opaque-tail branch with:

```python
else:
    return _ResolvedIndex(None, payload_resolution.ambiguous)
```

- [ ] **Step 7: Remove obsolete direct dispatcher-budget tests and imports**

Delete these five tests from `tests/test_github_ci_shell_scanner.py`:

```text
test_marker_free_dispatcher_candidates_consume_one_marker_pass_budget
test_marker_bearing_external_shell_candidates_consume_shared_budget
test_duplicate_dispatcher_candidates_classify_argv_once
test_repeated_opaque_tail_heads_classify_each_argv_once
test_dispatcher_free_command_skips_marker_pass_budget
```

Remove imports of `_ExecutableCandidate`, `_LauncherResolutionState`,
`_reject_marker_bearing_dispatcher`, and `_ShellWord`; the five deleted tests are their remaining
consumers. Retain `_ScanBudget`, `_ShellScanner`, and `_CommandScanState`; Task 1’s tests replace
the relevant budget and aggregate coverage.

- [ ] **Step 8: Prove the dispatcher boundary is gone and finding-path state remains**

Run:

```bash
rg -n \
  "_PLAIN_DISPATCHER_HEADS|_SHELL_DISPATCHER_HEADS|_SHELL_LONG_OPTIONS_WITH_ARGUMENTS|_SHELL_EAGER_STOP_OPTIONS|_ExecutableCandidate|executable_positions|opaque_tail_start|mark_opaque_tail|_reject_marker_bearing_dispatcher|_reachable_dispatcher_heads|_record_walk_start|_normalize_dispatcher_head|_shell_dispatcher_runs_inline_command|_is_noexec_setter|_is_pure_noexec_trigger|_is_shell_option_token|_shell_option_consumes_value" \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_scanner.py
```

Expected: exit 1 with no matches.

Run:

```bash
rg -n "_uv_requirement_executable_name|external_lookup" \
  src/doc_lattice/github_ci/shell_scanner.py \
  tests/test_github_ci_shell_scanner.py
```

Expected: `_uv_requirement_executable_name` still has its definition, nested-launcher finding-path
call, and parser tests; `_ResolvedIndex.external_lookup` and coproc/prefix consumers remain.

- [ ] **Step 9: Run the scanner and audit suites after cleanup**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py
```

Expected: all tests pass; the cleanup changes no outcomes from Task 2.

- [ ] **Step 10: Commit dispatcher cleanup**

```bash
git add src/doc_lattice/github_ci/shell_scanner.py tests/test_github_ci_shell_scanner.py
git commit -m "refactor: remove dispatcher certification machinery"
```

### Task 4: Publish the two-sided audit contract and durable decision

**Files:**

- Modify: `src/doc_lattice/github_ci/audit.py:94-98`
- Modify: `README.md:640-675`
- Modify: `ARCHITECTURE.md`
- Include: `docs/superpowers/plans/2026-07-23-inverted-marker-certification.md`

- [ ] **Step 1: Update the audit sentinel comment**

Replace the `_SCRIPT_SENTINEL` comment with:

```python
# The run-body placeholder must be inert: it stands in for the ``{0}`` script argument when the
# shell template is scanned, so it must not itself match the doc-lattice marker and make every
# placeholder-bearing template fail the inverted unresolved-command certification rule.
_SCRIPT_SENTINEL = "__run_body_script__"
```

- [ ] **Step 2: Replace the README dispatcher/noexec contract**

In the `ci audit` scanner section of `README.md`, replace the text beginning with “Known eager uv
help/version options” through the old dispatcher, noexec, wrapper, and assembled-payload paragraphs
with:

```markdown
A resolved doc-lattice executable with no effective command, including bare `doc-lattice`,
`doc-lattice --help`, and `doc-lattice --version`, produces no policy finding. Launcher
help/version forms that leave a retained doc-lattice marker under an unresolved command instead
exit 2.

For each simple command, audit decodes retained assignment-prefix and argv words and applies the
ASCII marker `doc[-_.]+lattice` case-insensitively. If the existing resolver classifies the
effective executable as doc-lattice, its launcher, subcommand, and fail-closed checks continue
unchanged. Otherwise any retained marker exits 2, regardless of the apparent command head or
whether that concrete spelling would execute on one host. Consequently forms such as
`echo doc-lattice reconcile`, `command -v doc-lattice`,
`bash --help -c 'doc-lattice reconcile'`, and `nohup bash ./doc-lattice-runner.sh` are not
certified as non-invocations. Comments and discarded redirection targets are not retained command
words.

Executable classification is syntactic basename resolution, not proof of runtime identity. Audit
does not model function, alias, or `PATH` shadowing; variables used as executable names; arbitrary
scripts, actions, reusable workflows, or renamed wrappers; or cross-command data flow such as file
handoff, variable-plus-`eval`, pipelines, heredoc/herestring bodies, and markers assembled across
words. Malformed, oversized, or otherwise unreliably inspectable workflows also exit 2 instead of
being treated as safe.
```

Keep the surrounding shell-selection, expansion, secret, and bootstrap audit documentation
unchanged.

- [ ] **Step 3: Add durable architecture decision AD-17**

Append this entry to `ARCHITECTURE.md`:

```markdown
### AD-17: CI shell marker certification is inverted

**Date:** 2026-07-23
**Status:** Accepted
**Context:** The retained CI shell scanner previously refused marker-bearing commands only when
an open enumeration recognized a reachable inline dispatcher. Unrecognized wrappers, script-file
forms, query commands, and ordinary-looking heads could therefore carry a doc-lattice marker while
being certified by omission. A trusted inert-head list would have the same flaw because shell
functions, aliases, and `PATH` can shadow bare names.
**Decision:** Each finalized decoded assignment-prefix or argv word records whether it matches the
ASCII, case-insensitive `doc[-_.]+lattice` marker, and simple-command state aggregates that fact.
When the existing resolver does not classify the effective executable as doc-lattice, any retained
marker fails closed. Resolved doc-lattice commands keep the existing launcher, option,
subcommand, and post-resolution behavior, including certified-empty root help/version forms. The
dispatcher head sets, shell `-c`/noexec option walk, executable-candidate recording, and opaque-tail
provenance are removed; `_ResolvedIndex.external_lookup` and uv requirement-name derivation remain
where the invocation-finding path uses them.
**Consequences:** Unknown marker-bearing heads refuse by construction, with no inert-command
allowlist. Marker detection adds one source-cap-bounded scan of each finalized decoded word and an
O(1) aggregate check at command flush. The resolver remains syntactic and does not prove runtime
identity or model function/alias/`PATH` shadowing or cross-command data flow; comments and
discarded redirection operands remain outside retained-word certification. Frozen evaluation
checkpoints stay immutable, while live scanner and audit tests own the changed expectations.
```

- [ ] **Step 4: Check documentation ownership and formatting**

Run:

```bash
git diff --check
uv run --no-sync python scripts/check_version_sync.py
git diff --name-only | rg '^CHANGELOG.md$'
```

Expected: `git diff --check` and version sync exit 0. The final command exits 1 with no output,
confirming no changelog edit.

- [ ] **Step 5: Commit durable documentation**

```bash
git add \
  src/doc_lattice/github_ci/audit.py \
  README.md \
  ARCHITECTURE.md \
  docs/superpowers/plans/2026-07-23-inverted-marker-certification.md
git commit -m "docs: document inverted marker certification"
```

### Task 5: Run the read-only corpus battery and full handoff verification

**Files:**

- Read only: `.worktrees/successor-evaluation/tests/fixtures/github_ci_successor_checkpoint/corpus/new_fixtures.json`
- Verify: entire repository

- [ ] **Step 1: Run the section-8 corpus battery without trusting frozen outcomes**

The frozen corpus labels describe the predecessor design. Read only the source inputs and assert
the live phase-1 outcomes explicitly:

```bash
uv run --no-sync python - <<'PY'
import json
from pathlib import Path

from doc_lattice.github_ci.shell_scanner import scan_doc_lattice_invocations

fixture = Path(
    ".worktrees/successor-evaluation/tests/fixtures/"
    "github_ci_successor_checkpoint/corpus/new_fixtures.json"
)
families = json.loads(fixture.read_text(encoding="utf-8"))["families"]

certified = {
    ("dispatcher", "marker-free-dispatch"): (),
    ("look_alike", "exe-head-certifies"): (("check", False),),
    ("look_alike", "casefold-head-certifies"): (("lint", False),),
}
refuse = {
    ("dispatcher", row["id"])
    for row in families["dispatcher"]
    if row["id"] != "marker-free-dispatch"
} | {
    ("look_alike", "underscore-head"),
    ("look_alike", "dotted-wrapper-head"),
}

seen = set()
for family in ("dispatcher", "look_alike"):
    for row in families[family]:
        key = (family, row["id"])
        result = scan_doc_lattice_invocations(row["source"])
        if key in certified:
            assert result.incomplete_reason is None, (key, result)
            assert result.invocations == certified[key], (key, result)
        else:
            assert key in refuse, key
            assert result.incomplete_reason is not None, (key, result)
            assert result.invocations == (), (key, result)
        seen.add(key)

assert seen == set(certified) | refuse
print(f"PASS: {len(seen)} successor corpus inputs match live inverted-marker expectations")
PY
```

Expected:

```text
PASS: 12 successor corpus inputs match live inverted-marker expectations
```

- [ ] **Step 2: Run focused scanner and audit verification**

Run:

```bash
uv run --no-sync pytest tests/test_github_ci_shell_scanner.py tests/test_github_ci_audit.py
```

Expected: all tests pass.

- [ ] **Step 3: Run the complete test suite and coverage gate**

Run:

```bash
uv run --no-sync pytest
```

Expected: all tests pass and the repository coverage threshold is met.

- [ ] **Step 4: Run lint, format, typing, and repository policy checks**

Run:

```bash
uv run --no-sync ruff check src/ tests/
uv run --no-sync ruff format --check src/ tests/
uv run --no-sync ty check src/
uv run --no-sync python scripts/check_typing_boundaries.py src/
uv run --no-sync python scripts/check_version_sync.py
git diff --check
```

Expected: every command exits 0. The typing-boundary script prints:

```text
PASS: typing.Any/typing.cast restricted to boundary modules
```

- [ ] **Step 5: Confirm immutable inputs and a clean branch**

Run:

```bash
git status --short
git diff --name-only main...HEAD -- \
  tests/fixtures/github_ci_checkpoint \
  .worktrees/successor-evaluation \
  CHANGELOG.md
```

Expected: both commands produce no output. The branch contains only the intended runtime, live
test, audit comment, README, architecture, spec, and plan history.
