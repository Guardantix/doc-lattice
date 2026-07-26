"""Repository-wide contract that doc-lattice can audit its own GitHub Actions workflows."""

from pathlib import Path

from doc_lattice.github_ci.audit import audit_global_workflows
from doc_lattice.github_ci.workflow_parser import parse_workflow

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIR = _ROOT / ".github/workflows"


def _documents() -> tuple:
    paths = sorted(path for path in _WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"})
    return tuple(parse_workflow(path, path.read_text(encoding="utf-8")) for path in paths)


def test_repository_workflows_certify_under_the_retained_shell_scanner():
    """Every repository workflow must scan completely under the AD-17 marker policy.

    A marker-bearing command that is not a certified doc-lattice invocation fails closed, so
    this repository's own workflows must avoid uncertified marker-bearing commands such as bare
    shell assignments that embed the distribution name.
    """
    audit_global_workflows(_documents())
