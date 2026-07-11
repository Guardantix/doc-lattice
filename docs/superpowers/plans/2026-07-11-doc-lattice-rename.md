# doc-lattice Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the project from game-lattice to doc-lattice across package, CLI, config file, cache, repo URL, docs, and prose, shipping as v0.9.0.

**Architecture:** One branch (`rename/doc-lattice`, already exists with the approved spec committed) carrying the whole rename in reviewable commits. Each task flips one coherent surface and leaves the full quality gate green. The GitHub repo rename happens only at cutover, after the PR is green and merge-ready. Spec: `docs/superpowers/specs/2026-07-11-doc-lattice-rename-design.md` (binding).

**Tech Stack:** Python 3.13+, uv, pytest, ruff, ty, typer; GitHub CLI (`gh`) for the cutover.

## Global Constraints

- Work on branch `rename/doc-lattice`; never commit to `main` (pre-commit blocks it).
- Every commit must pass the pre-commit hook: ruff (`--fix`), ruff-format, `ty`, `scripts/check_typing_boundaries.py`, `scripts/check_version_sync.py`, detect-secrets. If a hook auto-fixes a file, re-stage and re-commit.
- Run pytest as `env -u FORCE_COLOR uv run --group dev pytest` (this shell exports FORCE_COLOR=3, which breaks rich substring asserts; CI does not set it).
- Coverage gate: >= 80% (currently ~99%). Do not delete tests.
- ruff line length 100; module docstring on every module; no em-dashes in any drafted content (docstrings, messages, comments, docs).
- No Claude attribution anywhere in commit messages or the PR.
- Version sync triple: `src/doc_lattice/__init__.py` `__version__`, pyproject `version`, first versioned CHANGELOG heading. `version_check` also verifies README `doc-lattice@vX.Y.Z` pins match `__version__`. Version stays 0.8.0 until Task 6.
- The grep sweep target (Task 7): `grep -rIn 'game[-_]lattice\|GAME_LATTICE' .` (excluding `.git`) returns hits only in `CHANGELOG.md` history (entries 0.8.0 and older) and `docs/superpowers/specs/2026-07-11-doc-lattice-rename-design.md`.

---

### Task 1: Package, entry point, and pyproject rename

**Files:**
- Move: `src/game_lattice/` -> `src/doc_lattice/` (git mv, all 30 modules)
- Modify: every file under `src/`, `tests/`, `scripts/` importing `game_lattice`; `pyproject.toml`; `.github/workflows/ci.yml` (underscore forms only); `src/doc_lattice/__init__.py` docstring; `uv.lock` (regenerated)
- Test: whole suite

**Interfaces:**
- Consumes: nothing (first task).
- Produces: importable package `doc_lattice`; console script `doc-lattice = "doc_lattice.cli:main"`; distribution name `doc-lattice`. All later tasks import `doc_lattice` and invoke `uv run doc-lattice`.

- [ ] **Step 1: Move the package and rewrite all underscore references**

```bash
git mv src/game_lattice src/doc_lattice
grep -rl 'game_lattice' src tests scripts .github pyproject.toml | xargs sed -i 's/game_lattice/doc_lattice/g'
```

This covers imports, `--cov=game_lattice`, coverage `source`, `packages = ["src/game_lattice"]`, `tests/test_conventions.py` `SRC_DIR`, the two `import game_lattice` lines in `.github/workflows/ci.yml` (version echo and `git show "${TAG}:src/game_lattice/__init__.py"`), and the `game_lattice.__version__` mentions in `version_check.py` docstrings/messages.

- [ ] **Step 2: Rename the distribution, entry point, and Homepage in pyproject.toml**

Exact edits (hyphen forms; underscore forms were already rewritten by Step 1):

```toml
name = "doc-lattice"
description = "Traceability engine for design and production documentation"
[project.scripts]
doc-lattice = "doc_lattice.cli:main"
[project.urls]
Homepage = "https://github.com/Guardantix/doc-lattice"
```

(Keep every other line as is. The Homepage URL goes live at cutover; nothing fetches it before then.)

- [ ] **Step 3: Generalize the package docstring**

`src/doc_lattice/__init__.py` line 1:

```python
"""Traceability engine for design and production documentation"""
```

- [ ] **Step 4: Regenerate the environment and lockfile**

```bash
uv lock && uv sync --group dev
```

Expected: `uv.lock` now names `doc-lattice`; `.venv/bin/doc-lattice` exists.

- [ ] **Step 5: Run the full gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest -q
uv run --group dev ruff check src tests && uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
uv run doc-lattice --help
```

Expected: all pass (723 tests); `doc-lattice --help` prints the command list. `uv run game-lattice` now fails with "command not found" (verify once; that is the intended break).

- [ ] **Step 6: Verify no underscore stragglers and commit**

```bash
grep -rn 'game_lattice' src tests scripts .github pyproject.toml uv.lock
```

Expected: no output.

```bash
git add -A
git commit -m "refactor!: rename package game_lattice to doc_lattice"
```

---

### Task 2: Config filename clean break (.game-lattice.yml -> .doc-lattice.yml)

**Files:**
- Move: `tests/fixtures/release-smoke/.game-lattice.yml` -> `tests/fixtures/release-smoke/.doc-lattice.yml`
- Modify: `src/doc_lattice/config.py` (module docstring, `DEFAULT_CONFIG_NAME`, `ProjectConfig` docstring), `src/doc_lattice/cli.py` (ConfigOpt help, init docstring), `src/doc_lattice/linear_query.py` (team-key error message), `src/doc_lattice/scaffold.py` (render_config docstring), `scripts/bench_load_cache.py`, `.github/workflows/ci.yml` (fixture path), every test asserting the filename
- Test: `tests/test_config.py`, `tests/test_cli.py`, `tests/test_scaffold.py`, `tests/test_linear_query.py`

**Interfaces:**
- Consumes: package `doc_lattice` from Task 1.
- Produces: `DEFAULT_CONFIG_NAME = ".doc-lattice.yml"` in `doc_lattice.config`. Only this filename is discovered; no fallback.

- [ ] **Step 1: Flip the fixture and all test expectations first**

```bash
git mv tests/fixtures/release-smoke/.game-lattice.yml tests/fixtures/release-smoke/.doc-lattice.yml
grep -rl '\.game-lattice\.yml' tests | xargs sed -i 's/\.game-lattice\.yml/.doc-lattice.yml/g'
```

- [ ] **Step 2: Run the affected suites to verify they fail**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_config.py tests/test_cli.py tests/test_scaffold.py -q
```

Expected: FAILURES (tests now expect `.doc-lattice.yml` while the source still says `.game-lattice.yml`). If everything passes, the tests never pinned the filename; stop and inspect before proceeding.

- [ ] **Step 3: Flip the source, scripts, and CI**

```bash
grep -rl '\.game-lattice\.yml' src scripts .github | xargs sed -i 's/\.game-lattice\.yml/.doc-lattice.yml/g'
```

Covers `DEFAULT_CONFIG_NAME`, both config.py docstrings, the cli.py help string `"Path to .doc-lattice.yml."`, the init command docstring, the linear_query message `"... fix .doc-lattice.yml"`, scaffold's render_config docstring, `scripts/bench_load_cache.py`, and the ci.yml `fixture=` path.

- [ ] **Step 4: Run the full suite to verify it passes**

```bash
env -u FORCE_COLOR uv run --group dev pytest -q
grep -rn '\.game-lattice\.yml' src tests scripts .github
```

Expected: all pass; grep empty.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat!: recognize only .doc-lattice.yml as the config file"
```

---

### Task 3: Repo URL, scaffold codegen, cache path, and remaining source identity strings

**Files:**
- Modify: `src/doc_lattice/scaffold.py` (`GAME_LATTICE_REPO_URL` -> `DOC_LATTICE_REPO_URL`, header comment, `_invocation`), `src/doc_lattice/cache.py` (path segment, stderr prefix, docstring), `src/doc_lattice/cli.py` (root docstring, annotation titles, init codegen headers naming `.github/workflows/doc-lattice.yml`), `src/doc_lattice/linear_client.py` (User-Agent), `src/doc_lattice/version_check.py` (`_PINNED_REF` regex and messages), `.github/workflows/ci.yml` (release smoke `ref=` URL, `uvx ... doc-lattice` invocations, pinned-ref confirm step), `scripts/bench_load_cache.py` (cache dir segment), matching tests
- Test: `tests/test_scaffold.py`, `tests/test_cache.py`, `tests/test_cli.py`, `tests/test_linear_client.py`, `tests/test_version_check.py`

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces: `DOC_LATTICE_REPO_URL = "https://github.com/Guardantix/doc-lattice"` exported by `doc_lattice.scaffold` (tests import it by this exact name); cache layout `<cache_home>/doc-lattice/<cache_key>/load-cache.json`; `version_check._PINNED_REF` matching `doc-lattice@vX.Y.Z`.

- [ ] **Step 1: Flip test expectations first**

```bash
grep -rl 'GAME_LATTICE\|game-lattice' tests | xargs sed -i -e 's/GAME_LATTICE/DOC_LATTICE/g' -e 's/game-lattice/doc-lattice/g'
```

- [ ] **Step 2: Run affected suites to verify they fail**

```bash
env -u FORCE_COLOR uv run --group dev pytest tests/test_scaffold.py tests/test_cache.py tests/test_version_check.py tests/test_linear_client.py -q
```

Expected: FAILURES, including an ImportError in test_scaffold (`DOC_LATTICE_REPO_URL` does not exist yet).

- [ ] **Step 3: Flip the source, CI, and bench script**

```bash
grep -rl 'GAME_LATTICE\|game-lattice' src scripts .github | xargs sed -i -e 's/GAME_LATTICE/DOC_LATTICE/g' -e 's/game-lattice/doc-lattice/g'
```

Key resulting lines to spot-check by eye:
- scaffold.py: `DOC_LATTICE_REPO_URL = "https://github.com/Guardantix/doc-lattice"`; `_CONFIG_HEADER = f"# doc-lattice configuration. See {DOC_LATTICE_REPO_URL}\n"`; `_invocation` returns `uvx --python {PYTHON_PIN} --from git+{DOC_LATTICE_REPO_URL}@{rev} doc-lattice {command}`.
- cache.py: path is `cache_home(env) / "doc-lattice" / cache_key / CACHE_FILE_NAME`; stderr prefix `doc-lattice: could not write load cache ...`.
- cli.py: root docstring `"""doc-lattice: documentation traceability engine."""`; annotation titles `doc-lattice {status.state}` and `doc-lattice ladder violation`; init prints `# ===== .github/workflows/doc-lattice.yml (new file) =====`.
- linear_client.py: `"User-Agent": f"doc-lattice/{__version__}"`.
- version_check.py: `_PINNED_REF = re.compile(r"doc-lattice@v(?P<version>\d+\.\d+\.\d+)")`.
- ci.yml: `ref="git+https://github.com/Guardantix/doc-lattice@${GITHUB_SHA}"`; all `uvx` lines invoke `doc-lattice`; the confirm step pins `.../doc-lattice@${TAG}`. (These run only in the post-merge release job, after the cutover in Task 8, so pointing at the new URL now is safe.)

- [ ] **Step 4: Run the full gate to verify it passes**

```bash
env -u FORCE_COLOR uv run --group dev pytest -q
grep -rn 'game[-_]lattice\|GAME_LATTICE' src tests scripts .github
```

Expected: all pass; grep empty. (README still pins `game-lattice@v0.8.0` and `_PINNED_REF` now matches only `doc-lattice@`, so the README check finds zero pins, which `version_check` treats as nothing to verify; the pins are restored to matching form in Task 4.)

- [ ] **Step 5: Verify the version-sync guard still passes, then commit**

```bash
uv run --group dev python scripts/check_version_sync.py
git add -A
git commit -m "feat!: move repo URL, scaffold codegen, and cache path to doc-lattice"
```

---

### Task 4: Top-level prose (README, ARCHITECTURE, CLAUDE.md, RELEASING, roadmap, build-log)

**Files:**
- Modify: `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`, `RELEASING.md`, `roadmap.md`, `build-log.md`
- Test: `env -u FORCE_COLOR uv run --group dev pytest tests/test_version_check.py -q` plus manual read-through

**Interfaces:**
- Consumes: Tasks 1-3 (names and URL they establish).
- Produces: prose describing a general documentation traceability engine; README pins in the form `doc-lattice@v0.8.0` (bumped to v0.9.0 in Task 6).

- [ ] **Step 1: Mechanical rename in the six prose files**

```bash
sed -i -e 's/game_lattice/doc_lattice/g' -e 's/game-lattice/doc-lattice/g' README.md ARCHITECTURE.md CLAUDE.md RELEASING.md roadmap.md build-log.md
```

- [ ] **Step 2: Generalize the positioning sentences by hand**

- `README.md` line 3: `A deterministic, offline traceability engine for design and production documentation.`
- `README.md` intro paragraph: keep the game-flavored examples (player-character spec, art direction, level design) as illustrative examples, but rephrase the lead so the domain is general, e.g. `doc-lattice tracks the dependencies *between* your markdown docs. When a downstream document derives from an upstream one (a player-character spec built on the art direction, an implementation plan built on a product brief), it records that link in frontmatter.`
- `CLAUDE.md` opening: `doc-lattice is a deterministic, offline traceability engine for design and production docs.` Leave the rest of the sentence (frontmatter, edge graph, staleness) unchanged.
- Skim all six files for any remaining "game design"/"game production" phrasing outside deliberate examples and generalize each occurrence to "design"/"production documentation". The conftest example vocabulary (art-direction fixtures) stays.

- [ ] **Step 3: Verify README pins and the gate**

```bash
grep -n 'doc-lattice@v' README.md
env -u FORCE_COLOR uv run --group dev pytest tests/test_version_check.py -q
uv run --group dev python scripts/check_version_sync.py
grep -rn 'game[-_]lattice' README.md ARCHITECTURE.md CLAUDE.md RELEASING.md roadmap.md build-log.md
```

Expected: pins read `doc-lattice@v0.8.0` (matching `__version__`); tests and guard pass; final grep empty.

- [ ] **Step 4: Commit**

```bash
git add README.md ARCHITECTURE.md CLAUDE.md RELEASING.md roadmap.md build-log.md
git commit -m "docs: rebrand top-level prose to doc-lattice and generalize positioning"
```

---

### Task 5: Rename and update docs/superpowers specs and plans

**Files:**
- Move: the 13 files under `docs/superpowers/{specs,plans}/` whose names contain `game-lattice`, each to the same name with `doc-lattice`
- Modify: content of all files under `docs/superpowers/` EXCEPT `specs/2026-07-11-doc-lattice-rename-design.md` (it deliberately names the old identity)
- Test: grep only (docs task; the suite does not read these files)

**Interfaces:**
- Consumes: nothing from code tasks.
- Produces: `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md` as the new binding-design path; CLAUDE.md already points at it after Task 4's sed.

- [ ] **Step 1: Rename the files**

```bash
cd docs/superpowers
for f in specs/*game-lattice* plans/*game-lattice*; do git mv "$f" "${f//game-lattice/doc-lattice}"; done
cd ../..
```

- [ ] **Step 2: Rewrite content, sparing the rename spec**

```bash
grep -rl 'game[-_]lattice\|GAME_LATTICE' docs/superpowers | grep -v '2026-07-11-doc-lattice-rename-design.md' | xargs sed -i -e 's/GAME_LATTICE/DOC_LATTICE/g' -e 's/game_lattice/doc_lattice/g' -e 's/game-lattice/doc-lattice/g'
```

- [ ] **Step 3: Verify cross-references resolve**

```bash
grep -rn 'game[-_]lattice' docs/superpowers | grep -v '2026-07-11-doc-lattice-rename-design.md'
grep -n 'doc-lattice-local-core-design' CLAUDE.md
ls docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md
```

Expected: first grep empty; CLAUDE.md points at the renamed binding spec; the file exists.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs: rename superpowers specs and plans to doc-lattice"
```

---

### Task 6: Version 0.9.0 and the BREAKING changelog entry

**Files:**
- Modify: `src/doc_lattice/__init__.py`, `pyproject.toml` (`version`), `CHANGELOG.md` (new entry on top; history untouched), `README.md` (pins to v0.9.0), `uv.lock` (regenerated)
- Test: `tests/test_version_check.py`, `scripts/check_version_sync.py`

**Interfaces:**
- Consumes: everything prior (the entry describes it).
- Produces: version 0.9.0 everywhere the sync guard checks; merge to main will auto-cut tag `v0.9.0`.

- [ ] **Step 1: Bump the version**

`src/doc_lattice/__init__.py`: `__version__ = "0.9.0"`. `pyproject.toml`: `version = "0.9.0"`. Then:

```bash
sed -i 's/doc-lattice@v0\.8\.0/doc-lattice@v0.9.0/g' README.md
uv lock
```

- [ ] **Step 2: Add the CHANGELOG entry**

Insert directly under the `# Changelog` preamble (above `## [0.8.0]`), exactly:

```markdown
## [0.9.0] - 2026-07-11

### Changed

- **BREAKING:** the project is renamed from game-lattice to doc-lattice. The engine was never
  game-specific; the name now matches its general purpose. In one release this renames the
  repository (https://github.com/Guardantix/doc-lattice, with GitHub redirects from the old
  URL), the distribution and package (`doc-lattice` / `doc_lattice`), the CLI executable
  (`doc-lattice`), the config file (only `.doc-lattice.yml` is recognized; no fallback), and
  the opt-in load-cache location (`<cache_home>/doc-lattice/`; old cache directories are
  orphaned and safe to delete). Doc sets themselves need no edits; lattice frontmatter
  (`id`, `derives_from`, `authority`, `seen`) is unchanged.

### Migration (v0.8.x to v0.9.0)

Nothing breaks until you bump your pin: checked-in gates pin a tag
(`uvx --from git+.../game-lattice@v0.8.0 game-lattice ...`) and GitHub's rename redirect keeps
that resolving indefinitely. Upgrading the pin to v0.9.0 requires, in one commit:

1. Rename `.game-lattice.yml` to `.doc-lattice.yml` (contents unchanged).
2. Regenerate the checked-in pre-commit hook and CI workflow (re-run `doc-lattice init`
   codegen, or by hand update the repo URL, the `@v0.9.0` pin, and the executable name
   `game-lattice` to `doc-lattice` in each invocation).
3. Any Python code importing `game_lattice` switches to `doc_lattice` (the package is not on
   PyPI and no import consumers are known; listed for completeness).
```

- [ ] **Step 3: Verify sync and release-notes extraction**

```bash
uv run --group dev python scripts/check_version_sync.py
env -u FORCE_COLOR uv run --group dev pytest tests/test_version_check.py -q
uv run --no-sync python scripts/extract_release_notes.py 0.9.0
```

Expected: guard passes; tests pass; the extractor prints the 0.9.0 section above (it becomes the GitHub release notes).

- [ ] **Step 4: Full suite, then commit**

```bash
env -u FORCE_COLOR uv run --group dev pytest -q
git add -A
git commit -m "chore: release 0.9.0"
```

---

### Task 7: Full verification sweep, end-to-end run, and PR

**Files:**
- Create: none (verification only, plus the PR)

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a green, merge-ready PR. Cutover (Task 8) must not start before this completes.

- [ ] **Step 1: Full quality gate**

```bash
env -u FORCE_COLOR uv run --group dev pytest
uv run --group dev ruff check src tests && uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
```

Expected: all pass, coverage >= 80%.

- [ ] **Step 2: End-to-end CLI run against the release-smoke fixture**

```bash
uv run doc-lattice check --config tests/fixtures/release-smoke/.doc-lattice.yml
uv run doc-lattice lint --config tests/fixtures/release-smoke/.doc-lattice.yml
workdir="$(mktemp -d)" && (cd "$workdir" && uv run --project "$OLDPWD" doc-lattice init) && grep -q 'doc-lattice' "$workdir/.doc-lattice.yml" && rm -rf "$workdir"
uv run doc-lattice --version
```

Expected: check and lint exit 0 on the fixture; init writes `.doc-lattice.yml` whose header names doc-lattice; version prints 0.9.0.

- [ ] **Step 3: The final grep sweep**

```bash
grep -rIn 'game[-_]lattice\|GAME_LATTICE' . --exclude-dir=.git --exclude-dir=.venv | grep -v '^\./CHANGELOG\.md' | grep -v '^\./docs/superpowers/specs/2026-07-11-doc-lattice-rename-design\.md'
```

Expected: no output. Every CHANGELOG hit must sit inside the 0.9.0 migration text or an entry for 0.8.0 or older (eyeball `grep -n 'game' CHANGELOG.md` to confirm).

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin rename/doc-lattice
gh pr create --title "feat!: rename game-lattice to doc-lattice" --body "$(cat <<'EOF'
Renames the project to doc-lattice per docs/superpowers/specs/2026-07-11-doc-lattice-rename-design.md.

Breaking (details and migration checklist in the 0.9.0 CHANGELOG entry):
- distribution/package: doc-lattice / doc_lattice; CLI executable: doc-lattice
- config file: only .doc-lattice.yml is recognized (clean break)
- opt-in load cache moves to <cache_home>/doc-lattice/
- repo URL: github.com/Guardantix/doc-lattice (rename happens at cutover, immediately before merge)

Kept: CHANGELOG history, lattice frontmatter vocabulary, game-flavored illustrative examples.

Cutover order (spec section "Order of operations"): this PR goes green first; the GitHub repo rename happens immediately before merge; the release job then cuts v0.9.0 on the renamed repo.
EOF
)"
```

- [ ] **Step 5: Wait for PR CI to go green**

```bash
gh pr checks --watch
```

Expected: all checks pass. Do not proceed to Task 8 otherwise.

---

### Task 8: Cutover and merge (USER CHECKPOINT)

**Files:**
- None in-repo. External: GitHub repository name, local git remote.

**Interfaces:**
- Consumes: green PR from Task 7.
- Produces: repo `Guardantix/doc-lattice`, tag `v0.9.0`, GitHub release with the 0.9.0 notes.

- [ ] **Step 1: Confirm with the owner before renaming the repo**

The repo rename is an external mutation a PR revert cannot undo. Confirm the owner is ready for cutover + merge now. Rollback while unmerged: `gh repo rename game-lattice` restores the old name; nothing on `main` references the new one.

- [ ] **Step 2: Rename the repo and update the remote**

```bash
gh repo rename doc-lattice --yes
git remote set-url origin git@github.com:Guardantix/doc-lattice.git
git ls-remote https://github.com/Guardantix/doc-lattice.git HEAD
git ls-remote https://github.com/Guardantix/game-lattice.git HEAD
```

Expected: both ls-remote calls print the same HEAD sha (new name live, redirect intact).

- [ ] **Step 3: Merge immediately**

```bash
gh pr merge --squash --delete-branch
```

- [ ] **Step 4: Watch the release job cut v0.9.0**

```bash
gh run watch --exit-status $(gh run list --branch main --limit 1 --json databaseId -q '.[0].databaseId')
gh release view v0.9.0
```

Expected: release job green (version sync, uvx smoke from the new URL, tag push, release notes). If it fails, apply the manual tag fallback in RELEASING.md unchanged.

- [ ] **Step 5: Post-release smoke from a clean environment**

```bash
uvx --python 3.13 --from git+https://github.com/Guardantix/doc-lattice@v0.9.0 doc-lattice --version
uvx --python 3.13 --from git+https://github.com/Guardantix/game-lattice@v0.8.0 game-lattice --version
```

Expected: `0.9.0` from the new name, and `0.8.0` from the old name via redirect (proves existing adopter pins keep working).

---

### Task 9: Local environment and memory migration (after merge)

**Files:**
- External: `~/workspace/repos/tooling/game-lattice` -> `~/workspace/repos/tooling/doc-lattice`; Claude memory dir `~/.claude/projects/-home-guardantix-workspace-repos-tooling-game-lattice/` -> `...-doc-lattice/`

**Interfaces:**
- Consumes: merged main.
- Produces: working checkout under the new path with memory intact.

- [ ] **Step 1: Sync main and prune**

```bash
git checkout main && git pull --ff-only && git fetch --tags && git remote prune origin
```

- [ ] **Step 2: Update project memory content**

Update `~/.claude/projects/-home-guardantix-workspace-repos-tooling-game-lattice/memory/`: add a `doc-lattice-rename-status` memory (rename shipped as v0.9.0, PR number, cutover done) and update `MEMORY.md`. Stale per-slice status memories keep their history; only correct anything the rename falsified.

- [ ] **Step 3: Rename the working directory and migrate memory (final act; kills the session cwd)**

```bash
mv ~/workspace/repos/tooling/game-lattice ~/workspace/repos/tooling/doc-lattice
mkdir -p ~/.claude/projects/-home-guardantix-workspace-repos-tooling-doc-lattice
cp -a ~/.claude/projects/-home-guardantix-workspace-repos-tooling-game-lattice/memory \
      ~/.claude/projects/-home-guardantix-workspace-repos-tooling-doc-lattice/memory
```

Then the owner reopens Claude Code from `~/workspace/repos/tooling/doc-lattice`.
