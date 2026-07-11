# doc-lattice Init Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `doc-lattice init` command that scaffolds `.doc-lattice.yml` and prints pre-commit and CI codegen for a repo adopting doc-lattice, then ship it as the 0.2.0 release.

**Architecture:** A new pure, filesystem-free module `scaffold.py` builds three strings (the config text and two codegen snippets) from typed inputs. A single `init` command in `cli.py` is the only new disk-touching code: it validates flags, builds the scaffold, writes the config through a crash-safe no-overwrite helper (`_atomic_create`), and prints the snippets. This extends the existing pure-core / impure-edge split exactly as `render` and `reconcile` already do.

**Tech Stack:** Python 3.14+, `typer` (CLI), `pydantic` (the `Config` model), `ruamel.yaml` (config serialization), `uv` (toolchain), `pytest` (tests). All already in the project.

**Source spec:** `docs/superpowers/specs/2026-06-28-doc-lattice-init-design.md` is the source of truth.

## Global Constraints

Every task's requirements implicitly include these (copied from the spec and CLAUDE.md):

- Python `requires-python >= 3.14`; all commands run through `uv`.
- ruff line length 100. Module docstring on every module. Google-style docstrings on public functions. **No em-dashes** anywhere in drafted content (docstrings, messages, comments, docs).
- Untyped-to-typed boundary: `typing.Any` and `typing.cast` are allowed only in `*_parser`/`*_boundary`/`*_validator`-style modules. `scaffold.py` is NOT one, so it uses neither. (`ruamel.yaml` being untyped is fine; the rule only flags `typing.Any`/`typing.cast` literally in our source.)
- All custom exceptions extend `ProjectError` and carry a `code`. No bare `except Exception`/`except BaseException`.
- Constants use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py`; do not duplicate a string literal that should be a named constant.
- Coverage stays at or above the 80 percent gate (`fail_under = 80`).
- A pre-commit hook runs ruff (`--fix`), ruff-format, `ty`, the typing-boundary check, and detect-secrets on every commit, and blocks direct commits to `main`. Work happens on the `feat/init` branch. If a hook auto-fixes a file, re-stage and re-commit.
- Test files mirror sources (`src/doc_lattice/foo.py` -> `tests/test_foo.py`) and use `tmp_path` for filesystem work.

## File Structure

| File | Disposition | Responsibility |
|---|---|---|
| `src/doc_lattice/scaffold.py` | Create | Pure generators: `Scaffold` value, `build_scaffold`, `render_config`, `render_precommit`, `render_ci`, the repo URL constant. No I/O. |
| `tests/test_scaffold.py` | Create | Pure tests for `scaffold.py`, including hostile-scalar round-trips through the real `Config` model. |
| `src/doc_lattice/cli.py` | Modify | Add `_atomic_create` helper, `_validate_init_flags`, and the `init` command. Add imports. |
| `tests/test_cli.py` | Modify | Add `_atomic_create` unit tests and `init` command tests. |
| `src/doc_lattice/__init__.py` | Modify | Bump `__version__` to `0.2.0`. |
| `pyproject.toml` | Modify | Bump `version` to `0.2.0`. |
| `uv.lock` | Modify | Refreshed by `uv lock` after the version bump. |
| `CHANGELOG.md` | Modify | Add the `## [0.2.0]` release entry. |
| `RELEASING.md` | Create | Release checklist making the version tag an atomic part of cutting a release. |
| `README.md` | Modify | Add an adopter onboarding section for `init`. |
| `roadmap.md` | Modify | Move `linear` and `init` to shipped; add the release-automation deferred item. |

---

### Task 1: Pure `scaffold.py` module

**Files:**
- Create: `src/doc_lattice/scaffold.py`
- Test: `tests/test_scaffold.py`

**Interfaces:**
- Consumes: `doc_lattice.config.Config` (in tests only, for round-trip validation); `ruamel.yaml.YAML`.
- Produces:
  - `DOC_LATTICE_REPO_URL: str` = `"https://github.com/Guardantix/doc-lattice"`
  - `class Scaffold` (frozen dataclass) with `config_text: str`, `precommit_text: str`, `ci_text: str`
  - `render_config(docs_roots: tuple[str, ...], linear_team: str | None) -> str`
  - `render_precommit(rev: str) -> str`
  - `render_ci(rev: str) -> str`
  - `build_scaffold(docs_roots: tuple[str, ...], linear_team: str | None, rev: str) -> Scaffold`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scaffold.py`:

```python
"""Tests for the init scaffold generators."""

import pytest
from ruamel.yaml import YAML

from doc_lattice.config import Config
from doc_lattice.scaffold import (
    DOC_LATTICE_REPO_URL,
    build_scaffold,
    render_config,
)


def _load(text: str) -> Config:
    parsed = YAML(typ="safe").load(text)
    return Config.model_validate(parsed)


def test_render_config_default_has_docs_active_and_keys_commented():
    text = render_config(("docs",), None)
    assert "docs_roots:" in text
    assert "- docs" in text
    assert "# ignore_globs:" in text
    assert "# linear_team: my-team-slug" in text
    assert "# binding_layers: null" in text
    cfg = _load(text)
    assert cfg.docs_roots == ["docs"]
    assert cfg.linear_team is None


def test_render_config_lists_multiple_roots():
    text = render_config(("design", "lore"), None)
    assert _load(text).docs_roots == ["design", "lore"]


def test_render_config_bakes_linear_team_and_drops_comment():
    text = render_config(("docs",), "PC")
    assert "linear_team: PC" in text
    assert "# linear_team: my-team-slug" not in text
    assert _load(text).linear_team == "PC"


@pytest.mark.parametrize("value", ["1.0", "#hash", "a: b", "*anchor", "true", "0755"])
def test_render_config_quotes_hostile_linear_team(value):
    cfg = _load(render_config(("docs",), value))
    assert cfg.linear_team == value


@pytest.mark.parametrize("root", ["1.0", "#hash", "weird:name"])
def test_render_config_quotes_hostile_docs_root(root):
    cfg = _load(render_config((root,), None))
    assert cfg.docs_roots == [root]


def test_snippets_pin_rev_url_and_python():
    s = build_scaffold(("docs",), None, "v0.2.0")
    for text in (s.precommit_text, s.ci_text):
        assert "@v0.2.0" in text
        assert DOC_LATTICE_REPO_URL in text
        assert "--python 3.14" in text
    assert "repo: local" in s.precommit_text
    assert "pass_filenames: false" in s.precommit_text
    assert "actions/checkout@v4" in s.ci_text
    assert "astral-sh/setup-uv@v6" in s.ci_text
    assert "linear" not in s.ci_text  # only check runs in the generated CI
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_scaffold.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc_lattice.scaffold'`.

- [ ] **Step 3: Write the implementation**

Create `src/doc_lattice/scaffold.py`:

```python
"""Generate the config and codegen artifacts for the init command.

Pure and filesystem-free: every function returns a string built from typed
inputs, so the module is tested with no I/O. The init command in cli.py does the
disk write and the printing.
"""

import io
from dataclasses import dataclass

from ruamel.yaml import YAML

DOC_LATTICE_REPO_URL = "https://github.com/Guardantix/doc-lattice"
PYTHON_PIN = "3.14"

_CONFIG_HEADER = f"# doc-lattice configuration. See {DOC_LATTICE_REPO_URL}\n"
_COMMENTED_IGNORE = '# ignore_globs:\n#   - "**/superpowers/plans/**"\n'
_COMMENTED_LINEAR = "# linear_team: my-team-slug\n"
_COMMENTED_BINDING = "# binding_layers: null\n"


@dataclass(frozen=True, slots=True)
class Scaffold:
    """The three artifacts init produces: one written, two printed."""

    config_text: str
    precommit_text: str
    ci_text: str


def _check_invocation(rev: str) -> str:
    """Return the uvx command both gates run, pinned to rev and Python 3.14."""
    return (
        f"uvx --python {PYTHON_PIN} --from git+{DOC_LATTICE_REPO_URL}@{rev} "
        "doc-lattice check"
    )


def render_config(docs_roots: tuple[str, ...], linear_team: str | None) -> str:
    """Render .doc-lattice.yml with active keys serialized and optionals commented.

    The active block is dumped through ruamel.yaml so every value is quoted and
    typed by the library, never string-interpolated. The header comment and the
    commented-out example keys are static text.

    Args:
        docs_roots: The docs roots to write as the active docs_roots list.
        linear_team: The team slug to bake in, or None to leave it commented.

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
    """Render the repo: local pre-commit hook that runs doc-lattice check."""
    return (
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: doc-lattice-check\n"
        "        name: doc-lattice check\n"
        f"        entry: {_check_invocation(rev)}\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
    )


def render_ci(rev: str) -> str:
    """Render the GitHub Actions workflow that runs doc-lattice check."""
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
        f"      - run: {_check_invocation(rev)}\n"
    )


def build_scaffold(
    docs_roots: tuple[str, ...], linear_team: str | None, rev: str
) -> Scaffold:
    """Build all three init artifacts from typed inputs.

    Args:
        docs_roots: The docs roots for the config's docs_roots list.
        linear_team: The team slug to bake in, or None.
        rev: The git ref the snippets pin, for example "v0.2.0".

    Returns:
        A Scaffold holding the config text and the two codegen snippets.
    """
    return Scaffold(
        config_text=render_config(docs_roots, linear_team),
        precommit_text=render_precommit(rev),
        ci_text=render_ci(rev),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_scaffold.py -q`
Expected: PASS (all tests green).

- [ ] **Step 5: Lint, type, and boundary check the new module**

Run: `uv run --group dev ruff check src/doc_lattice/scaffold.py tests/test_scaffold.py && uv run --group dev ty check src && uv run --group dev python scripts/check_typing_boundaries.py src`
Expected: ruff clean, `ty` clean, boundary check prints `PASS`.

- [ ] **Step 6: Commit**

```bash
git add src/doc_lattice/scaffold.py tests/test_scaffold.py
git commit -m "feat: add scaffold module for init codegen"
```

---

### Task 2: Crash-safe `_atomic_create` write helper

**Files:**
- Modify: `src/doc_lattice/cli.py` (add helper + imports near the existing `_atomic_write`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `cli._atomic_create(path: Path, text: str) -> None`. Writes `text` to `path`, raising `FileExistsError` if `path` already exists and `OSError` on any other write/link failure. Never overwrites; never leaves a partial file or temp litter.

- [ ] **Step 1: Add the `os` import to the test file**

In `tests/test_cli.py`, the imports currently start with `import json`. Add `import os` directly above it so the block reads:

```python
import json
import os
from pathlib import Path
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_atomic_create_writes_when_absent(tmp_path: Path):
    target = tmp_path / ".doc-lattice.yml"
    cli_mod._atomic_create(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_atomic_create_refuses_existing_and_preserves_it(tmp_path: Path):
    target = tmp_path / ".doc-lattice.yml"
    target.write_text("original\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        cli_mod._atomic_create(target, "new\n")
    assert target.read_text(encoding="utf-8") == "original\n"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_atomic_create_leaves_nothing_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / ".doc-lattice.yml"

    def boom(_src, _dst):
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError, match="link failed"):
        cli_mod._atomic_create(target, "data\n")
    assert not target.exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cli.py -k atomic_create -q`
Expected: FAIL with `AttributeError: module 'doc_lattice.cli' has no attribute '_atomic_create'`.

- [ ] **Step 4: Write the implementation**

In `src/doc_lattice/cli.py`, add `import os` and `import tempfile` to the top import block (alongside `import json`). Then add this helper directly below the existing `_atomic_write` function (near the end of the file):

```python
def _atomic_create(path: Path, text: str) -> None:
    """Create path with text, crash-safe and never overwriting an existing file.

    Writes to a unique temp file in the same directory, fsyncs it so the bytes
    are durable, then publishes by hard-linking the temp onto the final path.
    os.link is atomic and raises FileExistsError if the target already exists, so
    the final path only ever appears complete, never empty or partial. The temp
    is always removed, so a failed run leaves no litter.

    Raises:
        FileExistsError: If path already exists.
        OSError: If the write or the link fails for another reason.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cli.py -k atomic_create -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc_lattice/cli.py tests/test_cli.py
git commit -m "feat: add crash-safe _atomic_create write helper"
```

---

### Task 3: The `init` command

**Files:**
- Modify: `src/doc_lattice/cli.py` (imports, `_validate_init_flags`, the `init` command)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `scaffold.build_scaffold` (Task 1); `cli._atomic_create` (Task 2); `config.DEFAULT_CONFIG_NAME`; `error_types.ConfigError`; `text_utils.strip_control_chars`; `doc_lattice.__version__`.
- Produces: the `init` Typer command (`doc-lattice init [--docs-root ...] [--linear-team ...]`) and `cli._validate_init_flags(docs_roots: tuple[str, ...], linear_team: str | None) -> None`.

- [ ] **Step 1: Wire up the imports**

In `src/doc_lattice/cli.py`, update three existing import lines:

Change `from .config import load_config` to:

```python
from .config import DEFAULT_CONFIG_NAME, load_config
```

Change `from .error_types import ProjectError, UnreadableDocError` to:

```python
from .error_types import ConfigError, ProjectError, UnreadableDocError
```

Add these two imports alongside the other `from .` imports (keep import order; ruff `I` will sort on commit):

```python
from .scaffold import build_scaffold
from .text_utils import strip_control_chars
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_init_writes_config_and_prints_codegen(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    config = (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8")
    assert "docs_roots:" in config
    assert "- docs" in config
    assert ".pre-commit-config.yaml" in result.stdout
    assert ".github/workflows/doc-lattice.yml" in result.stdout
    assert f"@v{__version__}" in result.stdout


def test_init_skips_existing_config_but_still_prints(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text("SENTINEL\n", encoding="utf-8")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == "SENTINEL\n"
    assert ".github/workflows/doc-lattice.yml" in result.stdout


def test_init_bakes_flag_values(tmp_path: Path, monkeypatch):
    from doc_lattice.config import load_config  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["init", "--docs-root", "design", "--docs-root", "lore", "--linear-team", "PC"]
    )
    assert result.exit_code == 0
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["design", "lore"]
    assert project.config.linear_team == "PC"


@pytest.mark.parametrize("bad", ["/etc", "../escape"])
def test_init_rejects_unsafe_docs_root(tmp_path: Path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", bad])
    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()


def test_init_rejects_control_character_in_flag(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--linear-team", "a\nb"])
    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()


def test_init_crash_during_link_leaves_clean_state(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_link = os.link

    def boom(_src, _dst):
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", boom)
    assert runner.invoke(app, ["init"]).exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())

    monkeypatch.setattr(os, "link", real_link)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").exists()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cli.py -k init -q`
Expected: FAIL (no `init` command; typer reports a usage error / exit 2 mismatch, or `_validate_init_flags` is undefined).

- [ ] **Step 4: Write the validation helper**

In `src/doc_lattice/cli.py`, add this helper above the `init` command (near the other module-level helpers):

```python
def _validate_init_flags(docs_roots: tuple[str, ...], linear_team: str | None) -> None:
    """Reject flag values that would corrupt the generated config.

    Args:
        docs_roots: The docs roots from --docs-root (or the default).
        linear_team: The --linear-team value, or None.

    Raises:
        ConfigError: If a value is empty or holds a control character, or a docs
            root is absolute or contains a parent reference.
    """
    values = list(docs_roots)
    if linear_team is not None:
        values.append(linear_team)
    for value in values:
        if not value or strip_control_chars(value) != value:
            msg = f"flag value {value!r} is empty or contains a control character"
            raise ConfigError(msg)
    for root in docs_roots:
        if Path(root).is_absolute() or ".." in Path(root).parts:
            msg = (
                f"--docs-root {root!r} must be a relative path inside the project, "
                "without '..' or a leading slash"
            )
            raise ConfigError(msg)
```

- [ ] **Step 5: Write the `init` command**

In `src/doc_lattice/cli.py`, add the command alongside the other `@app.command()` functions (for example after `linear`, before the `_atomic_write`/`_atomic_create` helpers):

```python
@app.command()
def init(
    docs_root: Annotated[
        list[str] | None,
        typer.Option("--docs-root", help="Docs root to write (repeatable). Defaults to docs."),
    ] = None,
    linear_team: Annotated[
        str | None,
        typer.Option("--linear-team", help="Linear team slug to bake into the config."),
    ] = None,
) -> None:
    """Scaffold .doc-lattice.yml and print pre-commit and CI codegen."""
    try:
        roots = tuple(docs_root) if docs_root else ("docs",)
        _validate_init_flags(roots, linear_team)
        scaffold = build_scaffold(roots, linear_team, f"v{__version__}")
        target = Path.cwd() / DEFAULT_CONFIG_NAME
        try:
            _atomic_create(target, scaffold.config_text)
        except FileExistsError:
            _err.print(f"{escape(target.name)} already exists, leaving it untouched")
        except OSError as exc:
            msg = f"cannot write {target.name}: {exc}"
            raise ConfigError(msg) from exc
        else:
            _err.print(f"wrote {escape(target.name)}")
        typer.echo("# ===== .pre-commit-config.yaml (add under `repos:`) =====")
        typer.echo(scaffold.precommit_text)
        typer.echo("# ===== .github/workflows/doc-lattice.yml (new file) =====")
        typer.echo(scaffold.ci_text)
        _err.print(
            "Add the pre-commit block under `repos:`, save the workflow as "
            ".github/workflows/doc-lattice.yml, and make sure the "
            f"v{__version__} tag is pushed so the pinned snippets resolve."
        )
    except ProjectError as exc:
        _err.print(f"[red]error[/red]: {exc} ({exc.code})")
        raise typer.Exit(2) from exc
    raise typer.Exit(0)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cli.py -k init -q`
Expected: PASS (all `init` tests green).

- [ ] **Step 7: Lint, type, and boundary check**

Run: `uv run --group dev ruff check src tests && uv run --group dev ty check src && uv run --group dev python scripts/check_typing_boundaries.py src`
Expected: ruff clean, `ty` clean, boundary check prints `PASS`. If ruff reordered imports, re-stage.

- [ ] **Step 8: Run the full suite to confirm nothing regressed**

Run: `uv run --group dev pytest -q`
Expected: PASS, coverage at or above 80 percent.

- [ ] **Step 9: Commit**

```bash
git add src/doc_lattice/cli.py tests/test_cli.py
git commit -m "feat: add init command"
```

---

### Task 4: Release mechanics (version 0.2.0)

**Files:**
- Modify: `src/doc_lattice/__init__.py`, `pyproject.toml`, `uv.lock`, `CHANGELOG.md`
- Create: `RELEASING.md`

**Interfaces:**
- Produces: the package at version `0.2.0` (so `init` pins `@v0.2.0`), a `RELEASING.md` checklist, and a `CHANGELOG.md` entry. No code logic changes; `init` reads `__version__` at runtime.

- [ ] **Step 1: Bump `__version__`**

In `src/doc_lattice/__init__.py`, change the version line to:

```python
__version__ = "0.2.0"
```

- [ ] **Step 2: Bump the packaging version**

In `pyproject.toml`, change line 10 from `version = "0.1.0"` to:

```toml
version = "0.2.0"
```

- [ ] ~~**Step 3: Bump the gx marker**~~ (REMOVED: spec error)

`.gx-new-version` records the version of the gx-new scaffolding tool, not the
doc-lattice package version. It is gitignored and is not part of a release, so it is
never bumped or committed. The version bump is the two locations in Steps 1 and 2.

- [ ] **Step 4: Refresh the lockfile**

Run: `uv lock`
Expected: `uv.lock` updates the `doc-lattice` entry to `0.2.0`. Then confirm the locked sync still works:
Run: `uv sync --locked --group dev`
Expected: succeeds (no "lockfile out of date" error).

- [ ] **Step 5: Add the changelog entry**

In `CHANGELOG.md`, insert this block directly above the existing `## [0.1.0] - 2026-06-27` section:

```markdown
## [0.2.0] - 2026-06-28

### Added

- `init` command: scaffolds `.doc-lattice.yml` and prints pre-commit and CI codegen for an adopting repo.
- `RELEASING.md`: release checklist that makes the version tag an atomic part of cutting a release.

```

- [ ] **Step 6: Write `RELEASING.md`**

Create `RELEASING.md`:

```markdown
# Releasing doc-lattice

doc-lattice is distributed from git, not from PyPI. The `init` command prints
pre-commit and CI snippets that pin `uvx --from git+...@vX.Y.Z`, so a release is
only complete once the matching tag exists and resolves. Cutting a release is one
atomic step: a half-done release (code merged but no tag, or a tag without the
version bump) leaves adopters with a gate that fails before `check` runs.

## Checklist

1. Bump the version to the new `X.Y.Z` in both locations:
   - `src/doc_lattice/__init__.py` (`__version__`)
   - `pyproject.toml` (`version`)
2. Run `uv lock` and commit the refreshed `uv.lock`.
3. Add a `## [X.Y.Z]` section to `CHANGELOG.md`.
4. Open the PR, get it green, and merge to `main`.
5. Tag the merge commit and push the tag:

   ```bash
   git tag vX.Y.Z <merge-commit-sha>
   git push origin vX.Y.Z
   ```

6. Smoke-test the pinned ref before the release is done:

   ```bash
   uvx --python 3.14 --from git+https://github.com/Guardantix/doc-lattice@vX.Y.Z doc-lattice check
   ```

   It must resolve and run. If it does not, cut `X.Y.(Z+1)` rather than moving the tag.

The tag must point at a commit that contains both `check` (so the gates run) and
`init` (so adopters can run `doc-lattice init` from the same ref).
```

- [ ] **Step 7: Verify the version is live and the suite is green**

Run: `uv run doc-lattice --version`
Expected: prints `0.2.0`.
Run: `uv run --group dev pytest -q`
Expected: PASS, coverage at or above 80 percent.

- [ ] **Step 8: Commit**

```bash
git add src/doc_lattice/__init__.py pyproject.toml uv.lock CHANGELOG.md RELEASING.md
git commit -m "chore: release 0.2.0 with init command and RELEASING checklist"
```

---

### Task 5: Onboarding docs and roadmap update

**Files:**
- Modify: `README.md`, `roadmap.md`

**Interfaces:**
- Produces: an adopter onboarding section in the README and a roadmap that reflects `linear` and `init` as shipped plus the new release-automation deferred item. No code; closing documentation step of the slice.

- [ ] **Step 1: Add the README onboarding section**

In `README.md`, insert this section directly below the `## Quick Start` block (after the `### Type check` subsection ends, before `## Documentation`):

```markdown
## Adopting doc-lattice in your docs repo

Bootstrap config and a drift gate for a repo whose docs you want to track:

```bash
uvx --from git+https://github.com/Guardantix/doc-lattice@v0.2.0 doc-lattice init
```

This writes `.doc-lattice.yml` (only if absent) and prints a pre-commit hook and
a GitHub Actions workflow that run `doc-lattice check` as your drift gate. Paste
each where the output says. Pass `--docs-root` (repeatable) or `--linear-team` to
bake those values into the generated config.
```

- [ ] **Step 2: Rewrite `roadmap.md`**

Replace the entire contents of `roadmap.md` with:

```markdown
# doc-lattice Roadmap

Forward-looking slices, derived from the local-core design spec's deferral map
(`docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md`, section 12).
The spec is the source of truth; this file is the at-a-glance index.

## Shipped

- **local-core (v1)** (PR #1). The deterministic local engine: lattice parse, the id-indexed edge
  graph derived on demand, and the `impact`, `check`, `reconcile`, and `graph` commands. No network,
  no secrets, no LLM. Spec: `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md`.
- **linear slice** (PR #3). The `linear` command resolves referenced tickets to live status and
  reports shipped-against-stale-spec drift. The first network-touching slice. Spec:
  `docs/superpowers/specs/2026-06-27-doc-lattice-linear-design.md`.
- **init slice** (this PR). The `init` command scaffolds `.doc-lattice.yml` and prints pre-commit
  and CI codegen for an adopting repo. Shipped as the 0.2.0 release. Spec:
  `docs/superpowers/specs/2026-06-28-doc-lattice-init-design.md`.

Acceptance (local-core spec section 13), still met:

| Pain | Solved by | Verifiable when |
|---|---|---|
| Discovery | `impact` over the reverse adjacency | a change to one section lists every downstream doc and ticket |
| Execution | stable ids plus `impact`-guided loading | edges survive splitting a file; `impact` points at the exact section |
| Confidence | `check` exit-code gate plus `reconcile` | a stale `seen` fails CI until consciously reconciled |

## Deferred enhancements (no spec yet)

- Release-tag automation. CI that creates and verifies the `vX.Y.Z` tag, replacing the manual
  `RELEASING.md` checklist with a machine-checked smoke test. Recorded by the init spec (section 11).
- Authority-ladder validation. `authority` is already parsed, stored, and rendered, but the ladder
  is not policed.
- Display-prefix lint. An optional future enhancement.

## Out of scope by design

- Gitignored performance cache. Not needed at the intended corpus size; the graph is always derived
  on demand, never committed.
- `split` command. Splitting a document is a manual or Claude-driven edit. "Execution has no command"
  by design; stable ids and `impact` make a split safe without dedicated tooling.
```

- [ ] **Step 3: Verify the docs are clean**

Run: `uv run --group dev pytest tests/test_conventions.py -q`
Expected: PASS (no convention regressions).
Visually confirm no em-dashes were introduced in either file.

- [ ] **Step 4: Commit**

```bash
git add README.md roadmap.md
git commit -m "docs: document init onboarding and mark linear and init shipped"
```

---

### Task 6: Full-slice verification and PR

**Files:** none (verification + integration).

**Interfaces:** consumes everything above; produces a green branch ready to merge.

- [ ] **Step 1: Run the complete quality gate**

Run each and confirm all pass:

```bash
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev pytest
```

Expected: ruff clean, format clean, `ty` clean, boundary check `PASS`, full suite green with coverage at or above 80 percent.

- [ ] **Step 2: Manual smoke test of the command**

```bash
cd "$(mktemp -d)"
uv run --directory "$OLDPWD" doc-lattice init
ls -a            # .doc-lattice.yml exists
uv run --directory "$OLDPWD" doc-lattice init   # second run: prints "already exists", still emits codegen, exit 0
cd "$OLDPWD"
```

Expected: first run writes `.doc-lattice.yml` and prints both snippets; second run leaves it untouched and still prints. Both exit 0.

- [ ] **Step 3: Confirm the branch history**

Run: `git log --oneline main..HEAD`
Expected: the feat/init commits from Tasks 1 through 5, in order.

- [ ] **Step 4: Push and open the PR**

Use the `superpowers:finishing-a-development-branch` skill to push `feat/init` and open the PR against `main`. The PR body should note that this ships the 0.2.0 release and that, per `RELEASING.md`, the `v0.2.0` tag must be cut on the merge commit and smoke-tested after merge.

> Reminder (from the spec's release model, section 8): merging this PR is not the end of the release. After merge, tag the merge commit `v0.2.0`, push the tag, and run the `RELEASING.md` smoke test so the pinned `@v0.2.0` snippets resolve for adopters.
