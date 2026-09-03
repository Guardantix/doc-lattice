"""Contract tests for release and PyPI publishing automation.

These assert which action each release step calls, not the commit it is pinned to.
`tests/test_workflow_pinning.py` owns the supply-chain rule that every `uses:` in every
workflow resolves to a 40-character commit SHA, so restating individual pins here would
only force a lockstep edit on every routine pin refresh.
"""

import itertools
import json
import re
import shlex
import tomllib
from collections.abc import Iterator
from pathlib import Path

from ruamel.yaml import YAML
from typer.testing import CliRunner
from workflow_helpers import _commands, _invocations, _invokes, _named_step, _uncommented

from doc_lattice.cli import app
from doc_lattice.config import DEFAULT_CONFIG_NAME

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
# The fixture is addressed the way the release step addresses it, relative to the repository
# root, so the test below runs the same resolution the smoke step does.
_RUNNER = CliRunner()
_PACKAGE_URL = "git+https://github.com/Guardantix/doc-lattice"
# A final release: numeric segments and nothing else. Everything PEP 440 admits beyond this --
# `*`, `a`/`b`/`rc`, `.post`, `.dev`, an `N!` epoch, a `+local` -- compares by its own rules and
# names something other than a single release, which is what a floor cell has to pin.
_FINAL_RELEASE = re.compile(r"\d+(?:\.\d+)*")
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
# GTX-239: the pre-publication execution of the built distribution, and the only one. The name is
# load-bearing twice over -- it is how this suite finds the step, so deleting the step fails here
# rather than silently publishing an artifact nothing ever ran.
_SMOKE_BUILT_STEP = "Smoke-test the built distribution"
# The provider that smoke runs against, named from the workspace root rather than relatively: the
# invocation runs from a throwaway directory, where `dist/` names nothing at all.
_WHEEL_GLOB = "${GITHUB_WORKSPACE}/dist/*.whl"
# GTX-292: the run of the shipped test suite from an unpacked sdist, and the only one. As with
# the built-distribution smoke, the name is how this suite finds the step, so deleting the step
# fails here rather than silently restoring the gap it closes.
_SDIST_SUITE_STEP = "Run the shipped test suite from an unpacked sdist"
# The step's interpreter condition, which the workflow spells as an exclusion of the older leg
# rather than as a selection of the newer one. Parsed rather than compared as a string, so the
# assertion below can say *which* leg is excluded instead of restating the whole expression.
_MATRIX_EXCLUSION = re.compile(r"^matrix\.python != '([^']+)'$")
# The line MANAGED_CI.md step 1 tells an adopter to read back, in full and on stderr. `init`
# prints it after the write path and after the benign already-exists path alike, which is why the
# config probes rather than this line are what prove scaffolding happened.
_BRANCH_READBACK = "workflow triggers on branch main (--default-branch)"
# Words that open a compound whose condition or body a command's exit status would become
# instead of it becoming the step's. `!` negates in place and needs no closer.
_COMPOUND_OPENERS = frozenset({"if", "while", "until"})
_COMPOUND_CLOSERS = frozenset({"fi", "done"})
# GTX-176: the jobs `main` requires in place of the per-leg contexts GitHub generates from a
# matrix. Each maps its job id to the fixed context name it reports and the matrix job it
# summarizes. Branch protection names the middle value, so it is the one that cannot drift.
_AGGREGATORS = {
    "code-quality-result": ("Code quality", "code-quality"),
    "runtime-floor-result": ("Runtime floor compatibility", "runtime-floor"),
    "tests-result": ("Tests", "tests"),
    "yaml-compatibility-result": ("YAML parser compatibility", "yaml-compatibility"),
}
# The one runtime dependency AD-26 gives a compatibility matrix of its own.
_YAML_MATRIX_DEPENDENCY = "ruamel.yaml"
# GTX-119: the runtime dependencies whose declared floor the `runtime-floor` matrix does not have
# to carry a cell for, and where each one is exercised instead. Membership routes a floor to
# another job; it never waives the correlation between what a job installs and what
# `pyproject.toml` declares, which every floor owes somewhere. Every other floor-declared runtime
# dependency needs a cell here, so adding a ranged dependency without either a cell or an entry
# fails the correlation guard rather than shipping an unverified span.
_RUNTIME_FLOOR_MATRIX_EXEMPT = {
    # AD-13 pins this exact, so `_declared_floors` never reports it and there is no span beneath a
    # floor to verify. Named anyway, so the reason is already here if that pin ever widens.
    "markdown-it-py": "pinned exact",
    # AD-26 runs this across its whole range, at both ends and with and without the optional C
    # accelerator, in the `yaml-compatibility` matrix. That matrix owns the execution only;
    # GTX-273 gives the correlation between its floor cell and the declaration its own test
    # below, since routing the execution elsewhere left the two free to drift apart.
    _YAML_MATRIX_DEPENDENCY: "execution owned by the yaml-compatibility matrix",
}


def _declared_floors() -> dict[str, str]:
    """Return each runtime dependency's declared `>=` floor, keyed by distribution name.

    A dependency pinned exact carries no `>=` and so no span beneath a floor; it is absent from
    the result rather than present with an empty value, which is what lets the caller treat
    "declares a floor" and "needs a floor cell" as the same question.
    """
    dependencies = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    floors: dict[str, str] = {}
    for requirement in dependencies["dependencies"]:
        start = next(
            (index for index, character in enumerate(requirement) if character in "<>=!~"), None
        )
        assert start is not None, f"{requirement!r} declares no version constraint at all"
        name = requirement[:start].strip()
        for specifier in requirement[start:].split(","):
            if specifier.strip().startswith(">="):
                assert name not in floors, f"{name} declares more than one floor: {requirement!r}"
                floors[name] = specifier.strip().removeprefix(">=").strip()
    return floors


def _release(version: str) -> tuple[int, ...]:
    """Return a final release's numeric segments, without the trailing zeros padding would add.

    PyPA compares two releases by zero-padding the shorter one, so `0.18` and `0.18.0` are the
    same release while `(0, 18) != (0, 18, 0)` in Python. Trimming trailing zeros from both is
    that same relation without the padding step, which is what lets a floor declared as `>=0.18`
    correlate with a matrix cell pinned `==0.18.0` on the version rather than on the spelling.

    Only final-release text is accepted. A wildcard, prerelease, postrelease, developmental
    release, epoch, or local version each carries comparison semantics of its own and names a
    span or a variant rather than the one release an exact floor cell has to install, so a series
    such as `0.18.*` is refused here instead of standing in for the floor it resolves above.
    """
    assert _FINAL_RELEASE.fullmatch(version), (
        f"{version!r} is not a final release, so it does not name one installable version to "
        "correlate a floor with. Spell the floor as an exact numeric release."
    )
    segments = [int(segment) for segment in version.split(".")]
    while len(segments) > 1 and segments[-1] == 0:
        segments.pop()
    return tuple(segments)


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


def _expands_under(word: str, name: str, child: str) -> bool:
    """Report whether a word names `child` inside the directory `name` holds, either spelling.

    The sibling of `_expands` for an operand rather than a `cd` target. Both spellings are
    accepted for the same reason every other helper here accepts more than one: the assertion
    is about which directory the workflow probes, and `$workdir` and `${workdir}` probe the
    same one.
    """
    return word in {f"${name}/{child}", f"${{{name}}}/{child}"}


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
    return [words for _name, words in _throwaway_dir_run_targets(text, program, subcommand)]


def _throwaway_dir_run_targets(
    text: str, program: str, subcommand: str
) -> list[tuple[str, list[str]]]:
    """Return each matched command paired with the throwaway variable its `cd` named.

    The scan `_throwaway_dir_runs` is the argv-only view of. It is kept as one implementation
    because the name and the command are decided together: the `cd` that reaches is the same
    event that identifies the directory, and re-deriving the pairing from a second pass would
    be a second rule that could disagree with this one. An assertion about what observes the
    directory the run used needs that name, since "some throwaway directory" is satisfied by a
    probe of a different one.

    Args:
        text: The step's command text.
        program: The program the command must run.
        subcommand: The subcommand it must run.

    Returns:
        One `(variable name, argv)` pair per matched command, in the order they run.
    """
    workdirs: dict[str, int] = {}
    opened = itertools.count(1)
    matched = []
    for line in text.splitlines():
        shells, moved, moved_at, target = [0], None, -1, ""
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
                reached = [
                    name
                    for word in words[1:]
                    for name, assigned_in in workdirs.items()
                    if assigned_in in enclosing and _expands(word, name)
                ]
                reaches = joins in _SEQUENCING and bool(reached)
                moved, moved_at = (enclosing[-1], index) if reaches else (None, -1)
                target = reached[0] if reaches else ""
            elif moved is not None and _launches(words, program, subcommand):
                matched.append((target, words))
    return matched


def _config_probes(text: str, names: set[str], child: str) -> list[tuple[int, bool]]:
    """Locate each `test`/`[` probe of `child` inside one of the named directories.

    Matching the parsed command rather than the literal line is what binds the observer to the
    directory the runs actually used. A line-equality check passes just as happily when the
    probe names a directory nothing ran in, which makes the CI step vacuous while the test
    stays green. Both `test` and `[` are accepted, and so is either variable spelling, for the
    same reason `_flag_value` accepts an attached value: the contract is what gets probed.

    Args:
        text: The step's command text.
        names: The directory variables a probe may name.
        child: The entry inside that directory the probe must name.

    Returns:
        One `(line index, negated)` pair per probe, in the order they run.
    """
    probes = []
    for index, line in enumerate(text.splitlines()):
        for _joined_by, argv in _separated_commands(line):
            words = argv[:-1] if argv[:1] == ["["] and argv[-1:] == ["]"] else argv
            if words[:1] not in (["test"], ["["]):
                continue
            operands = words[1:]
            negated = operands[:1] == ["!"]
            if negated:
                operands = operands[1:]
            if operands[:1] != ["-e"]:
                continue
            if any(_expands_under(word, name, child) for word in operands[1:] for name in names):
                probes.append((index, negated))
    return probes


def _expanded_name(word: str) -> str | None:
    """Return the variable a word expands, in either spelling, or None when it expands none.

    `_expands` answers the same question against a name already known. This one is for the
    callers that have to *learn* the name: a redirection target and the operand of the command
    that reads that file back have to be the same variable, and this contract fixes neither.
    """
    if word.startswith("${") and word.endswith("}"):
        return word[2:-1] or None
    if word.startswith("$"):
        return word[1:] or None
    return None


def _short_flags(argv: list[str]) -> set[str]:
    """Return the short-option letters a command carries, `-qxF` counting as three.

    Bundled and separate spellings are the same request to every option parser, so the contract
    is about which options are in force rather than about how they were written.
    """
    letters: set[str] = set()
    for word in argv[1:]:
        if word.startswith("--") or not word.startswith("-"):
            continue
        letters.update(word[1:])
    return letters


def _operands(argv: list[str]) -> list[str]:
    """Return a command's non-option arguments, in order.

    Only sound where the command's short options take no values, which holds for every `grep`
    and `ls` this suite reads; a value-taking flag would land its value here.
    """
    return [word for word in argv[1:] if not word.startswith("-")]


def _bindings(text: str) -> list[tuple[str, str]]:
    """Return every `name=value` binding a step performs, in the order it performs them.

    Only a command that is nothing but assignments counts, for the reason `_retarget` gives at
    length: `NAME=value cmd` sets the name for that one command, and `echo 'NAME=value'` sets
    nothing at all.
    """
    bound: list[tuple[str, str]] = []
    for line in text.splitlines():
        for _joined_by, argv in _separated_commands(line):
            pairs: list[tuple[str, str]] = []
            for word in argv:
                assignment = _ASSIGNMENT.match(word)
                if assignment is None:
                    pairs = []
                    break
                pairs.append((assignment.group(1), assignment.group(2)))
            bound.extend(pairs)
    return bound


def _makes_local_wheel(value: str) -> bool:
    """Report whether an assigned value is the path of the one wheel the job just built.

    The sibling of `_makes_throwaway_dir`, and it recognizes a single shape for the same reason:
    what a substitution expands to is what its last command printed, so a compound one vouches
    for nothing. The listing has to name `dist/*.whl` under the workspace root and nothing else.
    A relative `dist/` would resolve against the throwaway directory the run happens in, and a
    requirement naming the index or the repository would install something other than what this
    job produced, which is the whole claim the step exists to make.
    """
    substitution = _COMMAND_SUBSTITUTION.match(value)
    if substitution is None:
        return False
    inner = _invocations(substitution.group(1))
    if len(inner) != 1 or inner[0][:1] != ["ls"] or not _REDIRECTIONS.isdisjoint(inner[0]):
        return False
    return _operands(inner[0]) == [_WHEEL_GLOB]


def _sole_match(value: str, name: str, child: str, flags: set[str]) -> bool:
    """Report whether an assigned value is a listing of `child` inside the directory `name` holds.

    The third sibling of `_makes_throwaway_dir`, recognizing one shape and refusing the rest for
    the reason both of the others give: what a substitution expands to is what its last command
    printed, so a compound one vouches for nothing.

    The listing is also the sole-match gate, which is why the pattern rather than a resolved path
    is what has to be pinned. A glob matching two entries makes the value two lines, and the
    `test` that follows rejects it as neither a file nor a directory; a caller that resolved the
    match some other way could select one of several silently.

    Args:
        value: The assigned value, substitution included.
        name: The variable whose directory the pattern is anchored in.
        child: The pattern below that directory, `*.tar.gz` or `*/`.
        flags: Short options the listing must carry, `-1` as the letter ``1``.

    Returns:
        True when the value is exactly that listing.
    """
    substitution = _COMMAND_SUBSTITUTION.match(value)
    if substitution is None:
        return False
    inner = _invocations(substitution.group(1))
    if len(inner) != 1 or inner[0][:1] != ["ls"] or not _REDIRECTIONS.isdisjoint(inner[0]):
        return False
    operands = _operands(inner[0])
    return (
        flags <= _short_flags(inner[0])
        and len(operands) == 1
        and _expands_under(operands[0], name, child)
    )


def _sdist_suite_step() -> dict:
    """Return the `tests` step that runs the shipped suite from an unpacked sdist.

    Three tests read it, and `_named_step` raises rather than returning None, so deleting the
    step fails each of them at this line instead of leaving one of them vacuously green.
    """
    return _named_step(_WORKFLOW["jobs"]["tests"], _SDIST_SUITE_STEP)


def _redirections(argv: list[str]) -> dict[str, str]:
    """Return each file descriptor a command redirects, mapped to the target it writes to.

    `>` and `2>` reach a parsed command as ordinary words, the latter split into its descriptor
    and the operator, so a bare `>` is stdout and a `>` preceded by a digit is that descriptor.
    Reading them this way is what makes "captured apart" checkable rather than assumed. A step
    that merges the channels spells it `2>&1`, which tokenizes as `>&` and so produces no entry
    here at all; a step that points both at one file produces two entries holding one value.
    """
    targets: dict[str, str] = {}
    for index, word in enumerate(argv):
        if word != ">":
            continue
        previous = argv[index - 1] if index else ""
        descriptor = previous if previous.isdigit() else "1"
        targets[descriptor] = argv[index + 1] if index + 1 < len(argv) else ""
    return targets


def _status_reaches_the_step(text: str, program: str, subcommand: str) -> bool:
    """Report whether `program subcommand`'s own exit status is what the step exits with.

    Presence assertions cannot see this, and it is the half of the oracle a rewrite would take
    first. "Exit 0" in the acceptance criteria means the *command's* exit, not the last shell
    line's: `cmd || true`, `cmd | tee log`, `! cmd`, `if cmd; then`, and a preceding `set +e`
    each leave every presence, config, and readback check green in front of a run that failed.
    So the command has to be the last one on its line, joined only by operators that carry a
    failure out of the list, negated by nothing, and outside any compound still open above it.

    Compound depth is tracked by the word each line opens with, which is coarse and deliberately
    so: it over-rejects a legitimate one-line compound elsewhere in the step rather than
    reasoning about shell grammar, and over-rejection is the direction that fails closed.
    """
    depth, reached = 0, False
    for line in text.splitlines():
        commands = _separated_commands(line)
        words = [[word for word in argv if word not in {"(", ")"}] for _joined_by, argv in commands]
        if any(argv[:1] == ["set"] and "+e" in argv for argv in words):
            return False
        for index, argv in enumerate(words):
            if not _launches(argv, program, subcommand):
                continue
            if depth or index != len(words) - 1:
                return False
            if any(joined_by not in _SEQUENCING for joined_by, _argv in commands[1:]):
                return False
            if any(
                other[:1] == ["!"] or (bool(other) and other[0] in _COMPOUND_OPENERS)
                for other in words
            ):
                return False
            reached = True
        opener = words[0][0] if words and words[0] else ""
        if opener in _COMPOUND_OPENERS:
            depth += 1
        elif opener in _COMPOUND_CLOSERS:
            depth = max(depth - 1, 0)
    return reached


def _refuses_on_match(text: str, pattern_of: str, target: str) -> bool:
    """Report whether the step fails when `pattern_of` is found in the file `target` names.

    The negative half of the channel oracle. A bare `! grep` cannot express it, because errexit
    is defined not to apply to a negated pipeline, so the readback could reach stdout with the
    step still green; and a `grep` whose result nothing reads is a no-op either way. What has to
    be pinned is therefore the whole conditional: a fixed-string search of that one file, and a
    body that ends the step with a nonzero status before the compound closes.

    The pattern is required to be a prefix of the readback rather than equal to it, since any
    prefix of a line that must not appear is a stricter search and a legitimate spelling.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        commands = [argv for _joined_by, argv in _separated_commands(line)]
        if not commands or commands[0][:1] != ["if"]:
            continue
        condition = commands[0][1:]
        if condition[:1] != ["grep"] or "F" not in _short_flags(condition):
            continue
        operands = _operands(condition)
        if len(operands) != 2:
            continue
        pattern, searched = operands
        if not pattern or not pattern_of.startswith(pattern):
            continue
        if _expanded_name(searched) != target:
            continue
        for body in lines[index + 1 :]:
            argvs = [argv for _joined_by, argv in _separated_commands(body)]
            if any(argv[:1] == ["fi"] for argv in argvs):
                break
            if any(argv[:1] == ["exit"] and argv[1:2] not in ([], ["0"]) for argv in argvs):
                return True
    return False


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


def test_release_is_reachable_only_from_a_push_to_the_default_branch():
    # The re-arm token deliberately adds a second reason for the gate to create a tag without
    # adding a way to reach the job. That claim is only true while this condition holds: a
    # workflow_dispatch or a pull_request trigger here would be a release authority surface
    # governed separately from the branch protection that authorizes a version bump. The
    # triggers are asserted alongside the condition because either half alone can open one.
    triggers = _WORKFLOW["on"]
    assert set(triggers) == {"push", "pull_request"}
    assert triggers["push"]["branches"] == ["main"]
    assert _WORKFLOW["jobs"]["release"]["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )


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


def test_migration_rule_runs_in_code_quality_on_both_paths():
    # GTX-150. `Code quality` is this guard's only authority: the pre-commit hook is opt-in, and
    # the release job deliberately does not re-assert it, because this same job runs again on the
    # push to `main` that is the release commit. Both invocations are pinned because they carry
    # different halves of the rule -- the base-ref one is what catches a renderer and its baseline
    # updated together, and the plain one is what runs on that push, where there is no base ref.
    argvs = [
        argv
        for step in _WORKFLOW["jobs"]["code-quality"]["steps"]
        for argv in _invocations(_commands(step))
        if _invokes(argv, "scripts/check_migration_rule.py")
    ]
    assert len(argvs) == 2, "code-quality must run the migration guard on both paths"
    with_base = [argv for argv in argvs if "--base-ref" in argv]
    assert len(with_base) == 1
    assert with_base[0][with_base[0].index("--base-ref") + 1] == "FETCH_HEAD"
    assert [argv for argv in argvs if "--base-ref" not in argv]


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
    # instead: a throwaway directory, because `init` writes into the working directory and this
    # checkout tracks no `.doc-lattice.yml` of its own for the run to collide with, so a run in
    # the checkout root would scaffold one into the working tree of the release job.
    commands = _commands(step)
    inits = _throwaway_dir_runs(commands, "doc-lattice", "init")
    assert inits
    for argv in inits:
        assert _flag_value(argv, "--from") == "${REF}"
    # GTX-153 added the read-only mode, and a smoke run of it proves nothing without an observer:
    # nothing else in this step reads the directory afterwards, so an accidental write would land
    # silently. Both runs share the one throwaway directory and are separated by the two
    # assertions about it -- absent after the print-only run, present after the scaffolding one.
    # Asserting both is what keeps the addition from quietly retiring the packaged write path,
    # which is the coverage the throwaway directory has carried since GTX-142.
    #
    # The probes are matched through the throwaway names rather than by line text, so pointing
    # either run at a second `mktemp -d` while the probes keep naming the first one fails here
    # instead of leaving a green test in front of a CI check that observes a directory nothing
    # ran in.
    targets = {name for name, _argv in _throwaway_dir_run_targets(commands, "doc-lattice", "init")}
    assert len(targets) == 1, (
        "both init runs have to share one throwaway directory, or the probes below observe a "
        f"directory the other run never touched; got {sorted(targets)}"
    )
    probes = _config_probes(commands, targets, DEFAULT_CONFIG_NAME)
    assert [negated for _index, negated in probes] == [True, False], (
        f"expected exactly two probes of the throwaway config, absent then present, got {probes}"
    )
    lines = [line.strip() for line in commands.splitlines()]

    def _sole_line(predicate) -> int:
        matches = [index for index, line in enumerate(lines) if predicate(line)]
        assert len(matches) == 1, f"expected exactly one matching line, got {matches}"
        return matches[0]

    printing = _sole_line(lambda line: "--print-only" in line)
    scaffolding = _sole_line(lambda line: "doc-lattice init" in line and "--print-only" not in line)
    (absent, _), (present, _) = probes
    assert printing < absent < scaffolding < present
    assert _step_index(release, "Smoke-test the commit") < _step_index(release, _TAG_STEP)


def test_release_smoke_fixture_passes_the_commands_the_smoke_step_gates_on(monkeypatch):
    # The test above asserts only that the fixture file exists, and no other suite points a
    # command at it, so before this one the fixture was executed for the first time in the
    # release job itself. That is the worst place for it to fail: the job runs on the push that
    # already merged the version bump, and the tag-health gate reads the pre-push version, so a
    # fixture the release refuses leaves every required check green, fails the release, and
    # cannot be recovered by a follow-up push -- every later push to `main` reads the bump as
    # already landed and declines to create the tag. Running the two gating commands here moves
    # a config-schema change that strands the fixture into the pull request that makes it.
    monkeypatch.chdir(_ROOT)
    graph = _RUNNER.invoke(app, ["graph", "--config", _SMOKE_FIXTURE, "--format", "json"])
    assert graph.exit_code == 0, graph.output
    # Not a test of `graph`. A fixture whose `docs_roots` resolved to nothing at all would exit 0
    # from both commands below, so the node count is what keeps them from passing vacuously.
    assert json.loads(graph.stdout)["nodes"], f"{_SMOKE_FIXTURE} resolves to an empty lattice"
    for subcommand in ("check", "lint"):
        result = _RUNNER.invoke(app, [subcommand, "--config", _SMOKE_FIXTURE])
        assert result.exit_code == 0, result.output


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


def test_built_distribution_is_smoke_tested_before_it_can_be_published():
    """GTX-239: the packaged artifact is executed while publication can still be stopped.

    The `release` job's pre-tag smoke installs from the source tree and omits the flag, and
    Twine validation reads metadata without running anything, so before this step nothing
    exercised MANAGED_CI.md step 1's command shape against what an adopter actually installs.
    Once PyPI serves a version the only remedies left are the hotfix and yank paths RELEASING.md
    owns, which is why the check has to gate the upload rather than report after it.
    """
    build = _WORKFLOW["jobs"]["build-release"]
    step = _named_step(build, _SMOKE_BUILT_STEP)
    commands = _commands(step)
    # A failure has to prevent publication, and the ordering is the whole mechanism: `publish`
    # consumes the artifact the upload produces, so a smoke that fails before the upload leaves
    # that job nothing to download. Placed after it, the same failure would be a report on an
    # artifact already on its way to an approval gate.
    assert (
        _step_index(build, "Validate distributions")
        < _step_index(build, _SMOKE_BUILT_STEP)
        < _step_index(build, "Upload distributions")
    )
    # A step allowed to fail soft is the same as no step, and the job-level spelling reaches
    # every step in it.
    assert "continue-on-error" not in step
    assert "continue-on-error" not in build

    # Provenance, first half: the provider is the one wheel this job built, resolved absolutely
    # because the invocation runs somewhere `dist/` does not exist.
    wheels = [name for name, value in _bindings(commands) if _makes_local_wheel(value)]
    assert len(wheels) == 1, f"expected exactly one local-wheel binding, got {wheels}"
    (wheel,) = wheels
    # Provenance, second half: an installed `uv tool` copy of doc-lattice satisfies a run from
    # the default tool directory without consulting anything, which RELEASING.md records as a
    # live hazard rather than a hypothetical. Pointing the lookup at per-job scratch is what
    # makes "the wheel it built" observable instead of inferred.
    assert step["env"]["UV_TOOL_DIR"].startswith("${{ runner.temp }}/")

    # The invocation itself, bound to the directory it runs in the way the pre-tag smoke is.
    targets = _throwaway_dir_run_targets(commands, "doc-lattice", "init")
    assert len(targets) == 1, f"expected exactly one packaged init run, got {targets}"
    ((workdir, argv),) = targets
    assert _expanded_name(_flag_value(argv, "--from") or "") == wheel
    assert _flag_value(argv, "--default-branch") == "main"

    # The two channels are the interface under test: stdout owns the copy-paste blocks and the
    # readback is deliberately not among them, so a capture that merged them would satisfy every
    # assertion below without proving anything about what an adopter reads.
    channels = _redirections(argv)
    assert set(channels) == {"1", "2"}, f"stdout and stderr must be captured apart, got {channels}"
    stdout, stderr = (_expanded_name(channels["1"]), _expanded_name(channels["2"]))
    assert stdout
    assert stderr
    assert stdout != stderr

    # `exit 0` means this command's exit, not the shell's last line.
    assert _status_reaches_the_step(commands, "doc-lattice", "init")

    # Scaffolding is proven by the config, not by the readback, which `init` prints on the
    # already-exists path too. Both probes name the directory the run actually used, so pointing
    # the run at a second throwaway while the probes keep naming the first fails here.
    probes = _config_probes(commands, {workdir}, DEFAULT_CONFIG_NAME)
    assert [negated for _index, negated in probes] == [True, False], (
        f"expected exactly two probes of the throwaway config, absent then present, got {probes}"
    )

    # The readback, whole and on stderr. `-x` is what makes it exact: a substring match would
    # accept a line that named some other branch alongside this one.
    exact = [
        invocation
        for invocation in _invocations(commands)
        if invocation[:1] == ["grep"]
        and {"x", "F"} <= _short_flags(invocation)
        and _operands(invocation)[:1] == [_BRANCH_READBACK]
        and _expanded_name(_operands(invocation)[-1]) == stderr
    ]
    assert exact, f"no exact whole-line match of {_BRANCH_READBACK!r} against the stderr capture"
    # And absent from the other channel, which is the assertion the merge mutation fails.
    assert _refuses_on_match(commands, _BRANCH_READBACK, stdout)


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


def test_the_runtime_floor_matrix_covers_every_declared_floor():
    """GTX-119: the matrix is only a floor check if it installs the floors declared here.

    `tests/test_package_metadata.py` pins the declared floors, but it ships in the sdist and so
    cannot read `.github/`, which left the workflow's own `==` pins free to drift: raising a floor
    would fail that test and force an edit there, while this matrix went on installing an
    undeclared version and reported green. Correlating the two is the whole point of the matrix,
    so it is asserted here, in the repository-only module that already parses this workflow.

    Derived rather than listed, which is the part GTX-201's single-dependency version could not
    do. A ranged runtime dependency added with no cell and no `_FLOOR_EXEMPT` entry fails here,
    so the span beneath a new floor cannot ship unverified merely because nobody remembered this
    file.
    """
    cells = list(_WORKFLOW["jobs"]["runtime-floor"]["strategy"]["matrix"]["floor"])
    assert cells == sorted(cells), f"keep the floor cells sorted for readability, found {cells}"
    installed = {}
    for cell in cells:
        name, separator, version = cell.partition("==")
        # A cell that installs a range or a series resolves to something newer than the floor and
        # so tests the wrong version, which is the failure this whole matrix exists to prevent.
        assert separator, f"the floor cell {cell!r} does not pin a version with =="
        assert version, f"the floor cell {cell!r} pins an empty version"
        assert name not in installed, f"{name} has more than one floor cell: {cells}"
        installed[name] = version

    expected = {
        name: floor
        for name, floor in _declared_floors().items()
        if name not in _RUNTIME_FLOOR_MATRIX_EXEMPT
    }
    assert installed == expected, (
        f"the runtime-floor matrix installs {installed}, but pyproject.toml declares {expected}. "
        "The matrix exists to test the oldest supported version of each dependency, so the two "
        "move together or it tests nothing. A dependency exercised by a matrix of its own belongs "
        "in _RUNTIME_FLOOR_MATRIX_EXEMPT with the reason, and still owes a correlation test there."
    )


def test_the_runtime_floor_matrix_runs_the_whole_suite_on_every_supported_interpreter():
    """GTX-119: one dependency at its minimum, on every interpreter, against the whole suite.

    Three separable claims, because dropping any one of them leaves a supported combination
    unverified while the job still reports green:

    A constrained resolver may hold one dependency at its minimum and take the rest from a
    resolution as new as this project's lock, so each cell overlays exactly one floor onto the
    locked remainder. A cell holding every floor at once would not answer which dependency broke,
    and would test a combination no resolver has to produce.

    `pydantic` resolves through interpreter-specific `pydantic-core` wheels, so an answer on one
    interpreter is not an answer on the other. The interpreter list is compared against the
    `tests` job rather than spelled again, so adding a supported interpreter there extends the
    floor matrix instead of silently leaving its cells behind.

    And the suite runs whole. GTX-201's leg ran `tests/cli/` because the seam it was added for is
    a CLI one, but a floor that changes validation-message rendering or parser behavior is not
    confined to that directory, and a subset makes the green mean less than it appears to.
    """
    job = _WORKFLOW["jobs"]["runtime-floor"]
    interpreters = list(job["strategy"]["matrix"]["python"])
    assert interpreters == list(_WORKFLOW["jobs"]["tests"]["strategy"]["matrix"]["python"]), (
        "the floor matrix must cross every interpreter the `tests` job supports; otherwise a "
        "supported dependency and interpreter combination is never resolved at the floor."
    )
    assert job["env"]["UV_PYTHON"] == "${{ matrix.python }}", (
        "without UV_PYTHON the cells all resolve against the same default interpreter and the "
        "python axis silently runs the same environment twice."
    )

    commands = [shlex.split(step["run"]) for step in job["steps"] if "run" in step]
    installs = [argv for argv in commands if argv[:3] == ["uv", "pip", "install"]]
    assert installs == [["uv", "pip", "install", "${{ matrix.floor }}"]], (
        f"expected exactly one overlay installing the cell's own floor, found {installs}. A "
        "second install, or a hardcoded version, decouples the cell from the matrix value the "
        "correlation guard checks."
    )
    runs = [argv for argv in commands if _runs_subcommand(argv, "uv", "run")]
    assert runs == [["uv", "run", "--no-sync", "pytest"]], (
        f"expected the whole suite and no re-sync, found {runs}. `--no-sync` is load-bearing: "
        "`uv run` without it restores the locked version and the overlaid floor never runs."
    )


def test_the_yaml_compatibility_matrix_installs_the_declared_ruamel_floor():
    """GTX-273: the dedicated matrix is a floor check only while its cell is the declared floor.

    `_RUNTIME_FLOOR_MATRIX_EXEMPT` routes `ruamel.yaml` around the `runtime-floor` correlation
    because AD-26 gives it a job that runs the whole suite at both ends of the declared range.
    That routing decides where the floor executes and settles nothing about which floor executes:
    with no assertion here, raising the declaration to `>=0.19` would leave this leg installing
    `0.18.0` and reporting green on a release the project no longer claims to support -- the
    exact drift GTX-119 closed for every other floor, reintroduced by the exemption that was
    meant only to say the execution happens elsewhere.

    The floor cell is selected by shape rather than by position, so reordering the matrix or
    adding a mid-range cell cannot quietly point the assertion at the ceiling, and the ceiling
    stays a series without having to be named here.

    The install command is asserted alongside the cell because the matrix data alone is not the
    claim. `runtime-floor` proves separately that its install step consumes `${{ matrix.floor }}`
    (a hardcoded version there would drift behind a correct matrix); this job owes the same
    proof, or a correlated cell could sit beside a step installing some other release. The
    optional `ruamel.yaml.clib` overlay is a different distribution on its own axis and is
    filtered out rather than counted.
    """
    job = _WORKFLOW["jobs"]["yaml-compatibility"]
    cells = list(job["strategy"]["matrix"]["ruamel"])
    exact = [cell for cell in cells if _FINAL_RELEASE.fullmatch(cell)]
    assert len(exact) == 1, (
        f"expected exactly one exact release among the ruamel cells {cells}, found {exact}. The "
        "floor is the cell pinned to a single release and the ceiling is a series, so a second "
        "exact cell leaves it ambiguous which one this correlation is about."
    )

    declared = _declared_floors()[_YAML_MATRIX_DEPENDENCY]
    assert _release(exact[0]) == _release(declared), (
        f"the yaml-compatibility matrix installs {_YAML_MATRIX_DEPENDENCY} {exact[0]}, but "
        f"pyproject.toml declares a floor of {declared}. The floor cell and the declaration name "
        "one release or the leg verifies a span the project does not offer."
    )

    commands = [shlex.split(step["run"]) for step in job["steps"] if "run" in step]
    installs = [argv for argv in commands if argv[:3] == ["uv", "pip", "install"]]
    floor_installs = [
        argv
        for argv in installs
        if len(argv) > 3 and argv[3].partition("==")[0] == _YAML_MATRIX_DEPENDENCY
    ]
    expected = ["uv", "pip", "install", f"{_YAML_MATRIX_DEPENDENCY}==${{{{ matrix.ruamel }}}}"]
    assert floor_installs == [expected], (
        f"expected exactly one overlay installing the cell's own version, found {floor_installs}."
        " A second install, or a hardcoded version, decouples the job from the matrix value this "
        "test correlates and the correlation stops describing what CI runs."
    )


def test_the_shipped_sdist_suite_is_gated_by_the_protected_tests_context():
    """GTX-292: where the archive is executed, and on which leg.

    `tests/test_package_metadata.py` couples the sdist manifest's exclude list to its
    archive-membership denial set, which says the two lists agree and nothing about either being
    complete. A test module that reads `scripts/`, `.github/`, or a root document the sdist does
    not carry is absent from both and passes that coupling cleanly; it then ships, and fails at
    collection for anyone who runs the shipped suite. Five such modules accumulated before
    GTX-251 and a sixth afterwards, every existing gate green throughout, because nothing in CI
    ever unpacked the archive and ran it. AD-47 owns the decision; these three tests own its
    shape, and this module excludes itself from the sdist so it can read `ci.yml` without
    recreating the problem it pins.

    The step belongs to the `tests` matrix rather than to a job of its own, which is what gives it
    the protected `Tests` context `tests-result` reports and the `release` job's `needs:`. As a
    standalone job it would emit a context branch protection does not require -- advisory until
    the staged rollout RELEASING.md describes adds it -- and on the release path it would run
    after `release` has already pushed the immutable tag.

    It runs after the ordinary suite rather than instead of it, so the cheaper and more
    informative failure is the one a reader meets first and a manifest problem is distinguishable
    from a real one. And on one leg, spelled as an exclusion of the oldest interpreter for the
    reason `code-quality` records about `links`: naming the leg to run on drops the gate entirely
    if that matrix value is ever removed, while excluding one leg can at worst run it twice.
    """
    job = _WORKFLOW["jobs"]["tests"]
    step = _sdist_suite_step()
    steps = job["steps"]
    index = steps.index(step)

    ordinary = [
        position
        for position, other in enumerate(steps)
        if other is not step
        and any(_launches(argv, "uv", "run") for argv in _invocations(_commands(other)))
    ]
    assert ordinary, (
        f"the {_SDIST_SUITE_STEP!r} step is the job's only suite run. It duplicates the checkout's "
        "own run deliberately and does not replace it: a failure that reaches only the archive is "
        "a manifest problem, and telling the two apart needs both."
    )
    assert max(ordinary) < index, (
        f"the {_SDIST_SUITE_STEP!r} step must follow the checkout's own suite, found it at "
        f"{index} with runs at {ordinary}. A failure from the source tree is cheaper to read and "
        "explains more, so it is the one a reader should meet first."
    )

    interpreters = list(job["strategy"]["matrix"]["python"])
    exclusion = _MATRIX_EXCLUSION.fullmatch(step.get("if", ""))
    assert exclusion is not None, (
        f"expected the step's condition to exclude one interpreter, found {step.get('if')!r}. A "
        "condition naming the leg to run on silently drops this gate if that matrix value is "
        "ever removed; excluding a leg can at worst run the check twice."
    )
    assert exclusion.group(1) == min(interpreters, key=_release), (
        f"the step excludes {exclusion.group(1)!r}, but the matrix's oldest interpreter is "
        f"{min(interpreters, key=_release)!r}. Archive membership and working-directory isolation "
        "do not vary by interpreter, so the newest leg is the one that carries this."
    )

    addopts = shlex.split(step["env"]["PYTEST_ADDOPTS"])
    assert "--no-cov" in addopts, (
        f"expected the step to disable coverage, found {addopts}. The leg's ordinary run already "
        "enforces `fail_under`, and the shipped suite is a subset whose total is not the number "
        "that threshold describes."
    )
    assert addopts[addopts.index("--dist") + 1 : addopts.index("--dist") + 2] == ["loadfile"], (
        f"expected `--dist loadfile`, found {addopts}. The `tests` job records why file "
        "granularity is load-bearing rather than tuning: `tests/test_reconcile_fuzz.py` asserts "
        "over an accumulator the rest of its module populates."
    )


def test_the_shipped_sdist_suite_runs_from_a_tree_the_step_built_outside_the_checkout():
    """GTX-292: the assertions that keep the run honest rather than merely present.

    Provenance is the half a rewrite would take first, because losing it costs nothing visible.
    An unpack inside the working tree puts the repository's own `scripts/` and `.github/` within
    reach of a module the archive cannot supply them to, so the step goes green on exactly the
    drift it exists to catch -- a passing gate that has stopped asking the question.

    The two listings are the sole-match gates as well as the selections. Each is one `ls` and
    nothing else, for the reason `_makes_throwaway_dir` gives: a compound substitution expands to
    whatever its last command printed and vouches for nothing. A glob with two matches then makes
    the value two lines, which the `test` beside it rejects -- so the suite can only ever run from
    the one archive this step built, and only from the tree that archive produced.
    """
    step = _sdist_suite_step()
    text = _commands(step)
    commands = _invocations(text)
    build_dir = step["env"]["SDIST_DIR"]
    unpack_dir = step["env"]["UNPACK_DIR"]
    for label, value in (("SDIST_DIR", build_dir), ("UNPACK_DIR", unpack_dir)):
        assert value.startswith("${{ runner.temp }}/"), (
            f"{label} is {value!r}, which is not under the runner's temporary directory. A path "
            "inside the checkout lets the repository's own files satisfy a module the archive "
            "cannot, and the step then passes on the drift it exists to catch."
        )
    assert build_dir != unpack_dir, (
        f"the archive is built into and unpacked from the same directory {build_dir!r}, which "
        "leaves the sole-root probe unable to distinguish the extracted tree from its source."
    )

    builds = [argv for argv in commands if _runs_subcommand(argv, "uv", "build")]
    assert len(builds) == 1, f"expected exactly one build, found {builds}"
    assert "--sdist" in builds[0], (
        f"the build is {builds[0]}. A wheel proves nothing here: the modules at issue are "
        "excluded from the sdist manifest, which the wheel does not read."
    )
    assert _expands(_out_dir(builds[0]) or "", "SDIST_DIR"), (
        f"the build writes to {_out_dir(builds[0])!r} rather than to the directory SDIST_DIR "
        "names, so the probe below would select an archive this step did not build."
    )

    bindings = dict(_bindings(text))
    assert _sole_match(bindings.get("ARCHIVE", ""), "SDIST_DIR", "*.tar.gz", {"1"}), (
        f"ARCHIVE is bound to {bindings.get('ARCHIVE')!r}. It has to be a one-per-line listing "
        "of the built archives: a second archive then makes the value two lines, which the "
        "`test -f` below rejects, so the suite can only run from the one archive just built."
    )
    assert _sole_match(bindings.get("ROOT", ""), "UNPACK_DIR", "*/", {"1", "d"}), (
        f"ROOT is bound to {bindings.get('ROOT')!r}. The same probe for the extracted tree, with "
        "`-d` so the directory is listed rather than its contents; without it the value names "
        "the archive's files and the `cd` below lands nowhere."
    )
    # `test -f PATH` is three words, and a longer `test` is some other expression entirely.
    gates = {tuple(argv) for argv in commands if argv[:1] == ["test"] and len(argv) == 3}
    assert ("test", "-f", "${ARCHIVE}") in gates, (
        f"nothing gates ARCHIVE on being one existing file, found {sorted(gates)}. The listing "
        "alone selects nothing: with two matches the variable holds two lines, and only this "
        "check turns that into a failure instead of an unreadable path."
    )
    assert ("test", "-d", "${ROOT}") in gates, (
        f"nothing gates ROOT on being one existing directory, found {sorted(gates)}."
    )

    tars = [argv for argv in commands if argv[:1] == ["tar"]]
    assert len(tars) == 1, f"expected exactly one extraction, found {tars}"
    assert _expands(_flag_value(tars[0], "-C") or "", "UNPACK_DIR"), (
        f"the extraction targets {_flag_value(tars[0], '-C')!r} rather than the directory "
        "UNPACK_DIR names, which leaves the tree the suite runs from unrelated to the archive."
    )
    assert _expands(next((word for word in _operands(tars[0]) if word != "-C"), ""), "ARCHIVE"), (
        f"the extraction does not read the archive ARCHIVE names, found {tars[0]}."
    )


def test_the_shipped_sdist_suite_runs_from_the_extracted_root_with_pytests_own_status():
    """GTX-292: what the step runs, where it runs it, and what its exit status means.

    Both commands run from the extracted root, and the sync resolves the shipped dev group rather
    than the repository lock, which the sdist deliberately does not ship: resolving from scratch
    is part of the artifact under test.

    And pytest's own status is what the step exits with, which no presence assertion can see. A
    trailing `|| true`, a pipe, a `set +e`, or a `continue-on-error` on the step leaves every
    other check here green in front of the module-scope collection error this gate exists for --
    and collection errors, not assertion failures, are that failure's whole shape.
    """
    step = _sdist_suite_step()
    text = _commands(step)
    commands = _invocations(text)
    lines = text.splitlines()
    moves = [
        position
        for position, line in enumerate(lines)
        for _joined_by, argv in _separated_commands(line)
        if argv[:1] == ["cd"]
    ]
    assert len(moves) == 1, (
        f"expected exactly one change of directory, found {len(moves)} at lines {moves}. A second "
        "one leaves it unsettled which tree the two commands below ran in."
    )
    moved_at = moves[0]
    moved = _separated_commands(lines[moved_at])
    assert len(moved) == 1, (
        f"the move shares its line: {lines[moved_at].strip()!r}. Joined to another command it may "
        "not run at all, and the commands below would then read the checkout."
    )
    # `cd PATH` is two words; anything longer is not a plain move to one named directory.
    move = moved[0][1]
    assert len(move) == 2, f"the move takes more than one operand: {move}"
    assert _expands(move[1], "ROOT"), (
        f"the move is {lines[moved_at].strip()!r}. It has to target the directory ROOT names; any "
        "other target runs the suite from a tree that is not the extracted one."
    )

    syncs = []
    for position, line in enumerate(lines):
        for _joined_by, argv in _separated_commands(line):
            if not _launches(argv, "uv", "sync") and not _launches(argv, "uv", "run"):
                continue
            assert position > moved_at, (
                f"{' '.join(argv)!r} runs before the move to the extracted root, so it reads the "
                "checkout's own project rather than the archive's."
            )
            if _launches(argv, "uv", "sync"):
                syncs.append(argv)

    assert len(syncs) == 1, f"expected exactly one dependency sync, found {syncs}"
    assert _flag_value(syncs[0], "--group") == "dev", (
        f"the sync is {syncs[0]}. The shipped suite needs the dev group the archive's own "
        "`pyproject.toml` declares, which is the group under test."
    )
    assert "--locked" not in syncs[0], (
        f"the sync is {syncs[0]}. It resolves with no lock: the sdist ships `pyproject.toml` and "
        "deliberately not `uv.lock`, so resolving from scratch is part of what this step asserts "
        "about the artifact an adopter installs."
    )

    assert any(_launches(argv, "uv", "run") and "pytest" in argv for argv in commands), (
        f"the step never runs pytest from the extracted root: {text!r}"
    )
    assert "continue-on-error" not in step, (
        "continue-on-error makes this step advisory, which is the one thing it must not be: the "
        "drift it catches is invisible to every other gate."
    )
    assert _status_reaches_the_step(text, "uv", "run"), (
        f"pytest's exit status does not become the step's: {text!r}. A trailing `|| true`, a "
        "pipe, or a preceding `set +e` each leave every check above green in front of the "
        "module-scope collection error this gate exists to surface."
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
