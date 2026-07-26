"""Tests for pure authored-marker shell taint analysis."""

from dataclasses import replace

import pytest

from doc_lattice.error_types import ProjectError
from doc_lattice.github_ci.shell_scanner import (
    _effective_executable_evidence,
    _ScanBudget,
    _ShellWord,
    scan_doc_lattice_invocations,
)
from doc_lattice.github_ci.shell_taint import (
    Choice,
    ChoiceOutput,
    CommandOutput,
    Concat,
    ContentBuilder,
    ContentExpr,
    ContentTarget,
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
    _ContentValue,
    _contextualize_evidence,
    _eval_assignment_transfers,
    _eval_reparse_literal,
    _eval_syntax_expression,
    _EvalSyntaxContext,
    _evaluate_closed,
    _ExecutableEvidence,
    _FlowDefinitions,
    _FlowWrite,
    _is_function_positional_parameter,
    _marker_capable,
    _OutputLowering,
    _PipeEvidence,
    _ProcessResourceEvidence,
    _RedirectionEvent,
    _scoped_variable_name,
    _ShellTaintEvidence,
    _solve_eval_syntax_variables,
    _solve_flow_definitions,
    _static_eval_command_names,
    _static_eval_commands,
    _static_eval_mutations,
    _static_eval_program_commands,
    _static_eval_programs,
    _StreamScopeEvidence,
    _strip_trailing_newlines,
    _substitute_local_contents,
    _TaintLimitExceeded,
    analyze_marker_taint,
    choice,
    concat,
    normalize_static_resource,
    stream_ref_ids,
)


def _can_mark(expression: ContentExpr, *, strip: bool = False) -> bool:
    value = _evaluate_closed(expression)
    if strip:
        value = _strip_trailing_newlines(value)
    return _marker_capable(value)


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

    built = builder.build()

    assert built.argv_ports is not None
    assert [(port.literal, port.content) for port in built.argv_ports] == [
        ("doc-lattice", LiteralTransfer("doc-lattice")),
        ("doc-noop", LiteralTransfer("doc-noop")),
    ]


def test_content_builder_elides_wholly_empty_unquoted_brace_alternatives() -> None:
    builder = ContentBuilder.empty()
    for character in "{,}":
        builder.append_literal(character, brace_active=True)

    assert builder.build().argv_ports == ()


def test_content_builder_keeps_quoted_and_escaped_braces_literal() -> None:
    builder = ContentBuilder.empty()
    builder.append_literal("{doc-,lattice}")

    built = builder.build()

    assert built.argv_ports is not None
    assert [(port.literal, port.content) for port in built.argv_ports] == [
        ("{doc-,lattice}", LiteralTransfer("{doc-,lattice}")),
    ]


def test_content_builder_expands_bounded_ranges_without_turning_word_content_into_choice() -> None:
    builder = ContentBuilder.empty()
    for character in "doc-{1..2}-lattice":
        builder.append_literal(character, brace_active=True)

    built = builder.build()

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

    built = builder.build()

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

    built = builder.build()

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

    built = builder.build()

    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == expected


@pytest.mark.parametrize("source", ["{1..2..invalid}", "{1..²}"])
def test_content_builder_leaves_malformed_brace_ranges_literal(source: str) -> None:
    builder = ContentBuilder.empty()
    for character in source:
        builder.append_literal(character, brace_active=True)

    built = builder.build()

    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == [source]


def test_content_builder_expands_dynamic_recognized_brace_operand() -> None:
    builder = ContentBuilder.empty()
    for character in "{doc-,":
        builder.append_literal(character, brace_active=True)
    builder.append_expression(VariableRef("X"))
    for character in "}lattice":
        builder.append_literal(character, brace_active=True)

    built = builder.build()

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

    built = builder.build()

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

    built = builder.build(defer_brace_errors=True)

    assert built.assignment_content == LiteralTransfer("{1..5000}")
    assert built.argv_ports is not None
    assert [port.literal for port in built.argv_ports] == ["X={1..5000}"]
    assert built.brace_expansion_error == "shell taint brace expansion limit exceeded"


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

    definitions, inputs = _build_flow_definitions(evidence)

    assert inputs[2] == StreamRef(writer.output_scope_id)
    assert _marker_capable(_solve_flow_definitions(definitions).evaluate(StreamRef(2))) is True


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
        )
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_inserts_literal_spaces_between_argument_ports() -> None:
    command = _command(1, _arg("eval"), _arg("doc-"), _arg("lattice"), name="eval")

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_eval_reparse_keeps_external_only_value_non_evidentiary() -> None:
    command = _command(1, _arg("eval"), _arg("$EXTERNAL", VariableRef("EXTERNAL")), name="eval")

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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
    expression, quote = _eval_reparse_literal(text, None)

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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(
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


def test_eval_conditional_assignment_obeys_augmented_edge_cap() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("${X:=doc-}", LiteralTransfer("${X:=doc-}")),
        name="eval",
    )

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_eval_brace_expansion_obeys_taint_cap() -> None:
    command = _command(
        1,
        _arg("eval"),
        _arg("doc-{1..3}-lattice"),
        name="eval",
    )

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_dynamic_shell_selector_fails_closed_over_remaining_arguments() -> None:
    command = _command(
        1,
        _arg("bash"),
        _arg("$OPT", OutsideGap(), dynamic=True),
        _arg("$X", Concat((LiteralTransfer("doc-"), LiteralTransfer("lattice"))), dynamic=True),
        name="bash",
    )

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(reader, writer))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(reader, writer))) == (False, None)


def test_explicit_stdin_redirection_overrides_pipe_input() -> None:
    producer = _command(1, _arg("printf"), _arg("doc-lattice"), name="printf")
    consumer = _command(
        2,
        _arg("bash"),
        name="bash",
        redirections=(_RedirectionEvent(0, "<<<", 0, ContentTarget(LiteralTransfer("true\n"))),),
    )

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(
        _ShellTaintEvidence(commands=(first, second, sink), scopes=(sequence,))
    ) == (True, "authored marker flow reaches an execution sink")

    choice_sink = _command(
        3,
        _arg("eval"),
        _arg("$(...)", StreamRef(201), dynamic=True),
        name="eval",
    )
    assert analyze_marker_taint(
        _ShellTaintEvidence(commands=(first, second, choice_sink), scopes=(choice_scope,))
    ) == (False, None)


def test_evidence_edge_cap_counts_pipe_records() -> None:
    evidence = _ShellTaintEvidence(pipes=(_PipeEvidence(1, 2), _PipeEvidence(2, 3)))

    assert analyze_marker_taint(evidence, limits=TaintLimits(max_edges=1)) == (
        True,
        "shell taint edge limit exceeded",
    )


def test_pipe_without_consumer_fails_closed() -> None:
    producer = _command(1, _arg("printf"), name="printf")
    evidence = _ShellTaintEvidence(commands=(producer,), pipes=(_PipeEvidence(1),))

    assert analyze_marker_taint(evidence) == (
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

    assert analyze_marker_taint(evidence) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(scopes=scopes)) == (False, None)


def test_cyclic_scope_parents_fail_closed() -> None:
    scopes = (
        _StreamScopeEvidence(1, "subshell_group", 2, None, SequenceOutput(())),
        _StreamScopeEvidence(2, "subshell_group", 1, None, SequenceOutput(())),
    )

    assert analyze_marker_taint(_ShellTaintEvidence(scopes=scopes)) == (
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

    contextualized = _contextualize_evidence(_ShellTaintEvidence(scopes=(scope,)))

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
    assert _contextualize_evidence(contextualized).scopes[0].loop_bindings[0].name == (
        _scoped_variable_name(100, "ITEM")
    )


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(sink,), scopes=(root, nested))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(sink,), scopes=(root, nested))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(sink,), scopes=(root, nested))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(sink,), scopes=(root, child))) == (
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

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(
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
    assert analyze_marker_taint(evidence) == (
        True,
        "shell taint evidence cannot be structured",
    )


def test_evidence_count_limit_precedes_nested_reference_validation() -> None:
    duplicate_scopes = (
        _StreamScopeEvidence(1, "subshell_group", None, None, SequenceOutput(())),
        _StreamScopeEvidence(1, "subshell_group", None, None, SequenceOutput(())),
    )

    assert analyze_marker_taint(
        _ShellTaintEvidence(scopes=duplicate_scopes),
        limits=TaintLimits(max_table_entries=1),
    ) == (True, "shell taint table entry limit exceeded")


def test_uppercase_eval_is_not_a_builtin_execution_sink() -> None:
    command = _command(1, _arg("EVAL"), _arg("doc-lattice"), name="EVAL")

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (False, None)


def test_uppercase_source_is_not_a_builtin_execution_sink() -> None:
    writer = _command(
        1,
        _arg("printf"),
        _arg("doc-lattice"),
        name="printf",
        redirections=(_RedirectionEvent(0, ">", 1, StaticResourceTarget("task.sh")),),
    )
    command = _command(2, _arg("SOURCE"), _arg("task.sh"), name="SOURCE")

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(writer, command))) == (False, None)


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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(writer, command))) == (
        True,
        "authored marker flow reaches an execution sink",
    )


def test_shell_plus_c_selects_the_command_payload() -> None:
    command = _command(1, _arg("bash"), _arg("+c"), _arg("doc-lattice"), name="bash")

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(command,))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(writer, reader))) == (
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

    assert analyze_marker_taint(evidence) == (
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

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(writer, shell))) == (
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

    assert analyze_marker_taint(
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(writer, reader))) == (
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

    assert analyze_marker_taint(_ShellTaintEvidence(commands=(writer, reader))) == (
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

    assert analyze_marker_taint(
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
        )
    )

    assert _marker_capable(solved.evaluate(VariableRef("X"))) is True


def test_append_before_assignment_revisits_its_implicit_destination_dependency() -> None:
    solved = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("X", LiteralTransfer("lattice"), append=True),
                _FlowWrite("X", LiteralTransfer("doc-")),
            )
        )
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
        )
    )
    second = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("Y", LiteralTransfer("x")),
                _FlowWrite("X", expression),
            )
        )
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
    error = _TaintLimitExceeded("shell taint alternative limit exceeded")

    assert isinstance(error, ProjectError)
    assert error.code == "SHELL_TAINT_LIMIT_EXCEEDED"
    assert str(error) == "shell taint alternative limit exceeded"


def test_competing_variable_definitions_join_without_composing() -> None:
    solved = _solve_flow_definitions(
        _FlowDefinitions(
            variable_writes=(
                _FlowWrite("X", LiteralTransfer("doc-")),
                _FlowWrite("X", LiteralTransfer("lattice")),
            )
        )
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
        )
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
        )
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


def test_deep_local_content_substitution_fails_with_stable_bound() -> None:
    depth = 1_100
    local_contents: dict[str, ContentExpr] = {
        f"V{index}": VariableRef(f"V{index + 1}") for index in range(depth)
    }
    local_contents[f"V{depth}"] = LiteralTransfer("doc-")

    with pytest.raises(_TaintLimitExceeded, match="local substitution depth limit"):
        _substitute_local_contents(VariableRef("V0"), local_contents)


def test_bounded_local_content_substitution_preserves_marker_control() -> None:
    depth = 64
    local_contents: dict[str, ContentExpr] = {
        f"V{index}": VariableRef(f"V{index + 1}") for index in range(depth)
    }
    local_contents[f"V{depth}"] = LiteralTransfer("doc-")

    assert _substitute_local_contents(VariableRef("V0"), local_contents) == LiteralTransfer("doc-")


def _eval_command(program: str, *, function_context_id: int | None = None) -> _CommandEvidence:
    return replace(
        _command(1, _arg("eval"), _arg(program), name="eval"),
        resolved_eval_program=program,
        function_context_id=function_context_id,
    )


def _mutation_names(command: _CommandEvidence) -> list[str]:
    assignments, _ = _static_eval_mutations(command)
    return [item.assignment.name for item in assignments]


def test_static_eval_recovers_a_scalar_assignment_from_an_exact_payload() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("X=doc-"))

    assert unsets == ()
    assert len(assignments) == 1
    assert assignments[0].assignment.name == "X"
    assert assignments[0].assignment.content == LiteralTransfer("doc-")
    assert (assignments[0].local, assignments[0].force_global) == (False, False)


def test_static_eval_recovers_every_word_of_an_assignment_only_command() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("X=doc- Y=lattice"))

    assert unsets == ()
    assert [(item.assignment.name, item.assignment.content) for item in assignments] == [
        ("X", LiteralTransfer("doc-")),
        ("Y", LiteralTransfer("lattice")),
    ]


def test_static_eval_ignores_assignment_prefix_words_of_an_executed_command() -> None:
    assert _static_eval_mutations(_eval_command("X=doc- printf hi")) == ((), ())


@pytest.mark.parametrize(
    ("program", "expected_local", "expected_force_global"),
    [
        ("declare X=v", True, False),
        ("declare -g X=v", False, True),
        ("export X=v", False, False),
        ("readonly X=v", False, False),
        ("typeset X=v", True, False),
        ("typeset -g X=v", False, True),
    ],
    ids=("declare", "declare-global", "export", "readonly", "typeset", "typeset-global"),
)
def test_static_eval_declaration_builtin_carries_its_scope(
    program: str, expected_local: bool, expected_force_global: bool
) -> None:
    assignments, unsets = _static_eval_mutations(_eval_command(program))

    assert unsets == ()
    assert len(assignments) == 1
    assert assignments[0].assignment.name == "X"
    assert (assignments[0].local, assignments[0].force_global) == (
        expected_local,
        expected_force_global,
    )


def test_static_eval_local_declaration_inside_a_function_context_is_scoped_local() -> None:
    assignments, _ = _static_eval_mutations(_eval_command("local X=v", function_context_id=7))

    assert len(assignments) == 1
    assert assignments[0].assignment.name == "X"
    assert (assignments[0].local, assignments[0].force_global) == (True, False)


def test_static_eval_skips_a_local_declaration_without_a_function_context() -> None:
    assert _static_eval_mutations(_eval_command("local X=v")) == ((), ())


def test_static_eval_unset_collects_names_and_excludes_option_words() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("unset -v X Y"))

    assert assignments == ()
    assert unsets == ("X", "Y")


def test_static_eval_function_only_unset_keeps_the_variable_defined() -> None:
    assert _static_eval_mutations(_eval_command("unset -f X")) == ((), ())


def test_static_eval_combined_unset_still_removes_the_variable() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("unset -vf X"))

    assert assignments == ()
    assert unsets == ("X",)


def test_static_eval_unset_after_end_of_options_collects_the_operand() -> None:
    assignments, unsets = _static_eval_mutations(_eval_command("unset -- X"))

    assert assignments == ()
    assert unsets == ("X",)


def test_static_eval_nameref_unset_fails_closed() -> None:
    with pytest.raises(_TaintLimitExceeded, match="nameref unset"):
        _static_eval_mutations(_eval_command("unset -n R"))


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
    assignments, _ = _static_eval_mutations(_eval_command(program))

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
    assignments, unsets = _static_eval_mutations(_eval_command("declare -n R=X; R=doc-"))

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
        _static_eval_mutations(_eval_command(program))

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
        _static_eval_mutations(_eval_command(program))

    assert isinstance(raised.value, ProjectError)
    assert raised.value.code == "SHELL_TAINT_LIMIT_EXCEEDED"


def test_static_eval_nested_eval_shadowed_by_a_function_does_not_fail_closed() -> None:
    # A function named ``eval`` shadows the builtin, so the payload's ``eval`` word never runs
    # the real builtin and never persists state.
    command = replace(_eval_command('eval "X=doc-"'), active_function_names=frozenset({"eval"}))

    assert _static_eval_mutations(command) == ((), ())


def test_static_eval_nested_eval_in_an_unreachable_branch_does_not_fail_closed() -> None:
    # The outer eval invocation itself is statically unreachable, so its payload never runs.
    command = replace(_eval_command('eval "X=doc-"'), execution_status=False)

    assert _static_eval_mutations(command) == ((), ())


def test_static_eval_nested_eval_asynchronous_does_not_persist_state() -> None:
    # An asynchronous nested eval runs in a subshell whose mutations do not reach the caller.
    assert _static_eval_mutations(_eval_command('eval "X=doc-" &')) == ((), ())


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
        _static_eval_mutations(_eval_command(program, function_context_id=1))

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
    assignments, unsets = _static_eval_mutations(_eval_command(program))

    assert unsets == ()
    assert [(item.assignment.name, item.assignment.content) for item in assignments] == [
        ("A", content)
    ]


def test_static_eval_array_assignment_in_an_unreachable_branch_is_pruned() -> None:
    assert _static_eval_mutations(_eval_command("if false; then A=(doc-); fi")) == ((), ())


def test_static_eval_prunes_mutations_of_an_unreachable_branch() -> None:
    program = "if false; then X=doc-; fi"
    parsed = _static_eval_program_commands(program)

    assert [item.execution_status for item in parsed] == [True, False, True]
    assert _static_eval_mutations(_eval_command(program)) == ((), ())


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
        _static_eval_program_commands(program)

    assert raised.value.code == "SHELL_TAINT_LIMIT_EXCEEDED"

    with pytest.raises(_TaintLimitExceeded, match="shell eval payload cannot be tokenized"):
        _static_eval_mutations(_eval_command(program))


def test_static_eval_backslash_newline_continuation_matches_the_unsplit_payload() -> None:
    # A backslash-newline line continuation is ordinary, reachable Bash (issue #134): Bash
    # removes it before parsing, joining the two lines with no character inserted. The
    # tokenizer now does the same, so a continued and an unsplit payload recover the exact
    # same assignment instead of the continuation silently losing the mutation.
    continued = _static_eval_mutations(_eval_command("X=doc- \\\n; true"))
    unsplit = _static_eval_mutations(_eval_command("X=doc-; true"))

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
    parsed = _static_eval_commands(_eval_command("printf a; declare -g X=1"))

    assert [item.words for item in parsed] == [("printf", "a"), ("declare", "-g", "X=1")]


def test_static_eval_command_names_dedupe_in_first_use_order() -> None:
    names = _static_eval_command_names(_eval_command("printf a; printf b; declare -g X=1"))

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
