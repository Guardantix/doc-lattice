"""Tests for the D1 PR-reachability predicate."""

import pytest

from doc_lattice.github_ci.reachability import job_is_pr_reachable

PR = frozenset({"pull_request", "pull_request_review", "pull_request_review_comment"})


@pytest.mark.parametrize(
    ("condition", "events", "expected"),
    [
        (None, PR, True),
        ("", PR, True),
        ("github.event_name == 'push'", PR, False),
        ("github.event_name == 'pull_request'", PR, True),
        ("'push' == github.event_name", PR, False),
        ("${{ github.event_name == 'push' }}", PR, False),
        ("${{ github.event_name == 'PUSH' }}", PR, False),
        ("GITHUB.EVENT_NAME == 'push'", PR, False),
        (
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
            PR,
            False,
        ),
        ("github.ref == 'refs/heads/main'", PR, True),
        (
            "github.event_name == 'pull_request' && github.ref == 'refs/heads/main'",
            PR,
            True,
        ),
        ("github.event_name == 'push' || github.event_name == 'pull_request'", PR, True),
        ("github.event_name != 'push'", PR, True),
        ("!(github.event_name == 'push')", PR, True),
        ("${{ !(true && github.event_name == 'push' && true) }}", PR, True),
        ("!(github.event_name == 'push' && github.actor == 'x')", PR, True),
        ("(github.event_name == 'push') && (github.event_name == 'push')", PR, True),
        ("github.event_name == 'push' && github.head_ref == 'x(y)'", PR, False),
        ('github.event_name == "push"', PR, True),
        ("github.event_name == 'push' &&", PR, True),
        ("${{ github.event_name == 'push'", PR, True),
        ("github.event_name == 'push'", frozenset({"pull_request"}), False),
        ("github.event_name == 'pull_request'", frozenset(), False),
        (None, frozenset(), False),
    ],
)
def test_job_is_pr_reachable(condition, events, expected):
    assert job_is_pr_reachable(condition, events) is expected
