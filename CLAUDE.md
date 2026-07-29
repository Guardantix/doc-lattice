# CLAUDE.md

doc-lattice is a deterministic traceability engine for dependencies between Markdown documents.

## Authoritative sources

- [README.md](README.md) owns supported user behavior, configuration, commands, and examples.
- [ARCHITECTURE.md](ARCHITECTURE.md) owns durable decisions and pure/impure module boundaries.
- [CHANGELOG.md](CHANGELOG.md) owns release history and migrations.
- [RELEASING.md](RELEASING.md) owns the release procedure.
- [roadmap.md](roadmap.md) owns future direction.

When behavior or policy changes, update its owner and link to it. Do not restate the same contract
in another maintained document.

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

uv run --group dev python scripts/check_guard_inventory.py

uv run python scripts/fuzz_shell_taint.py --self-check
uv run python scripts/fuzz_shell_taint.py \
  --iterations 1200 --seed 1 --baseline tests/fixtures/shell_taint_fuzz_baseline.tsv
```

Pre-commit runs formatting, linting, type and boundary checks, version sync, secret detection,
and repository hygiene checks. If a hook changes a file, re-stage it before committing.

`scripts/fuzz_shell_taint.py` differentially tests the CI shell taint analysis against real Bash.
It generates run bodies from a compositional grammar, executes each one, and reports every body
Bash runs the authored marker in that the scanner certified. Run `--self-check` first: it validates
the execution oracle in both directions, and a fuzz result means nothing if that fails. The
baseline records the known-failing recipes tracked in the open `security` issues, so a run exits
non-zero only for a signature outside it.

The baseline is specific to the seed and iteration count it was captured at, which is the seed 1
run above. Other seeds surface further signatures, most of them the same known families, so a clean
gate run is evidence about that seed rather than proof that no unbaselined signature exists. Widen
the search with another seed when a change is broad, and triage what it finds against the open
issues. Regenerate the baseline with `--write-baseline` only when those issues are fixed, never to
silence a new finding and never to absorb another seed's signatures.

To score a hand-written body instead of a generated one, import `Case`, `Recipe`, and `execute`
from that script and pair `execute` with `scan_doc_lattice_invocations`. That is how a suspected
false certification and its control get confirmed before an issue is filed.

Changes to the taint analysis or the scanner should be checked against this tool as well as
pytest, since the suite pins known behavior while the fuzzer searches for behavior nobody has
pinned yet.

`scripts/check_guard_inventory.py` gates fail-closed guard identity, described by AD-20 in
[ARCHITECTURE.md](ARCHITECTURE.md). Every guard origin constructs a `GuardRefusal` with a literal
identifier and literal reason, and `tests/guard_witnesses.py` classifies each one by carrying
executable evidence: a script that reaches it through the public scan path, or a rationale plus a
boundary script. Anything unclassified is frozen in `tests/fixtures/shell_guard_debt.json`, which
may only shrink.

A guard must also sit in a function some public entry point of its own module reaches, and its
record covers the controls at its function's call sites, so withdrawing a guard by orphaning or
diverting around its function fails the gate rather than passing silently.

A boundary witness carries a predicate over the evidence its script builds, and that predicate must
read something the guard's own condition reads. The gate derives which attributes those are from the
guarded module, so a predicate over unrelated evidence is rejected however plausible it reads.

When you add or move a guard, add its classification to `tests/guard_witnesses.py`; when adding its
module, add that module to `GUARDED_MODULES` too. The base-owned comparison discovers guarded
modules recursively from the candidate tree, so an older base checker can validate the new origin.
Regenerate the debt snapshot only when a guard legitimately moves, never to silence a new one: CI
runs the base revision's copy of the checker against your tree and rejects any record the base did
not carry. When you remove a guard, record the identifier and the reason in
`tests/fixtures/shell_guard_retirements.json`; that same base-owned run rejects an origin the base
classified or froze that your tree no longer constructs.

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
- Do not call `datetime.now()` or `datetime.utcnow()` outside `datetime_utils.py`.
- Keep `src/doc_lattice/__init__.py`, `pyproject.toml`, the first versioned CHANGELOG heading,
  and exact README install pins synchronized. Run `scripts/check_version_sync.py` for every
  documentation or release change that can affect those values.
- Section identity is pinned to `markdown-it-py==4.2.0` and a `github-slugger@2.0.0` target.
  Never hand-edit `_github_slugger_data.py`. Node is a maintenance-only dependency for generator
  verification. Adapter, dependency, Unicode, or generated-data changes require the generator
  check, relevant parity tests, and `scripts/bench_sections.py`.
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
