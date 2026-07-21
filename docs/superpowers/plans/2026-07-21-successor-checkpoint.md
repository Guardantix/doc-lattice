# Successor Evaluation Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the frozen predeclaration checkpoint for the mvdan/sh helper successor
evaluation (spec section 8), ending in one squashed checkpoint commit presented for the
owner's checkpoint review and numeric-tripwire ratification.

**Architecture:** Every artifact lands under
`tests/fixtures/github_ci_successor_checkpoint/` as pure data (JSON, YAML, Markdown),
validated by pytest validators written before each artifact and by a permanent manifest
integrity test. No production module changes, no runtime behavior changes, no release
changes. The final task squashes all checkpoint work into a single commit on top of the
spec commits.

**Tech Stack:** Python 3.13 via `uv`, pytest, Go toolchain (install only; no Go source in
this plan), JSON Schema draft 2020-12.

**Spec:** `docs/superpowers/specs/2026-07-21-mvdan-helper-evaluation-design.md`
(sections cited as S3.1, S8, etc.). Read the cited sections before each task.

## Global Constraints

- Release freeze holds: `pyproject.toml` and `src/doc_lattice/__init__.py` stay `2.0.0`;
  no CHANGELOG heading changes; no tags.
- The frozen D3 checkpoint `tests/fixtures/github_ci_checkpoint/` is never modified;
  the permanent integrity test asserts this.
- No production module under `src/doc_lattice/` is created or modified by this plan.
- No em dashes in any drafted content (repo rule). ASCII hyphens only.
- Run pytest as `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest`
  (dev shell exports `VIRTUAL_ENV` and `FORCE_COLOR=3`, both break the run otherwise).
- Every new test file mirrors the repo pattern: module docstring first line, Google-style
  docstrings on public functions, Ruff 100-char lines.
- Checkpoint files are inputs only (S8): no gate results, no measurements, no evidence.
- Baseline for all derivations: commit `be4b7b1` (the branch point; `src/` here is
  byte-identical to it, which Task 9 verifies).
- Do not push. The branch stays local until the evaluation PR opens in a later plan.
- Checkpoint directory layout created by these tasks:
  `pins/`, `tables/`, `protocol/`, `corpus/`, `tiers/`, plus top-level `limits.json`,
  `budgets.json`, `tripwires.json`, `legacy_normalization.json`, `README.md`,
  `MANIFEST.sha256`.

---

### Task 1: Go toolchain pin and install

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/pins/go_toolchain.json`
- Create: `tests/test_successor_checkpoint.py` (validator start; grows in later tasks)
- Modify: `~/.claude/LCARS/refs/tool-inventory-linux-fleetyard.md` (outside repo; no
  commit here)

**Interfaces:**
- Produces: `pins/go_toolchain.json` with keys `version` (string, e.g. `"go1.26.5"`),
  `source` (`"https://go.dev/dl/"`), and `builders` (object mapping the five builder
  platform keys `linux-amd64`, `linux-arm64`, `darwin-amd64`, `darwin-arm64`,
  `windows-amd64` to `{filename, sha256}`).
- Produces: helper `checkpoint_path()` in `tests/test_successor_checkpoint.py` used by
  every later validator.

- [ ] **Step 1: Write the failing validator**

Create `tests/test_successor_checkpoint.py`:

```python
"""Validators for the successor evaluation checkpoint artifacts (spec S8)."""

import json
from pathlib import Path

CHECKPOINT = Path(__file__).parent / "fixtures" / "github_ci_successor_checkpoint"

_BUILDERS = frozenset(
    {"linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64"}
)


def _load(relative: str) -> dict:
    """Load one checkpoint JSON artifact by checkpoint-relative path."""
    return json.loads((CHECKPOINT / relative).read_text(encoding="utf-8"))


def test_go_toolchain_pin_shape():
    """The Go pin names one exact version and hashes all five builder archives."""
    pin = _load("pins/go_toolchain.json")
    assert set(pin) == {"version", "source", "builders"}
    assert pin["version"].startswith("go1.")
    assert set(pin["builders"]) == _BUILDERS
    for entry in pin["builders"].values():
        assert set(entry) == {"filename", "sha256"}
        assert len(entry["sha256"]) == 64
```

- [ ] **Step 2: Run it to verify it fails**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: FAIL (FileNotFoundError for `pins/go_toolchain.json`).

- [ ] **Step 3: Fetch the release manifest and write the pin**

```bash
mkdir -p tests/fixtures/github_ci_successor_checkpoint/pins
curl -fsSL 'https://go.dev/dl/?mode=json' -o /tmp/claude-1000/go-dl.json
```

Then generate the pin with this script (run with `python3`); it selects the newest
stable release and the five builder archives:

```python
import json

MAP = {
    ("linux", "amd64"): "linux-amd64",
    ("linux", "arm64"): "linux-arm64",
    ("darwin", "amd64"): "darwin-amd64",
    ("darwin", "arm64"): "darwin-arm64",
    ("windows", "amd64"): "windows-amd64",
}
releases = json.load(open("/tmp/claude-1000/go-dl.json"))
latest = next(r for r in releases if r["stable"])
builders = {}
for f in latest["files"]:
    key = MAP.get((f["os"], f["arch"]))
    if key and f["kind"] == "archive":
        builders[key] = {"filename": f["filename"], "sha256": f["sha256"]}
pin = {"version": latest["version"], "source": "https://go.dev/dl/", "builders": builders}
out = "tests/fixtures/github_ci_successor_checkpoint/pins/go_toolchain.json"
json.dump(pin, open(out, "w"), indent=2, sort_keys=True)
open(out, "a").write("\n")
```

- [ ] **Step 4: Run the validator to verify it passes**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Install the pinned toolchain locally (owner-run sudo)**

Download and verify against the recorded sha256, then ask Rick to run the privileged
steps (suggest the `!` prefix so output lands in the session):

```bash
V=$(python3 -c "import json;print(json.load(open('tests/fixtures/github_ci_successor_checkpoint/pins/go_toolchain.json'))['version'])")
F="$V.linux-amd64.tar.gz"
curl -fsSL "https://go.dev/dl/$F" -o "/tmp/claude-1000/$F"
python3 - <<EOF
import hashlib, json
pin = json.load(open("tests/fixtures/github_ci_successor_checkpoint/pins/go_toolchain.json"))
want = pin["builders"]["linux-amd64"]["sha256"]
got = hashlib.sha256(open("/tmp/claude-1000/$F", "rb").read()).hexdigest()
assert got == want, f"sha256 mismatch: {got} != {want}"
print("sha256 ok")
EOF
# Owner runs:
#   ! sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/claude-1000/$F
#   ! echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.profile
/usr/local/go/bin/go version
```

Expected: `go version go1.26.x linux/amd64` matching the pin. Then update the LCARS
tool inventory (move Go from "NOT installed" to the installed table with the exact
version and install path) and flag `lcars-refs` for re-indexing.

- [ ] **Step 6: Commit**

```bash
git add tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/pins/go_toolchain.json
git commit -m "test: pin the Go toolchain for the successor checkpoint"
```

---

### Task 2: External pins (parser, bash, shfmt, CI actions, platform matrix)

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/pins/parser_pin.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/pins/bash_pin.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/pins/shfmt_pin.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/pins/ci_actions.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/pins/platform_matrix.json`
- Modify: `tests/test_successor_checkpoint.py` (append validators)

**Interfaces:**
- Consumes: `checkpoint_path` layout from Task 1; the installed Go toolchain.
- Produces: `parser_pin.json` keys `module` (`"mvdan.cc/sh/v3"`), `version`
  (`"v3.13.1"`), `sum` and `gomod_sum` (the two `go.sum` hash lines, `h1:` prefixed);
  `platform_matrix.json` keys `targets` (list of five objects with `triple`,
  `wheel_tag`, `runner_label`, `build_container` [digest string or null for
  macOS/Windows native builds]).

- [ ] **Step 1: Write the failing validators**

Append to `tests/test_successor_checkpoint.py`:

```python
def test_parser_pin_is_exact():
    """The parser pin fixes mvdan.cc/sh/v3 at v3.13.1 with module hashes (S3.1)."""
    pin = _load("pins/parser_pin.json")
    assert pin["module"] == "mvdan.cc/sh/v3"
    assert pin["version"] == "v3.13.1"
    assert pin["sum"].startswith("h1:")
    assert pin["gomod_sum"].startswith("h1:")


def test_bash_and_shfmt_pins_carry_hashes():
    """Differential oracle pins carry exact versions, digests, and command lines (S8)."""
    bash = _load("pins/bash_pin.json")
    assert bash["version"] == "5.2.21"
    assert bash["container_digest"].startswith("sha256:")
    assert len(bash["binary_sha256"]) == 64
    shfmt = _load("pins/shfmt_pin.json")
    assert shfmt["version"] == "3.13.1"
    assert len(shfmt["binary_sha256"]) == 64
    assert "--to-json" in " ".join(shfmt["command_line"])


def test_ci_action_pins_are_commit_shas():
    """Every pinned CI action is `owner/repo` at a full 40-hex commit SHA (S8)."""
    pins = _load("pins/ci_actions.json")
    required = {
        "actions/checkout",
        "actions/setup-go",
        "actions/setup-python",
        "actions/upload-artifact",
        "actions/download-artifact",
    }
    assert required <= set(pins)
    for sha in pins.values():
        assert len(sha) == 40
        int(sha, 16)


def test_platform_matrix_covers_five_targets():
    """The platform matrix freezes labels, triples, tags, and build containers (S7)."""
    matrix = _load("pins/platform_matrix.json")
    triples = {t["triple"] for t in matrix["targets"]}
    assert triples == {
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
        "x86_64-pc-windows-msvc",
    }
    for target in matrix["targets"]:
        assert set(target) == {"triple", "wheel_tag", "runner_label", "build_container"}
```

- [ ] **Step 2: Run to verify the four new tests fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 1 pass (Task 1), 4 failures (missing files).

- [ ] **Step 3: Generate the pins**

Parser hashes come from the Go module proxy via the installed toolchain (no repo Go
module exists yet, so use a scratch module):

```bash
cd "$(mktemp -d)" && /usr/local/go/bin/go mod init scratch >/dev/null \
  && /usr/local/go/bin/go mod download -json mvdan.cc/sh/v3@v3.13.1
```

Copy `Sum` into `sum` and `GoModSum` into `gomod_sum` in `pins/parser_pin.json`.

`pins/bash_pin.json`: copy `version`, `container_digest`, and the bash binary sha256
from the frozen D3 artifact `tests/fixtures/github_ci_checkpoint/bash_pin.json`
(carrying the D3 values forward is the predeclared choice; the container digest field
name there may differ, preserve the digest value exactly), and add
`"role": "semantic-differential-oracle"`.

`pins/shfmt_pin.json`: version `3.13.1`; download the upstream release binary
`shfmt_v3.13.1_linux_amd64` from `https://github.com/mvdan/sh/releases/tag/v3.13.1`,
record its sha256, and set
`"command_line": ["shfmt", "--to-json", "--filename", "stdin.bash"]` plus
`"role": "syntax-differential-oracle"`.

`pins/ci_actions.json`: resolve each action's current default-major release commit with
`gh api repos/actions/checkout/git/ref/tags/v5 --jq .object.sha` (and the analogous tag
per action; if the tag object is annotated, dereference once more via
`gh api repos/actions/checkout/git/tags/<sha> --jq .object.sha`). Record
`{action: full_sha}` plus a sibling `"_tags"` object mapping each action to the tag
name it was resolved from.

`pins/platform_matrix.json` content, exactly:

```json
{
  "targets": [
    {"triple": "x86_64-unknown-linux-gnu", "wheel_tag": "manylinux_2_28_x86_64",
     "runner_label": "ubuntu-24.04", "build_container": null},
    {"triple": "aarch64-unknown-linux-gnu", "wheel_tag": "manylinux_2_28_aarch64",
     "runner_label": "ubuntu-24.04-arm", "build_container": null},
    {"triple": "x86_64-apple-darwin", "wheel_tag": "macosx_13_0_x86_64",
     "runner_label": "macos-15-intel", "build_container": null},
    {"triple": "aarch64-apple-darwin", "wheel_tag": "macosx_14_0_arm64",
     "runner_label": "macos-15", "build_container": null},
    {"triple": "x86_64-pc-windows-msvc", "wheel_tag": "win_amd64",
     "runner_label": "windows-2025", "build_container": null}
  ]
}
```

Cross-check each `runner_label` against the currently documented GitHub-hosted runner
labels before committing; if a label is no longer offered, substitute the nearest
supported label and note the substitution in the task report. `build_container` stays
null for all five because the helper is a static Go binary and the wheel is built with
`uv build` plus a tag rewrite (no compiled Python extension, so no manylinux container
is required); this rationale goes in the checkpoint README (Task 10).

- [ ] **Step 4: Run the validators to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/pins/
git commit -m "test: freeze parser, oracle, action, and platform pins for the successor checkpoint"
```

---

### Task 3: Contract tables

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/tables/certified_constructs.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tables/reason_codes.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tables/dispatcher_grammar.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tables/pre_policy_matrix.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tables/precedence.json`
- Modify: `tests/test_successor_checkpoint.py` (append validators)

**Interfaces:**
- Consumes: the installed Go toolchain and the downloaded `mvdan.cc/sh/v3@v3.13.1`
  module source (from Task 2's `go mod download`).
- Produces: `reason_codes.json` entries shaped
  `{code, scope, stable_reason, scan_reason_category, d4_mapping}` where `scope` is one
  of `terminal`, `subtree-local`, `command-local` (S3.3); `certified_constructs.json`
  entries shaped `{node, role, disposition}` with `disposition` in
  `traverse`, `ignore`, `refuse` (S3.2). Later plans (helper implementation) generate Go
  code and Python constants from these files, so key names here are contract.

- [ ] **Step 1: Write the failing validators**

Append to `tests/test_successor_checkpoint.py`:

```python
_SCOPES = frozenset({"terminal", "subtree-local", "command-local"})
_DISPOSITIONS = frozenset({"traverse", "ignore", "refuse"})


def test_certified_constructs_table_is_exhaustive():
    """Every exported syntax node type appears, and dispositions are valid (S3.2)."""
    table = _load("tables/certified_constructs.json")
    rows = table["rows"]
    assert table["parser"] == "mvdan.cc/sh/v3@v3.13.1"
    assert {r["disposition"] for r in rows} <= _DISPOSITIONS
    assert len({(r["node"], r["role"]) for r in rows}) == len(rows)
    covered_nodes = {r["node"] for r in rows}
    assert set(table["exported_node_types"]) <= covered_nodes
    required = {"CallExpr", "CmdSubst", "Subshell", "FuncDecl", "BinaryCmd", "Redirect"}
    assert required <= covered_nodes


def test_reason_codes_cover_spec_minimum_and_scopes():
    """The frozen reason-code table carries scope, stable reason, and D4 mapping (S3.3)."""
    rows = _load("tables/reason_codes.json")["rows"]
    codes = {r["code"] for r in rows}
    assert {
        "syntax-error",
        "unsupported-construct",
        "parser-divergence-guard",
        "dispatcher-payload",
        "marker-head-look-alike",
        "assignment-prefix",
        "unstable-first-word",
        "splitting-unsafe-word",
    } <= codes
    for row in rows:
        assert set(row) == {"code", "scope", "stable_reason", "scan_reason_category", "d4_mapping"}
        assert row["scope"] in _SCOPES
    terminal = {r["code"] for r in rows if r["scope"] == "terminal"}
    assert {"syntax-error", "parser-divergence-guard", "unsupported-construct"} <= terminal


def test_dispatcher_grammar_and_precedence():
    """Dispatcher heads and the policy precedence chain match the spec (S6.1, S6.3)."""
    grammar = _load("tables/dispatcher_grammar.json")
    assert set(grammar["plain_heads"]) == {"eval", "source", "."}
    assert set(grammar["shell_heads"]) == {"bash", "sh", "dash", "zsh"}
    assert grammar["shell_requires_c_option"] is True
    assert grammar["argv_wide_marker_rule"] is True
    chain = _load("tables/precedence.json")["chain"]
    assert chain == [
        "doc-lattice-or-launcher-resolution",
        "dispatcher-payload",
        "marker-head-look-alike",
        "off-floor-wrapper",
        "not-candidate",
    ]


def test_pre_policy_matrix_rows():
    """The pre-policy command matrix freezes the five S5.2 rows verbatim."""
    rows = _load("tables/pre_policy_matrix.json")["rows"]
    by_case = {r["case"]: r for r in rows}
    assert by_case["assignments-only"]["outcome"] == "no-command-no-refusal"
    assert by_case["assignments-plus-argv"]["outcome"] == "assignment-prefix-refusal-retain-argv"
    assert by_case["first-word-unknown"]["outcome"] == "unstable-first-word"
    assert by_case["multi-cardinality-word"]["outcome"] == "splitting-unsafe-word"
    assert by_case["ir-invariant"]["outcome"] == "text-implies-single"
```

- [ ] **Step 2: Run to verify the four new tests fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 5 passed (earlier tasks), 4 failed.

- [ ] **Step 3: Generate the node-type inventory, then author the tables**

List every exported node type of the pinned parser (the module cache path comes from
Task 2's `go mod download -json` output field `Dir`):

```bash
/usr/local/go/bin/go doc mvdan.cc/sh/v3/syntax 2>/dev/null | grep -E '^type [A-Z]' | awk '{print $2}' | sort
```

Author `tables/certified_constructs.json` as
`{"parser": "mvdan.cc/sh/v3@v3.13.1", "exported_node_types": [...], "rows": [...]}`,
classifying every `(node, role)` pair by these spec rules (S3.2):

- `traverse`: statement-bearing contexts (`Stmt`, `CallExpr` argv, `BinaryCmd`,
  `Pipeline` via `Stmt`, `Subshell`, `Block`, `CmdSubst`, `FuncDecl` body,
  `Redirect` target word expansions, unquoted-delimiter `HereDoc` bodies, `IfClause`,
  `WhileClause`, `ForClause`, `CaseClause` bodies and their condition/selector words).
- `ignore`: provably inert leaves (`Comment`, quoted-delimiter heredoc bodies,
  `SglQuoted` content as data).
- `refuse`: everything else, one row per remaining exported node type with the role
  `"*"` (wildcard role for a node refused in all contexts), including at minimum
  `ArithmCmd`, `ArithmExp` operands that are not statically resolvable, `ProcSubst`,
  `ExtGlob`, `CoprocClause`, `LetClause`, `DeclClause`, `TestClause`, `TimeClause`.

Every row carries a `spec` field citing the S3.2 disposition bullet it derives from.
Where a node type could be certified but the corpus never needs it, prefer `refuse`
(fail-closed default) and note it; the evaluation gates will surface over-refusal.

Author `tables/reason_codes.json` with one row per code. Fixed decisions: the eight
spec-minimum codes from the validator; `source-cap`, `statement-cap`, `depth-cap`,
`work-cap`, `event-cap` (all `terminal`); `redirect-unsupported` and
`expansion-unsupported` (`subtree-local`, satisfying the S3.3 resynchronization
conditions); command-local codes `dispatcher-payload`, `marker-head-look-alike`,
`assignment-prefix`, `unstable-first-word`, `splitting-unsafe-word`, plus the existing
launcher-policy refusal categories imported by reference (list each with its current
`ScanReasonCategory` value from `src/doc_lattice/constants.py`). `d4_mapping` is the
`BlockScan` status the code aggregates to (always `uninspectable`).

Author `tables/dispatcher_grammar.json`:

```json
{
  "plain_heads": ["eval", "source", "."],
  "shell_heads": ["bash", "sh", "dash", "zsh"],
  "shell_requires_c_option": true,
  "c_option_grammar": {
    "short_flag": "-c",
    "cluster_rule": "a short option cluster containing c (for example -lc, -xec) matches",
    "value_options_before_c": "any option-like word may precede; a word that is option-like but not statically resolvable refuses as dispatcher-selector-unresolved",
    "double_dash": "-- ends option parsing; -c after -- does not match",
    "exe_and_case": "head matched on casefolded basename with optional .exe suffix"
  },
  "argv_wide_marker_rule": true,
  "pinned_false_positive": "bash -c 'echo ok' doc-lattice refuses although the operand is $0"
}
```

Note: `dispatcher-selector-unresolved` must then also appear in
`tables/reason_codes.json` (command-local). Author `tables/precedence.json` and
`tables/pre_policy_matrix.json` exactly matching the Step 1 validators, with each row
carrying a `spec` citation (S6.3 chain, S5.2 matrix).

- [ ] **Step 4: Run the validators to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/tables/
git commit -m "test: freeze the successor contract tables"
```

---

### Task 4: Protocol schema, conformance fixtures, negatives, digest manifest

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/protocol/schema.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/protocol/encoder.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/protocol/digest_manifest.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/protocol/conformance/` (6 files)
- Create: `tests/fixtures/github_ci_successor_checkpoint/protocol/negative/` (12 files)
- Modify: `tests/test_successor_checkpoint.py` (append validators)

**Interfaces:**
- Produces: the wire contract consumed by the future Go helper and
  `helper_protocol_boundary.py`. Field names are frozen here: request
  `{protocol_version, sources: [{id, source}]}`; response
  `{protocol_version, helper_version, parser_version, results: [{id, events,
  work_units}]}`; event `{kind: "command_site", ordinal, start_byte, end_byte,
  assignments: [{name, value_known}], argv: [{text, single, start_byte, end_byte}]}` or
  `{kind: "refusal", code, start_byte, end_byte}` (S3.3, S4.1). `text` is string or
  null; `single` is boolean.

- [ ] **Step 1: Write the failing validators**

Append to `tests/test_successor_checkpoint.py`:

```python
def test_protocol_schema_is_strict():
    """The schema pins protocol_version 1 and closes every object (S4.1, S4.2)."""
    schema = _load("protocol/schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    request = schema["$defs"]["request"]
    response = schema["$defs"]["response"]
    for obj in (request, response):
        assert obj["additionalProperties"] is False
    assert request["properties"]["protocol_version"]["const"] == 1
    event = schema["$defs"]["event"]
    assert set(event["oneOf"][0]["properties"]["kind"]["enum"] + event["oneOf"][1]["properties"]["kind"]["enum"]) == {"command_site", "refusal"}


def test_conformance_and_negative_fixture_sets():
    """Positive fixtures validate; negative fixtures enumerate the S4.2 rejections."""
    conformance = sorted((CHECKPOINT / "protocol" / "conformance").iterdir())
    negative = sorted((CHECKPOINT / "protocol" / "negative").iterdir())
    assert len(conformance) == 6
    assert len(negative) == 12
    names = {p.stem for p in negative}
    assert {
        "duplicate-keys",
        "invalid-utf8",
        "lone-surrogate",
        "trailing-document",
        "wrong-type-bool-as-int",
        "non-contiguous-ids",
        "empty-batch",
        "nan-number",
        "unknown-field",
        "out-of-order-results",
        "span-out-of-range",
        "max-length-four-byte-source",
    } <= names


def test_encoder_rules_and_digest_manifest():
    """Canonical encoder rules and the digest-input manifest are frozen (S4.2, S4.3)."""
    encoder = _load("protocol/encoder.json")
    assert encoder["ensure_ascii"] is False
    assert encoder["allow_nan"] is False
    assert encoder["bom"] is False
    assert encoder["separators"] == [",", ":"]
    assert encoder["caps_measured_in"] == "utf-8-bytes"
    manifest = _load("protocol/digest_manifest.json")
    assert manifest["ordering"] == "path-lexicographic"
    included = manifest["include"]
    assert "helper/doc-lattice-shell-parser/main.go" in included
    assert "helper/doc-lattice-shell-parser/internal/certify/" in included
    assert "tests/fixtures/github_ci_successor_checkpoint/protocol/schema.json" in included
    assert "tests/fixtures/github_ci_successor_checkpoint/tables/" in included
    assert "tests/fixtures/github_ci_successor_checkpoint/limits.json" in included
    assert {"exclude_globs", "include"} <= set(manifest)
```

- [ ] **Step 2: Run to verify the three new tests fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 9 passed, 3 failed.

- [ ] **Step 3: Author the schema, encoder rules, fixtures, and manifest**

`protocol/schema.json`: JSON Schema draft 2020-12 with `$defs` for `request`,
`response`, `result`, `event` (a `oneOf` over the two kinds), `word`, and `assignment`.
Every object sets `additionalProperties: false` and lists all fields `required`.
Constraints: `protocol_version` const 1; ids `minimum: 0`; `start_byte`/`end_byte`
integers `minimum: 0`; `text` typed `["string", "null"]`; `work_units` integer
`minimum: 0`; `sources` `minItems: 1`.

`protocol/encoder.json`: exactly the fields asserted in Step 1 plus
`"surrogate_check": "reject-before-encoding"`.

`protocol/conformance/`: six request/response pairs as
`{"request": ..., "response": ...}` documents: `single-certified.json` (one source
`doc-lattice check`, one command_site), `single-refusal.json` (source `doc-lattice
check; echo "$("`, one site then one terminal `syntax-error` refusal, matching the
S3.1 canonical fixture), `batch-of-three.json` (mixed outcomes, ids 0..2),
`assignment-prefix.json` (`X=1 doc-lattice lint`), `dynamic-word.json`
(`doc-lattice "$CMD"` with `text: null, single: true`), `empty-events.json` (source
`true`, zero events, work_units still positive).

`protocol/negative/`: twelve single-document files named as in the validator. Encode
`invalid-utf8` and `lone-surrogate` as `.bin` written by a small Python script with
explicit byte escapes (`b'...\xff...'`); the JSON-decodable negatives are `.json`.
`max-length-four-byte-source` is a request whose single source is exactly 1,048,576
U+1F600 characters (4,194,304 UTF-8 bytes): generate it, do not hand-write it, and
assert in the generating script that the encoded request stays under the 8,388,608-byte
aggregate cap, pinning the S4.2 cap composition.

`protocol/digest_manifest.json`:

```json
{
  "ordering": "path-lexicographic",
  "include": [
    "helper/doc-lattice-shell-parser/main.go",
    "helper/doc-lattice-shell-parser/internal/certify/",
    "helper/doc-lattice-shell-parser/go.mod",
    "helper/doc-lattice-shell-parser/go.sum",
    "tests/fixtures/github_ci_successor_checkpoint/protocol/schema.json",
    "tests/fixtures/github_ci_successor_checkpoint/protocol/encoder.json",
    "tests/fixtures/github_ci_successor_checkpoint/tables/",
    "tests/fixtures/github_ci_successor_checkpoint/limits.json"
  ],
  "exclude_globs": ["**/*_test.go", "**/testdata/**"],
  "digest": "sha256 over newline-joined (path, file-sha256) pairs in path-lexicographic order"
}
```

- [ ] **Step 4: Run the validators to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/protocol/
git commit -m "test: freeze the successor wire protocol contract"
```

---

### Task 5: limits.json, budgets.json, tripwires.json

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/limits.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/budgets.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tripwires.json`
- Modify: `tests/test_successor_checkpoint.py` (append validators)

**Interfaces:**
- Produces: every numeric bound the helper, supervisor, and gates enforce. Later plans
  read these values; they are never restated in code comments or docs.

- [ ] **Step 1: Write the failing validators**

Append to `tests/test_successor_checkpoint.py`:

```python
def test_limits_freeze_spec_numbers():
    """All S3.5 and S4.4 numbers appear exactly once, in limits.json."""
    limits = _load("limits.json")
    assert limits["python_source_cap_chars"] == 1_048_576
    assert limits["helper_source_cap_bytes"] == 4_194_304
    assert limits["aggregate_request_cap_bytes"] == 8_388_608
    assert limits["stdout_cap_bytes"] == 16_777_216
    assert limits["stderr_capture_cap_bytes"] == 65_536
    deadline = limits["deadline_ms"]
    assert deadline == {"base": 2000, "per_source": 25, "per_4096_bytes": 1, "ceiling": 30000}
    for key in ("statement_cap", "visitor_node_cap", "visitor_depth_cap", "event_cap"):
        assert isinstance(limits[key], int) and limits[key] > 0
    assert limits["work_units"]["definition"]
    assert limits["peak_rss_max_bytes"] == 256 * 1024 * 1024
    assert limits["e2e_median_ceiling_ms"] == 750
    assert limits["e2e_repetitions_per_python"] == 50


def test_budgets_and_tripwires():
    """Tier budgets and retention tripwires match S9 and record ratification state."""
    budgets = _load("budgets.json")
    tier3b = budgets["tier3b"]
    assert tier3b == {
        "fixtures": 20,
        "max_total_indeterminate": 2,
        "max_newly_indeterminate": 2,
        "false_positive": 0,
        "false_safe": 0,
    }
    assert budgets["false_safe_anywhere"] == 0
    trip = _load("tripwires.json")
    assert trip["owned_production_surface_max_lines"] == 2200
    assert trip["net_production_reduction_min_lines"] == 1400
    assert trip["deletion_baseline_lines"] == 3704
    assert trip["helper_binary_max_bytes"] == 12 * 1024 * 1024
    assert trip["platform_wheel_max_bytes"] == 16 * 1024 * 1024
    assert trip["ci_native_target_executions_max"] == 5
    assert trip["artifact_retention_days"] == 7
    assert trip["ratified"] is False
```

- [ ] **Step 2: Run to verify the two new tests fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 12 passed, 2 failed.

- [ ] **Step 3: Author the three files**

`limits.json`: every asserted key (the S9 performance and RSS ceilings live here per
S8, and `tripwires.json` references them by key name rather than restating numbers),
plus `statement_cap`, `visitor_node_cap`,
`visitor_depth_cap`, `event_cap` set to 4096, 100000, 200, 10000 respectively (the
statement cap carries the D3 checkpoint's value forward if
`tests/fixtures/github_ci_checkpoint/limits.json` differs from 4096, in which case use
the D3 value and note it), and:

```json
"work_units": {
  "definition": "helper: one unit per visited node plus one per emitted event; python policy: one unit per ScanWord examined by the precheck and resolver",
  "helper_field": "work_units",
  "block_scan_field": "work_charged"
}
```

`budgets.json`: the asserted `tier3b` object, `false_safe_anywhere: 0`, and
`tier_predicates` restating S9 gates 1 to 8 as one-line strings keyed `gate1` ...
`gate8` (copy the S9 wording, condensed to a sentence each).

`tripwires.json`: the asserted numeric keys, plus
`"owned_surface_definition"` and `"net_reduction_definition"` copied verbatim from S9
gate 14, `"frozen_path_set"` listing the S9 accounting paths
(`src/doc_lattice/github_ci/`, `helper/doc-lattice-shell-parser/`,
`src/doc_lattice/constants.py`, `pyproject.toml`), `"separate_reporting"` listing
tests, fixtures, generated data, `go.mod`, `go.sum`, CI surface, and
`"ratified": false` with `"ratification": "owner ratifies at checkpoint review; flip to true only in the review-approved amendment commit"`.

- [ ] **Step 4: Run the validators to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/limits.json tests/fixtures/github_ci_successor_checkpoint/budgets.json tests/fixtures/github_ci_successor_checkpoint/tripwires.json
git commit -m "test: freeze successor limits, budgets, and retention tripwires"
```

---

### Task 6: Successor relabel of the 87-row acceptance corpus

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/corpus/acceptance_labels.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/corpus/relabel_report.md`
- Create: `scripts/derive_successor_labels.py`
- Modify: `tests/test_successor_checkpoint.py` (append validators)

**Interfaces:**
- Consumes: `ACCEPTANCE_CASES` (87 rows, `tests/test_github_ci_shell_scanner.py:30`),
  the D3 labels `tests/fixtures/github_ci_checkpoint/acceptance_labels.json` (78
  entries), and the Task 3 tables.
- Produces: `corpus/acceptance_labels.json` shaped
  `{"cases": [{description, label, expected_status, expected_invocations,
  reason_category, derivation, owner_adjudicate}]}` with `label` in `must-certify`,
  `intentional-exit-2`, `outside-direct-marker-contract`; row order identical to
  `ACCEPTANCE_CASES`; the first 78 descriptions matching the D3 artifact order.

- [ ] **Step 1: Write the failing validator**

Append to `tests/test_successor_checkpoint.py`:

```python
_LABELS = frozenset({"must-certify", "intentional-exit-2", "outside-direct-marker-contract"})


def test_successor_acceptance_labels_cover_all_rows():
    """Every acceptance row has a successor label, derivation, and consistent tuple."""
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # sibling test module,
    # imported the same way tests/test_github_ci_evaluation_gates.py imports the
    # sibling harness; if that file uses a different import form, mirror it exactly

    cases = _load("corpus/acceptance_labels.json")["cases"]
    assert len(cases) == len(ACCEPTANCE_CASES) == 87
    for row, case in zip(ACCEPTANCE_CASES, cases, strict=True):
        assert case["description"] == row[0]
        assert case["label"] in _LABELS
        assert case["derivation"]
        if case["label"] == "must-certify":
            assert case["expected_status"] == "certified"
        if case["label"] == "intentional-exit-2":
            assert case["expected_status"] == "uninspectable"
            assert case["reason_category"]
    adjudications = [c for c in cases if c.get("owner_adjudicate")]
    assert len(adjudications) <= 12, "too many unresolved judgment calls for review"
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py::test_successor_acceptance_labels_cover_all_rows -q --no-cov`
Expected: FAIL (missing file).

- [ ] **Step 3: Write the derivation script and generate the draft**

`scripts/derive_successor_labels.py` (module docstring, Google-style docstrings; this
is a maintained script, keep it Ruff-clean): for each of the 87 rows, start from the
D3 label when the row is in the 78-row prefix, else from the row's live-scanner
expectation, then apply the spec deltas in this order, recording each applied delta in
`derivation`:

1. D3-floor narrowness refusals that the successor traverses (pipelines, subshells,
   command substitutions, function bodies, redirects, control-flow compounds per the
   Task 3 `traverse` rows): flip `intentional-exit-2` to `must-certify` with the
   invocations the old scanner's recorded acceptance expectation carries
   (`ACCEPTANCE_CASES` row field), citing S3.2. Mark `owner_adjudicate: true` when the
   old scanner itself refused the row (no recorded invocations exist to carry).
2. Dispatcher rows (any row whose source matches the Task 3 dispatcher grammar with a
   marker in argv): label `intentional-exit-2`, `reason_category:
   "dispatcher-payload"`, citing S6.1. This includes rows previously labeled
   outside-contract for indirect dispatch.
3. Marker-bearing look-alike heads: `intentional-exit-2`,
   `reason_category: "marker-head-look-alike"`, citing S6.3.
4. Malformed-tail rows (a complete `doc-lattice` command precedes a later syntax
   error): keep `intentional-exit-2` but set `expected_invocations` to the retained
   pre-error invocations, citing S3.1 and S5.3.
5. Heredoc backslash-newline row (if present in the corpus): `intentional-exit-2` with
   `reason_category: "parser-divergence-guard"`, citing S3.4.
6. Everything else: carry the D3 or live expectation unchanged, `derivation:
   "carried"`.

The script prints a summary table (counts per label, per applied delta) and writes both
the JSON artifact and `corpus/relabel_report.md` listing every row whose label or tuple
changed versus its D3/live baseline, with the derivation sentence. Run it:

```bash
env -u VIRTUAL_ENV uv run --group dev python scripts/derive_successor_labels.py
```

Review the report manually; resolve `owner_adjudicate` rows that are mechanical after
all (documenting why) and leave at most 12 genuinely judgment-bearing rows flagged for
Rick's checkpoint review.

- [ ] **Step 4: Run the validator to verify it passes**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/derive_successor_labels.py tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/corpus/
git commit -m "test: relabel the acceptance corpus for the successor grammar"
```

---

### Task 7: New fixture families

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/corpus/new_fixtures.json`
- Modify: `tests/test_successor_checkpoint.py` (append validator)

**Interfaces:**
- Consumes: labels vocabulary and tuple shape from Task 6; reason codes from Task 3.
- Produces: `corpus/new_fixtures.json` shaped
  `{"families": {family_name: [{id, source, label, expected_status,
  expected_invocations, reason_category, spec}]}}` for families `dispatcher`,
  `look_alike`, `heredoc_guard`, `malformed_tail`, `offset_oracle`, `stmtsseq`,
  `encoder_composition`.

- [ ] **Step 1: Write the failing validator**

Append to `tests/test_successor_checkpoint.py`:

```python
_FAMILIES = frozenset(
    {
        "dispatcher",
        "look_alike",
        "heredoc_guard",
        "malformed_tail",
        "offset_oracle",
        "stmtsseq",
        "encoder_composition",
    }
)


def test_new_fixture_families_present_and_labeled():
    """All seven S8 fixture families exist with labeled, spec-cited members."""
    families = _load("corpus/new_fixtures.json")["families"]
    assert set(families) == _FAMILIES
    for name, rows in families.items():
        assert rows, name
        for row in rows:
            assert row["label"] in _LABELS
            assert row["spec"].startswith("S")
    heredoc = {r["id"]: r for r in families["heredoc_guard"]}
    regression = heredoc["benchmark-false-safe"]
    assert "$\\\n(doc-lattice linear)" in regression["source"]
    assert regression["expected_status"] in {"uninspectable", "certified"}
    assert regression["forbidden_outcome"] == "certified-empty"
    canonical = families["stmtsseq"][0]
    assert canonical["source"] == 'doc-lattice check; echo "$('
    assert canonical["expected_invocations"] == [["check", False]]
    assert canonical["pin_upgrade_tripwire"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py::test_new_fixture_families_present_and_labeled -q --no-cov`
Expected: FAIL (missing file).

- [ ] **Step 3: Author the families**

Minimum members per family (add more where a table row would otherwise be untested;
every row cites its spec section):

- `dispatcher` (S6.1): `bash -c 'doc-lattice linear'` (exit 2, dispatcher-payload);
  `eval "doc-lattice $X"` (exit 2); `sh -lc 'doc-lattice reconcile'` (exit 2, cluster
  rule); `bash -c 'echo ok' doc-lattice` (exit 2, the pinned argv-wide false positive,
  `"pinned_false_positive": true`); `bash --norc -c 'doc-lattice check'` (exit 2);
  `bash $OPT 'doc-lattice lint'` (exit 2, dispatcher-selector-unresolved);
  `bash -c 'echo hello'` inside a marker-bearing source (outside-direct-marker-contract
  for the dispatcher command itself: marker-free argv, S6.1 last sentence);
  `source ./doc-lattice-env.sh` (exit 2, plain head with marker argv).
- `look_alike` (S6.3): `doc_lattice check` (exit 2, marker-head-look-alike);
  `./doc.lattice-wrapper run` (exit 2); `doc-lattice.exe check` (must-certify,
  invocations `[["check", false]]`); `DOC-LATTICE lint` (exit 2, casefolded look-alike:
  resolves as head per the casefold rule, so this row is `must-certify` if
  `launcher_policy` casefolds to the doc-lattice head; derive from
  `_DOC_LATTICE_HEADS` and record the resolution in `spec`).
- `heredoc_guard` (S3.4): the benchmark false-safe verbatim
  (`cat <<EOF\n$\\\n(doc-lattice linear)\nEOF\n`, id `benchmark-false-safe`); the same
  construction with a quoted delimiter (`<<'EOF'`, must-certify as inert data with no
  invocations, i.e. outside-direct-marker-contract if no other marker exists; label per
  D2 on the raw text, which does contain the marker, so: intentional-exit-2 is wrong
  here; the correct tuple is certified with zero invocations; record the reasoning in
  `spec`); a plain unquoted heredoc containing `$(doc-lattice check)` without
  continuation (must-certify with `[["check", false]]`, S3.2 traverse rule).
- `malformed_tail` (S3.1, S5.3): `doc-lattice check; echo "$(`  duplicated from
  stmtsseq family with family-specific id; `doc-lattice linear && (` (retained
  `[["linear", false]]`, then terminal syntax-error); `doc-lattice reconcile --dry-run; do`
  (reserved word misuse after a complete command).
- `offset_oracle` (S5.2, S9 gate 8): sources with expected raw-text character indices
  for the first refusal: emoji before the marker (`echo "😀"; $X doc-lattice check`),
  a multibyte assignment value, a template case (`shell` value
  `bash -e {0} $EXTRA` with the refusal expected at the authored `$EXTRA` index), and
  a template case whose refusal falls inside `{0}` (expected index = the `{0}` start).
  Rows carry `expected_refusal_raw_index` instead of `expected_invocations`.
- `stmtsseq` (S3.1): the canonical fixture as asserted in Step 1.
- `encoder_composition` (S4.2): one row referencing
  `protocol/negative/max-length-four-byte-source` by path with
  `"assertion": "encoded request byte length < 8388608"`.

- [ ] **Step 4: Run the validator to verify it passes**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/corpus/new_fixtures.json
git commit -m "test: add the successor fixture families"
```

---

### Task 8: Tier expectations

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/tiers/tier1_expected.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tiers/tier2_expected.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tiers/tier3a_expected.json`
- Create: `tests/fixtures/github_ci_successor_checkpoint/tiers/tier3b_expected.json`
- Modify: `tests/test_successor_checkpoint.py` (append validators)

**Interfaces:**
- Consumes: D3 artifacts `tier3a_cases.json` and `tier3b/` (fixtures 01..20 plus
  provenance) from `tests/fixtures/github_ci_checkpoint/`; the rendered managed
  workflows via `doc_lattice.github_ci.render`; Task 6 label vocabulary.
- Produces: per-tier expected outcomes consumed by the future gate harness.

- [ ] **Step 1: Write the failing validators**

Append to `tests/test_successor_checkpoint.py`:

```python
def test_tier1_and_tier2_expectations_are_exact():
    """Tier 1 pins the exact managed findings; Tier 2 pins the repo workflow outcome."""
    tier1 = _load("tiers/tier1_expected.json")
    assert tier1["findings"] == [["ci", False], ["check", False], ["lint", False]]
    assert tier1["diagnostics"] == 0
    tier2 = _load("tiers/tier2_expected.json")
    assert tier2["findings"] == []
    assert tier2["diagnostics"] == 0
    assert tier2["workflows"], "tier 2 must enumerate the checked-in PR workflows"
    for workflow in tier2["workflows"]:
        assert {"path", "reachable_steps", "marker_gated_sources", "batched_sources"} <= set(workflow)


def test_tier3_expectations_rederived():
    """Tier 3A and 3B expectations exist for every D3 case with successor tuples."""
    d3_tier3a = json.loads(
        (CHECKPOINT.parent / "github_ci_checkpoint" / "tier3a_cases.json").read_text()
    )
    tier3a = _load("tiers/tier3a_expected.json")["cases"]
    assert len(tier3a) == len(d3_tier3a["cases"])
    tier3b = _load("tiers/tier3b_expected.json")["fixtures"]
    assert len(tier3b) == 20
    statuses = [f["expected_status"] for f in tier3b]
    assert statuses.count("uninspectable") <= 2, "predeclared expectation exceeds budget"
    for fixture in tier3b:
        assert fixture["id"].startswith("fixture-")
        assert fixture["derivation"]
```

- [ ] **Step 2: Run to verify the two new tests fail**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 16 passed, 2 failed.

- [ ] **Step 3: Derive the tier expectations**

Tier 1: render the managed workflows with the existing renderer (see how
`tests/github_ci_evaluation_harness.py` renders them) and record the exact expected
findings/diagnostics as asserted.

Tier 2: enumerate every checked-in workflow with a PR trigger (`.github/workflows/`),
and for each record its D1-reachable steps, which execution sources carry markers,
and which sources the successor batches. Derive from the current `ci audit` clean
state and the D1/D2/D6 rules; expected findings and diagnostics are both zero.

Tier 3A: map each D3 `tier3a_cases.json` case to its successor
(status, invocations, reason-category) tuple using the same delta rules as Task 6
(the derivation script from Task 6 is reusable: extend it with a `--tier3a` mode
rather than duplicating the rules).

Tier 3B: for each of the 20 envelope fixtures, derive the successor expectation with
the Task 6 delta rules. Fixtures 02, 05, and 14 (the D3 rejection trio) are compound
constructions the successor traverses: expected `certified` with their recorded
invocations. The `statuses.count` assertion enforces that the predeclared expectations
themselves respect the 2/20 budget; if honest derivation exceeds it, stop and surface
to Rick before committing (that would predict a gate failure at predeclaration time).

- [ ] **Step 4: Run the validators to verify they pass**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/tiers/
git commit -m "test: rederive tier expectations for the successor"
```

---

### Task 9: Legacy-reason normalization artifact

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/legacy_normalization.json`
- Create: `scripts/normalize_legacy_reasons.py`
- Modify: `tests/test_successor_checkpoint.py` (append validator)

**Interfaces:**
- Consumes: the 580-entry `tests/fixtures/github_ci_checkpoint/replay_inventory.json`
  (`entries[*].source`), the live scanner
  `doc_lattice.github_ci.shell_scanner.scan_doc_lattice_invocations`, and the Task 3
  reason-code table.
- Produces: `legacy_normalization.json` shaped `{"baseline_commit": "be4b7b1...",
  "mapping": {reason_substring: category}, "legacy_only_categories": [...],
  "entries": [{id, status, invocations, reason_category, owner_adjudicate}]}` in
  inventory order (S6.4).

- [ ] **Step 1: Write the failing validator**

Append to `tests/test_successor_checkpoint.py`:

```python
def test_legacy_normalization_covers_inventory():
    """Every replay entry has a normalized baseline tuple pinned at be4b7b1 (S6.4)."""
    artifact = _load("legacy_normalization.json")
    assert artifact["baseline_commit"].startswith("be4b7b1")
    inventory = json.loads(
        (CHECKPOINT.parent / "github_ci_checkpoint" / "replay_inventory.json").read_text()
    )
    assert len(artifact["entries"]) == inventory["count"] == 580
    assert artifact["mapping"], "the static reason mapping must be recorded, not inferred later"
    categories = {r["code"] for r in _load("tables/reason_codes.json")["rows"]}
    legacy = set(artifact["legacy_only_categories"])
    for entry in artifact["entries"]:
        assert entry["status"] in {"complete", "incomplete"}
        if entry["status"] == "incomplete":
            assert entry["reason_category"] in categories | legacy
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py::test_legacy_normalization_covers_inventory -q --no-cov`
Expected: FAIL (missing file).

- [ ] **Step 3: Write the normalization script and generate**

First verify the source tree matches the baseline:
`git diff --stat be4b7b1 -- src/` must be empty (docs-only commits sit on top);
abort with a clear message otherwise.

`scripts/normalize_legacy_reasons.py`: for each inventory entry, run the live scanner
on `entry["source"]`, record `status` (`complete` when `incomplete_reason` is None,
else `incomplete`), `invocations` (list of `[command, dry_run]` pairs), and, for
incomplete entries, map `incomplete_reason` to a category through an explicit static
`mapping` table written into the script (start from the distinct reason strings the
run produces: print them, group them, assign each group a category from the Task 3
table where one fits, else a `legacy_only_categories` entry such as
`legacy-scan-budget`). Entries whose mapping required judgment (a reason string
fitting two categories) get `owner_adjudicate: true`. The script embeds the mapping in
the artifact so the gate harness never re-infers it (S6.4).

```bash
env -u VIRTUAL_ENV uv run --group dev python scripts/normalize_legacy_reasons.py
```

- [ ] **Step 4: Run the validator to verify it passes**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/normalize_legacy_reasons.py tests/test_successor_checkpoint.py tests/fixtures/github_ci_successor_checkpoint/legacy_normalization.json
git commit -m "test: record the normalized legacy replay baseline"
```

---

### Task 10: README, manifest, integrity test, squash, review handoff

**Files:**
- Create: `tests/fixtures/github_ci_successor_checkpoint/README.md`
- Create: `tests/fixtures/github_ci_successor_checkpoint/MANIFEST.sha256`
- Create: `scripts/successor_checkpoint_manifest.py`
- Modify: `tests/test_successor_checkpoint.py` (append the permanent integrity tests)

**Interfaces:**
- Consumes: every artifact from Tasks 1 through 9.
- Produces: the immutable-inputs manifest (S8) and the squashed checkpoint commit.

- [ ] **Step 1: Write the failing integrity tests**

Append to `tests/test_successor_checkpoint.py` (add `import hashlib` to the existing
top-of-file import block, not inline, or Ruff flags it):

```python
def _manifest_lines() -> list[str]:
    """Compute (sha256, checkpoint-relative-path) lines in path order."""
    lines = []
    for path in sorted(CHECKPOINT.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(CHECKPOINT).as_posix()}")
    return lines


def test_manifest_matches_checkpoint_inputs():
    """MANIFEST.sha256 covers exactly the checkpoint inputs, never evidence (S8)."""
    recorded = (CHECKPOINT / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    assert recorded == _manifest_lines()


def test_frozen_d3_checkpoint_untouched():
    """The successor checkpoint never mutates the frozen D3 checkpoint (S8)."""
    d3 = CHECKPOINT.parent / "github_ci_checkpoint"
    recorded = (d3 / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    computed = []
    for path in sorted(d3.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            computed.append(f"{digest}  {path.relative_to(d3).as_posix()}")
    assert recorded == computed
```

Note: before relying on the second test, read the D3 `MANIFEST.sha256` to confirm its
line format matches `"{digest}  {relpath}"`; if it differs, adapt the parser side of
the test to the recorded format rather than regenerating the frozen file.

- [ ] **Step 2: Run to verify the manifest test fails**

Run: `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest tests/test_successor_checkpoint.py -q --no-cov`
Expected: the D3 test passes; `test_manifest_matches_checkpoint_inputs` fails.

- [ ] **Step 3: Write README and generate the manifest**

`README.md`: one section per artifact group (pins, tables, protocol, corpus, tiers,
limits/budgets/tripwires, legacy normalization) with a two-sentence purpose each, the
S8 immutability contract ("this manifest covers inputs only and is never repinned; a
post-freeze change creates a new checkpoint revision and invalidates the entire
evaluation"), the build-container rationale from Task 2, and the ratification list:
the `tripwires.json` numbers, the Task 6 `owner_adjudicate` rows, the Task 9
adjudications, and the Tier 3B predeclared expectations.

`scripts/successor_checkpoint_manifest.py`: writes `MANIFEST.sha256` from the same
walk as `_manifest_lines()` (import the layout constants; do not duplicate the hash
format). Run it:

```bash
env -u VIRTUAL_ENV uv run --group dev python scripts/successor_checkpoint_manifest.py
```

- [ ] **Step 4: Full verification**

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python -m pytest -q
uv run --group dev ruff check src tests scripts
uv run --group dev ruff format --check src tests scripts
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
```

Expected: full suite green (2651 baseline tests plus the new validators), coverage
threshold met, all checks clean. No `src/` changes means no boundary or version drift.

- [ ] **Step 5: Squash to the single checkpoint commit**

```bash
git log --oneline f3865f8..HEAD   # confirm only this plan's commits follow the spec
git reset --soft f3865f8
git commit -m "test: freeze the successor evaluation predeclaration checkpoint"
git log --oneline -3
```

Then re-run the full pytest suite once on the squashed head to confirm nothing was
lost in the squash. Expected: identical green results.

- [ ] **Step 6: Hand off for owner checkpoint review**

Do not push, do not open a PR, do not start implementation. Report to Rick: the
checkpoint commit hash, the artifact inventory, the ratification list from the README
(tripwire numbers, `owner_adjudicate` rows, Tier 3B expectations), and the relabel
report path. Implementation planning starts only after Rick approves the checkpoint
and ratifies the tripwires (spec S10), recorded by flipping `tripwires.json`
`"ratified"` to true in a review-approved amendment commit (which, per S8, is a new
checkpoint revision: regenerate `MANIFEST.sha256` in that same commit and note the
revision in the README).
