"""Every GTX-125-owned human-facing path sink goes through the display spelling.

The bug class this file exists for is an omitted construction site, not a wrong renderer, so
the coverage is per-sink rather than per-shape: each entry drives one construction site with a
hostile filename and asserts the message it built carries the display spelling and no raw
control byte. ``tests/test_path_utils.py`` pins what that spelling is; this file only asserts
that each sink reaches it. The renderer-level and raw-byte behavior is asserted end to end in
``tests/cli/test_contract.py``.

The sinks GTX-125 owns are the load boundary, the human reports, and the ordinary reconcile
paths. ``reconcile_transaction.py`` and reconcile's recovery reporting are GTX-209's, and the
machine channels (JSON, GitHub annotation ``file=``) are deliberately excluded: they carry
their own encoders, and substituting a display spelling into them breaks attachment semantics.
"""

import warnings
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from doc_lattice import discovery, orchestrate, reconcile
from doc_lattice.cli.commands.reconcile import _print_reconcile_lines
from doc_lattice.cli.output import github_annotation, warn_unattachable_annotations
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.error_types import ProjectError
from doc_lattice.frontmatter_parser import parse_meta, split_frontmatter_parts
from doc_lattice.loader import build_lattice
from doc_lattice.model import Node, NodeMeta, ParsedDoc, RawEdge
from doc_lattice.path_utils import format_path_for_display
from doc_lattice.report_render import render_impact
from doc_lattice.text_utils import strip_control_chars

# One filename carrying the vector from the issue: a colour SGR, then a cursor-up that would
# overwrite the diagnostic printed above it. Every sink below is driven with this same name so
# a failure names the sink, not the input.
HOSTILE = "pwn\x1b[31m\x1b[Aevil.md"

# The C0, DEL, and C1 code points a terminal acts on. No sink may pass one through.
CONTROLS = frozenset(chr(code) for code in [*range(0x20), 0x7F, *range(0x80, 0xA0)]) - {
    "\n",  # a message may legitimately span lines; the filename itself carries no newline
}


def _assert_displayed(text: str, path: Path) -> None:
    """Assert a built message names ``path`` in the display spelling and carries no control."""
    assert format_path_for_display(path) in text, f"sink did not use the display spelling: {text!r}"
    leaked = sorted(ch for ch in CONTROLS if ch in text)
    assert not leaked, f"sink leaked raw control bytes {leaked!r}: {text!r}"


def _capture_warning(func) -> str:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        func()
    assert len(caught) == 1, f"expected exactly one warning, got {len(caught)}"
    return str(caught[0].message)


def _console_output(func) -> str:
    output = StringIO()
    console = Console(file=output, width=1000, color_system=None)
    func(console)
    return output.getvalue()


def _runtime(stdout: StringIO, stderr: StringIO, cwd: Path) -> CliRuntime:
    """A runtime bound to plain consoles; neither loader is reached by these sinks."""

    def unused_load_config(_config, seen_cwd):
        raise AssertionError(f"unexpected load from {seen_cwd}")

    def unused_load_lattice(project, *, require_verified=False, persist_cache=True):
        del project, require_verified, persist_cache
        raise AssertionError("unexpected lattice load")

    return CliRuntime(
        stdout=Console(file=stdout, width=1000, color_system=None),
        stderr=Console(file=stderr, width=1000, color_system=None),
        cwd=cwd,
        load_config=unused_load_config,
        load_lattice=unused_load_lattice,
    )


def _node(path: Path, node_id: str = "down") -> Node:
    return Node(
        id=node_id,
        title="t",
        layer=None,
        authority=None,
        path=path,
        body="body\n",
        derives_from=(),
        tickets=(),
    )


def _parsed(path: Path, node_id: str, refs: tuple[str, ...] = ()) -> ParsedDoc:
    meta = NodeMeta(id=node_id, derives_from=[RawEdge(ref=ref) for ref in refs])
    return ParsedDoc(path=path, meta=meta, body="# H\n", sections=None)


class TestTypedErrorSinks:
    """Every typed error that interpolates a path builds it through the display spelling."""

    def test_unreadable_doc_read_failure(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            discovery.read_doc(path)
        _assert_displayed(str(exc.value), path)

    def test_unreadable_doc_non_utf8(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(ProjectError) as exc:
            discovery.read_doc(path)
        _assert_displayed(str(exc.value), path)

    def test_unreadable_doc_unclosed_fence(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            split_frontmatter_parts("---\nid: a\n", path)
        _assert_displayed(str(exc.value), path)

    def test_unreadable_doc_unparseable_frontmatter(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            parse_meta("id: [unclosed\n", path)
        _assert_displayed(str(exc.value), path)

    def test_frontmatter_error_invalid_schema(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            parse_meta("id: a\nauthority: nonsense\n", path)
        _assert_displayed(str(exc.value), path)

    def test_frontmatter_error_id_less_lattice_intent(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            parse_meta("derives_from: []\n", path)
        _assert_displayed(str(exc.value), path)

    def test_duplicate_id_error_names_both_registration_sites(self, tmp_path: Path):
        first = tmp_path / HOSTILE
        second = tmp_path / f"other-{HOSTILE}"
        with pytest.raises(ProjectError) as exc:
            build_lattice([_parsed(first, "collide"), _parsed(second, "collide")])
        _assert_displayed(str(exc.value), first)

    def test_reconcile_reader_failure(self, tmp_path: Path):
        path = tmp_path / HOSTILE

        def raise_os_error(_path: Path) -> bytes:
            msg = "disk vanished"
            raise OSError(msg)

        with pytest.raises(ProjectError) as exc:
            reconcile.plan_rewrites({path: {"a#x": "newhash"}}, raise_os_error)
        _assert_displayed(str(exc.value), path)

    def test_reconcile_unparseable_frontmatter(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        source = "---\nid: a\nderives_from:\n  - ref: b\n    seen: old\n---\nbody\n"

        with pytest.raises(ProjectError) as exc:
            reconcile.plan_rewrites(
                {path: {"b": "newhash"}},
                lambda _path: source.replace("id: a", "id: [unclosed").encode("utf-8"),
            )
        _assert_displayed(str(exc.value), path)

    def test_reconcile_unclosed_fence(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        with pytest.raises(ProjectError) as exc:
            reconcile.plan_rewrites({path: {"b": "newhash"}}, lambda _path: b"---\nid: a\n")
        _assert_displayed(str(exc.value), path)


class TestWarningSinks:
    """Every ``warnings.warn`` site that names a path builds it through the display spelling."""

    def test_id_less_skip(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        message = _capture_warning(lambda: orchestrate._report_skip("id-less", path))
        _assert_displayed(message, path)
        # AD-29: the prefix is load-bearing for PYTHONWARNINGS targetability.
        assert message.startswith("skipping ")

    def test_reused_anchor(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        message = _capture_warning(lambda: orchestrate._report_reused_anchors(True, path))
        _assert_displayed(message, path)
        assert message.startswith("reused anchor in ")

    def test_symlink_escape(self, tmp_path: Path):
        project_root = tmp_path / "project"
        outside = tmp_path / "outside"
        project_root.mkdir()
        outside.mkdir()
        target = outside / "target.md"
        target.write_text("---\nid: out\n---\n# Out\n", encoding="utf-8")
        link = project_root / HOSTILE
        link.symlink_to(target)

        message = _capture_warning(
            lambda: discovery.discover_doc_paths([project_root], (), project_root)
        )
        _assert_displayed(message, link)
        assert message.startswith("skipping ")


class TestDirectConsoleWriteSinks:
    """The three success-path console writes that print a path, none of them a diagnostic."""

    def test_impact_human_report(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        text = _console_output(lambda console: render_impact(console, [(_node(path), 1)]))
        _assert_displayed(text, path)

    def test_reconcile_success_line(self, tmp_path: Path):
        path = tmp_path / HOSTILE
        output = StringIO()
        runtime = _runtime(output, StringIO(), tmp_path)
        _print_reconcile_lines(runtime, path, frozenset({"a#x"}), dry_run=False)
        # The adapter prints the basename, so that is the path this sink is asserted against.
        _assert_displayed(output.getvalue(), Path(path.name))

    def test_unattachable_annotation_warning(self, tmp_path: Path):
        outside = tmp_path / "outside" / HOSTILE
        errors = StringIO()
        runtime = _runtime(StringIO(), errors, tmp_path / "inside")
        warn_unattachable_annotations(runtime, [outside])
        _assert_displayed(errors.getvalue(), outside)


def test_machine_channels_are_deliberately_untouched(tmp_path: Path):
    """JSON and the GitHub annotation ``file=`` keep their own encoders, per the issue's scope."""
    path = tmp_path / HOSTILE
    line = github_annotation(path, tmp_path, "title", "message")
    # The annotation encoder still emits the raw relative spelling, not the display one, so the
    # value stays something GitHub can attach to a diff.
    assert format_path_for_display(path) not in line
    assert HOSTILE in line


def test_strip_control_chars_is_unchanged():
    """The pre-existing network/init helper keeps deleting controls, and keeps its consumers."""
    # The very ambiguity that disqualified it for path display: two distinct inputs, one output.
    assert strip_control_chars("\x1b[31m") == strip_control_chars("[31m") == "[31m"
