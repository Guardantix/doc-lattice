"""doc-lattice launcher, option, and subcommand policy over the shared word IR.

This module owns the deterministic recognition floor for doc-lattice invocations that a shell
scanner discovers in GitHub CI execution sources. It exposes the shared word intermediate
representation (``ScanWord``) and a single pure entry point, ``resolve_command``, that
classifies one command's words into a ``CandidateResolution``.

The dependency direction is policy-first and permanent: the scanner imports this module and
never the reverse, so this module must not import the scanner. The semantics adapt
``shell_scanner.py`` to a deliberately narrower floor; where that reference is more permissive,
this contract wins.
"""

import re
from dataclasses import dataclass
from typing import Literal

from doc_lattice.constants import ScanReasonCategory

CandidateKind = Literal["resolved", "not_candidate", "refused"]

_DOC_LATTICE = "doc-lattice"

# Executable head basenames, matched by casefold. The .exe form mirrors the runtime scanner's
# _is_doc_lattice_executable_basename (shell_scanner.py:2961), which recognizes the Windows
# launcher shim; the floor must resolve the same heads or a marker-bearing .exe launch would
# certify with no invocation. The payload position keeps _DOC_LATTICE and fails closed on .exe
# look-alikes through _looks_like_doc_lattice, so this set is for the head position only.
_DOC_LATTICE_HEADS: frozenset[str] = frozenset({"doc-lattice", "doc-lattice.exe"})

# Value-taking launcher options recognized before the payload, adapted from shell_scanner.py:2606
# and shell_scanner.py:2713. The sets split by launcher form: --from selects the payload package
# for the package-form launchers only (uvx and uv tool run). uv run does not accept --from, and the
# old scanner reports an unresolved uv launcher option there, so the floor refuses it before the
# payload rather than skipping it and certifying a marker-bearing source with no invocation.
# --python and --with are shared by both forms. The --no-sync flag splits the other way: only
# uv run accepts it, and uv rejects it for uvx and uv tool run with an unexpected-argument error
# before any dispatch, so skipping a package-form --no-sync would certify a launch that never runs.
# The old scanner skips it for both forms (shell_scanner.py:214 feeds _UV_TOOL_RUN_FLAGS at
# shell_scanner.py:222), so the floor refuses rather than inheriting that false accept. Only the
# options this contract names are recognized; any other option-like word before the payload fails
# closed.
_PACKAGE_FORM_VALUE_OPTIONS: frozenset[str] = frozenset({"--python", "--from", "--with"})
_COMMAND_FORM_VALUE_OPTIONS: frozenset[str] = frozenset({"--python", "--with"})
_PACKAGE_FORM_FLAG_OPTIONS: frozenset[str] = frozenset()
_COMMAND_FORM_FLAG_OPTIONS: frozenset[str] = frozenset({"--no-sync"})

# A first word whose basename is one of these but whose text is not the bare launcher word is a
# path-form launcher (for example /usr/bin/uvx or ./uv). The old scanner resolves those by
# basename (shell_scanner.py:2178), so policy fails closed rather than leaving a false-safe hole
# where a marker-bearing path-form launch would certify with no invocation.
_LAUNCHER_BASENAMES: frozenset[str] = frozenset({"uvx", "uv"})

# Shell wrappers, re-dispatchers, and timing keywords that can prepend a doc-lattice launch
# through forms the floor grammar does not model (env option runs, exec/command/builtin
# re-dispatch, external time). Matched by basename so both bare (env) and path (/usr/bin/env)
# forms fail closed. Bare unquoted time is refused earlier as a control-flow keyword; this set
# also catches its path form and any quoted spelling that bypasses that earlier gate. The old
# scanner resolves through these; the floor does not grow to match, so it fails closed rather
# than leaving a marker-bearing wrapper launch to certify off-grammar or drop its invocation.
_OFF_FLOOR_DISPATCH: frozenset[str] = frozenset({"command", "exec", "builtin", "env", "time"})

# Root options between the executable and its subcommand, from shell_scanner.py:261-263.
# --no-color is skipped; --help and --version are eager options that resolve with no
# invocation before any subcommand runs.
_ROOT_SKIP_OPTIONS: frozenset[str] = frozenset({"--no-color"})
_ROOT_STOP_OPTIONS: frozenset[str] = frozenset({"--help", "--version"})

# Recognized subcommands. reconcile carries a mutation flag; every other member resolves to a
# non-mutating invocation under the floor.
_SUBCOMMANDS: frozenset[str] = frozenset({"check", "lint", "linear", "ci", "reconcile"})
_RECONCILE_DRY_RUN = "--dry-run"

# PEP 503 distribution normalization (shell_scanner.py:35-37). A requirement-style word's name
# ends at the first character in this set, and its separators collapse before comparison.
_REQUIREMENT_SUFFIX_STARTS: frozenset[str] = frozenset("([<>=!~@;")
_DISTRIBUTION_SEPARATOR_RE = re.compile(r"[-_.]+")


@dataclass(frozen=True, slots=True)
class ScanWord:
    """One normalized shell word in the shared word IR.

    Attributes:
        text: The dequoted, normalized word text. Permitted expansions are kept in raw form,
            so ``--config=$CFG`` has text ``--config=$CFG``.
        start: The source offset of the word's first character.
        end: The source offset one past the word's last character.
        unstable: True when the word contains a permitted expansion or an unquoted glob or
            tilde instability. A fully literal word is stable (``unstable=False``).
    """

    text: str
    start: int
    end: int
    unstable: bool


@dataclass(frozen=True, slots=True)
class CandidateResolution:
    """The policy disposition for one command's words.

    Attributes:
        kind: ``resolved`` for a recognized doc-lattice launch, ``not_candidate`` when the
            command is provably not a doc-lattice invocation, or ``refused`` when a candidate
            cannot be resolved under the floor.
        invocation: The ``(subcommand, mutating)`` pair for a resolved invocation, or None for
            a resolved disposition that records none, such as root ``--help``.
        reason_category: The refusal category; set only when ``kind`` is ``refused``.
        offset: The source offset the refusal is anchored to; set only when ``kind`` is
            ``refused``.
    """

    kind: CandidateKind
    invocation: tuple[str, bool] | None = None
    reason_category: ScanReasonCategory | None = None
    offset: int | None = None


_NOT_CANDIDATE = CandidateResolution("not_candidate")


def _refused(offset: int) -> CandidateResolution:
    """Build a policy-unresolvable refusal anchored at ``offset``."""
    return CandidateResolution("refused", None, "policy-unresolvable", offset)


def _resolved(invocation: tuple[str, bool] | None) -> CandidateResolution:
    """Build a resolved disposition carrying ``invocation`` (None records no invocation)."""
    return CandidateResolution("resolved", invocation)


def resolve_command(words: tuple[ScanWord, ...]) -> CandidateResolution:
    """Classify one command's words into a doc-lattice launcher policy disposition.

    The scanner guarantees the first word is literal (``unstable=False``) before calling, so
    policy never sees an unstable command word. The words are walked left to right: the head
    selects a direct executable or a ``uvx``/``uv`` launcher, launcher options are skipped to
    the payload, root options are skipped to the subcommand, and the subcommand table yields
    the invocation.

    Args:
        words: The command's words in source order. Expected to be non-empty with a literal
            first word.

    Returns:
        The command's ``CandidateResolution``.
    """
    if not words:
        return _NOT_CANDIDATE
    head = words[0].text
    base = _basename(head)
    if _is_doc_lattice_head(base):
        return _resolve_after_executable(words, 1)
    if head == "uvx":
        return _resolve_launcher_payload(words, 1, package_form=True)
    if head == "uv":
        return _resolve_uv(words)
    if base in _OFF_FLOOR_DISPATCH or base in _LAUNCHER_BASENAMES:
        # Fail closed on a shell wrapper, re-dispatcher, or timing keyword (command, exec,
        # builtin, env, time) or a path-form launcher (uvx/uv basename with non-bare text). Each
        # could carry a marker-bearing launch the floor grammar does not model, so none is
        # allowed to certify off-grammar or drop its invocation silently.
        return _refused(words[0].start)
    return _NOT_CANDIDATE


def _resolve_uv(words: tuple[ScanWord, ...]) -> CandidateResolution:
    """Continue only for the literal ``uv run`` and ``uv tool run`` launcher forms.

    An unstable word in the subcommand-selector position (before ``run`` or ``tool run`` is
    established) refuses with ``policy-unresolvable`` at that word's start: the selector could
    expand to ``run`` at runtime, so the launcher form cannot be ruled out and the command
    fails closed, matching the old scanner's incomplete result on a command-position expansion
    (contract point 3's "any unstable word before the payload is established, is refused"). A
    stable option-like selector (a global uv option) fails closed because the floor does not
    model uv's global options; any other stable non-``run``/``tool`` selector is not a candidate
    (contract point 2).
    """
    subcommand = _word_at(words, 1)
    if subcommand is None:
        return _NOT_CANDIDATE
    if subcommand.unstable:
        return _refused(subcommand.start)
    if subcommand.text == "run":
        return _resolve_launcher_payload(words, 2, package_form=False)
    if subcommand.text == "tool":
        return _resolve_uv_tool(words)
    if subcommand.text.startswith("-"):
        # A uv global option before the run or tool selector is outside the floor's launcher
        # forms; the old scanner models these options, so the floor fails closed rather than
        # dropping a launch that hides behind one.
        return _refused(subcommand.start)
    return _NOT_CANDIDATE


def _resolve_uv_tool(words: tuple[ScanWord, ...]) -> CandidateResolution:
    """Resolve the ``run`` selector of ``uv tool run``.

    An unstable selector fails closed, and so does a stable option-like one: uv accepts options
    between ``tool`` and ``run`` (``uv tool -q run`` dispatches to ``uv tool run``), so the floor
    refuses there under contract point 3's option-before-payload rule rather than certifying a
    marker-bearing launch that hides behind the option as a non-candidate with no invocation.
    The old scanner shares that drop; the floor does not inherit it. Any other stable
    non-``run`` selector is a different uv tool subcommand and not a candidate.
    """
    run = _word_at(words, 2)
    if run is None:
        return _NOT_CANDIDATE
    if run.unstable:
        return _refused(run.start)
    if run.text.startswith("-"):
        return _refused(run.start)
    if run.text != "run":
        return _NOT_CANDIDATE
    return _resolve_launcher_payload(words, 3, package_form=True)


def _resolve_launcher_payload(
    words: tuple[ScanWord, ...], start: int, *, package_form: bool
) -> CandidateResolution:
    """Skip launcher options to the payload word and resolve it against doc-lattice.

    Any unstable word or unknown option-like word before the payload is established refuses
    with ``policy-unresolvable`` (contract point 3). A resolved literal payload that cleanly
    names anything other than doc-lattice makes the command a non-candidate (contract point 4).
    A payload that is instead a wrapper, a nested launcher, or a doc-lattice look-alike that
    does not normalize cleanly fails closed: the old scanner resolves through those, and the
    floor refuses rather than dropping the launch they hide.

    Args:
        words: The command's words in source order.
        start: The index of the first word after the launcher head.
        package_form: True when the launcher (uvx or uv tool run) treats the payload as a
            package specification whose version and extras normalize; False when the launcher
            (uv run) launches a literal command matched by basename alone. The form also narrows
            the recognized value options: --from selects the payload package in package form only,
            so uv run refuses a --from before the payload rather than skipping it. The recognized
            flags narrow the other way: only uv run accepts --no-sync, so a package-form --no-sync
            refuses before the payload rather than certifying a launch uv rejects.

    Returns:
        The command's ``CandidateResolution``.
    """
    index = start
    value_options = _PACKAGE_FORM_VALUE_OPTIONS if package_form else _COMMAND_FORM_VALUE_OPTIONS
    flag_options = _PACKAGE_FORM_FLAG_OPTIONS if package_form else _COMMAND_FORM_FLAG_OPTIONS
    while index < len(words):
        word = words[index]
        if word.unstable:
            return _refused(word.start)
        text = word.text
        if text == "":
            break
        option_name = text.split("=", 1)[0]
        if option_name in value_options:
            if "=" in text:
                index += 1
                continue
            value_index = index + 1
            if value_index < len(words) and words[value_index].unstable:
                return _refused(words[value_index].start)
            index = value_index + 1
            continue
        if text in flag_options:
            index += 1
            continue
        if text.startswith("-"):
            return _refused(word.start)
        break
    if index >= len(words):
        return _NOT_CANDIDATE
    return _resolve_payload_word(words, index, package_form)


def _resolve_payload_word(
    words: tuple[ScanWord, ...], index: int, package_form: bool
) -> CandidateResolution:
    """Resolve the established launcher payload word against doc-lattice.

    A clean doc-lattice name continues after the executable. A payload that is a wrapper, a
    nested launcher, or a doc-lattice look-alike fails closed. Any other name is a non-candidate.
    """
    payload = words[index].text
    if _payload_is_doc_lattice(payload, package_form):
        return _resolve_after_executable(words, index + 1)
    if (
        _basename(payload) in _OFF_FLOOR_DISPATCH
        or _basename(payload) in _LAUNCHER_BASENAMES
        or _looks_like_doc_lattice(payload)
    ):
        return _refused(words[index].start)
    return _NOT_CANDIDATE


def _resolve_after_executable(words: tuple[ScanWord, ...], start: int) -> CandidateResolution:
    """Skip root options to the subcommand, honoring eager ``--help`` and ``--version``.

    An unstable word before the subcommand is established refuses with ``policy-unresolvable``
    (contract points 5 and 7), as does any unknown ``-``-prefixed root option. Reaching the
    end of the words with no subcommand resolves with no invocation, mirroring ``--help``.
    """
    index = start
    while index < len(words):
        word = words[index]
        if word.unstable:
            return _refused(word.start)
        text = word.text
        if text in _ROOT_STOP_OPTIONS:
            return _resolved(None)
        if text in _ROOT_SKIP_OPTIONS:
            index += 1
            continue
        if text.startswith("-"):
            return _refused(word.start)
        break
    if index >= len(words):
        return _resolved(None)
    return _resolve_subcommand(words, index)


def _resolve_subcommand(words: tuple[ScanWord, ...], index: int) -> CandidateResolution:
    """Resolve the subcommand word into its invocation or refuse an unknown subcommand.

    An eager ``--help`` or ``--version`` after the subcommand short-circuits execution in ways
    the floor does not model (the old scanner reports help or version, not an invocation), so it
    fails closed. Otherwise reconcile carries its dry-run flag and every other subcommand
    resolves to a non-mutating invocation.
    """
    word = words[index]
    subcommand = word.text
    if subcommand not in _SUBCOMMANDS:
        return _refused(word.start)
    rest = words[index + 1 :]
    eager = _eager_option_offset(rest)
    if eager is not None:
        return _refused(eager)
    if subcommand == "reconcile":
        return _resolve_reconcile(rest)
    dry = _plain_dry_run_offset(rest)
    if dry is not None:
        # --dry-run on a non-reconcile subcommand is a flag the old scanner credits but the
        # floor does not model there, so the floor fails closed rather than diverge on it.
        return _refused(dry)
    # check, lint, linear, and ci all resolve to a non-mutating invocation. ci consumes an
    # optional following literal ``audit`` word, but that consumption is inert here: no
    # trailing argument changes the ci disposition, so the invocation is fixed at (ci, False).
    return _resolved((subcommand, False))


def _resolve_reconcile(rest: tuple[ScanWord, ...]) -> CandidateResolution:
    """Resolve reconcile's dry-run flag over its trailing words (spec D3).

    Option processing stops at the first unstable word or an end-of-options ``--`` marker, so a
    ``--dry-run`` reached only past either is never credited. A ``--dry-run`` immediately
    preceded by a bare long option (one without ``=``) is ambiguous: the old scanner may bind it
    as that option's value, which the floor cannot decide without an option-arity table it does
    not keep, so it fails closed instead of guessing.
    """
    dry = False
    prev_bare_option = False
    for word in rest:
        if word.unstable:
            break
        text = word.text
        if text == "--":
            break
        if text == _RECONCILE_DRY_RUN:
            if prev_bare_option:
                return _refused(word.start)
            dry = True
            prev_bare_option = False
            continue
        prev_bare_option = text.startswith("--") and "=" not in text
    return _resolved(("reconcile", dry))


def _eager_option_offset(rest: tuple[ScanWord, ...]) -> int | None:
    """Return the offset of a bare eager ``--help``/``--version`` after the subcommand.

    The scan stops at the first unstable word or an end-of-options ``--`` marker, mirroring the
    dry-run scan, so an eager option reached only past either is not credited.
    """
    for word in rest:
        if word.unstable:
            return None
        text = word.text
        if text == "--":
            return None
        if text in _ROOT_STOP_OPTIONS:
            return word.start
    return None


def _plain_dry_run_offset(rest: tuple[ScanWord, ...]) -> int | None:
    """Return the offset of a plain ``--dry-run`` after a non-reconcile subcommand, if any.

    The scan stops at the first unstable word or an end-of-options ``--`` marker, matching the
    reconcile and eager-option scans.
    """
    for word in rest:
        if word.unstable:
            return None
        text = word.text
        if text == "--":
            return None
        if text == _RECONCILE_DRY_RUN:
            return word.start
    return None


def _payload_is_doc_lattice(text: str, package_form: bool) -> bool:
    """Return whether a launcher payload names doc-lattice.

    A payload always matches by basename. Only a package-form launcher (uvx or uv tool run)
    additionally accepts a requirement-style distribution spelling, because uv run launches a
    literal command whose name the old scanner never version-strips.
    """
    if _basename(text) == _DOC_LATTICE:
        return True
    return package_form and _is_doc_lattice_distribution(text)


def _is_doc_lattice_distribution(text: str) -> bool:
    """Return whether a requirement-style word normalizes to the doc-lattice distribution."""
    stem = text
    for position, character in enumerate(text):
        if character in _REQUIREMENT_SUFFIX_STARTS:
            stem = text[:position]
            break
    return _DISTRIBUTION_SEPARATOR_RE.sub("-", stem).casefold() == _DOC_LATTICE


def _looks_like_doc_lattice(text: str) -> bool:
    """Return whether a payload resembles doc-lattice without normalizing cleanly.

    A payload that carries the doc-lattice distribution name once its separators are collapsed,
    yet is not accepted by ``_payload_is_doc_lattice`` (surrounding whitespace, an embedded
    version or URL, and so on), is a look-alike the old scanner resolves through. The floor
    fails closed on it rather than dropping the launch it hides.
    """
    return _DOC_LATTICE in _DISTRIBUTION_SEPARATOR_RE.sub("-", text).casefold()


def _is_doc_lattice_head(base: str) -> bool:
    """Return whether a casefolded executable basename names doc-lattice (with or without .exe)."""
    return base.casefold() in _DOC_LATTICE_HEADS


def _basename(text: str) -> str:
    """Return the word text after its last path separator."""
    return text.rsplit("/", 1)[-1]


def _word_at(words: tuple[ScanWord, ...], index: int) -> ScanWord | None:
    """Return the word at ``index``, or None when the command has no such word."""
    return words[index] if index < len(words) else None
