"""Tests for pure authored-marker shell taint analysis."""

import pytest

from doc_lattice.github_ci.shell_taint import (
    Choice,
    Concat,
    ContentExpr,
    LiteralTransfer,
    OutsideGap,
    ResourceRef,
    StreamRef,
    TaintLimits,
    VariableRef,
    _evaluate_closed,
    _FlowDefinitions,
    _FlowWrite,
    _marker_capable,
    _solve_flow_definitions,
    _strip_trailing_newlines,
    _TaintLimitExceeded,
    choice,
    concat,
)


def _can_mark(expression: ContentExpr, *, strip: bool = False) -> bool:
    value = _evaluate_closed(expression)
    if strip:
        value = _strip_trailing_newlines(value)
    return _marker_capable(value)


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
