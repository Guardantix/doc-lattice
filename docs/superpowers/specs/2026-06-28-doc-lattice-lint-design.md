# doc-lattice Lint Slice: Design Spec

**Date:** 2026-06-28
**Status:** Design (post brainstorm, post adversarial review). Ready for implementation planning.
**Scope:** Structural coherence plus its gate wiring. One `lint` command that validates the lattice's
authority ladder, the codegen update that makes the gate real for adopters, and the 0.3.0 release
that ships them. No network, no secrets, no LLM, no mutation in the command itself. The command reads
the already-loaded lattice and reports; the only disk and codegen changes live in the existing `init`
scaffolding path.
**Builds on:** `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md` (the local
core, which parses and stores `authority`) and
`docs/superpowers/specs/2026-06-28-doc-lattice-init-design.md` (the init scaffolding and release
model this slice extends).

This spec turns the deferred "Authority-ladder validation" item from the local-core deferral map
(section 12, "`authority` is parsed, stored, rendered; not policed here") into a buildable design. It
does not re-open any locked decision from the local core. The `authority` field, its `Authority`
literal, and its presence on `Node` already exist; this slice adds the policing the local core left
out, makes that policing observable rather than silently partial, and wires it into the generated
gates so adopters actually run it.

## 1. Scope

In scope:

- A single `lint` command that validates one structural invariant of the loaded lattice: the
  authority ladder over `derives_from` edges. The command is pure over the loaded lattice (no network
  call, no file write, no mutation); it loads the lattice exactly as `check` does, classifies edges,
  renders, and sets an exit code.
- Skip observability. Edges the ladder cannot judge (an endpoint without `authority`) are reported,
  not silently dropped, so a clean result is distinguishable from an unjudged corpus.
- Codegen wiring. Update the `init` scaffolding (`scaffold.py` and its tests) so the generated
  pre-commit hook and CI workflow run both `doc-lattice check` and `doc-lattice lint`. Adopters then
  run the ladder check automatically; it enforces every detectable inversion (both endpoints
  annotated) and prints its skip coverage, rather than living only as a manual command.
- Shipping this as the 0.3.0 release (section 9): the generated snippets pin a release tag, and that
  tag must contain `lint`, so the slice bumps the version and cuts `v0.3.0` as one atomic step under
  the existing `RELEASING.md` rule.
- v1 contains exactly one lint rule (authority-ladder). The command is the designated home for future
  lattice lints; the roadmap's "display-prefix lint" lands here next without a new command.
- Updating `roadmap.md`, `CLAUDE.md`, and `RELEASING.md` as the closing step of the slice, so the PR
  is atomic and self-consistent: move "Authority-ladder validation" into a shipped entry, list `lint`
  in the command set and architecture, and extend the release invariant to name `lint`.

Explicitly out of scope, deferred or declined (see section 13):

- A `layer`-ladder check. `layer` (`design | technical | production`) is a partition, not cleanly a
  strength ladder in the consuming corpus (a design rationale doc legitimately derives from a
  technical binding contract), so it is not policed. This slice is authority only.
- A strict mode that fails on unannotated edges. v1 reports skips but never fails on them; turning a
  missing annotation into a failure (with an allowlist) is a future `--strict` opt-in, kept out
  because it reverses the local core's locked "authority is optional" decision.
- Per-rule severity and a config toggle to enable or disable the rule. The rule is always on; a
  violation always exits 1.
- Any change to `check`, `reconcile`, `graph`, `linear`, or the `init` command's behavior. The only
  `init`-adjacent change is the text the scaffolding emits (section 8); the command's flags, write
  semantics, and idempotence are untouched.

## 2. The rule

Authority has three values forming a ladder, strongest last:

```
exploratory  <  derived  <  binding
```

A `derives_from` edge `X derives_from Y` means X rests on Y: Y is the upstream artifact X is derived
from. For the ladder to hold, Y must be at least as authoritative as X. Writing `rank(a)` for an
authority's 1-based position on the ladder (`exploratory` = 1, `derived` = 2, `binding` = 3):

- For every resolved edge where **both** endpoints declare `authority`, require `rank(Y) >= rank(X)`.
- A **violation** is `rank(Y) < rank(X)`: a node derives from something weaker than itself. The
  offending cases are `binding` from `derived`, `binding` from `exploratory`, and `derived` from
  `exploratory`.
- Equal authority is allowed. Deriving from something stronger is allowed (the healthy direction).

This is the "narrower binding contracts beat broader summaries" precedence rule of the consuming doc
set, expressed as a graph constraint: a binding contract that depends on an exploratory doc has
inverted its own ladder. The healthy arteries (a `derived` tokens doc deriving from a `binding` art
direction, tickets deriving from that tokens doc) all pass.

## 3. Classification and skip rules

Each edge is classified in a fixed order, and an edge the ladder cannot judge is recorded as a skip
rather than dropped silently:

1. **Broken edge** (`target_id is None`): not lint's concern. It is skipped and not counted in lint's
   skip summary, because `check` already reports it as `BROKEN` and exits 1. Counting it here would
   double-report the same defect.
2. Otherwise resolve the target authority (section 4.1).
3. **Source unannotated** (`source_authority is None`): skipped, reason `source-unannotated`.
4. **Target unannotated** (the owning file's `authority is None`): skipped, reason
   `target-unannotated`.
5. Otherwise compare ranks: a violation if `rank(target) < rank(source)`, else a pass.

`authority` is optional, and much of an adopting corpus will not carry it, so skips are expected. The
rule fires only when both ends declare a value; an unrankable end is not a violation. Because a clean
result on an unannotated corpus would otherwise be indistinguishable from real coverage, the skipped
edges (reasons `source-unannotated` and `target-unannotated`) are surfaced in both renderings
(section 7). Equal authority is judged and passes; it is not a skip.

## 4. Data model and pure core

A new pure module `lint.py`, mirroring the shape of `check.py` (frozen result types plus a
lattice-walking classifier), filesystem-free and network-free:

```python
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
    reason: SkipReason  # "source-unannotated" | "target-unannotated"


@dataclass(frozen=True, slots=True)
class LintResult:
    """The full outcome: violations that fail the gate, plus the unjudged skips."""

    violations: tuple[LadderViolation, ...]
    skipped: tuple[SkippedEdge, ...]


def lint_lattice(lattice: Lattice) -> LintResult:
    """Classify every edge into a violation, a skip, or a silent pass, in node-id then edge order."""
```

`lint_lattice` walks `sorted(lattice.nodes_by_id)` and each node's `derives_from` in order (the same
determinism as `check_lattice`), applies the classification of section 3, and returns the violations
and skips in that order. Both authorities stored on a `LadderViolation` are non-`None` by
construction, and a `SkippedEdge.target_id` is always a resolved id, because broken edges are filtered
first. Ranking is a small pure helper internal to the module that reads the ordered ladder constant
(section 5); no module-level mutable state.

### 4.1 Resolving each endpoint's authority

The two endpoints are resolved differently, and the target case is the load-bearing one:

- **Source authority** is direct. A `derives_from` entry lives in a tracked file's frontmatter, and
  the walk iterates `nodes_by_id`, so the source is always a file node and `source_authority` is that
  node's `authority`.
- **Target authority** must be resolved, because `target_id` may be a **file id or a section anchor
  id**. Both kinds resolve through the same path index `resolve.target_content` already uses:
  `lattice.index[target_id].path` names the owning file, `lattice.file_id_by_path[path]` gives its
  node id, and that node's `authority` is the target authority. A section anchor carries no authority
  of its own; it inherits the authority of the file that owns it.

This matters because binding edges are section-level by design (the arteries are annotated at
`file#anchor` granularity), so section targets are the common case, not an edge case. The lint must
never assume `target_id` is a key in `nodes_by_id`: indexing `nodes_by_id[target_id]` directly would
raise `KeyError` on a valid section ref, breaking the gate precisely for the dependencies it most
needs to police. lint reuses the existing `resolve` path-to-node resolution (the helper behind
`target_content`) rather than duplicating it; promoting that private helper to a shared importable one
is a plan-stage detail. Resolution is total for any non-broken `target_id`, since every anchor's path
is a tracked file's path.

## 5. Constants

`constants.py` gains the ordered ladder and the skip-reason literal, following the established
data-only pattern (literal plus a derived structure, no logic):

```python
Authority = Literal["binding", "derived", "exploratory"]
VALID_AUTHORITIES: frozenset[str] = frozenset(get_args(Authority))
AUTHORITY_LADDER: tuple[Authority, ...] = ("exploratory", "derived", "binding")

SkipReason = Literal["source-unannotated", "target-unannotated"]
VALID_SKIP_REASONS: frozenset[str] = frozenset(get_args(SkipReason))
```

`AUTHORITY_LADDER` is the single source of strength order, ascending. A node's rank is its 1-based
index in the tuple, computed in `lint.py`. A `tests/test_conventions.py` assertion fixes the two
representations together: `frozenset(AUTHORITY_LADDER) == VALID_AUTHORITIES`, so adding an authority
value without placing it on the ladder fails CI rather than silently ranking it as missing.

## 6. Command surface and exit codes

A new Typer command `lint`, wired in `cli.py` exactly like `check`:

```python
@app.command()
def lint(config: ConfigOpt = None, json_out: JsonOpt = False) -> None:
    """Validate the authority ladder; exit 1 on a violation, 2 on tool error."""
```

- It loads the lattice through the existing `_load(config)` helper, catching `ProjectError` and
  exiting **2** with the standard `error: <message> (<code>)` line, identical to `check`.
- It runs `lint_lattice`, renders (section 7), and exits **1** if `result.violations` is non-empty,
  else **0**. Skips never affect the exit code.

Exit codes match `check` and the project's CI-gate ethos: 0 clean, 1 a real defect the corpus owner
must resolve, 2 a tool or load error. A clean `lint` and a clean `check` together gate a PR, and after
section 8 the generated codegen runs both.

The gate enforces the ladder wherever it is checkable, meaning an edge with both endpoints annotated.
It never fails on an edge it cannot rank, in keeping with the locked optional-authority model, so
enforcement coverage scales with how much of the corpus is annotated. This is a deliberate tradeoff,
not full enforcement: a real inversion is only ever defined once both endpoints carry authority. The
skip summary (section 7) prints on every run, including in CI, so a green gate reports how much it
judged rather than implying the whole graph was verified. A fail-on-unannotated mode is deferred
(section 13).

## 7. Output

Two renderings, selected by `--json`, mirroring `check`'s split. All node-derived strings pass
through `rich.markup.escape` before printing, per the repo's markup-escaping convention.

Human (default): one red line per violation, then a single always-printed summary line that names the
skip coverage, so a passing run states what it did and did not judge:

```
VIOLATION  art-direction (binding) -> pc-design#tokens (derived)
1 ladder violation, 3 edges unranked (2 target unannotated, 1 source unannotated)
```

A fully clean, fully annotated run prints `0 ladder violations, 0 edges unranked` and exits 0.

JSON (`--json`): a single object with a `violations` array and a `skipped` array:

```json
{
  "violations": [
    {
      "source_id": "art-direction",
      "source_authority": "binding",
      "target_id": "pc-design-tokens",
      "target_ref": "pc-design#tokens",
      "target_authority": "derived"
    }
  ],
  "skipped": [
    {
      "source_id": "vision-brief",
      "target_ref": "market-scan",
      "target_id": "market-scan",
      "reason": "target-unannotated"
    }
  ]
}
```

A fully clean, fully annotated run emits `{"violations": [], "skipped": []}`.

## 8. Scaffold and codegen wiring

`scaffold.py` stays pure (every function returns text built from typed inputs). Today it pins one
invocation, `doc-lattice check`, into the generated pre-commit hook and CI workflow. This slice
generalizes the invocation so both gates run `check` and `lint`:

- The pre-commit output gains a second `repo: local` hook, `doc-lattice-lint`, mirroring the existing
  `doc-lattice-check` hook (same `uvx --from git+...@<rev>` entry, same `files: \.md$`,
  `pass_filenames: false`), so a commit that inverts the ladder is blocked locally. pre-commit runs
  every hook and reports all failures (absent `fail_fast`), so the two hooks never mask each other.
- The CI workflow runs both commands in a single shell `- run:` step that captures each exit code,
  prints both outputs, and exits nonzero if either failed. A separate second step would be skipped by
  GitHub Actions whenever the first exits nonzero, and `check` exits 1 on routine drift (STALE,
  UNRECONCILED, or BROKEN), so an authority violation would otherwise be masked until the drift was
  fixed and CI re-ran. One aggregating step surfaces drift and ladder violations together on every
  run. lint is safe in a blocking gate because skips never fail it; only a real inversion does.
- Both invocations remain pinned to the same `rev` the command already passes (`f"v{__version__}"`),
  which after section 9 is `v0.3.0`.

`build_scaffold`, the `Scaffold` dataclass shape, and the `init` command's write and print flow are
otherwise unchanged. `tests/test_scaffold.py` is updated to assert both invocations appear in both
artifacts.

## 9. Release model: 0.3.0

The generated snippets pin `v{__version__}`, and `RELEASING.md` already requires the tag to contain
the commands the gates run. Because the gates now run `lint`, the pinned tag must contain `lint`, so
this slice ships as the 0.3.0 release in one atomic step, following the existing checklist:

- Bump the version to `0.3.0` in both locations (`src/doc_lattice/__init__.py` and `pyproject.toml`),
  run `uv lock`, and add a `## [0.3.0]` section to `CHANGELOG.md`.
- After merge, tag the merge commit `v0.3.0`, push it, and smoke-test the pinned ref. The smoke test
  runs both `doc-lattice check` and `doc-lattice lint` from `git+...@v0.3.0`, since both are now
  generated gates.
- Update the `RELEASING.md` invariant line so the tag must contain `check`, `init`, and `lint`.

A half-done release (codegen emitting `doc-lattice lint` but a tag without the command) would leave
adopters with a gate that errors on an unknown command. The atomic-release rule already guards exactly
this; the slice extends its statement to cover `lint`.

## 10. Error handling

No new error type. A load failure surfaces as the existing `ProjectError` subclasses (`ConfigError`,
`DuplicateIdError`, `UnreadableDocError`) and exits 2, exactly as in `check`. A ladder violation is not
an exception: it is a normal, reported lattice state that drives exit 1, the same modeling decision the
local core makes for a broken edge. A skip is likewise a normal state, never an error. No bare
`except`. No `datetime.now()`.

## 11. Conventions and invariants

- `lint.py` and the updated `scaffold.py` are pure and stay on the pure side of the pure-versus-impure
  split; only `cli.py` renders, writes, and sets the exit code. No `Any`, no `cast`: neither is a
  boundary module.
- Errors, constants, and path rules are inherited unchanged; `lint` introduces no new path input and
  no new disk or network access.
- ruff line length 100, a module docstring on `lint.py`, Google-style docstrings on public functions,
  and no em-dashes in any drafted content.

## 12. Testing

`tests/test_lint.py` mirrors `src/doc_lattice/lint.py`. The pure tests build synthetic `Lattice`
objects directly (no I/O), covering:

- Each violation case: `binding` from `derived`, `binding` from `exploratory`, `derived` from
  `exploratory`.
- Each passing case: equal authority, and deriving from stronger (the healthy direction).
- Each skip, reported with the right reason: `source-unannotated`, `target-unannotated`, and a broken
  edge that is skipped but absent from the skip list (owned by `check`).
- Section-target resolution: an edge whose `target_id` is a section anchor is judged by the owning
  file's authority, with a pass, a violation, and a skip (owning file unannotated) case. This guards
  the dominant binding-edge granularity against a `nodes_by_id` `KeyError`.
- Determinism: violations and skips come back in node-id then edge order with multiple edges.

A CLI test exercises `lint` end to end (exit 0, 1, and 2 paths, the human summary line, and the
`--json` shape including `skipped`). It uses a small dedicated doc set rather than the shared
`lattice_dir` fixture, which is load-bearing for the `check`, `reconcile`, and `cli` suites; adding an
authority-inverting edge to that fixture would perturb those expectations. `tests/test_scaffold.py`
asserts the generated pre-commit carries both hooks and the generated CI runs both commands in one
aggregating step, so a `check` failure cannot mask a `lint` failure. A `test_conventions.py` assertion fixes `AUTHORITY_LADDER` against `VALID_AUTHORITIES`. Coverage
stays at or above the 80 percent gate.

## 13. Non-goals and deferral map

| Deferred or declined item | Disposition |
|---|---|
| `layer`-ladder validation | Declined here; `layer` is a partition, not a clean strength ladder |
| `--strict` mode that fails on unannotated edges | Deferred; v1 reports skips but never fails on them, preserving the locked "authority is optional" decision |
| Per-rule severity, config toggle | Declined; the rule is always on and a violation always exits 1 |
| Display-prefix lint | Still deferred by the local-core map; lands in this command next |
| Cycle detection or other structural lints | Deferred; the `lint` command is built to host them later |

## 14. Acceptance

| Goal | Met when |
|---|---|
| The ladder is policed | An edge whose source is more authoritative than its target is reported and exits 1 |
| Healthy edges pass | Deriving from equal or stronger authority yields no violation and exits 0 |
| Section-target edges are policed | An edge pointing at a `file#anchor` is judged by the owning file's authority, never crashed on or silently dropped |
| Skips are observable | A clean run still reports how many edges were unranked and why, so a passing gate cannot be mistaken for an unjudged corpus |
| Optional authority stays optional | An edge with either endpoint unannotated, or a broken edge, is skipped, not failed |
| The gate is real for adopters | Generated pre-commit and CI run both `check` and `lint`, pinned to a `v0.3.0` tag that contains both |
| It composes as a CI gate | `lint` exits 1 on any violation and 2 on a load error; generated CI runs it with `check` in one aggregating step so neither masks the other |
| Single source of order | Adding an `Authority` value without placing it on `AUTHORITY_LADDER` fails the conventions test |
