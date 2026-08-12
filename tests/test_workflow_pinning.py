"""Repository-wide supply-chain pinning contract for every GitHub Actions workflow."""

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from doc_lattice.constants import CHECKOUT_USES, SETUP_UV_USES

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIR = _ROOT / ".github/workflows"
_SHA_PINNED_USES_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")
# The composed fragments, not their SHA halves: the release tag is rendered as a trailing
# `# vX.Y.Z` comment, which the safe loader discards, so comparing parsed refs would let a
# bumped SHA keep a stale tag and mislabel the commit everywhere the pin ships.
_SHIPPED_USES = {"actions/checkout": CHECKOUT_USES, "astral-sh/setup-uv": SETUP_UV_USES}


def _workflow_paths() -> list[Path]:
    return sorted(path for path in _WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"})


def _workflows() -> dict[str, Any]:
    loader = YAML(typ="safe")
    return {path.name: loader.load(path.read_text(encoding="utf-8")) for path in _workflow_paths()}


def _uses_fragments(path: Path) -> list[str]:
    """Every `uses:` value as written, including the trailing version comment.

    The value half is unquoted before reassembly: YAML allows `uses: "owner/action@sha"`, and
    the quote would otherwise survive into the extracted action name, silently dropping that
    reference out of the fragment parity check while the safe-loader pin test still passes.
    """
    fragments: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith(("uses:", "- uses:")):
            continue
        value, marker, comment = stripped.partition("uses:")[2].partition("#")
        value = value.strip().strip("'\"")
        fragments.append(f"{value} # {comment.strip()}" if marker else value)
    return fragments


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
    # Comparing whole fragments also pins the `# vX.Y.Z` label to the SHA it names.
    divergent = [
        f"{path.name}: {fragment}"
        for path in _workflow_paths()
        for fragment in _uses_fragments(path)
        for action, _, _ in [fragment.partition("@")]
        if action in _SHIPPED_USES and fragment != _SHIPPED_USES[action]
    ]
    assert divergent == []


def test_every_shipped_action_pin_is_exercised_by_this_repository():
    # The parity test above only compares references it happens to find, so it would pass
    # vacuously for any action this repository stopped referencing directly, and the pin would
    # then freeze at whatever release it held while still shipping to every scaffolded repo.
    # This is the guard that turns that silent lapse into a failure. Parsed references suffice
    # here: the trailing version comment matters only to the parity test above, and everything
    # after the `@` is discarded anyway.
    referenced = {
        reference.partition("@")[0]
        for workflow in _workflows().values()
        for reference in _action_references(workflow)
    }
    assert referenced >= set(_SHIPPED_USES)


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
