# Project-root Symlink Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover Markdown symlinks whose targets remain inside the project, while continuing to reject targets outside the project and preserving docs-root-relative ignore behavior.

**Architecture:** Make the security boundary explicit by adding `project_root` to `discover_doc_paths`, use it only for `safe_resolve`, and keep each configured docs root as the base for traversal and ignore matching. Thread `ProjectConfig.project_root` through both lattice load paths; keep discovered paths unresolved so cache keys, diagnostics, and node paths continue to use the symlink location.

**Tech Stack:** Python 3.13+, `pathlib`, pytest, uv, Ruff, ty

---

## File structure

- `src/doc_lattice/discovery.py`: Owns Markdown traversal, docs-root-relative ignores, and the
  project boundary check for discovered candidates.
- `src/doc_lattice/orchestrate.py`: Supplies the already-known project root to discovery from both
  cached and uncached load paths.
- `tests/test_discovery.py`: Covers the discovery boundary directly, including accepted in-project
  symlinks, rejected out-of-project symlinks, and unchanged ignore semantics.
- `tests/test_orchestrate.py`: Covers the complete discovery-to-lattice flow through both cache
  modes and proves the symlink path remains the document identity.

### Task 1: Implement project-root containment through red-green TDD

**Files:**
- Modify: `tests/test_discovery.py:17-195`
- Modify: `tests/test_orchestrate.py:81-159`
- Modify: `src/doc_lattice/discovery.py:13-45`
- Modify: `src/doc_lattice/orchestrate.py:34-62`

- [ ] **Step 1: Add failing direct and end-to-end regression tests**

Add this direct regression before the existing escaping-symlink test in
`tests/test_discovery.py`. Use the current two-argument call for the red run so the failure proves
the existing containment behavior, rather than merely producing a signature error:

~~~python
def test_discovery_allows_symlink_target_inside_project(tmp_path: Path):
    project_root = tmp_path / "repo"
    docs = project_root / "docs"
    shared = project_root / "shared"
    docs.mkdir(parents=True)
    shared.mkdir()
    target = shared / "spec.md"
    target.write_text("shared content", encoding="utf-8")
    link = docs / "linked.md"
    link.symlink_to(Path("../shared/spec.md"))

    assert discover_doc_paths([docs], []) == [link]
~~~

Add this parametrized end-to-end regression after `test_multiple_docs_roots_combine` in
`tests/test_orchestrate.py`:

~~~python
@pytest.mark.parametrize("cache_enabled", [False, True], ids=["uncached", "cached"])
def test_load_lattice_includes_in_project_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cache_enabled: bool
):
    project_root = tmp_path / "repo"
    docs = project_root / "docs"
    shared = project_root / "shared"
    docs.mkdir(parents=True)
    shared.mkdir()
    target = shared / "spec.md"
    target.write_text("---\nid: linked\n---\n# Linked\n", encoding="utf-8")
    link = docs / "linked.md"
    link.symlink_to(Path("../shared/spec.md"))

    config_lines = ['docs_roots: ["docs"]']
    if cache_enabled:
        config_lines.append("cache_key: symlink-test")
    (project_root / ".doc-lattice.yml").write_text(
        "\n".join(config_lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    lattice = load_lattice(load_config(None, project_root))

    assert set(lattice.nodes_by_id) == {"linked"}
    assert lattice.nodes_by_id["linked"].path == link
~~~

- [ ] **Step 2: Run the new tests and verify the reported bug**

Run:

~~~bash
uv run --group dev pytest \
  tests/test_discovery.py::test_discovery_allows_symlink_target_inside_project \
  tests/test_orchestrate.py::test_load_lattice_includes_in_project_symlink \
  --no-cov -q
~~~

Expected: three failed cases. The direct case returns `[]` instead of `[link]`; both `uncached`
and `cached` cases build a lattice without the `linked` node. Warnings report that each symlink
escaped the project root, demonstrating the inaccurate current boundary.

- [ ] **Step 3: Make the project boundary explicit in discovery**

Replace the signature and relevant docstring portion in `src/doc_lattice/discovery.py` with:

~~~python
def discover_doc_paths(
    roots: Sequence[Path],
    ignore_globs: Sequence[str],
    project_root: Path,
) -> list[Path]:
    """Return every ``.md`` path under the roots, minus ignored matches, sorted.

    Args:
        roots: Already project-contained docs roots (from ``ProjectConfig``).
        ignore_globs: Glob patterns matched against each file's path relative to its
            root, anchored at the root. ``drafts/*.md`` skips only top-level drafts, not
            a same-named subdirectory; use ``**`` to match at any depth.
        project_root: Boundary that resolved document paths must remain inside.

    Returns:
        A sorted, de-duplicated list of markdown file paths. A file that resolves outside
        the project root (via a symlink or absolute path) is skipped with a warning rather
        than read, so a silently missing doc does not masquerade as a broken ref later.
    """
~~~

Inside the candidate loop, change only the containment argument:

~~~python
            try:
                safe_resolve(path, project_root)
~~~

Do not assign the return value from `safe_resolve`; `found.add(path)` must continue to retain the
symlink path.

- [ ] **Step 4: Update every direct discovery test to supply its actual project root**

In `tests/test_discovery.py`, update the existing calls as follows:

~~~python
found = discover_doc_paths([root], [], tmp_path)
found = discover_doc_paths([root], ["**/archive/**"], tmp_path)
found = {
    path.relative_to(root).as_posix()
    for path in discover_doc_paths([root], ["drafts/*.md"], tmp_path)
}
found = discover_doc_paths([missing, root], [], tmp_path)
found = discover_doc_paths([root, sub], [], tmp_path)
found = discover_doc_paths([root_b, root_a], [], tmp_path)
found = discover_doc_paths([root], [], tmp_path)
~~~

Update the new in-project test's assertion to use its explicit boundary:

~~~python
    assert discover_doc_paths([docs], [], project_root) == [link]
~~~

Replace the existing escaping-symlink test with this version so `outside` is genuinely outside
the supplied project root:

~~~python
def test_discovery_skips_symlink_escaping_project(tmp_path: Path):
    project_root = tmp_path / "repo"
    root = project_root / "docs"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("secret content", encoding="utf-8")
    (root / "leak.md").symlink_to(secret)
    (root / "keep.md").write_text("safe content", encoding="utf-8")
    with pytest.warns(UserWarning, match="escapes the project root"):
        found = discover_doc_paths([root], [], project_root)
    names = [path.name for path in found]
    assert "keep.md" in names
    assert "leak.md" not in names  # skipped, but loudly (not silently)
~~~

- [ ] **Step 5: Thread `ProjectConfig.project_root` through both production load paths**

In `src/doc_lattice/orchestrate.py`, replace the uncached discovery loop with:

~~~python
    for path in discover_doc_paths(
        project.resolved_roots, project.config.ignore_globs, project.project_root
    ):
~~~

Replace the cached discovery loop with:

~~~python
    for doc_path in discover_doc_paths(
        project.resolved_roots, config.ignore_globs, project.project_root
    ):
~~~

These are the only production call sites. Do not infer the boundary from `resolved_roots` and do
not move ignore matching away from `_ignored(path, root, ignore_globs)`.

- [ ] **Step 6: Run focused tests and verify green behavior**

Run:

~~~bash
uv run --group dev pytest tests/test_discovery.py tests/test_orchestrate.py --no-cov -q
~~~

Expected: all discovery and orchestration tests pass. The accepted in-project symlink cases emit
no escape warning; the out-of-project regression emits the warning captured by `pytest.warns`.

- [ ] **Step 7: Run static checks for the changed Python surface**

Run:

~~~bash
uv run --group dev ruff check src/doc_lattice/discovery.py src/doc_lattice/orchestrate.py \
  tests/test_discovery.py tests/test_orchestrate.py
uv run --group dev ruff format --check src/doc_lattice/discovery.py \
  src/doc_lattice/orchestrate.py tests/test_discovery.py tests/test_orchestrate.py
uv run --group dev ty check src
~~~

Expected: every command exits 0 with no lint, format, or type errors.

- [ ] **Step 8: Run the full test suite and inspect the diff**

Run:

~~~bash
uv run --group dev pytest
git diff --check
git diff -- src/doc_lattice/discovery.py src/doc_lattice/orchestrate.py \
  tests/test_discovery.py tests/test_orchestrate.py
~~~

Expected: pytest exits 0 with coverage at or above 80%, `git diff --check` prints nothing, and the
diff contains only the explicit project-root parameter, two production call-site updates, direct
test call updates, and the symlink regressions.

- [ ] **Step 9: Commit the verified implementation**

~~~bash
git add src/doc_lattice/discovery.py src/doc_lattice/orchestrate.py \
  tests/test_discovery.py tests/test_orchestrate.py
git commit -m "fix: contain doc symlinks within project root"
~~~

Expected: pre-commit hooks pass and Git creates one implementation commit after the earlier design
and plan commits.
