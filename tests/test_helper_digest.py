"""Tests for the successor helper's manifest-derived build identity."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.check_helper_digest import compute_digest, covered_paths
from scripts.check_helper_digest import main as digest_main

REPO = Path(__file__).resolve().parents[1]
CHECKPOINT_MANIFEST = Path(
    "tests/fixtures/github_ci_successor_checkpoint/protocol/digest_manifest.json"
)
HELPER = Path("helper/doc-lattice-shell-parser")
PINNED_PARSER_VERSION = "mvdan.cc/sh/v3@v3.13.1"


def _string_dict(value: object) -> dict[str, object]:
    """Validate and type a JSON object with string keys."""
    assert isinstance(value, dict)
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = item
    return result


def _copy_digest_inputs(destination: Path) -> None:
    """Copy the frozen manifest and its current input set into a temporary repository."""
    manifest_target = destination / CHECKPOINT_MANIFEST
    manifest_target.parent.mkdir(parents=True)
    shutil.copy2(REPO / CHECKPOINT_MANIFEST, manifest_target)
    for relative in covered_paths(REPO):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)


def _run_digest_cli(repo_root: Path) -> subprocess.CompletedProcess[str]:
    """Run the digest checker against a repository tree."""
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [
            sys.executable,
            str(REPO / "scripts/check_helper_digest.py"),
            "--repo-root",
            str(repo_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _build_response(
    tmp_path: Path,
    *,
    environment_updates: dict[str, str] | None = None,
    unset_environment: tuple[str, ...] = (),
    source: str = "doc-lattice check",
) -> dict[str, object]:
    """Build the helper under an adversarial environment and return one response."""
    binary = tmp_path / "shell-parser"
    environment = os.environ.copy()
    environment.update(
        {
            "GOCACHE": str(tmp_path / "gocache"),
            "GOENV": "off",
            "GOFLAGS": "",
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
        }
    )
    if environment_updates is not None:
        environment.update(environment_updates)
    for name in unset_environment:
        environment.pop(name, None)
    build = subprocess.run(  # noqa: S603 - fixed repository build wrapper
        [str(REPO / "scripts/build_successor_helper.sh"), str(binary)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO,
        env=environment,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    request = json.dumps(
        {"protocol_version": 1, "sources": [{"id": 0, "source": source}]},
        separators=(",", ":"),
    ).encode()
    completed = subprocess.run(  # noqa: S603 - pytest-owned binary path
        [str(binary)],
        input=request,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return _string_dict(json.loads(completed.stdout))


def _overlay_attack(tmp_path: Path) -> Path:
    """Create an overlay that forges the helper identity after digest computation."""
    original = REPO / HELPER / "main.go"
    forged = tmp_path / "forged-main.go"
    source = original.read_text(encoding="utf-8")
    old = "HelperVersion:   helperVersion,"
    assert source.count(old) == 1
    forged.write_text(source.replace(old, 'HelperVersion:   "forged-overlay",'), encoding="utf-8")
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps({"Replace": {str(original): str(forged)}}),
        encoding="utf-8",
    )
    return overlay


def _run_build_to(output: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the build wrapper with a neutral test cache and an explicit output."""
    environment = os.environ.copy()
    environment.update({"GOCACHE": str(tmp_path / "gocache"), "GOTOOLCHAIN": "local"})
    return subprocess.run(  # noqa: S603 - fixed repository build wrapper
        [str(REPO / "scripts/build_successor_helper.sh"), str(output)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO,
        env=environment,
        timeout=30,
    )


def test_digest_manifest_covers_all_non_test_go_sources() -> None:
    """Every non-test Go source under the helper module is a digest input."""
    covered = set(covered_paths(REPO))
    helper_root = REPO / HELPER
    expected = {
        path.relative_to(REPO)
        for path in helper_root.rglob("*.go")
        if not path.name.endswith("_test.go")
    }

    assert expected <= covered


def test_digest_is_deterministic_and_matches_pair_encoding() -> None:
    """The digest uses sorted path-NUL-file-hash pairs without a trailing newline."""
    paths = covered_paths(REPO)
    payload = "\n".join(
        f"{path.as_posix()}\0{hashlib.sha256((REPO / path).read_bytes()).hexdigest()}"
        for path in paths
    ).encode()

    expected = hashlib.sha256(payload).hexdigest()
    assert compute_digest(REPO) == expected
    assert compute_digest(REPO) == expected


def test_digest_changes_when_a_covered_file_changes(tmp_path: Path) -> None:
    """Changing one covered helper source changes the semantic digest."""
    _copy_digest_inputs(tmp_path)
    before = compute_digest(tmp_path)
    changed = tmp_path / HELPER / "wire.go"
    changed.write_bytes(changed.read_bytes() + b"\n")

    assert compute_digest(tmp_path) != before


def test_cli_rejects_an_uncovered_non_test_go_source(tmp_path: Path) -> None:
    """The CLI fails before printing a digest when manifest coverage is incomplete."""
    _copy_digest_inputs(tmp_path)
    manifest_path = tmp_path / CHECKPOINT_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["include"] = [
        "helper/doc-lattice-shell-parser/main.go",
        "tests/fixtures/github_ci_successor_checkpoint/protocol/schema.json",
        "tests/fixtures/github_ci_successor_checkpoint/protocol/encoder.json",
        "tests/fixtures/github_ci_successor_checkpoint/tables/",
        "tests/fixtures/github_ci_successor_checkpoint/limits.json",
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_digest_cli(tmp_path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "helper/doc-lattice-shell-parser/wire.go" in result.stderr


def test_cli_rejects_changed_completeness_rule(tmp_path: Path) -> None:
    """The CLI rejects a manifest whose frozen completeness rule was changed."""
    _copy_digest_inputs(tmp_path)
    manifest_path = tmp_path / CHECKPOINT_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["completeness_rule"] = "accept uncovered helper sources"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_digest_cli(tmp_path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "completeness_rule" in result.stderr


def test_cli_reports_digest_input_read_error(monkeypatch, capsys) -> None:
    """A digest-input filesystem failure is a clear status-2 CLI error."""

    def fail_read(_path: Path) -> bytes:
        raise OSError("injected digest read failure")

    monkeypatch.setattr(Path, "read_bytes", fail_read)

    assert digest_main(["--repo-root", str(REPO)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "helper digest: injected digest read failure" in captured.err


def test_build_wrapper_rejects_device_output(tmp_path: Path) -> None:
    """The build wrapper rejects an existing character device as output."""
    result = _run_build_to(Path("/dev/null"), tmp_path)

    assert result.returncode == 2
    assert "regular" in result.stderr


def test_build_wrapper_rejects_fifo_output(tmp_path: Path) -> None:
    """The build wrapper rejects an existing FIFO as output."""
    fifo = tmp_path / "helper.fifo"
    os.mkfifo(fifo)

    result = _run_build_to(fifo, tmp_path)

    assert result.returncode == 2
    assert "regular" in result.stderr


def test_build_wrapper_injects_computed_digest(tmp_path: Path) -> None:
    """A real wrapper-built helper reports the exact manifest digest."""
    response = _build_response(tmp_path, source="true")
    assert response["helper_version"] == compute_digest(REPO)
    assert response["parser_version"] == PINNED_PARSER_VERSION


def test_build_wrapper_ignores_overlay_go_flag(tmp_path: Path) -> None:
    """An inherited source overlay cannot forge the reported helper identity."""
    overlay = _overlay_attack(tmp_path)

    response = _build_response(
        tmp_path,
        environment_updates={"GOFLAGS": f"-overlay={overlay}"},
    )

    assert response["helper_version"] == compute_digest(REPO)


def test_build_wrapper_ignores_modfile_go_flag(tmp_path: Path) -> None:
    """An inherited alternate modfile cannot redirect the release build graph."""
    missing_modfile = tmp_path / "attacker.mod"

    response = _build_response(
        tmp_path,
        environment_updates={"GOFLAGS": f"-modfile={missing_modfile}"},
    )

    assert response["parser_version"] == PINNED_PARSER_VERSION


def test_build_wrapper_ignores_persisted_overlay_go_flag(tmp_path: Path) -> None:
    """A persisted GOENV overlay cannot forge the reported helper identity."""
    overlay = _overlay_attack(tmp_path)
    goenv = tmp_path / "go.env"
    goenv.write_text(f"GOFLAGS=-overlay={overlay}\n", encoding="utf-8")

    response = _build_response(
        tmp_path,
        environment_updates={"GOENV": str(goenv)},
        unset_environment=("GOFLAGS",),
    )

    assert response["helper_version"] == compute_digest(REPO)


def test_build_wrapper_ignores_workspace_parser_replacement(tmp_path: Path) -> None:
    """An inherited workspace cannot replace parser behavior outside the digest."""
    neutral_environment = os.environ.copy()
    neutral_environment.update(
        {"GOENV": "off", "GOFLAGS": "", "GOTOOLCHAIN": "local", "GOWORK": "off"}
    )
    parser_module = subprocess.run(
        ["/usr/local/go/bin/go", "list", "-m", "-f", "{{.Dir}}", "mvdan.cc/sh/v3"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO / HELPER,
        env=neutral_environment,
    ).stdout.strip()
    parser_fork = tmp_path / "parser-fork"
    shutil.copytree(parser_module, parser_fork)
    parser_source = parser_fork / "syntax/parser.go"
    parser_source.chmod(0o644)
    source = parser_source.read_text(encoding="utf-8")
    parse_call = "\t\tp.stmts(yield)\n"
    assert source.count(parse_call) == 1
    parser_source.write_text(source.replace(parse_call, ""), encoding="utf-8")
    workspace = tmp_path / "go.work"
    workspace.write_text(
        "\n".join(
            (
                "go 1.26.5",
                f"use {REPO / HELPER}",
                f"replace mvdan.cc/sh/v3 => {parser_fork}",
                "",
            )
        ),
        encoding="utf-8",
    )

    response = _build_response(
        tmp_path,
        environment_updates={"GOWORK": str(workspace)},
    )

    assert response["parser_version"] == PINNED_PARSER_VERSION
    results = response["results"]
    assert isinstance(results, list)
    result = _string_dict(results[0])
    events = result["events"]
    assert isinstance(events, list)
    assert events
    event = _string_dict(events[0])
    assert event["kind"] == "command_site"


def test_build_wrapper_ignores_combined_hostile_runtime_environment(tmp_path: Path) -> None:
    """Python and Go runtime environment cannot redirect or disable a release build."""
    poisoned_root = tmp_path / "poisoned-runtime"

    response = _build_response(
        tmp_path,
        environment_updates={
            "GO111MODULE": "off",
            "GOROOT": str(poisoned_root / "go"),
            "PYTHONHOME": str(poisoned_root / "python"),
        },
    )

    assert response["helper_version"] == compute_digest(REPO)
    assert response["parser_version"] == PINNED_PARSER_VERSION
    results = response["results"]
    assert isinstance(results, list)
    result = _string_dict(results[0])
    events = result["events"]
    assert isinstance(events, list)
    assert events
    event = _string_dict(events[0])
    assert event["kind"] == "command_site"
