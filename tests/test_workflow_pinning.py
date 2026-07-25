"""Repository-wide supply-chain pinning contract for every GitHub Actions workflow."""

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

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


def _container_images(workflow: Any) -> list[str]:
    images: list[str] = []
    for job in workflow["jobs"].values():
        container = job.get("container")
        if isinstance(container, str):
            images.append(container)
        elif isinstance(container, dict):
            images.append(container["image"])
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
