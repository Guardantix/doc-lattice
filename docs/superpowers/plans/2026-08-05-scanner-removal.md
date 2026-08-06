# Scanner removal PR (extraction Phase 4) + 3.0.0

Removes the extracted shell scanner from doc-lattice. The scanner already lives, verbatim and
verified byte-identical modulo import renames, in `Guardantix/doc-lattice-shell-lint` (v0.1.0 on
PyPI, extracted at doc-lattice commit `38f00d6`). This PR deletes every scanner file, strips
shell-certification findings from `ci audit`, updates the owner documents, and bumps to 3.0.0.
Merging the PR triggers the 3.0.0 release pipeline (RELEASING.md), so the PR must be
release-complete but is NOT merged as part of this plan.

## Global Constraints

- **Removal only.** Delete scanner code and code that becomes dead *because of* this removal.
  No other refactoring, renaming, or improvement rides along.
- **ARCHITECTURE.md ADs 17 through 23 REMAIN in place as history.** The extracted repo's
  ARCHITECTURE points back at them. Do NOT delete or renumber them. The new decision record is
  **AD-25** (AD-24 is already "The supported Python floor is 3.13").
- `ci audit` keeps every structural finding: PULL_REQUEST_TARGET, LINEAR_SECRET_REFERENCE,
  MISSING_WORKFLOW_DIRECTORY, MISSING_MANAGED_ARTIFACT, MANAGED_MARKER, STALE_GENERATOR,
  REPOSITORY_IDENTITY, MANAGED_ATTRIBUTES, MISSING_MANAGED_WORKFLOW, MANAGED_TRIGGERS,
  MANAGED_PERMISSIONS, MANAGED_JOB, MANAGED_ACTION, MANAGED_CHECKOUT, MANAGED_CACHE,
  MANAGED_COMMAND, MANAGED_SECRET. It loses exactly two codes - **PR_LINEAR_INVOCATION** and
  **PR_MUTATING_RECONCILE** - and the exit-2 "shell scan incomplete" ConfigError outcome.
- Every module keeps its module docstring; Ruff line length 100; no em dashes in drafted prose;
  custom exceptions unchanged. No `typing.Any`/`cast` outside boundary modules.
- Run pytest as: `env -u FORCE_COLOR -u VIRTUAL_ENV uv run --group dev python -m pytest`
  (the dev shell exports FORCE_COLOR=3 and a stale VIRTUAL_ENV; both break things).
- Commit messages: conventional style matching repo history. **Never** add Claude attribution
  ("Generated with Claude Code", "Co-Authored-By: Claude", etc.) to commits or PR bodies.
- The suite must be green at the end of every task.

## Task 1: Delete the scanner and strip `ci audit` (code)

**Delete these files** (`git rm`):

- `src/doc_lattice/github_ci/shell_taint.py`, `src/doc_lattice/github_ci/shell_scanner.py`,
  `src/doc_lattice/github_ci/shell_guards.py`
- `tests/test_github_ci_shell_taint.py`, `tests/test_github_ci_shell_scanner.py`,
  `tests/test_github_ci_shell_guards.py`, `tests/test_fuzz_shell_taint.py`,
  `tests/test_corpus_differential.py`, `tests/test_workflow_corpus_differential.py`,
  `tests/test_check_guard_inventory.py`, `tests/test_guard_witness_sweep.py`,
  `tests/test_workflow_guard_debt.py`, `tests/test_github_ci_checkpoint.py`,
  `tests/test_workflow_shell_certification.py`, `tests/guard_witnesses.py`,
  `tests/test_worker_pool.py`
- `scripts/fuzz_shell_taint.py`, `scripts/corpus_differential.py`,
  `scripts/check_guard_inventory.py`, `scripts/guard_witness_sweep.py`,
  `scripts/checkpoint_manifest.py`, `scripts/checkpoint_derive_probe_spans.py`,
  `scripts/checkpoint_record_scanner_inputs.py`, `scripts/worker_pool.py`
- `tests/fixtures/github_ci_checkpoint/` (whole directory), `tests/fixtures/shell_guard_debt.json`,
  `tests/fixtures/shell_guard_retirements.json`, `tests/fixtures/shell_taint_fuzz_baseline.tsv`,
  `tests/fixtures/corpus_differential_acknowledgements.json`
- `docs/research/2026-07-bash-parser-benchmark/` and `docs/research/recognizer-benchmark/`
  (whole directories; the first contains its own `.gitattributes`, which goes with it)

**Strip `src/doc_lattice/github_ci/audit.py`** (997 lines today; line numbers from current main):

- `:30` delete `from .shell_scanner import direct_doc_lattice_invocations`
- `:38` delete `"direct_doc_lattice_invocations",` from `__all__`
- `:20` delete `from .path_display import display_path` (its only uses are in deleted code at
  263, 272, 278, 285 - verify before deleting)
- `:41–47` delete `PR_EVENTS` (only reader is the deleted block at :222)
- `:70–100` delete `_STATIC_SHELL_TEMPLATE_RE`, `_BASH_SHELL_EXECUTABLES`, `_BASH_LONG_FLAGS`,
  `_BASH_SHORT_FLAGS`, `_BASH_O_OPTIONS`, `_BASH_DEFAULT_RUNNERS`, `_SCRIPT_PLACEHOLDER`,
  `_SCRIPT_SENTINEL` and their comments
- `:222–244` delete the whole `if trigger_names & PR_EVENTS:` block (emits
  PR_LINEAR_INVOCATION and PR_MUTATING_RECONCILE)
- `:256–294` delete `_pr_step_invocations`; `:297–300` delete `_default_run_shell_is_bash`;
  `:303–324` delete `_supports_bash_run_body`; `:327–356` delete `_supports_bash_options`
- `:208–210` rewrite `audit_global_workflows` docstring `Raises:` clause (no longer raises
  from shell scanning); `:154–155` rewrite the `audit_repository` docstring sentence about
  "If a shell scan cannot complete"
- Keep the `ConfigError` import (still used at :442, :451, :550) and the `WorkflowJob`/
  `WorkflowStep` imports. Also remove the two message-dict entries for the retired codes if
  they live in the messages dict, and any now-unreferenced constant the removals orphan
  (verify with ruff/vulture-style reading, not guesswork).

**Remove the now-dead shell fields** (they lose their only reader, `audit.py:266`, with this
removal): `WorkflowStep.shell` (model.py:129), `WorkflowJob.default_shell` (model.py:142),
`WorkflowDocument.default_shell` (model.py:155); `workflow_parser._parse_default_shell` (:371)
and its call sites (:109, :320); delete
`tests/test_github_ci_workflow_parser.py::test_parse_workflow_normalizes_effective_shell_fields`
(:121–143). Before deleting, check whether `_parse_default_shell` has error/validation paths any
other test pins; if it can raise on malformed `shell:` values, note in your report that this
validation is retired (the structural comparison still catches `shell:` drift via
`_COMMAND_BEHAVIOR_FIELDS`). If removal turns out to cascade beyond these sites, stop and report
rather than widening the diff.

**Split `tests/test_github_ci_audit.py`** (2,899 lines, 118 tests):

- Delete every test in lines 42–1442 EXCEPT these three:
  `test_secret_name_regex_single_sources_from_secret_names` (:57),
  `test_global_audit_reports_target_secret_linear_and_mutating_reconcile` (:1028),
  `test_global_audit_allows_unrelated_release_workflow` (:1057).
- Rewrite `test_global_audit_reports_target_secret_linear_and_mutating_reconcile` to assert only
  the structural codes {PULL_REQUEST_TARGET, LINEAR_SECRET_REFERENCE} and rename it accordingly.
- Rewrite `test_global_audit_deduplicates_identical_findings_with_stable_details` (:1786): it
  currently asserts `[PR_LINEAR_INVOCATION, PULL_REQUEST_TARGET]`; keep it testing dedup with
  stable ordering using only structural finding sources.
- Delete `test_global_audit_documents_arbitrary_script_indirection_as_undetected` (:1816),
  `test_global_audit_fails_closed_on_cross_command_file_handoff` (:2859),
  `test_global_audit_does_not_aggregate_taint_across_run_steps` (:2884).
- Everything else in the file stays (secret-detection block :1473–1785, discover_workflows,
  inspect_installed_artifacts, managed-installation blocks).
- Also remove any helper functions/constants in that file that only the deleted tests used.

**Edit `tests/cli/test_ci.py`:**

- Delete the parametrize case `id="mutating-reconcile-on-pr"` (:752–761) from
  `test_ci_audit_reports_each_load_bearing_security_control_mutation`; keep the other cases.
- Delete `test_ci_audit_fails_closed_on_managed_marker_install_after_linear_pr_trigger`
  (:828–851) and `test_ci_audit_cross_command_marker_handoff_exits_two` (:1297–1330).

**Verify:** full pytest (coverage gate must pass; expect roughly 94–95% total), `ruff check src
tests scripts`, `ruff format --check src tests scripts`, `ty check src`,
`python scripts/check_typing_boundaries.py src`. Also
`grep -rn 'shell_taint\|shell_scanner\|shell_guards' src/ tests/ scripts/` must return nothing.

## Task 2: CI workflow and tooling config

- `.github/workflows/ci.yml`: delete the `guard-debt` job (:65–140) and the
  `corpus-differential` job (:142–311) in their entirety. Change `release.needs` (:315) from
  `[code-quality, tests, security-scan, guard-debt]` to `[code-quality, tests, security-scan]`.
  Touch nothing else in the file. Note `.github/workflows/claude.yml` needs no edit.
- `pyproject.toml`: remove `"shfmt-py==4.0.0"` from `[dependency-groups].dev` (:52) - it has
  zero code consumers, it was scanner-era residue. Remove the sdist `exclude` entry
  `"/tests/test_github_ci_checkpoint.py"` (:18).
- Run `uv lock` and commit the refreshed `uv.lock`.
- `.pre-commit-config.yaml`: remove the `end-of-file-fixer` exclude
  `'^docs/research/2026-07-bash-parser-benchmark/'` (:7).
- Regenerate `.secrets.baseline`: 602 of its 603 entries point at the deleted checkpoint
  fixtures. Regenerate with the same detect-secrets version and plugin set the pre-commit hook
  uses (read `.pre-commit-config.yaml:43–47` for the exact invocation and args; typically
  `detect-secrets scan --baseline .secrets.baseline` updates in place, or a fresh
  `detect-secrets scan > .secrets.baseline` matching the configured filters). The result must
  keep the hook green: run `pre-commit run detect-secrets --all-files` to prove it.
- **Verify:** full pytest again (`tests/test_release_workflow.py` and
  `tests/test_workflow_pinning.py` must pass against the edited ci.yml), plus
  `pre-commit run --all-files` (re-stage anything a hook rewrites).

## Task 3: Owner documents, AD-25, CHANGELOG, 3.0.0

- **README.md:**
  - Delete the shell-certification contract block, lines 645–712 (from "Audit recognizes direct
    Bash and `sh` invocations…" through "…modeled for function contexts only.").
  - Rewrite the audit-contract sentence around :642–645: drop the "direct Linear invocations
    under pull-request events, and direct mutating reconcile invocations under those events"
    clauses. Where the old text told readers not to rely on the shell analysis, replace with a
    short paragraph: shell run-body linting was extracted to the standalone
    [doc-lattice-shell-lint](https://github.com/Guardantix/doc-lattice-shell-lint) tool
    (`uvx doc-lattice-shell-lint`); `doc-lattice ci audit` performs no shell analysis, and anyone
    wanting that lint runs the standalone tool as its own explicit workflow step.
  - `:831` project-structure comment: drop ", shell scanner". `:836` scripts comment: drop the
    "CI guards plus" framing (now slug generation, section benchmark, release tooling).
  - Bump all three `doc-lattice==2.0.0` install pins (:455, :520, :559) to `doc-lattice==3.0.0`.
- **CLAUDE.md:** delete the scanner command lines from the contributor block (the
  `check_guard_inventory.py`, `guard_witness_sweep.py`, `corpus_differential.py`,
  `fuzz_shell_taint.py` invocations, :39–55 region) and the five scanner prose blocks
  (:63–184). Rewrite the handoff-verification sentence (:228–230) to drop the
  `check_guard_inventory.py` clause. Keep the `docs/` paragraph (it states policy, not
  inventory). Keep everything else byte-identical.
- **ARCHITECTURE.md:** append **AD-25** after AD-24. Required content (draft prose, match the
  file's existing AD voice and formatting):
  - Title: `### AD-25: The CI shell scanner is extracted to doc-lattice-shell-lint`
  - The scanner (`shell_taint`, `shell_scanner`, `shell_guards`) and its verification harness
    (differential fuzzer, frozen-corpus differential, guard inventory gate, witness sweep,
    checkpoint fixtures) moved verbatim to `Guardantix/doc-lattice-shell-lint`, extracted at
    doc-lattice commit `38f00d6` and released independently on PyPI.
  - The repositories are fully severed: no runtime, build, or CI dependency in either
    direction. `ci audit` performs no shell analysis; finding codes PR_LINEAR_INVOCATION and
    PR_MUTATING_RECONCILE are retired, as is the exit-2 "shell scan incomplete" outcome. An
    audit's contract must not vary with what is installed, so an optional-import integration
    was rejected; so were a hard dependency and deletion (see the extraction decision record's
    reasoning; restate the essentials rather than linking a private path).
  - ADs 17 through 23 remain in this document as the history of the extracted subsystem; their
    live successors are maintained in doc-lattice-shell-lint's own ARCHITECTURE (SL-1).
  - Rationale: AD-23 froze the scanner as an accident lint with an approximately-never release
    cadence; an 8k-line traceability engine carrying a 50k-line frozen scanner misstates the
    project's identity and taxes every contributor gate.
- **CHANGELOG.md:** under `[Unreleased]`, delete the four scanner bullets (:11–14, :25–27,
  :28–31, :35–38 - they describe scanner changes that now ship in doc-lattice-shell-lint's
  history instead). Promote `## [Unreleased]` to `## [3.0.0] - 2026-08-05` following the exact
  pattern of the 2.0.0 release (check `git log` for how 2.0.0 did it, including whether a fresh
  empty `[Unreleased]` section is kept). Add to the 3.0.0 section:
  - **Removed:** the CI shell scanner and its verification harness, extracted to
    doc-lattice-shell-lint; `ci audit` finding codes `PR_LINEAR_INVOCATION` and
    `PR_MUTATING_RECONCILE` retired; the exit-2 unsupported-shell-semantics audit outcome
    retired. Migration: run `doc-lattice-shell-lint` as its own workflow step to keep the lint.
  - A sentence on why this is major: `ci audit` reporting fewer finding classes is a gate
    becoming more permissive, which a consumer could silently depend on.
- **Version bump:** `src/doc_lattice/__init__.py:3` and `pyproject.toml:26` to `3.0.0`
  (README pins covered above; CHANGELOG heading covered above). Run `uv lock` again if
  pyproject changed (version field changes the lock's own-package entry).
- **Verify:** `python scripts/check_version_sync.py`; full pytest (note
  `tests/test_package_metadata.py` asserts content in README/ARCHITECTURE/CHANGELOG - it must
  pass); `git diff --check`; a relative-link check over the edited Markdown (verify every
  `](...)` target still exists, especially README links into ARCHITECTURE anchors - ADs 17–23
  survive, so links to them stay valid); `grep -rn` for
  `shell_taint|shell_scanner|shell_guards|guard_witness|guard inventory|corpus differential|fuzz` over README.md CLAUDE.md
  CHANGELOG.md RELEASING.md roadmap.md - the only intended survivors are inside ARCHITECTURE.md
  ADs 17–23 and the historical released sections of CHANGELOG.md.
