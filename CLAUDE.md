# CLAUDE.md

doc-lattice is a deterministic traceability engine for dependencies between Markdown documents.

## Authoritative sources

- [README.md](README.md) owns supported user behavior, configuration, commands, and examples.
- [MANAGED_CI.md](MANAGED_CI.md) owns the hand-installable recipe for protected Linear CI, the
  procedure for converting a managed installation to it, and the security model the recipe
  rests on.
- [RECONCILE.md](RECONCILE.md) owns the reconcile selector, dry-run, transaction durability, and
  recovery contract.
- [ARCHITECTURE.md](ARCHITECTURE.md) owns durable decisions and pure/impure module boundaries.
- [CHANGELOG.md](CHANGELOG.md) owns release history and migrations.
- [RELEASING.md](RELEASING.md) owns the release procedure, release authority, the bad-release
  playbook, and the accounts and access surface.
- [SECURITY.md](SECURITY.md) owns supported versions, the private vulnerability reporting path,
  the security scope boundary, and the disclosure expectation.
- [ROADMAP.md](ROADMAP.md) owns future direction.

When behavior or policy changes, update its owner and link to it. Do not restate the same contract
in another maintained document.

`docs/` is a staging area, not an archive. Nothing under it is authoritative or maintained;
update the owner documents above instead. Per the ownership decision in
[ARCHITECTURE.md](ARCHITECTURE.md) (AD-14), anything under it that is not presently
authoritative is mined for durable content and deleted at release.

## Contributor commands

Use Python 3.13 or later and run dependency management and project commands through `uv`.

```bash
uv sync --group dev
uv run --group dev pre-commit install
uv run doc-lattice --help

uv run --group dev pytest
uv run --group dev pytest tests/test_loader.py::test_duplicate_id_raises
uv run --group dev pytest tests/test_check.py -v

uv run --group dev ruff check src tests scripts
uv run --group dev ruff format --check src tests scripts
uv run --group dev ty check src scripts
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
uv run --group dev python scripts/check_doc_links.py
uv run --group dev python scripts/generate_github_slugger_data.py --check
uv run --group dev python scripts/bench_sections.py
```

Unset `FORCE_COLOR` when your shell sets it, since forced color breaks human-output assertions.

`pre-commit install` is a required setup step and has to be run once per clone, since Git hooks
are not carried in a checkout. Pre-commit runs formatting, linting, type and boundary checks,
version sync, secret detection, and repository hygiene checks. If a hook changes a file, re-stage
it before committing.

## Enforced repository rules

- Keep production code compatible with the supported Python versions and use `uv`, not ad hoc
  environment or dependency tooling.
- Before moving logic across an I/O boundary or changing which module owns an effect, consult
  [ARCHITECTURE.md](ARCHITECTURE.md) and update the relevant decision when the boundary changes.
  That source defines the `persistence.py` and `reconcile_transaction.py` ownership boundaries.
- `typing.Any` and `typing.cast` are limited to the boundary modules AD-3 names, which
  `scripts/check_typing_boundaries.py` holds as an exact allowlist of source-root-relative paths
  rather than a name pattern. Validate untyped YAML and JSON at those boundaries, then pass typed
  models through the rest of the engine. Opening a new boundary is an allowlist edit and an AD-3
  edit, never a rename that happens to match.
- Custom exceptions extend `ProjectError`, carry a code, and give actionable context. Do not add
  bare `except Exception` or `except BaseException` catches. The one exception is a signal type
  at an I/O boundary whose builtin ancestry is what classifies it: `cli/pipe_policy.py`'s
  `PipeClosed(BrokenPipeError)` and `persistence.py`'s `DestinationExistsError(FileExistsError)`
  are matched by Typer's `except OSError` and by `reconcile_transaction.py` respectively, and
  neither is a diagnostic a user can receive. Such a type stays a builtin subclass, lives with
  the boundary it serves rather than in `error_types.py`, and pins its ancestry with a test.
  Making one a `ProjectError` would give it a code, and `tests/test_error_types.py` requires the
  `ErrorCode` domain to be exactly the codes some type raises, so it would also require a README
  table row for a code nothing can report.
- A string domain that needs both a `Literal` type and a runtime value set keeps one source of
  truth in `constants.py`: derive the `frozenset` from the type with `get_args()`, and import
  those declarations instead of duplicating raw values. A domain that needs only the type may
  stay with the module that owns its semantics.
- Resolve user-controlled paths with `path_utils.safe_resolve()` at the owning boundary and
  preserve project-root containment. Reconcile destinations and recovery evidence require the
  independent containment checks recorded in [ARCHITECTURE.md](ARCHITECTURE.md).
- Do not call `datetime.now()` or `datetime.utcnow()` outside `datetime_utils.py`, the AD-2
  impure time boundary. Call `datetime_utils.utc_now()` instead, so the pure modules stay
  testable against fixed inputs and a deterministic clock has one function to substitute.
- Keep `src/doc_lattice/__init__.py`, `pyproject.toml`, the first versioned CHANGELOG heading,
  and the exact install pins in README.md and MANAGED_CI.md synchronized. Run
  `scripts/check_version_sync.py` for every documentation or release change that can affect those
  values. Release surfaces are declared there, not discovered: `PIN_MANIFEST` carries the exact
  recognized-pin count for each document that pins a live release, `HISTORICAL_PIN_DOCS` exempts
  CHANGELOG.md's preserved migration pins, and a recognized pin in any other maintained document
  fails as an unclassified release surface. Changing how many pinned install refs a document
  carries is therefore an enrollment decision: edit the count in the same change, and enroll a new
  document rather than letting it pin a release unenforced. The count closes any change that
  alters that number, never one that preserves it: a deletion compensated by a new current pin in
  the same document, and a spelling `_PINNED_REF` does not recognize, both pass.
- `scaffold.PYTHON_PIN` and the `requires-python` lower bound are both copies of the AD-24 floor
  and are held to each other by a parsed correspondence test in `tests/test_conventions.py`.
  Change both or neither. Ruff's `target-version`, the CI matrix, and the slugger generator's
  default interpreter are further machine-consumed copies that no gate correlates yet.
- `scripts/check_doc_links.py` resolves every relative Markdown link and `#anchor` in the
  maintained documents, which it takes as the sorted root `*.md` files. A target may be any
  repository-contained relative path, `docs/` staging included; absolute and external
  destinations are out of scope. Write destinations as Markdown links: a raw HTML anchor is
  reported rather than resolved, because markdown-it normalizes a Markdown destination and an
  attribute value arrives with none of that done, so resolving one means owning URL and HTML
  attribute semantics this gate does not take on. Fragments resolve against a link-target
  heading inventory the gate builds for itself, so renaming a heading or moving a file fails the
  hook and the CI code-quality job rather than breaking a deep link silently.
- That link-target inventory is deliberately separate from doc-lattice's section identity. It
  reads the gate's own full CommonMark parse, so it covers every heading form GitHub assigns an
  id to -- setext, ATX indented one to three spaces, and headings nested in a list item or a
  block quote -- while the addressable subset stays column-zero ATX only. Keep the separation:
  accepting a valid deep link by widening `extract_headings` instead would change which sections
  the engine sees, which is a cached-derivation change costing a `CACHE_VERSION` bump and an
  edit to README.md's addressable-subset paragraph and AD-13. Both inventories share one slug
  and collision implementation, `markdown_compat.github_ids_for_texts`, so a heading both see
  resolves to the same id. Use it for GitHub heading ids: `github_slug` is a base slug with no
  deduplication, and `anchor_ids` answers a different question, doc-lattice's explicit
  `{#anchor}` identity. Rendered inline heading text is out of reach on both sides, since ids
  are slugged from raw inline source rather than rendered text.
- Section identity is pinned to `markdown-it-py==4.2.0` and a `github-slugger@2.0.0` target.
  Never hand-edit `_github_slugger_data.py`. Node is a maintenance-only dependency for generator
  verification. Adapter, dependency, Unicode, or generated-data changes require the generator
  check, relevant parity tests, and `scripts/bench_sections.py`.
- Regenerating or verifying `_github_slugger_data.py` requires the exact Node version in
  [.nvmrc](.nvmrc); run `nvm use` first, since the generator rejects any other version and a
  partial pin would let an ICU update change the artifact bytes. Upstream input is the
  checksummed tarball in [vendor/](vendor/README.md), so both paths run offline and
  `--package-root` is only an explicit override. Never bump the Node pin, the tarball, or its
  digest without regenerating and re-running the parity tests and benchmark.
- Ruff uses a 100-character line length. Every module needs a module docstring, and public
  functions use Google-style docstrings. Do not use em dashes in drafted content.

## Testing expectations

- Mirror source modules in tests: `src/doc_lattice/foo.py` maps to `tests/test_foo.py`.
- Mirror CLI command adapters under `tests/cli/`; keep cross-command behavior in
  `tests/cli/test_contract.py`.
- Use `tmp_path` for filesystem tests and keep pure logic testable with synthetic inputs.
- Treat the shared `tests/conftest.py` `lattice_dir` fixture as load-bearing. Changes to its
  documents can alter check, reconcile, and CLI expectations across many suites.
- Run a focused test while iterating, then run the complete verification set before handoff.
  The full pytest suite enforces coverage of at least 80 percent.

For Markdown-only changes, at minimum run `scripts/check_version_sync.py`,
`scripts/check_doc_links.py`, and `git diff --check`. Run the full suite when commit hooks do not
execute it. For production changes, the complete handoff verification is pytest, Ruff check and
format check, `ty`, typing boundaries, version sync, doc links, and any generator or benchmark
gate affected by the change.
