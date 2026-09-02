"""CLI integration tests for the init command."""

import errno
import os
import subprocess
from pathlib import Path

import pytest

import doc_lattice.cli.commands.init as init_command
from doc_lattice import __version__, persistence
from doc_lattice.cli import app
from doc_lattice.path_utils import format_path_for_display

from .helpers import runner


@pytest.fixture(autouse=True)
def _git_repository(tmp_path: Path) -> None:
    """Run init command tests inside a real Git working tree."""
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - tests require the local git executable
        cwd=tmp_path,
        check=True,
    )


# Spelled out here rather than imported from the command module, so a wording change fails the
# test instead of being followed silently.
_EXPECTED_BASELINE_GUIDANCE = (
    "For an initial adoption with no established baseline, run `doc-lattice reconcile --all` "
    "once after annotating documents and before enabling the gates. It acknowledges the "
    "current state so the gates start from a known baseline; BROKEN edges are skipped and "
    "remain findings, so this does not by itself make CI green."
)

# The referent for the line above's "before enabling the gates", spelled out here for the same
# reason: a wording change has to fail this test rather than be followed silently.
_EXPECTED_ACTIVATION_GUIDANCE = (
    "Enabling the gates is that separate step, and adding the pre-commit block does not perform "
    "it: all three hooks stay inert until pre-commit writes `.git/hooks/pre-commit` in this "
    "clone. If this clone is not already gated, run `uv tool install pre-commit` and then "
    "`uv tool run pre-commit install`, or plain `pre-commit install` when a durable runner is "
    "already available. On an initial adoption do that only after the baseline above, and after "
    "`doc-lattice check` and `doc-lattice lint` are clean; an established installation enables "
    "them immediately."
)

# The recipe's own Git precondition, spelled out here for the same reason as the two above. It is
# printed unconditionally, so this is also the only place that pins the wording an adopter outside
# a worktree reads before the blocks that cannot be installed there.
_EXPECTED_GIT_PRECONDITION_GUIDANCE = (
    "The three blocks and the activation step install into a Git checkout of the docs "
    "repository, and none of them is usable outside one: the ignore patterns need a repository "
    "to be ignored in, the workflow needs a GitHub repository to run in, and activation needs a "
    "clone to write its hook into. Producing this output needs no repository of its own, so the "
    "precondition is on the repository you install into rather than on the directory this run "
    "happened in."
)


def _shared_guidance(version: str) -> str:
    return (
        "# ===== .gitignore (append these lines) =====\n"
        ".doc-lattice-reconcile.json\n"
        ".doc-lattice-reconcile.json.*.tmp\n"
        ".*.doc-lattice-before.*.tmp\n"
        ".*.doc-lattice-after.*.tmp\n"
        "\n"
        "# ===== .pre-commit-config.yaml (add under `repos:`) =====\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: doc-lattice-check\n"
        "        name: doc-lattice check\n"
        f"        entry: uvx --python 3.13 --from doc-lattice=={version} "
        "doc-lattice check\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
        "      - id: doc-lattice-lint\n"
        "        name: doc-lattice lint\n"
        f"        entry: uvx --python 3.13 --from doc-lattice=={version} "
        "doc-lattice lint\n"
        "        language: system\n"
        "        files: \\.md$\n"
        "        pass_filenames: false\n"
        "      # always_run rather than files: \\.md$, because the break links catches is\n"
        "      # cross-document: renaming a heading in one file invalidates a link written in\n"
        "      # another, and the file that changed is not the file that ends up wrong.\n"
        "      - id: doc-lattice-links\n"
        "        name: doc-lattice links\n"
        f"        entry: uvx --python 3.13 --from doc-lattice=={version} "
        "doc-lattice links\n"
        "        language: system\n"
        "        always_run: true\n"
        "        pass_filenames: false\n"
        "\n"
    )


# GTX-216: every value `init` validates before it writes anything reports VALIDATION_ERROR.
# Asserted structurally rather than by substring, so a code that merely contains the right word
# somewhere in a longer diagnostic cannot pass: the code sits in the leading `error (CODE):`
# field, and the old code must be absent from the whole stream. The scaffold check is the other
# half of the contract -- rejection happens before any write, so the directory keeps only its
# `.git` from the autouse repository fixture.
def _assert_rejected_before_any_write(result, tmp_path: Path) -> None:
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error (VALIDATION_ERROR): ")
    assert "CONFIG_ERROR" not in result.stderr
    assert {path.name for path in tmp_path.iterdir()} == {".git"}


def _legacy_stdout(version: str) -> str:
    return (
        _shared_guidance(version) + "# ===== .github/workflows/doc-lattice.yml (new file) =====\n"
        "name: doc-lattice\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  check:\n"
        "    name: Traceability check\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        # Spelled out rather than interpolated from constants, so this stays an independent
        # assertion of the exact bytes init prints.
        "      - uses: actions/checkout@"
        "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"  # pragma: allowlist secret
        "        with:\n"
        "          persist-credentials: false\n"
        "      - uses: astral-sh/setup-uv@"
        "20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1\n"  # pragma: allowlist secret
        "        with:\n"
        "          enable-cache: false\n"
        "      - run: |\n"
        "          set +e\n"
        f"          uvx --python 3.13 --from doc-lattice=={version} doc-lattice check\n"
        "          rc_check=$?\n"
        f"          uvx --python 3.13 --from doc-lattice=={version} doc-lattice lint\n"
        "          rc_lint=$?\n"
        f"          uvx --python 3.13 --from doc-lattice=={version} doc-lattice links "
        "--format github\n"
        "          rc_links=$?\n"
        '          [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ] && [ "$rc_links" -eq 0 ]\n'
        "\n"
    )


def test_init_delegates_create_only_write_to_shared_persistence(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, bytes, str]] = []

    def capture(path: Path, data: bytes, *, prefix: str) -> None:
        calls.append((path, data, prefix))

    monkeypatch.setattr(init_command, "atomic_create_bytes", capture)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert len(calls) == 1
    target, data, prefix = calls[0]
    assert target == tmp_path / ".doc-lattice.yml"
    assert data == (
        b"# doc-lattice configuration. See https://github.com/Guardantix/doc-lattice\n"
        b"lattice_format: 2\n"
        b"docs_roots:\n"
        b"  - docs\n"
        b"link_sources:\n"
        b"  - docs/**/*.md\n"
        b"# ignore_globs:\n"
        b'#   - "**/archive/**"\n'
        b"# cache_key: my-project-docs   # opt-in load cache slot under your cache home\n"
        b"# cache_trust_stat: true       "
        b"# opt-in stat fast tier for read-only commands (needs cache_key)\n"
        b"# linear_team: ENG\n"
    )
    assert prefix == ".doc-lattice.yml."
    assert result.stdout == _legacy_stdout(__version__)
    assert result.stderr == (
        # GTX-212: the scaffolded name is display-spelled like every other path in human
        # output, which quotes a name that used to be printed bare.
        "wrote '.doc-lattice.yml'\n"
        # The fixture repository has no remote, so the probe finds nothing and the fixed
        # fallback is used. Naming the source is what makes a silent fallback visible.
        "workflow triggers on branch main (fallback)\n"
        # The recipe's Git precondition, ahead of everything it qualifies and on its own line
        # rather than folded into the three-cause `(fallback)` label above. Hard-wrapped here,
        # unlike the two guidance lines below: it carries no copyable command, so it is prose
        # like the placement line and is printed without soft_wrap.
        "The three blocks and the activation step install into a Git checkout of the docs\n"
        "repository, and none of them is usable outside one: the ignore patterns need a \n"
        "repository to be ignored in, the workflow needs a GitHub repository to run in, \n"
        "and activation needs a clone to write its hook into. Producing this output needs\n"
        "no repository of its own, so the precondition is on the repository you install \n"
        "into rather than on the directory this run happened in.\n"
        "Append the .gitignore block, add the pre-commit block under `repos:`, save the \n"
        "workflow as .github/workflows/doc-lattice.yml, and make sure the exact pinned \n"
        f"version {__version__} is published on PyPI so the snippets resolve.\n"
        # One unwrapped line: soft_wrap keeps Rich from splitting `doc-lattice reconcile --all`
        # across a hard newline, which would break the command on copy and in redirection.
        f"{_EXPECTED_BASELINE_GUIDANCE}\n"
        # Unwrapped for the same reason, and it carries three commands rather than one.
        f"{_EXPECTED_ACTIVATION_GUIDANCE}\n"
    )


def _origin_head(cwd: Path, branch: str) -> None:
    """Point the fixture repository's cached origin/HEAD at a real remote-tracking branch."""
    for arguments in (
        (
            "-c",
            "user.name=doc-lattice tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "seed",
        ),
        ("update-ref", f"refs/remotes/origin/{branch}", "HEAD"),
        ("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"),
    ):
        subprocess.run(  # noqa: S603 - arguments are test-local literals
            ["git", *arguments],  # noqa: S607 - tests require the local git executable
            cwd=cwd,
            check=True,
            capture_output=True,
        )


def test_init_uses_the_probed_default_branch_and_names_its_source(tmp_path: Path, monkeypatch):
    _origin_head(tmp_path, "trunk")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "    branches: [trunk]\n" in result.stdout
    assert "[main]" not in result.stdout
    assert "workflow triggers on branch trunk (origin/HEAD)\n" in result.stderr


def test_init_default_branch_flag_beats_the_probe(tmp_path: Path, monkeypatch):
    # Precedence is flag, then probe, then fallback. The probe is a local hint that cannot see
    # an upstream rename, so the explicit flag has to win outright.
    _origin_head(tmp_path, "trunk")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--default-branch", "develop"])

    assert result.exit_code == 0
    assert "    branches: [develop]\n" in result.stdout
    assert "[trunk]" not in result.stdout
    assert "workflow triggers on branch develop (--default-branch)\n" in result.stderr


def test_init_falls_back_to_main_without_a_probe_result(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "    branches: [main]\n" in result.stdout
    assert "workflow triggers on branch main (fallback)\n" in result.stderr


def test_init_reports_the_branch_before_the_copy_paste_guidance(tmp_path: Path, monkeypatch):
    # Stdout owns the artifacts; the branch and its source belong on stderr, ahead of the
    # instructions that tell an adopter where to paste them.
    _origin_head(tmp_path, "trunk")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.stderr.index("workflow triggers on branch trunk") < result.stderr.index(
        "Append the .gitignore block"
    )
    assert "workflow triggers on branch" not in result.stdout


def test_init_names_the_git_precondition_between_the_branch_and_the_placement(
    tmp_path: Path,
    monkeypatch,
):
    # The precondition is about the recipe, the branch record is about this run, and they stay
    # separate lines in that order: the branch record must not become a fourth spelling of the
    # precondition, and the precondition must precede the instructions it qualifies. Run inside
    # the fixture repository with a controlled branch source, so this pins ordering alone and
    # `test_init_outside_any_repository_...` pins the outside-a-worktree case.
    _origin_head(tmp_path, "trunk")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    # Normalized because the line is prose and wraps, so its bytes are split by the console.
    stderr = " ".join(result.stderr.split())
    assert stderr.index("workflow triggers on branch trunk") < stderr.index(
        _EXPECTED_GIT_PRECONDITION_GUIDANCE
    )
    assert stderr.index(_EXPECTED_GIT_PRECONDITION_GUIDANCE) < stderr.index(
        "Append the .gitignore block"
    )
    assert _EXPECTED_GIT_PRECONDITION_GUIDANCE not in " ".join(result.stdout.split())


def _assert_outside_any_repository(directory: Path) -> None:
    """Fail unless no ancestor of `directory` carries a repository marker.

    The premise of the test below, asserted rather than assumed. pytest's basetemp is
    configurable, and one placed inside a checkout would put a repository above the directory,
    turning a test about running outside a worktree into a test about running inside one: the
    branch probe would report that repository's `origin/HEAD` instead of falling back, and the
    ancestor walk would find its boundary. Fail loudly rather than skip, so the case the issue
    names keeps being exercised instead of quietly going unrun.

    Args:
        directory: The invocation directory the test is about to use.
    """
    for ancestor in (directory, *directory.parents):
        marker = ancestor / ".git"
        # lexists, not exists: a dangling symlink is still a marker the command would honor.
        if os.path.lexists(marker):
            pytest.fail(
                f"expected no repository above {directory}, found {marker}: "
                "pytest's basetemp appears to sit inside a Git checkout"
            )


def test_init_outside_any_repository_prints_the_full_recipe_and_the_precondition(
    tmp_path_factory,
    monkeypatch,
):
    # The case the autouse fixture makes unreachable from `tmp_path`: a directory with no
    # repository anywhere above it. `init` has no Git requirement, so it still writes the config
    # and prints all three blocks byte-for-byte; what changes is that the recipe's own Git
    # precondition is named before them, which is the whole of what an adopter here can act on.
    outside = tmp_path_factory.mktemp("outside_any_repository")
    _assert_outside_any_repository(outside)
    monkeypatch.chdir(outside)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert result.stdout == _legacy_stdout(__version__)
    assert (outside / ".doc-lattice.yml").is_file()
    stderr = " ".join(result.stderr.split())
    assert _EXPECTED_GIT_PRECONDITION_GUIDANCE in stderr
    # The branch record stays the three-cause `(fallback)` label rather than disambiguating into
    # a fourth source spelling that names the missing worktree.
    assert "workflow triggers on branch main (fallback)\n" in result.stderr


@pytest.mark.parametrize("branch", ["release/*", "main branch", "!main", "..", "réf", "main."])
def test_init_rejects_hostile_default_branch_before_any_write(
    tmp_path: Path,
    monkeypatch,
    branch: str,
):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--default-branch", branch])

    assert "must be an ASCII Git branch name" in result.stderr
    # An explicit flag value is a command-line input, so the code names validation rather than a
    # config file `init` has not written and never reads.
    _assert_rejected_before_any_write(result, tmp_path)


def test_init_rejects_a_probed_branch_outside_the_supported_domain(tmp_path: Path, monkeypatch):
    # Discovery failure falls back silently; a name that was actually discovered and then fails
    # policy is reported instead, because rendering it would produce a filter that is wrong.
    monkeypatch.setattr(init_command, "probe_default_branch", lambda _cwd: "release/*")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert "must be an ASCII Git branch name" in result.stderr
    # The probed candidate moves with the flag deliberately: it is not a command-line value, but
    # it is still an input this run validated, and CONFIG_ERROR is exactly as wrong here.
    _assert_rejected_before_any_write(result, tmp_path)


def test_init_writes_config_and_prints_codegen(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    config = (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8")
    assert "docs_roots:" in config
    assert "- docs" in config
    assert ".pre-commit-config.yaml" in result.stdout
    assert ".github/workflows/doc-lattice.yml" in result.stdout
    assert f"--from doc-lattice=={__version__}" in result.stdout
    assert "git+" not in result.stdout
    narration = " ".join(result.stderr.split())
    assert f"exact pinned version {__version__} is published on PyPI" in narration
    assert "tag is pushed" not in narration


def test_init_prints_first_adoption_baseline_guidance(tmp_path: Path, monkeypatch):
    # Without a baseline, a fresh adoption turns CI red on its first run and reads as a
    # misconfiguration, so init has to say so.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    narration = " ".join(result.stderr.split())
    assert _EXPECTED_BASELINE_GUIDANCE in narration


def test_baseline_guidance_has_one_owner():
    # init prints the module-level constant verbatim, so the narration cannot drift from it.
    assert " ".join(init_command._BASELINE_GUIDANCE.split()) == _EXPECTED_BASELINE_GUIDANCE


def test_baseline_guidance_is_scoped_and_does_not_promise_green_ci():
    # init is rerunnable against an existing config, and reconcile --all acknowledges every
    # STALE and UNRECONCILED edge, so unqualified guidance would tell an established adopter to
    # erase legitimate drift. BROKEN edges are skipped, so the baseline is not a green-CI promise.
    guidance = " ".join(init_command._BASELINE_GUIDANCE.split())

    assert "initial adoption with no established baseline" in guidance
    assert "doc-lattice reconcile --all" in guidance
    assert "does not by itself make CI green" in guidance


def test_init_names_the_activation_step_the_baseline_guidance_orders_against(
    tmp_path: Path, monkeypatch
):
    # _BASELINE_GUIDANCE orders reconciliation "before enabling the gates". GTX-175 gave that
    # phrase a referent in README.md and MANAGED_CI.md and left init's own output out of scope,
    # so the CLI went on asserting an ordering constraint while never naming the act it orders
    # against. This is that half.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    # Its own unwrapped line: soft_wrap keeps the three commands it carries copyable, and a
    # separate line is what keeps placement, baseline, and activation three distinct acts.
    assert f"\n{_EXPECTED_ACTIVATION_GUIDANCE}\n" in result.stderr
    narration = " ".join(result.stderr.split())
    placement_at = narration.index("Append the .gitignore block")
    baseline_at = narration.index(_EXPECTED_BASELINE_GUIDANCE)
    activation_at = narration.index(_EXPECTED_ACTIVATION_GUIDANCE)
    assert placement_at < baseline_at < activation_at
    # The placement sentence stays placement-only. Appending an unconditional "then install" to
    # it is the shape that would reintroduce the ordering failure GTX-175 fixed.
    assert "pre-commit install" not in narration[placement_at:baseline_at]


def test_activation_guidance_has_one_owner():
    # init prints the module-level constant verbatim, so the narration cannot drift from it.
    assert " ".join(init_command._ACTIVATION_GUIDANCE.split()) == _EXPECTED_ACTIVATION_GUIDANCE


def test_activation_guidance_scopes_the_install_it_names():
    # Three properties the wording has to keep. Pasting the block installs no Git hook, so
    # activation is a real act rather than a restatement of pasting. An initial adoption enables
    # the gates only after the baseline, because `check` exits 1 on the unreconciled state that
    # adoption commits and gates enabled during setup would refuse it. And the same narration is
    # emitted by `--print-only`, the documented upgrade path, where a clone that is already gated
    # needs no reactivation -- so the instruction is conditioned rather than unconditional.
    guidance = " ".join(init_command._ACTIVATION_GUIDANCE.split())

    assert "stay inert" in guidance
    assert "uv tool install pre-commit" in guidance
    assert "uv tool run pre-commit install" in guidance
    assert "not already gated" in guidance
    assert "only after the baseline above" in guidance
    assert "established installation enables them immediately" in guidance


def test_init_prints_gitignore_guidance_before_other_snippets_and_preserves_existing_file(
    tmp_path: Path, monkeypatch
):
    gitignore = tmp_path / ".gitignore"
    original = b"existing bytes\r\n*.local\n"
    gitignore.write_bytes(original)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    expected = (
        "# ===== .gitignore (append these lines) =====\n"
        ".doc-lattice-reconcile.json\n"
        ".doc-lattice-reconcile.json.*.tmp\n"
        ".*.doc-lattice-before.*.tmp\n"
        ".*.doc-lattice-after.*.tmp\n"
    )
    assert expected in result.stdout
    assert result.stdout.index(expected) < result.stdout.index("# ===== .pre-commit-config.yaml")
    assert gitignore.read_bytes() == original
    assert "Append the .gitignore block" in result.stderr


def test_init_prints_gitignore_guidance_without_creating_gitignore(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert ".doc-lattice-reconcile.json.*.tmp" in result.stdout
    assert not (tmp_path / ".gitignore").exists()


def test_init_skips_existing_config_but_still_prints(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text("SENTINEL\n", encoding="utf-8")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == "SENTINEL\n"
    assert ".github/workflows/doc-lattice.yml" in result.stdout
    # GTX-212: the benign already-exists line names the file through the display spelling, the
    # same as the wrote line above it, so both sinks in this function are wrapped rather than
    # one of them riding an exemption written for the staged-file prefix beside them.
    assert "'.doc-lattice.yml' already exists, leaving it untouched" in result.stderr
    # SENTINEL is a bare scalar, not a mapping, so the pre-v7 report cannot answer and says
    # nothing. The next test is the one that pins the report itself.
    assert "lattice_format" not in result.stderr


def test_init_reports_an_existing_config_that_predates_the_lattice_format_key(
    tmp_path: Path, monkeypatch
):
    # The failure this closes: init narrating a full success on a repository where check, lint,
    # reconcile and the rest all exit 2, and naming nothing that connects the two.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots:\n  - docs\n", encoding="utf-8")

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    # Reported, never repaired: init is create-only and does not edit a config it did not write.
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == "docs_roots:\n  - docs\n"
    assert "'.doc-lattice.yml' already exists, leaving it untouched" in result.stderr
    assert "does not declare 'lattice_format: 2'" in result.stderr
    assert "CHANGELOG.md" in result.stderr


def test_init_reports_an_empty_existing_config_as_predating_the_key(tmp_path: Path, monkeypatch):
    # An empty file parses to an empty mapping, which is a config that omits the key rather than
    # one init cannot answer for, and load_config refuses it for exactly that reason.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text("", encoding="utf-8")

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == ""
    assert "does not declare 'lattice_format: 2'" in result.stderr


def test_init_says_nothing_about_an_existing_config_that_already_declares_the_key(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots:\n  - docs\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "'.doc-lattice.yml' already exists, leaving it untouched" in result.stderr
    assert "lattice_format" not in result.stderr


@pytest.mark.parametrize(
    ("label", "contents"),
    [
        ("unparseable", "docs_roots: [unclosed\n"),
        ("not-a-mapping", "- docs\n"),
    ],
)
def test_init_stays_silent_and_succeeds_on_a_config_it_cannot_answer_for(
    tmp_path: Path, monkeypatch, label: str, contents: str
):
    # init scaffolds; it is not a config gate. A config it cannot read is still a real failure,
    # but it is the one every loading command already reports with the context to act on, and
    # nothing init writes depends on the answer, so it must not raise or exit 2 here.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".doc-lattice.yml").write_text(contents, encoding="utf-8")

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, label
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == contents
    assert "'.doc-lattice.yml' already exists, leaving it untouched" in result.stderr
    assert "lattice_format" not in result.stderr


# A long name that passes the branch-name policy, so the branch record is wider than the pinned
# console on its own and the externally controlled token is unambiguously the thing that would
# hard-wrap. Both tests below use it.
_LONG_DEFAULT_BRANCH = "release/very-long-integration-branch-name-for-wrapping"


@pytest.mark.parametrize(
    ("preexisting", "config_record"),
    [
        (False, "wrote '.doc-lattice.yml'"),
        (True, "'.doc-lattice.yml' already exists, leaving it untouched"),
    ],
    ids=["wrote", "already-exists"],
)
def test_init_keeps_the_config_and_branch_records_on_one_line_at_any_width(
    tmp_path: Path, monkeypatch, preexisting: bool, config_record: str
):
    # Same one-record-per-line contract reconcile carries at
    # test_reconcile_keeps_every_record_on_one_line_at_any_width: a status record stays on one
    # physical line at any terminal width rather than hard-wrapping mid-token into a fragment.
    # Only the first two stderr lines are pinned: the placement guidance that follows is prose
    # and is meant to wrap, so asserting it here would freeze the wrong contract.
    #
    # Parametrized because the wrote and already-exists records are the `else` and `except` arms
    # of one create attempt, so no single run reaches both and each arm needs its own run.
    monkeypatch.setenv("COLUMNS", "20")
    monkeypatch.chdir(tmp_path)
    if preexisting:
        (tmp_path / ".doc-lattice.yml").write_text("SENTINEL\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--default-branch", _LONG_DEFAULT_BRANCH])

    assert result.exit_code == 0
    assert result.stderr.splitlines()[:2] == [
        config_record,
        f"workflow triggers on branch {_LONG_DEFAULT_BRANCH} (--default-branch)",
    ]


def test_init_reports_a_staging_collision_as_a_failure_not_an_existing_config(
    tmp_path: Path, monkeypatch
):
    # The benign branch used to be entered by any note-free FileExistsError, because it asked
    # whether the stage cleanup had failed rather than whether the destination existed.
    # `mkstemp` raises exactly that shape after exhausting its candidate names, so a staging
    # collision was reported to the user as an existing config and exited 0 while nothing had
    # been written. Staging runs before the link, so this is the seam that reaches the handler
    # without the destination existing at all.
    def _collide_in_staging(*_args, **_kwargs) -> Path:
        raise FileExistsError("no usable temporary file name found")

    monkeypatch.setattr(persistence, "stage_bytes", _collide_in_staging)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert "INIT_PERSISTENCE" in result.stderr
    assert "already exists, leaving it untouched" not in result.stderr
    assert not (tmp_path / ".doc-lattice.yml").exists()


def test_init_existing_config_with_stage_cleanup_failure_exits_2_and_names_orphan(
    tmp_path: Path, monkeypatch
):
    config = tmp_path / ".doc-lattice.yml"
    config.write_bytes(b"existing config bytes\n")
    cleanup_attempts: list[Path] = []

    def fail_cleanup(staged: Path) -> None:
        cleanup_attempts.append(staged)
        raise OSError("cleanup blocked")

    monkeypatch.setattr(persistence, "durable_unlink", fail_cleanup)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert config.read_bytes() == b"existing config bytes\n"
    assert len(cleanup_attempts) == 1
    orphan = cleanup_attempts[0]
    assert orphan.exists()
    # GTX-209 applied AD-34's display spelling to this shared note. The helper is reached from
    # `init`, the load cache, and reconcile alike, and a reconcile stage inherits a document
    # filename, so the spelling is settled at the one sink rather than per caller. `init`'s own
    # path is not a document path, and this assertion records that it moved with it.
    expected_note = (
        f"durable cleanup failed for helper-owned stage {format_path_for_display(orphan)}: "
        "cleanup blocked; it is not governed by a recovery journal, so inspect and remove it "
        "manually when safe"
    )
    assert expected_note in result.stderr
    # The failure is the stage cleanup, not the config file: it names the write boundary.
    assert "INIT_PERSISTENCE" in result.stderr
    assert "CONFIG_ERROR" not in result.stderr


def test_init_other_persistence_error_flattens_exception_notes(tmp_path: Path, monkeypatch):
    error = OSError("publication failed")
    error.add_note("exact orphan remediation note")

    def fail_create(*_args, **_kwargs) -> None:
        raise error

    monkeypatch.setattr(init_command, "atomic_create_bytes", fail_create)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot write '.doc-lattice.yml': publication failed" in result.stderr
    assert "exact orphan remediation note" in result.stderr
    assert "INIT_PERSISTENCE" in result.stderr
    assert "CONFIG_ERROR" not in result.stderr


def test_init_read_only_filesystem_reports_the_write_boundary_not_the_config(
    tmp_path: Path, monkeypatch
):
    # A read-only or permission-denied working directory is a problem in the directory being
    # scaffolded, so sending the user to .doc-lattice.yml with CONFIG_ERROR was the defect.
    # Injected at the atomic_create_bytes seam rather than by chmod, which root ignores and
    # which cannot represent EROFS at all.
    def fail_create(*_args, **_kwargs) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(init_command, "atomic_create_bytes", fail_create)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot write '.doc-lattice.yml'" in result.stderr
    assert "Read-only file system" in result.stderr
    assert "INIT_PERSISTENCE" in result.stderr
    assert "CONFIG_ERROR" not in result.stderr


def test_init_bakes_flag_values(tmp_path: Path, monkeypatch):
    from doc_lattice.config import load_config  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["init", "--docs-root", "design", "--docs-root", "lore", "--linear-team", "PC"]
    )
    assert result.exit_code == 0
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["design", "lore"]
    assert project.config.linear_team == "PC"


@pytest.mark.parametrize("bad", ["/etc", "../escape"])
def test_init_rejects_unsafe_docs_root(tmp_path: Path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", bad])
    assert "must be a relative path inside the project" in result.stderr
    _assert_rejected_before_any_write(result, tmp_path)


def test_init_rejects_control_character_in_flag(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # The shared empty-or-control-character site, which both flags reach. Kept as its own case so
    # the value-specific rejections around it do not become its only coverage.
    result = runner.invoke(app, ["init", "--linear-team", "a\nb"])
    assert "is empty or contains a control character" in result.stderr
    _assert_rejected_before_any_write(result, tmp_path)


def test_init_rejects_invalid_linear_team(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A lowercase, hyphenated value is not a valid Linear team key, so init must
    # refuse it rather than scaffold a config that the linear command rejects.
    result = runner.invoke(app, ["init", "--linear-team", "my-team-slug"])
    assert "must be a Linear team key" in result.stderr
    _assert_rejected_before_any_write(result, tmp_path)


def test_init_rejects_markup_metachar_in_docs_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "../[/]"])
    assert result.exception is None or isinstance(result.exception, SystemExit)
    _assert_rejected_before_any_write(result, tmp_path)


def test_init_crash_during_link_leaves_clean_state(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_link = os.link

    def boom(_src, _dst):
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", boom)
    assert runner.invoke(app, ["init"]).exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())

    monkeypatch.setattr(os, "link", real_link)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").exists()


# GTX-153. The two halves are asserted separately on purpose: `--print-only` is additive and is
# pinned against ordinary output, while the nested refusal changes what an existing zero-config
# run does and is pinned against the filesystem it declines to touch.


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Record every path under a directory, with the bytes of each regular file.

    An exit code proves nothing about a read-only path, and neither does a check of the one
    filename the command would have written: a mode that writes nothing has to be asserted
    against the whole tree, including a staged temporary it created and removed incompletely.
    Directories are recorded too, mapped to None, so a directory created or removed with no
    files in it is still a difference. An emptied one already shows up as its files vanishing.

    Args:
        root: The directory to walk.

    Returns:
        Relative POSIX paths mapped to file bytes, or to None for a directory.
    """
    return {
        entry.relative_to(root).as_posix(): None if entry.is_dir() else entry.read_bytes()
        for entry in sorted(root.rglob("*"))
    }


def test_init_print_only_prints_exactly_what_an_ordinary_run_prints(tmp_path: Path, monkeypatch):
    # Parity is the whole contract: an adopter re-fetching the pre-commit block on an upgrade
    # must get the same bytes the scaffolding run prints, or the read-only path is a second
    # source of truth. Two sibling directories inside the one fixture repository, so both runs
    # see the same absent remote and resolve the same fallback branch.
    ordinary_dir = tmp_path / "ordinary"
    printing_dir = tmp_path / "printing"
    ordinary_dir.mkdir()
    printing_dir.mkdir()

    monkeypatch.chdir(ordinary_dir)
    ordinary = runner.invoke(app, ["init"])
    monkeypatch.chdir(printing_dir)
    printed = runner.invoke(app, ["init", "--print-only"])

    assert ordinary.exit_code == 0
    assert printed.exit_code == 0
    assert printed.stdout == _legacy_stdout(__version__)
    assert printed.stdout == ordinary.stdout
    # The stderr contract is that same narration minus the one line that reports a write. The
    # branch and its source, the placement instructions, and the baseline guidance all stay.
    # The oracle is checked before it is used: if the ordinary run ever stops narrating the
    # write, `replace` becomes a no-op and this assertion quietly degenerates into
    # `printed.stderr == ordinary.stderr`, which would pass while pinning nothing.
    assert "wrote '.doc-lattice.yml'\n" in ordinary.stderr
    assert printed.stderr == ordinary.stderr.replace("wrote '.doc-lattice.yml'\n", "", 1)
    assert "wrote" not in printed.stderr
    assert "workflow triggers on branch main (fallback)\n" in printed.stderr
    assert _EXPECTED_BASELINE_GUIDANCE in " ".join(printed.stderr.split())
    # The upgrade path emits the activation guidance too, which is why it is conditioned on the
    # clone not already being gated rather than telling every re-fetch to reinstall the hook.
    assert _EXPECTED_ACTIVATION_GUIDANCE in " ".join(printed.stderr.split())
    # Emitted from `_print_artifacts`, the one site both modes reach, so parity holds by
    # construction rather than by two call sites being kept in step.
    assert _EXPECTED_GIT_PRECONDITION_GUIDANCE in " ".join(printed.stderr.split())


def test_init_print_only_leaves_the_directory_byte_identical(tmp_path: Path, monkeypatch):
    before = _snapshot(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--print-only"])

    assert result.exit_code == 0
    assert _snapshot(tmp_path) == before


def test_init_print_only_never_reaches_the_persistence_boundary(tmp_path: Path, monkeypatch):
    # The filesystem assertion above cannot distinguish a write that was attempted and undone
    # from one that never happened, and only the second is what this mode promises.
    #
    # Asserted at the effect rather than at the name. Patching `init_command.atomic_create_bytes`
    # alone would pin only "does not call that module global", which a rebind to
    # `persistence.atomic_create_bytes(...)` makes vacuous while staying green forever, and which
    # a write-then-unlink inside the branch satisfies. `os.link` is where a staged file becomes a
    # real one, so denying it is the same technique
    # `test_init_crash_during_link_leaves_clean_state` already uses.
    def refuse(*_args, **_kwargs) -> None:
        raise AssertionError("--print-only must not reach the write boundary")

    monkeypatch.setattr(init_command, "atomic_create_bytes", refuse)
    monkeypatch.setattr(os, "link", refuse)
    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--print-only"])

    assert result.exit_code == 0
    assert result.stdout == _legacy_stdout(__version__)


def test_init_print_only_honors_the_default_branch_flag(tmp_path: Path, monkeypatch):
    # The one flag that still has meaning in this mode, so an upgrade can print the workflow it
    # will actually commit rather than one resolved against the checkout it happened to run in.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--print-only", "--default-branch", "trunk"])

    assert result.exit_code == 0
    assert "    branches: [trunk]\n" in result.stdout
    assert "workflow triggers on branch trunk (--default-branch)\n" in result.stderr


def test_init_print_only_rejects_a_branch_outside_the_supported_domain(tmp_path: Path, monkeypatch):
    # Printing is not a reason to relax the branch policy: a pattern in the filter produces a
    # workflow that silently gates the wrong pushes, whether or not a config was written.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--print-only", "--default-branch", "release/*"])

    assert "must be an ASCII Git branch name" in result.stderr
    _assert_rejected_before_any_write(result, tmp_path)


@pytest.mark.parametrize(
    "flags",
    [
        ["--docs-root", "design"],
        ["--linear-team", "ENG"],
        ["--docs-root", "design", "--linear-team", "ENG"],
    ],
    ids=["docs-root", "linear-team", "both"],
)
def test_init_print_only_refuses_the_config_only_flags(tmp_path: Path, monkeypatch, flags):
    # Both flags feed only the config renderer, and this mode renders no config, so accepting
    # them would report success for a request nothing acted on. It is a flag combination with no
    # meaning rather than a value that failed validation, so it stays uncoded, the way
    # `reconcile --recover` with a selector does. README's error-code table owns that rule.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--print-only", *flags])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == (
        "error: --print-only cannot be combined with --docs-root or --linear-team\n"
    )
    assert "VALIDATION_ERROR" not in result.stderr
    assert {path.name for path in tmp_path.iterdir()} == {".git"}


def test_init_print_only_succeeds_where_an_ordinary_run_is_refused(tmp_path: Path, monkeypatch):
    # The situation the mode exists for: an adopter in a subdirectory of a configured repository
    # can still obtain the snippets, and gets exactly the same bytes.
    (tmp_path / ".doc-lattice.yml").write_text("SENTINEL\n", encoding="utf-8")
    nested = tmp_path / "docs" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init", "--print-only"])

    assert result.exit_code == 0
    assert result.stdout == _legacy_stdout(__version__)
    assert not (nested / ".doc-lattice.yml").exists()
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == "SENTINEL\n"


def test_init_refuses_to_scaffold_beneath_an_ancestor_config(tmp_path: Path, monkeypatch):
    # GTX-153's behavior change. This run used to exit 0 having written a second, nested config
    # with default settings that no run from the configured root would ever select, because every
    # lattice-loading command resolves its default config against its own current directory and
    # never walks up. That is also why the file is not merely inert: a command launched from this
    # same subdirectory would load it, which makes it a silently divergent second lattice.
    root_config = tmp_path / ".doc-lattice.yml"
    root_config.write_text("SENTINEL\n", encoding="utf-8")
    nested = tmp_path / "docs" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error (VALIDATION_ERROR): ")
    assert "CONFIG_ERROR" not in result.stderr
    # The diagnostic has to name the config it found, or the user cannot tell which directory to
    # run in, and has to name both ways forward from where they are standing.
    assert format_path_for_display(root_config) in result.stderr
    assert "--print-only" in result.stderr
    assert ".doc-lattice.yml here by hand" in result.stderr
    assert root_config.read_text(encoding="utf-8") == "SENTINEL\n"
    assert list(nested.iterdir()) == []


def test_init_retains_current_directory_behavior_in_a_nested_directory(tmp_path: Path, monkeypatch):
    # The other half of the pinned behavior: without an ancestor config there is nothing to
    # collide with, so a subdirectory scaffolds exactly as it always did.
    nested = tmp_path / "docs" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (nested / ".doc-lattice.yml").is_file()
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert "wrote '.doc-lattice.yml'" in result.stderr


def test_init_still_reports_an_existing_config_here(tmp_path: Path, monkeypatch):
    # The guard runs only when the target is absent, so the benign already-exists report is
    # unchanged even at a root that is itself beneath one -- the directory the diagnostic above
    # tells the user to move to must not then refuse them.
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".doc-lattice.yml").write_text("OUTER\n", encoding="utf-8")
    (inner / ".doc-lattice.yml").write_text("INNER\n", encoding="utf-8")
    monkeypatch.chdir(inner)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "'.doc-lattice.yml' already exists, leaving it untouched" in result.stderr
    assert (inner / ".doc-lattice.yml").read_text(encoding="utf-8") == "INNER\n"


def test_init_ignores_a_config_outside_the_repository_boundary(tmp_path: Path, monkeypatch):
    # The bound that keeps an unrelated config from blocking a new project: the invocation
    # directory is itself a repository root, so nothing above it is in scope at all.
    (tmp_path / ".doc-lattice.yml").write_text("OUTSIDE\n", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".git").mkdir()
    monkeypatch.chdir(inner)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (inner / ".doc-lattice.yml").is_file()
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == "OUTSIDE\n"


def test_init_scaffolds_inside_a_submodule_beneath_a_configured_root(tmp_path: Path, monkeypatch):
    # A submodule and a linked worktree both record `.git` as a regular file, so a marker test
    # spelled `is_dir()` would walk straight past this root and refuse a legitimate scaffold.
    (tmp_path / ".doc-lattice.yml").write_text("OUTER\n", encoding="utf-8")
    submodule = tmp_path / "vendor" / "library"
    submodule.mkdir(parents=True)
    (submodule / ".git").write_text("gitdir: ../../.git/modules/library\n", encoding="utf-8")
    nested = submodule / "docs"
    nested.mkdir()
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (nested / ".doc-lattice.yml").is_file()


def test_init_finds_the_nearest_ancestor_config_below_the_boundary(tmp_path: Path, monkeypatch):
    # Two configs in scope: the diagnostic must name the closer one, since that is the directory
    # whose lattice the user is standing inside.
    middle = tmp_path / "middle"
    nested = middle / "nested"
    nested.mkdir(parents=True)
    (tmp_path / ".doc-lattice.yml").write_text("ROOT\n", encoding="utf-8")
    (middle / ".doc-lattice.yml").write_text("MIDDLE\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert format_path_for_display(middle / ".doc-lattice.yml") in result.stderr
    assert format_path_for_display(tmp_path / ".doc-lattice.yml") not in result.stderr


def test_ancestor_walk_yields_nothing_without_a_repository_boundary(tmp_path: Path, monkeypatch):
    # Asserted at the unit rather than through the CLI, because the fixture repository puts a
    # marker above every temporary directory and the case under test is its absence. Renaming
    # the marker makes the walk reach the filesystem root, which is what an unbounded scan would
    # do on a project created under a home directory that happens to hold a stray config. The
    # answer must be None regardless of what any real ancestor holds.
    monkeypatch.setattr(init_command, "_REPOSITORY_MARKER", ".doc-lattice-no-such-marker")
    outer = tmp_path / "outer"
    nested = outer / "project"
    nested.mkdir(parents=True)
    (outer / ".doc-lattice.yml").write_text("STRAY\n", encoding="utf-8")

    assert init_command._find_ancestor_config(nested) is None


# GTX-153 review follow-up. The walk's predicates and its behavior when the filesystem cannot
# answer, which the original change left to `Path.exists()` and therefore left interpreter-
# dependent. Every test below fails on at least one supported interpreter without the fix.


def _stat_denying(target: Path):
    """Build a `Path.stat` that refuses one exact path and delegates every other.

    Patching `Path.stat` wholesale would break the Git probe and Typer alike, and patching
    `Path.exists` would test the very call the fix removes. Denying one path is also what makes
    these tests interpreter-independent: they assert the decision the command makes, not which
    of the two behaviors the standard library happened to supply.

    Args:
        target: The path whose stat raises EACCES.

    Returns:
        A replacement for `Path.stat`.
    """
    real = Path.stat

    def fake(self: Path, *args, **kwargs):
        if self == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    return fake


def test_init_refuses_when_an_ancestor_config_cannot_be_read(tmp_path: Path, monkeypatch):
    # The blocking defect. `Path.exists()` raises PermissionError on 3.13 and answers False on
    # 3.14, so this run used to crash with an uncoded `internal error` on one interpreter and,
    # on the other, silently write the nested config the guard exists to prevent while printing
    # `wrote`. A guard that guesses is not a guard: an unreadable entry is a coded refusal.
    ancestor = tmp_path / ".doc-lattice.yml"
    ancestor.write_text("SENTINEL\n", encoding="utf-8")
    nested = tmp_path / "docs" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setattr(Path, "stat", _stat_denying(ancestor))

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error (INIT_PERSISTENCE): ")
    assert "internal error" not in result.stderr
    assert format_path_for_display(ancestor) in result.stderr
    assert "--print-only" in result.stderr
    assert not (nested / ".doc-lattice.yml").exists()
    assert ancestor.read_text(encoding="utf-8") == "SENTINEL\n"


def test_init_refuses_when_the_repository_marker_cannot_be_read(tmp_path: Path, monkeypatch):
    # The same divergence on the other entry the walk reads. An unreadable marker is worse than
    # an unreadable config, because it decides where the walk stops rather than what it found.
    marker = tmp_path / ".git"
    nested = tmp_path / "docs" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setattr(Path, "stat", _stat_denying(marker))

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stderr.startswith("error (INIT_PERSISTENCE): ")
    assert "internal error" not in result.stderr
    assert format_path_for_display(marker) in result.stderr
    assert not (nested / ".doc-lattice.yml").exists()


def test_init_does_not_let_a_directory_named_like_the_config_skip_the_guard(
    tmp_path: Path, monkeypatch
):
    # `target.exists()` answered True for a directory of that name, so the guard never ran and
    # the run reported `already exists, leaving it untouched` while nothing was configured here
    # and the ancestor governed. A directory configures nothing.
    ancestor = tmp_path / ".doc-lattice.yml"
    ancestor.write_text("SENTINEL\n", encoding="utf-8")
    nested = tmp_path / "docs" / "deep"
    nested.mkdir(parents=True)
    (nested / ".doc-lattice.yml").mkdir()
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stderr.startswith("error (VALIDATION_ERROR): ")
    assert "already exists" not in result.stderr
    assert format_path_for_display(ancestor) in result.stderr


def test_init_ignores_an_ancestor_directory_named_like_the_config(tmp_path: Path, monkeypatch):
    # The same predicate on the other side of the walk: a directory of that name above the
    # invocation directory must not refuse a legitimate scaffold on the strength of its name.
    (tmp_path / ".doc-lattice.yml").mkdir()
    nested = tmp_path / "docs" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (nested / ".doc-lattice.yml").is_file()
    assert "wrote '.doc-lattice.yml'" in result.stderr


def test_init_treats_a_dangling_repository_marker_as_a_boundary(tmp_path: Path, monkeypatch):
    # A symlinked `.git` is what a relocated worktree leaves behind, and Git still recognizes the
    # root when the link's target has gone. `exists()` follows the link and answered False, so
    # the walk went straight past this root and refused on a config outside it.
    (tmp_path / ".doc-lattice.yml").write_text("OUTSIDE\n", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".git").symlink_to(tmp_path / "no-such-gitdir")
    monkeypatch.chdir(inner)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (inner / ".doc-lattice.yml").is_file()
    assert (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8") == "OUTSIDE\n"


def test_init_print_only_prints_over_an_existing_config_without_touching_it(
    tmp_path: Path, monkeypatch
):
    # The headline upgrade case, and the one every other print-only test misses: the adopter
    # re-fetching the pre-commit block is standing in their own configured repository root. The
    # already-exists narration must stay absent, because nothing was attempted.
    config = tmp_path / ".doc-lattice.yml"
    config.write_text("SENTINEL\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--print-only"])

    assert result.exit_code == 0
    assert result.stdout == _legacy_stdout(__version__)
    assert config.read_text(encoding="utf-8") == "SENTINEL\n"
    assert "already exists" not in result.stderr
    assert "wrote" not in result.stderr


def _written_config(tmp_path: Path) -> str:
    return (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8")


def test_init_derives_link_sources_from_the_default_docs_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert "link_sources:\n  - docs/**/*.md\n" in _written_config(tmp_path)


def test_init_spells_an_existing_file_root_as_itself(tmp_path: Path, monkeypatch):
    (tmp_path / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "SPEC.md"]).exit_code == 0
    assert "link_sources:\n  - SPEC.md\n" in _written_config(tmp_path)


def test_init_uses_the_directory_form_for_a_root_that_does_not_exist_yet(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "./design/"]).exit_code == 0
    assert "link_sources:\n  - design/**/*.md\n" in _written_config(tmp_path)


def test_init_escapes_a_metacharacter_in_a_root(tmp_path: Path, monkeypatch):
    (tmp_path / "notes [draft]").mkdir()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "notes [draft]"]).exit_code == 0
    assert "link_sources:\n  - notes [[]draft]/**/*.md\n" in _written_config(tmp_path)


def test_init_derives_the_selector_from_a_symlinked_roots_resolved_path(
    tmp_path: Path, monkeypatch
):
    # The checker never enters a symlinked directory, so a selector written over the link
    # would fail on every run; the resolved, contained path is what is written.
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "linked"]).exit_code == 0
    assert "link_sources:\n  - real/**/*.md\n" in _written_config(tmp_path)


def test_init_rejects_a_root_that_resolves_outside_the_project(tmp_path: Path, monkeypatch):
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (tmp_path / "away").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "away"])
    # Not `_assert_rejected_before_any_write`: that helper asserts the directory holds only
    # `.git`, and this case has to create the escaping symlink to have something to reject.
    # The write is still what is being ruled out, so the config's absence is asserted directly.
    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert "resolves outside" in result.stderr


def test_init_rejects_a_root_with_a_backslash(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "docs\\guides"])
    _assert_rejected_before_any_write(result, tmp_path)
    assert "backslash" in result.stderr


def test_init_rejects_a_root_whose_derived_selector_the_grammar_rejects(
    tmp_path: Path, monkeypatch
):
    # A drive prefix is not absolute on POSIX, so the flag-level relative-path check passes it
    # through. The derived selector is one the config loader refuses, so writing it would leave
    # a project whose every lattice-loading command exits 2 after an init that exited 0.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "C:foo"])
    _assert_rejected_before_any_write(result, tmp_path)
    assert "C:foo" in result.stderr
    assert "is absolute" in result.stderr


def test_init_rejects_an_existing_directory_whose_selector_the_grammar_rejects(
    tmp_path: Path, monkeypatch
):
    # The same defect reached through the resolved-path branch rather than the literal one.
    (tmp_path / "C:foo").mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "C:foo"])
    # Not `_assert_rejected_before_any_write`: the root has to exist for this branch to run, so
    # the directory holds more than `.git`. The write is still what is ruled out.
    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert "C:foo" in result.stderr
    assert "is absolute" in result.stderr
