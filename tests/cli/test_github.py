"""Tests for the GitHub Actions workflow-command encoder and its annotation writers."""

from io import StringIO
from pathlib import Path

import pytest

from doc_lattice.cli.github import (
    Annotation,
    escape_github_message,
    escape_github_property,
    github_annotation,
    warn_unattachable_annotations,
    write_annotations,
    write_document_annotation,
)
from doc_lattice.cli.pipe_policy import PipeClosed
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.error_types import FrontmatterError, UnreadableDocError

from .helpers import _contents, _stub_runtime


@pytest.fixture
def runtime(tmp_path: Path) -> CliRuntime:
    return _stub_runtime(tmp_path, "the annotation encoder")


def test_github_annotation_uses_escaped_absolute_path_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "outside%:,\nfile.md"

    result = github_annotation(Annotation(outside, "title", "message"), tmp_path)

    assert result == (f"::error file={escape_github_property(str(outside))},title=title::message")


def test_github_annotation_escapes_all_workflow_metacharacters(tmp_path: Path):
    result = github_annotation(
        Annotation(tmp_path / "sub%:,\nline.md", "title%:,\r\nline", "message%:,\r\nline"),
        tmp_path,
    )

    assert result == (
        "::error file=sub%25%3A%2C%0Aline.md,title=title%25%3A%2C%0D%0Aline::message%25:,%0D%0Aline"
    )


def test_github_annotation_emits_the_requested_severity(tmp_path: Path):
    line = github_annotation(
        Annotation(tmp_path / "doc.md", "title", "message", "warning"), tmp_path
    )

    assert line == "::warning file=doc.md,title=title::message"


def test_write_annotations_emits_each_items_own_severity(runtime: CliRuntime, tmp_path: Path):
    # The mixed call lint makes: one writer, two severities, one unattachable sweep at the end.
    write_annotations(
        runtime,
        [
            Annotation(tmp_path / "gated.md", "gated", "message"),
            Annotation(tmp_path / "reported.md", "reported", "message", "warning"),
        ],
    )

    assert _contents(runtime.stdout) == (
        "::error file=gated.md,title=gated::message\n"
        "::warning file=reported.md,title=reported::message\n"
    )


def test_escape_github_message_encodes_workflow_command_metacharacters():
    assert escape_github_message("100%\rfirst\nsecond: a,b") == ("100%25%0Dfirst%0Asecond: a,b")


def test_escape_github_property_encodes_message_and_property_metacharacters():
    assert escape_github_property("100%\rfirst\nsecond: a,b") == (
        "100%25%0Dfirst%0Asecond%3A a%2Cb"
    )


def test_write_document_annotation_names_the_document_and_its_code(
    runtime: CliRuntime, tmp_path: Path
):
    source = tmp_path / "docs" / "down.md"

    write_document_annotation(runtime, FrontmatterError("broken", source=source))

    assert _contents(runtime.stdout) == (
        "::error file=docs/down.md,title=doc-lattice FRONTMATTER_ERROR::broken\n"
    )


def test_write_document_annotation_carries_the_diagnostic_notes(
    runtime: CliRuntime, tmp_path: Path
):
    # The stderr diagnostic joins the message with its notes, and the annotation is the same
    # report on another channel: dropping them would make the two disagree.
    error = UnreadableDocError("cannot read", source=tmp_path / "down.md")
    error.add_note("check the encoding")

    write_document_annotation(runtime, error)

    assert _contents(runtime.stdout) == (
        "::error file=down.md,title=doc-lattice UNREADABLE_DOC::cannot read; check the encoding\n"
    )


def test_write_document_annotation_escapes_a_multiline_diagnostic(
    runtime: CliRuntime, tmp_path: Path
):
    # A schema failure's message is multi-line, and a raw newline would end the workflow command
    # partway through, so GitHub would read the remainder as ordinary log output.
    error = FrontmatterError("invalid frontmatter:\n  layer: unknown", source=tmp_path / "down.md")

    write_document_annotation(runtime, error)

    assert _contents(runtime.stdout) == (
        "::error file=down.md,title=doc-lattice FRONTMATTER_ERROR::"
        "invalid frontmatter:%0A  layer: unknown\n"
    )


def test_write_document_annotation_propagates_a_departed_stdout_reader(
    runtime: CliRuntime, tmp_path: Path
):
    # AD-40: the caller emits this before its stderr diagnostic precisely so a refused stdout
    # reaches the silent 141 rather than a tool error carrying a truncated annotation.
    class DepartedReader(StringIO):
        def write(self, _text: str) -> int:
            raise BrokenPipeError

    runtime.stdout.file = DepartedReader()

    with pytest.raises(PipeClosed):
        write_document_annotation(runtime, FrontmatterError("broken", source=tmp_path / "down.md"))


def test_warn_unattachable_annotations_is_silent_when_every_document_is_contained(
    runtime: CliRuntime, tmp_path: Path
):
    contained = tmp_path / "docs" / "down.md"

    warn_unattachable_annotations(runtime, [contained])

    assert _contents(runtime.stderr) == ""


def test_warn_unattachable_annotations_names_the_base_and_each_outside_document(
    runtime: CliRuntime, tmp_path: Path
):
    # An annotation rendered against a base that does not contain the document degrades to an
    # absolute path GitHub drops in silence, so the gate fails with nothing shown on the diff.
    outside = tmp_path.parent / "elsewhere" / "down.md"

    warn_unattachable_annotations(runtime, [outside])

    stderr = _contents(runtime.stderr)
    assert "warning" in stderr
    assert str(tmp_path) in stderr
    assert str(outside) in stderr


def test_warn_unattachable_annotations_reports_once_for_a_repeated_document(
    runtime: CliRuntime, tmp_path: Path
):
    # Findings are per edge, so one document can be annotated many times in a run; the warning
    # is per run.
    outside = tmp_path.parent / "elsewhere" / "down.md"

    warn_unattachable_annotations(runtime, [outside, outside, outside])

    assert _contents(runtime.stderr).count(str(outside)) == 1
    assert "1 annotated document(s)" in _contents(runtime.stderr)


def test_github_annotation_emits_a_line_only_when_given_one(tmp_path: Path):
    doc = tmp_path / "doc.md"
    without = github_annotation(Annotation(doc, "title", "message"), tmp_path)
    with_line = github_annotation(Annotation(doc, "title", "message", line=7), tmp_path)

    assert without == "::error file=doc.md,title=title::message"
    assert with_line == "::error file=doc.md,line=7,title=title::message"


def test_write_annotations_forwards_each_items_line(runtime: CliRuntime, tmp_path: Path):
    write_annotations(
        runtime,
        [
            Annotation(tmp_path / "a.md", "t", "m", line=3),
            Annotation(tmp_path / "b.md", "t", "m", "warning"),
        ],
    )

    assert _contents(runtime.stdout) == (
        "::error file=a.md,line=3,title=t::m\n::warning file=b.md,title=t::m\n"
    )
