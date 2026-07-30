"""Executable classification of every fail-closed guard origin in the CI shell scanner.

This registry *is* the guard inventory. A guard is classified by appearing here, and every entry
carries the executable evidence for its classification rather than a prose claim or a test name:

- `ReachableWitness` names an authored script (and optional shrunk limits) that drives the public
  scan path and must return that exact guard origin identifier.
- `InvariantWitness` records why authored input cannot reach the origin, plus a boundary script
  that exercises the nearest reachable state transition and the outcome it must produce.

Anything absent from both lists is unclassified rollout debt, frozen in
`tests/fixtures/shell_guard_debt.json` and gated by `scripts/check_guard_inventory.py`. The
tree-local closure check in `tests/test_github_ci_shell_guards.py` requires source origins to
partition exactly into this registry and that debt snapshot, so a new guard cannot arrive
unclassified and an unclassified guard cannot be edited while keeping its frozen debt record.

See AD-20 in ARCHITECTURE.md for the durable decision this registry implements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from doc_lattice.github_ci.shell_guards import ScanLimits, ScannerLimits, TaintLimits
from doc_lattice.github_ci.shell_taint import (
    ChoiceOutput,
    CommandOutput,
    ProcessResourceTarget,
    RepeatOutput,
    ScopeOutput,
    SequenceOutput,
)


def _output_nodes(output: Any) -> list[Any]:
    """Return every node of one output expression tree, the root included.

    The two exhaustive output walks fall through to their guard on a union member they do not
    handle, so a boundary script only exercises them by building a tree they actually descend. An
    empty script builds a single childless `SequenceOutput`, which is why a predicate over these
    nodes has to count them rather than merely find one.
    """
    found: list[Any] = []
    pending: list[Any] = [output]
    while pending:
        node = pending.pop()
        found.append(node)
        if isinstance(node, SequenceOutput | ChoiceOutput):
            pending.extend(node.parts)
        elif isinstance(node, RepeatOutput):
            pending.append(node.part)
    return found


def _scope_output_nodes(evidence: Any) -> list[Any]:
    """Return every output node across every scope the boundary script built."""
    return [
        node
        for scope in evidence.scopes
        for root in (scope.output, scope.entry)
        if root is not None
        for node in _output_nodes(root)
    ]


@dataclass(frozen=True, slots=True)
class ReachableWitness:
    """One authored input that provably reaches a guard origin through the public scan path.

    Attributes:
        origin_id: The guard origin identifier the scan must report.
        script: Literal Bash source handed to `scan_doc_lattice_invocations`.
        limits: Shrunk deterministic caps, when the guard is a resource bound no script of
            reviewable size can exhaust. `None` runs the witness under production caps.
        control_script: An input that exercises the same construct without reaching the guard,
            proving the witness isolates this guard rather than the surrounding machinery. The
            control always runs under production caps, so a shrunk-limits witness and its control
            can be the same script and still differ in outcome.
        control_guard_id: The guard the control reports, or `None` when the control certifies.
    """

    origin_id: str
    script: str
    limits: ScanLimits | None = None
    control_script: str | None = None
    control_guard_id: str | None = None


@dataclass(frozen=True, slots=True)
class InvariantWitness:
    """One guard origin that authored input cannot reach, with its nearest reachable boundary.

    Attributes:
        origin_id: The guard origin identifier claimed unreachable from authored input.
        rationale: Why no authored input can satisfy the guard's condition. Non-empty.
        boundary_script: Authored input that drives the same validation to its nearest reachable
            state, so a change that makes the origin reachable shows up as a changed outcome.
        boundary_evidence: Predicate over the evidence the boundary script produces, asserting it
            actually contains the structure this guard inspects. Required, and deliberately without
            a default: a permissive default would restore the hole this field closes, because these
            guards sit in validators that run for every scan, so "the condition was evaluated"
            holds even for a script that builds nothing for it to inspect. The predicate must also
            be *false* for the evidence an empty script produces, which is the builder's floor of
            one root scope and nothing else. A predicate that holds there discriminates nothing and
            is vacuous however it is spelled, `lambda _: True` being only the plainest form.
        boundary_guard_id: Guard origin the boundary script must report, or `None` when it
            certifies.
    """

    origin_id: str
    rationale: str
    boundary_script: str
    boundary_evidence: Callable[[Any], bool]
    boundary_guard_id: str | None = None


_EVIDENCE_SELF_CHECK = (
    "A `_MalformedTaintEvidence` origin in `_validate_nested_evidence` and its helpers is a "
    "backstop against a scanner defect, not a response to authored input: the evidence it "
    "inspects is built by `_EvidenceBuilder`, which allocates every identifier it later "
    "references and emits only declared scope kinds, redirection directions, and output nodes. "
    "No authored script can make the builder emit a dangling reference or an undeclared member, "
    "so the condition is false for every input the public scan path can construct. The boundary "
    "witness drives the same validation over the structure this guard inspects."
)


REACHABLE_WITNESSES: tuple[ReachableWitness, ...] = (
    ReachableWitness(
        "scanner.active-expansion.recursion-depth",
        "x=$((1 + 2))",
        limits=ScanLimits(scanner=ScannerLimits(max_recursion_depth=0)),
    ),
    ReachableWitness(
        "scanner.ansi-c.nul-decode",
        "doc-lattice $'linear\\0suffix'",
    ),
    ReachableWitness(
        "scanner.array-assignment.element-subscript",
        "args=([1+(2)]=doc-lattice linear)",
    ),
    ReachableWitness(
        "scanner.array-assignment.recursion-depth",
        "X=({1..1000}); true",
        limits=ScanLimits(scanner=ScannerLimits(max_recursion_depth=0)),
    ),
    ReachableWitness(
        "scanner.budget.step-limit",
        "bash",
        limits=ScanLimits(scanner=ScannerLimits(max_scan_steps=0)),
    ),
    ReachableWitness(
        "scanner.case.arm-limit-at-finish",
        "case x in a) :; esac",
        limits=ScanLimits(scanner=ScannerLimits(max_case_arms=0)),
        control_script="case x in a) :; esac",
        control_guard_id=None,
    ),
    ReachableWitness(
        "scanner.case.arm-limit-at-terminator",
        "case x in a) :;; esac",
        limits=ScanLimits(scanner=ScannerLimits(max_case_arms=0)),
        control_script="case x in a) :;; esac",
        control_guard_id=None,
    ),
    ReachableWitness(
        "scanner.case.dynamic-branch-limit",
        "case $x in case) echo a ;; *) echo b ;; esac",
        limits=ScanLimits(scanner=ScannerLimits(max_case_dynamic_branches=0)),
    ),
    ReachableWitness(
        "scanner.case.dynamic-header",
        "case x y in a) :;; esac",
    ),
    ReachableWitness(
        "scanner.case.keyword-shaped-pattern",
        "case $x in esac) echo a ;; *) echo b ;; esac",
    ),
    ReachableWitness(
        "scanner.commands.recursion-depth",
        "{ doc-lattice linear; }",
        limits=ScanLimits(scanner=ScannerLimits(max_recursion_depth=0)),
    ),
    ReachableWitness(
        "scanner.control-flow.duplicate-loop-do",
        "for x in a; do :; do",
    ),
    ReachableWitness(
        "scanner.control-flow.unfinished-if",
        "if true; fi\n",
    ),
    ReachableWitness(
        "scanner.control-flow.unfinished-loop",
        "for x in a; done",
    ),
    ReachableWitness(
        "scanner.control-flow.unterminated-at-scope-end",
        "case x in",
    ),
    ReachableWitness(
        "scanner.control-flow.unterminated-at-terminator",
        "(if true)",
    ),
    ReachableWitness(
        "scanner.descriptor.digit-limit",
        "echo hi >&99999999999999999999999999999999999999999999999999999999999999999999",
    ),
    ReachableWitness(
        "scanner.doc-lattice.unresolved-root-option",
        "doc-lattice --future-root-opt X linear",
    ),
    ReachableWitness(
        "scanner.env-option.ambiguous-long-option",
        "env --zzz doc-lattice check",
    ),
    ReachableWitness(
        "scanner.env-option.split-string-short-option",
        "env -S doc-lattice check",
    ),
    ReachableWitness(
        "scanner.env-option.unscannable-value",
        "env -u",
    ),
    ReachableWitness(
        "scanner.env-option.unsupported-short-option",
        "env -zz doc-lattice check",
    ),
    ReachableWitness(
        "scanner.env-prefix.argv-changing-word",
        "env {a,b}=1 doc-lattice check",
    ),
    ReachableWitness(
        "scanner.env-prefix.dynamic-word",
        'env ./"$TOOLS"/doc-lattice linear',
    ),
    ReachableWitness(
        "scanner.env-prefix.quoted-dynamic-assignment",
        'env FOO="$@" harmless',
    ),
    ReachableWitness(
        "scanner.env-prefix.split-string-long-option",
        "env --s 'doc-lattice linear'",
    ),
    ReachableWitness(
        "scanner.env-prefix.split-string-option",
        "$@ env -S 'doc-lattice linear'",
    ),
    ReachableWitness(
        "scanner.env-prefix.unquoted-dynamic-assignment",
        "env X=$Y doc-lattice check",
    ),
    ReachableWitness(
        "scanner.env-prefix.unsupported-option",
        "env --debug=1 doc-lattice check",
    ),
    ReachableWitness(
        "scanner.exec-option.requires-separate-argv0",
        "exec -z ignored doc-lattice linear",
    ),
    ReachableWitness(
        "scanner.exec.scope-redirection",
        "exec {v}>&1; echo doc- >&$v",
    ),
    ReachableWitness(
        "scanner.executable.argv-expansion",
        "{doc-lattice,} linear",
    ),
    ReachableWitness(
        "scanner.executable.dynamic-relative-path",
        './"$TOOLS"/doc-lattice linear',
    ),
    ReachableWitness(
        "scanner.executable.locale-translated",
        '$"harmless" linear',
    ),
    ReachableWitness(
        "scanner.heredoc.locale-translated-delimiter",
        'cat <<$"harmless"\nEOF\ndoc-lattice linear\nharmless\n',
    ),
    ReachableWitness(
        "scanner.invocations.limit",
        "doc-lattice linear",
        limits=ScanLimits(scanner=ScannerLimits(max_invocations=0)),
    ),
    ReachableWitness(
        "scanner.launcher.nesting-limit",
        "uv run --no-sync pytest",
        limits=ScanLimits(scanner=ScannerLimits(max_launcher_nesting_depth=0)),
    ),
    ReachableWitness(
        "scanner.legacy-substitution.unterminated",
        "echo `oops\nprintf '%s%s\\n' doc- 'lattice reconcile' > run.sh\nbash run.sh\n",
    ),
    ReachableWitness(
        "scanner.marker.non-invocation-command",
        "echo DOC_LATTICE",
    ),
    ReachableWitness(
        "scanner.simple-command.ambiguous-command-position",
        "uvx {a,b}",
    ),
    ReachableWitness(
        "scanner.simple-command.subcommand-argv-expansion",
        "doc-lattice chec*",
    ),
    ReachableWitness(
        "scanner.source.character-limit",
        "bash",
        limits=ScanLimits(scanner=ScannerLimits(max_source_chars=0)),
    ),
    ReachableWitness(
        "scanner.time-prefix.dynamic-word",
        'env time "$*" doc-lattice linear',
    ),
    ReachableWitness(
        "scanner.time-prefix.option",
        "\\time -f '%e' doc-lattice linear",
    ),
    ReachableWitness(
        "scanner.uv-tool.option-before-run-selector",
        "uv tool -q install doc-lattice",
    ),
    ReachableWitness(
        "scanner.uv-tool.run-selector-argv-expansion",
        "uv tool {run,doc-lattice} linear",
    ),
    ReachableWitness(
        "scanner.uv.dynamic-global-word-argv-expansion",
        "uv {run,doc-lattice} linear",
    ),
    ReachableWitness(
        "scanner.uv.unresolved-global-option",
        "uv -Z run doc-lattice linear",
    ),
    ReachableWitness(
        "scanner.uv.unresolved-global-option-at-payload",
        "uv --project $P --zzz run doc-lattice check",
    ),
    ReachableWitness(
        "scanner.uv.unresolved-launcher-option",
        "uvx -qZ doc-lattice linear",
    ),
    ReachableWitness(
        "scanner.word.extglob-opener",
        "shopt -s extglob\ndoc-lattice *(reconcile) --all",
    ),
    ReachableWitness(
        "taint.bash-env.source-state",
        "printf '%s\\n' 'A=doc-' '\"${A}lattice\" reconcile' > env.sh\nBASH_ENV=env.sh\nbash -c :",
    ),
    ReachableWitness(
        "taint.brace.alpha-sequence-limit",
        "echo {a..z}{0..9}",
        limits=ScanLimits(taint=TaintLimits(max_brace_expansions=0)),
    ),
    ReachableWitness(
        "taint.brace.depth-limit",
        "echo {a,$X}",
        limits=ScanLimits(taint=TaintLimits(max_brace_depth=0)),
    ),
    ReachableWitness(
        "taint.brace.expansion-limit",
        "echo {a,$X}",
        limits=ScanLimits(taint=TaintLimits(max_brace_expansions=0)),
    ),
    ReachableWitness(
        "taint.brace.integer-digit-limit",
        "eval {1.." + "9" * 4_992 + "}",
    ),
    ReachableWitness(
        "taint.brace.numeric-sequence-limit",
        "echo {1..300}",
        limits=ScanLimits(taint=TaintLimits(max_brace_expansions=0)),
    ),
    ReachableWitness(
        "taint.builtin-writer.unsupported-after-nameref-routing",
        "eval 'declare -n A=B; declare -n B=A'; eval \"$A\"lattice",
    ),
    ReachableWitness(
        "taint.builtin-writer.unsupported-before-context",
        'printf -v V %b "$Y"; eval "$V"lattice',
    ),
    ReachableWitness(
        "taint.child-shell.positional-operand-limit",
        "bash -o ta* -c 'echo hi'",
        limits=ScanLimits(taint=TaintLimits(max_table_entries=2)),
    ),
    ReachableWitness(
        "taint.contextualization.edge-limit",
        "A=doc-",
        limits=ScanLimits(taint=TaintLimits(max_edges=0)),
    ),
    ReachableWitness(
        "taint.contextualization.effect-fixed-point-limit",
        "f() { X=safe; }; eval '${X:=doc-}lattice'",
        limits=ScanLimits(taint=TaintLimits(max_fixed_point_updates=0)),
    ),
    ReachableWitness(
        "taint.contextualization.entry-alternative-limit",
        'f() { eval "$*"lattice; }; f doc-',
        limits=ScanLimits(taint=TaintLimits(max_alternatives=0)),
    ),
    ReachableWitness(
        "taint.contextualization.entry-fixed-point-limit",
        'f() { eval "$*"lattice; }; f doc-',
        limits=ScanLimits(taint=TaintLimits(max_fixed_point_updates=0)),
    ),
    ReachableWitness(
        "taint.descriptor.input-alias-unresolved",
        "exec 3< unrelated.txt\nbash 0<&3\n",
    ),
    ReachableWitness(
        "taint.descriptor.output-alias-unresolved",
        "exec 3> run.sh\nprintf '%s%s\\n' doc- 'lattice reconcile' >&3\nbash run.sh\n",
    ),
    ReachableWitness(
        "taint.eval-ansi-c.unrepresentable-codepoint",
        'function time { eval "$X"lattice; }; X=doc- eval "$\'\\U00110000time\'"',
    ),
    ReachableWitness(
        "taint.eval-array.compound-assignment",
        "eval 'A=(1 2)'; eval \"$A\"lattice",
    ),
    ReachableWitness(
        "taint.eval-array.declaration-builtin",
        "eval 'declare -A A'; eval \"$A\"lattice",
    ),
    ReachableWitness(
        "taint.eval-array.element-assignment-operand",
        "eval 'A[0]=doc-'; eval \"$A\"lattice",
    ),
    ReachableWitness(
        "taint.eval-array.element-assignment-word",
        "eval 'readonly A[1]=x'; eval \"$A\"lattice",
    ),
    ReachableWitness(
        "taint.eval-descriptor.digit-limit",
        "eval 'X=doc- >&" + "1" * 260 + '\'; eval "$X"lattice',
    ),
    ReachableWitness(
        "taint.eval-nameref.non-static-target",
        "eval 'declare -n R=$Y'; eval \"$R\"lattice",
    ),
    ReachableWitness(
        "taint.eval-nameref.unset-with-flag",
        "eval 'unset -n R'; eval \"$R\"lattice",
    ),
    ReachableWitness(
        "taint.eval-payload.missing-metadata",
        'eval "X=\'"; eval "$X"lattice',
    ),
    ReachableWitness(
        "taint.eval-reparse.backquote-substitution",
        "eval 'X=`cat`'; eval \"$X\"lattice",
    ),
    ReachableWitness(
        "taint.eval-reparse.branch-depth-limit",
        "eval doc- lattice",
        limits=ScanLimits(taint=TaintLimits(max_eval_reparse_depth=0)),
    ),
    ReachableWitness(
        "taint.eval-reparse.closing-depth-limit",
        "S='${X:-" + "$(" * 831 + ")" * 831 + '}\'; eval "$S"lattice',
    ),
    ReachableWitness(
        "taint.eval-reparse.dollar-command-substitution",
        "eval 'X=$( )'; eval \"$X\"lattice",
    ),
    ReachableWitness(
        "taint.eval-reparse.expanded-branch-limit",
        "eval doc- lattice",
        limits=ScanLimits(taint=TaintLimits(max_eval_reparse_branches=0)),
    ),
    ReachableWitness(
        "taint.eval-reparse.merged-branch-limit",
        'X=doc-; eval "${X:+lattice}"',
        limits=ScanLimits(taint=TaintLimits(max_eval_reparse_branches=0)),
    ),
    ReachableWitness(
        "taint.eval-reparse.stream-depth-limit",
        "X=''; eval '${X=doc-}lattice'",
        limits=ScanLimits(taint=TaintLimits(max_eval_reparse_depth=0)),
    ),
    ReachableWitness(
        "taint.eval-syntax.alternative-limit",
        'X={a..z}; eval "$X"lattice',
        limits=ScanLimits(taint=TaintLimits(max_alternatives=3)),
    ),
    ReachableWitness(
        "taint.eval-syntax.append-depth-limit",
        'Y=lattice; eval "doc-$Y"',
        limits=ScanLimits(taint=TaintLimits(max_eval_reparse_depth=1)),
    ),
    ReachableWitness(
        "taint.eval-syntax.append-transition-reentry",
        'A={; B=$C; C=$B; eval "$A$B"',
    ),
    ReachableWitness(
        "taint.eval-syntax.brace-depth-limit",
        "eval 'X={{{{a}}}}'; eval \"$X\"lattice",
        limits=ScanLimits(taint=TaintLimits(max_brace_depth=0)),
    ),
    ReachableWitness(
        "taint.eval-syntax.variable-overlay-table-limit",
        "bash -o ta* -c 'echo hi'",
        limits=ScanLimits(taint=TaintLimits(max_table_entries=3)),
    ),
    ReachableWitness(
        "taint.eval.nested-eval-state",
        "eval 'eval Z=doc-'; eval \"$Z\"lattice",
    ),
    ReachableWitness(
        "taint.evidence.edge-limit",
        "bash 0<&3\n",
        limits=ScanLimits(taint=TaintLimits(max_edges=0)),
    ),
    ReachableWitness(
        "taint.evidence.output-scope-node-limit",
        "bash",
        limits=ScanLimits(taint=TaintLimits(max_expression_nodes=0)),
    ),
    ReachableWitness(
        "taint.evidence.table-entry-limit",
        "bash",
        limits=ScanLimits(taint=TaintLimits(max_table_entries=0)),
    ),
    ReachableWitness(
        "taint.exact-value.length-limit",
        "A=doc-",
        limits=ScanLimits(taint=TaintLimits(max_exact_value_chars=0)),
    ),
    ReachableWitness(
        "taint.flow-build.variable-key-table-limit",
        "eval 'X=doc-; Y=$X'; eval \"$Y\"lattice",
        limits=ScanLimits(taint=TaintLimits(max_table_entries=3)),
    ),
    ReachableWitness(
        "taint.flow-build.variable-write-edge-limit",
        'for f in a; do for c in x; do echo "$c"; done; done',
        limits=ScanLimits(taint=TaintLimits(max_edges=1)),
    ),
    ReachableWitness(
        "taint.flow-solve.edge-limit",
        "bash",
        limits=ScanLimits(taint=TaintLimits(max_edges=0)),
    ),
    ReachableWitness(
        "taint.flow-solve.expression-node-limit",
        "bash",
        limits=ScanLimits(taint=TaintLimits(max_expression_nodes=2)),
    ),
    ReachableWitness(
        "taint.flow-solve.fixed-point-limit",
        "bash",
        limits=ScanLimits(taint=TaintLimits(max_fixed_point_updates=0)),
    ),
    ReachableWitness(
        "taint.flow-solve.table-entry-limit",
        "A=doc-",
        limits=ScanLimits(taint=TaintLimits(max_table_entries=2)),
    ),
    ReachableWitness(
        "taint.function-effects.depth-limit",
        'f(){ X=doc-; }; g(){ local X; f; }; f; eval "$X"lattice check',
        limits=ScanLimits(taint=TaintLimits(max_function_effect_depth=0)),
    ),
    ReachableWitness(
        "taint.function-positional.dynamic-bind-argument",
        'f() { eval "$1"; }; f "$X"',
    ),
    ReachableWitness(
        "taint.function-positional.dynamic-ifs",
        'f() { local IFS="$EXTERNAL"; eval "$*"; }; f doc lattice',
    ),
    ReachableWitness(
        "taint.function-positional.effect-set-unknown-operands",
        'f() { set "$EXTERNAL" doc-; shift; eval "$1"lattice; }; f',
    ),
    ReachableWitness(
        "taint.function-positional.effect-shift-dynamic-operand",
        'f() { shift $X; eval "$1"; }; f a b',
    ),
    ReachableWitness(
        "taint.function-positional.effect-shift-non-numeric",
        'f() { shift x; eval "$1"; }; f a b',
    ),
    ReachableWitness(
        "taint.function-positional.exact-set-dynamic-operand",
        'f() { set -- $X; eval "$1"; }; f',
    ),
    ReachableWitness(
        "taint.glob-script.operand-state",
        "X=doc-; printf '%s' \"$X\"'lattice check' > task.sh; bash ta*.sh",
    ),
    ReachableWitness(
        "taint.launcher-shell.positional-state",
        "printf 'doc-\\n' | xargs -n1 sh -c '${0}lattice reconcile'",
    ),
    ReachableWitness(
        "taint.local-substitution.depth-limit",
        'X=safe; f() { local X=doc-; local Y=$X; eval "$Y"lattice; }; f',
        limits=ScanLimits(taint=TaintLimits(max_local_substitution_depth=0)),
    ),
    ReachableWitness(
        # %b decodes escapes, so it can synthesize the marker from bytes no port shows
        # literally. The control spells a representable escape and is detected as marker flow,
        # which proves the fixture isolates the unrepresentable case rather than %b itself.
        "taint.printf-b.unrepresentable-output",
        'X=$(printf %b "\\U0110FFFF"); eval "$X"lattice',
        control_script='X=$(printf %b "\\x64oc-"); eval "$X"lattice',
        control_guard_id=None,
    ),
    ReachableWitness(
        "taint.shell-script.operand-state",
        "printf '%s\\n' 'A=doc-' 'eval \"${A}lattice\"' > env.sh; bash env.sh",
    ),
    ReachableWitness(
        "taint.shell-script.positional-state",
        "printf '%s\\n' '\"$1$2\" reconcile' > s.sh; sh s.sh doc- lattice",
    ),
    ReachableWitness(
        "taint.source.glob-operand-state",
        "X=doc-; printf '%s' \"$X\"'lattice check' > task.sh; . ta*.sh",
    ),
    ReachableWitness(
        "taint.source.payload-state",
        "printf 'Y=lattice' > s.sh; . s.sh; eval \"doc-$Y\"",
    ),
    ReachableWitness(
        "taint.top-level-positional.dynamic-mutation",
        'set -- $X; eval "$1"',
    ),
    ReachableWitness(
        "taint.values.alternative-limit",
        "bash",
        limits=ScanLimits(taint=TaintLimits(max_alternatives=0)),
    ),
)

INVARIANT_WITNESSES: tuple[InvariantWitness, ...] = (
    InvariantWitness(
        "taint.evidence.stream-scope-kind",
        f"{_EVIDENCE_SELF_CHECK} Every scope the builder allocates carries one of the declared "
        "stream-scope kinds.",
        "if true; then :; fi",
        # A root scope exists for every script including the empty one, so the boundary has to
        # build a scope the builder allocated for authored structure. The kind is what this guard
        # decides on, so the predicate reads it rather than the collection holding it.
        boundary_evidence=lambda evidence: len({scope.kind for scope in evidence.scopes}) > 1,
    ),
    InvariantWitness(
        "taint.evidence.pipe-consumer-arity",
        f"{_EVIDENCE_SELF_CHECK} The builder records a pipe with exactly one of a consuming "
        "command and a consuming scope, never both and never neither.",
        "echo a | cat",
        boundary_evidence=lambda evidence: any(
            pipe.consumer_command_id is not None or pipe.consumer_scope_id is not None
            for pipe in evidence.pipes
        ),
    ),
    InvariantWitness(
        "taint.evidence.process-resource-direction",
        f"{_EVIDENCE_SELF_CHECK} Process resources are recorded with a declared direction.",
        "cat <(echo a)",
        boundary_evidence=lambda evidence: bool(
            {resource.direction for resource in evidence.process_resources}
        ),
    ),
    InvariantWitness(
        "taint.evidence.duplicate-identifier",
        f"{_EVIDENCE_SELF_CHECK} Command, scope, resource and stream identifiers come from "
        "monotonic allocators, so two records cannot share one identifier.",
        "echo a; echo b",
        boundary_evidence=lambda evidence: (
            len({command.command_id for command in evidence.commands}) > 1
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-parent-scope",
        f"{_EVIDENCE_SELF_CHECK} A scope's parent scope is an identifier the builder allocated "
        "before the child scope existed.",
        "if true; then (:); fi",
        boundary_evidence=lambda evidence: any(
            s.parent_scope_id is not None for s in evidence.scopes
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-parent-command",
        f"{_EVIDENCE_SELF_CHECK} A scope's parent command is an identifier the builder allocated "
        "for a command it already recorded.",
        "echo $(echo a)",
        boundary_evidence=lambda evidence: any(
            s.parent_command_id is not None for s in evidence.scopes
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-pipe-producer",
        f"{_EVIDENCE_SELF_CHECK} A pipe's producing stream is a stream the builder allocated.",
        "echo a | cat",
        boundary_evidence=lambda evidence: bool(
            {pipe.producer_scope_id for pipe in evidence.pipes}
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-pipe-consumer-command",
        f"{_EVIDENCE_SELF_CHECK} A pipe's consuming command is a command the builder recorded.",
        "echo a | cat",
        boundary_evidence=lambda evidence: any(
            p.consumer_command_id is not None for p in evidence.pipes
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-pipe-consumer-scope",
        f"{_EVIDENCE_SELF_CHECK} A pipe's consuming scope is a scope the builder allocated.",
        "echo a | { :; }",
        boundary_evidence=lambda evidence: any(
            p.consumer_scope_id is not None for p in evidence.pipes
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-resource-scope",
        f"{_EVIDENCE_SELF_CHECK} A process resource names the scope it was allocated in.",
        "cat <(echo a)",
        boundary_evidence=lambda evidence: bool(
            {resource.scope_id for resource in evidence.process_resources}
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-redirection-resource",
        f"{_EVIDENCE_SELF_CHECK} A redirection to a process-resource target names a resource the "
        "builder allocated for the same body.",
        "cat < <(echo a)",
        # The guard resolves a redirection target's resource against the allocated ones, so the
        # boundary redirects from the process substitution rather than passing it as an argument:
        # `cat <(echo a)` allocates the resource but authors no redirection, and a universal claim
        # over the redirections would then hold vacuously over nothing.
        boundary_evidence=lambda evidence: any(
            isinstance(event.target, ProcessResourceTarget)
            and event.target.resource_id
            in {resource.resource_id for resource in evidence.process_resources}
            for events in (
                *(command.redirections for command in evidence.commands),
                *(scope.redirections for scope in evidence.scopes),
            )
            for event in events
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-argv-resource",
        f"{_EVIDENCE_SELF_CHECK} An argv port's process-resource reference names a resource the "
        "builder allocated for the same body.",
        "cat <(echo a)",
        boundary_evidence=lambda evidence: any(
            port.process_resource_id is not None for c in evidence.commands for port in c.argv
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-output-node",
        f"{_EVIDENCE_SELF_CHECK} Output expressions are a closed union the builder constructs, so "
        "the exhaustive walk has no unhandled member to reach.",
        "if true; then echo a; fi",
        # The walk reaches this guard only by dispatching on a node it does not recognize, so the
        # boundary must give it a tree it descends rather than the childless root an empty script
        # produces. Both recursing and leaf members have to be present for that dispatch to be
        # exercised over real evidence.
        boundary_evidence=lambda evidence: (
            len(_scope_output_nodes(evidence)) > 1
            and any(isinstance(node, CommandOutput) for node in _scope_output_nodes(evidence))
            and any(isinstance(node, ScopeOutput) for node in _scope_output_nodes(evidence))
        ),
    ),
    InvariantWitness(
        "taint.evidence.unknown-output-input-node",
        f"{_EVIDENCE_SELF_CHECK} The same closed output union bounds the input walk.",
        "while true; do echo a; done",
        # `RepeatOutput` is the arm this walk owns beyond the shared ones, so the loop boundary
        # is held to actually producing it.
        boundary_evidence=lambda evidence: any(
            isinstance(node, RepeatOutput) for node in _scope_output_nodes(evidence)
        ),
    ),
)


REACHABLE_IDS = frozenset(witness.origin_id for witness in REACHABLE_WITNESSES)
INVARIANT_IDS = frozenset(witness.origin_id for witness in INVARIANT_WITNESSES)
CLASSIFIED_IDS = REACHABLE_IDS | INVARIANT_IDS
