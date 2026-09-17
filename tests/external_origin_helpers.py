"""The externally declared two-node graph the origin-reporting suites share.

`up` owns a `#notes` section (duplicated when `ambiguous`), and `down` derives from it and from
a missing file, so check, lint, graph, and stale-shipped findings all name both external
records. Holding one copy keeps those suites asserting against the same graph.

The name keeps its leading underscore across the move, matching `tests/failing_streams.py`.
"""

from pathlib import Path

from doc_lattice.loader import build_lattice
from doc_lattice.model import DocumentOrigin, ExternalDeclaration, NodeMeta, ParsedDoc, RawEdge


def _external_lattice(*, ambiguous=False, authority="binding"):

    return build_lattice(
        [
            ParsedDoc(
                Path("docs/up.md"),
                NodeMeta(id="up", authority="derived"),
                "# Notes\n\n# Notes\n" if ambiguous else "# Notes\nbody\n",
                origin=DocumentOrigin(
                    Path("docs/up.md"), ExternalDeclaration("meta/up.yml", 4, "./docs/up.md")
                ),
            ),
            ParsedDoc(
                Path("docs/down.md"),
                NodeMeta(
                    id="down",
                    authority=authority,
                    tickets=["GTX-770"],
                    derives_from=[RawEdge(ref="up#notes", seen="old"), RawEdge(ref="missing")],
                ),
                "body\n",
                origin=DocumentOrigin(
                    Path("docs/down.md"), ExternalDeclaration("meta/down.yml", 1, "docs/down.md")
                ),
            ),
        ]
    )
