"""Contract tests for release and PyPI publishing automation.

These assert which action each release step calls, not the commit it is pinned to.
`tests/test_workflow_pinning.py` owns the supply-chain rule that every `uses:` in every
workflow resolves to a 40-character commit SHA, so restating individual pins here would
only force a lockstep edit on every routine pin refresh.
"""

import shlex
import tomllib
from pathlib import Path

from ruamel.yaml import YAML

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_TEXT = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
_WORKFLOW = YAML(typ="safe").load(_WORKFLOW_TEXT)
_CHECKOUT = "actions/checkout"
_UPLOAD_ARTIFACT = "actions/upload-artifact"
_DOWNLOAD_ARTIFACT = "actions/download-artifact"
_PYPI_PUBLISH = "pypa/gh-action-pypi-publish"
_ARTIFACT_NAME = "release-distributions"
_PROCEED = "steps.gate.outputs.proceed == 'true'"
_TAG_STEP = "Create and push the tag"
_SMOKE_FIXTURE = "tests/fixtures/release-smoke/.doc-lattice.yml"
_PACKAGE_URL = "git+https://github.com/Guardantix/doc-lattice"
# The release steps that verify the source and the published artifacts. None of them produces a
# job output, so deleting one leaves every other job green and the release still publishes; these
# names are the only thing standing between a silent deletion and an unverified release.
_PROTECTED_STEPS = frozenset(
    {"Re-assert version sync", "Smoke-test the commit", "Confirm pinned ref resolves"}
)


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _step_index(job: dict, name: str) -> int:
    """Return a named step's position, which is the order the runner executes it in."""
    return next(index for index, step in enumerate(job["steps"]) if step.get("name") == name)


def _action(step: dict) -> str:
    """Return a step's action reference with its pin stripped, or "" if it runs a script."""
    return step.get("uses", "").split("@", 1)[0]


def _uncommented(line: str) -> str:
    """Return a command line with any shell comment removed, ignoring `#` inside quotes."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line


def _commands(step: dict) -> str:
    """Return a step's executable command lines, dropping blanks and commented-out text.

    Comments are stripped because the shell ignores them: a step reading `uv build --wheel
    # --sdist` builds only a wheel, and leaving the comment in place would let it satisfy
    assertions about the arguments the step actually passes.
    """
    return "\n".join(
        stripped
        for line in step.get("run", "").splitlines()
        if (stripped := _uncommented(line.strip()))
    )


def _invocations(text: str) -> list[list[str]]:
    """Return the argument list of every command in shell text.

    A line may chain several commands, so read each one separately: `rm -rf dist && uv build`
    still builds, while the arguments in `uv build --wheel && echo --sdist` belong to two
    different programs and only the build's own arguments describe what it produces.
    """
    argvs = []
    for line in text.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        current: list[str] = []
        for token in lexer:
            if set(token) <= {"&", "|", ";"}:
                argvs.append(current)
                current = []
            else:
                current.append(token)
        argvs.append(current)
    return [argv for argv in argvs if argv]


def _out_dir(argv: list[str]) -> str | None:
    """Return a command's output-directory value, or None when the flag is absent.

    `uv build` documents `-o` and `--out-dir` as equivalent, so both forms count, as do the
    attached spellings a switch to the short option would introduce.
    """
    for index, word in enumerate(argv):
        if word in {"--out-dir", "-o"}:
            return argv[index + 1] if index + 1 < len(argv) else ""
        if word.startswith("--out-dir="):
            return word.split("=", 1)[1]
        if word.startswith("-o") and not word.startswith("--"):
            return word[2:].removeprefix("=")
    return None


def _flag_value(argv: list[str], flag: str) -> str | None:
    """Return the value a command passes to `flag`, or None when the flag is absent.

    Both the separated and the attached spelling count, since `--notes-file notes.md` and
    `--notes-file=notes.md` name the same file to every program that parses either.
    """
    for index, word in enumerate(argv):
        if word == flag:
            return argv[index + 1] if index + 1 < len(argv) else ""
        if word.startswith(f"{flag}="):
            return word.split("=", 1)[1]
    return None


def _redirect_target(text: str) -> str | None:
    """Return the path shell text redirects stdout to, or None when nothing is redirected.

    The extractor writes its notes to stdout, so the redirect target is the only record of
    which file the later publish step has to read back.
    """
    for argv in _invocations(text):
        for index, word in enumerate(argv):
            if word == ">":
                return argv[index + 1] if index + 1 < len(argv) else ""
    return None


def _runs_subcommand(argv: list[str], program: str, subcommand: str) -> bool:
    """Report whether a command runs `program` with `subcommand` as its first word.

    The program is matched anywhere in the line because the release steps reach their tools
    through runners: `uvx --from "${REF}" doc-lattice check` runs the packaged CLI's `check`
    just as `doc-lattice check` would, and the runner's own flags precede it.
    """
    return any(
        word == program and argv[index + 1 : index + 2] == [subcommand]
        for index, word in enumerate(argv)
    )


def _fetches_tags(argv: list[str]) -> bool:
    """Report whether a command fetches tags and overwrites the local refs.

    Both flags are required but their order is not, and unrelated flags are the maintainer's
    business, so match on the arguments rather than on how the line happens to read.
    """
    return argv[:2] == ["git", "fetch"] and {"--tags", "--force"} <= set(argv)


def _runs_twine_check(argv: list[str]) -> bool:
    """Report whether a command runs twine's `check` subcommand."""
    return _runs_subcommand(argv, "twine", "check")


def _invokes(argv: list[str], script: str) -> bool:
    """Report whether a command runs `script` rather than only naming it.

    `-c` and `-m` make the interpreter execute inline code or a module instead, which demotes
    any path on the line to an ordinary argument the gate never runs.
    """
    if not argv or argv[0] not in {"uv", "uvx", "python", "python3"}:
        return False
    return script in argv[1:] and set(argv).isdisjoint({"-c", "-m"})


def _dev_dependencies() -> str:
    """Return the project's declared dev dependencies as one searchable string."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project.get("dependency-groups", {}))


def test_release_exposes_publish_coordination_outputs():
    release = _WORKFLOW["jobs"]["release"]
    assert release["permissions"] == {"contents": "write"}
    assert release["outputs"] == {
        "proceed": "${{ steps.gate.outputs.proceed }}",
        "create_tag": "${{ steps.gate.outputs.create_tag }}",
        "version": "${{ steps.target.outputs.version }}",
        "tag": "${{ steps.target.outputs.tag }}",
    }


def test_release_gate_invokes_testable_script_with_runner_environment():
    steps = _WORKFLOW["jobs"]["release"]["steps"]
    gate_index = next(i for i, step in enumerate(steps) if step.get("name") == "Tag-health gate")
    assert steps[gate_index]["env"] == {
        "GITHUB_BEFORE": "${{ github.event.before }}",
        "TAG": "${{ steps.target.outputs.tag }}",
        "VERSION": "${{ steps.target.outputs.version }}",
    }
    # release_gate.py resolves refs/tags/<tag> from local state, so a tag created remotely
    # since checkout is invisible unless the fetch runs first. Steps in a job run in order,
    # so the fetch may live in the gate step or in any step before it. Require both to occupy
    # the executable position of a command line: a step that only prints the script name never
    # writes the gate outputs, so build and publish would skip while the tests stayed green.
    ordered = [argv for step in steps[: gate_index + 1] for argv in _invocations(_commands(step))]
    fetches = [i for i, argv in enumerate(ordered) if _fetches_tags(argv)]
    gates = [i for i, argv in enumerate(ordered) if _invokes(argv, "scripts/release_gate.py")]
    assert fetches
    assert gates
    assert fetches[0] < gates[0]


def test_release_notes_are_validated_before_the_tag_is_pushed():
    # A tag is immutable, so the changelog check has to fail while there is still nothing to
    # strand. Running it inside "Publish release notes" left an empty section tagging the commit
    # and then failing, which needs a hand-cut version to escape.
    release = _WORKFLOW["jobs"]["release"]
    extract = _named_step(release, "Extract release notes")
    assert extract["if"] == _PROCEED
    assert extract["env"]["VERSION"] == "${{ steps.target.outputs.version }}"
    # Require an actual run of the extractor. A step that only names the path exits 0 over an
    # empty section, which is the failure this whole reordering exists to prevent.
    extractions = [
        argv
        for argv in _invocations(_commands(extract))
        if _invokes(argv, "scripts/extract_release_notes.py")
    ]
    assert extractions
    # Without the version the extractor fails on argparse alone, which would fail every release
    # rather than only the ones whose section is missing or empty.
    assert "${VERSION}" in extractions[0]
    assert _step_index(release, "Extract release notes") < _step_index(release, _TAG_STEP)


def test_published_notes_come_from_the_file_extraction_wrote():
    # Splitting generation from publication only holds if both halves name the same file. A
    # publish step reading some other path would post whatever that path happened to contain.
    release = _WORKFLOW["jobs"]["release"]
    written = _redirect_target(_commands(_named_step(release, "Extract release notes")))
    assert written
    publish = _invocations(_commands(_named_step(release, "Publish release notes")))
    creates = [argv for argv in publish if argv[:3] == ["gh", "release", "create"]]
    assert creates
    for argv in creates:
        assert _flag_value(argv, "--notes-file") == written
    # Publication must not quietly regain its own extraction: a second copy running after the tag
    # would restore the very ordering this contract removes.
    assert not [argv for argv in publish if _invokes(argv, "scripts/extract_release_notes.py")]


def test_release_job_keeps_every_protected_step_under_the_gate():
    # Each of these verifies something no other job repeats, and none of them feeds a job output,
    # so deleting one produces a fully green run that published an unverified release.
    release = _WORKFLOW["jobs"]["release"]
    present = {step.get("name") for step in release["steps"]}
    assert _PROTECTED_STEPS.issubset(present)
    for name in sorted(_PROTECTED_STEPS):
        assert _named_step(release, name)["if"] == _PROCEED


def test_version_sync_is_re_asserted_against_the_release_source():
    # The same guard runs in code-quality, but against the pull request's merge commit. This is
    # the only run against the commit that actually becomes the tag.
    step = _named_step(_WORKFLOW["jobs"]["release"], "Re-assert version sync")
    assert any(
        _invokes(argv, "scripts/check_version_sync.py") for argv in _invocations(_commands(step))
    )


def test_smoke_step_runs_the_packaged_cli_against_the_release_fixture():
    # This is the pre-tag execution of the packaged CLI, installed from the release source rather
    # than from the working tree, so a packaging break shows up before anything immutable exists.
    release = _WORKFLOW["jobs"]["release"]
    step = _named_step(release, "Smoke-test the commit")
    assert step["env"]["REF"] == f"{_PACKAGE_URL}@${{{{ github.sha }}}}"
    assert step["env"]["FIXTURE"] == _SMOKE_FIXTURE
    assert (_ROOT / _SMOKE_FIXTURE).is_file()
    argvs = _invocations(_commands(step))
    for subcommand in ("check", "lint"):
        runs = [argv for argv in argvs if _runs_subcommand(argv, "doc-lattice", subcommand)]
        assert runs
        for argv in runs:
            # Reading the fixture off the step's own environment keeps the assertion on what the
            # command is handed, so pointing FIXTURE at a passing stub fails here too.
            assert _flag_value(argv, "--config") == "${FIXTURE}"
            assert _flag_value(argv, "--from") == "${REF}"
    assert _step_index(release, "Smoke-test the commit") < _step_index(release, _TAG_STEP)


def test_pinned_ref_confirmation_resolves_the_tag_that_was_pushed():
    # The only check that the published tag is installable as users will install it. Running it
    # against the SHA instead would pass even when the tag never reached the remote.
    release = _WORKFLOW["jobs"]["release"]
    step = _named_step(release, "Confirm pinned ref resolves")
    assert step["env"]["TAG"] == "${{ steps.target.outputs.tag }}"
    pinned = [
        argv
        for argv in _invocations(_commands(step))
        if _flag_value(argv, "--from") == f"{_PACKAGE_URL}@${{TAG}}"
    ]
    assert pinned
    assert any("doc-lattice" in argv for argv in pinned)
    assert _step_index(release, _TAG_STEP) < _step_index(release, "Confirm pinned ref resolves")


def test_tag_creation_and_github_release_are_idempotent():
    release = _WORKFLOW["jobs"]["release"]
    create_tag = _named_step(release, "Create and push the tag")
    assert create_tag["if"] == "steps.gate.outputs.create_tag == 'true'"
    notes = _named_step(release, "Publish release notes")["run"]
    assert 'gh release view "${TAG}"' in notes
    assert 'gh release create "${TAG}"' in notes


def test_build_job_uses_exact_tag_without_oidc():
    build = _WORKFLOW["jobs"]["build-release"]
    assert build["needs"] == "release"
    assert build["if"] == "needs.release.outputs.proceed == 'true'"
    assert build["permissions"] == {"contents": "read"}
    assert "id-token" not in build["permissions"]
    checkout = build["steps"][0]
    assert _action(checkout) == _CHECKOUT
    assert checkout["with"]["ref"] == "${{ needs.release.outputs.tag }}"


def test_build_job_builds_validates_and_uploads_one_artifact():
    build = _WORKFLOW["jobs"]["build-release"]
    # RELEASING.md requires publishing a wheel and a source distribution and validating both.
    # Both commands cover both formats when given no format argument, so naming one format is
    # only acceptable when the other is named too, as RELEASING.md's own invocations do.
    # Read the arguments off the build command itself. A step may legitimately run other
    # commands, and their flags say nothing about how the distributions get built.
    build_run = _commands(_named_step(build, "Build distributions"))
    builds = [argv for argv in _invocations(build_run) if argv[:2] == ["uv", "build"]]
    assert builds
    for argv in builds:
        assert ("--wheel" in argv) == ("--sdist" in argv)
        # The upload step below publishes `dist/` with `if-no-files-found: error`, so the build
        # has to write there. Compare the whole argument: a prefix check accepts `--out-dir
        # dist-old` and strands every distribution outside the uploaded directory.
        assert _out_dir(argv) in (None, "dist")
    validate = _named_step(build, "Validate distributions")
    checks = [argv for argv in _invocations(_commands(validate)) if _runs_twine_check(argv)]
    assert checks
    checked = " ".join(checks[0])
    assert (".whl" in checked) == (".tar.gz" in checked)
    # twine is not preinstalled on the runner, so `twine check` only resolves if the job supplies
    # it. Naming twine inline (`--from`/`--with`) works, as does installing it in an earlier step,
    # as does declaring it in the dev group and reaching it through `uv run`. A bare invocation,
    # or `uv run twine` while the dev group omits twine, fails on a clean runner.
    earlier = build["steps"][: build["steps"].index(validate)]
    installed_earlier = any(
        "install" in argv and "twine" in argv
        for step in earlier
        for argv in _invocations(_commands(step))
    )
    pairs = set(zip(checks[0], checks[0][1:], strict=False))
    supplied_inline = bool(pairs & {("--from", "twine"), ("--with", "twine")}) or bool(
        {"--from=twine", "--with=twine"} & set(checks[0])
    )
    from_dev_group = "twine" in _dev_dependencies() and checks[0][:2] == ["uv", "run"]
    assert supplied_inline or from_dev_group or installed_earlier
    upload = _named_step(build, "Upload distributions")
    assert sum(_action(step) == _UPLOAD_ARTIFACT for step in build["steps"]) == 1
    assert _action(upload) == _UPLOAD_ARTIFACT
    assert upload["with"] == {
        "name": _ARTIFACT_NAME,
        "path": "dist/",
        "if-no-files-found": "error",
    }


def test_publish_job_is_oidc_only_and_waits_for_build():
    publish = _WORKFLOW["jobs"]["publish"]
    assert publish["needs"] == ["release", "build-release"]
    assert publish["if"] == "needs.release.outputs.proceed == 'true'"
    assert publish["environment"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}


def test_publish_job_only_downloads_and_publishes_pinned_artifact():
    publish = _WORKFLOW["jobs"]["publish"]
    assert len(publish["steps"]) == 2
    download, upload = publish["steps"]
    assert download["name"] == "Download distributions"
    assert _action(download) == _DOWNLOAD_ARTIFACT
    assert download["with"] == {"name": _ARTIFACT_NAME, "path": "dist/"}
    assert upload["name"] == "Publish distributions to PyPI"
    assert _action(upload) == _PYPI_PUBLISH
    assert upload["with"]["skip-existing"] is True
    assert all("run" not in step for step in publish["steps"])
