# CLAUDE.md

doc-lattice is a deterministic traceability engine for dependencies between Markdown documents.

## Authoritative sources

- [README.md](README.md) owns supported user behavior, configuration, commands, and examples.
- [MANAGED_CI.md](MANAGED_CI.md) owns the managed GitHub and Linear CI setup, its migration
  procedure, and its security model.
- [RECONCILE.md](RECONCILE.md) owns the reconcile selector, dry-run, transaction durability, and
  recovery contract.
- [ARCHITECTURE.md](ARCHITECTURE.md) owns durable decisions and pure/impure module boundaries.
- [CHANGELOG.md](CHANGELOG.md) owns release history and migrations.
- [RELEASING.md](RELEASING.md) owns the release procedure, release authority, the bad-release
  playbook, and the accounts and access surface.
- [SECURITY.md](SECURITY.md) owns supported versions, the private vulnerability reporting path,
  the security scope boundary, and the disclosure expectation.
- [roadmap.md](roadmap.md) owns future direction.

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
uv run doc-lattice --help

uv run --group dev pytest
uv run --group dev pytest tests/test_loader.py::test_duplicate_id_raises
uv run --group dev pytest tests/test_check.py -v

uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
uv run --group dev python scripts/generate_github_slugger_data.py --check
uv run --group dev python scripts/bench_sections.py
```

Unset `FORCE_COLOR` when your shell sets it, since forced color breaks human-output assertions.

Pre-commit runs formatting, linting, type and boundary checks, version sync, secret detection,
and repository hygiene checks. If a hook changes a file, re-stage it before committing.

## Enforced repository rules

- Keep production code compatible with the supported Python versions and use `uv`, not ad hoc
  environment or dependency tooling.
- Before moving logic across an I/O boundary or changing which module owns an effect, consult
  [ARCHITECTURE.md](ARCHITECTURE.md) and update the relevant decision when the boundary changes.
  That source defines the `persistence.py` and `reconcile_transaction.py` ownership boundaries.
- `typing.Any` and `typing.cast` are limited to boundary modules recognized by
  `scripts/check_typing_boundaries.py`. Validate untyped YAML and JSON at those boundaries, then
  pass typed models through the rest of the engine.
- Custom exceptions extend `ProjectError`, carry a code, and give actionable context. Do not add
  bare `except Exception` or `except BaseException` catches.
- Shared string domains use the `Literal` plus `get_args()` plus `frozenset` pattern in
  `constants.py`. Import those constants instead of duplicating raw values.
- Resolve user-controlled paths with `path_utils.safe_resolve()` at the owning boundary and
  preserve project-root containment. Reconcile destinations and recovery evidence require the
  independent containment checks recorded in [ARCHITECTURE.md](ARCHITECTURE.md).
- Do not call `datetime.now()` or `datetime.utcnow()` outside `datetime_utils.py`. No such module
  exists today; recreate it if the engine ever needs the current time again.
- Keep `src/doc_lattice/__init__.py`, `pyproject.toml`, the first versioned CHANGELOG heading,
  and the exact install pins in README.md and MANAGED_CI.md synchronized. Run
  `scripts/check_version_sync.py` for every documentation or release change that can affect those
  values.
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

For Markdown-only changes, at minimum run the version-sync guard, a relative-link check, and
`git diff --check`. Run the full suite when commit hooks do not execute it. For production changes,
the complete handoff verification is pytest, Ruff check and format check, `ty`, typing boundaries,
version sync, and any generator or benchmark gate affected by the change.
