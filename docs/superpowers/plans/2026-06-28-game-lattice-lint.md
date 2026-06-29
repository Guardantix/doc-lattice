# game-lattice Lint Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `lint` command that flags `derives_from` edges where a more-authoritative doc derives from a weaker one, report unjudged edges instead of dropping them, wire both `check` and `lint` into the generated gates, and ship it as the 0.3.0 release.

**Architecture:** A new pure module `lint.py` (mirroring `check.py`) walks the loaded lattice and returns a `LintResult` of violations plus skips. `cli.py` gains a `lint` command that renders and sets the exit code. `scaffold.py` is extended so generated pre-commit and CI run both commands. The authority strength order lives as one ordered tuple in `constants.py`.

**Tech Stack:** Python 3.14+, `uv`, `typer`, `rich`, `pydantic`, `ruamel.yaml`, `pytest`, `ruff`, `ty`.

**Spec:** `docs/superpowers/specs/2026-06-28-game-lattice-lint-design.md`

## Global Constraints

Every task implicitly includes these. Exact values copied from the spec and `CLAUDE.md`:

- Dependency management and execution go through `uv` (Python 3.14+). Run tests with `uv run --group dev pytest`.
- `lint.py` and `scaffold.py` are NOT typing-boundary modules: no `typing.Any`, no `typing.cast`. Convert at boundaries only (none here).
- Constants use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py` and are imported; no raw string literal that duplicates a constant value where the constant exists.
- All custom exceptions extend `ProjectError` and carry a `code`; no bare `except Exception`/`except BaseException`. This slice adds no new exception.
- No `datetime.now()`/`utcnow()` outside `datetime_utils.py`.
- ruff line length 100; a module docstring on every module; Google-style docstrings on public functions; no em-dashes in any drafted content (docstrings, messages, comments).
- All node-derived strings printed through `rich` pass through `rich.markup.escape`.
- Coverage stays at or above the 80 percent gate.
- Work happens on branch `feat/lint-authority-ladder` (already created; the spec is already committed there). The pre-commit hook runs ruff (with `--fix`), ruff-format, `ty`, the typing-boundary check, and detect-secrets, and blocks direct commits to `main`. If a hook auto-fixes a file, re-stage and re-commit.

---

### Task 1: Constants for the authority ladder and skip reasons

**Files:**
- Modify: `src/game_lattice/constants.py`
- Test: `tests/test_constants.py`

**Interfaces:**
- Consumes: the existing `Authority` literal and `VALID_AUTHORITIES` frozenset.
- Produces:
  - `AUTHORITY_LADDER: tuple[Authority, ...]` ordered weakest to strongest: `("exploratory", "derived", "binding")`.
  - `SkipReason = Literal["source-unannotated", "target-unannotated"]` and `VALID_SKIP_REASONS: frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_constants.py`. Extend the existing import block to also import `AUTHORITY_LADDER`, `SkipReason`, and `VALID_SKIP_REASONS` from `game_lattice.constants`, then add:

```python
def test_authority_ladder_covers_every_authority():
    assert frozenset(AUTHORITY_LADDER) == VALID_AUTHORITIES


def test_authority_ladder_is_ordered_weak_to_strong():
    assert AUTHORITY_LADDER == ("exploratory", "derived", "binding")


def test_skip_reasons_match_literal():
    assert frozenset(get_args(SkipReason)) == VALID_SKIP_REASONS
    assert {"source-unannotated", "target-unannotated"} == set(VALID_SKIP_REASONS)
```

(`get_args` is already imported in `tests/test_constants.py`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_constants.py -v`
Expected: FAIL with `ImportError` (cannot import `AUTHORITY_LADDER` / `SkipReason` / `VALID_SKIP_REASONS`).

- [ ] **Step 3: Add the constants**

In `src/game_lattice/constants.py`, directly under the existing `Authority` / `VALID_AUTHORITIES` lines, add the ladder; and add the skip-reason literal in the constants block near the other literals:

```python
Authority = Literal["binding", "derived", "exploratory"]
VALID_AUTHORITIES: frozenset[str] = frozenset(get_args(Authority))
AUTHORITY_LADDER: tuple[Authority, ...] = ("exploratory", "derived", "binding")
```

```python
SkipReason = Literal["source-unannotated", "target-unannotated"]
VALID_SKIP_REASONS: frozenset[str] = frozenset(get_args(SkipReason))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_constants.py -v`
Expected: PASS (all constant tests green).

- [ ] **Step 5: Commit**

```bash
git add src/game_lattice/constants.py tests/test_constants.py
git commit -m "feat: add AUTHORITY_LADDER and SkipReason constants"
```

---

### Task 2: Expose `node_for_path` for reuse by lint

**Files:**
- Modify: `src/game_lattice/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: the existing private `_node_for_path(lattice, path) -> Node`.
- Produces: a public `node_for_path(lattice: Lattice, path: Path) -> Node` (the renamed helper) that returns the tracked node owning a location path. `target_content` keeps working unchanged.

This avoids duplicating the path-to-node resolution in `lint.py`. The helper is currently private and used only inside `resolve.py`, so the rename is local.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_resolve.py` (it already imports `build_lattice`, `ParsedDoc`, `NodeMeta`, and `Path`; if any are missing, add them):

```python
def test_node_for_path_returns_owning_node_for_an_anchor():
    from game_lattice.resolve import node_for_path

    docs = [ParsedDoc(Path("up.md"), NodeMeta(id="up", authority="binding"), "# Up {#sec}\nbody\n")]
    lat = build_lattice(docs)
    owner = node_for_path(lat, lat.index["sec"].path)
    assert owner.id == "up"
    assert owner.authority == "binding"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --group dev pytest tests/test_resolve.py::test_node_for_path_returns_owning_node_for_an_anchor -v`
Expected: FAIL with `ImportError` (cannot import `node_for_path`).

- [ ] **Step 3: Rename the helper to a public name**

In `src/game_lattice/resolve.py`, rename `_node_for_path` to `node_for_path` and update its one caller. The function body is unchanged except the name and docstring:

```python
def target_content(lattice: Lattice, target_id: str) -> str:
    ...
    node = node_for_path(lattice, location.path)
    ...


def node_for_path(lattice: Lattice, path: Path) -> Node:
    """Return the tracked node that owns a location path via the loader's path index.

    Args:
        lattice: The built lattice.
        path: A location path drawn from ``lattice.index``.

    Returns:
        The node whose file is ``path``.

    Raises:
        BrokenRefError: If no tracked node owns ``path``.
    """
    node_id = lattice.file_id_by_path.get(path)
    if node_id is None:
        msg = f"no node owns location path {path!r}"
        raise BrokenRefError(msg)
    return lattice.nodes_by_id[node_id]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_resolve.py -v`
Expected: PASS (the new test and every existing resolve test).

- [ ] **Step 5: Commit**

```bash
git add src/game_lattice/resolve.py tests/test_resolve.py
git commit -m "refactor: expose node_for_path for reuse"
```

---

### Task 3: The pure lint core (`lint.py`)

**Files:**
- Create: `src/game_lattice/lint.py`
- Test: `tests/test_lint.py`

**Interfaces:**
- Consumes: `AUTHORITY_LADDER`, `Authority`, `SkipReason` (Task 1); `node_for_path` (Task 2); `Lattice` from `model`.
- Produces:
  - `LadderViolation(source_id: str, source_authority: Authority, target_id: str, target_ref: str, target_authority: Authority)` (frozen).
  - `SkippedEdge(source_id: str, target_ref: str, target_id: str, reason: SkipReason)` (frozen).
  - `LintResult(violations: tuple[LadderViolation, ...], skipped: tuple[SkippedEdge, ...])` (frozen).
  - `lint_lattice(lattice: Lattice) -> LintResult`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lint.py` with the full pure suite. Each `Lattice` is built with `build_lattice` from synthetic docs, so the index, `file_id_by_path`, and authority wiring match production:

```python
"""Tests for the authority-ladder lint."""

from pathlib import Path

from game_lattice.lint import LintResult, lint_lattice
from game_lattice.loader import build_lattice
from game_lattice.model import NodeMeta, ParsedDoc, RawEdge


def _doc(id_, authority=None, derives=(), body="x\n"):
    """Build a ParsedDoc with optional authority and derives_from refs."""
    return ParsedDoc(
        Path(f"{id_}.md"),
        NodeMeta(
            id=id_,
            authority=authority,
            derives_from=[RawEdge(ref=r) for r in derives],
        ),
        body,
    )


def _lattice(*docs):
    return build_lattice(list(docs))


def test_binding_deriving_from_derived_is_a_violation():
    lat = _lattice(
        _doc("up", authority="derived"),
        _doc("down", authority="binding", derives=("up",)),
    )
    result = lint_lattice(lat)
    assert len(result.violations) == 1
    v = result.violations[0]
    assert (v.source_id, v.source_authority) == ("down", "binding")
    assert (v.target_id, v.target_authority) == ("up", "derived")
    assert result.skipped == ()


def test_binding_deriving_from_exploratory_is_a_violation():
    lat = _lattice(
        _doc("up", authority="exploratory"),
        _doc("down", authority="binding", derives=("up",)),
    )
    assert len(lint_lattice(lat).violations) == 1


def test_derived_deriving_from_exploratory_is_a_violation():
    lat = _lattice(
        _doc("up", authority="exploratory"),
        _doc("down", authority="derived", derives=("up",)),
    )
    assert len(lint_lattice(lat).violations) == 1


def test_equal_authority_passes():
    lat = _lattice(
        _doc("up", authority="binding"),
        _doc("down", authority="binding", derives=("up",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert result.skipped == ()


def test_deriving_from_stronger_passes():
    lat = _lattice(
        _doc("up", authority="binding"),
        _doc("down", authority="derived", derives=("up",)),
    )
    assert lint_lattice(lat).violations == ()


def test_unannotated_source_is_skipped_not_failed():
    lat = _lattice(
        _doc("up", authority="binding"),
        _doc("down", authority=None, derives=("up",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "source-unannotated"
    assert result.skipped[0].source_id == "down"


def test_unannotated_target_is_skipped_not_failed():
    lat = _lattice(
        _doc("up", authority=None),
        _doc("down", authority="binding", derives=("up",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "target-unannotated"
    assert result.skipped[0].target_id == "up"


def test_broken_edge_is_not_a_violation_and_not_in_skips():
    lat = _lattice(_doc("down", authority="binding", derives=("ghost",)))
    result = lint_lattice(lat)
    assert result.violations == ()
    assert result.skipped == ()  # broken edges are check's concern, not counted here


def test_section_target_violation_uses_owning_file_authority():
    lat = _lattice(
        _doc("up", authority="derived", body="# Up {#sec}\nbody\n"),
        _doc("down", authority="binding", derives=("up#sec",)),
    )
    result = lint_lattice(lat)
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.target_id == "sec"
    assert v.target_ref == "up#sec"
    assert v.target_authority == "derived"  # inherited from the owning file "up"


def test_section_target_passes_when_owning_file_is_stronger():
    lat = _lattice(
        _doc("up", authority="binding", body="# Up {#sec}\nbody\n"),
        _doc("down", authority="derived", derives=("up#sec",)),
    )
    assert lint_lattice(lat).violations == ()


def test_section_target_skipped_when_owning_file_unannotated():
    lat = _lattice(
        _doc("up", authority=None, body="# Up {#sec}\nbody\n"),
        _doc("down", authority="binding", derives=("up#sec",)),
    )
    result = lint_lattice(lat)
    assert result.violations == ()
    assert result.skipped[0].reason == "target-unannotated"
    assert result.skipped[0].target_id == "sec"


def test_results_are_in_node_id_then_edge_order():
    lat = _lattice(
        _doc("weak1", authority="exploratory"),
        _doc("weak2", authority="exploratory"),
        _doc("a", authority="binding", derives=("weak1", "weak2")),
        _doc("b", authority="binding", derives=("weak1",)),
    )
    result = lint_lattice(lat)
    assert isinstance(result, LintResult)
    assert [v.source_id for v in result.violations] == ["a", "a", "b"]
    assert [v.target_id for v in result.violations] == ["weak1", "weak2", "weak1"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_lint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'game_lattice.lint'`.

- [ ] **Step 3: Write the lint module**

Create `src/game_lattice/lint.py`:

```python
"""Validate the authority ladder over derives_from edges. Pure: no I/O."""

from dataclasses import dataclass

from .constants import AUTHORITY_LADDER, Authority, SkipReason
from .model import Lattice
from .resolve import node_for_path


@dataclass(frozen=True, slots=True)
class LadderViolation:
    """One derives_from edge that inverts the authority ladder."""

    source_id: str
    source_authority: Authority
    target_id: str
    target_ref: str
    target_authority: Authority


@dataclass(frozen=True, slots=True)
class SkippedEdge:
    """One edge the ladder could not judge because an endpoint lacks authority."""

    source_id: str
    target_ref: str
    target_id: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class LintResult:
    """Violations that fail the gate, plus the unjudged skips."""

    violations: tuple[LadderViolation, ...]
    skipped: tuple[SkippedEdge, ...]


def _rank(authority: Authority) -> int:
    """Return the ladder position of an authority; higher means stronger."""
    return AUTHORITY_LADDER.index(authority)


def _target_authority(lattice: Lattice, target_id: str) -> Authority | None:
    """Return the authority of the file node that owns a resolved target id.

    A section anchor inherits the authority of the file that owns it, so both file
    and section targets resolve through the same path index.
    """
    location = lattice.index[target_id]
    return node_for_path(lattice, location.path).authority


def lint_lattice(lattice: Lattice) -> LintResult:
    """Classify every edge as a violation, a skip, or a silent pass.

    Walks nodes in id order and each node's edges in order. A broken edge is left to
    ``check``. An edge with an unannotated endpoint is recorded as a skip. Otherwise a
    target weaker than its source is a violation.

    Args:
        lattice: The built lattice.

    Returns:
        The violations and skips, both in node-id then edge order.
    """
    violations: list[LadderViolation] = []
    skipped: list[SkippedEdge] = []
    for node_id in sorted(lattice.nodes_by_id):
        node = lattice.nodes_by_id[node_id]
        source_authority = node.authority
        for edge in node.derives_from:
            target_id = edge.target_id
            if target_id is None:
                continue  # broken edge: reported by check, not counted here
            target_authority = _target_authority(lattice, target_id)
            if source_authority is None:
                skipped.append(
                    SkippedEdge(node_id, edge.target_ref, target_id, "source-unannotated")
                )
                continue
            if target_authority is None:
                skipped.append(
                    SkippedEdge(node_id, edge.target_ref, target_id, "target-unannotated")
                )
                continue
            if _rank(target_authority) < _rank(source_authority):
                violations.append(
                    LadderViolation(
                        source_id=node_id,
                        source_authority=source_authority,
                        target_id=target_id,
                        target_ref=edge.target_ref,
                        target_authority=target_authority,
                    )
                )
    return LintResult(violations=tuple(violations), skipped=tuple(skipped))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_lint.py -v`
Expected: PASS (all lint tests green).

- [ ] **Step 5: Type-check and boundary-check the new module**

Run: `uv run --group dev ty check src && uv run --group dev python scripts/check_typing_boundaries.py src`
Expected: both clean (no `Any`/`cast` in `lint.py`, types resolve).

- [ ] **Step 6: Commit**

```bash
git add src/game_lattice/lint.py tests/test_lint.py
git commit -m "feat: authority-ladder lint core"
```

---

### Task 4: The `lint` CLI command

**Files:**
- Modify: `src/game_lattice/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `lint_lattice`, `LintResult` (Task 3); the existing `_load`, `ConfigOpt`, `JsonOpt`, `_out`, `_err`, `app`, `escape`, `json`.
- Produces: a `lint` Typer command. Exit 1 on any violation, 0 when clean, 2 on a `ProjectError`. Human output prints one line per violation then a summary line; `--json` emits `{"violations": [...], "skipped": [...]}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`. These use a dedicated doc set, not the shared `lattice_dir` fixture:

```python
def _write_lint_docs(root: Path) -> None:
    docs = root / "docs"
    docs.mkdir()
    # "down" is binding but derives from "up" (derived): a ladder inversion.
    (docs / "up.md").write_text(
        "---\nid: up\nauthority: derived\n---\n# Up\nbody\n", encoding="utf-8"
    )
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )


def test_lint_exits_1_on_violation(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 1
    assert "VIOLATION" in result.stdout


def test_lint_json_lists_violations(tmp_path: Path, monkeypatch):
    _write_lint_docs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint", "--json"])
    payload = json.loads(result.stdout)
    assert payload["violations"][0]["source_id"] == "down"
    assert payload["violations"][0]["target_authority"] == "derived"
    assert payload["skipped"] == []


def test_lint_exits_0_and_reports_skips(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    # down (binding) derives from up, which has no authority: a skip, not a failure.
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 0
    assert "0 ladder violations" in result.stdout
    assert "1 edges unranked" in result.stdout


def test_lint_json_reports_skips(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up\nbody\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nauthority: binding\nderives_from:\n  - ref: up\n---\n# Down\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint", "--json"])
    payload = json.loads(result.stdout)
    assert payload["violations"] == []
    assert payload["skipped"][0]["reason"] == "target-unannotated"


def test_lint_exits_2_on_bad_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cli.py -k lint -v`
Expected: FAIL (the `lint` command does not exist, so typer reports no such command / nonzero exits that do not match).

- [ ] **Step 3: Add the imports and the command**

In `src/game_lattice/cli.py`, add to the imports near the other intra-package imports:

```python
from .lint import LintResult, lint_lattice
```

Add this helper next to the other module-level helpers (for example just after `_load`):

```python
def _skip_summary(result: LintResult) -> str:
    """Render the one-line coverage summary printed after any human lint run."""
    violations = len(result.violations)
    unranked = len(result.skipped)
    targets = sum(1 for s in result.skipped if s.reason == "target-unannotated")
    sources = sum(1 for s in result.skipped if s.reason == "source-unannotated")
    label = "violation" if violations == 1 else "violations"
    line = f"{violations} ladder {label}, {unranked} edges unranked"
    if unranked:
        line += f" ({targets} target unannotated, {sources} source unannotated)"
    return line
```

Add the command beside the existing `check` command:

```python
@app.command()
def lint(config: ConfigOpt = None, json_out: JsonOpt = False) -> None:
    """Validate the authority ladder; exit 1 on a violation, 2 on tool error."""
    try:
        lattice = _load(config)
        result = lint_lattice(lattice)
    except ProjectError as exc:
        _err.print(f"[red]error[/red]: {escape(str(exc))} ({exc.code})")
        raise typer.Exit(2) from exc
    if json_out:
        payload = {
            "violations": [
                {
                    "source_id": v.source_id,
                    "source_authority": v.source_authority,
                    "target_id": v.target_id,
                    "target_ref": v.target_ref,
                    "target_authority": v.target_authority,
                }
                for v in result.violations
            ],
            "skipped": [
                {
                    "source_id": s.source_id,
                    "target_ref": s.target_ref,
                    "target_id": s.target_id,
                    "reason": s.reason,
                }
                for s in result.skipped
            ],
        }
        typer.echo(json.dumps(payload))
    else:
        for v in result.violations:
            _out.print(
                f"[red]VIOLATION[/red]  {escape(v.source_id)} ({v.source_authority}) -> "
                f"{escape(v.target_ref)} ({v.target_authority})"
            )
        _out.print(_skip_summary(result))
    raise typer.Exit(1 if result.violations else 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cli.py -k lint -v`
Expected: PASS (all five lint CLI tests green).

- [ ] **Step 5: Commit**

```bash
git add src/game_lattice/cli.py tests/test_cli.py
git commit -m "feat: lint command"
```

---

### Task 5: Wire `lint` into the generated gates

**Files:**
- Modify: `src/game_lattice/scaffold.py`
- Test: `tests/test_scaffold.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: generated pre-commit text with two hooks (`game-lattice-check`, `game-lattice-lint`); generated CI text running both commands in one aggregating shell step. `_check_invocation` becomes `_invocation(rev, command)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_scaffold.py`:

```python
def test_generated_gates_run_check_and_lint():
    s = build_scaffold(("docs",), None, "v0.3.0")
    assert "id: game-lattice-check" in s.precommit_text
    assert "id: game-lattice-lint" in s.precommit_text
    assert "game-lattice check" in s.precommit_text
    assert "game-lattice lint" in s.precommit_text
    assert "game-lattice check" in s.ci_text
    assert "game-lattice lint" in s.ci_text


def test_ci_runs_both_commands_in_one_step():
    # A second GitHub Actions run step would be skipped after check exits nonzero,
    # so both commands share one step that captures each exit code and fails if
    # either failed.
    ci = build_scaffold(("docs",), None, "v0.3.0").ci_text
    assert ci.count("- run:") == 1
    assert "rc_check=$?" in ci
    assert "rc_lint=$?" in ci
```

Also update the existing `test_snippets_pin_rev_url_and_python`: change its final assertion's comment so it no longer claims only check runs:

```python
    assert "linear" not in s.ci_text  # the network command never runs in the generated CI
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_scaffold.py -v`
Expected: FAIL on `test_generated_gates_run_check_and_lint` and `test_ci_runs_both_commands_in_one_step` (no `game-lattice-lint`, no `rc_check`).

- [ ] **Step 3: Generalize the invocation and render both gates**

In `src/game_lattice/scaffold.py`, replace `_check_invocation` with a command-parameterized `_invocation`, and rewrite `render_precommit` and `render_ci`:

```python
def _invocation(rev: str, command: str) -> str:
    """Return the uvx command a gate runs, pinned to rev and Python 3.14."""
    return f"uvx --python {PYTHON_PIN} --from git+{GAME_LATTICE_REPO_URL}@{rev} game-lattice {command}"


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
    check = _invocation(rev, "check")
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
        f"          {check}\n"
        "          rc_check=$?\n"
        f"          {lint_cmd}\n"
        "          rc_lint=$?\n"
        '          [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]\n'
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_scaffold.py -v`
Expected: PASS (new and existing scaffold tests green).

- [ ] **Step 5: Commit**

```bash
git add src/game_lattice/scaffold.py tests/test_scaffold.py
git commit -m "feat: generated gates run check and lint"
```

---

### Task 6: Cut the 0.3.0 release

**Files:**
- Modify: `src/game_lattice/__init__.py`
- Modify: `pyproject.toml:10`
- Modify: `CHANGELOG.md`
- Modify: `RELEASING.md:32-33`
- Regenerate: `uv.lock`

**Interfaces:**
- Consumes: nothing.
- Produces: version `0.3.0` everywhere the tool reads it, so `init` pins generated snippets to `v0.3.0`; a changelog entry; the release invariant naming `lint`.

The version-pinned tests read `__version__` dynamically (`test_cli.py:335` asserts `f"@v{__version__}"`) or pass an explicit rev (`test_scaffold.py`), so the bump breaks no test.

- [ ] **Step 1: Bump the version in both locations**

In `src/game_lattice/__init__.py`:

```python
__version__ = "0.3.0"
```

In `pyproject.toml` line 10:

```toml
version = "0.3.0"
```

- [ ] **Step 2: Refresh the lockfile**

Run: `uv lock`
Expected: `uv.lock` updates the project version to 0.3.0.

- [ ] **Step 3: Add the changelog entry**

In `CHANGELOG.md`, add a new section above `## [0.2.0] - 2026-06-28`:

```markdown
## [0.3.0] - 2026-06-28

### Added

- `lint` command: validates the authority ladder over `derives_from` edges and reports edges it cannot rank.
- Generated pre-commit and CI now run both `game-lattice check` and `game-lattice lint`.
```

- [ ] **Step 4: Extend the release invariant**

In `RELEASING.md`, update the closing paragraph (lines 32-33) so the tag must contain `lint`:

```markdown
The tag must point at a commit that contains `check` and `lint` (so the gates run) and
`init` (so adopters can run `game-lattice init` from the same ref).
```

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

Run: `uv run --group dev pytest`
Expected: PASS, coverage at or above 80 percent.

- [ ] **Step 6: Commit**

```bash
git add src/game_lattice/__init__.py pyproject.toml uv.lock CHANGELOG.md RELEASING.md
git commit -m "chore: release 0.3.0"
```

Note: cutting and pushing the actual `v0.3.0` tag happens after the PR merges, per the `RELEASING.md` checklist. This task only prepares the release commit.

---

### Task 7: Update the roadmap and project docs

**Files:**
- Modify: `roadmap.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: nothing.
- Produces: docs that reflect `lint` as shipped and listed in the command set.

- [ ] **Step 1: Move authority-ladder validation to Shipped in the roadmap**

In `roadmap.md`, under `## Shipped`, add after the init entry:

```markdown
- **lint slice** (v0.3.0). The `lint` command validates the authority ladder over `derives_from`
  edges, reports edges it cannot rank, and is wired into the generated pre-commit and CI gates
  alongside `check`. Spec: `docs/superpowers/specs/2026-06-28-game-lattice-lint-design.md`.
```

Remove the `Authority-ladder validation.` bullet from `## Deferred enhancements (no spec yet)`.

- [ ] **Step 2: List `lint` in CLAUDE.md**

In `CLAUDE.md`, update the commands comment so `lint` appears:

```bash
uv run game-lattice --help                # run the CLI (commands: check, impact, reconcile, graph, linear, init, lint)
```

Add a sentence to the architecture section near the `check`/`reconcile` description, for example after the drift-detection paragraph:

```markdown
`lint` is a pure structural check separate from drift: it flags a `derives_from` edge whose source
is more authoritative than its target (binding > derived > exploratory), reports edges it cannot rank
because an endpoint lacks `authority`, and never mutates. It exits 1 on a violation, mirroring `check`.
Spec: `docs/superpowers/specs/2026-06-28-game-lattice-lint-design.md`.
```

- [ ] **Step 3: Verify the full suite and lint still pass**

Run: `uv run --group dev pytest && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add roadmap.md CLAUDE.md
git commit -m "docs: record lint slice in roadmap and CLAUDE"
```

---

## Self-Review

**Spec coverage:**

- Section 2 (the rule) -> Task 3 violation/pass tests and `_rank`.
- Section 3 (classification and skip rules, broken excluded) -> Task 3 `lint_lattice` ordering and the broken/skip tests.
- Section 4 / 4.1 (data model, target authority resolution) -> Task 2 (`node_for_path`) and Task 3 (`_target_authority`, section-target tests).
- Section 5 (constants) -> Task 1.
- Section 6 (command surface, exit codes, honest-coverage framing) -> Task 4.
- Section 7 (human + JSON output, skip summary) -> Task 4 `_skip_summary` and JSON tests.
- Section 8 (scaffold and codegen wiring, one aggregating CI step) -> Task 5.
- Section 9 (0.3.0 release) -> Task 6.
- Section 11 (conventions) -> Global Constraints, enforced by Task 3 Step 5 and Task 7 Step 3.
- Section 12 (testing, dedicated CLI doc set, conventions assertion) -> Tasks 1, 3, 4, 5.
- Section 14 (acceptance) -> the test set across Tasks 3-5.

Deviation noted: the spec mentions the `AUTHORITY_LADDER == VALID_AUTHORITIES` assertion in `test_conventions.py`; this plan places it in `tests/test_constants.py` (Task 1), where every other `frozenset(get_args(X)) == VALID_X` assertion already lives. Same enforcement, consistent home.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N" present; every code step shows complete code.

**Type consistency:** `lint_lattice`, `LintResult`, `LadderViolation`, `SkippedEdge`, `_rank`, `_target_authority`, `node_for_path`, `_invocation`, and `_skip_summary` are named identically wherever referenced across Tasks 2-5. `SkipReason` values (`source-unannotated`, `target-unannotated`) match between constants (Task 1), the core (Task 3), and the summary (Task 4).
