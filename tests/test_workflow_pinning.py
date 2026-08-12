"""Repository-wide supply-chain pinning contract for every GitHub Actions workflow."""

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from doc_lattice.constants import CHECKOUT_REF, SETUP_UV_REF

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIR = _ROOT / ".github/workflows"
_SHA_PINNED_USES_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _workflows() -> dict[str, Any]:
    loader = YAML(typ="safe")
    paths = sorted(path for path in _WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"})
    return {path.name: loader.load(path.read_text(encoding="utf-8")) for path in paths}


def _action_references(workflow: Any) -> list[str]:
    references: list[str] = []
    for job in workflow["jobs"].values():
        if "uses" in job:
            references.append(job["uses"])
        for step in job.get("steps", []):
            if "uses" in step:
                references.append(step["uses"])
    return references


def _image_of(container: Any) -> str | None:
    if isinstance(container, str):
        return container
    if isinstance(container, dict):
        return container["image"]
    return None


def _container_images(workflow: Any) -> list[str]:
    """Collect job containers and service containers, both of which the runner pulls."""
    images: list[str] = []
    for job in workflow["jobs"].values():
        for container in (job.get("container"), *job.get("services", {}).values()):
            image = _image_of(container)
            if image is not None:
                images.append(image)
    return images


def test_every_workflow_file_is_discovered():
    assert set(_workflows()) >= {"ci.yml", "claude.yml"}


def test_every_workflow_action_is_pinned_to_a_commit_sha():
    workflows = _workflows()
    found = [ref for workflow in workflows.values() for ref in _action_references(workflow)]
    assert found
    unpinned = [
        f"{name}: {ref}"
        for name, workflow in workflows.items()
        for ref in _action_references(workflow)
        if not _SHA_PINNED_USES_RE.match(ref)
    ]
    assert unpinned == []


def test_shipped_action_pins_match_the_pins_this_repository_runs():
    # A SHA we ship to users cannot go stale on its own the way the floating tags it replaced
    # could not, so the only thing keeping it current is that it is the same pin our own gates
    # depend on. Tying the two together makes an upgrade here fail until constants.py follows,
    # which is what stops scaffolded repositories from being frozen a release behind us.
    shipped = {"actions/checkout": CHECKOUT_REF, "astral-sh/setup-uv": SETUP_UV_REF}
    divergent = [
        f"{name}: {ref}"
        for name, workflow in _workflows().items()
        for ref in _action_references(workflow)
        for action, _, sha in [ref.partition("@")]
        if action in shipped and sha != shipped[action]
    ]
    assert divergent == []


def test_every_workflow_container_image_is_pinned_to_a_digest():
    workflows = _workflows()
    found = [image for workflow in workflows.values() for image in _container_images(workflow)]
    assert found
    unpinned = [
        f"{name}: {image}"
        for name, workflow in workflows.items()
        for image in _container_images(workflow)
        if "@sha256:" not in image
    ]
    assert unpinned == []
