"""Tests for path utilities."""

from pathlib import Path

import pytest

from doc_lattice.path_utils import format_path_for_display, safe_resolve


def test_safe_resolve_within_root(tmp_path):
    (tmp_path / "file.txt").touch()
    result = safe_resolve(tmp_path / "file.txt", root=tmp_path)
    assert result == (tmp_path / "file.txt").resolve()


def test_safe_resolve_accepts_root_itself(tmp_path):
    assert safe_resolve(tmp_path, root=tmp_path) == tmp_path.resolve()


def test_safe_resolve_accepts_nonexistent_child(tmp_path):
    result = safe_resolve(tmp_path / "nope.txt", root=tmp_path)
    assert result == (tmp_path / "nope.txt").resolve()


def test_safe_resolve_escapes_root(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        safe_resolve("../../etc/passwd", root=tmp_path)


def test_safe_resolve_rejects_absolute_path_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    intruder = outside / "f.txt"
    with pytest.raises(ValueError, match="outside"):
        safe_resolve(intruder, root=root)


def test_safe_resolve_rejects_symlink_escaping_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = root / "leak.txt"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="outside"):
        safe_resolve(link, root=root)


def test_safe_resolve_defaults_root_to_cwd(tmp_path, monkeypatch):
    (tmp_path / "file.txt").touch()
    monkeypatch.chdir(tmp_path)
    assert safe_resolve("file.txt") == (tmp_path / "file.txt").resolve()


def test_safe_resolve_default_root_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        safe_resolve("../escape.txt")


class TestFormatPathForDisplay:
    """The display spelling is exactly ``repr(str(path))`` on the active interpreter."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # C0 controls Python spells with a named escape rather than a hex one.
            ("a\tb.md", "'a\\tb.md'"),
            ("a\nb.md", "'a\\nb.md'"),
            ("a\rb.md", "'a\\rb.md'"),
            # C0 controls with no named escape, including the ESC that drives ANSI.
            ("pwn\x1b[31m\x1b[Aevil.md", "'pwn\\x1b[31m\\x1b[Aevil.md'"),
            ("a\x00b.md", "'a\\x00b.md'"),
            ("a\x0bb.md", "'a\\x0bb.md'"),
            ("a\x07b.md", "'a\\x07b.md'"),
            # DEL and the C1 range, which drive 8-bit terminals.
            ("a\x7fb.md", "'a\\x7fb.md'"),
            ("a\x85b.md", "'a\\x85b.md'"),
            ("a\x9bb.md", "'a\\x9bb.md'"),
            # Printable non-ASCII survives verbatim.
            ("café-naïve.md", "'café-naïve.md'"),
            ("日本語.md", "'日本語.md'"),
            # Backslashes double, so a literal cannot forge an escape.
            ("a\\x1bb.md", "'a\\\\x1bb.md'"),
            ("a\\tb.md", "'a\\\\tb.md'"),
        ],
    )
    def test_spelling_matrix(self, name, expected):
        assert format_path_for_display(Path(name)) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # CPython picks single quotes by default...
            ("plain.md", "'plain.md'"),
            # ...switches to double quotes for a name holding a single quote and no double...
            ("it's.md", '"it\'s.md"'),
            # ...and stays on single quotes, escaping, when the name holds both.
            ('it\'s-a-"quote".md', "'it\\'s-a-\"quote\".md'"),
            # A double quote alone does not trigger the switch.
            ('say-"hi".md', "'say-\"hi\".md'"),
        ],
    )
    def test_quote_style_switch(self, name, expected):
        assert format_path_for_display(Path(name)) == expected

    def test_undecodable_bytes_render_without_raising(self):
        # Python surfaces an undecodable filename byte as a lone U+DCxx surrogate. str() on it
        # is fine; anything that encodes it (a print to a UTF-8 stream) would raise instead.
        path = Path("bad-\udcff-\udc80.md")
        assert format_path_for_display(path) == "'bad-\\udcff-\\udc80.md'"

    def test_matches_repr_of_str_exactly(self):
        for name in ["plain.md", "a\x1bb.md", "it's.md", "café.md", "a\\b.md"]:
            path = Path(name)
            assert format_path_for_display(path) == repr(str(path))

    def test_distinct_names_never_collide(self):
        # The property `strip_control_chars` cannot offer: deleting controls maps ESC[31m and a
        # literal [31m onto one spelling, while an injective encoding keeps them apart.
        names = [
            "\x1b[31m.md",
            "[31m.md",
            "a\\x1bb.md",
            "a\x1bb.md",
            "a\\tb.md",
            "a\tb.md",
            "it's.md",
            'it"s.md',
            "café.md",
            "cafe.md",
        ]
        rendered = [format_path_for_display(Path(name)) for name in names]
        assert len(set(rendered)) == len(names)

    def test_no_raw_control_byte_survives(self):
        controls = [chr(code) for code in [*range(0x20), 0x7F, *range(0x80, 0xA0)]]
        for control in controls:
            # A path separator cannot appear in one component, and Path() collapses it anyway.
            rendered = format_path_for_display(Path(f"a{control}b.md"))
            assert control not in rendered

    def test_directory_path_renders_as_one_quoted_string(self):
        rendered = format_path_for_display(Path("docs/sub dir/a\x1bb.md"))
        assert rendered == "'docs/sub dir/a\\x1bb.md'"
