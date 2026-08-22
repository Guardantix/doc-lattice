"""Grammar tests for the shared workflow parsing helpers.

`tests/workflow_helpers.py` is read by four suites, and every assertion they build on it
compares sets, counts, or membership -- shapes a dropped reference cannot disturb. A parse
that silently narrows there leaves all four green, so the parse is pinned here directly,
against spellings the repository's own workflows do not currently use.
"""

from pathlib import Path

from workflow_helpers import _action_references, _load_workflow, _uses_fragments


def _workflow(tmp_path: Path, body: str) -> Path:
    """Write one workflow file and return its path."""
    path = tmp_path / "workflow.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_uses_fragments_reads_every_valid_spacing_after_the_list_marker(tmp_path):
    """One space is a formatting habit, not the grammar the runner accepts."""
    path = _workflow(
        tmp_path,
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: owner/one@aaa # v1\n"
        "      -  uses: owner/two@bbb # v2\n"
        "      -\tuses: owner/three@ccc # v3\n"
        "      - name: named\n"
        "        uses: owner/four@ddd # v4\n",
    )

    assert _uses_fragments(path) == [
        "owner/one@aaa # v1",
        "owner/two@bbb # v2",
        "owner/three@ccc # v3",
        "owner/four@ddd # v4",
    ]


def test_uses_fragments_ignores_text_that_is_not_a_step(tmp_path):
    """`-uses:` is a scalar, and a commented reference is one the runner never resolves."""
    path = _workflow(
        tmp_path,
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      # - uses: owner/commented@aaa\n"
        "      - run: echo -uses: owner/scalar@bbb\n"
        "      - uses: 'owner/real@ccc' # v1\n",
    )

    assert _uses_fragments(path) == ["owner/real@ccc # v1"]


def test_action_references_reads_the_job_level_reference_too(tmp_path):
    """A called workflow is a reference the runner resolves without a step to hang it on."""
    path = _workflow(
        tmp_path,
        "jobs:\n"
        "  called:\n"
        "    uses: owner/reusable/.github/workflows/w.yml@aaa\n"
        "  build:\n"
        "    steps:\n"
        "      -   uses: owner/action@bbb\n",
    )

    assert _action_references(_load_workflow(path)) == [
        "owner/reusable/.github/workflows/w.yml@aaa",
        "owner/action@bbb",
    ]
