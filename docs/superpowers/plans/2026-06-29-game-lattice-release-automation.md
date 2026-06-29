# Release Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `vX.Y.Z` release tag a product of a green CI pipeline, and block version drift before merge, replacing the manual tag and smoke steps in `RELEASING.md`.

**Architecture:** A pure version-consistency function in the package (`version_check.py`) is wrapped by a thin script (`scripts/check_version_sync.py`) that runs in pre-commit and CI on every PR. A new merge-triggered `release` job in `.github/workflows/ci.yml` reads `__version__`, verifies any existing tag is healthy, smoke-tests the exact commit over `git+...@<sha>`, then creates and pushes a lightweight `vX.Y.Z` tag.

**Tech Stack:** Python 3.14 (stdlib `tomllib`, `re`), `uv` / `uvx`, GitHub Actions, pre-commit, pytest.

## Global Constraints

These apply to every task. Each task's requirements implicitly include this section.

- **Python 3.14+** (`requires-python = ">=3.14"`); `tomllib` is in the standard library.
- **All dependency and execution go through `uv`** (`uv run --no-sync ...`, `uvx ...`).
- **ruff line length 100**; a module docstring on every module; Google-style docstrings on public functions; **no em-dashes** in any docstring, message, comment, or doc.
- **`typing.Any` / `typing.cast` are forbidden** outside `*_boundary` / `*_parser` style modules. `version_check.py` is a normal pure module, so it must not use them.
- **Custom exceptions extend `ProjectError`** with a `code` (error_types.py). The pure core here raises nothing: a source it cannot parse is reported as a mismatch message, not an exception. No bare `except Exception`.
- **No `datetime.now()`** anywhere in new code.
- **Coverage gate is 80%** over `src/game_lattice` (`pyproject.toml [tool.coverage]`). `version_check.py` lives in that source tree, so it must be fully tested.
- **ruff per-file ignores:** `src/**` enforces `T20` (no `print`), `ANN` (annotations), `PTH`, `S`, `PLR`. `scripts/**` ignores `T201` (so `print` is allowed) and `S`. `tests/**` ignores `S101`, `T201`, `ANN`, `PLR2004`.
- **Branch:** all work lands on the existing `release-automation` branch. The pre-commit `no-commit-to-branch` hook blocks `main`.
- **Do not bump `__version__`.** It stays at `0.3.0` for this slice (see Task 4 rationale).
- Repo URL (used verbatim in the workflow): `https://github.com/Guardantix/game-lattice`.

## File Structure

- Create `src/game_lattice/version_check.py` - pure version-consistency core (no I/O).
- Create `tests/test_version_check.py` - unit tests for the core.
- Create `scripts/check_version_sync.py` - thin I/O wrapper over the core, run by pre-commit and CI.
- Modify `.pre-commit-config.yaml` - add a local `check-version-sync` hook.
- Modify `.github/workflows/ci.yml` - add a `code-quality` step and the new `release` job.
- Modify `RELEASING.md` - drop the manual tag and smoke steps.
- Modify `roadmap.md` - move "Release-tag automation" from Deferred to Shipped.
- Modify `CLAUDE.md` - record the version-sync invariant and the release job.
- Modify `CHANGELOG.md` - add an `## [Unreleased]` section.

---

### Task 1: Pure version-consistency core

**Files:**
- Create: `src/game_lattice/version_check.py`
- Test: `tests/test_version_check.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib only).
- Produces: `check_version_consistency(init_version: str, pyproject_text: str, changelog_text: str) -> list[str]` - returns one message per source disagreeing with `init_version`; empty list means all agree. Also two private helpers `_pyproject_version(str) -> str | None` and `_changelog_version(str) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_version_check.py`:

```python
"""Tests for version_check."""

from game_lattice.version_check import check_version_consistency

_PYPROJECT = '[project]\nname = "game-lattice"\nversion = "0.4.0"\n'
_CHANGELOG = "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n### Added\n\n- thing\n"


def test_all_sources_agree_returns_empty():
    assert check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG) == []


def test_pyproject_disagrees_is_reported():
    pyproject = '[project]\nname = "game-lattice"\nversion = "0.3.0"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]
    assert "0.4.0" in messages[0]


def test_changelog_disagrees_is_reported():
    changelog = "# Changelog\n\n## [0.3.0] - 2026-06-28\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]


def test_both_disagree_returns_two_messages():
    pyproject = '[project]\nversion = "0.1.0"\n'
    changelog = "# Changelog\n\n## [0.2.0]\n"
    messages = check_version_consistency("0.4.0", pyproject, changelog)
    assert len(messages) == 2


def test_unreleased_heading_is_skipped():
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-01\n"
    assert check_version_consistency("0.4.0", _PYPROJECT, changelog) == []


def test_missing_pyproject_version_is_a_mismatch():
    pyproject = '[project]\nname = "game-lattice"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_malformed_pyproject_is_a_mismatch_not_an_error():
    pyproject = "[project"  # unterminated table header, invalid TOML
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_changelog_without_version_heading_is_a_mismatch():
    changelog = "# Changelog\n\nNo releases yet.\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync --group dev pytest tests/test_version_check.py -v`
Expected: collection/import error or FAIL, "No module named 'game_lattice.version_check'" (the module does not exist yet).

- [ ] **Step 3: Write the implementation**

Create `src/game_lattice/version_check.py`:

```python
"""Check that the package version agrees across its declared sources."""

import re
import tomllib

_VERSION_HEADING = re.compile(r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)


def _pyproject_version(pyproject_text: str) -> str | None:
    """Return the [project] version declared in pyproject text, or None if absent."""
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _changelog_version(changelog_text: str) -> str | None:
    """Return the first versioned ``## [X.Y.Z]`` heading in changelog text, or None.

    A non-version heading such as ``## [Unreleased]`` does not match and is skipped,
    so the first real release heading is returned.
    """
    match = _VERSION_HEADING.search(changelog_text)
    return match.group("version") if match else None


def check_version_consistency(
    init_version: str, pyproject_text: str, changelog_text: str
) -> list[str]:
    """Return a message for each version source that disagrees with init_version.

    Args:
        init_version: The canonical package version, ``game_lattice.__version__``.
        pyproject_text: The full text of ``pyproject.toml``.
        changelog_text: The full text of ``CHANGELOG.md``.

    Returns:
        One message per disagreeing source, naming the file and the expected value.
        An empty list means every source matches ``init_version``. A source that
        cannot be parsed is reported as a mismatch rather than raising.
    """
    messages: list[str] = []
    pyproject_version = _pyproject_version(pyproject_text)
    if pyproject_version != init_version:
        messages.append(
            f"pyproject.toml version is {pyproject_version!r}, expected {init_version!r}; "
            f"set [project] version to match game_lattice.__version__."
        )
    changelog_version = _changelog_version(changelog_text)
    if changelog_version != init_version:
        messages.append(
            f"CHANGELOG.md top version heading is {changelog_version!r}, "
            f"expected {init_version!r}; add or fix the '## [{init_version}]' section."
        )
    return messages
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync --group dev pytest tests/test_version_check.py -v`
Expected: PASS, all 8 tests green.

- [ ] **Step 5: Lint, format, and type-check the new files**

Run: `uv run --no-sync ruff check src/game_lattice/version_check.py tests/test_version_check.py && uv run --no-sync ruff format --check src/game_lattice/version_check.py tests/test_version_check.py && uv run --no-sync ty check src/game_lattice/version_check.py`
Expected: all pass, no findings. If `ruff format --check` reports a diff, run `uv run --no-sync ruff format src/game_lattice/version_check.py tests/test_version_check.py` and re-run.

- [ ] **Step 6: Commit**

```bash
git add src/game_lattice/version_check.py tests/test_version_check.py
git commit -m "feat: pure version-consistency core"
```

---

### Task 2: Version-sync script and PR-time wiring

**Files:**
- Create: `scripts/check_version_sync.py`
- Modify: `.pre-commit-config.yaml` (add a local hook after `check-typing-boundaries`)
- Modify: `.github/workflows/ci.yml` (add a step at the end of the `code-quality` job)

**Interfaces:**
- Consumes: `game_lattice.__version__` and `game_lattice.version_check.check_version_consistency` (Task 1).
- Produces: a `scripts/check_version_sync.py` with a `main() -> None` entry point that exits 1 on any mismatch, 0 otherwise. No new Python symbols other tasks depend on.

- [ ] **Step 1: Write the script**

Create `scripts/check_version_sync.py`:

```python
#!/usr/bin/env python3
"""Verify __version__, pyproject.toml, and CHANGELOG.md declare the same version."""

import sys
from pathlib import Path

from game_lattice import __version__
from game_lattice.version_check import check_version_consistency

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    """Read the three version sources and exit non-zero on any disagreement."""
    pyproject_text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog_text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    messages = check_version_consistency(__version__, pyproject_text, changelog_text)
    for message in messages:
        print(message, file=sys.stderr)
    sys.exit(1 if messages else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script against the real repo to verify it passes**

Run: `uv run --no-sync python scripts/check_version_sync.py; echo "exit: $?"`
Expected: no output, `exit: 0` (the repo's `__version__`, `pyproject.toml`, and top `## [0.3.0]` heading all agree).

- [ ] **Step 3: Manually verify the failure path**

Run a one-off check that a wrong version is reported (this does not modify any file):

```bash
uv run --no-sync python -c "
from game_lattice.version_check import check_version_consistency
from pathlib import Path
py = Path('pyproject.toml').read_text(encoding='utf-8')
cl = Path('CHANGELOG.md').read_text(encoding='utf-8')
msgs = check_version_consistency('9.9.9', py, cl)
print(len(msgs), 'mismatch(es)')
assert len(msgs) == 2, msgs
"
```
Expected: `2 mismatch(es)` (both sources disagree with the fake `9.9.9`).

- [ ] **Step 4: Add the pre-commit hook**

In `.pre-commit-config.yaml`, inside the `- repo: local` block, add this hook immediately after the existing `check-typing-boundaries` hook (after its `pass_filenames: false` line):

```yaml
      - id: check-version-sync
        name: check version sync
        entry: uv run --locked --group dev python scripts/check_version_sync.py
        language: system
        always_run: true
        pass_filenames: false
```

Note: `always_run: true` (rather than the sibling hook's `types: [python]`) so a commit that edits only `pyproject.toml` or `CHANGELOG.md`, with no Python file staged, is still checked.

- [ ] **Step 5: Run the pre-commit hook to verify it passes**

Run: `uv run --no-sync pre-commit run check-version-sync --all-files`
Expected: `check version sync.....Passed`.

- [ ] **Step 6: Wire the check into CI**

In `.github/workflows/ci.yml`, in the `code-quality` job, add one line immediately after the existing typing-boundaries step:

```yaml
      - run: uv run --no-sync python scripts/check_typing_boundaries.py src/
      - run: uv run --no-sync python scripts/check_version_sync.py
```

(The first line already exists; add only the second.)

- [ ] **Step 7: Validate the workflow YAML still parses**

Run: `uv run --no-sync python -c "from ruamel.yaml import YAML; YAML(typ='safe').load(open('.github/workflows/ci.yml')); print('yaml ok')"`
Expected: `yaml ok`.

- [ ] **Step 8: Commit**

```bash
git add scripts/check_version_sync.py .pre-commit-config.yaml .github/workflows/ci.yml
git commit -m "feat: version-sync guard in pre-commit and CI"
```

---

### Task 3: Merge-triggered release job

**Files:**
- Modify: `.github/workflows/ci.yml` (append the `release` job after the `tests` job)

**Interfaces:**
- Consumes: `scripts/check_version_sync.py` (Task 2), `game_lattice.__version__`, the `check` / `lint` / `init` CLI subcommands (already shipped).
- Produces: a `release` job. No Python symbols.

- [ ] **Step 1: Append the release job**

In `.github/workflows/ci.yml`, after the final `tests` job (at the end of the file), add:

```yaml
  release:
    name: Release
    needs: [code-quality, tests, security-scan]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - run: uv python install 3.14
      - run: uv sync --locked --group dev

      - name: Determine target tag
        id: target
        run: |
          version="$(uv run --no-sync python -c 'import game_lattice; print(game_lattice.__version__)')"
          echo "version=${version}" >> "$GITHUB_OUTPUT"
          echo "tag=v${version}" >> "$GITHUB_OUTPUT"

      - name: Tag-health gate
        id: gate
        run: |
          tag='${{ steps.target.outputs.tag }}'
          version='${{ steps.target.outputs.version }}'
          git fetch --tags --force
          if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
            tagged="$(git show "${tag}:src/game_lattice/__init__.py" | sed -n 's/^__version__ = "\(.*\)"/\1/p')"
            if [ "${tagged}" = "${version}" ]; then
              echo "Tag ${tag} already exists at version ${version}; no-op."
              echo "proceed=false" >> "$GITHUB_OUTPUT"
            else
              echo "::error::Tag ${tag} exists but points at version '${tagged}', not ${version}."
              exit 1
            fi
          else
            echo "proceed=true" >> "$GITHUB_OUTPUT"
          fi

      - name: Re-assert version sync
        if: steps.gate.outputs.proceed == 'true'
        run: uv run --no-sync python scripts/check_version_sync.py

      - name: Smoke-test the commit
        if: steps.gate.outputs.proceed == 'true'
        run: |
          ref="git+https://github.com/Guardantix/game-lattice@${{ github.sha }}"
          uvx --python 3.14 --from "${ref}" game-lattice check
          uvx --python 3.14 --from "${ref}" game-lattice lint
          workdir="$(mktemp -d)"
          ( cd "${workdir}" && uvx --python 3.14 --from "${ref}" game-lattice init )

      - name: Create and push the tag
        if: steps.gate.outputs.proceed == 'true'
        run: |
          tag='${{ steps.target.outputs.tag }}'
          git tag "${tag}" '${{ github.sha }}'
          git push origin "${tag}"

      - name: Confirm pinned ref resolves
        if: steps.gate.outputs.proceed == 'true'
        run: |
          tag='${{ steps.target.outputs.tag }}'
          uvx --python 3.14 --from "git+https://github.com/Guardantix/game-lattice@${tag}" game-lattice --version
```

- [ ] **Step 2: Validate the workflow YAML parses**

Run: `uv run --no-sync python -c "from ruamel.yaml import YAML; YAML(typ='safe').load(open('.github/workflows/ci.yml')); print('yaml ok')"`
Expected: `yaml ok`.

- [ ] **Step 3: Test the tag-health extraction logic locally (no-op path)**

The gate's core is `git show <tag>:src/game_lattice/__init__.py` plus a `sed` extraction. Run it against the real `v0.3.0` tag to prove the healthy no-op path:

```bash
tag=v0.3.0
version="${tag#v}"
tagged="$(git show "${tag}:src/game_lattice/__init__.py" | sed -n 's/^__version__ = "\(.*\)"/\1/p')"
if [ "${tagged}" = "${version}" ]; then echo "GATE: healthy existing tag -> proceed=false (no-op)"; else echo "GATE: MISMATCH (${tagged} != ${version})"; fi
```
Expected: `GATE: healthy existing tag -> proceed=false (no-op)`.

- [ ] **Step 4: Review the job against the spec by reading it back**

Run: `git diff .github/workflows/ci.yml`
Confirm by inspection: `needs` lists all three existing jobs; `if` gates on push to `main`; `permissions: contents: write` is present; every step after the gate carries `if: steps.gate.outputs.proceed == 'true'`; the smoke step runs `check`, `lint`, and `init`; the tag is lightweight (`git tag "${tag}" <sha>`, no `-a`).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "feat: merge-triggered release job"
```

---

### Task 4: Documentation and changelog

**Files:**
- Modify: `RELEASING.md`
- Modify: `roadmap.md`
- Modify: `CLAUDE.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing in code. Keeps `__version__` at `0.3.0` so the merge no-ops.
- Produces: no code symbols.

Rationale for not bumping the version: leaving `__version__` at `0.3.0` means that when this branch merges to `main`, the new `release` job finds the existing healthy `v0.3.0` tag and exits as a no-op. That is the safest first run of brand-new automation. The first real automated tag-cut happens on a later deliberate bump. Accordingly, the changelog notes go under `## [Unreleased]`, and the version-sync guard still passes because its regex skips `[Unreleased]` and matches the top versioned heading `## [0.3.0]`, which equals `__version__`.

- [ ] **Step 1: Replace `RELEASING.md`**

Overwrite `RELEASING.md` with:

```markdown
# Releasing game-lattice

game-lattice is distributed from git, not from PyPI. The `init` command prints
pre-commit and CI snippets that pin `uvx --from git+...@vX.Y.Z`, so a release is
only complete once the matching tag exists and resolves. The version bump is the
human step; CI cuts and verifies the tag on merge, so a half-done release (code
merged but no tag, or a tag without the version bump) cannot land.

## Checklist

1. Bump the version to the new `X.Y.Z` in both locations:
   - `src/game_lattice/__init__.py` (`__version__`)
   - `pyproject.toml` (`version`)
2. Run `uv lock` and commit the refreshed `uv.lock`.
3. Add a `## [X.Y.Z]` section to `CHANGELOG.md` (rename the `## [Unreleased]`
   section if you have been accumulating notes there).
4. Open the PR and get it green. The `check-version-sync` gate fails the PR if the
   three version sources disagree, so fix any drift before merge.
5. Merge to `main`. On that push, the `release` job:
   - verifies version sync again,
   - smoke-tests the exact commit over `git+...@<sha>`, running `check`, `lint`,
     and `init`,
   - creates and pushes the lightweight `vX.Y.Z` tag,
   - confirms the pinned `@vX.Y.Z` ref resolves.

   An ordinary merge that does not change the version is a safe no-op: the job
   confirms the existing tag points at a commit of the matching version.

If the release job fails after the tag is pushed, do not move the tag: cut
`X.Y.(Z+1)` instead, because a moved tag breaks adopters already pinned to the
old one.

The tag must point at a commit that contains `check`, `lint`, and `init`, so the
gates run and adopters can run `game-lattice init` from the same ref. The release
job's smoke step enforces exactly this before the tag is created.
```

- [ ] **Step 2: Update `roadmap.md`**

In the `## Shipped` list, add this entry after the `lint slice` bullet:

```markdown
- **release automation**. The version-sync guard (`scripts/check_version_sync.py`) fails any PR whose
  `__version__`, `pyproject.toml`, and top `CHANGELOG.md` entry disagree, and a merge-triggered CI
  `release` job creates and smoke-tests the `vX.Y.Z` tag (running `check`, `lint`, and `init` against
  the pinned ref), so the tag is a product of a green pipeline. Spec:
  `docs/superpowers/specs/2026-06-29-game-lattice-release-automation-design.md`.
```

Then, in `## Deferred enhancements (no spec yet)`, delete the `Release-tag automation.` bullet (the two lines beginning `- Release-tag automation.`), leaving only the `Display-prefix lint.` bullet.

- [ ] **Step 3: Update `CLAUDE.md`**

In the `## Commands` fenced block, add this line after the existing `check_typing_boundaries.py` line:

```bash
uv run --group dev python scripts/check_version_sync.py    # version-consistency guard
```

Then, in `## Project-specific invariants`, add this bullet after the `No datetime.now()` bullet:

```markdown
- **Version sync.** `__version__` (`src/game_lattice/__init__.py`), the `pyproject.toml` `version`,
  and the top `## [X.Y.Z]` `CHANGELOG.md` heading must agree. The pure core is `version_check.py`;
  `scripts/check_version_sync.py` wraps it and runs in pre-commit and CI. On merge to `main` the
  `release` job in `.github/workflows/ci.yml` verifies sync, smoke-tests the commit, and cuts the
  lightweight `vX.Y.Z` tag. Release flow: `RELEASING.md`.
```

- [ ] **Step 4: Update `CHANGELOG.md`**

Insert an `## [Unreleased]` section immediately after the `Keep a Changelog` intro line and before `## [0.3.0] - 2026-06-28`:

```markdown
## [Unreleased]

### Added

- Version-consistency guard (`scripts/check_version_sync.py`, pure core `version_check.py`) wired into pre-commit and CI: `__version__`, `pyproject.toml`, and the top `CHANGELOG.md` entry must agree.
- Merge-triggered `release` CI job that creates and verifies the lightweight `vX.Y.Z` tag, smoke-testing `check`, `lint`, and `init` against the pinned ref.

```

- [ ] **Step 5: Verify the version-sync guard still passes after the changelog edit**

Run: `uv run --no-sync python scripts/check_version_sync.py; echo "exit: $?"`
Expected: no output, `exit: 0` (the regex skips `[Unreleased]` and matches `## [0.3.0]`, which equals `__version__`).

- [ ] **Step 6: Run the full suite and all hooks**

Run: `uv run --no-sync --group dev pytest && uv run --no-sync pre-commit run --all-files`
Expected: pytest passes with coverage at or above 80%; every pre-commit hook reports Passed.

- [ ] **Step 7: Commit**

```bash
git add RELEASING.md roadmap.md CLAUDE.md CHANGELOG.md
git commit -m "docs: automate the release tag in RELEASING, roadmap, CLAUDE, CHANGELOG"
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Implemented by |
|---|---|
| 4. Pure version-consistency core | Task 1 (`version_check.py` + tests) |
| 5. Thin script wrapper | Task 2, Step 1-3 |
| 6. PR-time wiring (CI + pre-commit) | Task 2, Steps 4-7 |
| 7. Release job (all 6 steps incl. tag-health, SHA smoke, lightweight tag, post-tag confirm) | Task 3 |
| 8. Data flow / exit behavior | Task 3 (gate `if` conditions) |
| 9. Error handling (drift, SHA smoke fail, healthy vs corrupt tag, no loop) | Task 2 (drift), Task 3 (gate + smoke) |
| 10. Conventions | Global Constraints + Task 1 Step 5 |
| 11. Testing | Task 1 (unit), Task 3 Step 3 (gate logic) |
| 12. Non-goals | Honored: no GitHub Release, no PyPI, no version bump, no changelog generation |
| 13. Acceptance | Task 2 (drift gate), Task 3 (forgotten tag, corrupt tag, broken ref) |

No spec section is unimplemented.

**2. Placeholder scan**: every step contains exact file paths, full code or YAML, exact commands, and expected output. No "TBD", "add error handling", or "similar to Task N".

**3. Type consistency**: `check_version_consistency(init_version, pyproject_text, changelog_text) -> list[str]` is defined in Task 1 and consumed with the same name and argument order by `scripts/check_version_sync.py` in Task 2. `__version__` is imported from `game_lattice` in both Task 2 (script) and Task 3 (workflow `python -c`). The gate output key `proceed` is written and read consistently within Task 3. The `${{ steps.target.outputs.tag }}` / `version` references match the step `id: target` outputs.
