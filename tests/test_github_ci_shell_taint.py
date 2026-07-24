"""Tests for pure authored-marker shell taint analysis."""

import pytest

from doc_lattice.error_types import ProjectError
from doc_lattice.github_ci.shell_scanner import (
    _effective_executable_evidence,
    _ScanBudget,
    _ShellWord,
)
from doc_lattice.github_ci.shell_taint import (
    Choice,
    ChoiceOutput,
    CommandOutput,
    Concat,
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
    _contextualize_evidence,
    _evaluate_closed,
    _ExecutableEvidence,
    _FlowDefinitions,
    _FlowWrite,
    _marker_capable,
    _OutputLowering,
    _PipeEvidence,
    _ProcessResourceEvidence,
    _RedirectionEvent,
    _scoped_variable_name,
    _ShellTaintEvidence,
    _solve_flow_definitions,
    _StreamScopeEvidence,
    _strip_trailing_newlines,
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
