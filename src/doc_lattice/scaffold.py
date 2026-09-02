"""Generate the config and codegen artifacts for the init command.

Pure and filesystem-free: every function returns a string built from typed
inputs, so the module is tested with no I/O. The init command adapter in the CLI
package does the disk write and the printing.
"""

import io
from dataclasses import dataclass
from typing import Literal

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .constants import (
    CHECKOUT_USES,
    LATTICE_FORMAT_VERSION,
    PERSISTENCE_TEMP_SUFFIX,
    RECONCILE_AFTER_IMAGE_INFIX,
    RECONCILE_BEFORE_IMAGE_INFIX,
    RECONCILE_JOURNAL_NAME,
    SETUP_UV_USES,
)
from .link_selectors import escape_selector_literal

DOC_LATTICE_REPO_URL = "https://github.com/Guardantix/doc-lattice"
PYTHON_PIN = "3.13"

# What a docs root turned out to be when init looked, which decides the selector shape written
# for it. A nonexistent root is classified as a directory, because the default root is created
# after init and a directory is what a docs root usually is.
RootKind = Literal["file", "directory"]
_RECURSIVE_MARKDOWN = "**/*.md"

# ruamel wraps flow sequences at 80 columns by default, which would split a long branch filter
# across lines and corrupt the hand-assembled workflow text around it.
_YAML_UNLIMITED_WIDTH = 1 << 30

# ruamel emits under YAML 1.2, where these are plain strings, but GitHub Actions reads workflows
# under YAML 1.1 boolean resolution -- the same rule that makes a bare `on:` key a boolean there.
# A branch actually named `on` or `no` would otherwise emit unquoted and be read as a boolean, so
# these are forced to a quoted scalar. Comparison is lowercased, which is wider than YAML 1.1's
# three accepted casings; over-quoting an unaffected spelling is harmless.
_YAML_11_BOOLEAN_WORDS = frozenset({"y", "yes", "n", "no", "true", "false", "on", "off"})

_CONFIG_HEADER = f"# doc-lattice configuration. See {DOC_LATTICE_REPO_URL}\n"
_COMMENTED_IGNORE = '# ignore_globs:\n#   - "**/archive/**"\n'
_COMMENTED_CACHE = "# cache_key: my-project-docs   # opt-in load cache slot under your cache home\n"
# The one commented key carrying a correctness caveat, so it is scaffolded rather than left for a
# reader to discover in the docs. The scaffolded value is true rather than the false default,
# because every commented line here is written to do something when uncommented; a scaffolded
# false would read as the opt-in the trailing comment calls it while actually being a no-op. The
# trailing comment names the tier, the commands it applies to, and the dependency that makes
# uncommenting this line by itself a config error, since cache_trust_stat: true without cache_key
# is rejected. The caveats that make the tier opt-in live in README's Load cache section, which
# has room to state them properly. They are deliberately not restated here, since a copy would
# have to be re-synced with that prose.
_COMMENTED_TRUST_STAT = (
    "# cache_trust_stat: true       "
    "# opt-in stat fast tier for read-only commands (needs cache_key)\n"
)
_COMMENTED_LINEAR = "# linear_team: ENG\n"


@dataclass(frozen=True, slots=True)
class Scaffold:
    """The four artifacts init produces: one written, three printed."""

    config_text: str
    gitignore_text: str
    precommit_text: str
    ci_text: str


def _invocation(version: str, command: str) -> str:
    """Return a uvx command pinned to an exact PyPI version and Python interpreter."""
    return f"uvx --python {PYTHON_PIN} --from doc-lattice=={version} doc-lattice {command}"


def selector_for_root(root: str, kind: RootKind) -> str:
    """Spell one docs root as the ``link_sources`` selector that covers it.

    The literal is normalized (a leading ``./``, trailing slashes, and interior ``//`` or
    ``/./`` removed) and then escaped, so a root containing ``*``, ``?``, or ``[`` names itself
    rather than becoming a pattern. The project root itself becomes every Markdown file beneath
    it.

    Args:
        root: The root as a project-relative POSIX path. The init adapter passes the resolved,
            contained path for a root that exists and the literal flag for one that does not.
        kind: Whether the root is a file or a directory.

    Returns:
        The file itself for a file root, or ``root/**/*.md`` for a directory root.
    """
    normalized = "/".join(part for part in root.split("/") if part not in ("", "."))
    if normalized == "":
        return _RECURSIVE_MARKDOWN
    literal = escape_selector_literal(normalized)
    return literal if kind == "file" else f"{literal}/{_RECURSIVE_MARKDOWN}"


def render_config(
    docs_roots: tuple[str, ...], link_sources: tuple[str, ...], linear_team: str | None
) -> str:
    """Render .doc-lattice.yml with active keys serialized and optionals commented.

    The active block is dumped through ruamel.yaml so hostile scalars are quoted
    by the library's own emission logic, never by hand or string-interpolated. The
    header comment and the commented-out example keys are static text. The required
    lattice_format key leads the active block, because it is the version-skew guard and an
    adopter reading the file should meet it first.

    Args:
        docs_roots: The docs roots to write as the active docs_roots list.
        link_sources: The selectors to write as the active link_sources list, already derived
            from the roots by the caller.
        linear_team: The team key to bake in, or None to leave it commented.

    Returns:
        The full text of the config file.
    """
    data: dict[str, int | list[str] | str] = {
        "lattice_format": LATTICE_FORMAT_VERSION,
        "docs_roots": list(docs_roots),
        "link_sources": list(link_sources),
    }
    if linear_team is not None:
        data["linear_team"] = linear_team
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    yaml.dump(data, buf)
    parts = [
        _CONFIG_HEADER,
        buf.getvalue(),
        _COMMENTED_IGNORE,
        _COMMENTED_CACHE,
        _COMMENTED_TRUST_STAT,
    ]
    if linear_team is None:
        parts.append(_COMMENTED_LINEAR)
    return "".join(parts)


def render_gitignore() -> str:
    """Render ignore patterns for recoverable reconcile artifacts."""
    return (
        f"{RECONCILE_JOURNAL_NAME}\n"
        f"{RECONCILE_JOURNAL_NAME}.*{PERSISTENCE_TEMP_SUFFIX}\n"
        f".*{RECONCILE_BEFORE_IMAGE_INFIX}*{PERSISTENCE_TEMP_SUFFIX}\n"
        f".*{RECONCILE_AFTER_IMAGE_INFIX}*{PERSISTENCE_TEMP_SUFFIX}\n"
    )


def render_precommit(version: str) -> str:
    """Render the repo: local pre-commit hooks that run doc-lattice check, lint, and links."""
    return (
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: doc-lattice-check\n"
        "        name: doc-lattice check\n"
        f"        entry: {_invocation(version, 'check')}\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
        "      - id: doc-lattice-lint\n"
        "        name: doc-lattice lint\n"
        f"        entry: {_invocation(version, 'lint')}\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
        "      # always_run rather than files: \\.md$, because the break links catches is\n"
        "      # cross-document: renaming a heading in one file invalidates a link written in\n"
        "      # another, and the file that changed is not the file that ends up wrong.\n"
        "      - id: doc-lattice-links\n"
        "        name: doc-lattice links\n"
        f"        entry: {_invocation(version, 'links')}\n"
        "        language: system\n"
        "        always_run: true\n"
        "        pass_filenames: false\n"
    )


def _render_branch_filter(default_branch: str) -> str:
    """Serialize one branch name as a YAML flow sequence through the emitter.

    The caller validates the name against a strict ASCII domain, but validation and output
    escaping are independent invariants: the sequence is emitted by ruamel.yaml so a scalar
    that would need quoting gets it from the library, never from hand-written interpolation.

    Args:
        default_branch: The branch name to place in the trigger filter.

    Returns:
        The filter as a single-line YAML flow sequence, for example ``[main]``.
    """
    scalar: str | DoubleQuotedScalarString = default_branch
    if default_branch.lower() in _YAML_11_BOOLEAN_WORDS:
        scalar = DoubleQuotedScalarString(default_branch)
    yaml = YAML()
    yaml.default_flow_style = True
    yaml.width = _YAML_UNLIMITED_WIDTH
    buf = io.StringIO()
    yaml.dump([scalar], buf)
    return buf.getvalue().strip()


def render_ci(version: str, *, default_branch: str) -> str:
    """Render the GitHub Actions workflow that runs doc-lattice check, lint, and links.

    All three commands run in one shell step so a check failure does not skip the rest. set +e
    disables errexit so every exit code is captured; the final test fails the step if any
    command failed. ``links`` runs with ``--format github`` because annotations on the
    pull-request diff are the surface a reviewer sees; ``check`` and ``lint`` keep their plain
    invocations.

    ``default_branch`` is required and keyword-only so no caller can silently restore a
    hard-wired ``main``: an adopting repository whose default branch is ``master``, ``trunk``,
    or ``develop`` would otherwise install a workflow that reads as correct and never triggers.
    This module stays pure, so the name arrives already resolved and validated by the CLI
    adapter and nothing is probed here.

    Both actions are pinned by commit SHA with a trailing version comment, and the job carries
    the same least-privilege posture the recipe in MANAGED_CI.md documents: a read-only
    ``contents`` token,
    ``persist-credentials: false`` so the job's token is not left in ``.git/config`` while the
    following step resolves and runs third-party packages, and ``enable-cache: false`` so no
    persistent cross-run cache another workflow on the repository can populate is restored into
    the gate job. The pinned ``uses:`` fragments are read from ``constants.py``, the single owner
    of both halves of every pin, so a bump is one edit here.
    """
    check_cmd = _invocation(version, "check")
    lint_cmd = _invocation(version, "lint")
    links_cmd = _invocation(version, "links --format github")
    branch_filter = _render_branch_filter(default_branch)
    return (
        "name: doc-lattice\n"
        "on:\n"
        "  push:\n"
        f"    branches: {branch_filter}\n"
        "  pull_request:\n"
        f"    branches: {branch_filter}\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  check:\n"
        "    name: Traceability check\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"      - uses: {CHECKOUT_USES}\n"
        "        with:\n"
        "          persist-credentials: false\n"
        f"      - uses: {SETUP_UV_USES}\n"
        "        with:\n"
        "          enable-cache: false\n"
        "      - run: |\n"
        "          set +e\n"
        f"          {check_cmd}\n"
        "          rc_check=$?\n"
        f"          {lint_cmd}\n"
        "          rc_lint=$?\n"
        f"          {links_cmd}\n"
        "          rc_links=$?\n"
        '          [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ] && [ "$rc_links" -eq 0 ]\n'
    )


def build_scaffold(
    docs_roots: tuple[str, ...],
    link_sources: tuple[str, ...],
    linear_team: str | None,
    version: str,
    *,
    default_branch: str,
) -> Scaffold:
    """Build all four init artifacts from typed inputs.

    Args:
        docs_roots: The docs roots for the config's docs_roots list.
        link_sources: The selectors for the config's link_sources list, already derived from
            the roots by the caller.
        linear_team: The team key to bake in, or None.
        version: The exact PyPI package version the snippets install, for example "1.0.0".
        default_branch: The already validated branch the workflow's triggers filter on.
            Required and keyword-only for the reason ``render_ci`` documents.

    Returns:
        A Scaffold holding the config text and the three guidance snippets.
    """
    return Scaffold(
        config_text=render_config(docs_roots, link_sources, linear_team),
        gitignore_text=render_gitignore(),
        precommit_text=render_precommit(version),
        ci_text=render_ci(version, default_branch=default_branch),
    )
