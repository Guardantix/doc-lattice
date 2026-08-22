"""Contract tests binding README.md to the behavior it publishes.

README.md owns the user-facing contract, so two of its blocks are derived facts rather than
prose. The error-code table enumerates a closed domain declared in ``constants.py``, and the
sample ``.doc-lattice.yml`` fence claims to be what ``init`` writes. Both drift silently under
review, and both had: the published domain went years undocumented while the type tree grew, and
the sample and the generator differ on most of their lines. Asserting them here turns each into
a gate rather than something a reader is expected to notice.

Only the mechanical claims live here. Whether a row's prose *describes* its code correctly is a
review judgment, and a prose assertion strong enough to catch a wrong description would break on
every wording change, so it is deliberately not attempted.
"""

import re
from pathlib import Path
from typing import get_args

from doc_lattice.constants import ErrorCode
from doc_lattice.scaffold import render_config

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"

# The base default. No subclass claims it and no production path constructs a bare
# ``ProjectError``, so advertising it would name a diagnostic a reader can never receive.
_UNDOCUMENTED_CODE = "UNKNOWN"

# What ``init`` writes with no flags: the default docs root, and no team baked in.
_DEFAULT_DOCS_ROOTS = ("docs",)

_HEADING = re.compile(r"#{1,6} ")
_CODE_ROW = re.compile(r"^\| `([A-Z_]+)` \|", re.MULTILINE)
_YAML_FENCE = re.compile(r"^```yaml\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return one Markdown section's body, stopping at the next heading of any level.

    Fenced blocks are tracked so a comment line inside a code fence is not mistaken for a
    heading. The Configuration sample opens with ``# doc-lattice configuration``, which a naive
    line scan would read as an H1 and truncate the section to nothing.
    """
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == heading]
    assert len(starts) == 1, f"expected exactly one {heading!r} heading, found {len(starts)}"
    body: list[str] = []
    fenced = False
    for line in lines[starts[0] + 1 :]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and _HEADING.match(line):
            break
        body.append(line)
    return "".join(body)


def test_readme_error_code_table_is_exactly_the_published_domain():
    """The table's rows are the whole coded domain, in declaration order, minus UNKNOWN."""
    documented = _CODE_ROW.findall(_section(_readme(), "### Error codes"))
    expected = [code for code in get_args(ErrorCode) if code != _UNDOCUMENTED_CODE]
    assert documented == expected, (
        "README's error-code table drifted from ErrorCode: "
        f"missing {sorted(set(expected) - set(documented))}, "
        f"unexpected {sorted(set(documented) - set(expected))}"
    )


def test_readme_error_code_table_omits_the_base_default():
    """UNKNOWN stays out of the published table even though it is a member of the domain."""
    documented = _CODE_ROW.findall(_section(_readme(), "### Error codes"))
    assert _UNDOCUMENTED_CODE not in documented
    assert _UNDOCUMENTED_CODE in get_args(ErrorCode), "guard is only meaningful while it exists"


def test_readme_config_sample_is_byte_for_byte_the_generated_default():
    """The Configuration fence is what a flagless ``init`` writes, not a hand-kept paraphrase."""
    fences = _YAML_FENCE.findall(_section(_readme(), "## Configuration"))
    assert len(fences) == 1, f"expected one yaml fence under Configuration, found {len(fences)}"
    assert fences[0] == render_config(_DEFAULT_DOCS_ROOTS, None)
