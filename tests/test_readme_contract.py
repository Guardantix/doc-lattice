"""Contract tests binding README.md to the behavior it publishes.

README.md owns the user-facing contract, so several of its blocks are derived facts rather than
prose. The error-code table enumerates a closed domain declared in ``constants.py``, the
global-options table enumerates the root callback's own parameters, and the sample
``.doc-lattice.yml`` fence claims to be what ``init`` writes. All three drift silently under
review, and two of them had: the printed codes went undocumented from the first release while
the type tree grew around them, and the sample and the generator differ on most of their lines.
Asserting them here turns each into a gate rather than something a reader is expected to notice.

Only the mechanical claims live here. Whether a row's prose *describes* its code correctly is a
review judgment, and a prose assertion strong enough to catch a wrong description would break on
every wording change, so it is deliberately not attempted. The two structural constraints that
survive rewording -- no empty description, no two rows sharing one -- are asserted, since those
catch truncation and copy-paste duplication without pinning any wording.
"""

import re
from pathlib import Path
from typing import get_args

import typer

from doc_lattice.cli.application import create_app
from doc_lattice.constants import ErrorCode
from doc_lattice.scaffold import render_config
from doc_lattice.sections import build_toc, split_body_lines

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"

# The base default. No subclass claims it and no production path constructs a bare
# ``ProjectError``, so advertising it would name a diagnostic a reader can never receive.
_UNDOCUMENTED_CODE = "UNKNOWN"

# What ``init`` writes with no flags: the default docs root, and no team baked in.
_DEFAULT_DOCS_ROOTS = ("docs",)

_CODE_ROW = re.compile(r"^\| `([A-Z_]+)` \| (.*?) \|$", re.MULTILINE)
_OPTION_ROW = re.compile(r"^\| `(--[a-z-]+)` \|", re.MULTILINE)
_YAML_FENCE = re.compile(r"^```yaml\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def _section(text: str, level: int, title: str) -> str:
    """Return one Markdown section's body, stopping at the next heading of any level.

    Headings come from the engine's own pinned parser rather than a line scan, so a ``#``
    line inside a fence cannot be mistaken for a heading. The Configuration sample opens with
    ``# doc-lattice configuration``; a naive scan reads that as an H1 and cuts the section off
    at the fence opener, leaving the sample itself outside the returned body.

    The cut is at the next heading of *any* level, which is deliberately unlike
    ``sections.section_spans``: that treats a section as containing its subsections, which would
    let ``## Configuration`` swallow ``### Load cache (opt-in)`` and any fence inside it.
    """
    toc = build_toc(text)
    matches = [h for h in toc if h.level == level and h.text == title]
    assert len(matches) == 1, f"expected exactly one {title!r} heading, found {len(matches)}"
    start = matches[0].line
    later = [h.line for h in toc if h.line > start]
    lines = split_body_lines(text)
    end = (later[0] - 1) if later else len(lines)
    return "\n".join(lines[start:end]) + "\n"


def _code_rows(text: str) -> list[tuple[str, str]]:
    return _CODE_ROW.findall(_section(text, 3, "Error codes"))


def test_readme_error_code_table_is_exactly_the_published_domain():
    """The table's rows are the whole coded domain, in declaration order, minus UNKNOWN."""
    documented = [code for code, _ in _code_rows(_readme())]
    expected = [code for code in get_args(ErrorCode) if code != _UNDOCUMENTED_CODE]
    assert documented == expected, (
        "README's error-code table drifted from ErrorCode: "
        f"missing {sorted(set(expected) - set(documented))}, "
        f"unexpected {sorted(set(documented) - set(expected))}"
    )


def test_readme_error_code_table_omits_the_base_default():
    """UNKNOWN stays out of the published table even though it is a member of the domain."""
    documented = [code for code, _ in _code_rows(_readme())]
    assert _UNDOCUMENTED_CODE not in documented
    assert _UNDOCUMENTED_CODE in get_args(ErrorCode), "guard is only meaningful while it exists"


def test_readme_error_code_rows_each_describe_something():
    """No row is left with an empty description cell.

    Wording is a review judgment, but emptiness is not: a truncated row would otherwise satisfy
    the set-equality assertion above while publishing a code with no meaning attached.
    """
    for code, description in _code_rows(_readme()):
        assert description.strip(), f"{code} has an empty description cell"


def test_readme_error_code_rows_do_not_share_a_description():
    """Two codes describing themselves identically means one was copied and never edited."""
    rows = _code_rows(_readme())
    seen: dict[str, str] = {}
    for code, description in rows:
        previous = seen.setdefault(description, code)
        assert previous == code, f"{code} repeats the description written for {previous}"


def test_readme_global_options_match_the_root_callback():
    """The documented global options are exactly the root callback's own parameters.

    ``--help`` is synthesized lazily by Click and so is absent from ``.params``; completion
    options are absent because the app is built with ``add_completion=False``.
    """
    commands = _section(_readme(), 2, "Commands")
    table = commands.split("Two options are global rather than per-command", 1)[1]
    documented = set(_OPTION_ROW.findall(table))
    declared = {parameter.opts[0] for parameter in typer.main.get_command(create_app()).params}
    assert documented == declared, (
        "README's global-options table drifted from the root callback: "
        f"missing {sorted(declared - documented)}, unexpected {sorted(documented - declared)}"
    )


def test_readme_config_sample_is_byte_for_byte_the_generated_default():
    """The Configuration fence is what a flagless ``init`` writes, not a hand-kept paraphrase."""
    fences = _YAML_FENCE.findall(_section(_readme(), 2, "Configuration"))
    assert len(fences) == 1, f"expected one yaml fence under Configuration, found {len(fences)}"
    assert fences[0] == render_config(_DEFAULT_DOCS_ROOTS, None)
