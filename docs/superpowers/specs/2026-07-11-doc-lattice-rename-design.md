# doc-lattice rename design

Date: 2026-07-11
Status: approved

## Goal

Rename the project from game-lattice to doc-lattice everywhere: GitHub repo, Python package,
CLI entry point, adopter config file, cache location, docs, and prose positioning. The engine
was never game-specific; the name should match the general purpose (traceability for design
and production documentation in any domain).

## Decisions (confirmed with the owner)

1. **GitHub repo is renamed** to `Guardantix/doc-lattice`. GitHub redirects the old URL for
   web, git, and API traffic, so existing `uvx --from git+...@v0.8.0` pins keep working.
2. **Config filename is a clean break.** Only `.doc-lattice.yml` is recognized from 0.9.0.
   No fallback to `.game-lattice.yml`, no deprecation shim. Release notes tell adopters to
   rename the file.
3. **Prose generalizes.** Descriptions say design and production documentation generally, not
   game docs. Game-flavored illustrative examples (art direction, player-character spec) stay;
   they are good examples and the conftest fixture built on them is load-bearing.
4. **Ships as v0.9.0** with an explicit BREAKING section in the changelog. Historical
   spec/plan docs under `docs/superpowers/` are renamed and updated (the local-core spec is
   binding and must not contradict the code). CHANGELOG history stays untouched; entries were
   true when written.

## Approach

One atomic rename PR, with the GitHub repo renamed first so the new repo URL baked into the
scaffold code already resolves. Alternatives rejected: staged PRs (every intermediate state is
internally inconsistent), and a fresh repo (loses history, issues, and redirects).

## Order of operations

1. **GitHub + remote.** `gh repo rename doc-lattice`, then
   `git remote set-url origin git@github.com:Guardantix/doc-lattice.git`.
2. **The rename PR**, branched off `main` at v0.8.0 (`rename/doc-lattice`):
   - Package/CLI: `git mv src/game_lattice src/doc_lattice`; pyproject `name`, entry point
     (`doc-lattice = "doc_lattice.cli:main"`), `packages`, coverage paths, Homepage; all
     imports across src and tests.
   - Breaking adopter surfaces: `DEFAULT_CONFIG_NAME = ".doc-lattice.yml"` (config.py);
     scaffold constant renamed to `DOC_LATTICE_REPO_URL` with the new URL and regenerated
     pre-commit/CI codegen text; cache path segment `<cache_home>/doc-lattice/` (old opt-in
     caches are orphaned, not migrated; they regenerate on first load);
     `tests/fixtures/release-smoke/.doc-lattice.yml` and the CI smoke step that reads it.
   - Prose: README, pyproject description, ARCHITECTURE.md, CLAUDE.md, RELEASING.md,
     roadmap.md, build-log.md, module docstrings, and CLI help/error strings.
   - Docs: rename the `docs/superpowers/` spec and plan files whose names contain
     game-lattice and update their content references.
   - Version: 0.9.0 in `src/doc_lattice/__init__.py`, pyproject, and a new CHANGELOG entry
     listing the breaking changes (config filename, CLI and package name, cache location,
     repo URL). `uv lock` regenerates the lockfile.
3. **Verification before the PR:** full pytest (with `env -u FORCE_COLOR`), ruff check and
   format, ty, the typing-boundary and version-sync scripts, an end-to-end
   `uv run doc-lattice check` against a fixture, and a repo-wide
   `grep -rI 'game.lattice'` sweep that must be empty outside CHANGELOG history and this
   spec (which necessarily names the old identity).
4. **Merge.** The existing release job on `main` verifies version sync, smoke-tests, and cuts
   the `v0.9.0` tag on the renamed repo. Post-merge smoke test:
   `uvx --from git+https://github.com/Guardantix/doc-lattice@v0.9.0 doc-lattice --help`.
5. **Local environment, last:** rename the working directory
   `~/workspace/repos/tooling/game-lattice` to `doc-lattice` and migrate the Claude memory
   directory to the new project-path key. This invalidates the running session's cwd, so it
   is the final act; the owner reopens Claude Code from the new path.

## Error handling and risks

- `gh repo rename` requires admin rights on the repo (the owner has them).
- If the release job fails after merge, the manual tag fallback in RELEASING.md applies
  unchanged.
- Old cache directories under `<cache_home>/game-lattice/` are stale garbage after upgrade;
  harmless, and adopters can delete them.
- No data or graph semantics change; the lattice frontmatter vocabulary (`id`,
  `derives_from`, `authority`, `seen`) is untouched, so adopter doc sets need only the
  config-file rename.

## Testing

The existing suite (723 tests, coverage gate 80 percent) already pins every renamed surface:
test_scaffold asserts the repo URL and codegen text, test_config asserts the config filename,
test_cache asserts the cache path, test_cli asserts help text. Updating those assertions is
part of the rename; no new test machinery is needed.
