"""Contract tests for release and PyPI publishing automation.

These assert which action each release step calls, not the commit it is pinned to.
`tests/test_workflow_pinning.py` owns the supply-chain rule that every `uses:` in every
workflow resolves to a 40-character commit SHA, so restating individual pins here would
only force a lockstep edit on every routine pin refresh.
"""

import itertools
import re
import shlex
import tomllib
from collections.abc import Iterator
from pathlib import Path

from ruamel.yaml import YAML
from workflow_helpers import _commands, _invocations, _invokes, _named_step, _uncommented

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
# `name=value`, which `_invocations` hands back as one word. The value half is re-read rather
# than matched as a string, so a command reached through flags or extra spacing still counts.
_ASSIGNMENT = re.compile(r"^(\w+)=(.*)$")
_COMMAND_SUBSTITUTION = re.compile(r"^\$\((.*)\)$")
# The two operators that run what follows in the same shell and only on the left side
# succeeding, which is what carries a `cd` to the commands after it.
_SEQUENCING = frozenset({"&&", ";"})
# Operators that keep the success path running once the `cd` has already reached. `||` drops
# off it -- it runs its right side only when the left one failed -- and `&` backgrounds
# everything to its left, the `cd` with it.
_CONTINUES = frozenset({"&&", ";", "|"})
# Redirections, which make a command's output go somewhere other than the substitution reading
# it. `$(mktemp -d >/dev/null)` expands to nothing at all.
_REDIRECTIONS = frozenset({">", ">>", "<", "<<"})
# Programs that run another program rather than being the work themselves. A release step
# reaches the packaged CLI through one of these, so the CLI's own name is not argv[0].
_LAUNCHERS = frozenset({"uv", "uvx", "python", "python3"})
# The release steps that verify the source and the published artifacts. None of them produces a
# job output, so deleting one leaves every other job green and the release still publishes; these
# names are the only thing standing between a silent deletion and an unverified release.
_PROTECTED_STEPS = frozenset(
    {"Re-assert version sync", "Smoke-test the commit", "Confirm pinned ref resolves"}
)
# GTX-176: the jobs `main` requires in place of the per-leg contexts GitHub generates from a
# matrix. Each maps its job id to the fixed context name it reports and the matrix job it
# summarizes. Branch protection names the middle value, so it is the one that cannot drift.
_AGGREGATORS = {
    "code-quality-result": ("Code quality", "code-quality"),
    "tests-result": ("Tests", "tests"),
    "yaml-compatibility-result": ("YAML parser compatibility", "yaml-compatibility"),
}


def _step_index(job: dict, name: str) -> int:
    """Return a named step's position, which is the order the runner executes it in."""
    return next(index for index, step in enumerate(job["steps"]) if step.get("name") == name)


def _action(step: dict) -> str:
    """Return a step's action reference with its pin stripped, or "" if it runs a script."""
    return step.get("uses", "").split("@", 1)[0]


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


def _expands(word: str, name: str) -> bool:
    """Report whether a word expands the shell variable `name`, in either spelling."""
    return word in {f"${name}", f"${{{name}}}"}


def _makes_throwaway_dir(value: str) -> bool:
    """Report whether an assigned value is the path `mktemp -d` printed.

    The whole value has to be one `mktemp -d` and nothing else. What a substitution expands to
    is what its *last* command printed, so finding a qualifying `mktemp` somewhere inside it
    proves nothing: `$(mktemp -d >/dev/null; printf '%s' "${GITHUB_WORKSPACE}")` runs one and
    still expands to the checkout. Rather than work out what a compound substitution evaluates
    to, this recognizes the one shape it can vouch for and refuses every other -- a redirection
    included, since `$(mktemp -d >/dev/null)` expands to nothing.
    """
    substitution = _COMMAND_SUBSTITUTION.match(value)
    if substitution is None:
        return False
    inner = _invocations(substitution.group(1))
    return (
        len(inner) == 1
        and inner[0][:1] == ["mktemp"]
        and "-d" in inner[0]
        and _REDIRECTIONS.isdisjoint(inner[0])
    )


def _retarget(words: list[str], workdirs: dict[str, int], shell: int) -> None:
    """Update, in place, which variables currently hold a freshly created temporary directory.

    Assignments are applied in command order rather than collected over the whole step, so a
    variable reassigned to a checkout path stops counting from that command onward. Collecting
    them timelessly would keep `workdir` classified as a throwaway through
    `workdir="$(mktemp -d)"; workdir="${GITHUB_WORKSPACE}"; cd "${workdir}"`, which lands in
    the checkout.

    Only a directory `mktemp` made itself qualifies. `mktemp` without `-d` creates a file, and
    a literal path would name somewhere in the checkout, which is the thing a throwaway workdir
    exists not to be -- so any other assignment drops the name.

    Only a command that is *nothing but* assignments counts, which is the form whose effect
    outlives the command. A word that merely looks like one is not one: `echo 'D=$(mktemp -d)'`
    prints text and assigns nothing, and `D=… cmd` sets `D` in that one command's environment.
    Reading every word would let either declare the checkout a throwaway directory.

    Each name is recorded against the shell instance that assigned it, because an assignment
    dies with the subshell that made it: `( D="$(mktemp -d)" )` leaves `D` untouched outside,
    so the runner's own value -- the checkout, for a name like `GITHUB_WORKSPACE` -- is what a
    later `cd` reads. A name assigned to anything else is dropped outright rather than scoped,
    which can retire a binding that would really have survived; that direction is the safe one.

    Args:
        words: One command's words, with the parentheses around them already removed.
        workdirs: Each throwaway-holding variable and the shell that assigned it, updated in
            place.
        shell: The shell instance this command runs in.
    """
    assignments = []
    for word in words:
        assignment = _ASSIGNMENT.match(word)
        if assignment is None:
            return
        assignments.append(assignment.groups())
    for name, value in assignments:
        if _makes_throwaway_dir(value):
            workdirs[name] = shell
        else:
            workdirs.pop(name, None)


def _separated_commands(line: str) -> list[tuple[str, list[str]]]:
    """Return each command on a line paired with the operator that precedes it.

    `_invocations` drops the operator, which is the right shape for every caller asking what
    ran. A caller asking where it ran needs the operator back: `cd d && x` and `cd d || x` are
    the same two argvs to a reader that only sees the split.

    The first command's operator is "", since nothing precedes it.
    """
    separated = []
    lexer = shlex.shlex(_uncommented(line.strip()), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    separator = ""
    current: list[str] = []
    for token in lexer:
        if set(token) <= {"&", "|", ";"}:
            separated.append((separator, current))
            separator, current = token, []
        else:
            current.append(token)
    separated.append((separator, current))
    return [(operator, argv) for operator, argv in separated if argv]


def _unnested(
    argv: list[str], shells: list[int], opened: Iterator[int]
) -> tuple[list[str], tuple[int, ...]]:
    """Separate a command's own words from the subshell parentheses around them.

    Each `(` is a distinct shell instance rather than one more level, because a count cannot
    say *which* shell a command ran in: `( cd "${d}" ) && ( x )` closes the one that moved and
    opens a sibling at the same nesting, so a depth comparison reads them as the same shell and
    accepts an `x` that bash runs in the checkout. Numbering the instances keeps them apart.

    Args:
        argv: One command's tokens, parentheses included.
        shells: The shell instances currently open, outermost first, updated in place.
        opened: Source of instance numbers, one per subshell this script opens.

    Returns:
        The command's words, and the shell instances open when it ran.
    """
    words: list[str] = []
    enclosing = tuple(shells)
    for word in argv:
        if word == "(":
            shells.append(next(opened))
        elif word == ")":
            if len(shells) > 1:
                shells.pop()
        else:
            if not words:
                enclosing = tuple(shells)
            words.append(word)
    return words, enclosing


def _launches(argv: list[str], program: str, subcommand: str) -> bool:
    """Report whether a command runs `program subcommand` rather than merely naming it.

    `_runs_subcommand` finds the two words anywhere in the argv, which is what lets a runner's
    own flags precede the CLI. On its own it also accepts `echo … doc-lattice init`, so the
    argv has to start with the program itself or with something that launches it.
    """
    return (
        bool(argv)
        and (argv[0] == program or argv[0] in _LAUNCHERS)
        and _runs_subcommand(argv, program, subcommand)
    )


def _throwaway_dir_runs(text: str, program: str, subcommand: str) -> list[list[str]]:
    """Return each `program subcommand` command that runs inside a throwaway directory.

    Three separate presence checks would not prove this. `_invocations` splits a line at its
    operators, so it exposes the command's own argv but no longer associates it with the `cd`
    in front of it, and an unrelated `mktemp` block plus a command left in the checkout root
    would satisfy them all. The binding is re-established here by reading one line at a time:
    the `cd` has to target a variable this same text assigned from `mktemp -d`, and the command
    has to come after it. A later `cd` somewhere else ends its reach, since from that point the
    command runs wherever that `cd` landed.

    Two things besides that order decide whether the `cd` reaches, and dropping either one
    accepts a rewrite that breaks the guarantee while leaving this green.

    *Which operator joined them.* Only `&&` and `;` run the rest of the list in the shell that
    moved and on the `cd` succeeding. `||` runs it precisely when the `cd` failed; `|` and `&`
    run it in a sibling subshell that inherited the *original* directory, which is the checkout
    root this assertion exists to exclude.

    *Whether the success path still reaches.* Every operator between the two matters, not only
    the first: `cd "${d}" && true || x` leaves the directory changed and never runs `x`, since
    a successful `true` skips the `||` branch. So `||` and `&` end the binding wherever they
    appear -- `&` because it backgrounds everything to its left, the `cd` with it -- while `|`
    is admitted after the first operator, where it only opens a subshell that inherits the
    directory. This under-accepts on purpose: `cd "${d}" && a || b && x` does reach `x`, and is
    rejected rather than reasoned about, because that direction fails closed.

    *Which subshell each ran in.* A `cd` inside a subshell is undone at the closing paren, so
    `( cd "${d}" ) && x` runs `x` in the checkout however the two are joined. The shell that
    moved has to still be open when the command runs, and it is identified rather than counted:
    `( cd "${d}" ) && ( x )` opens a sibling at the same nesting, which a depth comparison
    cannot tell from the shell that moved. `cd "${d}" && ( x )` still matches, since a subshell
    opened afterwards inherits the directory.

    The match itself has to be an invocation and not a mention. `_runs_subcommand` finds the
    two words anywhere in an argv, which is what lets a runner's own flags precede the CLI, but
    it also accepts `echo … doc-lattice init` -- a step that would exit 0 having scaffolded
    nothing. So the argv has to start with the program itself or with something that launches
    it. That bounds the hole rather than closing it: a launcher pointed at another program
    still satisfies it, and no argv-shaped check can tell that from the real thing.
    """
    workdirs: dict[str, int] = {}
    opened = itertools.count(1)
    matched = []
    for line in text.splitlines():
        shells, moved, moved_at = [0], None, -1
        commands = _separated_commands(line)
        for index, (joined_by, argv) in enumerate(commands):
            words, enclosing = _unnested(argv, shells, opened)
            _retarget(words, workdirs, enclosing[-1])
            if moved is not None and index > moved_at + 1 and joined_by not in _CONTINUES:
                # The success path out of the `cd` does not reach this command.
                moved = None
            if moved is not None and moved not in enclosing:
                # The shell that moved has exited, taking its directory with it.
                moved = None
            if words[:1] == ["cd"]:
                joins = commands[index + 1][0] if index + 1 < len(commands) else ""
                reaches = joins in _SEQUENCING and any(
                    _expands(word, name)
                    for word in words[1:]
                    for name, assigned_in in workdirs.items()
                    if assigned_in in enclosing
                )
                moved, moved_at = (enclosing[-1], index) if reaches else (None, -1)
            elif moved is not None and _launches(words, program, subcommand):
                matched.append(words)
    return matched


def _fetches_tags(argv: list[str]) -> bool:
    """Report whether a command fetches tags and overwrites the local refs.

    Both flags are required but their order is not, and unrelated flags are the maintainer's
    business, so match on the arguments rather than on how the line happens to read.
    """
    return argv[:2] == ["git", "fetch"] and {"--tags", "--force"} <= set(argv)


def _runs_twine_check(argv: list[str]) -> bool:
    """Report whether a command runs twine's `check` subcommand."""
    return _runs_subcommand(argv, "twine", "check")


def _needs(job: dict) -> list[str]:
    """Return a job's declared dependencies; GitHub accepts a bare string for a single one."""
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _always(job: dict) -> bool:
    """Report whether a job's condition is a bare `always()` and nothing else.

    `always()` and `${{ always() }}` are the same condition to the runner, so both spellings
    count. Anything else does not: the point of the condition is that no result of the job's
    dependencies can skip it, and any additional term reintroduces a way for it to be skipped.
    """
    condition = str(job.get("if", "")).strip()
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    return condition == "always()"


def _result_gate(job: dict, matrix_job: str) -> tuple[list[str], str]:
    """Return the last command of the step gating on `matrix_job`, and the variable it reads.

    The result has to reach the step through `env` rather than be interpolated into the run body,
    which is what lets this return the variable name the assertion is then required to compare.
    Exactly one step may read it, so a second step cannot quietly become the real gate.
    """
    expression = f"${{{{ needs.{matrix_job}.result }}}}"
    gates = [
        (step, name)
        for step in job["steps"]
        for name, value in (step.get("env") or {}).items()
        if value == expression
    ]
    assert len(gates) == 1, f"expected one step reading {expression}, found {len(gates)}"
    step, variable = gates[0]
    invocations = _invocations(_commands(step))
    assert invocations, f"the step reading {expression} runs no commands"
    return invocations[-1], variable


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
    # The scaffolding path has no fixture to run against, so it is pinned by where it runs
    # instead: a throwaway directory, because `init` writes into the working directory and the
    # checkout already holds a configuration file that would mask a scaffolding regression.
    inits = _throwaway_dir_runs(_commands(step), "doc-lattice", "init")
    assert inits
    for argv in inits:
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


def test_the_rich_floor_leg_installs_the_declared_floor():
    """GTX-201: the leg is only a floor check if it installs the floor `pyproject.toml` declares.

    `tests/test_package_metadata.py` pins the declared floor, but it ships in the sdist and so
    cannot read `.github/`, which left the workflow's own `rich==` pin free to drift: raising the
    floor would fail that test and force an edit there, while this leg went on installing an
    undeclared version and reported green. Correlating the two is the whole point of the leg, so
    it is asserted here, in the repository-only module that already parses this workflow.
    """
    declared = [
        specifier.removeprefix(">=").strip()
        for requirement in tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["dependencies"]
        if requirement.startswith("rich")
        for specifier in requirement.removeprefix("rich").split(",")
        if specifier.strip().startswith(">=")
    ]
    assert len(declared) == 1, f"expected exactly one rich floor, found {declared}"

    runs = [step["run"] for step in _WORKFLOW["jobs"]["rich-floor"]["steps"] if "run" in step]
    installs = [shlex.split(run)[-1] for run in runs if "uv pip install" in run and "rich==" in run]
    assert installs == [f"rich=={declared[0]}"], (
        f"the rich-floor leg installs {installs}, but pyproject.toml declares a floor of "
        f"{declared[0]}. The leg exists to test the oldest supported rich, so the two move "
        "together or it tests nothing."
    )


def test_matrix_aggregators_pin_a_fixed_context_that_fails_closed():
    """GTX-176: branch protection requires these names instead of the generated per-leg ones.

    A matrix leg's context name carries its matrix values, so requiring the legs themselves puts
    every value in `ci.yml`'s matrices into the branch rule as well. Each aggregator reports one
    fixed context for its whole matrix instead, which is what decouples the two settings.

    The shape asserted here is security-sensitive rather than cosmetic. A `needs:` job left with
    the default `if:` is *skipped* when its dependency fails, and GitHub does not treat a skipped
    check as failing, so a naive aggregator turns a red matrix into a mergeable pull request. The
    job-level `always()` plus a success-only comparison in a runner step is what makes it fail
    closed, and the comparison has to stay out of the `if:`, where it would skip the job and
    restore exactly that bypass.
    """
    for job_id, (display, matrix_job) in sorted(_AGGREGATORS.items()):
        job = _WORKFLOW["jobs"][job_id]
        assert job["name"] == display
        assert _needs(job) == [matrix_job]
        assert _always(job), f"{job_id} must carry a job-level always(), found {job.get('if')!r}"
        # The bare name is only free because GitHub renders every leg of the matrix job with its
        # values appended, so the aggregator and the job it summarizes have to keep agreeing.
        matrix = _WORKFLOW["jobs"][matrix_job]
        assert matrix["name"] == display
        assert matrix["strategy"]["matrix"]
        # Trivial by construction: no checkout and no network, so the aggregator has no way to
        # fail for reasons of its own and report a green matrix as red.
        assert all("uses" not in step for step in job["steps"])
        gate, variable = _result_gate(job, matrix_job)
        # `test X = success` exits nonzero for `failure`, `cancelled`, `skipped`, and for any
        # result GitHub adds later. Requiring it to be the step's last command is what stops a
        # trailing `|| true`, or any other recovery, from swallowing that exit status.
        assert gate == ["test", f"${{{variable}}}", "=", "success"]
