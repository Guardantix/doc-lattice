"""Validators for the successor evaluation checkpoint artifacts (spec S8)."""

import hashlib
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
    convention = table["traversal_convention"]
    assert convention["container_rule"]
    assert convention["wildcard_rule"]


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
    """The pre-policy command matrix freezes the six S5.2 rows verbatim."""
    rows = _load("tables/pre_policy_matrix.json")["rows"]
    by_case = {r["case"]: r for r in rows}
    assert by_case["assignments-only"]["outcome"] == "no-command-no-refusal"
    assert by_case["assignments-plus-argv-literal"]["outcome"] == "certify-with-invocations"
    assert (
        by_case["assignments-plus-argv-dynamic"]["outcome"]
        == "assignment-prefix-refusal-retain-argv"
    )
    assert by_case["first-word-unknown"]["outcome"] == "unstable-first-word"
    assert by_case["multi-cardinality-word"]["outcome"] == "splitting-unsafe-word"
    assert by_case["ir-invariant"]["outcome"] == "text-implies-single"


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
    assert set(
        event["oneOf"][0]["properties"]["kind"]["enum"]
        + event["oneOf"][1]["properties"]["kind"]["enum"]
    ) == {"command_site", "refusal"}


def test_conformance_and_negative_fixture_sets():
    """Positive fixtures validate; negative fixtures enumerate the S4.2 rejections."""
    conformance = sorted((CHECKPOINT / "protocol" / "conformance").iterdir())
    negative = sorted((CHECKPOINT / "protocol" / "negative").iterdir())
    assert len(conformance) == 7
    assert len(negative) == 15
    names = {p.stem for p in negative}
    assert {
        "duplicate-keys",
        "invalid-utf8",
        "lone-surrogate",
        "escaped-lone-surrogate",
        "trailing-document",
        "wrong-type-bool-as-int",
        "non-contiguous-ids",
        "empty-batch",
        "nan-number",
        "unknown-field",
        "out-of-order-results",
        "span-out-of-range",
        "max-length-four-byte-source",
        "source-count-over-limit",
        "json-depth-over-limit",
    } <= names
    boundary = CHECKPOINT / "protocol" / "boundary"
    assert boundary.is_dir()
    at_limit = boundary / "source-count-at-limit.json"
    assert at_limit.is_file()
    request = json.loads(at_limit.read_text(encoding="utf-8"))
    assert len(request["sources"]) == 4096


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


def test_limits_freeze_spec_numbers():
    """All S3.5 and S4.4 numbers appear exactly once, in limits.json."""
    limits = _load("limits.json")
    assert limits["python_source_cap_chars"] == 1_048_576
    assert limits["helper_source_cap_bytes"] == 4_194_304
    assert limits["aggregate_request_cap_bytes"] == 8_388_608
    assert limits["stdout_cap_bytes"] == 16_777_216
    assert limits["stderr_capture_cap_bytes"] == 65_536
    assert limits["max_sources_per_batch"] == 4096
    assert limits["json_max_depth"] == 64
    assert limits["max_argv_words_per_site"] == 4096
    assert limits["max_assignments_per_site"] == 256
    deadline = limits["deadline_ms"]
    assert deadline == {"base": 2000, "per_source": 25, "per_4096_bytes": 1, "ceiling": 30000}
    for key in ("statement_cap", "visitor_node_cap", "visitor_depth_cap", "event_cap"):
        assert isinstance(limits[key], int)
        assert limits[key] > 0
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


_LABELS = frozenset({"must-certify", "intentional-exit-2", "outside-direct-marker-contract"})


def test_successor_acceptance_labels_cover_all_rows():
    """Every acceptance row has a successor label, derivation, and consistent tuple."""
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    cases = _load("corpus/acceptance_labels.json")["cases"]
    assert len(cases) == len(ACCEPTANCE_CASES) == 87
    for row, case in zip(ACCEPTANCE_CASES, cases, strict=True):
        assert case["description"] == row[0]
        assert case["source"] == row[1]
        assert case["label"] in _LABELS
        assert case["derivation"]
        if case["label"] == "must-certify":
            assert case["expected_status"] == "certified"
        if case["label"] == "intentional-exit-2":
            assert case["expected_status"] == "uninspectable"
            assert case["reason_category"]
    adjudications = [c for c in cases if c.get("owner_adjudicate")]
    assert len(adjudications) <= 12, "too many unresolved judgment calls for review"


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


def _labeled_sources() -> list[tuple[str, str, str]]:
    """Collect (origin, label, source) for every corpus case and fixture row with a source."""
    rows: list[tuple[str, str, str]] = []
    for case in _load("corpus/acceptance_labels.json")["cases"]:
        if "source" in case:
            rows.append((f"corpus:{case['description']}", case["label"], case["source"]))
    for name, fixtures in _load("corpus/new_fixtures.json")["families"].items():
        for row in fixtures:
            if "source" in row:
                rows.append((f"{name}:{row['id']}", row["label"], row["source"]))
    return rows


def test_d2_reachability_bidirectional_consistency():
    """A labeled source is outside-direct-marker-contract iff its raw text has no marker (S6.2).

    D2 gates on authored raw text: a source with no ``DIRECT_MARKER_RE`` match is dropped at
    collection (S5.1) and can never reach the certifier, so it must be labeled
    ``outside-direct-marker-contract``. Conversely a source whose raw text matches the marker
    anywhere (comments count; template sources keep the literal ``{0}``, whose eventual synthetic
    sentinel never counts) is batched and must not carry that label. This guards against a
    fixture predeclaring an unreachable expectation or mislabeling a batched source.
    """
    from doc_lattice.github_ci.direct_marker_scanner import DIRECT_MARKER_RE  # noqa: PLC0415

    violations = []
    for origin, label, source in _labeled_sources():
        has_marker = DIRECT_MARKER_RE.search(source) is not None
        is_outside = label == "outside-direct-marker-contract"
        if is_outside == has_marker:
            violations.append((origin, label, has_marker, source))
    assert not violations, f"D2 reachability inconsistencies: {violations}"


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
        assert {"path", "reachable_steps", "marker_gated_sources", "batched_sources"} <= set(
            workflow
        )


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
    # The four contextual-collapse strings are classified per entry from source text (S6.4), so
    # their sub-rules are frozen, not a single global category.
    contextual = artifact["contextual_mapping"]
    assert contextual["strings"], "the contextual pins must be recorded"
    assert contextual["rules"], "the contextual sub-rules must be recorded"
    # One representative individual entry per resolved context class, pinned by id.
    by_id = {entry["id"]: entry for entry in artifact["entries"]}
    assert by_id["replay-0023"]["reason_category"] == "assignment-prefix"  # dynamic env prefix
    assert not by_id["replay-0023"]["owner_adjudicate"]
    assert by_id["replay-0532"]["reason_category"] == "splitting-unsafe-word"  # "$@" splat head
    assert not by_id["replay-0532"]["owner_adjudicate"]
    assert by_id["replay-0073"]["reason_category"] == "policy-unresolvable"  # quoted subcommand
    assert not by_id["replay-0073"]["owner_adjudicate"]
    assert by_id["replay-0057"]["reason_category"] == "unstable-first-word"  # glob head
    assert not by_id["replay-0057"]["owner_adjudicate"]
    # An unresolved shape keeps the conservative pin with owner_adjudicate true.
    assert by_id["replay-0060"]["reason_category"] == "unquoted-expansion-in-command-word"
    assert by_id["replay-0060"]["owner_adjudicate"]


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
