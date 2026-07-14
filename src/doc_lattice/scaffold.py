"""Generate the config and codegen artifacts for the init command.

Pure and filesystem-free: every function returns a string built from typed
inputs, so the module is tested with no I/O. The init command in cli.py does the
disk write and the printing.
"""

import io
from dataclasses import dataclass

from ruamel.yaml import YAML

from .constants import (
    PERSISTENCE_TEMP_SUFFIX,
    RECONCILE_AFTER_IMAGE_INFIX,
    RECONCILE_BEFORE_IMAGE_INFIX,
    RECONCILE_JOURNAL_NAME,
)

DOC_LATTICE_REPO_URL = "https://github.com/Guardantix/doc-lattice"
PYTHON_PIN = "3.13"

_CONFIG_HEADER = f"# doc-lattice configuration. See {DOC_LATTICE_REPO_URL}\n"
_COMMENTED_IGNORE = '# ignore_globs:\n#   - "**/superpowers/plans/**"\n'
_COMMENTED_CACHE = "# cache_key: my-project-docs   # opt-in load cache slot under your cache home\n"
_COMMENTED_LINEAR = "# linear_team: ENG\n"
_COMMENTED_BINDING = "# binding_layers: null\n"


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


def render_config(docs_roots: tuple[str, ...], linear_team: str | None) -> str:
    """Render .doc-lattice.yml with active keys serialized and optionals commented.

    The active block is dumped through ruamel.yaml so hostile scalars are quoted
    by the library's own emission logic, never by hand or string-interpolated. The
    header comment and the commented-out example keys are static text.

    Args:
        docs_roots: The docs roots to write as the active docs_roots list.
        linear_team: The team key to bake in, or None to leave it commented.

    Returns:
        The full text of the config file.
    """
    data: dict[str, list[str] | str] = {"docs_roots": list(docs_roots)}
    if linear_team is not None:
        data["linear_team"] = linear_team
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    yaml.dump(data, buf)
    parts = [_CONFIG_HEADER, buf.getvalue(), _COMMENTED_IGNORE, _COMMENTED_CACHE]
    if linear_team is None:
        parts.append(_COMMENTED_LINEAR)
    parts.append(_COMMENTED_BINDING)
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
    """Render the repo: local pre-commit hooks that run doc-lattice check and lint."""
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
    )


def render_ci(version: str) -> str:
    """Render the GitHub Actions workflow that runs doc-lattice check and lint.

    Both commands run in one shell step so a check failure does not skip lint. set +e
    disables errexit so both exit codes are captured; the final test fails the step if
    either command failed.
    """
    check_cmd = _invocation(version, "check")
    lint_cmd = _invocation(version, "lint")
    return (
        "name: doc-lattice\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  check:\n"
        "    name: Traceability check\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: astral-sh/setup-uv@v6\n"
        "      - run: |\n"
        "          set +e\n"
        f"          {check_cmd}\n"
        "          rc_check=$?\n"
        f"          {lint_cmd}\n"
        "          rc_lint=$?\n"
        '          [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]\n'
    )


def build_scaffold(docs_roots: tuple[str, ...], linear_team: str | None, version: str) -> Scaffold:
    """Build all four init artifacts from typed inputs.

    Args:
        docs_roots: The docs roots for the config's docs_roots list.
        linear_team: The team key to bake in, or None.
        version: The exact PyPI package version the snippets install, for example "1.0.0".

    Returns:
        A Scaffold holding the config text and the three guidance snippets.
    """
    return Scaffold(
        config_text=render_config(docs_roots, linear_team),
        gitignore_text=render_gitignore(),
        precommit_text=render_precommit(version),
        ci_text=render_ci(version),
    )
