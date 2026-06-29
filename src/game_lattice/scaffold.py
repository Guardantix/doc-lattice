"""Generate the config and codegen artifacts for the init command.

Pure and filesystem-free: every function returns a string built from typed
inputs, so the module is tested with no I/O. The init command in cli.py does the
disk write and the printing.
"""

import io
from dataclasses import dataclass

from ruamel.yaml import YAML

GAME_LATTICE_REPO_URL = "https://github.com/Guardantix/game-lattice"
PYTHON_PIN = "3.14"

_CONFIG_HEADER = f"# game-lattice configuration. See {GAME_LATTICE_REPO_URL}\n"
_COMMENTED_IGNORE = '# ignore_globs:\n#   - "**/superpowers/plans/**"\n'
_COMMENTED_LINEAR = "# linear_team: ENG\n"
_COMMENTED_BINDING = "# binding_layers: null\n"


@dataclass(frozen=True, slots=True)
class Scaffold:
    """The three artifacts init produces: one written, two printed."""

    config_text: str
    precommit_text: str
    ci_text: str


def _invocation(rev: str, command: str) -> str:
    """Return the uvx command a gate runs, pinned to rev and the PYTHON_PIN interpreter."""
    return (
        f"uvx --python {PYTHON_PIN} --from git+{GAME_LATTICE_REPO_URL}@{rev} game-lattice {command}"
    )


def render_config(docs_roots: tuple[str, ...], linear_team: str | None) -> str:
    """Render .game-lattice.yml with active keys serialized and optionals commented.

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
    parts = [_CONFIG_HEADER, buf.getvalue(), _COMMENTED_IGNORE]
    if linear_team is None:
        parts.append(_COMMENTED_LINEAR)
    parts.append(_COMMENTED_BINDING)
    return "".join(parts)


def render_precommit(rev: str) -> str:
    """Render the repo: local pre-commit hooks that run game-lattice check and lint."""
    return (
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: game-lattice-check\n"
        "        name: game-lattice check\n"
        f"        entry: {_invocation(rev, 'check')}\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
        "      - id: game-lattice-lint\n"
        "        name: game-lattice lint\n"
        f"        entry: {_invocation(rev, 'lint')}\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
    )


def render_ci(rev: str) -> str:
    """Render the GitHub Actions workflow that runs game-lattice check and lint.

    Both commands run in one shell step so a check failure does not skip lint. set +e
    disables errexit so both exit codes are captured; the final test fails the step if
    either command failed.
    """
    check_cmd = _invocation(rev, "check")
    lint_cmd = _invocation(rev, "lint")
    return (
        "name: game-lattice\n"
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


def build_scaffold(docs_roots: tuple[str, ...], linear_team: str | None, rev: str) -> Scaffold:
    """Build all three init artifacts from typed inputs.

    Args:
        docs_roots: The docs roots for the config's docs_roots list.
        linear_team: The team key to bake in, or None.
        rev: The git ref the snippets pin, for example "v0.2.0".

    Returns:
        A Scaffold holding the config text and the two codegen snippets.
    """
    return Scaffold(
        config_text=render_config(docs_roots, linear_team),
        precommit_text=render_precommit(rev),
        ci_text=render_ci(rev),
    )
