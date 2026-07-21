"""Generate or verify the SHA-256 manifest for the issue #100 predeclaration checkpoint.

The manifest freezes every checkpoint artifact. ``--write`` regenerates it during checkpoint
authoring; ``--check`` (used by tests and CI) fails when any artifact drifts from the frozen
hashes, enforcing the spec's immutability rule for the remainder of PR A.
"""

import hashlib
import sys
from pathlib import Path

CHECKPOINT_DIR = Path("tests/fixtures/github_ci_checkpoint")
MANIFEST = CHECKPOINT_DIR / "MANIFEST.sha256"


def _entries() -> list[str]:
    """Return sorted ``<sha256>  <relpath>`` lines for every artifact except the manifest."""
    lines: list[str] = []
    for path in sorted(CHECKPOINT_DIR.rglob("*")):
        if path.is_dir() or path == MANIFEST:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(CHECKPOINT_DIR).as_posix()}")
    return lines


def main() -> int:
    """Write or verify the manifest per the single CLI argument."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    current = "\n".join(_entries()) + "\n"
    if mode == "--write":
        MANIFEST.write_text(current)
        return 0
    if not MANIFEST.exists():
        print("checkpoint manifest missing", file=sys.stderr)
        return 1
    if MANIFEST.read_text() != current:
        print("checkpoint artifacts drifted from MANIFEST.sha256", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
