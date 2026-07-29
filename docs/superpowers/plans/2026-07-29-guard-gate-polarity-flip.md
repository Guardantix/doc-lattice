# Guard Gate Polarity Flip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three round-4 Codex findings on PR #179 as spelling *classes*, not spellings, by flipping the gate's provenance rules from blacklist recognizers to deny-by-default grammars, and by tightening invariant relevance to leaf reads with layered derivation.

**Architecture:** Three rule changes in `scripts/check_guard_inventory.py`, all candidate-owned gate properties. None of them touches fingerprint inputs, so `SCHEMA_VERSION` stays 12 and `tests/fixtures/shell_guard_debt.json` must be byte-identical at the end. Production modules (`shell_taint.py`, `shell_scanner.py`) are NOT modified; if a rule cannot classify an existing spelling in them, extend the rule's benign grammar rather than rewriting the module. The witness registry (`tests/guard_witnesses.py`) changes only in `boundary_evidence` predicate expressions.

**Tech Stack:** Python 3.13, `ast` module, pytest. All commands via `uv`.

## Global Constraints

- Run everything as `env -u VIRTUAL_ENV uv run --group dev <cmd>` (the dev shell's `VIRTUAL_ENV` points at a 3.12 devenv and shadows `uv run`).
- Run pytest as `env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest` (the shell exports `FORCE_COLOR=3`, which breaks rich substring asserts).
- The pre-commit `ty` hook checks `src/`, `scripts/`, AND `tests/` — new test code must be `ty`-clean.
- No em dashes in any drafted content (docstrings, docs, commit messages).
- Ruff line length 100; every module has a module docstring; public functions use Google-style docstrings.
- No Claude attribution in commit messages, PR titles, or PR bodies.
- `SCHEMA_VERSION` must remain 12. `env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py --emit-debt` must leave `tests/fixtures/shell_guard_debt.json` unchanged (verify with `git diff --exit-code tests/fixtures/shell_guard_debt.json`).
- Reference reproductions live at `/tmp/claude-1000/-home-guardantix-workspace-repos-tooling-doc-lattice/5d9b0efb-4647-42aa-a570-6d8792924bc2/scratchpad/repro_round4.py` (all six checks CONFIRMED against the pre-plan tree). The three bypasses it demonstrates must become committed failing-first tests.

---

### Task 1: Magnitude classification becomes deny-by-default

**Files:**
- Modify: `scripts/check_guard_inventory.py` (`_is_magnitude_binding`, ~line 3031, and its docstring)
- Test: `tests/test_check_guard_inventory.py`

**Interfaces:**
- Consumes: `_is_magnitude_binding(expression: ast.expr) -> bool`, `STRUCTURAL_GUARD_LITERALS`.
- Produces: same signature, inverted default. Callers (`_module_constants`, `_local_numeric_names`, `_binding_magnitudes` call sites) are unchanged.

**Design.** Today `_is_magnitude_binding` returns True only for enumerated magnitude shapes (whole-constant expressions, IfExp arms, container elements, scaling BinOps), so a `BoolOp` spelling such as `(strict and 100) or 200` silently classifies as not-a-magnitude. Invert it: collect every non-structural numeric literal occurrence in the expression (int/float, not bool, absolute value not in `STRUCTURAL_GUARD_LITERALS`); the expression IS a magnitude binding unless every such literal occurrence sits in a **benign role**. Benign roles, each pinned by an existing spelling in the guarded modules (do not widen beyond these):

1. **Displacement:** literal operand of an `ast.BinOp` with `Add`/`Sub` whose other operand subtree is not a numeric constant expression (`index + 2` at `shell_taint.py:3786`, `start + 2` at `shell_taint.py:8515`). A literal displaced from another *literal* is still a magnitude.
2. **Subscript or slice position:** literal inside the `slice` field of an `ast.Subscript` (`range_parts[:2]` at `shell_taint.py:608`, `basename[:-4]` at `shell_scanner.py:6076`, `words[3:]` at `shell_scanner.py:1466`).
3. **Numeric base argument:** literal argument in call position where the callee name is `int` (`int(text[start:stop], 16)` at `shell_taint.py:3796`) or where the callee is one of the module's ANSI-escape readers taking a base/width (`_read_ansi_c_digits`, `_read_ansi_c_prefixed_escape` at `shell_scanner.py:5872-5880`). Implement as: literal directly an argument of a call whose callee final name is in a small frozenset `_LITERAL_ARGUMENT_CALLEES = frozenset({"int", "_read_ansi_c_digits", "_read_ansi_c_prefixed_escape", "range", "round", "min", "max"})` is NOT automatically benign — see below. Only `int` and the two readers are benign; `min`/`max`/`range` literals are magnitudes (`max(100, x)` floors a value at 100).
4. **Arity membership sets:** a `Set`/`Tuple` of literals used as the comparator of an `ast.Compare` with an `In`/`NotIn` op (`len(range_parts) in {2, 3}` at `shell_taint.py:597`).
5. **Modulo parity:** literal right operand of a `Mod` BinOp (`... % 2` at `shell_scanner.py:1180`).
6. **Escape-width table subscripted immediately:** a `Dict` of literals that is itself the value of an `ast.Subscript` (`{'x': 2, 'u': 4, 'U': 8}[escape]` at `shell_taint.py:3785`). A bare `Dict`/`Set`/`Tuple`/`List` of literals in value position stays a magnitude, as today.

Keep the existing scaling rule as an additional magnitude source: a literal in a `Mult`/`Div`/`FloorDiv`/`Pow`/`LShift` BinOp is a magnitude even when the other operand is dynamic (`cap = 512 * factor`).

Rewrite the docstring to state the inverted contract: the roles enumerate what is *benign*, an unrecognized literal role classifies as a magnitude, and a false positive is resolved by spelling the binding plainly or inventorying the bound name, never by extending this list without a pinned in-tree spelling.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_check_guard_inventory.py`, near `test_an_uninventoried_guard_threshold_is_rejected` (~line 330):

```python
def test_a_boolop_bound_threshold_is_rejected() -> None:
    source = (
        "def _guard(items, strict):\n"
        "    cap = (strict and 100) or 200\n"
        "    if len(items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.boolop", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("cap" in violation for violation in violations)


def test_an_unrecognized_literal_spelling_defaults_to_a_magnitude() -> None:
    source = (
        "def _guard(items, floor):\n"
        "    cap = max(100, floor)\n"
        "    if len(items) > cap:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.floored", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    assert any("cap" in violation for violation in violations)


@pytest.mark.parametrize(
    "binding",
    [
        "stop = index + 2",
        "head = parts[:2]",
        "value = int(text, 16)",
        "wide = length % 2",
        "step = {'x': 2, 'u': 4}[escape]",
    ],
)
def test_benign_literal_roles_are_not_magnitudes(binding: str) -> None:
    source = (
        "def _guard(items, index, parts, text, length, escape):\n"
        f"    {binding}\n"
        "    if len(items) > _MAX_DEMO_ITEMS:\n"
        '        raise _TaintLimitExceeded(GuardRefusal("taint.demo.benign", "nope"))\n'
    )

    violations = checker.find_threshold_violations(source, "shell_taint.py")

    flagged = binding.split(" ", 1)[0]
    assert not any(flagged in violation for violation in violations)
```

The `_MAX_DEMO_ITEMS` comparison keeps one deliberate violation in the benign cases so the guard is exercised; the assertion is only that the benign binding's *name* is not additionally reported.

- [ ] **Step 2: Run the new tests, confirm the first two FAIL and the benign five PASS**

Run: `env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_guard_inventory.py -k "boolop or unrecognized_literal or benign_literal" -v`
Expected: `test_a_boolop_bound_threshold_is_rejected` FAILS (no violations), `test_an_unrecognized_literal_spelling_defaults_to_a_magnitude` FAILS, all five benign cases PASS (they document current behavior that must survive the flip).

- [ ] **Step 3: Invert `_is_magnitude_binding`**

Replace the function body: walk the expression once collecting `(literal_node, parent_chain)` for every non-structural numeric literal; return True if the scaling rule fires OR any literal occurrence is not in a benign role; keep the IfExp/container recursion by treating those arms as value positions (a literal directly in an IfExp arm, BoolOp operand, or container element is in value position, hence a magnitude). Implementation guidance: build a parent map with `ast.iter_child_nodes` over the expression; classify each literal by inspecting its ancestors: benign iff its nearest role-deciding ancestor matches roles 1-6 above. Add `_LITERAL_BENIGN_CALLEES = frozenset({"int", "_read_ansi_c_digits", "_read_ansi_c_prefixed_escape"})` beside `STRUCTURAL_GUARD_LITERALS`.

- [ ] **Step 4: Run the focused tests, then the guard gate against the real tree**

Run: `env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_guard_inventory.py -v`
Expected: all pass, including the pre-existing threshold and module-constant tests.
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py`
Expected: exit 0, no new violations (the benign grammar covers every in-tree spelling; the probe at `/tmp/claude-1000/-home-guardantix-workspace-repos-tooling-doc-lattice/5d9b0efb-4647-42aa-a570-6d8792924bc2/scratchpad/probe_flip.py` section 1 lists the spellings that must stay clean).
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py --emit-debt && git diff --exit-code tests/fixtures/shell_guard_debt.json`
Expected: exit 0, snapshot unchanged.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_guard_inventory.py tests/test_check_guard_inventory.py
git commit -m "fix(ci): classify unrecognized threshold spellings as magnitudes"
```

---

### Task 2: Constructor references become deny-by-default

**Files:**
- Modify: `scripts/check_guard_inventory.py` (new helper `_unanalyzable_constructor_references`, wired into `find_limits_violations` ~line 2904 and `find_shape_violations` ~line 2790)
- Test: `tests/test_check_guard_inventory.py`

**Interfaces:**
- Consumes: `_constructor_names`, `_paired_bindings`, `_rebinding_targets`, `_defaulted_arguments`, `_referenced_name`, `LIMITS_CONSTRUCTORS`, `REFUSAL_CONSTRUCTOR`, `REFUSAL_EXCEPTIONS`, `RESULT_CONSTRUCTOR`.
- Produces: `_unanalyzable_constructor_references(tree: ast.Module, constructors: frozenset[str], path: str) -> tuple[str, ...]` returning one violation string per disallowed reference, formatted `f"{path}:{lineno}: {name} is referenced in a form the inventory cannot follow; bind it plainly, call it directly, or use a declared context"`.

**Design.** `_constructor_names` follows aliases only through recognized binding forms, so `factory = TaintLimits if use_default else injected` binds a constructor through a spelling no rule sees, and `factory()` then mints production limits invisibly. Instead of teaching the alias-follower each new spelling, reject every reference to a tracked constructor that is not in an allowed context. Allowed contexts, verified clean against the current tree (probe section 2 reported zero residuals):

1. Callee position: any node inside `call.func` of an `ast.Call`.
2. Recognized binding RHS: the value side of an `ast.Assign`/`ast.AnnAssign` (including destructuring) that `_paired_bindings` resolves, and a parameter default that `_referenced_name` resolves. These register the alias, so they are followed, not rejected.
3. `field(default_factory=<constructor>)` keyword value.
4. Exception contexts: `ast.ExceptHandler.type`, `ast.Raise.exc`, `ast.Raise.cause` subtrees.
5. `isinstance`/`issubclass` second argument subtree.
6. Annotation subtrees: any node under an `annotation` or `returns` field.
7. `ast.MatchClass.cls` subtree.

Any `ast.Name` whose `id` is a tracked constructor name, or `ast.Attribute` whose `attr` is one, outside these contexts is a violation. Wire it in: `find_limits_violations` extends its result with `_unanalyzable_constructor_references(tree, limit_names, path)` (note: the *transitively aliased* name set, so a reference to an already-registered alias in a weird position is also caught); `find_shape_violations` does the same for `refusals | exceptions | results` name sets it already computes at ~line 2680.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_conditional_constructor_alias_is_rejected() -> None:
    source = (
        "def _helper(expression, use_default, injected):\n"
        "    factory = TaintLimits if use_default else injected\n"
        "    return _evaluate(expression, factory())\n"
    )

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("TaintLimits" in violation for violation in violations)


def test_a_boolop_constructor_alias_is_rejected() -> None:
    source = (
        "def _helper(expression, injected):\n"
        "    factory = injected or TaintLimits\n"
        "    return _evaluate(expression, factory())\n"
    )

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("TaintLimits" in violation for violation in violations)


def test_a_container_held_constructor_is_rejected() -> None:
    source = "def _helper(expression):\n    factories = [TaintLimits]\n    return factories\n"

    violations = checker.find_limits_violations(source, "shell_taint.py")

    assert any("TaintLimits" in violation for violation in violations)


@pytest.mark.parametrize(
    "source",
    [
        "def _helper(x):\n    return isinstance(x, TaintLimits)\n",
        "def _helper(x):\n    if isinstance(x, TaintLimits):\n        raise ValueError(x)\n",
        "def _helper(x: TaintLimits) -> TaintLimits:\n    return x\n",
        "factory = TaintLimits\n",
    ],
)
def test_declared_constructor_contexts_are_accepted(source: str) -> None:
    assert not any(
        "cannot follow" in violation
        for violation in checker.find_limits_violations(source, "shell_taint.py")
    )


def test_an_unanalyzable_refusal_reference_is_rejected() -> None:
    source = (
        "def _helper(use_default, injected):\n"
        "    make = GuardRefusal if use_default else injected\n"
        '    return make("taint.demo.hidden", "nope")\n'
    )

    violations = checker.find_shape_violations(source, "shell_taint.py")

    assert any("GuardRefusal" in violation for violation in violations)
```

- [ ] **Step 2: Run them, confirm the rejection tests FAIL and the accepted-context tests PASS**

Run: `env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_guard_inventory.py -k "conditional_constructor or boolop_constructor or container_held or declared_constructor_contexts or unanalyzable_refusal" -v`
Expected: three FAIL (no violation reported today), accepted-context cases PASS.

- [ ] **Step 3: Implement `_unanalyzable_constructor_references` and wire it into both rules**

Single tree walk: first collect the allowed-context node id sets exactly as listed in the design (the probe script `/tmp/claude-1000/-home-guardantix-workspace-repos-tooling-doc-lattice/5d9b0efb-4647-42aa-a570-6d8792924bc2/scratchpad/probe_flip.py` section 2 is a working reference implementation of the context collection), then report every tracked-name reference outside them. Write a Google-style docstring stating the inversion: contexts enumerate what the inventory can follow, and an unlisted context is rejected so a new spelling fails loudly instead of hiding a constructor.

- [ ] **Step 4: Verify against the real tree and the debt snapshot**

Run: `env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_guard_inventory.py -v`
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py`
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py --emit-debt && git diff --exit-code tests/fixtures/shell_guard_debt.json`
Expected: all pass, gate exit 0, snapshot unchanged.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_guard_inventory.py tests/test_check_guard_inventory.py
git commit -m "fix(ci): reject constructor references the inventory cannot follow"
```

---

### Task 3: Invariant relevance uses layered leaf reads

**Files:**
- Modify: `scripts/check_guard_inventory.py` (`guard_condition_reads` ~line 3818, `repository_invariant_relevance_violations` ~line 3934, new helpers `_transport_argument_reads` and `_leaf_reads`)
- Modify: `tests/guard_witnesses.py` (seven `boundary_evidence` predicates, listed below)
- Test: `tests/test_check_guard_inventory.py`, `tests/test_github_ci_shell_guards.py`

**Interfaces:**
- Consumes: `_origin_calls_by_statement`, `_annotate`, `_guarded_bodies`, `_attribute_reads`, `_writer_statements`, `_reachability_inputs`, `DECLARED_TRANSPORTS`, `invariant_predicate_reads`.
- Produces: `guard_condition_reads(source, path) -> dict[str, tuple[frozenset[str], ...]]` now returns ordered non-empty layers per origin (memoization keys unchanged). `repository_invariant_relevance_violations` intersects the predicate reads against the leaf-filtered FIRST layer only.

**Design.** Two orthogonal tightenings:

*Layering.* Per origin, derive up to three layers and keep the first non-empty one as authoritative:
- Layer T (transported refusals only): the refusal construction is an argument of a call to a declared transport (`DECLARED_TRANSPORTS`, resolved as in `_declared_transport_parameters` in `tests/test_github_ci_shell_guards.py:362`). Layer T is the attribute reads of the tests governing the transport's `raise` of that parameter, unioned with the attribute reads of the caller-side writer closure of the transport call's non-refusal arguments (for `scope-parent-cycle`: the writers of `parent_graph`, giving `{parent_scope_id, scope_id, scopes}`). Use `_writer_statements(statement, scope, reads, values, cache)` with `reads`/`values` seeded from the call's non-refusal argument expressions instead of the governing tests.
- Layer C (condition proper): `_attribute_reads((statement, *governing, *body))`, the current derivation minus the writer closure.
- Layer W (closure): the current narrow set including writers.
- Fallback: the current controls-based set, only when T, C, and W are all empty, exactly as today.

*Leaf filtering.* Within the chosen layer's derivation statements, an attribute is an **intermediate** iff it is read as the iteration source of a `for` statement or comprehension generator in those statements AND the iteration variable itself has attribute reads inside the same derivation (in `for scope in evidence.scopes: ... scope.parent_scope_id`, `scopes` is intermediate, `parent_scope_id` is a leaf). The relevant set is the layer minus intermediates, falling back to the whole layer when the subtraction empties it. The predicate must intersect the relevant set.

Empirically verified consequences (probe at `/tmp/claude-1000/-home-guardantix-workspace-repos-tooling-doc-lattice/5d9b0efb-4647-42aa-a570-6d8792924bc2/scratchpad/probe_layers.py`):
- `taint.evidence.scope-parent-cycle` (hypothetical future row): relevant set `{parent_scope_id, scope_id}`; the Codex predicate `lambda e: bool(e.commands) and bool(e.scopes)` is rejected; a genuine parent-edge predicate reading `parent_scope_id` is accepted.
- Seven shipped rows pass unchanged: `unknown-parent-scope`, `unknown-parent-command`, `unknown-pipe-consumer-command`, `unknown-pipe-consumer-scope`, `unknown-argv-resource`, `unknown-output-node`, `unknown-output-input-node`.
- Seven shipped rows must strengthen their predicate to read the leaf attribute (Step 3 below). Each strengthened predicate must still reject the evidence an empty script builds (the existing executable assertions in `tests/test_github_ci_shell_guards.py` enforce this; do not weaken them).

`guard_condition_reads` changes its return type; update its two call sites (`repository_invariant_relevance_violations` and any test referencing it) rather than keeping a compatibility shim.

- [ ] **Step 1: Write the failing tests**

In `tests/test_check_guard_inventory.py`:

```python
def test_a_container_borrowing_predicate_is_rejected_for_a_transported_guard(
    tmp_path: Path,
) -> None:
    # Codex round-4 P1: bool(evidence.commands) and bool(evidence.scopes) rejects the empty
    # control and intersects the flat derived set through the container attribute alone, while
    # inspecting no parent edge. The leaf rule must reject it.
    root = _invariant_row_root(
        tmp_path,
        origin_id="taint.evidence.scope-parent-cycle",
        predicate="lambda evidence: bool(evidence.commands) and bool(evidence.scopes)",
    )

    violations = checker.repository_invariant_relevance_violations(root)

    assert any("scope-parent-cycle" in violation for violation in violations)


def test_a_leaf_reading_predicate_is_accepted_for_a_transported_guard(tmp_path: Path) -> None:
    root = _invariant_row_root(
        tmp_path,
        origin_id="taint.evidence.scope-parent-cycle",
        predicate=(
            "lambda evidence: any(scope.parent_scope_id is not None for scope in evidence.scopes)"
        ),
    )

    violations = checker.repository_invariant_relevance_violations(root)

    assert not any("scope-parent-cycle" in violation for violation in violations)
```

with a module-level helper beside the existing `_fake_root`:

```python
def _invariant_row_root(tmp_path: Path, *, origin_id: str, predicate: str) -> Path:
    """Copy the guarded modules and registry, adding one invariant row for this origin."""
    marker = "INVARIANT_WITNESSES: tuple[InvariantWitness, ...] = ("
    row = (
        f'{marker}\n    InvariantWitness(\n        "{origin_id}",\n'
        f'        "structural rationale for the relevance rule tests",\n'
        f'        "echo hi",\n        boundary_evidence={predicate},\n    ),'
    )
    for module in checker.GUARDED_MODULES:
        destination = tmp_path / module
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((_ROOT / module).read_text(encoding="utf-8"), encoding="utf-8")
    registry = _ROOT / checker.REGISTRY_PATH
    destination = tmp_path / checker.REGISTRY_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = registry.read_text(encoding="utf-8")
    destination.write_text(source.replace(marker, row), encoding="utf-8")
    return tmp_path
```

Note `invariant_predicate_reads` is memoized on source text, so distinct predicates produce distinct memo keys and no cache clearing is needed.

- [ ] **Step 2: Run them, confirm rejection test FAILS**

Run: `env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_guard_inventory.py -k "borrowing_predicate or leaf_reading_predicate" -v`
Expected: `test_a_container_borrowing_predicate_is_rejected_for_a_transported_guard` FAILS (accepted today), the leaf-reading acceptance test PASSES.

- [ ] **Step 3: Implement layers and leaf filtering, then strengthen the seven registry predicates**

Exact replacement predicates in `tests/guard_witnesses.py` (verify attribute names against the evidence dataclasses in `shell_taint.py` before finalizing; the leaf attribute each must read is fixed, the access path may need adjusting):

| origin id | new `boundary_evidence` |
| --- | --- |
| `taint.evidence.stream-scope-kind` | `lambda evidence: len({scope.kind for scope in evidence.scopes}) > 1` |
| `taint.evidence.pipe-consumer-arity` | `lambda evidence: any(pipe.consumer_command_id is not None or pipe.consumer_scope_id is not None for pipe in evidence.pipes)` |
| `taint.evidence.process-resource-direction` | `lambda evidence: bool({resource.direction for resource in evidence.process_resources})` |
| `taint.evidence.duplicate-identifier` | `lambda evidence: len({command.command_id for command in evidence.commands}) > 1` |
| `taint.evidence.unknown-pipe-producer` | `lambda evidence: bool({pipe.producer_scope_id for pipe in evidence.pipes})` |
| `taint.evidence.unknown-resource-scope` | `lambda evidence: bool({resource.scope_id for resource in evidence.process_resources})` |
| `taint.evidence.unknown-redirection-resource` | must read `resource_id` off the redirection target the guard checks; derive the exact path from the guard at the `unknown-redirection-resource` origin in `shell_taint.py` and keep the boundary script `cat <(echo a)` unchanged |

Update the comment above `unknown-output-node`'s predicate only if its wording now contradicts the layered rule; its predicate itself passes unchanged.

- [ ] **Step 4: Run the full relevance surface**

Run: `env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_guard_inventory.py tests/test_github_ci_shell_guards.py -v`
Expected: all pass, including the existing invariant-witness executable assertions over the strengthened predicates (each still rejects empty-control evidence) and the existing test pinning that the misdirected `unknown-output-node` predicate `bool(evidence.commands)` is rejected (~line 784; it must still be rejected under the leaf rule, now with the relevant set reported from the chosen layer).
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py`
Expected: exit 0 with the relevance property reporting the leaf-filtered sets.
Run: `env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py --emit-debt && git diff --exit-code tests/fixtures/shell_guard_debt.json`
Expected: snapshot unchanged (relevance derivations feed no fingerprint).

- [ ] **Step 5: Commit**

```bash
git add scripts/check_guard_inventory.py tests/guard_witnesses.py tests/test_check_guard_inventory.py tests/test_github_ci_shell_guards.py
git commit -m "fix(ci): hold boundary predicates to the leaf reads of the deciding layer"
```

---

### Task 4: Record the polarity decision and run the full handoff battery

**Files:**
- Modify: `ARCHITECTURE.md` (AD-20)
- Modify: `CLAUDE.md` (guard-gate paragraph, only the sentence describing the relevance rule)

**Interfaces:** none; documentation and verification only.

- [ ] **Step 1: Amend AD-20**

Add a subsection recording, in this order: (1) the rule polarity decision: provenance rules enumerate what the inventory can follow and reject the rest, so an unrecognized spelling is a loud false positive to spell plainly, never a silent bypass; extending a benign grammar requires a pinned in-tree spelling. (2) The relevance floor after the leaf rule: the predicate must read a leaf attribute of the first non-empty derivation layer; what remains out of mechanical reach is a predicate that is weak about the right leaf, and that residual is owned by human review of the invariant rationale, per AD-19. (3) The three round-4 findings as the motivating evidence, with the measured outcome: zero fingerprint churn, `SCHEMA_VERSION` unchanged at 12. No em dashes.

- [ ] **Step 2: Update the CLAUDE.md gate paragraph**

Replace the sentence "A boundary witness carries a predicate over the evidence its script builds, and that predicate must read something the guard's own condition reads." with wording that says the predicate must read a leaf attribute of the layer that decides the guard's refusal, and that the gate derives that set from the guarded module. Keep the paragraph's remaining sentences unchanged.

- [ ] **Step 3: Full verification battery**

```bash
env -u VIRTUAL_ENV env -u FORCE_COLOR uv run --group dev python -m pytest
env -u VIRTUAL_ENV uv run --group dev ruff check src tests scripts
env -u VIRTUAL_ENV uv run --group dev ruff format --check src tests scripts
env -u VIRTUAL_ENV uv run --group dev ty check src scripts tests
env -u VIRTUAL_ENV uv run --group dev python scripts/check_typing_boundaries.py src
env -u VIRTUAL_ENV uv run --group dev python scripts/check_version_sync.py
env -u VIRTUAL_ENV uv run --group dev python scripts/check_guard_inventory.py
git diff --exit-code tests/fixtures/shell_guard_debt.json
env -u VIRTUAL_ENV uv run python scripts/fuzz_shell_taint.py --self-check
env -u VIRTUAL_ENV uv run python scripts/fuzz_shell_taint.py --iterations 1200 --seed 1 --baseline tests/fixtures/shell_taint_fuzz_baseline.tsv
```

Expected: pytest fully green with coverage at or above the current 93.25 percent; everything else exit 0. Also time one `scripts/check_guard_inventory.py` run and compare against the 3.46s single-derivation figure from the PR body; the three rules added here are single-pass AST walks and must not move it materially.

- [ ] **Step 4: Base-owned comparison across the unchanged schema**

```bash
git show origin/main:scripts/check_guard_inventory.py > /tmp/claude-1000/-home-guardantix-workspace-repos-tooling-doc-lattice/5d9b0efb-4647-42aa-a570-6d8792924bc2/scratchpad/base_checker.py 2>/dev/null || git show 763f43d:scripts/check_guard_inventory.py > /tmp/claude-1000/-home-guardantix-workspace-repos-tooling-doc-lattice/5d9b0efb-4647-42aa-a570-6d8792924bc2/scratchpad/base_checker.py
```

If the base revision predates the checker (main does not carry it), instead run the previous branch commit's copy: `git show HEAD~1:scripts/check_guard_inventory.py`. Then run that copy with `--compare-base` semantics against the working tree the same way `.github/workflows` `guard-debt` job does (read the job definition and replicate its invocation). Expected: exit 0.

- [ ] **Step 5: Commit docs**

```bash
git add ARCHITECTURE.md CLAUDE.md
git commit -m "docs: record the guard gate rule polarity in AD-20"
```
