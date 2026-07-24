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
emitted, and phase-1 in-word refusals still fire. Phase 2 only adds refusals for cross-command
marker flow.

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
the 3,031-line scanner focused and gives phase 2 an independently testable unit driven by synthetic
evidence, mirrored at `tests/test_github_ci_shell_taint.py` per the repo convention.

The taint pass runs once, after a top-level scan completes, and returns a pure
`(verdict, reason)`. The translation to `_ShellScanIncomplete` happens inside
`scan_doc_lattice_invocations` (`shell_scanner.py:1648-1659`), the common result path, so both it
and `direct_doc_lattice_invocations` inherit the identical verdict and cannot diverge. A refusal
becomes `ShellScanResult.incomplete_reason` on the `scan_*` API and a raised `_ShellScanIncomplete`
on the `direct_*` API, exactly as bounded-scan exhaustion does today.

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
    | SubstitutionRef(command_id)    # $(cmd) / `cmd`, spliced with trailing newlines stripped
    | Concat(parts...)               # ordered, sequential composition
    | OutsideGap                     # authored-external boundary (section 4.4)
```

`Concat` is the only operator that composes sequentially. Alternatives (competing definitions,
truncating writes, branches) are represented by *joining* content values in the tables (section 5),
never by `Concat`.

### 4.2 Content channels are distinct ports

`_CommandEvidence` does not carry one flat ordered summary. It carries typed ports that never
compose across each other:

- **argv** -- one `ContentExpr` per argv position, in order;
- **assignments** -- name -> `ContentExpr` for each assignment prefix or standalone assignment;
- **stdin** -- a heredoc body, a herestring, or an incoming pipe / input process-substitution link;
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

- **Sequential composition** (`Concat`, ordered argv fragments, `X+=`, `>>`, a heredoc feeding its
  command, pipe producer -> consumer): relational composition, elementwise across alternative sets;
  the left's exit states feed the right's entry states. A marker straddling the boundary (`doc-`
  then `lattice`) is caught because composition threads the mid-marker DFA state across the join.
- **Join** (competing `X=`, truncating `>`, branches): set union of alternatives. Unrelated
  `doc-` and `lattice` remain separate alternatives; neither alone reaches accept.

A value is **marker-capable** if some alternative, entered at the DFA start state, passes through
accept.

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
(<https://www.gnu.org/software/bash/manual/bash.html#Command-Substitution>). So
`eval "$(producer emitting 'doc-\n')lattice"` can execute `doc-lattice` even though raw-stdout
composition would not match. `SubstitutionRef` therefore evaluates as `StripTrailingNewlines(stdout)`.
A plain transfer relation loses suffix information, so the evaluated domain carries a **finite
suffix-aware component**: a transducer summary recording the exit-state set reachable at trailing-
newline-run boundaries, so strip-then-concat is representable. If that summary cannot be computed
within caps, the pass fails closed.

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

Resource-key identity: `task.sh` and `./task.sh` normalize to the same key when no modeled directory
change intervenes (minimum static-path equivalence). `..`, `cd`, symlinks, and filesystem aliases
are **not** modeled and stay disclosed. A dynamic redirection target is a distinct typed
`DynamicResource` key (no modeled write edge), never an `OutsideGap`.

### 5.3 Pipe edges

A `|` records producer-id -> consumer-id; the producer's stdout port becomes the consumer's stdin
port. A producer whose stdout carries no authored marker (for example `curl`, whose stdout reduces
to `OUTSIDE`) gives the consumer an `OUTSIDE` stdin, so `curl | bash` certifies.

### 5.4 Process substitution edges

Input and output process substitutions (`cmd <(producer)`, `cmd >(consumer)`,
`bash < <(producer)`) are modeled as **pipe-like producer/consumer stream edges**, since bash
defines them as producer/consumer streams
(<https://www.gnu.org/software/bash/manual/bash.html#Process-Substitution>) and the scanner already
parses the constructs (`_consume_process_substitution`). An input process substitution feeding a
shell stdin is a stream edge into that stdin port.

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
stdin. So `bash -c 'echo ok' <<EOF...EOF` does not treat the heredoc as executed shell.

**Static-path execution forms** (all read the resource's content into a sink): shell **script-file
operand** (`bash task.sh`), **direct path head** (`./task.sh`, `/abs/task.sh`), **`source`/`.`**
(`source task.sh`, `. task.sh`), and each of those behind supported `env` / `command` / `exec` /
`time` wrappers. May-flow links a static execution sink to a matching write **anywhere** in the body,
not only earlier, consistent with the scanner's deliberate reachability-insensitivity.

**Refusal rule (verbatim).** Refuse when authored fragments can compose `doc[-_.]+lattice` along a
modeled content flow, and that content reaches an execution sink. Operationally: evaluate the sink
port's content value, resolving refs through the tables; if any authored-only alternative passes
through accept, the pass returns a refusal with reason
`"cross-command marker flow reaches an execution sink"`. Resolved doc-lattice invocations are
unaffected; marker-free flow, and flow whose only marker-capable alternative depends on an `OUTSIDE`
contribution, certify (the latter disclosed).

## 7. Cyclic references, caps, and fail-closed

The evaluated domain is finite: finitely many transfer relations (with the bounded suffix-aware
component) over a fixed DFA, alternative sets capped. Resolving `VariableRef` / `SubstitutionRef` /
resource reads is a monotone **least fixed point** over that finite lattice, computed by worklist
iteration to convergence -- not by evaluation order. A self- or mutually-referential variable
(`X="$X..."`, `X`->`Y`->`X`) converges to a finite value; the fixed point, not end-of-scan timing,
is what makes the analysis sound for loops and cyclic references.

Every bound fails closed (returns the taint verdict -> `_ShellScanIncomplete`), never certifies by
giving up:

- a cap on alternatives per content value (join width);
- a cap on total `ContentExpr` nodes;
- a cap on table entries (variables, resource keys, pipe / substitution / process-substitution
  edges);
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
  previous flush** (tracked by count at flush time), never onto a single "last flushed command."
- **Pipe / process-substitution edges** are recorded at the `|` operator (`shell_scanner.py:739-768`)
  and at process-substitution parsing, linking producer and consumer IDs.

No rewrite of the scan loop; the change is localized to the flush and newline/operator boundaries the
issue identifies.

## 9. Tests

Pure-domain suite `tests/test_github_ci_shell_taint.py` drives synthetic `_CommandEvidence` /
`ContentExpr`: join-vs-compose, `OutsideGap` epsilon/barrier alternatives, `StripTrailingNewlines`,
fixed-point convergence on cyclic references, and each cap's fail-closed exhaustion. End-to-end rows
are added to `tests/test_github_ci_shell_scanner.py`, plus an `audit.py` integration case (a PR run
body smuggling via file handoff exits 2 end to end).

**End-to-end refusal rows must prove phase 2 fired, not phase 1.** Phase 1 already refuses a complete
marker in one assignment word (`tests/test_github_ci_shell_scanner.py:752-756`, `2422-2428`), so
refusal rows use genuinely split authored content and assert the exact phase-2 reason
(`"cross-command marker flow reaches an execution sink"`) so a phase-1 refusal cannot satisfy the
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
`X=doc-; X+='lattice reconcile'; eval "$X"`; a split pipeline producer -> `bash`; a split
substitution `eval "$(...'doc-\n')lattice"`; a split herestring `bash <<< "$X"`; and the input
process substitution `bash < <(printf '%s%s\n' doc- 'lattice reconcile')`.

Mandatory CERTIFY (must not regress): `curl ... | bash`; `echo 'make build' > run.sh; bash run.sh`;
`eval doc- lattice` (space barrier); `bash -c 'echo ok' <<EOF...EOF` (source selection -- stdin not a
sink); `bash -c 'echo hi' > doc-lattice.log` (phase-1 redirection-operand pin); and
`doc${EXTERNAL}lattice` reaching a sink (disclosed external separator, certifies).

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
- **Dynamic resource identity** -- dynamic write / execution paths, `..` / `cd` directory changes,
  rename / symlink / FD aliasing.
- **Pre-existing phase-1 disclosures** -- function / alias / `PATH` shadowing, dynamic executable
  names.

## 11. Docs and logistics

- README audit-limitations paragraph is extended to state the cross-command contract (certified
  means no authored marker composes to an execution sink within a `run:` body) and the section 10
  disclosure boundary, in those words rather than as a soundness claim.
- ARCHITECTURE.md gains **AD-18** recording the authored-marker cross-command taint decision: the
  step-local certification unit, the port-typed symbolic-plus-evaluated evidence model, the
  join-vs-compose domain, the fixed-point and fail-closed caps, and the disclosed boundary.
- No CHANGELOG entry: `ci audit` is unreleased (`[Unreleased]`), consistent with the phase-1 and
  retain-decision precedent.
- Branch off `main` (`93a9ee3`) referencing #110. Implementation via subagent-driven development per
  the repo's Fable delegation policy, with the full handoff verification set plus the corpus battery.
