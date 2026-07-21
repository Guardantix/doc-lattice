"""Integrity gates for the issue #100 predeclaration checkpoint artifacts."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

CHECKPOINT = Path("tests/fixtures/github_ci_checkpoint")
_FROZEN_ACCEPTANCE_COUNT = 78
_FROZEN_ACCEPTANCE_SHA256 = (
    "7fb1da742f7f659e4454fbc85c4f7bf500f21d6cbbc275684878262bb9314f3b"  # pragma: allowlist secret
)

STATUSES = frozenset({"not_applicable", "certified", "uninspectable"})
REASON_CATEGORIES = frozenset(
    {
        "control-character",
        "unsupported-operator",
        "unsupported-expansion",
        "unquoted-expansion-in-command-word",
        "quote-spans-newline",
        "unterminated-quote",
        "control-flow-keyword",
        "assignment-prefix",
        "unstable-first-word",
        "policy-unresolvable",
        "cap-exceeded",
    }
)


def test_manifest_matches_artifacts():
    result = subprocess.run(
        [sys.executable, "scripts/checkpoint_manifest.py", "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_limits_match_spec():
    limits = json.loads((CHECKPOINT / "limits.json").read_text())
    assert limits["source_cap_chars"] == 1_048_576
    assert limits["invocation_cap"] == 10_000
    assert limits["token_cap"] == 262_144
    assert limits["statement_cap"] == 65_536
    assert limits["work_limit"] == "min(4194304, 4 * source_length + 4096)"
    assert set(limits["charges"]) == {
        "marker_pass",
        "character_examined",
        "token_emitted",
        "statement_closed",
        "policy_step",
    }


def test_category_d_exceptions_is_empty():
    assert json.loads((CHECKPOINT / "category_d_exceptions.json").read_text()) == []


def test_replay_inventory_is_internally_consistent():
    import hashlib  # noqa: PLC0415 (scoped to this test only)

    inventory = json.loads((CHECKPOINT / "replay_inventory.json").read_text())
    entries = inventory["entries"]
    assert inventory["count"] == len(entries) > 0
    hashes = [entry["sha256"] for entry in entries]
    assert hashes == sorted(hashes)
    for index, entry in enumerate(entries, start=1):
        assert entry["id"] == f"replay-{index:04d}"
        assert hashlib.sha256(entry["source"].encode()).hexdigest() == entry["sha256"]
    aggregate = hashlib.sha256("\n".join(hashes).encode()).hexdigest()
    assert inventory["aggregate_sha256"] == aggregate


def test_replay_inventory_remains_covered_by_suite_exercise(tmp_path):
    out = tmp_path / "replay_inventory.json"
    env = {**os.environ, "CHECKPOINT_REPLAY_OUT": str(out)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_github_ci_shell_scanner.py",
            "-p",
            "scripts.checkpoint_record_scanner_inputs",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    recorded = json.loads(out.read_text())
    frozen = json.loads((CHECKPOINT / "replay_inventory.json").read_text())
    recorded_shas = {entry["sha256"] for entry in recorded["entries"]}
    frozen_shas = {entry["sha256"] for entry in frozen["entries"]}
    assert frozen_shas <= recorded_shas


def test_replay_inventory_covers_acceptance_corpus():
    # tests/ is not a package: pytest prepends the test file's directory to sys.path, so this
    # cross-module import only resolves inside a running test.
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    inventory = json.loads((CHECKPOINT / "replay_inventory.json").read_text())
    labels = json.loads((CHECKPOINT / "acceptance_labels.json").read_text())["cases"]
    sources = {entry["source"] for entry in inventory["entries"]}
    frozen_cases = ACCEPTANCE_CASES[: len(labels)]
    missing = [d for d, script, _ in frozen_cases if script not in sources]
    assert missing == []


def test_acceptance_prefix_matches_frozen_ordered_digest():
    # tests/ is not a package: the cross-module import only resolves inside a running test.
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    frozen_cases = ACCEPTANCE_CASES[:_FROZEN_ACCEPTANCE_COUNT]
    payload = json.dumps(frozen_cases, ensure_ascii=True, separators=(",", ":"))

    assert len(frozen_cases) == _FROZEN_ACCEPTANCE_COUNT
    assert hashlib.sha256(payload.encode()).hexdigest() == _FROZEN_ACCEPTANCE_SHA256


def test_acceptance_labels_align_with_corpus():
    # tests/ is not a package: the cross-module import only resolves inside a running test.
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    labels = json.loads((CHECKPOINT / "acceptance_labels.json").read_text())["cases"]
    assert len(labels) == _FROZEN_ACCEPTANCE_COUNT
    assert len(ACCEPTANCE_CASES) >= len(labels)
    frozen_cases = ACCEPTANCE_CASES[: len(labels)]
    for row, (description, _script, expected) in zip(labels, frozen_cases, strict=True):
        assert row["description"] == description
        assert row["label"] in {
            "must-certify",
            "intentional-exit-2",
            "outside-direct-marker-contract",
        }
        assert row["expected_status"] in STATUSES
        if row["label"] == "must-certify":
            assert row["expected_status"] == "certified"
            assert isinstance(expected, tuple)
            assert [tuple(i) for i in row["expected_invocations"]] == list(expected)
        if row["label"] == "outside-direct-marker-contract":
            assert row["expected_status"] == "not_applicable"
            assert row["expected_invocations"] == []
        if row["label"] == "intentional-exit-2":
            assert row["expected_status"] == "uninspectable"
            assert row["reason_category"] in REASON_CATEGORIES


def test_acceptance_labels_marker_consistency():
    import re  # noqa: PLC0415

    # tests/ is not a package: the cross-module import only resolves inside a running test.
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    marker = re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)
    labels = json.loads((CHECKPOINT / "acceptance_labels.json").read_text())["cases"]
    frozen_cases = ACCEPTANCE_CASES[: len(labels)]
    for row, (_d, script, _e) in zip(labels, frozen_cases, strict=True):
        has_marker = bool(marker.search(script))
        assert (row["label"] == "outside-direct-marker-contract") == (not has_marker)


def test_tier3a_cases_are_well_formed():
    cases = json.loads((CHECKPOINT / "tier3a_cases.json").read_text())["cases"]
    assert [case["id"] for case in cases] == [
        "direct-audit",
        "direct-check",
        "direct-lint",
        "uvx-pinned-audit",
        "uvx-continuation",
        "uv-run-help",
        "uv-run-no-sync",
        "uv-tool-run-long-form",
        "dynamic-non-policy-arg",
        "conditional-list-and",
        "conditional-list-or",
        "offline-template-block",
        "linear-template-quoted-path",
    ]
    for case in cases:
        assert case["expected_status"] in STATUSES
        if case["expected_status"] == "uninspectable":
            assert case["reason_category"] in REASON_CATEGORIES


def test_tier3b_fixtures_are_well_formed():
    import re  # noqa: PLC0415 (scoped to this test only)

    marker = re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)
    tier3b = CHECKPOINT / "tier3b"
    provenance = json.loads((tier3b / "provenance.json").read_text())
    fixtures = provenance["fixtures"]
    assert len(fixtures) == 20
    repos = set()
    for row in fixtures:
        path = tier3b / f"{row['id']}.yml"
        text = path.read_text()
        assert marker.search(text), row["id"]
        assert "pull_request" in text
        assert row["source_url"].startswith("https://")
        assert len(row["source_commit"]) == 40
        repo = "/".join(row["source_url"].split("/")[3:5])
        assert repo not in repos
        repos.add(repo)
        assert row["expected_status"] in STATUSES
        if row["expected_status"] == "uninspectable":
            assert row["reason_category"] in REASON_CATEGORIES


def test_probes_and_bash_pin_are_well_formed():
    probes = json.loads((CHECKPOINT / "probes.json").read_text())
    assert probes["spans"], "probe span inventory must not be empty"
    for span in probes["spans"]:
        assert span["end"] > span["start"] >= 0
        assert span["text"]
        assert isinstance(span["expected_stable_argv_prefix"], list)
    pin = json.loads((CHECKPOINT / "bash_pin.json").read_text())
    assert pin["version"] == "5.2.21(1)-release"
    assert pin["container"].startswith("ubuntu:24.04@sha256:")
    assert len(pin["local_binary_sha256"]) == 64


def test_mutation_sites_reference_real_spans():
    probes = json.loads((CHECKPOINT / "probes.json").read_text())
    span_ids = {(s["fixture_id"], s["span_id"]) for s in probes["spans"]}
    mutations = json.loads((CHECKPOINT / "mutations.json").read_text())
    assert mutations["sites"], "mutation set must not be empty"
    for site in mutations["sites"]:
        assert site["kind"] in mutations["kinds"]
        assert (site["fixture_id"], site["span_id"]) in span_ids
        assert site["expected_reason_category"] in REASON_CATEGORIES
        assert site["offset"] >= 0
