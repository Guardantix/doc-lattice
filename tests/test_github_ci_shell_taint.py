"""Tests for pure authored-marker shell taint analysis."""

import time
from dataclasses import replace

import pytest

from doc_lattice.error_types import ProjectError
from doc_lattice.github_ci import shell_taint
from doc_lattice.github_ci.shell_guards import GuardRefusal, MarkerDetected
from doc_lattice.github_ci.shell_scanner import (
    _effective_executable_evidence,
    _ScanBudget,
    _ShellWord,
    scan_doc_lattice_invocations,
)
from doc_lattice.github_ci.shell_taint import (
    TAINT_REFUSAL_REASON,
    Choice,
    ChoiceOutput,
    CommandOutput,
    Concat,
    ContentBuilder,
    ContentExpr,
    ContentTarget,
    DescriptorTarget,
    DynamicResourceTarget,
    LiteralTransfer,
    NullTarget,
    OutputExpr,
    OutsideGap,
    ProcessResourceTarget,
    RepeatOutput,
    ResourceRef,
    ScopeOutput,
    SequenceOutput,
    StaticResourceTarget,
    StreamRef,
    TaintLimits,
    VariableRef,
    _ArgPort,
    _AssignmentEvidence,
    _build_flow_definitions,
    _CommandEvidence,
    _ContentToken,
    _ContentValue,
    _contextualize_evidence,
    _eval_assignment_transfers,
    _eval_command_substitution_closing,
    _eval_reparse_content,
    _eval_reparse_literal,
    _eval_syntax_expression,
    _EvalSyntaxContext,
    _EvalSyntaxState,
    _EvalSyntaxTransition,
    _evaluate_closed,
    _evaluate_with_tables,
    _EvidenceBuilder,
    _ExecutableEvidence,
    _expression_variable_names,
    _FlowDefinitions,
    _FlowWrite,
    _is_function_positional_parameter,
    _marker_capable,
    _ordered_eval_syntax_variables,
    _OutputLowering,
    _PipeEvidence,
    _PositionalBinding,
    _ProcessResourceEvidence,
    _RedirectionEvent,
    _scoped_variable_name,
    _ShellTaintEvidence,
    _solve_eval_syntax_variables,
    _solve_flow_definitions,
    _static_eval_command_names,
    _static_eval_command_stdout,
    _static_eval_commands,
    _static_eval_mutations,
    _static_eval_program_commands,
    _static_eval_programs,
    _StreamScopeEvidence,
    _strip_trailing_newlines,
    _substitute_local_contents,
    _substituted_text_composes_marker,
    _TaintLimitExceeded,
    analyze_marker_taint,
    choice,
    concat,
    normalize_static_resource,
    stream_ref_ids,
)


def marker_taint_tuple(
    evidence: _ShellTaintEvidence,
    **kwargs: object,
) -> tuple[bool, str | None]:
    """Project a taint verdict onto this suite's historical (refused, reason) pair.

    These cases pin refusal reasons rather than guard-origin identity; guard identity is pinned
    by the witness registry in `tests/test_github_ci_shell_guards.py`.
    """
    verdict = analyze_marker_taint(evidence, **kwargs)  # ty: ignore[invalid-argument-type]
    if isinstance(verdict, GuardRefusal):
        return True, verdict.reason
    if isinstance(verdict, MarkerDetected):
        return True, TAINT_REFUSAL_REASON
    return False, None


def _can_mark(expression: ContentExpr, *, strip: bool = False) -> bool:
    value = _evaluate_closed(expression)
    if strip:
        value = _strip_trailing_newlines(value)
    return _marker_capable(value)


def _can_mark_with_tables(expression: ContentExpr) -> bool:
    """Resolve against empty tables, for an expression carrying resource or stream references."""
    return _marker_capable(_evaluate_with_tables(expression, {}, {}, {}, TaintLimits()))


def _deep_concat(depth: int) -> ContentExpr:
    expression: ContentExpr = LiteralTransfer("x")
    for _ in range(depth):
        expression = Concat((expression, LiteralTransfer("")))
    return expression


def _arg(literal: str, expression: ContentExpr | None = None, *, dynamic: bool = False) -> _ArgPort:
    return _ArgPort(
        literal=literal,
        content=expression or LiteralTransfer(literal),
        dynamic=dynamic,
    )


def _command(  # noqa: PLR0913
    command_id: int,
    *argv: _ArgPort,
    name: str,
    head_index: int = 0,
    external_lookup: bool = False,
    assignments: tuple[_AssignmentEvidence, ...] = (),
    redirections: tuple[_RedirectionEvent, ...] = (),
    container_scope_id: int = 100,
) -> _CommandEvidence:
    return _CommandEvidence(
        command_id=command_id,
        output_scope_id=command_id,
        container_scope_id=container_scope_id,
        argv=argv,
        assignments=assignments,
        redirections=redirections,
        executable=_ExecutableEvidence(
            argv_index=head_index,
            name=name,
            literal=argv[head_index].literal,
            external_lookup=external_lookup,
        ),
    )


def test_scope_output_lowers_to_its_stream_reference() -> None:
    lowered = _OutputLowering({1: 10}).lower(ScopeOutput(7), [])

    assert lowered == StreamRef(7)


def test_content_builder_expands_active_braces_into_ordered_argv_ports() -> None:
    builder = ContentBuilder.empty()
    for character in "doc-{lattice,noop}":
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.argv_ports is not None
    assert [(port.literal, port.content) for port in built.argv_ports] == [
        ("doc-lattice", LiteralTransfer("doc-lattice")),
        ("doc-noop", LiteralTransfer("doc-noop")),
    ]


def test_content_builder_elides_wholly_empty_unquoted_brace_alternatives() -> None:
    builder = ContentBuilder.empty()
    for character in "{,}":
        builder.append_literal(character, brace_active=True)

    assert builder.build(limits=TaintLimits()).argv_ports == ()


def test_content_builder_keeps_quoted_and_escaped_braces_literal() -> None:
    builder = ContentBuilder.empty()
    builder.append_literal("{doc-,lattice}")

    built = builder.build(limits=TaintLimits())

    assert built.argv_ports is not None
    assert [(port.literal, port.content) for port in built.argv_ports] == [
        ("{doc-,lattice}", LiteralTransfer("{doc-,lattice}")),
    ]


def test_content_builder_expands_bounded_ranges_without_turning_word_content_into_choice() -> None:
    builder = ContentBuilder.empty()
    for character in "doc-{1..2}-lattice":
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.expression == LiteralTransfer("doc-{1..2}-lattice")
    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == [
        "doc-1-lattice",
        "doc-2-lattice",
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{-2..0}", ["-2", "-1", "0"]),
        ("{+1..+3}", ["1", "2", "3"]),
        ("{c..a..-1}", ["c", "b", "a"]),
    ],
    ids=("signed-numeric", "positive-numeric", "letter"),
)
def test_content_builder_preserves_signed_numeric_and_letter_ranges(
    source: str, expected: list[str]
) -> None:
    builder = ContentBuilder.empty()
    for character in source:
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{01..03}", ["01", "02", "03"]),
        ("{1..03}", ["01", "02", "03"]),
        ("{03..1}", ["03", "02", "01"]),
        ("{-03..-01}", ["-03", "-02", "-01"]),
        ("{-3..-01}", ["-03", "-02", "-01"]),
        ("{01..-01}", ["001", "000", "-01"]),
    ],
    ids=(
        "padded",
        "mixed-endpoint",
        "descending",
        "negative",
        "mixed-negative-endpoint",
        "cross-zero",
    ),
)
def test_content_builder_preserves_bash_numeric_range_padding(
    source: str,
    expected: list[str],
) -> None:
    builder = ContentBuilder.empty()
    for character in source:
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == expected
    assert [port.content for port in built.argv_ports] == [
        LiteralTransfer(value) for value in expected
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{1..3..0}", ["1", "2", "3"]),
        ("{1..3..-1}", ["1", "2", "3"]),
        ("{3..1..1}", ["3", "2", "1"]),
        ("{a..c..0}", ["a", "b", "c"]),
        ("{a..c..-1}", ["a", "b", "c"]),
        ("{c..a..1}", ["c", "b", "a"]),
    ],
    ids=(
        "numeric-zero",
        "numeric-opposite",
        "numeric-descending-opposite",
        "letter-zero",
        "letter-opposite",
        "letter-descending-opposite",
    ),
)
def test_content_builder_normalizes_range_step_like_bash(source: str, expected: list[str]) -> None:
    builder = ContentBuilder.empty()
    for character in source:
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == expected


@pytest.mark.parametrize("source", ["{1..2..invalid}", "{1..²}"])
def test_content_builder_leaves_malformed_brace_ranges_literal(source: str) -> None:
    builder = ContentBuilder.empty()
    for character in source:
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == [source]


def test_content_builder_expands_dynamic_recognized_brace_operand() -> None:
    builder = ContentBuilder.empty()
    for character in "{doc-,":
        builder.append_literal(character, brace_active=True)
    builder.append_expression(VariableRef("X"))
    for character in "}lattice":
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == ["doc-lattice", "lattice"]
    assert built.argv_ports[1].content == concat(VariableRef("X"), LiteralTransfer("lattice"))


def test_content_builder_assignment_rhs_preserves_original_unexpanded_tokens() -> None:
    builder = ContentBuilder.empty()
    for character in "X=":
        builder.append_literal(character, brace_active=True)
    builder.mark_assignment("X", append=False)
    for character in "{doc-,lattice}":
        builder.append_literal(character, brace_active=True)

    built = builder.build(limits=TaintLimits())

    assert built.assignment_content == LiteralTransfer("{doc-,lattice}")
    assert built.argv_ports is not None
    assert [(port.literal, port.content) for port in built.argv_ports] == [
        ("X=doc-", LiteralTransfer("X=doc-")),
        ("X=lattice", LiteralTransfer("X=lattice")),
    ]
    assert built.brace_expansion_error is None


def test_content_builder_assignment_rhs_retains_deferred_brace_expansion_error() -> None:
    builder = ContentBuilder.empty()
    for character in "X=":
        builder.append_literal(character, brace_active=True)
    builder.mark_assignment("X", append=False)
    for character in "{1..5000}":
        builder.append_literal(character, brace_active=True)

    built = builder.build(defer_brace_errors=True, limits=TaintLimits())

    assert built.assignment_content == LiteralTransfer("{1..5000}")
    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == ["X={1..5000}"]
    assert built.brace_expansion_error == GuardRefusal(
        "taint.brace.numeric-sequence-limit",
        "shell taint brace expansion limit exceeded",
    )


@pytest.mark.parametrize(
    ("source", "limits", "reason"),
    [
        (
            "{1..3}",
            TaintLimits(max_brace_expansions=2),
            "shell taint brace expansion limit exceeded",
        ),
        (
            "{a,{b,c}}",
            TaintLimits(max_brace_depth=1),
            "shell taint brace expansion depth limit exceeded",
        ),
    ],
    ids=("range-cap", "nested-depth"),
)
def test_content_builder_brace_bounds_are_deterministic(
    source: str, limits: TaintLimits, reason: str
) -> None:
    builder = ContentBuilder.empty()
    for character in source:
        builder.append_literal(character, brace_active=True)

    with pytest.raises(_TaintLimitExceeded, match=reason):
        builder.build(limits)


def test_stream_ref_ids_descends_only_through_choice_and_concat() -> None:
    expression = Concat((StreamRef(1), Choice((StreamRef(2), VariableRef("X")))))

    assert stream_ref_ids(expression) == (1, 2)


def test_output_process_substitution_binds_writer_scope_to_consumer_stdin() -> None:
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, ProcessResourceTarget(1)),),
    )
    consumer = _command(2, _arg("bash"), name="bash")
    evidence = _ShellTaintEvidence(
        commands=(writer, consumer),
        scopes=(
            _StreamScopeEvidence(
                20,
                "process_substitution",
                None,
                None,
                CommandOutput(2),
            ),
        ),
        process_resources=(_ProcessResourceEvidence(1, 20, "output"),),
    )

    definitions, inputs = _build_flow_definitions(evidence, limits=TaintLimits())

    assert inputs[2] == StreamRef(writer.output_scope_id)
    assert (
        _marker_capable(
            _solve_flow_definitions(definitions, limits=TaintLimits()).evaluate(StreamRef(2))
        )
        is True
    )


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (TaintLimits(max_edges=3), "shell taint edge limit exceeded"),
        (TaintLimits(max_table_entries=3), "shell taint table entry limit exceeded"),
    ],
    ids=("edges", "table-entries"),
)
def test_assignment_environment_materialization_checks_limits_incrementally(
    limits: TaintLimits,
    reason: str,
) -> None:
    assignment = _command(
        1,
        _arg("true"),
        name="true",
        assignments=(_AssignmentEvidence("X", LiteralTransfer("value")),),
    )
    isolated = tuple(
        replace(
            _command(command_id, _arg("true"), name="true"),
            isolated_execution=True,
            isolated_context_id=200 + command_id,
        )
        for command_id in range(2, 5)
    )
    commands = (assignment, *isolated)
    evidence = _contextualize_evidence(
        _ShellTaintEvidence(
            commands=commands,
            scopes=(
                _StreamScopeEvidence(
                    100,
                    "command",
                    None,
                    None,
                    SequenceOutput(
                        tuple(CommandOutput(command.command_id) for command in commands)
                    ),
                ),
            ),
        ),
        limits=TaintLimits(),
    )

    with pytest.raises(_TaintLimitExceeded, match=reason):
        _build_flow_definitions(evidence, limits=limits)


def test_function_call_contextualization_checks_limits_incrementally() -> None:
    definitions = tuple(
        replace(
            _command(command_id, _arg(f"f{command_id}"), name=f"f{command_id}"),
            defines_function_context_id=100 + command_id,
            defines_function_name=f"f{command_id}",
        )
        for command_id in range(1, 4)
    )
    bodies = tuple(
        replace(
            _command(
                10 + command_id,
                _arg("true"),
                name="true",
                assignments=(_AssignmentEvidence("X", LiteralTransfer("doc-")),),
            ),
            argv=(),
            executable=_ExecutableEvidence(None, None, None),
            function_context_id=100 + command_id,
            function_name=f"f{command_id}",
        )
        for command_id in range(1, 4)
    )
    calls = tuple(
        _command(20 + command_id, _arg(f"f{command_id}"), name=f"f{command_id}")
        for command_id in range(1, 4)
    )
    commands = (*definitions, *bodies, *calls)
    evidence = _ShellTaintEvidence(
        commands=commands,
        scopes=(
            _StreamScopeEvidence(
                100,
                "command",
                None,
                None,
                SequenceOutput(tuple(CommandOutput(command.command_id) for command in commands)),
            ),
        ),
    )

    with pytest.raises(_TaintLimitExceeded, match="shell taint edge limit exceeded"):
        _contextualize_evidence(evidence, limits=TaintLimits(max_edges=4))


def test_eval_joins_dynamic_variable_assignment_and_append() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("doc-")),
            _AssignmentEvidence("X", LiteralTransfer("lattice"), append=True),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_inserts_literal_spaces_between_argument_ports() -> None:
    command = _command(1, _arg("eval"), _arg("doc-"), _arg("lattice"), name="eval")

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


@pytest.mark.parametrize(
    "expression",
    [
        LiteralTransfer("${X}lattice reconcile"),
        LiteralTransfer('"${X}lattice reconcile"'),
        Concat((LiteralTransfer("${X}"), LiteralTransfer("lattice reconcile"))),
        Choice((LiteralTransfer("safe"), LiteralTransfer("${X}lattice reconcile"))),
    ],
    ids=("unquoted", "double-quoted", "concat", "choice"),
)
def test_eval_reparses_literal_variable_reference_on_second_pass(expression: ContentExpr) -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("${X}lattice reconcile", expression),
        name="eval",
        assignments=(_AssignmentEvidence("X", LiteralTransfer("doc-")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_reparse_keeps_second_pass_single_quoted_variable_reference_literal() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("'${X}lattice'", LiteralTransfer("'${X}lattice'")),
        name="eval",
        assignments=(_AssignmentEvidence("X", LiteralTransfer("doc-")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_eval_reparse_interprets_quotes_contributed_by_variable_value() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg(
            "$Alattice' --help",
            Concat((VariableRef("A"), LiteralTransfer("lattice' --help"))),
            dynamic=True,
        ),
        name="eval",
        assignments=(_AssignmentEvidence("A", LiteralTransfer("doc-'")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_reparse_decodes_ansi_c_literal_escapes() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$'doc-\\x6cattice' --help", LiteralTransfer("$'doc-\\x6cattice' --help")),
        name="eval",
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_reparse_keeps_external_only_value_non_evidentiary() -> None:
    command = _command(1, _arg("eval"), _arg("$EXTERNAL", VariableRef("EXTERNAL")), name="eval")

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "doc-{lattice,noop}",
            Choice((LiteralTransfer("doc-lattice"), LiteralTransfer("doc-noop"))),
        ),
        (
            "'doc-{lattice,noop}'",
            LiteralTransfer("doc-{lattice,noop}"),
        ),
        (
            r"doc-\{lattice,noop\}",
            LiteralTransfer("doc-{lattice,noop}"),
        ),
        (
            "{doc-,lattice}",
            Choice((LiteralTransfer("doc-"), LiteralTransfer("lattice"))),
        ),
    ],
    ids=("active", "single-quoted", "escaped", "separate-argv"),
)
def test_eval_literal_reparse_tracks_active_brace_syntax(text: str, expected: ContentExpr) -> None:
    expression, quote = _eval_reparse_literal(text, None, limits=TaintLimits())

    assert expression == expected
    assert quote is None


def test_eval_variable_syntax_expands_braces_after_assignment_flow() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(_AssignmentEvidence("X", LiteralTransfer("doc-{lattice,noop}")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_variable_syntax_preserves_braces_across_append_writes() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("doc-{")),
            _AssignmentEvidence("X", LiteralTransfer("lattice,noop}"), append=True),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


@pytest.mark.parametrize(
    "assignments",
    [
        (_AssignmentEvidence("X", LiteralTransfer("{doc-,x}lattice")),),
        (
            _AssignmentEvidence("X", LiteralTransfer("{doc-,x}")),
            _AssignmentEvidence("X", LiteralTransfer("lattice"), append=True),
        ),
        (_AssignmentEvidence("X", LiteralTransfer("{doc-,x}{lattice,y}")),),
    ],
    ids=("same-write-suffix", "append-suffix", "cartesian-groups"),
)
def test_eval_variable_syntax_distributes_suffixes_across_brace_words(
    assignments: tuple[_AssignmentEvidence, ...],
) -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=assignments,
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_variable_syntax_keeps_cross_write_brace_words_separate() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("{doc-,")),
            _AssignmentEvidence("X", LiteralTransfer("lattice}"), append=True),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_eval_variable_syntax_cartesian_words_obey_alternative_cap() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("{d,do,doc}")),
            _AssignmentEvidence("X", LiteralTransfer("{-,-l,-la}"), append=True),
        ),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(command,)),
        limits=TaintLimits(max_alternatives=3),
    ) == (True, "shell taint eval syntax alternative limit exceeded")


def test_eval_variable_syntax_mutual_cycle_obeys_fixed_point_cap() -> None:
    writes = (
        _FlowWrite("X", LiteralTransfer("{")),
        _FlowWrite("X", VariableRef("Y")),
        _FlowWrite("Y", VariableRef("X")),
    )

    with pytest.raises(
        _TaintLimitExceeded,
        match="shell taint eval syntax fixed-point update limit exceeded",
    ):
        _solve_eval_syntax_variables(
            writes,
            {},
            TaintLimits(max_fixed_point_updates=1),
        )


# The three wall-clock assertions below separate "returns promptly" from the seconds-to-minutes
# blowups of issues #140 and #149. They are not a constant-factor budget, so the bound is set for
# the slowest runner rather than the fastest: the 3.13 CI job runs about twice as slowly as the
# 3.14 one and roughly nine times as slowly as a development machine, which took the 60-write
# accumulation from 0.3s locally to 2.6s there and failed a 2.0s bound three commits running. The
# regressions these guard were 20s and 21s, and #149's token-step test is the machine-independent
# guard against its quadratic invalidation returning, so headroom here costs nothing.
_EVAL_SYNTAX_WALL_CLOCK_BOUND_SECONDS = 8.0


def test_eval_syntax_self_referential_append_certifies_promptly() -> None:
    """Reproduce #140: a marker-free self-referential append feeding an eval sink.

    ``_EvalSyntaxValue`` had no analogue of ``_merge_content_summaries``, so the join that
    accumulates alternatives for one eval-syntax variable was a bare set union over exact
    ``literal_texts``. AD-18 promises marker-free dynamic execution keeps certifying, and it
    must do so quickly rather than limping to that verdict.
    """
    script = 'ARGS="$ARGS --quiet"; eval "mytool $ARGS"'

    start = time.monotonic()
    result = scan_doc_lattice_invocations(script)
    elapsed = time.monotonic() - start

    assert result.incomplete_reason is None
    assert elapsed < _EVAL_SYNTAX_WALL_CLOCK_BOUND_SECONDS, (
        f"eval syntax fixed point took {elapsed:.2f}s, expected under "
        f"{_EVAL_SYNTAX_WALL_CLOCK_BOUND_SECONDS}s"
    )


def test_eval_syntax_seeded_self_referential_append_certifies_promptly() -> None:
    """A seeded self-referential reassignment must collapse, not diverge.

    A prior independent write gives ``ARGS`` a real starting alternative, so the fixed point
    in ``_solve_eval_syntax_variables`` genuinely grows the tracked value across passes instead
    of bottoming out immediately: each outer pass re-derives ``ARGS``'s self-referential write
    against the wider value the previous pass installed, so the join walked one more exact
    literal alternative (``--verbose --quiet``, then that plus one more composition, and so on)
    every pass instead of collapsing equal DFA behavior. Before the fix this took roughly 20s
    and then refused on the alternative cap; nothing here ever composes the doc-lattice marker.
    """
    script = 'ARGS="--verbose"\nARGS="$ARGS --quiet"\neval "mytool $ARGS"'

    start = time.monotonic()
    result = scan_doc_lattice_invocations(script)
    elapsed = time.monotonic() - start

    assert result.incomplete_reason is None
    assert elapsed < _EVAL_SYNTAX_WALL_CLOCK_BOUND_SECONDS, (
        f"eval syntax fixed point took {elapsed:.2f}s, expected under "
        f"{_EVAL_SYNTAX_WALL_CLOCK_BOUND_SECONDS}s"
    )


def test_eval_syntax_marker_bearing_self_referential_append_still_refuses() -> None:
    """Collapsing surplus alternatives must widen to top, never drop the marker flow.

    Neither authored assignment word contains the full ``doc-lattice`` marker on its own, so
    this exercises the eval-syntax taint solver rather than the earlier syntactic
    marker-bearing-word check. The two self-referential appends compose the marker only through
    content flow, and collapsing past the alternative cap must widen to
    ``projection_incomplete`` (the top of the projection lattice) rather than silently dropping
    the composing alternative, so this must still refuse.
    """
    script = 'ARGS="${ARGS}doc-"\nARGS="${ARGS}lattice"\neval "$ARGS"'

    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason == TAINT_REFUSAL_REASON


def _eval_syntax_state(literal: str = "x") -> _EvalSyntaxState:
    """Return one real eval-syntax parse state for transition-table bookkeeping tests."""
    context = _EvalSyntaxContext({}, {}, TaintLimits(), {})
    return next(iter(_eval_syntax_expression(LiteralTransfer(literal), None, context)))


def test_eval_syntax_invalidation_keeps_a_transition_that_observed_no_slot() -> None:
    """A transition computed without reading the solved table cannot be stale (issue #149).

    ``context.variables`` is read at exactly one site: the cycle-detected ``VariableRef`` branch
    of ``_eval_syntax_append``. Every other input to a replayed transition is immutable for the
    life of the pass, so an entry that never reached that site is independent of later writes and
    must survive them.
    """
    context = _EvalSyntaxContext({}, {}, TaintLimits(), {})
    key = ("A", _eval_syntax_state())
    context.begin_observations()
    context.transitions[key] = _EvalSyntaxTransition(frozenset(), context.end_observations())

    context.invalidate_transitions(("A", None))

    assert key in context.transitions


def test_eval_syntax_invalidation_drops_only_transitions_observing_the_written_slot() -> None:
    """Invalidation is scoped to the rewritten ``(name, quote)`` slot, not the whole table."""
    context = _EvalSyntaxContext({}, {}, TaintLimits(), {})
    observing = ("A", _eval_syntax_state("observing"))
    unrelated = ("B", _eval_syntax_state("unrelated"))
    for key, slot in ((observing, ("A", None)), (unrelated, ("B", '"'))):
        context.begin_observations()
        context.observe_slot(slot)
        context.transitions[key] = _EvalSyntaxTransition(frozenset(), context.end_observations())

    context.invalidate_transitions(("A", None))

    assert observing not in context.transitions
    assert unrelated in context.transitions


def test_eval_syntax_nested_observations_propagate_to_the_enclosing_transition() -> None:
    """An enclosing transition inherits every slot its nested computations observed.

    Without this an outer entry would be retained on the strength of its own direct reads while
    depending on an inner value that the same write invalidated, which is the one way this cache
    turns unsound rather than merely slow.
    """
    context = _EvalSyntaxContext({}, {}, TaintLimits(), {})

    context.begin_observations()
    context.begin_observations()
    context.observe_slot(("inner", None))
    inner = context.end_observations()
    outer = context.end_observations()

    assert inner == frozenset({("inner", None)})
    assert outer == inner


def test_eval_syntax_cache_hit_propagates_the_cached_transitions_observed_slots() -> None:
    """Reusing a memoized transition carries its recorded dependencies to the caller."""
    context = _EvalSyntaxContext({}, {}, TaintLimits(), {})

    context.begin_observations()
    context.inherit_observations(frozenset({("cached", "'")}))
    observed = context.end_observations()

    assert observed == frozenset({("cached", "'")})


_ACCUMULATING_WRITES = (
    _FlowWrite("ARGS", LiteralTransfer("--a0")),
    _FlowWrite("ARGS", concat(VariableRef("ARGS"), LiteralTransfer(" --a1"))),
)


def test_ordered_eval_syntax_pass_never_invalidates_its_memoized_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordered pass reads a fixed fallback table, so no write can stale a transition.

    Its own result accumulates in a separate dictionary. Sharing one dictionary for both meant
    every write changed the table a cycle-detected read falls back on, which invalidated the memo
    table once per write and made the pass quadratic (issue #149).
    """
    invalidated: list[tuple[str | int, str | None]] = []
    original = _EvalSyntaxContext.invalidate_transitions

    def record(self: _EvalSyntaxContext, slot: tuple[str | int, str | None]) -> None:
        invalidated.append(slot)
        original(self, slot)

    solved = _solve_eval_syntax_variables(_ACCUMULATING_WRITES, {}, TaintLimits())
    monkeypatch.setattr(_EvalSyntaxContext, "invalidate_transitions", record)
    _ordered_eval_syntax_variables(_ACCUMULATING_WRITES, {}, TaintLimits(), solved)

    assert invalidated == []


def test_ordered_eval_syntax_pass_leaves_the_solved_fallback_table_untouched() -> None:
    """Ordered results must not leak back into the table the fallback reads."""
    solved = _solve_eval_syntax_variables(_ACCUMULATING_WRITES, {}, TaintLimits())
    before = dict(solved)

    ordered = _ordered_eval_syntax_variables(_ACCUMULATING_WRITES, {}, TaintLimits(), solved)

    assert solved == before
    assert ordered is not solved


def _eval_syntax_token_calls(monkeypatch: pytest.MonkeyPatch, script: str) -> int:
    """Count second-pass token steps one scan spends, as an execution-independent work meter."""
    calls = 0
    original = shell_taint._eval_syntax_token

    def counted(
        state: _EvalSyntaxState,
        token: _ContentToken,
        context: _EvalSyntaxContext,
    ) -> frozenset[_EvalSyntaxState]:
        nonlocal calls
        calls += 1
        return original(state, token, context)

    monkeypatch.setattr(shell_taint, "_eval_syntax_token", counted)
    assert scan_doc_lattice_invocations(script).incomplete_reason is None
    return calls


def _self_referential_accumulation(writes: int) -> str:
    """Build the ``ARGS="$ARGS --flag"`` accumulation idiom feeding one eval sink."""
    lines = ['ARGS="--a0"']
    lines.extend(f'ARGS="$ARGS --a{index}"' for index in range(1, writes))
    lines.append('eval "mytool $ARGS"')
    return "\n".join(lines)


def test_eval_syntax_accumulation_work_grows_linearly_in_write_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin #149: clearing the whole transition table per write made the pass quadratic.

    ``_ordered_eval_syntax_variables`` walks writes times three quote states and each replayed
    transition re-derives a variable's whole write program, so discarding every memoized
    transition after every write made second-pass token work grow with the square of the write
    count: 15,172 then 59,332 then 234,052 token steps at 20, 40, and 80 writes, or 3.9x per
    doubling. Scoping invalidation to the rewritten slot takes that to 1.9x per doubling. The
    bound is deliberately loose because it separates quadratic from linear, not one constant
    factor from another, and counting token steps keeps it independent of machine speed.
    """
    smaller = _eval_syntax_token_calls(monkeypatch, _self_referential_accumulation(20))
    larger = _eval_syntax_token_calls(monkeypatch, _self_referential_accumulation(40))

    assert larger < smaller * 2.5, (
        f"second-pass token work grew {larger / smaller:.1f}x when the write count doubled, "
        "which is the quadratic transition-cache invalidation of issue #149"
    )


def test_eval_syntax_long_accumulation_still_certifies() -> None:
    """Sixty marker-free accumulating writes keep certifying, and promptly."""
    start = time.monotonic()
    result = scan_doc_lattice_invocations(_self_referential_accumulation(60))
    elapsed = time.monotonic() - start

    assert result.incomplete_reason is None
    assert elapsed < _EVAL_SYNTAX_WALL_CLOCK_BOUND_SECONDS, (
        f"eval syntax second pass took {elapsed:.2f}s, expected under "
        f"{_EVAL_SYNTAX_WALL_CLOCK_BOUND_SECONDS}s"
    )


def test_eval_syntax_long_accumulation_carrying_the_marker_still_refuses() -> None:
    """Retaining memoized transitions must not lose marker flow through the same idiom."""
    script = _self_referential_accumulation(60).replace(
        'ARGS="$ARGS --a30"',
        'ARGS="$ARGS doc-lattice"',
    )

    assert scan_doc_lattice_invocations(script).incomplete_reason is not None


def test_eval_conditional_assignment_obeys_augmented_edge_cap() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("${X:=doc-}", LiteralTransfer("${X:=doc-}")),
        name="eval",
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(command,)),
        limits=TaintLimits(max_edges=1),
    ) == (True, "shell taint edge limit exceeded")


def test_eval_base_definition_cap_precedes_side_effect_discovery() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$(printf doc-)", LiteralTransfer("$(printf doc-)")),
        name="eval",
        assignments=(_AssignmentEvidence("X", _deep_concat(2)),),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(command,)),
        limits=TaintLimits(max_expression_nodes=2),
    ) == (True, "shell taint expression node limit exceeded")


def test_eval_side_effect_discovery_shares_expression_work_cap() -> None:
    commands = tuple(
        _command(
            command_id,
            _arg("eval"),
            _arg("${X:=doc-}", LiteralTransfer("${X:=doc-}")),
            name="eval",
        )
        for command_id in (1, 2)
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=commands),
        limits=TaintLimits(max_expression_nodes=3),
    ) == (True, "shell taint expression node limit exceeded")


def test_deep_eval_syntax_fails_with_stable_depth_reason() -> None:
    assignment = _AssignmentEvidence("X", _deep_concat(1000))
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(assignment,),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "shell taint eval reparse depth limit exceeded",
    )


def test_eval_syntax_reuses_scoped_overlay_for_long_token_stream() -> None:
    raw_variables: dict[str | int, _ContentValue] = {
        _scoped_variable_name(1, f"V{index}"): _evaluate_closed(LiteralTransfer(f"value-{index}"))
        for index in range(512)
    }
    context = _EvalSyntaxContext({}, raw_variables, TaintLimits(), {})

    states = _eval_syntax_expression(
        LiteralTransfer("x" * 4096),
        None,
        context,
        environment=1,
    )

    assert len(states) == 1
    assert len(context.variable_overlays) == 1


@pytest.mark.parametrize(
    ("assignments", "sink_names"),
    [
        (
            (
                _AssignmentEvidence("A", LiteralTransfer("doc-{")),
                _AssignmentEvidence("B", LiteralTransfer("lattice,noop}")),
            ),
            ("A", "B"),
        ),
        (
            (
                _AssignmentEvidence("A", LiteralTransfer("{doc-,x")),
                _AssignmentEvidence("B", LiteralTransfer("}{lattice,y}")),
            ),
            ("A", "B"),
        ),
        (
            (
                _AssignmentEvidence("A", LiteralTransfer("doc-{")),
                _AssignmentEvidence("B", VariableRef("A")),
                _AssignmentEvidence("C", VariableRef("B")),
                _AssignmentEvidence("D", LiteralTransfer("lattice,x}")),
            ),
            ("C", "D"),
        ),
    ],
    ids=("open-prefix", "cartesian-groups", "alias-chain"),
)
def test_eval_variable_syntax_composes_open_braces_across_variables(
    assignments: tuple[_AssignmentEvidence, ...],
    sink_names: tuple[str, ...],
) -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg(
            "$VARS",
            Concat(tuple(VariableRef(name) for name in sink_names)),
            dynamic=True,
        ),
        name="eval",
        assignments=assignments,
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_variable_syntax_aliases_preserve_append_provenance() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("{")),
            _AssignmentEvidence("X", LiteralTransfer("x"), append=True),
            _AssignmentEvidence("Y", VariableRef("X")),
            _AssignmentEvidence("X", VariableRef("Y")),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_eval_variable_syntax_distinct_appends_each_apply_once_through_aliases() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("{")),
            _AssignmentEvidence("X", LiteralTransfer("x"), append=True),
            _AssignmentEvidence("X", LiteralTransfer("y"), append=True),
            _AssignmentEvidence("Y", VariableRef("X")),
            _AssignmentEvidence("X", VariableRef("Y")),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


@pytest.mark.parametrize(
    "definitions",
    [
        ("lattice,x}", "safe,x}"),
        ("safe,x}", "lattice,x}"),
    ],
    ids=("marker-first", "marker-last"),
)
def test_eval_variable_syntax_joins_competing_definitions(
    definitions: tuple[str, str],
) -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg(
            "$AB",
            Concat((VariableRef("A"), VariableRef("B"))),
            dynamic=True,
        ),
        name="eval",
        assignments=(
            _AssignmentEvidence("A", LiteralTransfer("doc-{")),
            *(_AssignmentEvidence("B", LiteralTransfer(definition)) for definition in definitions),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_variable_syntax_applies_append_to_every_competing_definition() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg(
            "$AB",
            Concat((VariableRef("A"), VariableRef("B"))),
            dynamic=True,
        ),
        name="eval",
        assignments=(
            _AssignmentEvidence("A", LiteralTransfer("doc-{")),
            _AssignmentEvidence("B", LiteralTransfer("lat")),
            _AssignmentEvidence("B", LiteralTransfer("safe")),
            _AssignmentEvidence("B", LiteralTransfer("tice,x}"), append=True),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_variable_syntax_competing_definitions_never_concatenate() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg(
            "$AB",
            Concat((VariableRef("A"), VariableRef("B"))),
            dynamic=True,
        ),
        name="eval",
        assignments=(
            _AssignmentEvidence("A", LiteralTransfer("doc-{")),
            _AssignmentEvidence("B", LiteralTransfer("lattice,")),
            _AssignmentEvidence("B", LiteralTransfer("x}")),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_eval_brace_expansion_obeys_taint_cap() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("doc-{1..3}-lattice"),
        name="eval",
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(command,)),
        limits=TaintLimits(max_brace_expansions=2),
    ) == (True, "shell taint brace expansion limit exceeded")


def test_eval_brace_expansion_honors_custom_higher_cap() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("{1..257}"),
        name="eval",
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(command,)),
        limits=TaintLimits(max_alternatives=300, max_brace_expansions=257),
    ) == (False, None)


@pytest.mark.parametrize(
    ("words", "expected_index", "expected_name"),
    [
        (("exec", "command", "bash", "-c", "$X"), 1, "command"),
        (("exec", "builtin", "bash", "-c", "$X"), 1, "builtin"),
        (("exec", "exec", "bash", "-c", "$X"), 1, "exec"),
        (("command", "exec", "exec", "bash", "-c", "$X"), 2, "exec"),
    ],
    ids=("command", "builtin", "exec", "command-exec"),
)
def test_external_wrapper_evidence_does_not_activate_shell_sink(
    words: tuple[str, ...], expected_index: int, expected_name: str
) -> None:
    executable = _effective_executable_evidence(
        [_ShellWord(word) for word in words],
        _ScanBudget(),
    )

    assert executable is not None
    assert (executable.argv_index, executable.name, executable.external_lookup) == (
        expected_index,
        expected_name,
        True,
    )
    command = _CommandEvidence(
        command_id=1,
        output_scope_id=1,
        container_scope_id=100,
        argv=tuple(
            _arg(word, LiteralTransfer("doc-lattice"), dynamic=True) if word == "$X" else _arg(word)
            for word in words
        ),
        assignments=(),
        redirections=(),
        executable=executable,
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_non_sink_builtin_target_does_not_activate_shell_sink() -> None:
    executable = _effective_executable_evidence(
        [
            _ShellWord("builtin"),
            _ShellWord("bash"),
            _ShellWord("-c"),
            _ShellWord("$X"),
        ],
        _ScanBudget(),
    )

    assert executable is None
    command = _CommandEvidence(
        command_id=1,
        output_scope_id=1,
        container_scope_id=100,
        argv=(
            _arg("builtin"),
            _arg("bash"),
            _arg("-c"),
            _arg("$X", LiteralTransfer("doc-lattice"), dynamic=True),
        ),
        assignments=(),
        redirections=(),
        executable=_ExecutableEvidence(None, None, None),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_executable_alternates_count_against_table_limit() -> None:
    command = _CommandEvidence(
        command_id=1,
        output_scope_id=1,
        container_scope_id=100,
        argv=(_arg("true"),),
        assignments=(),
        redirections=(),
        executable=_ExecutableEvidence(
            0,
            "true",
            "true",
            alternates=(
                _ExecutableEvidence(
                    0,
                    "eval",
                    "eval",
                    alternates=(_ExecutableEvidence(0, "source", "source"),),
                ),
            ),
        ),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(command,)), limits=TaintLimits(max_table_entries=2)
    ) == (True, "shell taint table entry limit exceeded")


def test_external_lookup_eval_is_not_treated_as_eval_sink() -> None:
    command = _command(
        1,
        _arg("env"),
        _arg("eval"),
        _arg("$X", VariableRef("X"), dynamic=True),
        name="eval",
        head_index=1,
        external_lookup=True,
        assignments=(
            _AssignmentEvidence("X", LiteralTransfer("doc-")),
            _AssignmentEvidence("X", LiteralTransfer("lattice"), append=True),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_shell_command_payload_wins_over_heredoc_stdin() -> None:
    command = _command(
        1,
        _arg("bash"),
        _arg("-c"),
        _arg("echo ok"),
        name="bash",
        redirections=(
            _RedirectionEvent(
                0,
                "<<",
                0,
                ContentTarget(LiteralTransfer("doc-lattice reconcile\n")),
            ),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_dynamic_shell_selector_fails_closed_over_remaining_arguments() -> None:
    command = _command(
        1,
        _arg("bash"),
        _arg("$OPT", OutsideGap(), dynamic=True),
        _arg("$X", Concat((LiteralTransfer("doc-"), LiteralTransfer("lattice"))), dynamic=True),
        name="bash",
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_static_script_write_is_visible_to_reader_regardless_of_command_order() -> None:
    reader = _command(1, _arg("bash"), _arg("./task.sh"), name="bash")
    writer = _command(
        2,
        _arg("printf"),
        _arg("doc-"),
        _arg("lattice reconcile"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget("task.sh")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(reader, writer))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_later_descriptor_binding_overrides_earlier_static_stdout_target() -> None:
    reader = _command(1, _arg("bash"), _arg("./task.sh"), name="bash")
    writer = _command(
        2,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(
            _RedirectionEvent(0, ">", 1, StaticResourceTarget("task.sh")),
            _RedirectionEvent(1, ">", 1, NullTarget()),
        ),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(reader, writer))) == (False, None)


def test_explicit_stdin_redirection_overrides_pipe_input() -> None:
    producer = _command(1, _arg("printf"), _arg("doc-lattice"), name="printf")
    consumer = _command(
        2,
        _arg("bash"),
        name="bash",
        redirections=(_RedirectionEvent(0, "<<<", 0, ContentTarget(LiteralTransfer("true\n"))),),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(producer, consumer), pipes=(_PipeEvidence(1, 2),))
    ) == (False, None)


def test_command_substitution_sequence_is_not_a_choice() -> None:
    first = _command(1, _arg("printf"), _arg("doc-"), name="printf")
    second = _command(2, _arg("printf"), _arg("lattice"), name="printf")
    sink = _command(3, _arg("eval"), _arg("$(...)", StreamRef(200), dynamic=True), name="eval")
    sequence = _StreamScopeEvidence(
        200,
        "command_substitution",
        None,
        None,
        SequenceOutput((CommandOutput(1), CommandOutput(2))),
    )
    choice_scope = _StreamScopeEvidence(
        201,
        "command_substitution",
        None,
        None,
        ChoiceOutput((CommandOutput(1), CommandOutput(2))),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(first, second, sink), scopes=(sequence,))
    ) == (True, "authored marker flow reaches an execution sink")

    choice_sink = _command(
        3,
        _arg("eval"),
        _arg("$(...)", StreamRef(201), dynamic=True),
        name="eval",
    )
    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(first, second, choice_sink), scopes=(choice_scope,))
    ) == (False, None)


def test_evidence_edge_cap_counts_pipe_records() -> None:
    evidence = _ShellTaintEvidence(pipes=(_PipeEvidence(1, 2), _PipeEvidence(2, 3)))

    assert marker_taint_tuple(evidence, limits=TaintLimits(max_edges=1)) == (
        True,
        "shell taint edge limit exceeded",
    )


def test_pipe_without_consumer_fails_closed() -> None:
    producer = _command(1, _arg("printf"), name="printf")
    evidence = _ShellTaintEvidence(commands=(producer,), pipes=(_PipeEvidence(1),))

    assert marker_taint_tuple(evidence) == (
        True,
        "shell taint pipe cannot be structured",
    )


def test_pipe_with_command_and_scope_consumers_fails_closed() -> None:
    producer = _command(1, _arg("printf"), name="printf")
    consumer = _command(2, _arg("bash"), name="bash")
    consumer_scope = _StreamScopeEvidence(
        3,
        "subshell_group",
        None,
        None,
        SequenceOutput(()),
    )
    evidence = _ShellTaintEvidence(
        commands=(producer, consumer),
        scopes=(consumer_scope,),
        pipes=(_PipeEvidence(1, consumer_command_id=2, consumer_scope_id=3),),
    )

    assert marker_taint_tuple(evidence) == (
        True,
        "shell taint pipe cannot be structured",
    )


def test_reverse_ordered_scope_chain_stays_within_declared_limits() -> None:
    scopes = tuple(
        _StreamScopeEvidence(
            scope_id,
            "subshell_group",
            scope_id - 1 if scope_id > 1 else None,
            None,
            SequenceOutput(()),
        )
        for scope_id in range(1_100, 0, -1)
    )

    assert marker_taint_tuple(_ShellTaintEvidence(scopes=scopes)) == (False, None)


def test_cyclic_scope_parents_fail_closed() -> None:
    scopes = (
        _StreamScopeEvidence(1, "subshell_group", 2, None, SequenceOutput(())),
        _StreamScopeEvidence(2, "subshell_group", 1, None, SequenceOutput(())),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(scopes=scopes)) == (
        True,
        "shell taint stream scope cannot be structured",
    )


def test_scope_content_targets_and_loop_bindings_use_scope_environment() -> None:
    scope = _StreamScopeEvidence(
        100,
        "subshell_group",
        None,
        None,
        SequenceOutput(()),
        redirections=(_RedirectionEvent(0, "<<<", 0, ContentTarget(VariableRef("PAYLOAD"))),),
        loop_bindings=(_AssignmentEvidence("ITEM", VariableRef("PAYLOAD")),),
    )

    contextualized = _contextualize_evidence(
        _ShellTaintEvidence(scopes=(scope,)), limits=TaintLimits()
    )

    assert contextualized.scopes[0].redirections == (
        _RedirectionEvent(
            0,
            "<<<",
            0,
            ContentTarget(VariableRef(_scoped_variable_name(100, "PAYLOAD"))),
        ),
    )
    assert contextualized.scopes[0].loop_bindings == (
        _AssignmentEvidence(
            _scoped_variable_name(100, "ITEM"),
            VariableRef(_scoped_variable_name(100, "PAYLOAD")),
        ),
    )
    assert _contextualize_evidence(contextualized, limits=TaintLimits()).scopes[0].loop_bindings[
        0
    ].name == (_scoped_variable_name(100, "ITEM"))


@pytest.mark.parametrize(
    "kind",
    ["subshell_group", "brace_group"],
    ids=("isolated", "shared"),
)
def test_loop_binding_reaches_eval_in_same_non_root_scope(kind: str) -> None:
    sink = _command(
        10,
        _arg("eval"),
        _arg(
            "$ITEM lattice reconcile",
            Concat((VariableRef("ITEM"), LiteralTransfer("lattice reconcile"))),
            dynamic=True,
        ),
        name="eval",
        container_scope_id=100,
    )
    root = _StreamScopeEvidence(1, "command", None, None, ScopeOutput(100))
    nested = _StreamScopeEvidence(
        100,
        kind,
        1,
        None,
        CommandOutput(10),
        loop_bindings=(_AssignmentEvidence("ITEM", LiteralTransfer("doc-")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(sink,), scopes=(root, nested))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_subshell_loop_binding_does_not_leak_to_parent_eval() -> None:
    sink = _command(
        10,
        _arg("eval"),
        _arg(
            "$ITEM lattice reconcile",
            Concat((VariableRef("ITEM"), LiteralTransfer("lattice reconcile"))),
            dynamic=True,
        ),
        name="eval",
        container_scope_id=1,
    )
    root = _StreamScopeEvidence(
        1,
        "command",
        None,
        None,
        SequenceOutput((ScopeOutput(100), CommandOutput(10))),
    )
    nested = _StreamScopeEvidence(
        100,
        "subshell_group",
        1,
        None,
        SequenceOutput(()),
        loop_bindings=(_AssignmentEvidence("ITEM", LiteralTransfer("doc-")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(sink,), scopes=(root, nested))) == (
        False,
        None,
    )


def test_brace_loop_binding_shares_parent_eval_environment() -> None:
    sink = _command(
        10,
        _arg("eval"),
        _arg(
            "$ITEM lattice reconcile",
            Concat((VariableRef("ITEM"), LiteralTransfer("lattice reconcile"))),
            dynamic=True,
        ),
        name="eval",
        container_scope_id=1,
    )
    root = _StreamScopeEvidence(
        1,
        "command",
        None,
        None,
        SequenceOutput((ScopeOutput(100), CommandOutput(10))),
    )
    nested = _StreamScopeEvidence(
        100,
        "brace_group",
        1,
        None,
        SequenceOutput(()),
        loop_bindings=(_AssignmentEvidence("ITEM", LiteralTransfer("doc-")),),
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(sink,), scopes=(root, nested))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_root_loop_binding_is_inherited_by_child_subshell_eval() -> None:
    sink = _command(
        10,
        _arg("eval"),
        _arg(
            "$ITEM lattice reconcile",
            Concat((VariableRef("ITEM"), LiteralTransfer("lattice reconcile"))),
            dynamic=True,
        ),
        name="eval",
        container_scope_id=100,
    )
    root = _StreamScopeEvidence(
        1,
        "command",
        None,
        None,
        ScopeOutput(100),
        loop_bindings=(_AssignmentEvidence("ITEM", LiteralTransfer("doc-")),),
    )
    child = _StreamScopeEvidence(100, "subshell_group", 1, None, CommandOutput(10))

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(sink,), scopes=(root, child))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_shared_loop_binding_is_inherited_by_deeper_descendant_eval() -> None:
    sink = _command(
        10,
        _arg("eval"),
        _arg(
            "$ITEM lattice reconcile",
            Concat((VariableRef("ITEM"), LiteralTransfer("lattice reconcile"))),
            dynamic=True,
        ),
        name="eval",
        container_scope_id=200,
    )
    root = _StreamScopeEvidence(1, "command", None, None, ScopeOutput(100))
    shared = _StreamScopeEvidence(
        100,
        "brace_group",
        1,
        None,
        ScopeOutput(200),
        loop_bindings=(_AssignmentEvidence("ITEM", LiteralTransfer("doc-")),),
    )
    descendant = _StreamScopeEvidence(
        200,
        "subshell_group",
        100,
        None,
        CommandOutput(10),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(sink,), scopes=(root, shared, descendant))
    ) == (True, "authored marker flow reaches an execution sink")


def test_subshell_loop_binding_does_not_leak_to_sibling_eval() -> None:
    sink = _command(
        10,
        _arg("eval"),
        _arg(
            "$ITEM lattice reconcile",
            Concat((VariableRef("ITEM"), LiteralTransfer("lattice reconcile"))),
            dynamic=True,
        ),
        name="eval",
        container_scope_id=200,
    )
    root = _StreamScopeEvidence(
        1,
        "command",
        None,
        None,
        SequenceOutput((ScopeOutput(100), ScopeOutput(200))),
    )
    local = _StreamScopeEvidence(
        100,
        "subshell_group",
        1,
        None,
        SequenceOutput(()),
        loop_bindings=(_AssignmentEvidence("ITEM", LiteralTransfer("doc-")),),
    )
    sibling = _StreamScopeEvidence(200, "subshell_group", 1, None, CommandOutput(10))

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(sink,), scopes=(root, local, sibling))
    ) == (False, None)


@pytest.mark.parametrize(
    "evidence",
    [
        _ShellTaintEvidence(
            scopes=(
                _StreamScopeEvidence(
                    1,
                    "subshell_group",
                    999,
                    None,
                    SequenceOutput(()),
                ),
            )
        ),
        _ShellTaintEvidence(
            scopes=(
                _StreamScopeEvidence(
                    1,
                    "subshell_group",
                    None,
                    None,
                    ScopeOutput(999),
                ),
            )
        ),
        _ShellTaintEvidence(
            commands=(_command(1, _arg("bash"), name="bash"),),
            pipes=(_PipeEvidence(999, consumer_command_id=1),),
        ),
        _ShellTaintEvidence(
            commands=(
                _command(1, _arg("true"), name="true"),
                _command(1, _arg("bash"), name="bash"),
            ),
        ),
        _ShellTaintEvidence(
            scopes=(
                _StreamScopeEvidence(1, "subshell_group", None, None, SequenceOutput(())),
                _StreamScopeEvidence(1, "subshell_group", None, None, SequenceOutput(())),
            )
        ),
        _ShellTaintEvidence(
            process_resources=(
                _ProcessResourceEvidence(1, 10, "input"),
                _ProcessResourceEvidence(1, 11, "input"),
            )
        ),
        _ShellTaintEvidence(
            commands=(_command(1, _arg("printf"), name="printf"),),
            pipes=(_PipeEvidence(1, consumer_command_id=999),),
        ),
        _ShellTaintEvidence(
            commands=(_command(1, _arg("printf"), name="printf"),),
            pipes=(_PipeEvidence(1, consumer_scope_id=999),),
        ),
        _ShellTaintEvidence(
            process_resources=(_ProcessResourceEvidence(1, 999, "input"),),
        ),
        _ShellTaintEvidence(
            scopes=(
                _StreamScopeEvidence(1, "subshell_group", None, None, ScopeOutput(2)),
                _StreamScopeEvidence(2, "subshell_group", None, None, ScopeOutput(1)),
            )
        ),
        _ShellTaintEvidence(
            scopes=(
                _StreamScopeEvidence(
                    1,
                    "pipeline",
                    None,
                    None,
                    SequenceOutput(()),
                    entry=ScopeOutput(2),
                ),
                _StreamScopeEvidence(
                    2,
                    "pipeline",
                    None,
                    None,
                    SequenceOutput(()),
                    entry=ScopeOutput(1),
                ),
            )
        ),
    ],
    ids=(
        "dangling-parent-scope",
        "dangling-scope-output",
        "dangling-pipe-producer",
        "duplicate-command-id",
        "duplicate-scope-id",
        "duplicate-process-resource-id",
        "dangling-command-consumer",
        "dangling-scope-consumer",
        "dangling-process-resource-scope",
        "scope-output-cycle",
        "scope-entry-cycle",
    ),
)
def test_malformed_nested_evidence_references_fail_closed(
    evidence: _ShellTaintEvidence,
) -> None:
    assert marker_taint_tuple(evidence) == (
        True,
        "shell taint evidence cannot be structured",
    )


def test_evidence_count_limit_precedes_nested_reference_validation() -> None:
    duplicate_scopes = (
        _StreamScopeEvidence(1, "subshell_group", None, None, SequenceOutput(())),
        _StreamScopeEvidence(1, "subshell_group", None, None, SequenceOutput(())),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(scopes=duplicate_scopes),
        limits=TaintLimits(max_table_entries=1),
    ) == (True, "shell taint table entry limit exceeded")


def test_uppercase_eval_is_not_a_builtin_execution_sink() -> None:
    command = _command(1, _arg("EVAL"), _arg("doc-lattice"), name="EVAL")

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_uppercase_source_is_not_a_builtin_execution_sink() -> None:
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget("task.sh")),),
    )
    command = _command(2, _arg("SOURCE"), _arg("task.sh"), name="SOURCE")

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(writer, command))) == (False, None)


@pytest.mark.parametrize("builtin", ["eval", "source"])
def test_path_qualified_builtin_name_uses_static_direct_resource(builtin: str) -> None:
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget(builtin)),),
    )
    command = _command(2, _arg(f"./{builtin}"), _arg("safe"), name=builtin)

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(writer, command))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_shell_plus_c_selects_the_command_payload() -> None:
    command = _command(1, _arg("bash"), _arg("+c"), _arg("doc-lattice"), name="bash")

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_static_normalization_collapses_double_slash_absolute_resource_keys() -> None:
    assert normalize_static_resource("//task.sh", dynamic=False) == "/task.sh"
    assert normalize_static_resource("///task.sh", dynamic=False) == "/task.sh"

    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget("/task.sh")),),
    )
    reader = _command(2, _arg("bash"), _arg("//task.sh"), name="bash")

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(writer, reader))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("foo/../task.sh", "task.sh"),
        ("foo/./bar/../task.sh", "foo/task.sh"),
        ("../task.sh", "../task.sh"),
        ("foo/../../task.sh", "../task.sh"),
        ("/../../task.sh", "/task.sh"),
    ],
)
def test_static_normalization_collapses_parent_segments(literal: str, expected: str) -> None:
    assert normalize_static_resource(literal, dynamic=False) == expected


def test_deep_structured_output_is_lowered_without_recursion_error() -> None:
    output: OutputExpr = CommandOutput(1)
    for _ in range(1_200):
        output = SequenceOutput((output,))
    producer = _command(1, _arg("printf"), _arg("doc-lattice"), name="printf")
    sink = _command(2, _arg("eval"), _arg("$(...)", StreamRef(200), dynamic=True), name="eval")
    scope = _StreamScopeEvidence(200, "command_substitution", None, None, output)

    evidence = _ShellTaintEvidence(commands=(producer, sink), scopes=(scope,))

    assert marker_taint_tuple(evidence) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_empty_repeat_preserves_recursive_equation_for_node_limit_accounting() -> None:
    scope = _StreamScopeEvidence(
        200,
        "pipeline",
        None,
        None,
        RepeatOutput(SequenceOutput(())),
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(scopes=(scope,)),
        limits=TaintLimits(max_expression_nodes=3),
    ) == (True, "shell taint expression node limit exceeded")


def test_ambiguous_shell_selector_includes_static_script_candidate() -> None:
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget("task.sh")),),
    )
    shell = _command(
        2,
        _arg("bash"),
        _arg("$OPT", OutsideGap(), dynamic=True),
        _arg("task.sh"),
        name="bash",
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(writer, shell))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_ambiguous_shell_selector_includes_input_process_script_candidate() -> None:
    producer = _command(1, _arg("printf"), _arg("doc-lattice"), name="printf")
    scope = _StreamScopeEvidence(
        200,
        "process_substitution",
        None,
        None,
        CommandOutput(1),
    )
    shell = _command(
        2,
        _arg("bash"),
        _arg("$OPT", OutsideGap(), dynamic=True),
        _ArgPort("proc", LiteralTransfer("safe"), process_resource_id=7),
        name="bash",
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(
            commands=(producer, shell),
            scopes=(scope,),
            process_resources=(_ProcessResourceEvidence(7, 200, "input"),),
        )
    ) == (True, "authored marker flow reaches an execution sink")


def test_relative_direct_executable_with_slash_reads_static_resource() -> None:
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget("dir/task.sh")),),
    )
    reader = _command(2, _arg("dir/task.sh"), name="dir/task.sh")

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(writer, reader))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_direct_path_named_like_shell_remains_a_resource_sink() -> None:
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget("dir/bash")),),
    )
    reader = _command(
        2,
        _arg("dir/bash"),
        _arg("-c"),
        _arg("true"),
        name="bash",
    )

    assert marker_taint_tuple(_ShellTaintEvidence(commands=(writer, reader))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_deep_producer_content_hits_node_limit_without_recursion_error() -> None:
    deep_concat = _deep_concat(1_200)
    deep_choice: ContentExpr = LiteralTransfer("y")
    for _ in range(1_200):
        deep_choice = Choice((deep_choice,))
    concat_producer = _command(1, _arg("printf"), _arg("x", deep_concat), name="printf")
    choice_producer = _command(2, _arg("printf"), _arg("y", deep_choice), name="printf")

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(concat_producer, choice_producer)),
        limits=TaintLimits(max_expression_nodes=3),
    ) == (True, "shell taint expression node limit exceeded")


def test_concat_threads_dfa_state_across_fragment_boundaries() -> None:
    expression = Concat((LiteralTransfer("doc-"), LiteralTransfer("lattice reconcile")))

    assert _can_mark(expression) is True


def test_choice_joins_alternatives_without_concatenating_them() -> None:
    expression = Choice((LiteralTransfer("doc-"), LiteralTransfer("lattice")))

    assert _can_mark(expression) is False


def test_concat_recursively_flattens_and_discards_exposed_epsilon_literals() -> None:
    expression = concat(
        Concat((LiteralTransfer(""), Concat((LiteralTransfer("x"), LiteralTransfer("")))))
    )

    assert expression == LiteralTransfer("x")


def test_choice_recursively_flattens_while_retaining_epsilon_alternatives() -> None:
    expression = choice(
        Choice((LiteralTransfer(""), Choice((LiteralTransfer("x"), LiteralTransfer("y")))))
    )

    assert expression == Choice((LiteralTransfer(""), LiteralTransfer("x"), LiteralTransfer("y")))


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (Concat((LiteralTransfer("doc-"), OutsideGap(), LiteralTransfer("lattice"))), True),
        (Concat((LiteralTransfer("doc"), OutsideGap(), LiteralTransfer("lattice"))), False),
    ],
    ids=("authored-separator", "external-separator"),
)
def test_outside_gap_offers_epsilon_and_non_authored_barrier(
    expression: Concat, expected: bool
) -> None:
    assert _can_mark(expression) is expected


def test_command_substitution_strips_only_trailing_newlines() -> None:
    assert _can_mark(Concat((LiteralTransfer("doc-\n"), LiteralTransfer("lattice")))) is False
    assert _can_mark(Concat((LiteralTransfer("prefix"), LiteralTransfer("doc-\n")))) is False
    assert _can_mark(Concat((LiteralTransfer(""), LiteralTransfer("lattice")))) is False
    assert _can_mark(LiteralTransfer("doc-\nlattice")) is False
    assert _can_mark(LiteralTransfer("doc-lattice")) is True
    assert _can_mark(Concat((LiteralTransfer("doc-"), LiteralTransfer("lattice")))) is True

    substituted = Concat((LiteralTransfer("doc-\n"),))
    stripped_left = _strip_trailing_newlines(_evaluate_closed(substituted))
    right = _evaluate_closed(LiteralTransfer("lattice"))
    composed = frozenset(left.compose(after) for left in stripped_left for after in right)

    assert _marker_capable(composed) is True


def test_variable_assignment_and_append_compose_in_the_fixed_point() -> None:
    solved = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("X", LiteralTransfer("doc-")),
                _FlowWrite("X", LiteralTransfer("lattice"), append=True),
            )
        ),
        limits=TaintLimits(),
    )

    assert _marker_capable(solved.evaluate(VariableRef("X"))) is True


def test_append_before_assignment_revisits_its_implicit_destination_dependency() -> None:
    solved = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("X", LiteralTransfer("lattice"), append=True),
                _FlowWrite("X", LiteralTransfer("doc-")),
            )
        ),
        limits=TaintLimits(),
    )

    assert _marker_capable(solved.evaluate(VariableRef("X"))) is True


def test_declared_forward_reference_uses_bottom_regardless_of_write_order() -> None:
    expression = Concat((LiteralTransfer("doc-"), VariableRef("Y"), LiteralTransfer("lattice")))
    first = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("X", expression),
                _FlowWrite("Y", LiteralTransfer("x")),
            )
        ),
        limits=TaintLimits(),
    )
    second = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("Y", LiteralTransfer("x")),
                _FlowWrite("X", expression),
            )
        ),
        limits=TaintLimits(),
    )

    assert first.evaluate(VariableRef("X")) == second.evaluate(VariableRef("X"))
    assert _marker_capable(first.evaluate(VariableRef("X"))) is False


def test_deep_expression_below_node_cap_evaluates_without_recursion_error() -> None:
    expression = _deep_concat(1_100)

    solved = _solve_flow_definitions(
        _FlowDefinitions(variable_writes=(_FlowWrite("X", expression),)),
        limits=TaintLimits(max_expression_nodes=3_000),
    )

    assert _marker_capable(solved.evaluate(VariableRef("X"))) is False


def test_deep_expression_over_node_cap_raises_limit_error_without_recursion_error() -> None:
    expression = _deep_concat(1_100)

    with pytest.raises(_TaintLimitExceeded, match="shell taint expression node limit exceeded"):
        _solve_flow_definitions(
            _FlowDefinitions(variable_writes=(_FlowWrite("X", expression),)),
            limits=TaintLimits(max_expression_nodes=100),
        )


def test_taint_limit_exception_is_a_coded_project_error() -> None:
    refusal = GuardRefusal(
        "taint.values.alternative-limit",
        "shell taint alternative limit exceeded",
    )
    error = _TaintLimitExceeded(refusal)

    assert isinstance(error, ProjectError)
    assert error.code == "SHELL_TAINT_LIMIT_EXCEEDED"
    assert str(error) == "shell taint alternative limit exceeded"
    assert error.refusal is refusal


def test_competing_variable_definitions_join_without_composing() -> None:
    solved = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("X", LiteralTransfer("doc-")),
                _FlowWrite("X", LiteralTransfer("lattice")),
            )
        ),
        limits=TaintLimits(),
    )

    assert _marker_capable(solved.evaluate(VariableRef("X"))) is False


def test_resource_append_and_stream_strip_resolve_through_typed_tables() -> None:
    solved = _solve_flow_definitions(
        _FlowDefinitions(
            resource_writes=(
                _FlowWrite("task.sh", LiteralTransfer("doc-")),
                _FlowWrite("task.sh", LiteralTransfer("lattice\n"), append=True),
            ),
            stream_writes=(
                _FlowWrite(
                    7,
                    Concat((ResourceRef("task.sh"), LiteralTransfer(""))),
                    strip_trailing_newlines=True,
                ),
            ),
        ),
        limits=TaintLimits(),
    )

    assert _marker_capable(solved.evaluate(ResourceRef("task.sh"))) is True
    assert _marker_capable(solved.evaluate(StreamRef(7))) is True


def test_mutually_referential_variables_converge_by_least_fixed_point() -> None:
    solved = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("X", LiteralTransfer("doc-")),
                _FlowWrite("X", VariableRef("Y")),
                _FlowWrite("Y", VariableRef("X")),
            )
        ),
        limits=TaintLimits(),
    )

    assert (
        _marker_capable(solved.evaluate(Concat((VariableRef("Y"), LiteralTransfer("lattice")))))
        is True
    )


@pytest.mark.parametrize(
    ("definitions", "limits", "message"),
    [
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", Choice((LiteralTransfer("a"), LiteralTransfer("b")))),
                )
            ),
            TaintLimits(max_alternatives=1),
            "shell taint alternative limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", Concat((LiteralTransfer("a"), LiteralTransfer("b")))),
                )
            ),
            TaintLimits(max_expression_nodes=2),
            "shell taint expression node limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", LiteralTransfer("a")),
                    _FlowWrite("Y", LiteralTransfer("b")),
                )
            ),
            TaintLimits(max_table_entries=1),
            "shell taint table entry limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", LiteralTransfer("a")),
                    _FlowWrite("Y", VariableRef("X")),
                )
            ),
            TaintLimits(max_edges=1),
            "shell taint edge limit exceeded",
        ),
        (
            _FlowDefinitions(
                variable_writes=(
                    _FlowWrite("X", LiteralTransfer("a")),
                    _FlowWrite("Y", VariableRef("X")),
                )
            ),
            TaintLimits(max_fixed_point_updates=1),
            "shell taint fixed-point update limit exceeded",
        ),
    ],
    ids=("alternatives", "nodes", "tables", "edges", "updates"),
)
def test_every_flow_bound_fails_closed(
    definitions: _FlowDefinitions, limits: TaintLimits, message: str
) -> None:
    with pytest.raises(_TaintLimitExceeded, match=message):
        _solve_flow_definitions(definitions, limits=limits)


def test_eval_reparse_depth_cap_is_a_taint_limits_field_that_shrinks() -> None:
    """Issue #139: the eval reparse depth cap must be shrinkable to prove the guard fires.

    ``_MAX_EVAL_REPARSE_DEPTH`` used to be a module constant no test could reach. Promoted
    into ``TaintLimits.max_eval_reparse_depth``, a deeply nested (but otherwise unremarkable)
    ``Concat`` now exhausts it deterministically.
    """
    with pytest.raises(_TaintLimitExceeded, match="shell taint eval reparse depth limit exceeded"):
        _eval_reparse_content(_deep_concat(50), TaintLimits(max_eval_reparse_depth=5))


def test_eval_reparse_branch_cap_is_a_taint_limits_field_that_shrinks() -> None:
    """Issue #139: the eval reparse branch cap must be shrinkable to prove the guard fires."""
    with pytest.raises(_TaintLimitExceeded, match="shell taint eval reparse branch limit exceeded"):
        _eval_reparse_content(
            Choice((LiteralTransfer("a"), LiteralTransfer("b"))),
            TaintLimits(max_eval_reparse_branches=1),
        )


def test_eval_command_substitution_depth_cap_shrinks_with_taint_limits() -> None:
    """The nested ``$(...)`` closing scanner shares the promoted depth field, not a constant."""
    with pytest.raises(
        _TaintLimitExceeded, match="shell taint eval command substitution cannot be bounded"
    ):
        _eval_command_substitution_closing(
            "$(x)", 0, limits=TaintLimits(max_eval_reparse_depth=0), depth=1
        )


def test_eval_command_substitution_depth_cap_fails_closed_end_to_end() -> None:
    """A nested command substitution inside an authored ``eval`` body exhausts the shrunk cap.

    This is the end-to-end path (through ``analyze_marker_taint``), not just the direct unit
    call, so it proves the promoted field actually reaches the scanning entry point.
    """
    command = _command(
        1,
        _arg("eval"),
        _arg("$($(x))", LiteralTransfer("$($(x))")),
        name="eval",
    )

    assert marker_taint_tuple(
        _ShellTaintEvidence(commands=(command,)),
        limits=TaintLimits(max_eval_reparse_depth=0),
    ) == (True, "shell taint eval command substitution cannot be bounded")


def test_eval_syntax_append_depth_cap_shrinks_with_taint_limits() -> None:
    """The eval-syntax second-pass appender shares the promoted depth field via its context."""
    with pytest.raises(_TaintLimitExceeded, match="shell taint eval reparse depth limit exceeded"):
        _solve_eval_syntax_variables(
            (_FlowWrite("X", _deep_concat(50)),),
            {},
            TaintLimits(max_eval_reparse_depth=5),
        )


def test_deep_local_content_substitution_fails_with_stable_bound() -> None:
    depth = 1_100
    local_contents: dict[str, ContentExpr] = {
        f"V{index}": VariableRef(f"V{index + 1}") for index in range(depth)
    }
    local_contents[f"V{depth}"] = LiteralTransfer("doc-")

    with pytest.raises(_TaintLimitExceeded, match="local substitution depth limit"):
        _substitute_local_contents(VariableRef("V0"), local_contents, limits=TaintLimits())


def test_bounded_local_content_substitution_preserves_marker_control() -> None:
    depth = 64
    local_contents: dict[str, ContentExpr] = {
        f"V{index}": VariableRef(f"V{index + 1}") for index in range(depth)
    }
    local_contents[f"V{depth}"] = LiteralTransfer("doc-")

    assert _substitute_local_contents(
        VariableRef("V0"), local_contents, limits=TaintLimits()
    ) == LiteralTransfer("doc-")


def _eval_command(program: str, *, function_context_id: int | None = None) -> _CommandEvidence:
    return replace(
        _command(1, _arg("eval"), _arg(program), name="eval"),
        resolved_eval_program=program,
        function_context_id=function_context_id,
    )


def _mutation_names(command: _CommandEvidence) -> list[str]:
    assignments, _ = _static_eval_mutations(command, limits=TaintLimits())
    return [item.assignment.name for item in assignments]


def test_static_eval_recovers_a_scalar_assignment_from_an_exact_payload() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("X=doc-"), limits=TaintLimits())

    assert unsets == ()
    assert len(assignments) == 1
    assert assignments[0].assignment.name == "X"
    assert assignments[0].assignment.content == LiteralTransfer("doc-")
    assert (assignments[0].local, assignments[0].force_global) == (False, False)


def test_static_eval_recovers_every_word_of_an_assignment_only_command() -> None:
    assignments, unsets = _static_eval_mutations(
        _eval_command("X=doc- Y=lattice"), limits=TaintLimits()
    )

    assert unsets == ()
    assert [(item.assignment.name, item.assignment.content) for item in assignments] == [
        ("X", LiteralTransfer("doc-")),
        ("Y", LiteralTransfer("lattice")),
    ]


def test_static_eval_ignores_assignment_prefix_words_of_an_executed_command() -> None:
    assert _static_eval_mutations(_eval_command("X=doc- printf hi"), limits=TaintLimits()) == (
        (),
        (),
    )


@pytest.mark.parametrize(
    ("program", "expected_local", "expected_force_global"),
    [
        # Outside a function, `declare`/`typeset` create a plain global variable, exactly like a
        # bare assignment -- only inside a function (see the dedicated test below) do they shadow
        # the caller. Issue #117 follow-up: these two used to be mislabeled local=True with no
        # function-context gate, which excluded them from the eval-replayed-assignment lowering.
        ("declare X=v", False, False),
        ("declare -g X=v", False, True),
        ("export X=v", False, False),
        ("readonly X=v", False, False),
        ("typeset X=v", False, False),
        ("typeset -g X=v", False, True),
    ],
    ids=("declare", "declare-global", "export", "readonly", "typeset", "typeset-global"),
)
def test_static_eval_declaration_builtin_carries_its_scope(
    program: str, expected_local: bool, expected_force_global: bool
) -> None:
    assignments, unsets = _static_eval_mutations(_eval_command(program), limits=TaintLimits())

    assert unsets == ()
    assert len(assignments) == 1
    assert assignments[0].assignment.name == "X"
    assert (assignments[0].local, assignments[0].force_global) == (
        expected_local,
        expected_force_global,
    )


def test_static_eval_declare_inside_a_function_context_is_scoped_local() -> None:
    """Companion to the top-level case above: inside a function, `declare` (like `local`) IS
    genuinely local, so it must still carry `local=True` there. Only the no-function-context case
    was mislabeled.
    """
    assignments, _ = _static_eval_mutations(
        _eval_command("declare X=v", function_context_id=7), limits=TaintLimits()
    )

    assert len(assignments) == 1
    assert assignments[0].assignment.name == "X"
    assert (assignments[0].local, assignments[0].force_global) == (True, False)


def test_static_eval_local_declaration_inside_a_function_context_is_scoped_local() -> None:
    assignments, _ = _static_eval_mutations(
        _eval_command("local X=v", function_context_id=7), limits=TaintLimits()
    )

    assert len(assignments) == 1
    assert assignments[0].assignment.name == "X"
    assert (assignments[0].local, assignments[0].force_global) == (True, False)


def test_static_eval_skips_a_local_declaration_without_a_function_context() -> None:
    assert _static_eval_mutations(_eval_command("local X=v"), limits=TaintLimits()) == ((), ())


def test_static_eval_unset_collects_names_and_excludes_option_words() -> None:
    assignments, unsets = _static_eval_mutations(
        _eval_command("unset -v X Y"), limits=TaintLimits()
    )

    assert assignments == ()
    assert unsets == ("X", "Y")


def test_static_eval_function_only_unset_keeps_the_variable_defined() -> None:
    assert _static_eval_mutations(_eval_command("unset -f X"), limits=TaintLimits()) == ((), ())


def test_static_eval_combined_unset_still_removes_the_variable() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("unset -vf X"), limits=TaintLimits())

    assert assignments == ()
    assert unsets == ("X",)


def test_static_eval_unset_after_end_of_options_collects_the_operand() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("unset -- X"), limits=TaintLimits())

    assert assignments == ()
    assert unsets == ("X",)


def test_static_eval_nameref_unset_fails_closed() -> None:
    with pytest.raises(_TaintLimitExceeded, match="nameref unset"):
        _static_eval_mutations(_eval_command("unset -n R"), limits=TaintLimits())


@pytest.mark.parametrize(
    "program",
    [
        "{ X=doc-; }",
        "for i in 1; do X=doc-; done",
        "time X=doc-",
        "! X=doc-",
        "command -p export X=doc-",
    ],
    ids=("brace-group", "loop-body", "time-prefix", "negation", "command-wrapper-options"),
)
def test_static_eval_recovers_an_assignment_behind_a_reserved_word_prefix(program: str) -> None:
    assignments, _ = _static_eval_mutations(_eval_command(program), limits=TaintLimits())

    assert [item.assignment.name for item in assignments] == ["X"]
    assert assignments[0].assignment.content == LiteralTransfer("doc-")


@pytest.mark.parametrize(
    "name",
    ["\u00b2", "\u0661", "1\u00b2"],
    ids=("superscript-two", "arabic-indic-one", "mixed-ascii-and-superscript"),
)
def test_non_ascii_digits_are_not_function_positional_parameters(name: str) -> None:
    assert _is_function_positional_parameter(name) is False


@pytest.mark.parametrize(
    "program",
    [
        "builtin export X=v",
        "builtin -- export X=v",
        "command export X=v",
        "command -- export X=v",
    ],
    ids=("builtin", "builtin-dashdash", "command", "command-dashdash"),
)
def test_static_eval_skips_execution_wrappers_before_reading_the_executable(program: str) -> None:
    assert _mutation_names(_eval_command(program)) == ["X"]


def test_static_eval_nameref_routes_a_later_write_to_its_target() -> None:
    assignments, unsets = _static_eval_mutations(
        _eval_command("declare -n R=X; R=doc-"), limits=TaintLimits()
    )

    assert unsets == ()
    assert [item.assignment.name for item in assignments] == ["R", "X"]
    assert assignments[0].assignment.nameref_target == "X"
    assert assignments[1].assignment.content == LiteralTransfer("doc-")


@pytest.mark.parametrize(
    ("program", "message"),
    [
        ("declare -n R=R; R=doc-", "shell eval nameref cycle cannot be represented"),
        ("declare -n R=1bad; R=doc-", "shell eval nameref target cannot be represented"),
    ],
    ids=("cycle", "non-name-target"),
)
def test_static_eval_nameref_hazard_fails_closed(program: str, message: str) -> None:
    with pytest.raises(_TaintLimitExceeded, match=message) as raised:
        _static_eval_mutations(_eval_command(program), limits=TaintLimits())

    assert isinstance(raised.value, ProjectError)
    assert raised.value.code == "SHELL_TAINT_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "program",
    [
        'eval "X=doc-"',
        'command eval "X=doc-"',
        'builtin eval "X=doc-"',
    ],
    ids=("plain", "command-wrapper", "builtin-wrapper"),
)
def test_static_eval_nested_eval_fails_closed(program: str) -> None:
    # A nested ``eval`` persists its assignment to the calling shell under real bash, but the
    # interpreter does not recurse into eval payloads, so the mutation would otherwise silently
    # contribute no evidence (issue #114).
    with pytest.raises(
        _TaintLimitExceeded, match="shell nested eval state cannot be represented"
    ) as raised:
        _static_eval_mutations(_eval_command(program), limits=TaintLimits())

    assert isinstance(raised.value, ProjectError)
    assert raised.value.code == "SHELL_TAINT_LIMIT_EXCEEDED"


def test_static_eval_nested_eval_shadowed_by_a_function_does_not_fail_closed() -> None:
    # A function named ``eval`` shadows the builtin, so the payload's ``eval`` word never runs
    # the real builtin and never persists state.
    command = replace(_eval_command('eval "X=doc-"'), active_function_names=frozenset({"eval"}))

    assert _static_eval_mutations(command, limits=TaintLimits()) == ((), ())


def test_static_eval_nested_eval_in_an_unreachable_branch_does_not_fail_closed() -> None:
    # The outer eval invocation itself is statically unreachable, so its payload never runs.
    command = replace(_eval_command('eval "X=doc-"'), execution_status=False)

    assert _static_eval_mutations(command, limits=TaintLimits()) == ((), ())


def test_static_eval_nested_eval_asynchronous_does_not_persist_state() -> None:
    # An asynchronous nested eval runs in a subshell whose mutations do not reach the caller.
    assert _static_eval_mutations(_eval_command('eval "X=doc-" &'), limits=TaintLimits()) == (
        (),
        (),
    )


@pytest.mark.parametrize(
    "program",
    [
        "A=(doc-)",
        "A=(doc- lattice)",
        "A+=(lattice)",
        "A=()",
        "declare A=(doc-)",
        "export A=(doc-)",
        "readonly A=(doc-)",
        "A[0]=doc-",
        "A[0]+=doc-",
        "declare -a A",
        "local -A A",
        "typeset -a A",
    ],
    ids=(
        "compound",
        "compound-two-elements",
        "compound-append",
        "compound-empty",
        "declare-compound",
        "export-compound",
        "readonly-compound",
        "element",
        "element-append",
        "declare-indexed",
        "local-associative",
        "typeset-indexed",
    ),
)
def test_static_eval_array_assignment_fails_closed(program: str) -> None:
    # The payload tokenizer lexes an unquoted ``(`` as a command separator, so ``A=(doc-)`` used
    # to record the scalar ``A = ""`` and scatter its elements into the following commands.
    with pytest.raises(
        _TaintLimitExceeded,
        match="shell eval array assignment cannot be represented",
    ) as raised:
        _static_eval_mutations(_eval_command(program, function_context_id=1), limits=TaintLimits())

    assert raised.value.code == "SHELL_TAINT_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    ("program", "content"),
    [
        ("A='(doc-)'", LiteralTransfer("(doc-)")),
        ("A=\\(doc-\\)", LiteralTransfer("(doc-)")),
    ],
    ids=("quoted", "escaped"),
)
def test_static_eval_keeps_a_quoted_parenthesis_scalar(program: str, content: ContentExpr) -> None:
    # A quoted or escaped ``(`` stays inside its word, where it really is one scalar character.
    assignments, unsets = _static_eval_mutations(_eval_command(program), limits=TaintLimits())

    assert unsets == ()
    assert [(item.assignment.name, item.assignment.content) for item in assignments] == [
        ("A", content)
    ]


def test_static_eval_array_assignment_in_an_unreachable_branch_is_pruned() -> None:
    assert _static_eval_mutations(
        _eval_command("if false; then A=(doc-); fi"), limits=TaintLimits()
    ) == ((), ())


def test_static_eval_prunes_mutations_of_an_unreachable_branch() -> None:
    program = "if false; then X=doc-; fi"
    parsed = _static_eval_program_commands(program, limits=TaintLimits())

    assert [item.execution_status for item in parsed] == [True, False, True]
    assert _static_eval_mutations(_eval_command(program), limits=TaintLimits()) == ((), ())


@pytest.mark.parametrize(
    "program",
    [
        "X='doc-",
        'X="doc-',
        "X=doc- \\",
    ],
    ids=("unterminated-single-quote", "unterminated-double-quote", "trailing-backslash"),
)
def test_static_eval_unacceptable_payload_fails_closed(program: str) -> None:
    # issue #134: a payload the tokenizer cannot accept used to contribute no evidence at all,
    # silently discarding any mutation (or a later sink's dependence on one) instead of
    # refusing. Missing evidence is not the same as no evidence, so this must fail closed.
    with pytest.raises(
        _TaintLimitExceeded, match="shell eval payload cannot be tokenized"
    ) as raised:
        _static_eval_program_commands(program, limits=TaintLimits())

    assert raised.value.code == "SHELL_TAINT_LIMIT_EXCEEDED"

    with pytest.raises(_TaintLimitExceeded, match="shell eval payload cannot be tokenized"):
        _static_eval_mutations(_eval_command(program), limits=TaintLimits())


def test_static_eval_backslash_newline_continuation_matches_the_unsplit_payload() -> None:
    # A backslash-newline line continuation is ordinary, reachable Bash (issue #134): Bash
    # removes it before parsing, joining the two lines with no character inserted. The
    # tokenizer now does the same, so a continued and an unsplit payload recover the exact
    # same assignment instead of the continuation silently losing the mutation.
    continued = _static_eval_mutations(_eval_command("X=doc- \\\n; true"), limits=TaintLimits())
    unsplit = _static_eval_mutations(_eval_command("X=doc-; true"), limits=TaintLimits())

    assert continued == unsplit
    assert [item.assignment.content for item in continued[0]] == [LiteralTransfer("doc-")]


def test_eval_payload_line_continuation_false_safe_is_closed() -> None:
    # Verified false-safe from issue #134, reproduced under real Bash 5.2: the continuation
    # used to defeat the eval payload tokenizer entirely, so the marker flow through the
    # recovered assignment went undetected and the body certified clean. Bash's own line
    # continuation is ordinary and reachable, so the fix teaches the tokenizer to join it
    # rather than merely refusing whenever it appears; the body now refuses through the same
    # marker-flow mechanism as the unsplit control below, which already refused correctly.
    exploit = "eval 'X=doc- \\\n; true'; eval \"$X\"lattice"
    control = "eval 'X=doc-; true'; eval \"$X\"lattice"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_static_eval_commands_split_an_exact_payload_on_separators() -> None:
    parsed = _static_eval_commands(_eval_command("printf a; declare -g X=1"), limits=TaintLimits())

    assert [item.words for item in parsed] == [("printf", "a"), ("declare", "-g", "X=1")]


@pytest.mark.parametrize(
    ("payload", "operator", "descriptor", "target"),
    [
        ("printf a > out.sh", ">", 1, StaticResourceTarget("out.sh")),
        ("printf a >> out.sh", ">>", 1, StaticResourceTarget("out.sh")),
        ("printf a >| out.sh", ">|", 1, StaticResourceTarget("out.sh")),
        ("printf a 2> out.sh", ">", 2, StaticResourceTarget("out.sh")),
        ("printf a &> out.sh", "&>", 1, StaticResourceTarget("out.sh")),
        ("printf a >&2", ">&", 1, DescriptorTarget(2)),
        ("printf a > /dev/null", ">", 1, NullTarget()),
        ("printf a > /dev/fd/1", ">", 1, DescriptorTarget(1)),
        ("printf a < in.sh", "<", 0, StaticResourceTarget("in.sh")),
        ("printf a > $LOG", ">", 1, DynamicResourceTarget()),
    ],
)
def test_static_eval_commands_retain_payload_redirections(
    payload: str,
    operator: str,
    descriptor: int,
    target: object,
) -> None:
    """Issue #146: an eval payload's own redirections are evidence, not noise.

    The tokenizer used to drop the operator and skip its target word outright, so a write
    performed inside an exact payload registered no resource write and a later sink read a key
    the model believed was never written. Retaining the event is the first of the two halves;
    ``test_eval_payload_write_reaches_a_sourced_sink`` pins the flow-graph half.
    """
    (parsed,) = _static_eval_commands(_eval_command(payload), limits=TaintLimits())

    assert parsed.words == ("printf", "a")
    assert [(item.operator, item.descriptor, item.target) for item in parsed.redirections] == [
        (operator, descriptor, target)
    ]


def test_static_eval_commands_retain_a_brace_descriptor_without_a_number() -> None:
    """A ``{fd}>`` names a descriptor Bash chooses at run time, so it binds nothing statically."""
    (parsed,) = _static_eval_commands(_eval_command("printf a {fd}> out.sh"), limits=TaintLimits())

    assert parsed.words == ("printf", "a")
    assert [(item.operator, item.descriptor) for item in parsed.redirections] == [(">", None)]


def test_static_eval_commands_order_payload_redirections_left_to_right() -> None:
    """Ordinals sequence the replay, so a truncation before an append keeps its side effect."""
    (parsed,) = _static_eval_commands(
        _eval_command("printf a > first.sh 2>> second.sh"), limits=TaintLimits()
    )

    assert [(item.ordinal, item.operator, item.target) for item in parsed.redirections] == [
        (0, ">", StaticResourceTarget("first.sh")),
        (1, ">>", StaticResourceTarget("second.sh")),
    ]


def test_static_eval_commands_separate_redirections_per_payload_command() -> None:
    parsed = _static_eval_commands(
        _eval_command("printf a > first.sh; printf b > second.sh"), limits=TaintLimits()
    )

    assert [item.words for item in parsed] == [("printf", "a"), ("printf", "b")]
    assert [[event.target for event in item.redirections] for item in parsed] == [
        [StaticResourceTarget("first.sh")],
        [StaticResourceTarget("second.sh")],
    ]


def test_static_eval_commands_fail_closed_on_an_unmodeled_redirection_operator() -> None:
    """An operator run this analysis cannot name is missing evidence, not the absence of any.

    Dropping it silently would leave the write it performs unmodeled while the rest of the
    payload still contributed state, which is the same fail-closed reasoning issue #134 applied
    to a payload the tokenizer cannot accept.
    """
    with pytest.raises(_TaintLimitExceeded, match="shell eval payload cannot be tokenized"):
        _static_eval_commands(_eval_command("printf a >>| out.sh"), limits=TaintLimits())


def test_static_eval_command_names_dedupe_in_first_use_order() -> None:
    names = _static_eval_command_names(
        _eval_command("printf a; printf b; declare -g X=1"), limits=TaintLimits()
    )

    assert names == ("printf", "declare")


def test_static_eval_programs_prefer_the_resolved_program_list() -> None:
    command = replace(
        _command(1, _arg("eval"), _arg("Z=3"), name="eval"),
        resolved_eval_program="A=1",
        resolved_eval_programs=("B=2", "C=3"),
    )

    assert _static_eval_programs(command) == ("B=2", "C=3")


def test_static_eval_programs_fall_back_to_joined_literal_arguments() -> None:
    command = _command(1, _arg("eval"), _arg("X=doc-"), _arg("Y=2"), name="eval")

    assert _static_eval_programs(command) == ("X=doc- Y=2",)


@pytest.mark.parametrize("spelling", ["source", "."])
def test_source_payload_persists_assignment_fails_closed(spelling: str) -> None:
    """Verified false-safe from issue #133, reproduced under real Bash 5.2.

    AD-18 replays an eval payload's state effects because the payload text is directly in the
    command's own arguments. A source payload's state effects live in a FILE the argument only
    names, and this analysis has no exact-literal model of a sourced file's content the way it
    does variable assignments, so it cannot rule out a marker-composing assignment such as
    ``X=doc-``. Before this fix the body below certified clean and executed the marker; the
    control shows the identical flow through a direct eval payload already refuses correctly.
    The written content ``X=doc-`` carries the marker fragment ``doc-`` as a PREFIX -- see
    ``test_source_payload_suffix_fragment_fails_closed`` for the mirror-image SUFFIX case, and
    ``test_marker_free_write_then_source_still_certifies`` for the companion over-refusal guard,
    where the sourced content carries no such fragment either way.
    """
    control = "eval 'X=doc-'; eval \"$X\"lattice"
    exploit = f"printf 'X=doc-' > s.sh; {spelling} s.sh; eval \"$X\"lattice"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == "shell source payload state cannot be represented"


@pytest.mark.parametrize("spelling", ["source", "."])
def test_source_payload_suffix_fragment_fails_closed(spelling: str) -> None:
    """Round-2 review finding: the fix's first pass only detected PREFIX fragments.

    Verified under real Bash 5.2. ``_resource_marker_fragment_capable`` originally checked only
    the fresh-start DFA entry state, which finds a fragment like ``doc-`` (it advances the scan on
    its own) but structurally cannot find ``lattice`` -- scanned fresh, "lattice" never leaves
    state zero, since nothing about it is special without already having matched ``doc-`` first.
    The non-source control proves the general composition machinery already gets this right in
    both directions (it resolves ``$Y`` to the real assigned value via the ordinary AST-level
    assignment parser, not a byte scan of a file), which is what made this a genuine, in-scope gap
    rather than an inherent limit: a sourced file's bytes carry no such parse, so the fix has to
    reconstruct the isolated value itself before checking it from every entry state.
    """
    control = 'Y=lattice; eval "doc-$Y"'
    exploit = f"printf 'Y=lattice' > s.sh; {spelling} s.sh; eval \"doc-$Y\""

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == "shell source payload state cannot be represented"


@pytest.mark.parametrize("spelling", ["source", "."])
def test_marker_free_suffix_shaped_write_then_source_still_certifies(spelling: str) -> None:
    """Over-refusal guard for the round-2 suffix fix, mirroring the prefix-side guard.

    ``TAG=latest`` is suffix-shaped (a ``NAME=VALUE`` line whose value gets used as a suffix) but
    is not the marker: the DFA requires literal ``lattice`` (double ``t``, then ``ice``), not
    ``latest``, so ``doc-$TAG`` never completes it. This proves checking every DFA entry state
    for the isolated assignment value did not regress into flagging any suffix-shaped write
    whatsoever -- it still requires the actual marker text.
    """
    body = f'printf "TAG=latest" > t.sh; {spelling} t.sh; echo "doc-$TAG"'

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_source_payload_split_across_boundary_fails_closed() -> None:
    """A marker fragment can land on either side of the source boundary.

    ``echo 'X=doc' > s.sh`` writes a fragment ending in ``doc`` (echo's trailing newline is
    stripped the same way command substitution's is before this is checked); the later
    ``eval "${X}-lattice"`` supplies the rest as authored literal text. Neither this analysis nor
    a byte-for-byte read of the sourced file alone reveals the full marker, only the composition
    does -- which is exactly why the fix asks the resource's content whether it leaves a
    fresh-start DFA scan in a nonzero state, not whether the file's own text completes the match.
    """
    exploit = "echo 'X=doc' > s.sh; source s.sh; eval \"${X}-lattice\""

    result = scan_doc_lattice_invocations(exploit)

    assert result.incomplete_reason == "shell source payload state cannot be represented"


def test_marker_free_write_then_source_still_certifies() -> None:
    """Over-refusal guard: the ordinary "generate an env file, then source it" CI idiom.

    Review finding on the first pass of this fix: an unconditional "any script-written source
    target fails closed" rule refused this body even though it carries the marker nowhere --
    ``REGION=us-east-1`` has no ``d`` character at all, so it cannot contribute any "doc"/
    separator/"lattice" progress under any reading. `direct_doc_lattice_invocations` turns any
    non-``None`` `incomplete_reason` into a raised `ConfigError`, so this shape would have broken
    an entirely ordinary CI step. The fix must gate the refusal on the sourced content actually
    being able to carry a marker fragment, not merely on the target being one this script wrote.
    """
    body = 'echo "REGION=us-east-1" > env.sh; source env.sh; aws configure set region "$REGION"'

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'A=doc-; printf "%s\\n" "${A}lattice reconcile" > s.sh; cat s.sh | bash',
        'A=doc-; cat <(printf "%s\\n" "${A}lattice reconcile") | bash',
    ],
    ids=("named-operand", "process-substitution"),
)
def test_producer_stdout_links_named_operand_and_process_substitution(script: str) -> None:
    """Verified false-safes from issue #136, reproduced under real Bash 5.2.

    ``_producer_stdout`` already assumed a command may echo its stdin back out (the redirection
    form ``cat < s.sh | bash`` already refuses), but it did not extend that same may-output
    assumption to a resource a command names as an OPERAND, nor to a process substitution it
    reads. Both are the most idiomatic CI file handoffs into a shell and both used to certify
    clean while actually executing the split marker.
    """
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == ()
    assert result.incomplete_reason == "authored marker flow reaches an execution sink"


def test_producer_stdout_redirection_form_control_still_refuses() -> None:
    """Control for issue #136: the redirection form already modeled this handoff correctly."""
    control = 'A=doc-; printf "%s\\n" "${A}lattice reconcile" > s.sh; cat < s.sh | bash'

    result = scan_doc_lattice_invocations(control)

    assert result.invocations == ()
    assert result.incomplete_reason == "authored marker flow reaches an execution sink"


def test_producer_stdout_named_operand_stays_content_aware() -> None:
    """Over-refusal guard: naming a resource operand must not fail closed unconditionally.

    ``cat file | bash`` is one of the most common CI idioms there is. The fix must only treat a
    named operand as a possible stdout source when that resource actually carries a tracked
    marker-bearing write within this evidence graph -- mirroring how the redirection form already
    behaves -- not refuse every ``cat <anything> | bash`` regardless of content.
    """
    body = "cat README.md | bash"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_static_eval_programs_are_empty_for_a_dynamic_argument() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("$P", LiteralTransfer("X=doc-"), dynamic=True),
        name="eval",
    )

    assert _static_eval_programs(command) == ()


def test_eval_replayed_assignment_reaches_a_bash_c_sink() -> None:
    """Verified false-safe from issue #117, reproduced under real Bash 5.2.

    AD-18 claims an eval payload's recovered assignments are retained "so a later sink observes
    them", but that was only ever wired into the exact-literal table another eval reads --
    ``_build_flow_definitions`` never saw them, so any NON-eval sink missed the assignment
    entirely. The control shows the identical flow through a direct (non-eval) assignment already
    refuses; before this fix the eval-replayed form certified clean and executed the marker.
    """
    control = "X=doc-; bash -c \"$X\"'lattice check'"
    exploit = "eval 'X=doc-'; bash -c \"$X\"'lattice check'"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_eval_replayed_assignment_reaches_a_written_script_sink() -> None:
    """Companion false-safe for issue #117 through a written-script, not a ``bash -c``, sink.

    Isolates the same missing lowering through a different non-eval sink shape: the eval-replayed
    value composes the marker into a file this same body then executes with a plain ``bash``.
    """
    control = "X=doc-; printf '%s' \"$X\"'lattice check' > t.sh; bash t.sh"
    exploit = "eval 'X=doc-'; printf '%s' \"$X\"'lattice check' > t.sh; bash t.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_eval_replayed_assignment_control_via_eval_sink_still_refuses() -> None:
    """Control for issue #117: the eval-sink form already refused via the exact-literal table.

    This isolates the missing lowering as the cause of the false-safes above rather than the
    assignment recovery itself -- the exact table already threads this assignment to another
    eval's payload; it is only the non-eval sinks that were blind to it.
    """
    exploit = "eval 'X=doc-'; eval \"$X\"'lattice check'"

    result = scan_doc_lattice_invocations(exploit)

    assert result.incomplete_reason == "authored marker flow reaches an execution sink"


@pytest.mark.parametrize("keyword", ["declare", "typeset"])
def test_eval_replayed_top_level_declare_reaches_a_bash_c_sink(keyword: str) -> None:
    """Follow-up false-safe found in review of #117, reproduced under real Bash 5.2.

    ``bash -c 'eval "declare X=doc-"; echo "$X"'`` prints ``doc-``: outside a function,
    ``declare``/``typeset`` (like a bare assignment) create a plain GLOBAL variable, only
    shadowing the caller when used INSIDE a function. ``_static_eval_mutations`` mislabeled a
    top-level ``declare``/``typeset`` as ``local=True`` (bare ``local`` is correctly gated on
    function context two lines above, but ``declare``/``typeset`` were not), so the #117 lowering
    excluded them via its ``not mutation.local`` filter and this recipe kept certifying clean
    through a non-eval sink after that fix landed.
    """
    exploit = f"eval '{keyword} X=doc-'; bash -c \"$X\"'lattice check'"

    result = scan_doc_lattice_invocations(exploit)

    assert result.incomplete_reason == "authored marker flow reaches an execution sink"


def test_eval_replayed_in_function_declare_stays_scoped() -> None:
    """Scoping control for the declare/typeset fix above: must not over-refuse.

    ``bash -c 'f(){ eval "declare X=doc-"; }; f; echo "${X:-unset}"'`` prints ``unset``: a
    ``declare`` inside a function IS genuinely local and must not persist to the caller. This
    proves the fix is gated on ``command.function_context_id``, not a blanket "declare is always
    global" change that would itself be an unsound new false-safe.
    """
    body = "f(){ eval 'declare X=doc-'; }; f; bash -c \"$X\"'lattice check'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_eval_replayed_assignment_marker_free_still_certifies() -> None:
    """Over-refusal guard for the #117 lowering itself: a marker-free eval-replayed value must
    not flip an ordinary body to refuse.
    """
    body = "eval 'X=safe'; bash -c \"$X\"'run'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_static_eval_command_stdout_carries_authored_operand_content() -> None:
    """Issue #146: a payload command's operands are the content its redirection writes.

    Without this the write registered no content the later sink could compose against, which is
    what let ``eval 'printf X=doc- > s.sh'; source s.sh; eval "${X}lattice"`` certify.
    """
    (parsed,) = _static_eval_commands(_eval_command("printf X=doc- > s.sh"), limits=TaintLimits())

    stdout = _static_eval_command_stdout(parsed, 0, scoped=False, limits=TaintLimits())

    assert _can_mark_with_tables(concat(stdout, LiteralTransfer("lattice")))


def test_static_eval_command_stdout_of_a_marker_free_operand_cannot_mark() -> None:
    """Over-refusal guard: the ordinary generated-env-file idiom carries no fragment."""
    (parsed,) = _static_eval_commands(
        _eval_command("printf REGION=us-east-1 > env.sh"), limits=TaintLimits()
    )

    stdout = _static_eval_command_stdout(parsed, 0, scoped=False, limits=TaintLimits())

    assert not _can_mark_with_tables(concat(stdout, LiteralTransfer("lattice")))


def test_static_eval_command_stdout_resolves_an_expanded_operand() -> None:
    """The payload expands its own parameters, so the written content is not its source text."""
    (parsed,) = _static_eval_commands(_eval_command("printf X=$V > s.sh"), limits=TaintLimits())

    stdout = _static_eval_command_stdout(parsed, 0, scoped=False, limits=TaintLimits())

    assert "V" in _expression_variable_names(stdout)


def test_eval_payload_write_reaches_a_sourced_sink() -> None:
    """Verified false-safe from issue #146, reproduced under real Bash 5.2.

    The write happens inside the eval payload, so the target file was never registered in the
    resource table and the content-gated ``source`` guard took its ``key not in resources``
    escape. The control shows the identical flow through an authored redirection already
    refuses, isolating the missing lowering rather than the guard itself as the cause.
    """
    control = 'printf X=doc- > s.sh; source s.sh; eval "${X}lattice check"'
    exploit = "eval 'printf X=doc- > s.sh'; source s.sh; eval \"${X}lattice check\""

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason is not None
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_eval_payload_write_reaches_a_script_sink_without_source() -> None:
    """Companion false-safe for issue #146 through a plain ``bash`` sink, not ``source``.

    Isolates the mechanism as the missing resource write rather than anything source-specific:
    the payload writes the marker prefix and an ordinary authored append completes it.
    """
    control = "printf doc- > s.sh\nprintf 'lattice check' >> s.sh\nbash s.sh"
    exploit = "eval 'printf doc- > s.sh'\nprintf 'lattice check' >> s.sh\nbash s.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_eval_payload_write_from_a_variable_payload_reaches_a_sink() -> None:
    """The payload need not be spelled literally; an exactly resolved one behaves the same."""
    exploit = 'P=\'printf X=doc- > s.sh\'; eval "$P"; source s.sh; eval "${X}lattice check"'

    result = scan_doc_lattice_invocations(exploit)

    assert result.incomplete_reason is not None


def test_eval_payload_write_of_a_named_resource_operand_reaches_a_sink() -> None:
    """A payload command naming a resource this body writes reproduces that content on stdout.

    AD-18 already puts the ``cat s.sh`` handoff inside the modeled may-output boundary for an
    authored command; the payload route models it the same way rather than a narrower one.
    """
    body = "printf 'doc-' > s.sh\nprintf 'lattice check' >> s.sh\neval 'cat s.sh > t.sh'\nbash t.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason == "authored marker flow reaches an execution sink"


@pytest.mark.parametrize(
    "spelling",
    [">&1", "> /dev/fd/1", "> /dev/stdout"],
    ids=("descriptor-duplication", "dev-fd", "dev-stdout"),
)
def test_eval_payload_write_through_the_enclosing_eval_descriptor(spelling: str) -> None:
    """Issue #146: a payload runs with the enclosing eval's own descriptors installed.

    ``eval 'printf X=doc- >&1' > s.sh`` routes the payload's stdout into ``s.sh`` under real bash
    5.2, because the payload duplicates a descriptor the eval command itself bound. Passing only
    the enclosing scope's bindings left descriptor 1 unresolved, so the write was dropped and the
    body certified. The authored brace-group analogue ``{ printf X=doc- >&1; } > s.sh`` already
    refuses, so this closes a route the payload spelling recognized more narrowly than the
    authored one.
    """
    control = f'{{ printf X=doc- {spelling}; }} > s.sh; source s.sh; eval "${{X}}lattice check"'
    exploit = f"eval 'printf X=doc- {spelling}' > s.sh; source s.sh; eval \"${{X}}lattice check\""

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "shell source payload state cannot be represented"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_eval_payload_write_to_an_unbound_descriptor_registers_nothing() -> None:
    """Over-refusal guard: a descriptor nothing in the body binds is a Bash error, not a flow.

    AD-18 records that a descriptor no part of the body binds keeps routing nowhere, so the
    payload route must not fabricate a write for one either.
    """
    body = "eval 'printf X=doc- >&7'; source s.sh; eval \"${X}lattice check\""

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_eval_payload_write_without_a_sink_still_certifies() -> None:
    """Over-refusal guard: AD-18's sink boundary survives the lowering.

    Composing a marker into a file is not executing it. Only a body that actually reaches an
    execution sink is refused, so the write alone must stay certified.
    """
    body = "eval 'printf X=doc- > s.sh'; echo done"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_eval_payload_dead_branch_write_still_certifies() -> None:
    """Over-refusal guard: a statically dead payload branch registers no write.

    ``_static_eval_mutations`` already skips a branch whose literal status is False; the write
    lowering follows the same reachability rule rather than registering every write it can lex.
    """
    body = "eval 'if false; then printf X=doc- > s.sh; fi'; source s.sh; eval \"${X}lattice check\""

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_eval_payload_stderr_write_still_certifies() -> None:
    """Over-refusal guard: only descriptor 1 carries the producer's content.

    ``printf X=doc- 2> s.sh`` leaves ``s.sh`` empty under real bash, so registering the key with
    opaque content must not make the later ``source`` fail closed.
    """
    body = "eval 'printf X=doc- 2> s.sh'; source s.sh; eval \"${X}lattice check\""

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    ("prefix", "expected_append"),
    [("X=doc-", False), ("X+=doc-", True)],
    ids=("assign", "append"),
)
def test_eval_assignment_transfer_retains_one_dynamic_assignment(
    prefix: str, expected_append: bool
) -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg(
            f"{prefix}$V",
            Concat((LiteralTransfer(prefix), LiteralTransfer("v"))),
            dynamic=True,
        ),
        name="eval",
    )

    transfers = _eval_assignment_transfers(command)

    assert len(transfers) == 1
    assert transfers[0].assignment.name == "X"
    assert transfers[0].assignment.append is expected_append
    assert transfers[0].assignment.content == concat(LiteralTransfer("doc-"), LiteralTransfer("v"))


def test_eval_assignment_transfer_skips_a_multi_argument_command() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("X=doc-$V", Concat((LiteralTransfer("X=doc-"), LiteralTransfer("v"))), dynamic=True),
        _arg("tail"),
        name="eval",
    )

    assert _eval_assignment_transfers(command) == ()


def test_mid_cluster_short_option_o_widens_instead_of_dropping_the_sink() -> None:
    """Issue #137: ``-o``/``-O`` mid-cluster used to drop the ``-c`` payload as the sink.

    ``bash -oe pipefail -c PAYLOAD`` sets both ``errexit`` (``-e``) and ``pipefail`` (``-o
    pipefail``) under real Bash 5.2: ``-o`` always takes its value from the NEXT argv word, even
    when it is not the cluster's last character, and the remaining characters in the same word
    keep being read as ordinary short options. Before this fix, ``_select_shell_source`` only
    special-cased ``-o``/``-O`` when it was the cluster's last character, so it fell through and
    misread ``pipefail`` -- the option's own value -- as the script operand, never reaching
    ``-c``'s real payload.
    """
    control = "X=doc-; bash -c \"$X\"'lattice check'"
    exploit = "X=doc-; bash -oe pipefail -c \"$X\"'lattice check'"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


@pytest.mark.parametrize(
    ("cluster", "value"),
    [("-oe", "pipefail"), ("-eo", "pipefail"), ("-Oe", "extglob"), ("-eO", "extglob")],
    ids=("o-first", "o-last", "O-first", "O-last"),
)
def test_mid_cluster_short_option_o_or_O_any_position_refuses(cluster: str, value: str) -> None:
    """Every cluster position, and both the ``-o`` and ``-O`` spellings, hit the same fix."""
    exploit = f"X=doc-; bash {cluster} {value} -c \"$X\"'lattice check'"

    result = scan_doc_lattice_invocations(exploit)

    assert result.incomplete_reason == "authored marker flow reaches an execution sink"


def test_mid_cluster_short_option_o_marker_free_still_certifies() -> None:
    """Over-refusal guard: the widened corner must only refuse when it could carry marker flow."""
    body = "bash -oe pipefail -c 'echo hi'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "option", ["-norc", "-posix", "-noprofile", "-noediting", "-restricted", "-login"]
)
def test_single_dash_long_option_widens_instead_of_dropping_the_sink(option: str) -> None:
    """Issue #137: a single-dash GNU long option used to be decomposed as a short-option cluster.

    ``-norc``/``-posix`` and their siblings are whole-word option spellings Bash accepts with one
    leading dash, behaving exactly like ``--norc``/``--posix`` (verified under real Bash 5.2).
    Before this fix ``_select_shell_source`` had no atomic recognition for the single-dash
    spelling, so it fell into the short-option-cluster fallback and decomposed the word letter by
    letter -- ``-norc``, for example, contains a ``c``, so the loop wrongly set
    ``command_selected`` and treated the script operand that followed as a ``-c`` command string
    rather than a script, so the file's own tracked marker-bearing content was never reached as a
    sink.
    """
    control = "X=doc-; printf '%s' \"$X\"'lattice check' > t.sh; bash t.sh"
    exploit = f"X=doc-; printf '%s' \"$X\"'lattice check' > t.sh; bash {option} t.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_single_dash_long_option_marker_free_target_still_certifies() -> None:
    """Over-refusal guard: an untracked target behind a single-dash long option still certifies."""
    body = "bash -norc README.md"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_glob_script_operand_widens_instead_of_resolving_to_an_exact_name() -> None:
    """Issue #137: an unquoted glob script operand used to resolve to its own literal name.

    ``_ArgPort`` did not carry ``active_argv_expansion`` -- the same flag ``_ShellWord`` already
    computes elsewhere in this analysis for an unquoted glob, brace, or bracket expression -- so
    ``bash ta*.sh`` resolved the operand as the literal, almost certainly nonexistent, resource key
    ``"ta*.sh"`` instead of widening to the file(s) an unquoted ``*`` can expand to at run time.
    One of those, ``task.sh``, is a file this same body's own ``printf`` tracked a marker-bearing
    write to.

    A glob's exact match set is genuinely unknowable without reading the filesystem, so once
    ``_select_shell_source`` widens to ``AMBIGUOUS`` the general sink machinery alone resolves to
    ``OutsideGap()`` -- an opaque, external unknown that is not itself proof of marker composition
    and would still certify clean. ``_glob_script_operand_state_unrepresentable`` closes that gap
    the same way ``_source_payload_state_unrepresentable`` does for a sourced file: it stays narrow
    to a resource this same script writes, matched against the authored pattern.
    """
    control = "X=doc-; printf '%s' \"$X\"'lattice check' > task.sh; bash task.sh"
    exploit = "X=doc-; printf '%s' \"$X\"'lattice check' > task.sh; bash ta*.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert (
        exploit_result.incomplete_reason == "shell glob script operand state cannot be represented"
    )


def test_source_dashdash_widens_instead_of_dropping_the_sink() -> None:
    """Issue #137: ``source --``'s end-of-options marker used to be mistaken for the operand.

    ``source -- ./task.sh`` is ordinary Bash: ``--`` ends option parsing for the ``source``
    builtin and ``./task.sh`` is the file sourced (verified under real Bash 5.2). The operand
    lookup took the word immediately after the executable unconditionally, so it read ``--``
    itself as the operand; the resource lookup for the key ``"--"`` never matched the tracked
    ``task.sh`` write, so the sourced file's marker-bearing content was never reached as a sink.
    """
    control = "X=doc-; printf '%s' \"$X\"'lattice check' > task.sh; source task.sh"
    exploit = "X=doc-; printf '%s' \"$X\"'lattice check' > task.sh; source -- ./task.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


def test_source_dashdash_marker_free_target_still_certifies() -> None:
    """Over-refusal guard: an untracked target behind ``source --`` still certifies."""
    body = "source -- README.md"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_shell_option_grammar_normal_bash_c_still_certifies() -> None:
    """Brief's baseline over-refusal guard: an ordinary marker-free ``bash -c`` still certifies."""
    body = "bash -c 'echo hi'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_shell_option_grammar_exact_script_operand_still_certifies() -> None:
    """Brief's baseline over-refusal guard: an exact, untracked script operand still certifies."""
    body = "bash script.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


# Issue #150: four confirmed false-safes where an unquoted glob operand certified clean while
# real Bash executed the authored marker. Each family below pins a control that already refused
# beside the exploit spelling that slipped past, plus the over-refusal guards that must keep
# certifying so the fix stays narrow to script-tracked, marker-bearing targets.
_MARKER_WRITE = "X=doc-; printf '%s' \"$X\"'lattice check' > task.sh; "
_SHELL_GLOB_REASON = "shell glob script operand state cannot be represented"
_SOURCE_GLOB_REASON = "shell source glob operand state cannot be represented"


@pytest.mark.parametrize("head", ["source", "."], ids=("source", "dot"))
def test_source_glob_operand_fails_closed(head: str) -> None:
    """Issue #150: a glob ``source`` operand reached the tracked marker file and certified.

    ``source ta*.sh`` is ordinary Bash: the builtin's operand is expanded before the file is
    named, so the pattern resolves at run time to whatever matches in the current directory --
    here ``task.sh``, a file this same body's own ``printf`` tracked a marker-bearing write to
    (verified under real Bash 5.2). ``_source_payload_state_unrepresentable`` asked
    ``normalize_static_resource`` for an exact key with ``dynamic=`` set for exactly this case, so
    a glob operand resolved to None and the guard skipped it entirely, leaving nothing else to
    reach the sourced file's content.
    """
    control = _MARKER_WRITE + "source task.sh"
    exploit = _MARKER_WRITE + f"{head} ta*.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == _SOURCE_GLOB_REASON


def test_source_dashdash_glob_operand_fails_closed() -> None:
    """Issue #150: ``--`` ends the builtin's option parsing, so the glob still names the file."""
    body = _MARKER_WRITE + "source -- ta*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason == _SOURCE_GLOB_REASON


def test_source_dotslash_glob_operand_fails_closed() -> None:
    """Issue #150: a ``./`` prefixed pattern needs key-space normalization to match at all.

    The raw pattern ``./ta*.sh`` never ``fnmatch``es the normalized resource key ``task.sh``,
    so matching the authored spelling alone certified this even once the operand was reached.
    """
    body = _MARKER_WRITE + "source ./ta*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason == _SOURCE_GLOB_REASON


@pytest.mark.parametrize(
    "body",
    [
        _MARKER_WRITE + "source env.sh ta*.sh",
        _MARKER_WRITE + "touch env.sh; source env.sh ta*.sh",
    ],
    ids=("absent-target", "empty-target"),
)
def test_source_later_arguments_are_positional_parameters_not_targets(body: str) -> None:
    """Over-refusal guard: only the first ``source`` operand names a file.

    Every word after it becomes a positional parameter for the sourced script, never a second
    source target (verified under real Bash 5.2). A glob-operand guard that scanned later
    arguments would match ``ta*.sh`` against the tracked marker-bearing ``task.sh`` and refuse a
    body real Bash never runs the marker in.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_source_glob_marker_free_target_still_certifies() -> None:
    """Over-refusal guard: a matched but marker-free tracked target still certifies."""
    body = "printf 'REGION=us-east-1\\n' > task.sh; source ta*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_source_glob_unmatched_pattern_still_certifies() -> None:
    """Over-refusal guard: a pattern matching no tracked resource still certifies."""
    body = _MARKER_WRITE + "source zz*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_dotslash_glob_script_operand_fails_closed() -> None:
    """Issue #150: ``bash ./ta*.sh`` certified because the raw pattern never matched the key.

    ``_glob_script_operand_state_unrepresentable`` matched each tracked resource key against the
    operand's authored literal only. Resource keys are lexically normalized (``./task.sh`` is
    stored as ``task.sh``), so a pattern carrying a ``./`` prefix could not match any key no
    matter which file real Bash expanded it to.
    """
    control = _MARKER_WRITE + "bash ./task.sh"
    exploit = _MARKER_WRITE + "bash ./ta*.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == _SHELL_GLOB_REASON


def test_dotslash_glob_marker_free_target_still_certifies() -> None:
    """Over-refusal guard: pattern normalization must not refuse a marker-free tracked target.

    The content has to end with the marker scan idle to be marker-free at all. ``make build``
    does not: its trailing ``d`` is the marker's own first character, so it leaves the scan
    mid-match and the already-shipped ``bash ta*.sh`` route refuses it too.
    """
    body = "printf 'REGION=us-east-1\\n' > task.sh; bash ./ta*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "tail",
    [
        "bash -o ta* -c 'echo hi'",
        "bash -eo ta* -c 'echo hi'",
        "bash -O ta* -c 'echo hi'",
        "bash -o ta*",
        "bash --rcfile ta*.sh",
        "bash --init-file ta*.sh",
    ],
    ids=(
        "short-o",
        "mid-cluster-o",
        "short-O",
        "short-o-trailing",
        "long-rcfile",
        "long-init-file",
    ),
)
def test_option_value_glob_fails_closed(tail: str) -> None:
    """Issue #150: a glob in an option's VALUE position widened but was never inspected.

    ``_select_shell_source`` widens to ``AMBIGUOUS`` from the option word itself when the value
    that follows carries argv expansion, so ``candidate_indices[0]`` is the literal ``-o`` or
    ``--rcfile`` word rather than the glob. The guard only inspected that first candidate, found
    no ``active_argv_expansion`` on it, and certified -- even though ``--rcfile ta*.sh`` makes
    Bash read the matched file outright.
    """
    body = _MARKER_WRITE + tail

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason == _SHELL_GLOB_REASON


def test_option_value_glob_marker_free_still_certifies() -> None:
    """Over-refusal guard: an option-value glob with nothing tracked still certifies."""
    body = "bash -o ta* -c 'echo hi'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_option_value_glob_shift_reaches_script_operand() -> None:
    """Issue #150: an option-value glob can shift a later word into the operand position.

    ``bash -o p* ta*.sh`` expands ``p*`` to the tracked ``pipefail`` file, so ``-o`` consumes it
    as a valid option name and the script operand really is ``ta*.sh`` -- the tracked
    marker-bearing ``task.sh`` (verified under real Bash 5.2). Scanning every candidate word,
    rather than only the first, is what reaches it once the expansion count is unknown.
    """
    body = _MARKER_WRITE + "echo x > pipefail; bash -o p* ta*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason == _SHELL_GLOB_REASON


@pytest.mark.parametrize("launcher", ["timeout 5", "nohup"], ids=("timeout", "nohup"))
def test_launcher_glob_script_operand_fails_closed(launcher: str) -> None:
    """Issue #150: a shell reached through an unrecognized launcher skipped the glob guard.

    ``_candidate_sink_expressions`` already routes ``timeout 5 bash ...`` to the shell option
    grammar through ``_nested_shell_index``, but the glob guard only recognized a shell that was
    the command's own resolved head, so the launcher spelling of the same exploit certified.
    """
    control = _MARKER_WRITE + f"{launcher} bash task.sh"
    exploit = _MARKER_WRITE + f"{launcher} bash ta*.sh"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == _SHELL_GLOB_REASON


def test_launcher_glob_marker_free_target_still_certifies() -> None:
    """Over-refusal guard: the launcher route must not refuse a marker-free tracked target.

    ``make build`` would not serve here: its trailing ``d`` is the marker's own first character,
    so it leaves the marker scan mid-match and every glob route refuses it, this one included.
    """
    body = "printf 'REGION=us-east-1\\n' > task.sh; timeout 5 bash ta*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "head",
    ['command "$RUNNER"', 'exec "$RUNNER"', 'builtin -- "$RUNNER"'],
    ids=("command", "exec", "builtin"),
)
def test_ambiguous_glob_script_operand_route_is_not_constructible(head: str) -> None:
    """Issue #150: the ambiguous shell route's glob guard is a mirror, not a reachable exploit.

    ``_candidate_sink_expressions`` reads the shell option grammar from argv index zero when the
    head cannot be resolved to an exact name, and the glob guard mirrors that route for
    fail-closure. No run body reaches it: a command whose head is unresolved and whose operand
    carries a glob refuses earlier, at the executable-word check, as pinned here. The mirror is
    kept because the earlier check is a separate guard whose scope could narrow.
    """
    body = _MARKER_WRITE + f"{head} ta*.sh"

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason == "executable word uses brace or glob expansion"


# Issue #154: the shell ``-c`` payload second pass recognized a shell only at an executable
# candidate's own argv index, so every dispatch spelling except the direct head skipped the second
# parse and certified a body real Bash runs the marker in. Each family below pins the direct-head
# control that already refused beside the spelling that slipped past, plus the over-refusal guards
# and the selections that must stay outside the pass so the fix stays narrow to interpreted
# payloads.
_LITERAL_PAYLOAD = "A=doc-; B='lattice reconcile'; export A B; "
_MARKER_FREE_PAYLOAD = "A=safe; B=thing; export A B; "


@pytest.mark.parametrize(
    "launcher",
    ["timeout 60", "nohup", "nice -n 5", "setsid", "stdbuf -oL"],
    ids=("timeout", "nohup", "nice", "setsid", "stdbuf"),
)
def test_launcher_spelled_shell_c_payload_fails_closed(launcher: str) -> None:
    """Issue #154: a shell reached through a launcher never entered the payload second pass.

    ``_candidate_sink_expressions`` already dispatches ``timeout 60 bash ...`` to the shell option
    grammar through ``_nested_shell_index``, but ``_shell_command_payload_marker_capable`` required
    the shell name at an executable candidate's own argv index, so the launcher spelling skipped
    the second parse. The child shell expands the single-quoted payload itself, so the marker is
    composed although no word of the parent command carries it (verified under real Bash 5.2).
    """
    control = _LITERAL_PAYLOAD + "bash -c '$A$B'"
    exploit = _LITERAL_PAYLOAD + f"{launcher} bash -c '$A$B'"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


@pytest.mark.parametrize(
    "head",
    ["S=bash; $S", '"$SHELL"', "${SHELL}"],
    ids=("assigned-head", "quoted-environment-head", "braced-environment-head"),
)
def test_dynamic_head_shell_c_payload_fails_closed(head: str) -> None:
    """Issue #154: an unresolved head reaches the option grammar but skipped the second parse.

    ``_shell_source_head_index`` reads the grammar from argv index zero when the head cannot be
    resolved to an exact name, which selects the ``-c`` payload here. The expansion direction of
    the same body already refused through the ordinary sink path, which isolates the gap to the
    literal-payload second pass rather than to ambiguous head handling in general.
    """
    control = _LITERAL_PAYLOAD + "bash -c '$A$B'"
    exploit = _LITERAL_PAYLOAD + f"{head} -c '$A$B'"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


@pytest.mark.parametrize("wrapper", ["command", "exec"], ids=("command", "exec"))
def test_wrapped_dynamic_head_shell_c_payload_fails_closed(wrapper: str) -> None:
    """Issue #154: a wrapped dynamic head yields an ambiguous selection, not an exact one.

    ``_select_shell_source`` retains every remaining candidate once the argv shape is uncertain,
    so these bodies select ``_ShellSourceKind.AMBIGUOUS`` rather than ``COMMAND``. A gate that
    accepted only the exact ``COMMAND`` selection still skipped them, which is why the pass scans
    every candidate word the same way ``_shell_source_sinks`` builds a choice over all of them.
    """
    control = _LITERAL_PAYLOAD + "bash -c '$A$B'"
    exploit = _LITERAL_PAYLOAD + f"S=bash; {wrapper} \"$S\" -c '$A$B'"

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


@pytest.mark.parametrize(
    "tail",
    [
        "OPT=pipefail; bash -o \"$OPT\" -c '$A$B'",
        "RC=rc.sh; bash --rcfile \"$RC\" -c '$A$B'",
        "OPT=pipefail; timeout 60 bash -o \"$OPT\" -c '$A$B'",
    ],
    ids=("short-o", "long-rcfile", "launcher-short-o"),
)
def test_dynamic_shell_option_value_shell_c_payload_fails_closed(tail: str) -> None:
    """Issue #154: a dynamic option value widens the selection past the exact ``COMMAND`` kind.

    The head here is an ordinary literal ``bash``, so the direct route was never the problem: the
    dynamic value of ``-o`` or ``--rcfile`` makes ``_select_shell_source`` return an ambiguous
    candidate set, which the exact-only gate dropped. The ``-c`` payload is one of those
    candidates.
    """
    control = _LITERAL_PAYLOAD + "bash -c '$A$B'"
    exploit = _LITERAL_PAYLOAD + tail

    control_result = scan_doc_lattice_invocations(control)
    exploit_result = scan_doc_lattice_invocations(exploit)

    assert control_result.incomplete_reason == "authored marker flow reaches an execution sink"
    assert exploit_result.incomplete_reason == control_result.incomplete_reason


@pytest.mark.parametrize(
    "tail",
    [
        "timeout 60 bash -c '$A$B'",
        "nohup bash -c '$A$B'",
        "S=bash; $S -c '$A$B'",
        "S=bash; command \"$S\" -c '$A$B'",
        "OPT=pipefail; bash -o \"$OPT\" -c '$A$B'",
    ],
    ids=("launcher", "nohup", "dynamic-head", "wrapped-dynamic-head", "dynamic-option-value"),
)
def test_widened_payload_routes_keep_marker_free_bodies_certified(tail: str) -> None:
    """Over-refusal guard: every widened route must still certify a marker-free payload."""
    body = _MARKER_FREE_PAYLOAD + tail

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_script_operand_selection_stays_out_of_the_payload_second_pass() -> None:
    """Issue #154: a script operand names a file, so it is the sink path's business, not this pass.

    ``timeout 60 bash '$A$B'`` reaches the option grammar through the launcher route and selects
    ``_ShellSourceKind.SCRIPT``: the operand is a filename spelled ``$A$B``, which real Bash
    reports as missing rather than expanding (verified under real Bash 5.2, the shim never runs).
    Second-parsing that text as shell source would compose the marker and refuse, so the exclusion
    is what keeps this certified. A script's own content already reaches the sink path through
    ``_shell_script_source_expression``.
    """
    body = _LITERAL_PAYLOAD + "timeout 60 bash '$A$B'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_source_pre_shell_exclusion_survives_the_widened_payload_routes() -> None:
    """Issue #154: only the first ``source`` operand names a file; later words are positional.

    ``_shell_source_head_index`` returns None for a ``source`` head before any launcher search
    runs, and that early return is load-bearing here: without it the search would find ``bash`` at
    the second word and second-parse ``'$A$B'`` as a shell payload, when both words are only
    positional parameters for the sourced script. Real Bash runs nothing (verified under 5.2).
    """
    body = _LITERAL_PAYLOAD + "touch env.sh; source env.sh bash -c '$A$B'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_builtin_wrapped_shell_is_not_a_payload_route() -> None:
    """Issue #154: ``builtin`` cannot reach a shell, so it is not a route this pass must cover.

    Verified under real Bash 5.2: ``builtin bash -c ...`` fails with ``builtin: bash: not a shell
    builtin`` and executes nothing, so certifying it is correct rather than a gap. The ``command``
    and ``exec`` spellings of the same shape do run, which is why they refuse and this one does
    not.
    """
    body = _LITERAL_PAYLOAD + "S=bash; builtin -- \"$S\" -c '$A$B'"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


# The ``-c`` payload second pass expanded the payload only against the parent's environment. It
# therefore missed both of the ways a child shell's own state differs from the parent's: the
# assignments the payload performs before the rest of it expands, and the operands after the
# payload word, which the child binds as ``$0`` onward. Every refusing body below was verified
# under real Bash 5.2 with a ``doc-lattice`` shim on ``PATH``.
@pytest.mark.parametrize(
    "body",
    [
        """bash -c 'A=doc-; "$A"lattice reconcile'""",
        """sh -c 'A=doc-; "$A"lattice reconcile'""",
        """timeout 5 bash -c 'A=doc-; "$A"lattice reconcile'""",
        """bash -c 'A=doc-; B=lattice; "$A$B" reconcile'""",
        """bash -c 'export A=doc-; "$A"lattice reconcile'""",
        """bash -c 'declare A=doc-; "$A"lattice reconcile'""",
        """bash -c 'A=doc; A+=-; "$A"lattice reconcile'""",
    ],
    ids=(
        "bash",
        "sh",
        "launcher",
        "two-fragments",
        "export",
        "declare",
        "append",
    ),
)
def test_shell_c_payload_replays_its_own_assignments(body: str) -> None:
    """A payload's own assignments run before the rest of the payload expands."""
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        """bash -c '$0$1 reconcile' doc- lattice""",
        """bash -c '$1$2 reconcile' sh doc- lattice""",
        """bash -c '${0}${1} reconcile' doc- lattice""",
        """timeout 5 bash -c '$0$1 reconcile' doc- lattice""",
    ],
    ids=("dollar-zero", "later-positionals", "braced", "launcher"),
)
def test_shell_c_payload_binds_trailing_operands_as_positionals(body: str) -> None:
    """Operands after a ``-c`` payload become the child's ``$0`` onward, not the parent's."""
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        """bash -c 'A=safe; "$A" reconcile'""",
        """bash -c 'A=doc-; "$A"umentation reconcile'""",
        """bash -c '$1 reconcile'""",
        """bash -c 'echo "$0"' doc- lattice""",
        """bash -c 'A=doc-; echo "$A"' lattice""",
    ],
    ids=(
        "inert-assignment",
        "non-composing-assignment",
        "unbound-positional",
        "operands-not-composed",
        "assignment-not-composed",
    ),
)
def test_shell_c_payload_child_state_keeps_marker_free_bodies_certified(body: str) -> None:
    """Neither the payload's assignments nor its operands may refuse a marker-free child."""
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize("wrapper", ["command", "exec"], ids=("command", "exec"))
def test_wrapper_with_a_dynamic_head_and_dynamic_payload_fails_closed(wrapper: str) -> None:
    """Issue #157: an unresolved head is now read as unknown rather than as nothing.

    This body has no nameable word after the wrapper, so the resolver returns no executable at
    all and ``_candidate_sink_expressions`` used to stop at its first guard, leaving the command
    with no sink to widen. ``_unresolved_head_sinks`` now routes exactly that case through the
    same entry the ambiguous selection uses, so the option grammar is read from argv index zero
    and the ``-c`` payload becomes a sink. Verified under real Bash 5.2: this executes the marker.
    """
    body = _LITERAL_PAYLOAD + f'S=bash; {wrapper} "$S" -c "$A$B"'

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "head",
    ['exec -a sh "$S"', 'command -- "$S"'],
    ids=("exec-dash-a", "command-dash-dash"),
)
def test_wrapper_option_grammar_shifts_the_ambiguous_selection_is_a_known_gap(head: str) -> None:
    """Issue #158: the ambiguous route reads the shell option grammar across the wrapper's options.

    Anchoring at argv index zero makes ``_select_shell_source`` walk ``exec``'s ``-a`` value and
    ``command``'s ``--`` as if they were the shell's own grammar, so it returns a ``SCRIPT``
    selection pointing at a wrapper operand and never reaches the ``-c`` payload. Their
    literal-head counterparts refuse, which isolates this to the dynamic head. Verified under real
    Bash 5.2: these certify and execute the marker.
    """
    body = _LITERAL_PAYLOAD + f"S=bash; {head} -c '$A$B'"

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        """printf '%s%s\\n' doc- lattice | { read A; eval '$A reconcile'; }""",
        """printf '%s%s %s\\n' doc- lattice reconcile | { read A B; eval '$A $B'; }""",
        """printf '%s%s\\n' doc- lattice > s.txt; A=$(cat s.txt); eval '$A reconcile'""",
        """printf '%s%s %s\\n' doc- lattice reconcile | eval "$(cat)\"""",
        """printf '%s%s %s\\n' doc- lattice reconcile | { V=$(cat); eval "$V"; }""",
    ],
    ids=("read-one", "read-two", "resource", "stdin-substitution", "assigned-substitution"),
)
def test_eval_layer_drops_resource_and_stream_content_is_a_known_gap(body: str) -> None:
    """Issue #159: the eval layer resolves content against empty resource and stream tables.

    ``evaluate_assignment`` and ``_finalize_eval_syntax`` both pass ``{}`` for the resource and
    stream tables, so a value arriving at an eval sink as a ``ResourceRef`` or ``StreamRef``
    becomes an outside gap. Verified under real Bash 5.2: these certify and execute the marker.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        """printf '%s%s\\n' doc- lattice | { read A; "$A" reconcile; }""",
        """printf '%s%s\\n' doc- lattice > s.txt; A=$(cat s.txt); "$A" reconcile""",
        """printf '%s%s %s\\n' doc- lattice reconcile | bash -c "$(cat)\"""",
        """read A B <<< 'doc-lattice reconcile'; eval '$A $B'""",
        """A=doc-; B=lattice; eval '$A$B reconcile'""",
    ],
    ids=(
        "stream-to-head",
        "resource-to-head",
        "stream-to-shell-payload",
        "literal-to-eval",
        "literal-value-to-eval",
    ),
)
def test_eval_layer_gap_is_confined_to_resource_and_stream_carriers(body: str) -> None:
    """Issue #159 controls: the same content refuses through every other route.

    A literal value reaching an eval sink refuses, so the eval layer's variable handling is sound
    and only its resource and stream tables are missing. The ``bash -c`` control shows the pipe
    content does reach a nested command substitution, which rules out a missing-stdin reading.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


def test_eval_second_parse_misses_function_positionals_is_a_known_gap() -> None:
    """Issue #160: a function's call arguments never enter the tables the second parse reads.

    They are applied by substitution over flow effect expressions rather than stored as variables,
    so an ``eval`` payload inside a function reads ``$1`` as absent. The ``-c`` payload sibling is
    fixed here because its positionals come from argv rather than from a call site. Verified under
    real Bash 5.2: this certifies and executes the marker.
    """
    body = """f() { eval '$1$2 reconcile'; }; f doc- lattice"""

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        """f() { "$1$2" reconcile; }; f doc- lattice""",
        """f() { eval "$1$2 reconcile"; }; f doc- lattice""",
    ],
    ids=("ordinary-sink", "expanded-before-eval"),
)
def test_function_positional_gap_is_confined_to_the_second_parse(body: str) -> None:
    """Issue #160 controls: the flow machinery does bind these call arguments."""
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


# A command whose every argv word is fully dynamic resolved to no executable at all, so
# ``_candidate_sink_expressions`` stopped at its first guard and the command contributed no sink.
# That made the issue #131 head-sink rule depend on argument shape rather than on the head, and it
# certified the shortest evasion the scanner has: two assignments and one command. Every body below
# was verified under real Bash 5.2 with a ``doc-lattice`` shim on ``PATH``.
@pytest.mark.parametrize(
    "body",
    [
        'A=doc-; B=lattice; "$A$B"',
        "A=doc-; B=lattice; $A$B",
        'A=doc-; B=lattice; C="$A$B"; "$C"',
        'A=doc-; B=lattice; "$A$B"; echo done',
        'A=doc-; B=lattice; ( "$A$B" )',
        'A=doc-; B=lattice; "$A$B" > /dev/null',
        'A=doc-; B=lattice; V=1 "$A$B"',
        'A=doc-; B=lattice; "$A$B" | cat',
        'A=doc-; B=lattice; if "$A$B"; then :; fi',
        'read A <<< doc-lattice; "$A"',
        "A=$(printf '%s%s' doc- lattice); \"$A\"",
    ],
    ids=(
        "quoted-concat",
        "unquoted-concat",
        "through-a-third-variable",
        "followed-by-another-command",
        "inside-a-subshell",
        "with-a-redirection",
        "behind-an-assignment-prefix",
        "as-a-pipeline-producer",
        "as-an-if-condition",
        "from-a-herestring-read",
        "from-a-command-substitution",
    ),
)
def test_a_fully_dynamic_command_head_is_a_sink(body: str) -> None:
    """An unresolved head is unknown, not inert, so it is a sink on the same footing as argv."""
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        'A=doc-; B=lattice; "$A" "$B"',
        'A=safe; B=thing; "$A$B"',
        "A=doc-",
        'A=doc-; B=umentation; "$A$B"',
    ],
    ids=("separate-words", "marker-free", "assignment-only", "non-composing"),
)
def test_a_fully_dynamic_command_head_keeps_marker_free_bodies_certified(body: str) -> None:
    """Widening the unresolved head must not refuse a body that composes no marker.

    ``"$A" "$B"`` is the load-bearing one: Bash runs ``doc-`` with ``lattice`` as an argument and
    never names the marker, so it has to stay certified even though both words are dynamic and
    their concatenation would compose it. A command with no argv word at all runs nothing.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


# Codex review round on PR #112. Every body below was verified under real Bash 5.2 with a
# ``doc-lattice`` shim on ``PATH``, paired with the matched control recorded beside it.
@pytest.mark.parametrize(
    "body",
    [
        """printf '%s\\n' 'A=doc-' '"${A}lattice" reconcile' > env.sh; bash env.sh""",
        """printf '%s\\n' 'A=doc-' 'eval "${A}lattice"' > env.sh; bash env.sh""",
        """printf '%s\\n' 'A=doc-' '"${A}lattice" reconcile' > env.sh; bash ./env.sh""",
        """printf '%s\\n' 'A=doc-' '"${A}lattice" reconcile' > env.sh; timeout 5 bash env.sh""",
    ],
    ids=("plain", "eval-inside", "dot-slash-spelling", "behind-a-launcher"),
)
def test_shell_script_operand_state_fails_closed(body: str) -> None:
    """The exact script operand of a shell had no state-effects guard at all.

    ``_source_payload_state_unrepresentable`` makes this argument for ``source``/``.`` and
    ``_glob_script_operand_state_unrepresentable`` makes it for a glob operand, so ``source env.sh``
    and ``bash e*.sh`` both refused the identical file while ``bash env.sh`` certified and executed
    the marker. The file's content does reach the sink as a VALUE, which is why content that is
    already the whole marker refuses; what had no model is the file's own state effects, so content
    that only COMPOSES the marker once the child shell runs it was dropped.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == "shell script operand state cannot be represented"


@pytest.mark.parametrize("spelling", ["--rcfile", "--init-file", "-rcfile", "-init-file"])
def test_shell_init_file_option_value_fails_closed(spelling: str) -> None:
    """Codex finding: an ``--rcfile``/``--init-file`` value was skipped as an inert option word.

    Bash reads that file before the ``-c`` payload, so ``bash --rcfile env.sh -ic :`` executed the
    marker ``env.sh`` composes while the scanner certified. The option grammar consumed the value
    and moved on, so no sink was ever built for it. The single-dash spellings are the ones Bash
    accepts as compatibility forms and are recognized atomically for the same reason ``-norc`` is.
    """
    body = (
        """printf '%s\\n' 'A=doc-' 'eval "${A}lattice"' > env.sh; """
        f"""bash {spelling} env.sh -ic :"""
    )

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == "shell script operand state cannot be represented"


def test_shell_init_file_option_value_reaches_the_value_sink() -> None:
    """An init file whose content is already the whole marker refuses through the sink path.

    This is the companion route to the state guard above: the value the option names now
    contributes a script-source expression, so it no longer needs the state guard to be seen.
    """
    body = """printf '%s%s\\n' doc- lattice > env.sh; bash --rcfile env.sh -ic :"""

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        """echo 'make build' > run.sh; bash run.sh""",
        """printf '%s\\n' 'echo hello' > run.sh; bash run.sh""",
        """printf 'REGION=us-east-1\\n' > env.sh; bash env.sh""",
        """printf 'TAG=latest\\n' > env.sh; bash --rcfile env.sh -ic :""",
    ],
    ids=("make-build", "echo-hello", "region-assignment", "tag-assignment"),
)
def test_shell_script_operand_keeps_ordinary_generated_scripts_certified(body: str) -> None:
    """Over-refusal guard: the content test for a child-run file is assignments-only.

    The ``source`` guard also asks whether the file's raw bytes could continue a partial match that
    began OUTSIDE the file, which is right when the file's state merges into the current shell.
    Ordinary script text answers that yes -- ``make build`` ends in ``d`` and so advances the scan
    from the idle entry state -- and applying it here refused a mandatory certification row. A child
    shell's variables never return to this body, so only the file's own assignments matter.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        "bash -s -- doc- lattice <<'EOF'\n$1$2 reconcile\nEOF",
        "bash -s doc- lattice <<'EOF'\n$1$2 reconcile\nEOF",
        """bash -s -- doc- lattice <<< '$1$2 reconcile'""",
        "timeout 5 bash -s -- doc- lattice <<'EOF'\n$1$2 reconcile\nEOF",
        "export A=doc-; export B=lattice; bash -s <<'EOF'\n$A$B reconcile\nEOF",
    ],
    ids=(
        "heredoc-with-dash-dash",
        "heredoc-without-dash-dash",
        "herestring",
        "behind-a-launcher",
        "exported-parent-variables",
    ),
)
def test_shell_stdin_program_binds_child_positionals(body: str) -> None:
    """Codex finding: a stdin program was returned raw, with no second parse and no positionals.

    ``bash -s -- doc- lattice`` binds the trailing words as ``$1``, ``$2``, ... , so the child
    composes the marker out of text that is plain argv in the parent -- the same flow the ``-c``
    operand binding closes, one dispatch form over. Verified under real Bash 5.2.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


def test_shell_stdin_positionals_start_at_one_not_zero() -> None:
    """``-s`` leaves ``$0`` as the shell's own name, unlike ``-c``, which binds it from argv.

    Binding a ``-s`` operand list from ``$0`` would shift every parameter by one, so this body has
    to stay certified: Bash 5.2 runs ``$0`` as ``bash`` and never names the marker.
    """
    body = "bash -s -- doc- lattice <<'EOF'\n$0 reconcile\nEOF"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        "bash -s -- saf e <<'EOF'\n$1$2 reconcile\nEOF",
        "bash -s -- doc- lattice <<'EOF'\necho hello\nEOF",
        """printf '%s\\n' 'echo hello' | bash -s""",
    ],
    ids=("inert-operands", "no-positional-reference", "ordinary-piped-script"),
)
def test_shell_stdin_program_keeps_marker_free_bodies_certified(body: str) -> None:
    """Over-refusal guard for the stdin second parse and the ordinary pipe-into-shell idiom."""
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        """printf '%s\\n' '$1$2 reconcile' | bash -s -- doc- lattice""",
        """printf '%s\\n' '$1$2 reconcile' > p.sh; bash -s -- doc- lattice < p.sh""",
        """export A=doc-; export B=lattice; printf '%s\\n' '$A$B reconcile' | bash -s""",
    ],
    ids=("pipe-carrier", "file-carrier", "pipe-with-exported-variables"),
)
def test_shell_stdin_program_from_a_ref_carrier_is_a_known_gap(body: str) -> None:
    """Issue #159 again, reached by the stdin route rather than by ``eval``.

    The positional binding above is carrier-independent, but the second parse can only read a
    program it can see as text. ``_eval_syntax_append`` folds ``ResourceRef`` and ``StreamRef`` into
    the same opaque token it uses for ``OutsideGap``, so a program arriving through a pipe or a
    redirection loses its content before any binding can apply. The heredoc and herestring
    spellings of the identical flow refuse, which isolates this to the carrier rather than to the
    binding. Verified under real Bash 5.2: these certify and execute the marker.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        """A=doc-; trap '${A}lattice reconcile' EXIT""",
        """A=doc-; trap -- '${A}lattice reconcile' EXIT""",
        """trap 'A=doc-; "$A"lattice reconcile' EXIT""",
        """f() { A=doc-; trap '${A}lattice reconcile' EXIT; }; f""",
        """A=doc-; B=lattice; trap "$A$B reconcile" EXIT""",
    ],
    ids=(
        "deferred-expansion",
        "after-dash-dash",
        "action-assigns-its-own-fragment",
        "registered-inside-a-function",
        "value-route",
    ),
)
def test_trap_action_is_an_interpreted_sink(body: str) -> None:
    """Codex finding: no sink was dispatched for the literal ``trap`` builtin.

    ``help trap`` specifies that the action is read and executed when the signal arrives, and that
    an ``EXIT`` action runs on shell exit. Deferring expansion to that moment is what hid it: as a
    VALUE the single-quoted word is the inert text ``${A}lattice``, so it needs the same second
    parse an ``eval`` payload gets. Verified under real Bash 5.2.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        """A=doc-; trap - EXIT""",
        """A=doc-; trap -p EXIT""",
        """trap -l""",
        """A=doc-; trap '' EXIT""",
        """A=safe; trap '${A}thing reconcile' EXIT""",
        """trap 'rm -f /tmp/build.lock' EXIT""",
    ],
    ids=(
        "reset-to-default",
        "print-disposition",
        "list-signals",
        "ignore-signal",
        "inert-action",
        "ordinary-cleanup",
    ),
)
def test_trap_registrations_that_run_nothing_stay_certified(body: str) -> None:
    """Over-refusal guard: three spellings register no action, and an inert one composes nothing.

    ``-l`` and ``-p`` make the builtin print rather than register, a literal ``-`` restores the
    default disposition, and an empty action ignores the signal.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


# Codex review round 3. Every word after a shell's script operand becomes the child's ``$1``,
# ``$2``, ... , so a tracked script that reads them composes out of text the CALLER supplies. The
# assignments-only content test cannot see it, because the composing file need contain no
# assignment at all. Each case below pins a control that already refused beside the spelling that
# slipped past, plus the over-refusal guards that keep the conjunction narrow.
_POSITIONAL_SCRIPT = "printf '%s\\n' '\"$1$2\" reconcile' > s.sh; "
_POSITIONAL_REASON = "shell script positional state cannot be represented"


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_SCRIPT + "bash s.sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash ./s.sh doc- lattice",
        _POSITIONAL_SCRIPT + "sh s.sh doc- lattice",
        _POSITIONAL_SCRIPT + "timeout 60 bash s.sh doc- lattice",
        "printf '%s\\n' '\"${1}${2}\" reconcile' > s.sh; bash s.sh doc- lattice",
        "printf '%s\\n' '\"$*\" reconcile' > s.sh; bash s.sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash s.sh lattice doc-",
    ],
    ids=(
        "exact-operand",
        "dot-slash-operand",
        "sh-head",
        "behind-a-launcher",
        "braced-positionals",
        "star-expansion",
        "arguments-given-in-the-other-order",
    ),
)
def test_shell_script_positional_arguments_fail_closed(body: str) -> None:
    """Codex finding: a tracked script composing the marker from its arguments certified.

    ``printf '%s\\n' '"$1$2" reconcile' > s.sh; bash s.sh doc- lattice`` executes the marker under
    real Bash 5.2 with a ``doc-lattice`` shim on ``PATH``, while nothing in the file is an
    assignment for ``_shell_script_operand_state_unrepresentable`` to read. The ``-c`` spelling of
    the identical composition already refuses through the operand binding that route received.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _POSITIONAL_REASON


def test_shell_script_positional_c_spelling_control_still_refuses() -> None:
    """The control that isolates the dispatch form rather than the composition itself."""
    result = scan_doc_lattice_invocations("""bash -c '"$1$2" reconcile' sh doc- lattice""")

    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_SCRIPT + "bash s.sh",
        _POSITIONAL_SCRIPT + "bash s.sh --verbose",
        _POSITIONAL_SCRIPT + "bash s.sh build",
        _POSITIONAL_SCRIPT + "bash s.sh make build",
        "printf '%s\\n' 'echo hi' > s.sh; bash s.sh doc- lattice",
        "printf '%s\\n' '\"$0\" reconcile' > s.sh; bash s.sh doc- lattice",
        "bash s.sh doc- lattice",
    ],
    ids=(
        "no-arguments",
        "ordinary-flag",
        "ordinary-word",
        "ordinary-word-pair",
        "file-ignores-its-arguments",
        "dollar-zero-is-the-script-path",
        "operand-names-no-tracked-file",
    ),
)
def test_shell_script_positional_over_refusal_guards(body: str) -> None:
    """Over-refusal guards: all three conditions must hold together before this refuses.

    A file that ignores its arguments cannot compose out of them, ordinary arguments cannot carry
    marker text, an unwritten target was never in this analysis's purview, and a script's ``$0`` is
    its own path rather than a word the caller chose.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


# Issue #175. The positional-composition test above was written into the exact script operand's own
# guard, while the assignment content test it sits beside is applied at every route in the same
# family. The same tracked file therefore certified on all six other spellings of the same read.
# Every body below executes the marker under real Bash 5.2 with a ``doc-lattice`` shim on ``PATH``,
# confirmed with the differential oracle in ``scripts/fuzz_shell_taint.py``.
_POSITIONAL_INERT_SCRIPT = "printf '%s\\n' 'run \"$1\"' > s.sh; "
_POSITIONAL_ZERO_SCRIPT = "printf '%s\\n' '\"$0\" reconcile' > s.sh; "
_POSITIONAL_ZERO_ONE_SCRIPT = "printf '%s\\n' '\"$0$1\" reconcile' > s.sh; "


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_SCRIPT + "source s.sh doc- lattice",
        _POSITIONAL_SCRIPT + ". ./s.sh doc- lattice",
        _POSITIONAL_SCRIPT + "source -- s.sh doc- lattice",
        _POSITIONAL_SCRIPT + 'F=s.sh; source "$F" doc- lattice',
        _POSITIONAL_SCRIPT + "source s*.sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash s*.sh doc- lattice",
        _POSITIONAL_SCRIPT + 'F=s.sh; bash "$F" doc- lattice',
        _POSITIONAL_SCRIPT + "bash --rcfile s.sh -ic : sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash --init-file s.sh -ic : sh doc- lattice",
        _POSITIONAL_SCRIPT + "timeout 5 bash --rcfile s.sh -ic : sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash --rcfile s.sh -i t.sh doc- lattice",
        _POSITIONAL_SCRIPT + "export BASH_ENV=s.sh; bash -c : sh doc- lattice",
        _POSITIONAL_SCRIPT + "BASH_ENV=s.sh bash -c : sh doc- lattice",
        _POSITIONAL_SCRIPT + "export BASH_ENV=s.sh; bash -s doc- lattice </dev/null",
        _POSITIONAL_SCRIPT + "export BASH_ENV=s.sh; bash $FLAGS -c : sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash --rcfile s.sh $FLAGS -ic : sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash --rcfile s.sh -i q*.txt doc- lattice",
    ],
    ids=(
        "source-builtin",
        "dot-builtin",
        "source-past-end-of-options",
        "source-operand-through-a-variable",
        "source-glob-operand",
        "glob-script-operand",
        "variable-script-operand",
        "rcfile-startup-file",
        "init-file-startup-file",
        "rcfile-behind-a-launcher",
        "rcfile-beside-a-script-dispatch",
        "bash-env-exported",
        "bash-env-prefix-assignment",
        "bash-env-stdin-dispatch",
        "bash-env-under-an-ambiguous-option-word",
        "rcfile-under-an-ambiguous-option-word",
        "rcfile-under-an-ambiguous-glob-operand",
    ),
)
def test_shell_source_positional_routes_fail_closed(body: str) -> None:
    """Issue #175: the positional test now covers every route its sibling content test covers.

    Each spelling names the SAME tracked file with the SAME arguments as the exact script operand
    control, which already refused, so the route rather than the file, the composition or the sink
    is what these isolate. ``source`` and ``.`` take the words after the operand as the sourced
    script's positionals; a glob or variable operand resolves through the same widened match the
    content half already uses; and a ``--rcfile``/``--init-file`` value or a ``BASH_ENV`` target is
    read by a child whose positionals the selected program's operands already bound.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _POSITIONAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_INERT_SCRIPT + "source s.sh build",
        _POSITIONAL_INERT_SCRIPT + ". ./s.sh build",
        _POSITIONAL_INERT_SCRIPT + 'F=s.sh; source "$F" build',
        _POSITIONAL_INERT_SCRIPT + "source s*.sh build",
        _POSITIONAL_INERT_SCRIPT + "bash s*.sh build",
        _POSITIONAL_INERT_SCRIPT + 'F=s.sh; bash "$F" build',
        _POSITIONAL_INERT_SCRIPT + "bash --rcfile s.sh -ic : sh build",
        _POSITIONAL_INERT_SCRIPT + "export BASH_ENV=s.sh; bash -c : sh build",
        _POSITIONAL_SCRIPT + "source s.sh",
        _POSITIONAL_SCRIPT + "bash --rcfile s.sh -ic :",
        _POSITIONAL_SCRIPT + "export BASH_ENV=s.sh; bash -c :",
        "source /etc/profile.d/ci.sh doc- lattice",
        _POSITIONAL_SCRIPT + "bash --rcfile s.sh --version",
        _POSITIONAL_SCRIPT + "export BASH_ENV=s.sh; bash --version",
        _POSITIONAL_SCRIPT + "source --",
    ],
    ids=(
        "source-ordinary-argument",
        "dot-ordinary-argument",
        "source-operand-through-a-variable",
        "source-glob-operand",
        "glob-script-operand",
        "variable-script-operand",
        "rcfile-ordinary-argument",
        "bash-env-ordinary-argument",
        "source-without-arguments",
        "rcfile-without-arguments",
        "bash-env-without-arguments",
        "source-target-this-body-never-writes",
        "rcfile-beside-an-eager-stop-option",
        "bash-env-beside-an-eager-stop-option",
        "source-names-no-operand-at-all",
    ),
)
def test_shell_source_positional_route_over_refusal_guards(body: str) -> None:
    """The widened routes keep all three conditions, so the ordinary spellings still certify.

    ``build`` ends in ``d`` and so advances the scan from the idle entry state, which is exactly
    the trap a marker-FRAGMENT question over the arguments falls into; substituting instead reads
    where the file actually places the argument. Sourcing a helper with arguments is an ordinary CI
    idiom, so this direction is what keeps the ``source`` route's cost at zero.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_ZERO_ONE_SCRIPT + "export BASH_ENV=s.sh; bash -c : doc- lattice",
        _POSITIONAL_ZERO_ONE_SCRIPT + "bash --rcfile s.sh -ic : doc- lattice",
    ],
    ids=("bash-env-command-form", "rcfile-command-form"),
)
def test_startup_file_dollar_zero_is_caller_supplied_for_the_command_form(body: str) -> None:
    """A ``-c`` child's ``$0`` is the first operand, so a startup file's ``$0`` is caller supplied.

    Verified under real Bash 5.2: a ``BASH_ENV`` file spelling ``"$0" reconcile`` runs the marker
    for ``bash -c : doc-lattice`` and does not for ``bash -s doc-lattice``. Substituting ``$1``
    alone leaves ``"$0lattice" reconcile``, which composes nothing, so excluding ``$0`` on this
    form would certify a body Bash runs the marker in.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _POSITIONAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_ZERO_SCRIPT + "source s.sh doc- lattice",
        _POSITIONAL_ZERO_SCRIPT + ". ./s.sh doc- lattice",
        _POSITIONAL_ZERO_SCRIPT + "export BASH_ENV=s.sh; bash -s doc- lattice </dev/null",
        _POSITIONAL_ZERO_SCRIPT + "bash s.sh doc- lattice",
    ],
    ids=("source-builtin", "dot-builtin", "bash-env-stdin-form", "script-operand"),
)
def test_startup_file_dollar_zero_excluded_where_the_caller_never_chose_it(body: str) -> None:
    """The other three dispatch forms leave ``$0`` outside the caller's reach, so it must not fire.

    ``source`` leaves the current shell's ``$0`` untouched, ``bash -s`` starts its operands at
    ``$1`` with ``$0`` left as the shell's own name, and a script operand's ``$0`` is its own path.
    Real Bash runs no marker in any of these, and each would refuse if ``$0`` counted everywhere.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


# Codex review round 3. A launcher appends words to the command it runs, taken from its own
# standard input (``xargs --help``: "Run COMMAND with arguments INITIAL-ARGS and more arguments
# read from input"). Those words are the child shell's positional parameters and no argv position
# in the body spells them, so every operand binding this analysis performs sees an empty list.
_LAUNCHER_REASON = "launcher-fed shell positional state cannot be represented"


@pytest.mark.parametrize(
    "body",
    [
        "printf '%s%s\\n' doc- lattice | xargs -n1 sh -c '$0 reconcile'",
        "printf '%s\\n' doc- lattice | xargs -n2 sh -c '$0$1 reconcile'",
        "printf '%s\\n' doc- lattice | xargs sh -c '$0$1 reconcile'",
        "printf '%s\\n' lattice doc- | xargs -n2 sh -c '$0$1 reconcile'",
        "printf 'doc-\\n' | xargs -n1 sh -c '${0}lattice reconcile'",
        "printf '%s%s\\n' doc- lattice | xargs -n1 timeout 5 sh -c '$0 reconcile'",
        (
            "printf '%s\\n' '\"$1$2\" reconcile' > s.sh; "
            "printf '%s\\n' doc- lattice | xargs bash s.sh"
        ),
        (
            "printf '%s\\n' '\"$1$2\" reconcile' > s.sh; "
            "printf '%s\\n' doc- lattice | xargs bash s*.sh"
        ),
    ],
    ids=(
        "one-word-at-dollar-zero",
        "two-words-split-across-records",
        "no-arity-flag",
        "input-words-in-the-other-order",
        "input-supplies-only-the-prefix",
        "nested-launchers",
        "launcher-fed-script-operand",
        "launcher-fed-glob-script-operand",
    ),
)
def test_launcher_fed_shell_positionals_fail_closed(body: str) -> None:
    """Codex finding: launcher-supplied argv was outside the positional model.

    ``printf '%s%s\\n' doc- lattice | xargs -n1 sh -c '$0 reconcile'`` executes the marker under
    GNU xargs 4.9 and real Bash 5.2 with a ``doc-lattice`` shim on ``PATH``. The direct spelling
    ``sh -c '$0 reconcile' doc-lattice`` already refuses, and an exported parent variable in the
    same ``xargs`` payload already refuses too, which isolates the launcher-fed argv rather than
    the payload parse. Recognizing ``xargs`` by name is what AD-17 rules out, so the trigger is the
    launcher's INPUT, the only channel any launcher has for argv this body never spells.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _LAUNCHER_REASON


def test_launcher_fed_shell_exported_variable_control_still_refuses() -> None:
    """The control proving the payload second pass does reach through a launcher already."""
    body = "export A=doc-\nexport B=lattice\nprintf 'x\\n' | xargs -n1 sh -c '$A$B reconcile'"

    result = scan_doc_lattice_invocations(body)

    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        "timeout 60 sh -c '$0 reconcile'",
        "ls *.md | xargs -n1 sh -c 'echo $0'",
        "find . -name '*.md' | xargs -n1 sh -c 'echo $0'",
        "git ls-files | xargs -n1 sh -c 'wc -l $0'",
        "printf 'build\\n' | xargs sh -c 'echo hi'",
        "printf 'make\\nbuild\\n' | xargs -n2 sh -c '$0 $1'",
        "printf '%s%s\\n' doc- lattice | xargs -n1 sh -c 'echo hi'",
        "cat list.txt | xargs -n1 sh -c '$0 reconcile'",
        "sh -c '$0 build'",
        "printf '%s\\n' 'run \"$1\"' > s.sh; ls *.md | xargs -n1 bash s*.sh",
    ],
    ids=(
        "launcher-reads-nothing",
        "glob-listing-into-a-shell",
        "find-into-a-shell",
        "git-ls-files-into-a-shell",
        "ordinary-word-advances-the-dfa",
        "ordinary-word-pair",
        "payload-references-no-positional",
        "opaque-input-is-outside-purview",
        "direct-shell-is-not-a-launcher",
        "glob-operand-whose-file-composes-nothing",
    ),
)
def test_launcher_fed_shell_over_refusal_guards(body: str) -> None:
    """Over-refusal guards keeping this narrow to input the body itself composes.

    Substituting the input into the payload's own references rather than asking whether the input
    is marker-FRAGMENT capable is what keeps the ordinary pipelines here certified: ``.md`` and
    ``build`` both end in ``d`` and so advance the scan from the idle entry state. An opaque input
    is the step's own or an untracked file's, which is pre-existing content outside this analysis's
    purview by the rule every guard in this family applies to an unwritten target.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


# Issue #176. Every positional reference used to be substituted by ONE string, the concatenation
# of all the caller's arguments, on the claim that substituting the same text everywhere was sound
# without solving which argument lands at which position. It is not: a file selecting a
# NON-ADJACENT subset composes a marker the concatenation never spells. ``bash s.sh doc- SAFE
# lattice`` against ``"$1$3"`` joins to ``doc-SAFElattice``, which carries no marker progress in
# either its plain or its doubled form, while Bash expands ``$1$3`` to ``doc-lattice`` and runs it.
# Each reference is now replaced by a CHOICE over the individual arguments and their join, so the
# marker DFA covers every assignment of arguments to references at once without enumerating them.
# Every refusing body below executes the marker under real Bash 5.2 with a ``doc-lattice`` shim on
# ``PATH``, and every certifying one runs no marker, confirmed with the differential oracle in
# ``scripts/fuzz_shell_taint.py``.
_POSITIONAL_SUBSET_SCRIPT = "printf '%s\\n' '\"$1$3\" reconcile' > s.sh; "


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_SUBSET_SCRIPT + "bash s.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "bash ./s.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "sh s.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "timeout 60 bash s.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "source s.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + ". ./s.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + 'F=s.sh; source "$F" doc- SAFE lattice',
        _POSITIONAL_SUBSET_SCRIPT + "source s*.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "bash s*.sh doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + 'F=s.sh; bash "$F" doc- SAFE lattice',
        _POSITIONAL_SUBSET_SCRIPT + "bash --rcfile s.sh -ic : X doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "bash --init-file s.sh -ic : X doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "export BASH_ENV=s.sh; bash -c : X doc- SAFE lattice",
        _POSITIONAL_SUBSET_SCRIPT + "BASH_ENV=s.sh bash -c : X doc- SAFE lattice",
        "printf '%s\\n' '\"${1}${3}\" reconcile' > s.sh; bash s.sh doc- SAFE lattice",
        "printf '%s\\n' '\"$1$5\" reconcile' > s.sh; bash s.sh doc- A B C lattice",
    ],
    ids=(
        "exact-operand",
        "dot-slash-operand",
        "sh-head",
        "behind-a-launcher",
        "source-builtin",
        "dot-builtin",
        "source-operand-through-a-variable",
        "source-glob-operand",
        "glob-script-operand",
        "variable-script-operand",
        "rcfile-startup-file",
        "init-file-startup-file",
        "bash-env-exported",
        "bash-env-prefix-assignment",
        "braced-positionals",
        "four-arguments-apart",
    ),
)
def test_shell_script_positional_subset_selection_fails_closed(body: str) -> None:
    """Issue #176: a file reading a non-adjacent subset of its arguments certified everywhere.

    The joined string the analysis substituted, ``doc-SAFElattice``, composes nothing whether it
    is read once or doubled, so condition 3 answered no on all six routes that reach the shared
    helper. Replacing each reference with a choice over the individual arguments lets the marker
    DFA see ``$1`` taking ``doc-`` while ``$3`` takes ``lattice``, which is what Bash does.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _POSITIONAL_REASON


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_SUBSET_SCRIPT + "printf '%s\\n' doc- SAFE lattice | xargs bash s.sh",
        _POSITIONAL_SUBSET_SCRIPT + "printf '%s\\n' doc- SAFE lattice | xargs bash s*.sh",
        "printf '%s\\n' doc- SAFE lattice | xargs sh -c '$0$2 reconcile'",
    ],
    ids=(
        "launcher-fed-script-operand",
        "launcher-fed-glob-script-operand",
        "launcher-fed-payload",
    ),
)
def test_launcher_fed_positional_subset_selection_fails_closed(body: str) -> None:
    """Issue #176: launcher input is bound word by word, so a subset of its words composes too.

    A launcher splits its input into words and appends them as separate argv entries. Joining them
    into ``doc-SAFElattice`` lost that structure twice over: the cheap fragment precheck answered
    no, so the composition test never ran, and the composition test would have answered no too.
    Each input WORD is now a text a positional can bind to, beside the join the model already had.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _LAUNCHER_REASON


@pytest.mark.parametrize(
    "body",
    [
        _POSITIONAL_SUBSET_SCRIPT + "bash s.sh build test deploy",
        _POSITIONAL_SUBSET_SCRIPT + "bash s.sh --verbose --quiet --color",
        _POSITIONAL_SUBSET_SCRIPT + "source s.sh build test deploy",
        _POSITIONAL_SUBSET_SCRIPT + "bash --rcfile s.sh -ic : X build test deploy",
        _POSITIONAL_SUBSET_SCRIPT + "export BASH_ENV=s.sh; bash -c : X build test deploy",
        _POSITIONAL_SUBSET_SCRIPT + "printf '%s\\n' build test deploy | xargs bash s.sh",
        _POSITIONAL_SUBSET_SCRIPT + "bash s.sh",
        "printf '%s\\n' 'run build' > s.sh; bash s.sh doc- SAFE lattice",
        "bash s.sh doc- SAFE lattice",
    ],
    ids=(
        "ordinary-words",
        "ordinary-flags",
        "ordinary-words-through-source",
        "ordinary-words-through-an-rcfile",
        "ordinary-words-through-bash-env",
        "ordinary-words-through-a-launcher",
        "no-arguments",
        "file-ignores-its-arguments",
        "operand-names-no-tracked-file",
    ),
)
def test_positional_subset_selection_over_refusal_guards(body: str) -> None:
    """Widening one string into a choice adds alternatives, not a fragment question.

    ``build`` and ``deploy`` both end in ``d`` and so advance the scan from the idle entry state,
    which is the trap a marker-FRAGMENT question over the arguments falls into. The choice is still
    substituted at the reference positions the file actually spells, so ordinary argument lists,
    ordinary flags and a file that ignores its arguments all stay certified on every route.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def test_positional_choice_keeps_the_joined_alternative_it_replaces() -> None:
    """The joined text stays an alternative, so the widening cannot remove an existing refusal.

    A file with ONE reference and a caller passing two arguments refuses only through the joined
    alternative: ``doc-lattice`` is neither argument on its own, and Bash binds ``$1`` to ``doc-``
    alone, so this is a refusal of a body that runs no marker. It is kept deliberately. Dropping
    the join in favor of the per-argument texts would remove it, and removing it would also remove
    the word-splitting case the join stands in for, where one argument the caller spells becomes
    several words the child concatenates back.
    """
    body = "printf '%s\\n' '\"$1\" reconcile' > s.sh; bash s.sh doc- lattice"

    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _POSITIONAL_REASON


def test_positional_binding_choice_respects_the_alternative_cap() -> None:
    """A binding wider than the alternative cap raises rather than narrowing the choice.

    The substituted expression is evaluated through the same bounded evaluator the rest of the
    module uses, so a choice this analysis cannot represent fails closed at the shared cap instead
    of silently dropping alternatives and certifying on a narrower model than the one intended.
    """
    binding = _PositionalBinding(parts=("doc-", "lattice", "build"), joined="doc-latticebuild")

    with pytest.raises(_TaintLimitExceeded):
        _substituted_text_composes_marker(
            '"$1$2" reconcile',
            binding,
            TaintLimits(max_alternatives=2),
            include_zero=False,
        )


def test_positional_binding_choice_covers_every_argument_and_their_join() -> None:
    """Each argument, each argument's own alternatives, and their join are all bindable."""
    binding = _PositionalBinding(parts=("doc-", "SAFE", "lattice"), joined="doc-SAFElattice")

    assert binding.texts == ("doc-", "SAFE", "lattice", "doc-SAFElattice")
    assert binding.fragment_capable is True
    assert _substituted_text_composes_marker(
        '"$1$3" reconcile', binding, TaintLimits(), include_zero=False
    )
    assert not _substituted_text_composes_marker(
        '"$1$3" reconcile',
        _PositionalBinding(parts=("build", "test"), joined="buildtest"),
        TaintLimits(),
        include_zero=False,
    )


# Codex review round 3. A non-interactive Bash child reads the file BASH_ENV names BEFORE the
# -c, script, or stdin program it was selected to run. Nothing selected it: _select_shell_source
# reads argv, and this arrives through the environment, so no argv-driven route can see it.
_BASH_ENV_WRITE = "printf '%s\\n' 'A=doc-' '\"${A}lattice\" reconcile' > env.sh\n"
_BASH_ENV_REASON = "shell BASH_ENV source state cannot be represented"


@pytest.mark.parametrize(
    "body",
    [
        _BASH_ENV_WRITE + "export BASH_ENV=env.sh\nbash -c :",
        _BASH_ENV_WRITE + "BASH_ENV=env.sh bash -c :",
        _BASH_ENV_WRITE + "export BASH_ENV=./env.sh\nbash -c :",
        _BASH_ENV_WRITE + "P=env.sh\nexport BASH_ENV=$P\nbash -c :",
        _BASH_ENV_WRITE + "export BASH_ENV=env.sh\nbash s.sh",
        _BASH_ENV_WRITE + "export BASH_ENV=env.sh\ntimeout 5 bash -c :",
        (
            "A=doc-\nprintf '%s\\n' \"${A}lattice reconcile\" > env.sh\n"
            "export BASH_ENV=env.sh\nbash -c :"
        ),
    ],
    ids=(
        "exported-variable",
        "per-command-prefix-assignment",
        "dot-slash-spelling",
        "value-resolved-through-a-variable",
        "script-dispatch-form",
        "behind-a-launcher",
        "file-content-composes-the-whole-marker",
    ),
)
def test_bash_env_child_shell_source_fails_closed(body: str) -> None:
    """Codex finding: BASH_ENV named a child shell source no argv-driven route could see.

    Verified under real Bash 5.2 with a ``doc-lattice`` shim on ``PATH``. The same file reached by
    an argv-selected route already refuses on both spellings that name it there,
    ``bash --rcfile env.sh -ic :`` and ``. ./env.sh``, which isolates the environment channel
    rather than the file or its content.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason == _BASH_ENV_REASON


def test_bash_env_unexported_over_approximates_pending_issue_122() -> None:
    """Export status is not modeled, so an unexported BASH_ENV refuses although Bash ignores it.

    This is the same over-approximation the ``-c`` payload second pass already makes, tracked as
    issue #122. Narrowing it would mean reading export evidence the body itself supplies, which
    AD-17's founding principle rules out. Pinned so the cost is visible rather than discovered.
    """
    result = scan_doc_lattice_invocations(_BASH_ENV_WRITE + "BASH_ENV=env.sh\nbash -c :")

    assert result.incomplete_reason == _BASH_ENV_REASON


@pytest.mark.parametrize(
    "body",
    [
        "printf '%s\\n' 'echo hi' > env.sh\nexport BASH_ENV=env.sh\nbash -c :",
        "printf '%s\\n' 'make build' > env.sh\nexport BASH_ENV=env.sh\nbash -c :",
        "printf '%s\\n' 'REGION=us-east-1' > env.sh\nexport BASH_ENV=env.sh\nbash -c :",
        "export BASH_ENV=/etc/profile.d/ci.sh\nbash -c :",
        _BASH_ENV_WRITE + "export BASH_ENV=env.sh\necho done",
    ],
    ids=(
        "inert-file",
        "ordinary-script-text",
        "config-file-idiom",
        "target-this-body-never-writes",
        "command-reaches-no-shell",
    ),
)
def test_bash_env_over_refusal_guards(body: str) -> None:
    """Over-refusal guards: the content gate and the tracked-target rule keep this narrow.

    ``make build`` is the case the raw fragment question would refuse, since it ends in ``d`` and
    so advances the scan from the idle entry state; the child-run content gate asks only what the
    file assigns to its own variables, plus whether it composes the marker outright.
    """
    result = scan_doc_lattice_invocations(body)

    assert result.invocations == ()
    assert result.incomplete_reason is None


def _redirection_builder() -> tuple[_EvidenceBuilder, _CommandEvidence, _StreamScopeEvidence]:
    """Return a builder holding one command and one scope, each redirected at ordinal 0."""
    builder = _EvidenceBuilder.empty()
    command = _command(
        1,
        _arg("cat"),
        name="cat",
        redirections=(_RedirectionEvent(0, "<<", 0, ContentTarget(LiteralTransfer(""))),),
    )
    scope = _StreamScopeEvidence(
        100,
        "brace_group",
        None,
        None,
        SequenceOutput(()),
        redirections=(_RedirectionEvent(0, "<<", 0, ContentTarget(LiteralTransfer(""))),),
    )
    builder.commands.append(command)
    builder.scopes.append(scope)
    return builder, command, scope


def test_attach_redirection_content_replaces_the_placeholder_at_a_matching_ordinal() -> None:
    builder, _command_evidence, _scope = _redirection_builder()
    assignments = (_AssignmentEvidence("A", LiteralTransfer("doc-"), True),)

    attached = builder.attach_redirection_content(1, 0, LiteralTransfer("body"), assignments)

    assert attached is True
    assert builder.commands[0].redirections[0].target == ContentTarget(LiteralTransfer("body"))
    assert builder.commands[0].assignments == assignments


def test_attach_redirection_content_reports_an_ordinal_its_owner_never_recorded() -> None:
    """The False direction the caller refuses on, and the mutation it must not leave behind.

    This is the shape issue #167 produced by reassigning a compound stage's heredoc to a pipeline
    consumer: the owner exists, so nothing raises, but it carries no redirection at that ordinal,
    so the authored body would replace nothing and vanish from the model.
    """
    builder, command, _scope = _redirection_builder()

    attached = builder.attach_redirection_content(
        1,
        3,
        LiteralTransfer("body"),
        (_AssignmentEvidence("A", LiteralTransfer("doc-"), True),),
    )

    assert attached is False
    assert builder.commands[0] == command


def test_attach_scope_redirection_content_replaces_the_placeholder_at_a_matching_ordinal() -> None:
    builder, _command_evidence, _scope = _redirection_builder()
    bindings = (_AssignmentEvidence("A", LiteralTransfer("doc-"), True),)

    attached = builder.attach_scope_redirection_content(100, 0, LiteralTransfer("body"), bindings)

    assert attached is True
    assert builder.scopes[0].redirections[0].target == ContentTarget(LiteralTransfer("body"))
    assert builder.scopes[0].loop_bindings == bindings


def test_attach_scope_redirection_content_reports_an_ordinal_its_owner_never_recorded() -> None:
    """The scope-side False direction, with the same no-mutation requirement."""
    builder, _command_evidence, scope = _redirection_builder()

    attached = builder.attach_scope_redirection_content(
        100,
        3,
        LiteralTransfer("body"),
        (_AssignmentEvidence("A", LiteralTransfer("doc-"), True),),
    )

    assert attached is False
    assert builder.scopes[0] == scope
