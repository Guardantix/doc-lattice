"""D3 floor-grammar scanner for doc-lattice direct-marker execution sources.

This module owns the marker gate and the conservative allowlist recognizer that certifies one
GitHub CI execution source (a shell template or a ``run:`` body) against the frozen D3 floor
grammar. It exposes exactly one public entry point, ``scan_execution_source``, so templates and
bodies can never drift, plus the frozen ``DIRECT_MARKER_RE`` for the harness and later audit
wiring.

The scanner is a single iterative left-to-right pass with no recursion, bounded by explicit,
predeclared caps and a linear work counter. It consumes the shared word IR and launcher policy
from ``launcher_policy`` and returns a ``BlockScan`` from ``model``; refusal categories are the
``constants.ScanReasonCategory`` domain. The dependency direction is permanent: this module
imports policy, never the reverse.
"""

import re
from dataclasses import dataclass, field

from doc_lattice.constants import ScanReasonCategory
from doc_lattice.github_ci.launcher_policy import CandidateResolution, ScanWord, resolve_command
from doc_lattice.github_ci.model import BlockScan

# Frozen direct marker (spec D2): an ASCII-case-insensitive ``doc[-_.]+lattice`` substring with
# no word boundaries. re.ASCII keeps re.IGNORECASE from also matching Unicode dotted and dotless
# I variants, which would over-match paths that are not doc-lattice.
DIRECT_MARKER_RE = re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)

# Predeclared caps and the linear work limit (spec D3 Architecture, checkpoint limits.json).
_SOURCE_CAP = 1_048_576
_INVOCATION_CAP = 10_000
_TOKEN_CAP = 262_144
_STATEMENT_CAP = 65_536
_WORK_CEILING = 4_194_304
_WORK_SLACK = 4_096

# Refusal categories, named once from the shared domain so raw strings are never restated.
_CONTROL_CHARACTER: ScanReasonCategory = "control-character"
_UNSUPPORTED_OPERATOR: ScanReasonCategory = "unsupported-operator"
_UNSUPPORTED_EXPANSION: ScanReasonCategory = "unsupported-expansion"
_UNQUOTED_EXPANSION_IN_COMMAND_WORD: ScanReasonCategory = "unquoted-expansion-in-command-word"
_QUOTE_SPANS_NEWLINE: ScanReasonCategory = "quote-spans-newline"
_UNTERMINATED_QUOTE: ScanReasonCategory = "unterminated-quote"
_CONTROL_FLOW_KEYWORD: ScanReasonCategory = "control-flow-keyword"
_ASSIGNMENT_PREFIX: ScanReasonCategory = "assignment-prefix"
_UNSTABLE_FIRST_WORD: ScanReasonCategory = "unstable-first-word"
_POLICY_UNRESOLVABLE: ScanReasonCategory = "policy-unresolvable"
_CAP_EXCEEDED: ScanReasonCategory = "cap-exceeded"

_REASON_PHRASE: dict[ScanReasonCategory, str] = {
    _CONTROL_CHARACTER: "disallowed control character",
    _UNSUPPORTED_OPERATOR: "unsupported shell operator",
    _UNSUPPORTED_EXPANSION: "unsupported shell expansion",
    _UNQUOTED_EXPANSION_IN_COMMAND_WORD: "unquoted parameter expansion in a command word",
    _QUOTE_SPANS_NEWLINE: "quoted string spans a newline",
    _UNTERMINATED_QUOTE: "unterminated quoted string",
    _CONTROL_FLOW_KEYWORD: "control-flow keyword in command position",
    _ASSIGNMENT_PREFIX: "assignment prefix precedes a command",
    _UNSTABLE_FIRST_WORD: "non-literal command name",
    _POLICY_UNRESOLVABLE: "unresolvable doc-lattice invocation",
    _CAP_EXCEEDED: "scan resource cap exceeded",
}

# Control-flow keywords in command position and function definitions are refused (spec D3).
_CONTROL_FLOW_KEYWORDS: frozenset[str] = frozenset(
    {
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "do",
        "done",
        "for",
        "in",
        "select",
        "case",
        "esac",
        "function",
        "!",
        "time",
        "coproc",
    }
)

# Characters that end a word without being part of it (whitespace and list or statement
# separators), and characters that are always unsupported operators when unquoted.
_WORD_TERMINATORS: frozenset[str] = frozenset({" ", "\t", "\n", ";", "&", "|"})
_OPERATOR_CHARS: frozenset[str] = frozenset({"<", ">", "(", ")", "`", "\\", "{", "}"})
_GLOB_CHARS: frozenset[str] = frozenset({"*", "?", "[", "]"})

# Both plain (NAME=) and append (NAME+=) assignment prefixes: the old scanner skips either to
# reach the following command, so the floor treats both as an assignment prefix and fails closed
# when one precedes a command.
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_NAME_PARAM_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_BRACE_PARAM_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _is_control(char: str) -> bool:
    """Return whether ``char`` is a refused C0 control character (spec D3 preconditions).

    Carriage return and every C0 code point below space refuse, except newline and tab, which
    are ordinary separators. Non-ASCII code points are never control characters.
    """
    return char < " " and char not in {"\n", "\t"}


def _reason_text(category: ScanReasonCategory, offset: int) -> str:
    """Return the actionable uninspectability reason for ``category`` at ``offset``."""
    return f"{_REASON_PHRASE[category]} at offset {offset}"


@dataclass(frozen=True, slots=True)
class _Refusal:
    """One earliest-offset refusal discovered while scanning a source."""

    offset: int
    category: ScanReasonCategory


@dataclass(frozen=True, slots=True)
class _Word:
    """One normalized word with the metadata the grammar needs beyond the shared IR.

    Attributes:
        text: The dequoted word text with permitted expansions kept raw.
        start: The source offset of the word's first character.
        end: The source offset one past the word's last character.
        unstable: True when the word carries an expansion, an unquoted glob, or a leading tilde.
        is_plain: True when the word is built only from unquoted literal characters.
        unquoted_expansion_offset: The offset of the first unquoted permitted expansion, or None.
    """

    text: str
    start: int
    end: int
    unstable: bool
    is_plain: bool
    unquoted_expansion_offset: int | None


@dataclass(frozen=True, slots=True)
class _CommandEnd:
    """The delimiter that closed one command's word run.

    Attributes:
        words: The command's words in source order.
        list_op: True when the delimiter was ``&&`` or ``||`` (a list join, not a statement end).
        offset: The source offset of the closing delimiter (or end of source).
        semicolon: True when the delimiter was a ``;`` (a statement separator, not a newline). The
            D3 grammar permits a blank line but has no empty-semicolon statement, so an empty
            command closed by ``;`` refuses where one closed by a newline certifies.
    """

    words: tuple[_Word, ...]
    list_op: bool
    offset: int
    semicolon: bool = False


@dataclass
class _WorkCounter:
    """Bounded work meter with the linear D3 limit."""

    limit: int
    used: int = 0

    def charge(self, amount: int = 1) -> bool:
        """Charge ``amount`` units of work; return whether the meter is still within budget."""
        self.used += amount
        return self.used <= self.limit

    def over(self) -> bool:
        """Return whether the accumulated work has passed the limit."""
        return self.used > self.limit


def scan_execution_source(source: str) -> BlockScan:
    """Certify one GitHub CI execution source against the D3 floor grammar.

    The marker gate runs first: a source with no direct marker is not applicable and charges only
    the single marker pass. A marker anywhere, including comments and quoted data, proceeds to the
    grammar. Sources longer than the source cap are uninspectable before scanning. Otherwise the
    source is scanned in one iterative left-to-right pass and the accumulated ``BlockScan`` is
    returned, retaining every invocation proven before any refusal (monotonic evidence).

    Args:
        source: The raw execution source text (a shell template or ``run:`` body).

    Returns:
        The source's ``BlockScan`` certification outcome.
    """
    if DIRECT_MARKER_RE.search(source) is None:
        return BlockScan("not_applicable", (), None, None, None, len(source))
    if len(source) > _SOURCE_CAP:
        return BlockScan(
            "uninspectable", (), _CAP_EXCEEDED, _reason_text(_CAP_EXCEEDED, 0), 0, len(source)
        )
    return _Scanner(source).run()


def certified_command_words(source: str) -> tuple[tuple[str, ...], ...]:
    """Expose the command structure of a certified source for the gate 7 differential.

    Pure observability with no effect on ``scan_execution_source``. When the source certifies,
    the scanner's tokenizer is rerun and every command's dequoted word texts are returned, one
    tuple per command in source order, with one entry per ``&&``/``||`` list arm. Candidate and
    non-candidate commands alike are reported; assignment-only statements contribute nothing,
    matching the recognizer, which reports only commands. A source that does not certify yields
    ``()`` and no structure is exposed.

    Args:
        source: The raw execution source text (a shell template or ``run:`` body).

    Returns:
        One tuple of dequoted word texts per command in source order, or ``()`` when the source
        is not certified.
    """
    if scan_execution_source(source).status != "certified":
        return ()
    return _Scanner(source).command_words()


@dataclass
class _Scanner:
    """Iterative single-pass recognizer state for one execution source."""

    source: str
    pos: int = 0
    invocations: list[tuple[str, bool]] = field(default_factory=list)
    token_count: int = 0
    statement_count: int = 0
    invocation_count: int = 0
    after_list_op: int | None = None
    work: _WorkCounter = field(init=False)

    def __post_init__(self) -> None:
        """Size the work meter from the source length and charge the marker pass."""
        length = len(self.source)
        self.work = _WorkCounter(min(_WORK_CEILING, 4 * length + _WORK_SLACK))
        self.work.charge(length)

    def run(self) -> BlockScan:
        """Scan every statement left to right, returning the accumulated ``BlockScan``."""
        length = len(self.source)
        while self.pos < length:
            outcome = self._scan_command()
            if isinstance(outcome, _Refusal):
                return self._uninspectable(outcome)
            refusal = self._commit_command(outcome)
            if refusal is not None:
                return self._uninspectable(refusal)
        if self.after_list_op is not None:
            # The source ended with a && or || still pending and no right-hand command ever
            # arrived; bash -n rejects the dangling operator. This is the single point that refuses
            # every such variant (immediate end of source, trailing whitespace, or trailing blank
            # and comment continuation lines), because a newline after the operator continues the
            # list rather than closing it. Refuse at the operator offset and keep every invocation
            # proven before it (monotonic evidence).
            return self._uninspectable(_Refusal(self.after_list_op, _UNSUPPORTED_OPERATOR))
        return BlockScan("certified", tuple(self.invocations), None, None, None, self.work.used)

    def command_words(self) -> tuple[tuple[str, ...], ...]:
        """Re-scan a certified source, returning each command's dequoted words in source order.

        Assignment-only statements emit nothing; every other command emits its dequoted word
        texts. The caller certifies the source before calling, so no refusal is reachable; an
        unexpected one collapses to ``()`` rather than exposing partial structure.
        """
        commands: list[tuple[str, ...]] = []
        length = len(self.source)
        while self.pos < length:
            outcome = self._scan_command()
            if isinstance(outcome, _Refusal):
                return ()
            if outcome.words and not self._is_assignment_only(outcome.words):
                commands.append(tuple(word.text for word in outcome.words))
        return tuple(commands)

    def _is_assignment_only(self, words: tuple[_Word, ...]) -> bool:
        """Return whether a command is a single assignment word (which is not a command)."""
        return len(words) == 1 and _ASSIGNMENT_RE.match(self.source, words[0].start) is not None

    def _commit_command(self, outcome: _CommandEnd) -> _Refusal | None:  # noqa: PLR0911
        """Resolve one closed command, flush evidence, and advance list or statement state."""
        if outcome.words:
            refusal = self._resolve_and_flush(outcome.words)
            if refusal is not None:
                return refusal
        elif self.after_list_op is not None:
            if outcome.list_op or outcome.semicolon:
                # A pending operator's missing right-hand command is the earlier failure, so it
                # outranks a second list operator or the semicolon that closes this same empty
                # command, consistent with the ``&& ;`` and end-of-source refusals that also anchor
                # at the pending operator.
                return _Refusal(self.after_list_op, _UNSUPPORTED_OPERATOR)
            # A newline after a pending ``&&`` or ``||`` continues the list: bash reads the
            # right-hand command from the following lines, past blank and comment lines, so the
            # operator stays pending and no statement closes. A source that ends before that command
            # arrives is refused by ``run``'s dangling check at the operator offset.
            return None
        elif outcome.list_op:
            return _Refusal(outcome.offset, _UNSUPPORTED_OPERATOR)
        elif outcome.semicolon:
            # An empty statement closed by ``;`` (a leading ``;``, ``cmd;;``, or a lone ``;`` line)
            # has no D3 production and bash rejects it. This branch follows the after_list_op one
            # so ``cmd && ;`` still anchors at the earlier operator, not the semicolon.
            return _Refusal(outcome.offset, _UNSUPPORTED_OPERATOR)
        if outcome.list_op:
            self.after_list_op = outcome.offset
            return None
        self.after_list_op = None
        return self._close_statement()

    def _scan_command(self) -> _CommandEnd | _Refusal:
        """Read one command, reporting the earliest command-level or lexical refusal (spec D4).

        The lexical scan retains the words already read when it refuses mid-command, including the
        partial word a mid-word refusal interrupts, and the command those words form may fail the
        grammar or policy at an earlier source offset. The earliest failure by offset wins; a tie
        keeps the lexical refusal and its category, so a control-flow keyword head (refused
        lexically at its own offset) ties the policy refusal a time-style head would raise there
        and stays control-flow-keyword. The command-level refusal is derived purely, so it never
        charges the work meter or flushes an invocation.
        """
        words: list[_Word] = []
        outcome = self._scan_command_words(words)
        if isinstance(outcome, _Refusal) and words:
            command_refusal = self._command_refusal(tuple(words))
            if command_refusal is not None and command_refusal.offset < outcome.offset:
                return command_refusal
        return outcome

    def _scan_command_words(self, words: list[_Word]) -> _CommandEnd | _Refusal:  # noqa: PLR0911
        """Read one command's words up to the next list or statement delimiter or source end."""
        length = len(self.source)
        while self.pos < length:
            if self.work.over():
                return _Refusal(self.pos, _CAP_EXCEEDED)
            char = self.source[self.pos]
            if _is_control(char):
                return _Refusal(self.pos, _CONTROL_CHARACTER)
            if char in " \t":
                self._advance()
                continue
            delimiter = self._read_delimiter(char, words)
            if delimiter is not None:
                return delimiter
            if char == "#":
                comment = self._consume_comment()
                if comment is not None:
                    return comment
                continue
            if char in _OPERATOR_CHARS:
                return _Refusal(self.pos, _UNSUPPORTED_OPERATOR)
            refusal = self._read_word(words)
            if refusal is not None:
                return refusal
            word = words[-1]
            if len(words) == 1 and word.is_plain and word.text in _CONTROL_FLOW_KEYWORDS:
                # A control-flow keyword in command position refuses at the keyword itself, which
                # is earlier than any operator the keyword's own syntax (for example the ``)`` of
                # a ``case`` arm) would otherwise surface first.
                return _Refusal(word.start, _CONTROL_FLOW_KEYWORD)
            token_refusal = self._emit_token()
            if token_refusal is not None:
                return token_refusal
        return _CommandEnd(tuple(words), False, self.pos)

    def _read_delimiter(self, char: str, words: list[_Word]) -> _CommandEnd | _Refusal | None:
        """Classify a statement or list delimiter at the cursor, or None for a word start.

        The ``words`` list is materialized into a tuple only in the branches that construct a
        ``_CommandEnd``, so a word-start call (the common case) never copies it. This keeps
        tokenization linear in word count rather than quadratic.
        """
        if char in "\n;":
            offset = self.pos
            self._advance()
            return _CommandEnd(tuple(words), False, offset, semicolon=char == ";")
        if char == "&":
            return self._read_pair(words, "&")
        if char == "|":
            return self._read_pair(words, "|")
        return None

    def _read_pair(self, words: list[_Word], char: str) -> _CommandEnd | _Refusal:
        """Accept a doubled ``&&`` or ``||`` list operator; refuse the single form."""
        offset = self.pos
        if self._peek(1) == char:
            self._advance_to(offset + 2)
            return _CommandEnd(tuple(words), True, offset)
        return _Refusal(offset, _UNSUPPORTED_OPERATOR)

    def _read_word(self, words: list[_Word]) -> _Refusal | None:  # noqa: PLR0912, PLR0915
        """Read one maximal word and append it, or the partial word before a refusal, to ``words``.

        The word is built from literals, quoted strings, and permitted expansions. On success the
        completed word is appended and None is returned. On a mid-word lexical refusal the partial
        word read before it is still appended, unless no character was consumed, and the refusal is
        returned, so ``_scan_command``'s command-level pass can anchor an earlier grammar or policy
        failure inside that partial word (spec D4).
        """
        start = self.pos
        length = len(self.source)
        parts: list[str] = []
        quoted = False
        has_expansion = False
        glob_unstable = False
        unquoted_expansion_offset: int | None = None
        refusal: _Refusal | None = None
        while self.pos < length:
            if self.work.over():
                refusal = _Refusal(self.pos, _CAP_EXCEEDED)
                break
            char = self.source[self.pos]
            if _is_control(char):
                refusal = _Refusal(self.pos, _CONTROL_CHARACTER)
                break
            if char in _WORD_TERMINATORS:
                break
            if char == "#" or char in _OPERATOR_CHARS:
                refusal = _Refusal(self.pos, _UNSUPPORTED_OPERATOR)
                break
            if char == "'":
                segment = self._read_single_quote()
                if isinstance(segment, _Refusal):
                    refusal = segment
                    break
                parts.append(segment)
                quoted = True
            elif char == '"':
                segment = self._read_double_quote()
                if isinstance(segment, _Refusal):
                    refusal = segment
                    break
                text, saw_expansion = segment
                parts.append(text)
                quoted = True
                has_expansion = has_expansion or saw_expansion
            elif char == "$":
                dollar = self.pos
                expansion = self._read_expansion()
                if isinstance(expansion, _Refusal):
                    refusal = expansion
                    break
                parts.append(expansion)
                has_expansion = True
                if unquoted_expansion_offset is None:
                    unquoted_expansion_offset = dollar
            else:
                glob_unstable = glob_unstable or self._is_glob_char(char, start)
                parts.append(char)
                self._advance()
        if refusal is not None and self.pos == start:
            return refusal
        text = "".join(parts)
        if text in {"[", "]"}:
            glob_unstable = False
        words.append(
            _Word(
                text,
                start,
                self.pos,
                has_expansion or glob_unstable,
                not quoted and not has_expansion,
                unquoted_expansion_offset,
            )
        )
        return refusal

    def _is_glob_char(self, char: str, start: int) -> bool:
        """Return whether an unquoted literal ``char`` makes the word glob- or tilde-unstable."""
        if char in _GLOB_CHARS:
            return True
        return char == "~" and self.pos == start

    def _read_single_quote(self) -> str | _Refusal:
        """Read a single-quoted string; its content is inert literal text."""
        open_offset = self.pos
        self._advance()
        length = len(self.source)
        buffer: list[str] = []
        while self.pos < length:
            if self.work.over():
                return _Refusal(self.pos, _CAP_EXCEEDED)
            char = self.source[self.pos]
            if char == "'":
                self._advance()
                return "".join(buffer)
            if char == "\n":
                return _Refusal(self.pos, _QUOTE_SPANS_NEWLINE)
            if _is_control(char):
                return _Refusal(self.pos, _CONTROL_CHARACTER)
            buffer.append(char)
            self._advance()
        return _Refusal(open_offset, _UNTERMINATED_QUOTE)

    def _read_double_quote(self) -> tuple[str, bool] | _Refusal:  # noqa: PLR0911
        """Read a double-quoted string of literals and permitted parameter forms."""
        open_offset = self.pos
        self._advance()
        length = len(self.source)
        buffer: list[str] = []
        saw_expansion = False
        while self.pos < length:
            if self.work.over():
                return _Refusal(self.pos, _CAP_EXCEEDED)
            char = self.source[self.pos]
            if char == '"':
                self._advance()
                return "".join(buffer), saw_expansion
            if char == "\n":
                return _Refusal(self.pos, _QUOTE_SPANS_NEWLINE)
            if _is_control(char):
                return _Refusal(self.pos, _CONTROL_CHARACTER)
            if char in "\\`":
                return _Refusal(self.pos, _UNSUPPORTED_OPERATOR)
            if char == "$":
                expansion = self._read_expansion()
                if isinstance(expansion, _Refusal):
                    return expansion
                buffer.append(expansion)
                saw_expansion = True
                continue
            buffer.append(char)
            self._advance()
        return _Refusal(open_offset, _UNTERMINATED_QUOTE)

    def _read_expansion(self) -> str | _Refusal:
        """Read a permitted ``$?``, ``$NAME``, or ``${NAME}`` form; refuse every other ``$``."""
        dollar = self.pos
        following = self._peek(1)
        if following == "?":
            self._advance_to(dollar + 2)
            return "$?"
        if following == "{":
            brace = _BRACE_PARAM_RE.match(self.source, dollar)
            if brace is None:
                return _Refusal(dollar, _UNSUPPORTED_EXPANSION)
            self._advance_to(brace.end())
            return brace.group()
        name = _NAME_PARAM_RE.match(self.source, dollar)
        if name is None:
            return _Refusal(dollar, _UNSUPPORTED_EXPANSION)
        self._advance_to(name.end())
        return name.group()

    def _consume_comment(self) -> _Refusal | None:
        """Consume an unquoted comment to end of line, refusing any control character in it."""
        length = len(self.source)
        while self.pos < length:
            char = self.source[self.pos]
            if char == "\n":
                return None
            if _is_control(char):
                return _Refusal(self.pos, _CONTROL_CHARACTER)
            self._advance()
        return None

    def _resolve_and_flush(self, words: tuple[_Word, ...]) -> _Refusal | None:
        """Apply the command-level grammar and policy, charging work and flushing an invocation."""
        refusal = self._precheck_command_refusal(words)
        if refusal is not None:
            return refusal
        if self._is_assignment_only(words):
            return None
        scan_words = tuple(ScanWord(w.text, w.start, w.end, w.unstable) for w in words)
        if not self.work.charge(len(scan_words)):
            return _Refusal(words[0].start, _CAP_EXCEEDED)
        return self._apply_resolution(resolve_command(scan_words), words[0].start)

    def _command_refusal(self, words: tuple[_Word, ...]) -> _Refusal | None:
        """Derive one command's grammar-or-policy refusal without charging or flushing (spec D4).

        Pure counterpart to _resolve_and_flush that _scan_command uses to surface a command-level
        failure preceding a later lexical refusal. It shares the same prechecks and policy-refusal
        mapping, but never charges the work meter, appends an invocation, or touches the invocation
        cap, so the accounting is identical whether or not this path runs.
        """
        refusal = self._precheck_command_refusal(words)
        if refusal is not None:
            return refusal
        if self._is_assignment_only(words):
            return None
        scan_words = tuple(ScanWord(w.text, w.start, w.end, w.unstable) for w in words)
        return self._resolution_refusal(resolve_command(scan_words), words[0].start)

    def _precheck_command_refusal(self, words: tuple[_Word, ...]) -> _Refusal | None:
        """Return the earliest grammar refusal shared by both command-level paths, or None.

        The checks are the assignment prefix (a NAME= prefix before a command word), an unstable
        first word, then an unquoted expansion in any command word. A lone assignment word is not
        a command and returns None here, deferring to each caller's assignment-only guard.
        _scan_command refuses a control-flow keyword head before a command closes, so the flushing
        path never sees one; the pure path can, and there the keyword ties its own policy refusal
        on offset (see _scan_command) and keeps the lexical control-flow-keyword category.
        """
        first = words[0]
        if _ASSIGNMENT_RE.match(self.source, first.start) is not None:
            if len(words) > 1:
                return _Refusal(first.start, _ASSIGNMENT_PREFIX)
            return None
        if first.unstable:
            return _Refusal(first.start, _UNSTABLE_FIRST_WORD)
        for word in words:
            if word.unquoted_expansion_offset is not None:
                return _Refusal(word.unquoted_expansion_offset, _UNQUOTED_EXPANSION_IN_COMMAND_WORD)
        return None

    def _resolution_refusal(self, resolution: CandidateResolution, anchor: int) -> _Refusal | None:
        """Map a refused policy disposition to its _Refusal; None for resolved or non-candidate."""
        if resolution.kind == "refused":
            category = resolution.reason_category
            return _Refusal(
                resolution.offset if resolution.offset is not None else anchor,
                category if category is not None else _POLICY_UNRESOLVABLE,
            )
        return None

    def _apply_resolution(self, resolution: CandidateResolution, anchor: int) -> _Refusal | None:
        """Fold one policy disposition into the accumulated evidence, flushing a resolved one."""
        refusal = self._resolution_refusal(resolution, anchor)
        if refusal is not None:
            return refusal
        if resolution.kind == "resolved" and resolution.invocation is not None:
            self.invocation_count += 1
            if self.invocation_count > _INVOCATION_CAP:
                return _Refusal(anchor, _CAP_EXCEEDED)
            self.invocations.append(resolution.invocation)
        return None

    def _emit_token(self) -> _Refusal | None:
        """Charge one emitted token and enforce the token and work caps."""
        self.token_count += 1
        if self.token_count > _TOKEN_CAP or not self.work.charge():
            return _Refusal(self.pos, _CAP_EXCEEDED)
        return None

    def _close_statement(self) -> _Refusal | None:
        """Charge one closed statement and enforce the statement and work caps."""
        self.statement_count += 1
        if self.statement_count > _STATEMENT_CAP or not self.work.charge():
            return _Refusal(self.pos, _CAP_EXCEEDED)
        return None

    def _uninspectable(self, refusal: _Refusal) -> BlockScan:
        """Build the uninspectable result, retaining every invocation proven so far."""
        return BlockScan(
            "uninspectable",
            tuple(self.invocations),
            refusal.category,
            _reason_text(refusal.category, refusal.offset),
            refusal.offset,
            self.work.used,
        )

    def _peek(self, ahead: int) -> str:
        """Return the character ``ahead`` positions past the cursor, or empty at source end."""
        index = self.pos + ahead
        return self.source[index] if index < len(self.source) else ""

    def _advance(self) -> None:
        """Consume one character, charging one unit of work."""
        self.pos += 1
        self.work.charge()

    def _advance_to(self, target: int) -> None:
        """Consume characters up to ``target``, charging one unit of work per character."""
        self.work.charge(target - self.pos)
        self.pos = target
