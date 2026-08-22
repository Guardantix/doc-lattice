"""Shared parsing helpers for the workflow-contract test modules.

Four suites assert against the same two grammars: the YAML of ``.github/workflows/*.yml``, and
the shell text inside a ``run:`` step. Both are subtle enough to get wrong more than once -- a
``#`` inside quotes is not a comment, ``-c`` and ``-m`` demote a script path to an argument
nothing runs, and a YAML 1.1 resolver reads a bare ``on:`` key as the boolean ``True``. Holding
one implementation of each is what keeps a fix to any of them from landing in one suite and
leaving the others reading the old semantics.

The names keep their leading underscore across the move, matching ``tests/cli/helpers.py``.
"""

import re
import shlex
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIR = _ROOT / ".github/workflows"
# One whitespace run after the list marker, not the single space the workflows happen to
# be formatted with: `-  uses:` is the same step to the runner.
_USES_RE = re.compile(r"^(?:-\s+)?uses:(.*)$")


def _workflow_paths() -> list[Path]:
    """Return every workflow file in the repository, in a stable order."""
    return sorted(path for path in _WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"})


def _load_workflow(path: Path) -> Any:
    """Return one workflow or Actions config file parsed by the safe loader."""
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def _workflows() -> dict[str, Any]:
    """Return every workflow parsed by the safe loader, keyed by file name."""
    return {path.name: _load_workflow(path) for path in _workflow_paths()}


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return a workflow's ``on:`` block under either YAML spelling.

    A 1.1 resolver reads the bare key ``on`` as the boolean ``True``, and the safe loader keeps
    that resolution, so reading only the string key would silently find nothing and pass every
    trigger assertion vacuously.

    Args:
        workflow: A parsed workflow document.

    Returns:
        The trigger mapping.
    """
    for key in ("on", True):
        if key in workflow:
            triggers = workflow[key]
            assert isinstance(triggers, dict)
            return triggers
    raise AssertionError(f"workflow declares no triggers: {sorted(map(str, workflow))}")


def _action_references(workflow: Any) -> list[str]:
    """Return every ``uses:`` reference a parsed workflow declares, job-level ones included.

    Prefer this over `_uses_fragments` wherever the trailing version comment does not matter:
    the loader has already resolved the layout, so no spelling of a step can hide a reference
    from a caller counting them.

    Args:
        workflow: A parsed workflow document.

    Returns:
        Each reference as the loader read it, in document order.
    """
    references: list[str] = []
    for job in workflow["jobs"].values():
        if "uses" in job:
            references.append(job["uses"])
        for step in job.get("steps", []):
            if "uses" in step:
                references.append(step["uses"])
    return references


def _uses_fragments(path: Path) -> list[str]:
    """Every ``uses:`` value as written, including the trailing version comment.

    Read line by line rather than from the parsed document because the ``# vX.Y.Z`` label is a
    comment the loader discards, and pinning parity is asserted over the label and the SHA as
    one fragment. Only that parity needs the raw text; a caller after the action names alone
    should walk `_action_references`, which no layout can hide a reference from.

    The list marker carries its own whitespace run rather than being matched as the literal
    ``- uses:``: ``-  uses: owner/action@sha`` is an equally valid step, and a fragment dropped
    here would go missing from a set an assertion then reads as complete.

    The value half is unquoted before reassembly: YAML allows ``uses: "owner/action@sha"``, and
    the quote would otherwise survive into the extracted action name, silently dropping that
    reference out of any parity check while a safe-loader pin test still passes.

    Args:
        path: The workflow file to read.

    Returns:
        One reassembled fragment per ``uses:`` line, in file order.
    """
    fragments: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _USES_RE.match(line.strip())
        if match is None:
            continue
        value, marker, comment = match.group(1).partition("#")
        value = value.strip().strip("'\"")
        fragments.append(f"{value} # {comment.strip()}" if marker else value)
    return fragments


def _named_step(job: dict, name: str) -> dict:
    """Return the step a job declares under ``name``."""
    return next(step for step in job["steps"] if step.get("name") == name)


def _uncommented(line: str) -> str:
    """Return a command line with any shell comment removed, ignoring ``#`` inside quotes."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line


def _commands(step: dict) -> str:
    """Return a step's executable command lines, dropping blanks and commented-out text.

    Comments are stripped because the shell ignores them: a step reading `uv build --wheel
    # --sdist` builds only a wheel, and leaving the comment in place would let it satisfy
    assertions about the arguments the step actually passes.

    Args:
        step: A parsed workflow step.

    Returns:
        The step's ``run:`` text with comments and blank lines removed.
    """
    return "\n".join(
        stripped
        for line in step.get("run", "").splitlines()
        if (stripped := _uncommented(line.strip()))
    )


def _invocations(text: str) -> list[list[str]]:
    """Return the argument list of every command in shell text.

    A line may chain several commands, so read each one separately: `rm -rf dist && uv build`
    still builds, while the arguments in `uv build --wheel && echo --sdist` belong to two
    different programs and only the build's own arguments describe what it produces.

    Comments are stripped per line rather than assumed already gone, so this reads a raw
    ``run:`` body and a body already passed through `_commands` identically.

    Args:
        text: Shell text, typically a step's ``run:`` body.

    Returns:
        One argument list per command, with empty ones dropped.
    """
    argvs = []
    for line in text.splitlines():
        lexer = shlex.shlex(_uncommented(line.strip()), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        current: list[str] = []
        for token in lexer:
            if set(token) <= {"&", "|", ";"}:
                argvs.append(current)
                current = []
            else:
                current.append(token)
        argvs.append(current)
    return [argv for argv in argvs if argv]


def _invokes(argv: list[str], script: str) -> bool:
    """Report whether a command runs ``script`` rather than only naming it.

    ``-c`` and ``-m`` make the interpreter execute inline code or a module instead, which
    demotes any path on the line to an ordinary argument nothing ever runs.

    Args:
        argv: One command's argument list.
        script: The script path the command must execute.

    Returns:
        True when the command runs that script.
    """
    if not argv or argv[0] not in {"uv", "uvx", "python", "python3"}:
        return False
    return script in argv[1:] and set(argv).isdisjoint({"-c", "-m"})
