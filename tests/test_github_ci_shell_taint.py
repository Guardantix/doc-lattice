"""Tests for pure authored-marker shell taint analysis."""

import pytest

from doc_lattice.github_ci.shell_taint import (
    Choice,
    Concat,
    ContentExpr,
    LiteralTransfer,
    OutsideGap,
    _evaluate_closed,
    _marker_capable,
    _strip_trailing_newlines,
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
