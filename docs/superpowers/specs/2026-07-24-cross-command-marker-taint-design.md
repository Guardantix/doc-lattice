# Cross-command marker taint for the CI shell scanner (issue #110, phase 2)

Date: 2026-07-24

Status: design spec for the phase 2 deliverable of issue #106 (issue #110). Non-authoritative
until implemented; durable decisions transfer to
[ARCHITECTURE.md](../../../ARCHITECTURE.md) and any release note to
[CHANGELOG.md](../../../CHANGELOG.md) at ship time.

Issue: <https://github.com/Guardantix/doc-lattice/issues/110> (part of
<https://github.com/Guardantix/doc-lattice/issues/106>)

Predecessor: phase 1 inverted marker certification (PR #109, spec
[2026-07-23-inverted-marker-certification-design.md](2026-07-23-inverted-marker-certification-design.md),
AD-17). Phase 1 refuses any single simple command that carries the whole `doc[-_.]+lattice` marker
in a retained assignment-prefix, argv, or array-assignment element word unless the resolver
classifies the executable as doc-lattice. Section 7 of that spec deferred cross-command marker flow
to this design.

Baseline for all accounting: `main` at `93a9ee3` (after PR #109 merged).

## 1. Verdict

After phase 1, every surviving smuggle must keep the marker out of every retained word of the
executing command. The remaining false-safe surface is data flow *between* commands in one shell
body: the marker is authored in one command (split across words, in a heredoc body, in an
assignment, in a producer's output) and reassembled where a later command executes it. Two escapes
are verified to run `doc-lattice reconcile` under real bash while every individual command stays
marker-free:

```bash
printf '%s%s\n' 'doc-' 'lattice reconcile' > task.sh   # no word bears the whole marker
bash task.sh                                            # marker-free; the file handoff is invisible
```

```bash
cat > task.sh <<'EOF'
doc-lattice reconcile
EOF
bash task.sh          # marker lives in a heredoc body the phase-1 word model does not retain
```

Phase 2 adds a bounded, order-insensitive taint analysis over one `run:` body. It refuses when
authored fragments can compose the marker along a modeled content flow and that content reaches an
execution sink. It changes no phase-1 outcome: resolved doc-lattice invocations are still found and
emitted, and phase-1 in-word refusals still fire. Phase 2 adds refusals for marker synthesis the
phase-1 literal-word model cannot see: cross-command data flow, and in-word synthesis (brace and
parameter expansion) that composes a marker within a single command from words no one of which
carries it whole.

## 2. Certification unit and posture (settled decisions)

Two decisions were settled during brainstorming and are fixed for this spec.

**Certification unit: one `run:` body.** The analysis is confined to a single shell body. `audit.py`
continues to call the scanner independently per step (`_pr_step_invocations`,
`src/doc_lattice/github_ci/audit.py:249-283`) and does not aggregate evidence across steps. Cross-step,
cross-job, `uses:` action, and reusable-workflow handoff remain disclosed limitations. The evidence
records defined here are shaped so a future job-level aggregator could consume them, but this spec
does not build that.

**Taint posture: authored-marker taint, not strict UNKNOWN-to-sink.** AD-17 is a marker-anchored
certification policy, not a general prohibition on dynamic execution. Phase 2 preserves that anchor.
The refusal rule is:

> Refuse when authored fragments can compose `doc[-_.]+lattice` along a modeled content flow, and
> that content reaches an execution sink.

Content the analysis cannot attribute to authored text in this body -- unresolved-producer output
beyond the may-output rule (section 5), external environment variables, external files -- is
**outside the evidence domain and disclosed**, represented as absence of evidence, never as a trust
claim that it is inert. Residuals compose only along explicit, ordered data-flow edges; unrelated
`doc-` and `lattice` text elsewhere in the body must not combine. Consequently `curl ... | bash`,
`eval "$EXTERNAL"`, and marker-free generated scripts continue to certify, while both escapes above,
variable-plus-`eval`, pipeline producer/consumer, and substitution-assembled payloads refuse.
Encoding and transform synthesis (for example `base64 -d`) remains explicitly disclosed.

## 3. Architecture and module boundary

A new pure module **`src/doc_lattice/github_ci/shell_taint.py`** owns the entire taint analysis: the
symbolic content layer, the abstract evaluated domain, the flow tables, the source-selector and sink
classifiers, the bounded fixed point, and the refusal verdict. `shell_scanner.py` gains only two
responsibilities:

- drive a taint content builder (owned by `shell_taint.py`) as it parses word fragments, so
  expansions that are otherwise discarded as empty dynamic fragments
  (`shell_scanner.py:1313-1321`, `1365-1377`) contribute symbolic references; and
- emit an immutable `_CommandEvidence` record at each command flush, plus the pipe, process
  substitution, heredoc, herestring, and redirection links defined below.

The scanner never learns what a marker residual is; the taint module never parses shell. This keeps
the large scanner module focused and gives phase 2 an independently testable unit driven by synthetic
evidence, mirrored at `tests/test_github_ci_shell_taint.py` per the repo convention.

The taint pass runs once, after a top-level scan completes, and returns a pure `(verdict, reason)`.
The scanner turns a refusal verdict into an `_ShellScanIncomplete` inside
`scan_doc_lattice_invocations` (`shell_scanner.py:1648-1659`), the common result path, so both APIs
inherit the identical verdict and cannot diverge. A refusal therefore becomes
`ShellScanResult.incomplete_reason` on the `scan_*` API and, exactly as bounded-scan exhaustion does
today, a `ConfigError` raised by `direct_doc_lattice_invocations` when it observes that
`incomplete_reason` (`shell_scanner.py:1688-1692`).

## 4. Two layers: symbolic content and evaluated domain

Residuals are not available at flush -- `_ShellWord` retains only composed literal text and
dynamism booleans (`shell_scanner.py:366-440`). The design therefore separates a symbolic layer,
built during parsing, from an evaluated abstract layer, computed during the taint pass.

### 4.1 Symbolic content expressions

The content builder produces, per port, an immutable expression:

```text
ContentExpr =
    | LiteralTransfer(text)          # authored literal fragment
    | VariableRef(name)              # $X / ${X}
    | StreamRef(scope_id)            # aggregated stdout of a stream scope (section 4.6)
    | ResourceRef(key)               # content read from a static resource (section 5.2)
    | Choice(parts...)               # mutually exclusive in-word alternatives (section 4.7)
    | Concat(parts...)               # ordered, sequential composition
    | OutsideGap                     # authored-external boundary (section 4.4)
```

`Concat` is the only operator that composes sequentially. Mutually exclusive alternatives arise two
ways, and neither is ever a `Concat`: across commands they are *joined* into content values in the
tables (competing definitions, truncating writes, branches; section 5), and within a single word
they are a `Choice` (parameter default/alternate expansion; section 4.7). Brace expansion is neither
join nor `Choice`: it fans one lexical word out into several ordered argv ports (section 4.7). A
`StreamRef` names a stream scope whose aggregated stdout is defined in section 4.6, replacing a bare
command ID so a substitution or group that contains several commands is representable.

### 4.2 Content channels are distinct ports

`_CommandEvidence` does not carry one flat ordered summary. It carries typed ports that never
compose across each other:

- **argv** -- one `ContentExpr` per argv position, in order (a static brace expansion fans out into
  several ordered argv positions, section 4.7);
- **assignments** -- name -> `ContentExpr` for each assignment prefix or standalone assignment;
- **stdin** -- a heredoc body, a herestring, an incoming pipe / input process-substitution link, or a
  static file read (`ResourceRef`), and only when the redirection targets descriptor 0 (section 5.2);
- **redirection-written content** -- per static resource key (section 5.2);
- **executable disposition** -- resolved doc-lattice, a shell dispatch with a source-selector
  result (section 6), a static script-file execution operand, `source`/`.`, or none.

Composition happens only within one byte stream or one explicit command semantic. A `-c` payload
word and an unrelated stdin heredoc are different ports and never combine into a synthetic marker.

### 4.3 The evaluated abstract domain

The marker `doc[-_.]+lattice` compiles to a fixed DFA (`re.ASCII`, case-insensitive, identical to
phase 1's `_DISPATCHER_MARKER_RE`). An authored fragment abstracts to a **transfer relation** over
DFA states: for each state the fragment could be entered in, the set of states it could be exited
in, and whether the traversal passed through the accept state.

An evaluated content value is a **set of alternatives**, each a transfer relation. This makes the
join/compose distinction structural:

- **Sequential composition** (`Concat`, ordered argv fragments, `X+=`, `>>`): relational
  composition, elementwise across alternative sets; the left's exit states feed the right's entry
  states. A marker straddling the boundary (`doc-` then `lattice`) is caught because composition
  threads the mid-marker DFA state across the join.
- **Join** (competing `X=`, truncating `>`, branches, `Choice` alternatives): set union of
  alternatives. Unrelated `doc-` and `lattice` remain separate alternatives; neither alone reaches
  accept.

A value is **marker-capable** if some alternative, entered at the DFA start state, passes through
accept.

**Value transport is not composition.** Moving a value into a port -- a heredoc/herestring into its
command's stdin, a producer's stdout into a pipe consumer's stdin, a scope's aggregated stdout into
a `StreamRef` -- carries the value unchanged; it does not concatenate the two commands. Sequential
composition only occurs where authored text is genuinely adjacent in one stream (`Concat` of ordered
fragments, `X+=`, `>>`, or the ordered stdout aggregation of a stream scope in section 4.6).

### 4.4 `OUTSIDE` is a gap, not a barrier

External content is an `OutsideGap`, evaluated to at least two alternatives: **epsilon** (the empty
string) and an **opaque non-authored barrier** (arbitrary external bytes, which may or may not carry
a separator or marker text, but whose marker-capability is not authored evidence). The epsilon
alternative lets adjacent authored fragments meet:

- `eval "doc-${EXTERNAL}lattice"` -> epsilon alternative yields authored `doc-` + `lattice` =
  `doc-lattice` -> **refuse**, even when `EXTERNAL` is empty; every marker character is authored.
- `eval "doc${EXTERNAL}lattice"` -> epsilon yields `doclattice` (no `[-_.]+` separator, not a
  marker); the only path to a marker needs the separator supplied by external content, so no
  authored-only alternative reaches accept -> **certify, disclosed**.

An `OutsideGap` is never itself marker-capable through authored evidence; it only permits or blocks
adjacency. Resource-identity gaps (dynamic paths) are a distinct type in the resource domain
(section 5.2), not `OutsideGap`.

### 4.5 Command-substitution trailing-newline stripping

Bash removes all trailing newlines from command-substitution output before splicing
(<https://www.gnu.org/software/bash/manual/bash.html#Command-Substitution>). So a substitution that
emits `doc-` followed by a newline, spliced against a following authored `lattice`, can execute
`doc-lattice` even though raw-stdout composition would not match. Stripping applies **only to
command-substitution scopes**: a `StreamRef` to a `command_substitution` scope evaluates as
`StripTrailingNewlines(stream_stdout)`, while a pipe, process substitution, group redirection, or
file read transports bytes verbatim with no stripping (section 4.6 carries the scope kind that
selects this). A plain transfer relation loses suffix information, so the evaluated domain carries a
**finite suffix-aware component**: a transducer summary recording the exit-state set reachable at
trailing-newline-run boundaries, so strip-then-concat is representable. If that summary cannot be
computed within caps, the pass fails closed.

### 4.6 Stream scopes and `StreamRef`

A `StreamRef` names a **stream scope**, not a single command, so a substitution or group containing
several commands is representable. The scanner emits a `_StreamScopeEvidence` per scope:

- **scope kind** -- `command_substitution` (`$(...)`, `` `...` ``), `process_substitution`
  (`<(...)`, `>(...)`), `subshell_group` (`( ... )`, `{ ...; }`), or `pipeline`. The kind selects
  trailing-newline stripping (section 4.5: only `command_substitution`).
- **parent** -- the enclosing scope or command, so nested scopes resolve.
- **output structure** -- how the contained commands' stdout ports combine, which is **not** a blind
  concatenation. Sequential commands (`;`, newline, `&&`, `||`) form a `Sequence` (sequential
  composition). Mutually exclusive branches (`if`/`elif`/`else`, and `case` arms terminated by `;;`)
  form a `Choice` (join, consistent with section 4.3 -- branches must not concatenate). **`case`
  fallthrough is not mutually exclusive:** an arm terminated by `;&` executes the following arm, and
  `;;&` may continue matching later arms (the terminators the scanner already distinguishes in
  `_advance_case_body`, `shell_scanner.py:975-980`); fallthrough-connected arms compose as a
  `Sequence`, or fail closed if the chain cannot be structured. A loop forms a `Repeat`, evaluated as the
  reflexive-transitive closure of its per-iteration trace's transfer relation in the finite domain.
  Two loop-shape details matter:
  - **`for`/`select`** bind a variable each iteration, which `Repeat` alone misses, so the loop first
    adds **loop-binding evidence**: the variable is joined with the loop's authored iteration words
    (section 5.1) before the closure applies, so `for X in doc- lattice; do printf %s "$X"; done |
    bash` composes to a marker across iterations. Their per-iteration trace is the body.
  - **`while`/`until`** run the **test-command list before every iteration**
    (<https://www.gnu.org/software/bash/manual/html_node/Looping-Constructs.html>), so content
    composes across a body and the next test list. The per-iteration trace is therefore
    `Concat(test-list output, body output)`, closed under `Repeat`, with the test list also
    contributing an initial (pre-first-body) and final (post-last-body) run. A body-only closure
    would miss `#\n` then `doc-lattice` assembled across the body -> next-test boundary.
  Compound control flow the scanner cannot structure this way fails closed rather than blindly
  concatenating.

Aggregated stdout is that output structure applied to the stdout ports of the contained commands
that write to the scope's stdout. This closes the two compound classes a bare command ID could not
represent:

```bash
eval "$(printf doc-; printf 'lattice reconcile')"   # Sequence in one substitution scope -> refuse
```

```bash
{ printf doc-; printf 'lattice reconcile'; } > task.sh   # group stdout redirected to a resource
bash task.sh                                             # -> refuse
```

Command IDs remain the evidence attachment key (section 8); the scope ID is the aggregation key. A
scope whose contained producers are all unresolved contributes each producer's may-output stdout
(section 5.5) through the output structure.

### 4.7 In-word synthesis: parameter and brace expansion

Alternatives are not only cross-command. Several word-level expansions author a branch inside one
word, and each currently certifies while executing `doc-lattice` under real bash:

```bash
unset X; eval "${X:-doc-}lattice reconcile"   # default expansion
unset X; eval "${X:=doc-}lattice"             # assign-default: returns AND assigns "doc-"
eval doc-{lattice,noop}                        # brace expansion -> "doc-lattice doc-noop"
```

The scanner currently consumes parameter internals without surfacing their authored operands
(`_consume_parameter`, `shell_scanner.py:1433-1485`), and brace expansion is only flagged, not
expanded. The content builder must surface these:

- **Default / alternate** (`-`, `:-`, `+`, `:+`): `${X:-word}` and `${X-word}` evaluate to
  `Choice(VariableRef(X), <word>)`; `${X:+word}` and `${X+word}` evaluate to
  `Choice(<empty>, <word>)` -- the alternate branch is the authored `word`, the other branch is
  **epsilon** (the empty string), not `OutsideGap`.
- **Assign-default** (`=`, `:=`): `${X:=word}` and `${X=word}` evaluate their value exactly like the
  default form, `Choice(VariableRef(X), <word>)`, **and** carry conditional-assignment evidence: the
  authored `word` is joined as a may-flow alternative into `X`'s variable-table entry (section 5.1),
  because the expansion also assigns it. Modeling only `Choice` would miss a later `eval "$X"`.
- **Brace expansion** (`doc-{lattice,noop}`) is **argv fan-out, not `Choice`**. Bash emits every
  brace result left-to-right as separate words, so a single lexical word expands into several ordered
  argv ports, not one word with alternatives. `doc-{lattice,noop}` fans out to the argv words
  `doc-lattice` `doc-noop` (the marker-bearing first word refuses), and crucially the results are
  adjacent to their neighbors: `printf %s {doc-,lattice}` fans out to `printf %s doc- lattice`, whose
  may-output composes `doc-` and `lattice` in order. A `Choice` would wrongly model these as one word
  and never compose the two. Bounded static braces (comma lists, small numeric/char ranges) expand
  into the ordered argv ports within the caps; a brace expansion that cannot be bounded (numeric
  ranges beyond a cap, deep nesting, or a dynamic operand) fails closed.
- **Remaining parameter forms** -- pattern replacement (`${X/a/b}`), substring (`${X:off:len}`),
  indirection (`${!X}`), transformation (`${X@Q}`) -- are not modeled precisely: their authored
  literal operands (replacement text, patterns) are surfaced as authored fragments joined with an
  `OutsideGap` so authored marker text cannot hide inside them, while the variable-derived portion
  stays disclosed; if such a form cannot be bounded within caps, the pass fails closed.

See
<https://www.gnu.org/software/bash/manual/html_node/Shell-Parameter-Expansion.html>
and <https://www.gnu.org/software/bash/manual/html_node/Brace-Expansion.html>.

## 5. Flow tables and edges

The taint pass builds join-only tables from the ordered `_CommandEvidence` stream, then evaluates
sinks against them at end of scan. All edges are explicit and ordered; there is no ambient pool of
`doc-` text.

### 5.1 Variable table

Name -> evaluated content value. `X=expr` **joins** a fresh alternative (competing definitions
widen; may-flow, never overwrite). `X+=expr` yields `Concat(prior_X, expr)`. `VariableRef("X")`
resolves through this table during evaluation.

### 5.2 Resource table

Static resource key -> content value. `> path` joins written content as an alternative; `>> path`
appends via `Concat`. The written content is the producing command's **stdout port** (section 5.5).

**Static reads are input edges, descriptor-aware.** A static input redirection reads the resource:
it produces a `ResourceRef(key)`. It transports into the reading command's **stdin port only when the
redirection targets descriptor 0** (an omitted descriptor defaults to 0 for `<`). A non-zero
descriptor (`bash 3<task.sh`, `bash 3<<EOF`) opens the file on that descriptor, not stdin, and is
**not** a shell-stdin sink; the descriptor must be recorded (section 8) so the routing is correct.
Descriptor duplication (`<&`, `>&`) stays under the disclosed FD-alias limitation (section 10).
Combined with the shell source-selector (section 6), a descriptor-0 read connects the file-to-stdin
sink:

```bash
printf '%s%s\n' doc- 'lattice reconcile' > task.sh
bash < task.sh          # 0< (default) + no script operand -> bash reads commands from stdin -> refuse
```

Resource-key identity: `task.sh` and `./task.sh` normalize to the same key when no modeled directory
change intervenes (minimum static-path equivalence). `..`, `cd`, symlinks, and filesystem aliases
are **not** modeled and stay disclosed. A dynamic redirection target (read or write) is a distinct
typed `DynamicResource` key (no modeled edge), never an `OutsideGap`.

### 5.3 Pipe edges

A `|` records producer-id -> consumer-id; the producer's stdout port (its stream-scope aggregation,
section 4.6, when the producer side is itself compound) is a **candidate** binding of the consumer's
descriptor 0. A producer whose stdout carries no authored marker (for example `curl`, whose stdout
reduces to `OUTSIDE`) gives the consumer an `OUTSIDE` stdin, so `curl | bash` certifies. The pipe is a
*candidate* rather than a final edge because a later redirection on the consumer can rebind descriptor
0; the resolution is section 5.6.

### 5.4 Process substitution as typed ephemeral resources

`<(...)` and `>(...)` expand to **filenames**, not automatic pipe edges. Blindly treating every
`cmd <(producer)` as a producer -> consumer stream would over-connect: in `grep x <(producer)` the
filename is an ordinary argument to a non-sink and the content never reaches execution. So an input
process substitution is a typed **ephemeral input resource** whose content is the producer stream
scope (section 4.6), and an output process substitution is an ephemeral **output resource** whose
content is what the writer emits. The ephemeral resource connects to a flow edge **only where syntax
or a known sink reads or writes that filename**: an input substitution on shell stdin
(`bash < <(...)`), a shell script-file operand that is a substitution (`bash <(...)`, `source <(...)`),
or a command whose stdout is redirected into an output substitution (`cmd > >(...)`). The scanner
already parses these constructs (`_consume_process_substitution`).
See <https://www.gnu.org/software/bash/manual/bash.html#Process-Substitution>.

### 5.5 Producer stdout (generic may-output; no producer-identity trust)

Every unresolved producer's stdout is a join of authored possibilities, with no assumption about
the head's semantics:

```text
stdout(unresolved producer) = Join(
    OUTSIDE,                                          # may emit anything / nothing
    Concat(authored argv payload words, in order),   # e.g. printf 'doc-' 'lattice...'
    authored stdin content                           # e.g. cat passthrough of a heredoc body
)
```

The argv-derived and stdin-derived possibilities are **separate alternatives** (Join), never
concatenated to each other. This closes the `printf` split (ordered argv composition) and the
`cat > task.sh <<EOF` handoff (stdin passthrough) without trusting any head. `curl | bash` stays
certified because neither authored alternative bears a marker and `OUTSIDE` carries the rest. A
producer the phase-1 resolver classifies as doc-lattice keeps its existing finding-path treatment.

### 5.6 Ordered descriptor binding (last binding wins)

Pipe, heredoc, herestring, static read, and static write are **not** independent content edges.
Bash establishes pipeline connections first, then processes each command's redirections
left-to-right, and the **final binding of each descriptor** determines byte flow
(<https://www.gnu.org/software/bash/manual/html_node/Pipelines.html>,
<https://www.gnu.org/software/bash/manual/html_node/Redirections.html>). Evidence therefore records
a per-command **ordered redirection-event list** `(ordinal, operator, effective descriptor, target)`,
and the taint pass resolves byte flow by replay:

1. install the pipeline endpoints (a `|` binds the producer's stdout to the consumer's descriptor 0);
2. replay the command's redirection events in order, each rebinding its effective descriptor to its
   target (a file resource, a heredoc/herestring body, or `/dev/null`);
3. create content edges **only from the resulting final bindings** -- the final reader of descriptor
   0 is the command's stdin content; the final writer of each output descriptor routes that
   descriptor's content to the bound resource.

Consequences the independent-edge model got wrong:

- `printf ... | bash <<<'true'` **certifies**: the pipe binds `bash` descriptor 0 to `printf`, then
  the herestring rebinds descriptor 0 to `true`; the marker-bearing pipe is not the final stdin.
- `printf ... > task.sh > /dev/null; bash task.sh` **certifies**: descriptor 1's final binding is
  `/dev/null`, so `task.sh` is created/truncated (a filesystem side effect: the resource key exists
  with empty content) but receives no stdout. Reversing to `> /dev/null > task.sh` **refuses**.

Earlier `>` targets retain their truncation side effect (the resource becomes an empty-content write)
but do not receive the producer's output. **Descriptor scope:** only descriptor 1 (stdout) carries
the section 5.5 may-output content into its bound resource; a write bound to another output
descriptor (`2>`, and merges like `&>`/`2>&1` under the FD-alias limitation) routes `OUTSIDE` content,
disclosed, since the may-output rule models stdout composition, not stderr. Only descriptor 0 input
bindings feed a consumer's stdin.

## 6. Sinks, source selection, and the refusal rule

A **sink** is a port where authored content becomes executed shell in this body:

- `eval` -- its arguments are joined with a **space-transfer inserted between argument ports**
  (<https://www.gnu.org/software/bash/manual/bash.html#Bourne-Shell-Builtins>), so `eval doc- lattice`
  (two args -> `doc- lattice`) certifies while `eval doc-lattice` refuses.
- shell dispatch `-c` payload word.
- shell **stdin**, only when invocation semantics select it.
- execution of a static-path resource written in this body.

**Shell source-selector classifier.** "Resolves to a shell" is insufficient; a bounded classifier
decides, from the shell command's own arguments, which port is the code sink
(<https://www.gnu.org/software/bash/manual/bash.html#Invoking-Bash>): `-c` selects the payload word;
a remaining non-option argument selects a script-file operand; `-s` or no remaining argument selects
stdin. So `bash -c 'echo ok' <<EOF...EOF` does not treat the heredoc as executed shell. The
recognized shell heads are the phase-1 dispatch shells (`sh`, `bash`, `dash`, `zsh`, `ksh`, and their
`.exe` and restricted variants), matched on the **basename-normalized** head so a path-qualified
shell (`/bin/bash -c "$X"`, `/usr/bin/env`-resolved paths) is still recognized; a head the
classifier does not recognize as a shell is not a shell sink.

**Ambiguous selection fails closed.** When the selector itself is not statically resolvable -- an
option word that is dynamic or external, as in `bash "$OPT" "$X"` where `$OPT` could be `-c` -- the
classifier cannot prove which port is the code sink. External data supplies no marker character
here; it only chooses the sink. The rule is to **refuse whenever an unresolved selector could choose
a marker-capable authored argv or stdin port**: the analysis considers every port the selector could
select and refuses if any is marker-capable. A shell invocation whose candidate ports are all
marker-free still certifies.

**Static-path execution forms** (all read the resource's content into a sink): shell **script-file
operand** (`bash task.sh`), **direct path head** (`./task.sh`, `/abs/task.sh`), and **`source`/`.`**
(`source task.sh`, `. task.sh`). May-flow links a static execution sink to a matching write
**anywhere** in the body, not only earlier, consistent with the scanner's deliberate
reachability-insensitivity.

**Effective-head resolution reuses the full phase-1 launcher grammar, preserving lookup provenance.**
Sink classification must not hand-list a wrapper subset; a subset leaves the omitted launchers as
sink bypasses (`builtin eval "$X"`, `uv run bash -c "$X"`). The effective head for every sink --
`eval`, shell `-c`, shell stdin, and static-path execution alike -- is resolved by the same launcher
and wrapper grammar the finding path already implements: `env`, `command`, `exec`, `time`, `builtin`
(`shell_scanner.py:1933`), `coproc`, and the `uv run` / `uvx` / `uv tool run` chains
(`shell_scanner.py:2175`, `:2260`). The resolver carries the existing `external_lookup` provenance
(`_ResolvedIndex.external_lookup`, `shell_scanner.py:468`; `_skip_shell_prefixes`,
`shell_scanner.py:1889`): `env`, `exec`, and external `time` cross to a PATH `execve` that can never
reach a shell builtin, while `command`, `builtin`, and the `time` keyword keep shell lookup. The
builtin sinks `eval`, `source`, and `.` are therefore sinks only on a non-external lookup:
`command eval "$X"` and `builtin eval "$X"` are `eval` sinks, but `env eval "$X"` and `exec eval "$X"`
are not (there is no `eval` executable on `PATH`; the wrapper would fail or run an unrelated binary).
A shell reached through a launcher (`uv run bash -c "$X"`) is a `-c` sink after the launcher resolves
its command; the `-c` / stdin / script-file shell sinks are otherwise unaffected by the provenance
bit, since a shell resolves through `execve` regardless.

**Refusal rule (verbatim).** Refuse when authored fragments can compose `doc[-_.]+lattice` along a
modeled content flow, and that content reaches an execution sink. Operationally: evaluate the sink
port's content value, resolving refs through the tables; if any authored-only alternative passes
through accept, the pass returns a refusal with reason
`"authored marker flow reaches an execution sink"`. Resolved doc-lattice invocations are
unaffected; marker-free flow, and flow whose only marker-capable alternative depends on an `OUTSIDE`
contribution, certify (the latter disclosed).

## 7. Cyclic references, caps, and fail-closed

The evaluated domain is finite: finitely many transfer relations (with the bounded suffix-aware
component) over a fixed DFA, alternative sets capped. Resolving `VariableRef` / `StreamRef` /
resource reads is a monotone **least fixed point** over that finite lattice, computed by worklist
iteration to convergence -- not by evaluation order. A self- or mutually-referential variable
(`X="$X..."`, `X`->`Y`->`X`) converges to a finite value; the fixed point, not end-of-scan timing,
is what makes the analysis sound for loops and cyclic references.

Every bound fails closed (returns the taint verdict -> `_ShellScanIncomplete`), never certifies by
giving up:

- a cap on alternatives per content value (join width);
- a cap on total `ContentExpr` nodes;
- a cap on table entries (variables, resource keys, stream scopes, pipe / substitution /
  process-substitution edges, `Choice` and brace-expansion alternatives);
- a cap on **deterministic fixed-point work**, charged as edge relaxations / successful lattice
  updates, **not** a raw worklist-loop count (which could vary with scheduling order despite
  identical evidence, making exhaustion order-dependent and tests unstable).

Work that extends scanning charges the existing `_ScanBudget`; the taint pass adds its own
deterministic counters for the above.

## 8. Command-evidence attachment (the scanner change)

The one structural scanner change: a **monotonic command ID assigned at flush** that survives the
flush, so evidence parsed after a command is gone can attach to its owner.

- `_flush_command` (`shell_scanner.py:1010`) assigns the ID and records the command's ports into the
  taint state under that ID.
- **Heredoc owner attachment.** Heredoc bodies are consumed at the newline boundary
  (`shell_scanner.py:982-1008`), after the owning command flushed, and `reset_command` does not
  clear `state.heredocs` (`shell_scanner.py:519-525`), so `;`-separated commands can flush between a
  `<<EOF` and its newline. The owner ID is stamped onto **the heredocs a command registered since the
  previous flush** (tracked by count at flush time), never onto a single "last flushed command." A
  heredoc routes to the owner's stdin port only when its descriptor is 0 (default); `3<<EOF` targets
  descriptor 3 and is not a stdin sink.
- **Ordered redirection events.** `_redirection_at` parses the numeric descriptor prefix but discards
  it (`shell_scanner.py:1040-1056`); phase 2 records, per command, the ordered event list
  `(ordinal, operator, effective descriptor, target)` over reads, writes, heredocs, and herestrings,
  so the section 5.6 last-binding-wins replay is possible. Descriptor duplication forms (`<&`, `>&`,
  `&>`, `2>&1`) are not resolved and stay under the disclosed FD-alias limitation (section 10).
- **Pipe edges use a pending producer, not an eager consumer ID.** Because IDs are assigned at flush,
  the consumer does not exist when `|` is scanned (`shell_scanner.py:739-768`, `:1010`). At the `|`
  the just-flushed producer is recorded as a **pending producer edge**; the next command to flush
  finalizes it as producer -> consumer. This also carries the pipeline's stdin/stdout routing for the
  `pipeline` stream scope (section 4.6).
- **Process-substitution edges** are recorded at process-substitution parsing as the typed ephemeral
  resources of section 5.4, linked to a sink only where syntax reads or writes the filename.
- **Stream-scope evidence.** Each command substitution, subshell/brace group, and pipeline emits a
  `_StreamScopeEvidence` (section 4.6) with its kind, parent, and the output structure derived from
  the control flow the scanner already tracks (sequence operators, `case` arms via the existing
  `_CaseScanState`, `if`/loop keywords); scopes whose structure cannot be derived fail closed.
- **Static read edges.** A static `< path` redirection records a `ResourceRef` input on the reading
  command's stdin port when the descriptor is 0 (section 5.2), parsed where `_consume_redirection`
  currently discards the operand (`shell_scanner.py:1058-1080`).

No rewrite of the scan loop; the changes are localized to the flush, newline, operator, redirection,
and scope-parsing boundaries the issue identifies.

## 9. Tests

Pure-domain suite `tests/test_github_ci_shell_taint.py` drives synthetic `_CommandEvidence` /
`ContentExpr`: join-vs-compose, `OutsideGap` epsilon/barrier alternatives, `StripTrailingNewlines`,
fixed-point convergence on cyclic references, and each cap's fail-closed exhaustion. End-to-end rows
are added to `tests/test_github_ci_shell_scanner.py`, plus an `audit.py` integration case (a PR run
body smuggling via file handoff exits 2 end to end).

**End-to-end refusal rows must prove phase 2 fired, not phase 1.** Phase 1 already refuses a complete
marker in one assignment word (`tests/test_github_ci_shell_scanner.py:752-756`, `2422-2428`), so
refusal rows use genuinely split authored content and assert the exact phase-2 reason
(`"authored marker flow reaches an execution sink"`) so a phase-1 refusal cannot satisfy the
test by accident:

```bash
X=doc-
X+='lattice reconcile'
eval "$X"
```

Mandatory REFUSE (each verified under real bash as a fixture row, all split so no single word bears
the marker): the two-word `printf '%s%s\n' 'doc-' 'lattice reconcile' > task.sh; bash task.sh`; the
same escape through the `./` variant `printf ... > task.sh; bash ./task.sh` (pinning that `task.sh`
and `./task.sh` normalize to one resource key); the `cat > task.sh <<EOF` heredoc handoff; the split
`X=doc-; X+='lattice reconcile'; eval "$X"`; a split pipeline producer -> `bash`; a split herestring
`bash <<< "$X"`; the input process substitution `bash < <(printf '%s%s\n' doc- 'lattice reconcile')`;
the multi-command substitution scope `eval "$(printf doc-; printf 'lattice reconcile')"`; the
compound-group handoff `{ printf doc-; printf 'lattice reconcile'; } > task.sh; bash task.sh`; the
in-word parameter default `unset X; eval "${X:-doc-}lattice reconcile"`; the assign-default form
`unset X; eval "${X:=doc-}lattice"` (also pinning that the assigned `X` refuses a later `eval "$X"`);
the brace fan-out `eval doc-{lattice,noop}` and the adjacency fan-out
`printf %s {doc-,lattice} | bash` (proving fan-out composes neighbors, which a `Choice` model would
miss); the loop-binding case `for X in doc- lattice; do printf %s "$X"; done | bash`; the `case`
fallthrough `case a in a) printf doc- ;& *) printf lattice ;; esac | bash` (`;&` composes across
arms); the static file read `printf ... > task.sh; bash < task.sh`; the launcher-head bypasses
`X=doc-; X+=lattice; builtin eval "$X"` and `X=doc-; X+=lattice; uv run bash -c "$X"`; and the
ambiguous selector `X=doc-; X+='lattice reconcile'; bash "$OPT" "$X"`. The
trailing-newline-strip case uses an executable heredoc-backed substitution rather than relying on any
one command's newline behavior:

```bash
eval "$(cat <<'EOF'
doc-
EOF
)lattice reconcile"   # substitution output "doc-\n" strips to "doc-", spliced to "lattice" -> refuse
```

The `while` test-list case pins that the closure includes the pre-body test output (section 4.6),
composing `doc-` from the body with `lattice` from the next iteration's test:

```bash
i=0; P='#\n'
while { printf %b "$P"; test "$i" -lt 1; }; do printf doc-; P=lattice; i=1; done | bash
```

The descriptor-order case pins section 5.6 last-binding-wins: `printf ... > /dev/null > task.sh;
bash task.sh` refuses (descriptor 1's final binding is `task.sh`), while the reversed
`printf ... > task.sh > /dev/null; bash task.sh` and `printf ... | bash <<<'true'` certify.

Mandatory CERTIFY (must not regress): `curl ... | bash`; `echo 'make build' > run.sh; bash run.sh`;
`eval doc- lattice` (space barrier); `bash -c 'echo ok' <<EOF...EOF` (source selection -- stdin not a
sink); `bash -c 'echo hi' > doc-lattice.log` (phase-1 redirection-operand pin);
`doc${EXTERNAL}lattice` reaching a sink (disclosed external separator, certifies);
`grep x <(printf '%s%s' doc- lattice)` (process substitution read by a non-sink is not over-connected
to execution); `env eval "$X"` with a marker-capable `X` (external lookup cannot reach the `eval`
builtin, so it is not an `eval` sink), paired with `command eval "$X"` and `builtin eval "$X"` as the
REFUSE counterparts that can; and the non-zero descriptor read `printf ... > task.sh; bash 3<
task.sh` (descriptor 3, not stdin, so no shell-stdin sink), paired with the descriptor-0 form as the
REFUSE counterpart.

Full handoff verification set at ship: pytest at the repo coverage gate, Ruff check and format,
`ty`, typing boundaries, version sync, plus the read-only `successor-evaluation` corpus battery
rerun against these outcomes with results kept in-session, not committed as a fixture.

## 10. Disclosure boundary (documented limitations)

Stated as absence of evidence, not trust:

- **Cross-step / cross-job / `uses:` action / reusable-workflow handoff** -- analysis is one `run:`
  body; `audit.py` does not aggregate.
- **External content** -- unresolved-producer stdout beyond the section 5.5 may-output rule,
  external environment variables, external files (`OUTSIDE`). `curl ... | bash`, `eval "$EXTERNAL"`,
  and `doc${EXTERNAL}lattice` (separator would come from outside) certify.
- **Encoding / transform synthesis** -- `base64 -d`, `tr`, `sed` reconstructing a marker from
  non-marker authored text; no transducer models arbitrary transforms.
- **Unsupported parameter-expansion forms** -- pattern replacement (`${X/a/b}`), substring
  (`${X:off:len}`), indirection (`${!X}`), and transformation (`${X@Q}`): only their authored literal
  operands are surfaced (section 4.7); the variable-derived result is disclosed, or the pass fails
  closed when the form cannot be bounded.
- **Dynamic resource identity** -- dynamic write / execution paths, `..` / `cd` directory changes,
  rename / symlink aliasing.
- **File-descriptor aliasing** -- descriptor duplication (`<&`, `>&`) and moving descriptors; only
  the effective descriptor of a direct read / heredoc is modeled (section 5.2, section 8), not later
  reassignment of descriptor 0.
- **Pre-existing phase-1 disclosures** -- function / alias / `PATH` shadowing, dynamic executable
  names.

## 11. Docs and logistics

- README audit-limitations paragraph is extended to state the cross-command contract (certified
  means no authored marker composes to an execution sink within a `run:` body) and the section 10
  disclosure boundary, in those words rather than as a soundness claim.
- ARCHITECTURE.md gains **AD-18** recording the authored-marker cross-command taint decision: the
  step-local certification unit, the port-typed symbolic-plus-evaluated evidence model (stream-scope
  aggregation with `Sequence`/`Choice`/`Repeat` output structure, in-word `Choice` and brace
  argv fan-out, loop-variable binding, descriptor-aware stdin routing), the join-vs-compose domain,
  the effective-head resolver reusing the full launcher grammar with `external_lookup` provenance,
  the shell source-selector and its ambiguous-selection fail-closed rule, the fixed-point and
  fail-closed caps, and the disclosed boundary.
- No CHANGELOG entry: `ci audit` is unreleased (`[Unreleased]`), consistent with the phase-1 and
  retain-decision precedent.
- Branch off `main` (`93a9ee3`) referencing #110. Implementation via subagent-driven development per
  the repo's Fable delegation policy, with the full handoff verification set plus the corpus battery.
