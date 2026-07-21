"""Gate 7: the semantic differential for the issue #100 candidate recognizer.

Four layers verify the recognizer against independent oracles over the frozen checkpoint
corpus: the pinned Bash static parser plus ``shfmt`` structural agreement over the 35 certified
fixtures, span reproduction over the 36 frozen probe spans, probe execution under the pinned
Bash with recorder stubs, and the 50 boundary mutations. The probe layer executes only the
synthesized single-command probe body of each span, one probe per list arm, under a PATH that
contains only the three recorder stubs; original fixture text is never executed.
"""

import hashlib
import json
import stat
import subprocess
from pathlib import Path

from doc_lattice.github_ci.direct_marker_scanner import (
    certified_command_words,
    scan_execution_source,
)

BASH = "/bin/bash"

# mvdan/sh typed-JSON operator codes for the two list operators the recognizer accepts. A pipe
# (code 13) never appears in a certified source, so only these two flatten into arms.
_AND_OR_OPS = frozenset({11, 12})


def _bash_pin_checked():
    from github_ci_evaluation_harness import load_bash_pin  # noqa: PLC0415

    pin = load_bash_pin()
    version = subprocess.run(  # noqa: S603 - pinned system Bash at a fixed absolute path
        [BASH, "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    assert pin["version"] in version, (version, pin["version"])
    digest = hashlib.sha256(Path(BASH).read_bytes()).hexdigest()
    assert digest == pin["local_binary_sha256"], (digest, pin["local_binary_sha256"])
    return pin


def _shfmt_command_structure(tree):
    """Flatten a shfmt typed-JSON File into ``(word_count, first_literal_or_None)`` per command.

    Simple commands (``CallExpr`` with arguments) contribute one entry whose count is the number
    of arguments and whose first literal is the value of a single-``Lit`` first argument (or
    ``None`` when the first word is not a bare literal). ``&&``/``||`` lists (``BinaryCmd``)
    recurse into their two arms in source order. Assignment-only statements (``CallExpr`` with
    assignments and no arguments) contribute nothing, matching the recognizer, which reports only
    commands. Comment lines produce no statements.

    Args:
        tree: The decoded ``shfmt --to-json`` document for one source.

    Returns:
        The command structure as a list of ``(word_count, first_literal_or_None)`` tuples in
        source order.
    """
    commands: list[tuple[int, str | None]] = []

    def walk(cmd):
        if cmd is None:
            return
        node_type = cmd.get("Type")
        if node_type == "BinaryCmd" and cmd.get("Op") in _AND_OR_OPS:
            walk(cmd["X"].get("Cmd"))
            walk(cmd["Y"].get("Cmd"))
            return
        if node_type == "CallExpr":
            args = cmd.get("Args") or []
            if not args:
                return
            first_literal = None
            parts = args[0].get("Parts") or []
            if len(parts) == 1 and parts[0].get("Type") == "Lit":
                first_literal = parts[0].get("Value")
            commands.append((len(args), first_literal))
            return
        # A certified source contains only simple commands and &&/|| lists; any other command
        # node is recorded verbatim so the count comparison surfaces it as a gate finding.
        commands.append((-1, node_type))

    for statement in tree.get("Stmts") or []:
        walk(statement.get("Cmd"))
    return commands


def _certified_corpus():
    from github_ci_evaluation_harness import (  # noqa: PLC0415
        load_tier3a_cases,
        load_tier3b_provenance,
        tier3b_run_block,
    )
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    labels = json.loads(
        Path("tests/fixtures/github_ci_checkpoint/acceptance_labels.json").read_text()
    )["cases"]
    corpus = [
        (row["description"], script)
        for row, (_d, script, _e) in zip(labels, ACCEPTANCE_CASES, strict=True)
        if row["label"] == "must-certify"
    ]
    corpus += [
        (case["id"], case["source"])
        for case in load_tier3a_cases()
        if case["expected_status"] == "certified"
    ]
    corpus += [
        (row["id"], tier3b_run_block(row["id"]))
        for row in load_tier3b_provenance()["fixtures"]
        if row["expected_status"] == "certified"
    ]
    assert len(corpus) == 7 + 11 + 17
    return corpus


def test_static_layer_bash_and_shfmt_agree(tmp_path):
    _bash_pin_checked()
    for name, source in _certified_corpus():
        script = tmp_path / "candidate.sh"
        script.write_text(source if source.endswith("\n") else source + "\n")
        bash_check = subprocess.run(  # noqa: S603 - pinned system Bash at a fixed absolute path
            [BASH, "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert bash_check.returncode == 0, (name, bash_check.stderr)
        shfmt = subprocess.run(
            ["shfmt", "--to-json"],  # noqa: S607 - shfmt is provided on PATH by the dev group
            input=script.read_text(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert shfmt.returncode == 0, (name, shfmt.stderr)
        shfmt_commands = _shfmt_command_structure(json.loads(shfmt.stdout))
        recognizer_commands = certified_command_words(source)
        assert len(shfmt_commands) == len(recognizer_commands), name
        for (shfmt_count, shfmt_first), rec_words in zip(
            shfmt_commands, recognizer_commands, strict=True
        ):
            assert shfmt_count == len(rec_words), (name, rec_words)
            if shfmt_first is not None:
                assert shfmt_first == rec_words[0], name


def _span_sources():
    from github_ci_evaluation_harness import (  # noqa: PLC0415
        load_probes,
        load_tier3a_cases,
        load_tier3b_provenance,
        tier3b_run_block,
    )
    from test_github_ci_shell_scanner import ACCEPTANCE_CASES  # noqa: PLC0415

    by_fixture = {}
    for description, script, _expected in ACCEPTANCE_CASES:
        by_fixture[description] = script
    for case in load_tier3a_cases():
        by_fixture[case["id"]] = case["source"]
    for row in load_tier3b_provenance()["fixtures"]:
        by_fixture[row["id"]] = tier3b_run_block(row["id"])
    return load_probes(), by_fixture


def test_probe_spans_are_reproduced_by_the_recognizer():
    probes, _by_fixture = _span_sources()
    assert len(probes["spans"]) == 36
    for span in probes["spans"]:
        result = scan_execution_source(span["text"])
        assert result.status == "certified", (span["span_id"], result.reason)
        commands = certified_command_words(span["text"])
        assert len(commands) == 1, span["span_id"]
        prefix = span["expected_stable_argv_prefix"]
        assert list(commands[0][: len(prefix)]) == prefix, span["span_id"]


def _write_stubs(stub_dir, record_path):
    stub_dir.mkdir()
    for name in ("doc-lattice", "uvx", "uv"):
        stub = stub_dir / name
        stub.write_text(
            '#!/bin/bash\nprintf \'%s\\n\' "===probe===" "$0" "$@" >> "$PROBE_RECORD"\n'
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return record_path


def test_probe_layer_matches_bash_execution(tmp_path):
    _bash_pin_checked()
    probes, _by_fixture = _span_sources()
    stub_dir = tmp_path / "stubs"
    record = _write_stubs(stub_dir, tmp_path / "record.txt")
    for span in probes["spans"]:
        record.write_text("")
        probe = tmp_path / "probe.sh"
        probe.write_text(span["text"] + "\n")
        env = dict(probes["env"])
        env["PATH"] = str(stub_dir)
        env["PROBE_RECORD"] = str(record)
        completed = subprocess.run(  # noqa: S603 - pinned system Bash at a fixed absolute path
            [BASH, str(probe)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, (span["span_id"], completed.stderr)
        lines = record.read_text().splitlines()
        assert lines, span["span_id"]
        assert lines[0] == "===probe===", span["span_id"]
        assert lines.count("===probe===") == 1, span["span_id"]
        argv = [Path(lines[1]).name, *lines[2:]]
        prefix = span["expected_stable_argv_prefix"]
        assert argv[: len(prefix)] == prefix, (span["span_id"], argv)

        result = scan_execution_source(span["text"])
        expected = span["expected_verdict"]
        if expected is None:
            assert result.invocations == (), span["span_id"]
        else:
            assert result.invocations == ((expected["subcommand"], expected["dry_run"]),), span[
                "span_id"
            ]


def test_boundary_mutations_all_refuse_at_their_sites():
    from github_ci_evaluation_harness import load_mutations  # noqa: PLC0415

    _probes, by_fixture = _span_sources()
    mutations = load_mutations()
    assert len(mutations["sites"]) == 50
    for site in mutations["sites"]:
        source = by_fixture[site["fixture_id"]]
        offset = site["offset"]
        mutated = source[:offset] + site["inserted_text"] + source[offset:]
        result = scan_execution_source(mutated)
        assert result.status == "uninspectable", (site["span_id"], site["kind"])
        assert result.reason_category == site["expected_reason_category"], (
            site["span_id"],
            site["kind"],
            result.reason_category,
        )
        assert offset <= result.offset <= offset + len(site["inserted_text"]), (
            site["span_id"],
            site["kind"],
            result.offset,
        )
