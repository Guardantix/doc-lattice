"""Round-trip fuzz gate for the reconcile frontmatter rewriter's declared subset (AD-31).

The properties here generate documents from the AD-31 layer 2 matrix by writable position and
by load phase, rather than from a flat bag of YAML features, because support is conditional on
both. Family 1 generates only the strict tracked-document column plus the layer 2a envelope and
demands a correct rewrite. Family 2 generates the layer 5 outcomes at the exact scope each one
is guaranteed at, and accepts a safe outcome union for the defensive reread column. Family 3
pins the two ordered-map behaviors AD-31 assigns to this gate as current bounded behavior.

Every expectation is computed from the generated model, never from production planning code, so
a rewriter defect cannot make its own oracle agree with it. The one helper that shares a rule
with production is ``_document_ending``, which classifies the rewritten output where production
classifies the input, and its docstring records why that is not a mirror. No property asserts
anything
stricter than AD-31: the mutation footprint is checked against the allowances the model predicts
for the shape it generated, never against a universal one-line diff, and syntax outside layer 2
is never required to be refused.
"""

import re
from collections import Counter
from dataclasses import KW_ONLY, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from ruamel.yaml.error import ReusedAnchorWarning

from doc_lattice import frontmatter_parser
from doc_lattice.error_types import FrontmatterError, ProjectError, UnreadableDocError
from doc_lattice.frontmatter_parser import FrontmatterParts, parse_meta, split_frontmatter_parts
from doc_lattice.hashing import normalize_newlines
from doc_lattice.reconcile import Rewrite, apply_reconcile, plan_rewrites

DOC = Path("doc.md")
BOM = chr(0xFEFF)
# The spacing every flow carrier in this file writes between two entries. It is named so the
# generators do not each spell their own and the rendered documents stay comparable by eye. The
# flow-line assertion does not depend on the value: it recovers the carrier's own source by
# cutting the entries it knows out of the line it rendered, so whatever sits between them comes
# back as literal text. Changing this is a readability change, not a coverage one.
FLOW_SEPARATOR = ", "
# The characters that punctuate a flow collection. A rewrite that adds or drops one of these
# outside the value it was asked to write has restyled source layer 3 says it may not touch,
# even where the document still loads as the very same mapping.
FLOW_INDICATORS = ",{}[]"
# The characters YAML separates nodes inside a flow collection with. Both are whitespace the
# loader discards, so an edit that swapped one for the other, or wrote either where the author
# wrote none, changes source without changing the document.
FLOW_SEPARATION = " \t"
# The flow indicators each layer 4 edit legitimately writes into the entry it lands in. An edit
# that lands on a value already written, or just after a separator the source already carries,
# adds none. An appended pair writes the separator in front of it. An appended sequence item
# writes its own braces as well, and lands inside the sequence bracket rather than the mapping
# one, so it is the only kind that leaves a single bracket rather than the whole run behind it.
FLOW_EDIT_INDICATORS = {"none": "", "separator": ",", "item": ",{}"}

# One named settings object per this file, applied as a decorator to each property. A registered
# profile would be suite-global and would retune the Hypothesis tests other modules already own.
# derandomize keeps CI reproducible without a fixed seed, and the deadline is off because one
# example parses YAML several times.
FUZZ_SETTINGS = settings(max_examples=300, derandomize=True, deadline=None)

# A reused anchor name is a supported spelling under the pure parser, so the warning it raises is
# expected output of the shapes that write one rather than a fault worth surfacing in the summary.
# It is silenced per test rather than repo-wide, and the warning is itself asserted by
# ``test_the_pure_reread_warns_about_a_reused_anchor_name``, so nothing is hidden by silencing it.
# Both reads that see this shape now run on the pure parser, so every test marked here raises it
# on every leg rather than only where the optional accelerator is absent. What still raises it is
# the reread: the strict boundary captures ruamel's warning and re-reports the fact through
# ``orchestrate`` so a warm cache replays it (AD-29), while ``reconcile`` builds its own loaders
# per AD-26 and lets the original escape.
EXPECT_REUSED_ANCHOR = pytest.mark.filterwarnings("ignore::ruamel.yaml.error.ReusedAnchorWarning")

# How many times each conditional claim actually fired this session, counted where it fires
# rather than where it is attempted. The table floor further down keeps a spelling in the corpus
# for each of these; this keeps the claim itself from going quiet for some other reason, a
# generator that stopped producing the shape or a model field that stopped being set. Measured
# vacuity is high and legitimate: most documents carry no comment and most entries are not flow.
_CLAIMS: Counter[str] = Counter()
_REQUIRED_CLAIMS = (
    "comment-at-its-own-site",
    "control-value-refused",
    "flow-line",
    "member-head",
    "relocation",
    "relocation-drop",
    "recovery-arm",
)


# --------------------------------------------------------------------------------------------
# The typed syntax model: spellings by writable position, and the documents built from them.
# --------------------------------------------------------------------------------------------

# The vocabularies the model is written in, spelled the way `constants.py` spells a shared string
# domain. They stay here rather than there because they have no production counterpart: the rule
# exists to stop a production domain being duplicated, and moving a test vocabulary into the
# engine's constants would invert the ownership it protects.
#
# What each one buys is a table row that cannot quietly mean nothing. A misspelled value used to
# fall through to a default that most spellings load to anyway, so the row passed while asserting
# something other than what it declared.
SeenKind = Literal["old", "old-newline", "old-blank-line", "null", "empty-string"]
SEEN_KINDS = frozenset(get_args(SeenKind))

FlowEditKind = Literal["none", "separator", "item"]
FLOW_EDIT_KINDS = frozenset(get_args(FlowEditKind))

# "mixed" is how a document was written, not an ending it can be restored to; `_document_ending`
# answers with it and `_with_ending` writes it, while production's own rule has three values and
# normalizes the fourth away. The extra value is what keeps the two from being mistaken for one.
EnvelopeEnding = Literal["\n", "\r\n", "\r", "mixed"]
ENVELOPE_ENDINGS = frozenset(get_args(EnvelopeEnding))


@dataclass(frozen=True, slots=True)
class RefForm:
    """One supported spelling of an entry's ``ref`` member and the layout it leaves behind.

    Attributes:
        name: Identifier used in failure messages.
        templates: Entry lines up to and including ``ref``, the first opening with ``- ``.
        key_indent: Column the entry's own keys sit at, relative to the sequence item.
        split: Whether the ref is written across two lines. A plain, double-quoted or folded
            scalar all fold the break into a single space, so the value is the two halves
            joined by one.
        trailing: What the spelling's chomping indicator leaves after the last line, which is
            a newline for a clipped block scalar and nothing for a stripped one.
        note: Whether the spelling carries a trailing comment.
        site: Text to locate the entry by, for a spelling whose source does not write its ref
            value literally. It is a template like the others and defaults to the ref itself,
            which every spelling but an escaped one spells out somewhere in its source.
    """

    name: str
    templates: tuple[str, ...]
    key_indent: int
    split: bool
    trailing: str
    note: bool
    _: KW_ONLY
    site: str | None = None


@dataclass(frozen=True, slots=True)
class SeenForm:
    """One supported spelling of an entry's ``seen`` member.

    Attributes:
        name: Identifier used in failure messages.
        templates: Member lines, the first at the entry's key column and every continuation
            line already carrying the extra indentation it needs.
        value: How the loaded value relates to the written text: ``"old"``, ``"old-newline"``
            for a block scalar whose chomping retains the final line break,
            ``"old-blank-line"`` for a keep-chomped one that retains a trailing blank line as
            well, or ``"null"``.
        present: Whether a ``seen`` member is written at all.
        anchored: Whether the member anchors its value, which makes relocation possible.
        note: Whether the spelling carries a comment. Its text differs from the one a ``ref``
            spelling writes, because a ``seen`` comment sits inside the allowed footprint while
            a ``ref`` comment does not: sharing one text would let the pinned copy satisfy the
            layer 3 assertion for a rewrite that dropped the copy actually at risk.
        written_lines: How many lines the member is written on once an edit has landed on it.
            Every value a rewrite writes is a one-line plain scalar, so this is one line for
            most spellings and two for the ones carrying source above the value that survives
            the edit: an explicit key on its own line, and a comment the author left between
            the key and the value.
        appends_own_line: Whether a rewrite of this spelling writes a line just past the member
            rather than over it, which an explicit key with no ``:`` needs because there is no
            value written for the hash to replace. A present member otherwise writes in place.
    """

    name: str
    templates: tuple[str, ...]
    value: SeenKind
    present: bool
    anchored: bool
    note: bool
    written_lines: int = 1
    appends_own_line: bool = False


@dataclass(frozen=True, slots=True)
class WholeForm:
    """One supported spelling of a whole entry, written as its own lines or inline.

    Attributes:
        name: Identifier used in failure messages.
        templates: Entry lines for a block context, or a single inline spelling when
            ``inline`` is set.
        inline: The one-line flow spelling, or None for a block-only form.
        value: How the loaded ``seen`` relates to the written text, as for ``SeenForm``.
        present: Whether a ``seen`` member is written at all.
        anchored: Whether the entry anchors its ``seen`` value.
        writes: Which ``FLOW_EDIT_INDICATORS`` kind a rewrite of this flow spelling is, meaning
            the punctuation its edit is allowed to write. Meaningless for a block-only form,
            which has no flow source to pin.
        written_lines: How many lines the ``seen`` member is written on once an edit has landed
            on it, as for ``SeenForm``. A flow form is always one, since the whole entry shares
            a line however its edit lands.
        appends_own_line: As for ``SeenForm``, and read only on the block branch. A flow entry
            is one line however its member is written, so its append lands inside the bracket
            rather than on a line of its own and this says nothing about it.
    """

    name: str
    templates: tuple[str, ...]
    inline: str | None
    value: SeenKind
    present: bool
    anchored: bool
    writes: FlowEditKind = "none"
    written_lines: int = 1
    appends_own_line: bool = False


@dataclass(frozen=True, slots=True)
class FlowPin:
    """What an edited flow entry's own source still has to look like afterwards.

    Attributes:
        pattern: The entry's source as a regular expression whose one group is the region the
            edit lands in, bounded by the source before it, up to and including the ``seen``
            key an edit writes past, and by the brackets the entry closes on after it.
        budget: Every flow indicator that region is allowed to hold once the edit has landed,
            as one character per occurrence. It is the punctuation the spelling already wrote
            there plus the punctuation the edit itself has to write, so an inert separator or
            a restyled bracket is over budget even where the document still loads the same.
    """

    pattern: str
    budget: str


@dataclass(frozen=True, slots=True)
class Entry:
    """One generated ``derives_from`` entry: its source, and what the loader makes of it.

    Attributes:
        name: The spellings this entry combines, for failure messages.
        lines: Block-context source lines, the first opening with ``- ``.
        inline: The flow spelling when the entry has one, for a flow-carrier sequence.
        ref: The ref string the entry loads as.
        seen: The ``seen`` value the entry loads as.
        present: Whether the entry carries a ``seen`` member before any rewrite.
        anchor: The anchor name written on the ``seen`` value, when it carries one.
        notes: Comment texts written inside the entry.
        extras: Members beyond ``ref`` and ``seen``, which only the fresh reread tolerates.
        marker: The opening of the entry's source, up to the point an allowed edit may reach,
            carrying the punctuation that tells its collection style apart: a block entry
            keeps its sequence dash, a flow entry keeps the bracket it opens on. Layer 3 keeps
            the style the author wrote, so this text has to come back verbatim. It is None for
            an entry whose whole site layer 4 may replace, which is what an alias site
            expansion does.
        edits: Offsets into ``lines`` a rewrite of this entry may change, which for most
            spellings is the ``seen`` member and nothing else. None means the shape's layer 4
            mutation families genuinely spread across the entry, so the whole span stays
            allowed rather than being modelled line by line.
        appends: Whether a rewrite may insert a line just past the entry, which is how a
            missing ``seen`` pair and the ``:`` an explicit ``? seen`` lacks are written.
        pin: For a flow entry, what its source still has to look like once it is edited. None
            for a block entry, whose source the line footprint pins instead.
        site: Text that occurs in this entry's source and nowhere else in the document, which
            is what locates the entry in the rewritten block so a claim about its own source is
            read inside it rather than anywhere in the frontmatter: where its comments came
            back, and what the member an edit landed on still opens with. It is None for an
            entry no such claim is made about.
        written_lines: How many lines this entry's ``seen`` member is written on once an edit
            has landed on it, which is what says how far a member the rewrite shrank is allowed
            to have pulled the rest of the block up. None means the model makes no line-count
            claim for this entry, which covers two cases. One is a shape whose layer 4 mutation
            families spread rather than replacing one member, the case ``edits`` being None also
            marks, where how many lines the edit settles on is the rewriter's to choose. The
            other is a reread-only shape, which is asserted only through the safe-outcome union
            and never has its block length read at all.
        inherits: The ref of the entry this one takes its ``seen`` from through a merge key,
            when it spells none of its own. Writing a hash at that entry changes this one too,
            with nothing written here, so the semantic oracle has to expect it. None for an
            entry whose ``seen`` follows nothing but its own updates.
        displaced: The exact source an anchored value being replaced has to be re-emitted as at
            the alias site it is relocated onto. None makes no relocation claim, which is the
            default because the rule is not the same for every anchor: a reused name rebinds,
            so the first alias below the definition is the wrong answer there. This is the one
            layer 4 claim the semantic oracle cannot make, since it compares loaded values and
            a relocated ``&name ~`` reloads exactly as ``&name null`` does, and a multi-line
            tagged scalar re-emitted without its quoting reloads as the number it always was.
    """

    name: str
    lines: tuple[str, ...]
    inline: str | None
    ref: str
    seen: object
    present: bool
    anchor: str | None
    notes: tuple[str, ...]
    extras: tuple[tuple[str, object], ...] = ()
    # Everything above describes what the entry is and reads clearly in order. Everything below
    # is footprint modelling, and it holds two adjacent `str | None` fields, `marker` and `site`,
    # that feed different assertions and were listed in the wrong order in this docstring until
    # now. Passing those by position is how a reader following the docstring silently swaps two
    # live fields with no type error, so from here they are keyword-only.
    _: KW_ONLY
    marker: str | None = None
    edits: tuple[int, ...] | None = None
    appends: bool = False
    pin: FlowPin | None = None
    site: str | None = None
    written_lines: int | None = None
    inherits: str | None = None
    displaced: str | None = None


@dataclass(frozen=True, slots=True)
class Envelope:
    """The lexical envelope of layer 2a, chosen independently of the frontmatter structure.

    Attributes:
        boms: Length of the byte-order-mark run before the opening fence.
        open_fence: The opening fence line as written, its surrounding space included.
        close_fence: The closing fence line as written.
        trailing_newline: Whether a newline follows the closing fence.
        body: Everything after that newline.
        ending: ``"\\n"``, ``"\\r\\n"``, ``"\\r"``, or ``"mixed"``.
        directive: The ``%YAML`` version the block declares, or None.
    """

    boms: int
    open_fence: str
    close_fence: str
    trailing_newline: bool
    body: str
    ending: EnvelopeEnding
    directive: str | None


@dataclass(frozen=True, slots=True)
class Document:
    """A generated document, its independent semantics, and the footprint a rewrite may take.

    Attributes:
        meta_lines: The frontmatter block between the fences, one string per line.
        entries: The generated entries, in ``derives_from`` order.
        spans: Half-open ``meta_lines`` ranges each entry occupies.
        root: The root members other than ``derives_from``, as the loader builds them.
        key_order: The root key order the reload has to show, or None where no order claim is
            made: a merge makes the order a loader detail rather than a preservation claim, and
            a reread-only shape is outside layer 3, which is what makes the order a claim.
        envelope: The layer 2a choices this document was rendered with.
        notes: Every comment text written in the frontmatter.
        mirrors: Root keys the loader gives the very same list object as ``derives_from``,
            which an alias spelling produces and which therefore follow every update.
    """

    meta_lines: tuple[str, ...]
    entries: tuple[Entry, ...]
    spans: tuple[tuple[int, int], ...]
    root: dict[str, object]
    key_order: tuple[str, ...] | None
    envelope: Envelope
    notes: tuple[str, ...]
    mirrors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Check each span really is the source of the entry it is paired with.

        A drifted span does not fail anything on its own, it quietly moves the oracle: the
        footprint widens or narrows around the wrong lines, and ``flow_lines`` skips the entry
        outright rather than pinning it, because a span that is no longer one line long fails
        the test that decides a flow entry has a line to pin. Spans are hand-written at more
        than a dozen sites, one of them a literal, so the drift is a live possibility and it is
        the only silent-disable path the model has.

        What can be checked is containment: the text an entry is located by, and its flow
        source, have to occur inside the lines the span names. Not the line count, since a
        merge-supplied entry legitimately has no lines of its own and several flow entries
        legitimately share one line. An entry with neither a site nor an inline gets the range
        check alone, which is the honest limit of this.

        ``dataclasses.replace`` re-runs it, so ``_finish`` shifting every span past a ``%YAML``
        directive is checked as well as the original assembly.
        """
        assert len(self.spans) == len(self.entries), (
            f"{len(self.entries)} entries carry {len(self.spans)} spans"
        )
        for entry, (start, stop) in zip(self.entries, self.spans, strict=True):
            assert 0 <= start < stop <= len(self.meta_lines), (
                f"{entry.name} has span ({start}, {stop}) outside a "
                f"{len(self.meta_lines)}-line block"
            )
            region = "\n".join(self.meta_lines[start:stop])
            assert entry.site is None or entry.site in region, (
                f"{entry.name} is located by {entry.site!r}, which its span does not hold"
            )
            assert entry.inline is None or entry.inline in region, (
                f"{entry.name} is written {entry.inline!r}, which its span does not hold"
            )

    def render(self) -> str:
        """Return the exact document text, envelope and line ending included."""
        env = self.envelope
        meta = "".join(f"{line}\n" for line in self.meta_lines)
        text = f"{env.open_fence}\n{meta}{env.close_fence}"
        if env.trailing_newline:
            text = f"{text}\n{env.body}"
        return _with_ending(BOM * env.boms + text, env.ending)

    def applied(self, updates: dict[str, str]) -> frozenset[str]:
        """Return the refs whose ``seen`` the requested updates actually change."""
        return frozenset(
            entry.ref
            for entry in self.entries
            if entry.ref in updates and updates[entry.ref] != entry.seen
        )

    def expected(self, updates: dict[str, str]) -> dict[str, object]:
        """Return the mapping the rewritten frontmatter has to load as.

        An entry reading its ``seen`` through a merge key takes whatever its source holds
        afterwards, so a hash written at the source lands here as well with nothing written at
        this entry at all. An update naming this entry is what stops that: the rewriter writes
        it a ``seen`` of its own, which shadows the merged one from then on.
        """
        sources = {entry.ref: entry for entry in self.entries}
        entries: list[dict[str, object]] = []
        for entry in self.entries:
            seen, present = entry.seen, entry.present
            planned = updates.get(entry.ref)
            if planned is not None and planned != seen:
                seen, present = planned, True
            elif entry.inherits is not None:
                source = sources[entry.inherits]
                supplied = updates.get(source.ref)
                if supplied is not None and supplied != source.seen:
                    seen, present = supplied, True
            item: dict[str, object] = {"ref": entry.ref}
            if present:
                item["seen"] = seen
            item.update(dict(entry.extras))
            entries.append(item)
        expected = {**self.root, "derives_from": entries}
        for key in self.mirrors:
            expected[key] = entries
        return expected

    def footprint(self, updates: dict[str, str]) -> tuple[set[int], set[int]]:
        """Return the layer 4 footprint of ``updates`` as changeable lines and insert points.

        The allowance is modelled per spelling rather than per entry wherever the model can
        predict it exactly: an ordinary in-place replacement may touch the ``seen`` member's
        own lines and nothing else, so an edit that also restyled the ``ref``, reindented the
        entry or disturbed an extra member is caught even though the loaded value is
        unchanged. Only the shapes whose mutation families genuinely spread keep the whole
        entry span, which is what ``Entry.edits`` being None marks. An alias site an anchored
        ``seen`` may be relocated onto is added wherever it is written, since layer 4 allows
        that edit to land outside the entry entirely.

        Returns:
            The indices whose line may change or disappear, and the indices a rewrite may
            insert new lines immediately before.
        """
        applied = self.applied(updates)
        allowed: set[int] = set()
        inserts: set[int] = set()
        for entry, (start, stop) in zip(self.entries, self.spans, strict=True):
            if entry.ref not in applied:
                continue
            if entry.edits is None:
                allowed.update(range(start, stop))
                inserts.add(stop)
            else:
                allowed.update(start + offset for offset in entry.edits)
                if entry.appends:
                    inserts.add(stop)
            if entry.anchor is None:
                continue
            alias = f"*{entry.anchor}"
            allowed.update(index for index, line in enumerate(self.meta_lines) if alias in line)
        return allowed, inserts

    def written_lines(self, updates: dict[str, str]) -> int | None:
        """Return how many lines the rewritten block holds, or None when that is not predictable.

        The footprint says which lines a rewrite may replace but not how many it writes back, so
        a member spelled over several lines hands the edit as many lines as it took. That is what
        an in-place replacement needs, since it may shrink a member and pull the rest of the
        block up, but as a bound it also lets an edit write a line of its own for every source
        line it consumed. Nothing else would see that: the surviving source is found in order
        either way, and a spare line written past the last of it has nothing behind it at all.

        What the model can say exactly is how many lines each edited member is written on
        afterwards, since every value written here is a one-line plain scalar and what surrounds
        it is the source the spelling declares. That premise characterizes the writer rather than
        the record: AD-31 layer 3 promises the surrounding source back, not a scalar style, so a
        writer that one day replaces a block scalar with a block scalar would be inside the
        record and outside this count. Updating it then is a re-record, not a regression fixed.
        The block's own length follows: the lines it was
        read with, less the ones the footprint hands to an edit, plus the ones those edits write.
        An alias site an anchored value is relocated onto writes one line for the one it takes,
        since layer 4 re-emits a displaced value on the line its alias sat on however it was
        spelled. Entries written on one shared flow line are one edit for this purpose, since
        the line is written once however many of them land on it.

        Returns:
            The line count the rewritten block has to have, or None when any edited entry's
            layer 4 families spread rather than replacing one member.
        """
        applied = self.applied(updates)
        allowed, _ = self.footprint(updates)
        regions: dict[tuple[int, int], int] = {}
        claimed: set[int] = set()
        for entry, span in zip(self.entries, self.spans, strict=True):
            if entry.ref not in applied:
                continue
            if entry.written_lines is None:
                return None
            claimed.update(span[0] + offset for offset in entry.edits or ())
            regions[span] = max(regions.get(span, 0), entry.written_lines)
        return len(self.meta_lines) - len(allowed) + sum(regions.values()) + len(allowed - claimed)

    def flow_lines(self, updates: dict[str, str]) -> list[tuple[str, tuple[str, ...]]]:
        """Return one pin per edited line whose entries are written in flow style.

        A flow entry shares its line with its carrier, and with its neighbours whenever the
        carrier is a flow sequence too, so the smallest footprint the line model can express
        is that whole line. That allowance is far wider than layer 3 permits and nothing else
        narrows it: the semantic oracle reads only loaded values, and an entry marker pins an
        opening rather than a whole entry. The line is therefore pinned here at character
        level instead, as the pattern its rewritten form still has to match in full.

        The pattern is the rendered line with the edit region of each edited entry cut out of
        it, which is what leaves the carrier's own source fixed at both ends: its opening
        bracket and everything before it, the separators between two entries, and the brackets
        it closes on. An untouched neighbour is fixed as well, since no edit may reach it. Each
        region that was cut carries the budget from that entry's ``FlowPin``, so what an edit
        may write inside one is bounded rather than surrendered along with the line.

        Returns:
            One full-line regular expression per edited line carrying flow entries, each with
            the punctuation budget of its groups in the order the groups appear.
        """
        applied = self.applied(updates)
        shared: dict[tuple[int, int], list[Entry]] = {}
        for entry, span in zip(self.entries, self.spans, strict=True):
            shared.setdefault(span, []).append(entry)
        pins: list[tuple[str, tuple[str, ...]]] = []
        for (start, stop), entries in shared.items():
            if stop - start != 1 or any(entry.pin is None for entry in entries):
                continue
            if not any(entry.ref in applied for entry in entries):
                continue
            line = self.meta_lines[start]
            pattern, budgets, cursor = "", [], 0
            for entry in entries:
                inline, pin = entry.inline or "", entry.pin
                found = line.index(inline, cursor)
                pattern += re.escape(line[cursor:found])
                if pin is not None and entry.ref in applied:
                    pattern += pin.pattern
                    budgets.append(pin.budget)
                else:
                    pattern += re.escape(inline)
                cursor = found + len(inline)
            pins.append((pattern + re.escape(line[cursor:]), tuple(budgets)))
        return pins


# --------------------------------------------------------------------------------------------
# Layer 2 spellings, one table per writable position.
# --------------------------------------------------------------------------------------------

REF_FORMS = (
    RefForm("plain", ("- ref: {ref}",), 2, False, "", False),
    # A quoted ``ref`` is a layer 2 spelling like any other whose constructed value is a string,
    # and the ``seen`` table carries both quote styles, so the asymmetry here was an oversight
    # rather than a decision. The escape a double-quoted scalar can carry matters as much as the
    # quotes: it is the spelling where locating the value means decoding it first.
    RefForm("single-quoted", ("- ref: '{ref}'",), 2, False, "", False),
    RefForm("double-quoted", ('- ref: "{ref}"',), 2, False, "", False),
    # The escape is the half of the quoted spelling the quotes alone do not reach. A row that
    # merely quotes an unescaped ref is still source the ref is written literally in, so a
    # rewrite could find and hand back the member by looking for the ref itself; this is the
    # one spelling where the value appears nowhere in the source and the entry can only be
    # located by what the loader constructed. The escape decodes to the same ``up-N#sN``, so
    # nothing else in the model moves and the row is the source difference alone.
    #
    # There is no single-quoted counterpart. That style escapes one character, the apostrophe,
    # by doubling it, so a row carrying an escape would have to put an apostrophe in the ref
    # itself, and a ref is a document id and a section slug rather than free text.
    RefForm(
        "double-quoted-escape",
        (r'- ref: "\x75p-{index}#s{index}"',),
        2,
        False,
        "",
        False,
        site="p-{index}#s{index}",
    ),
    RefForm("explicit-pair", ("- ? ref", "  : {ref}"), 2, False, "", False),
    RefForm("trailing-comment", ("- ref: {ref} # {note}",), 2, False, "", True),
    RefForm("literal-block-scalar", ("- ref: |-", "    {ref}"), 2, False, "", False),
    RefForm("clipped-block-scalar", ("- ref: |", "    {ref}"), 2, False, "\n", False),
    RefForm("folded-block-scalar", ("- ref: >-", "    {ref}"), 2, False, "", False),
    RefForm("clipped-folded-block-scalar", ("- ref: >", "    {ref}"), 2, False, "\n", False),
    RefForm(
        "multi-line-folded-block-scalar",
        ("- ref: >-", "    {head}", "    {tail}"),
        2,
        True,
        "",
        False,
    ),
    RefForm("multi-line-double-quoted", ('- ref: "{head}', '    {tail}"'), 2, True, "", False),
    RefForm("multi-line-plain", ("- ref: {head}", "    {tail}"), 2, True, "", False),
    RefForm("entry-anchor", ("- &entry{index}", "    ref: {ref}"), 4, False, "", False),
    RefForm("entry-tag", ("- !!map", "    ref: {ref}"), 4, False, "", False),
    RefForm(
        "entry-anchor-and-tag", ("- &entry{index} !!map", "    ref: {ref}"), 4, False, "", False
    ),
)

SEEN_FORMS = (
    SeenForm("absent", (), "null", False, False, False),
    SeenForm("plain", ("seen: {old}",), "old", True, False, False),
    SeenForm("single-quoted", ("seen: '{old}'",), "old", True, False, False),
    SeenForm("double-quoted", ('seen: "{old}"',), "old", True, False, False),
    SeenForm("explicit-null", ("seen: null",), "null", True, False, False),
    SeenForm("tilde-null", ("seen: ~",), "null", True, False, False),
    SeenForm("empty", ("seen:",), "null", True, False, False),
    SeenForm("empty-with-comment", ("seen: # {seen_note}",), "null", True, False, True),
    SeenForm("literal-strip", ("seen: |-", "  {old}"), "old", True, False, False),
    SeenForm("literal-clip", ("seen: |", "  {old}"), "old-newline", True, False, False),
    SeenForm("literal-keep", ("seen: |+", "  {old}"), "old-newline", True, False, False),
    SeenForm("folded-strip", ("seen: >-", "  {old}"), "old", True, False, False),
    SeenForm("folded-clip", ("seen: >", "  {old}"), "old-newline", True, False, False),
    SeenForm("folded-keep", ("seen: >+", "  {old}"), "old-newline", True, False, False),
    # A keep-chomped block with a blank line under it is the one chomping spelling whose
    # retained newlines differ from the clipped form's, so it is written out separately: the
    # blank belongs to the scalar, which makes it part of the member a rewrite replaces rather
    # than untouched source between two entries.
    SeenForm(
        "literal-keep-blank-line", ("seen: |+", "  {old}", ""), "old-blank-line", True, False, False
    ),
    SeenForm(
        "folded-keep-blank-line", ("seen: >+", "  {old}", ""), "old-blank-line", True, False, False
    ),
    # AD-31 declares an explicit indentation indicator on a block scalar in either style, and
    # YAML lets the two header indicators be written in either order, so the headers are
    # spelled out rather than left to stand for one another. The cross product is style by
    # chomping by order: the two chomping indicators a header writes compose with the
    # indentation one in either order, and a clipped header writes no chomping indicator at
    # all, so it has one spelling per style rather than two. Ten rows, and none of them stands
    # in for another, because what the scanner hands back is the span the whole header opens
    # and the line break its chomping retained rather than the indicators one at a time.
    SeenForm("literal-indent-indicator", ("seen: |2-", "  {old}"), "old", True, False, False),
    SeenForm("folded-indent-indicator", ("seen: >2-", "  {old}"), "old", True, False, False),
    SeenForm("literal-indent-after-chomp", ("seen: |-2", "  {old}"), "old", True, False, False),
    SeenForm("folded-indent-after-chomp", ("seen: >-2", "  {old}"), "old", True, False, False),
    SeenForm(
        "literal-indent-indicator-kept",
        ("seen: |2+", "  {old}"),
        "old-newline",
        True,
        False,
        False,
    ),
    SeenForm(
        "folded-indent-indicator-kept", ("seen: >2+", "  {old}"), "old-newline", True, False, False
    ),
    SeenForm(
        "literal-indent-after-keep", ("seen: |+2", "  {old}"), "old-newline", True, False, False
    ),
    SeenForm(
        "folded-indent-after-keep", ("seen: >+2", "  {old}"), "old-newline", True, False, False
    ),
    SeenForm(
        "literal-indent-indicator-clipped",
        ("seen: |2", "  {old}"),
        "old-newline",
        True,
        False,
        False,
    ),
    SeenForm(
        "folded-indent-indicator-clipped",
        ("seen: >2", "  {old}"),
        "old-newline",
        True,
        False,
        False,
    ),
    SeenForm(
        "literal-header-comment", ("seen: |- # {seen_note}", "  {old}"), "old", True, False, True
    ),
    SeenForm("explicit-pair", ("? seen", ": {old}"), "old", True, False, False, 2),
    SeenForm("explicit-key-no-value", ("? seen",), "null", True, False, False, 2, True),
    SeenForm("anchored", ("seen: &{anchor} {old}",), "old", True, True, False),
    SeenForm("tagged", ("seen: !!str {old}",), "old", True, False, False),
    SeenForm("anchor-then-tag", ("seen: &{anchor} !!str {old}",), "old", True, True, False),
    SeenForm("tag-then-anchor", ("seen: !!str &{anchor} {old}",), "old", True, True, False),
    SeenForm(
        "anchor-with-comment-above-value",
        ("seen: &{anchor} # {seen_note}", "  {old}"),
        "old",
        True,
        True,
        True,
        2,
    ),
    SeenForm("tag-on-its-own-line", ("seen:", "  !!str {old}"), "old", True, False, False, 2),
    SeenForm(
        "both-properties-on-their-own-lines",
        ("seen: &{anchor} # {seen_note}", "  !!str", "  {old}"),
        "old",
        True,
        True,
        True,
        2,
    ),
    # An empty value with its properties written below it. Layer 4 has replacement consume those
    # properties, and where the value is the empty scalar there is no value text to write over,
    # so the removal has to reach a line the member occupies alone and take the line break with
    # it. Every row above writes a value, so none of them reaches that arm.
    #
    # A tagged empty scalar constructs to the empty string rather than null, and the empty string
    # is a ``str``, so these sit in the strict column exactly as the untagged ones do.
    #
    # The three commented rows are the ones that reach the break removal. Without a comment the
    # value edit already runs up to the first property and consumes the break itself, so the
    # removal has nothing left to widen; the comment stops the value edit short and leaves the
    # break to the removal. Both are generated, since they are different paths to one outcome.
    SeenForm("empty-anchor-on-its-own-line", ("seen:", "  &{anchor}"), "null", True, True, False),
    SeenForm("empty-tag-on-its-own-line", ("seen:", "  !!str"), "empty-string", True, False, False),
    SeenForm(
        "empty-comment-then-anchor-below",
        ("seen: # {seen_note}", "  &{anchor}"),
        "null",
        True,
        True,
        True,
    ),
    SeenForm(
        "empty-comment-then-tag-below",
        ("seen: # {seen_note}", "  !!str"),
        "empty-string",
        True,
        False,
        True,
    ),
    SeenForm(
        "empty-comment-then-both-below",
        ("seen: # {seen_note}", "  !!str", "  &{anchor}"),
        "empty-string",
        True,
        True,
        True,
    ),
)

WHOLE_FORMS = (
    WholeForm(
        "omap-block", ("- !!omap", "  - ref: {ref}", "  - seen: {old}"), None, "old", True, False
    ),
    WholeForm("omap-block-absent", ("- !!omap", "  - ref: {ref}"), None, "null", False, False),
    WholeForm(
        "omap-block-explicit-key",
        ("- !!omap", "  - ref: {ref}", "  - ? seen"),
        None,
        "null",
        True,
        False,
        written_lines=2,
        # The explicit key writes no value, so the hash goes on a line of its own past it. The
        # flow spelling of the same key does not get this: `_whole_entry` takes the flow branch
        # first and writes the append inside the bracket, so setting it there would hand the
        # footprint an insert point no rewrite ever uses and widen the claim for nothing.
        appends_own_line=True,
    ),
    WholeForm(
        "omap-block-anchored",
        ("- !!omap", "  - ref: {ref}", "  - seen: &{anchor} {old}"),
        None,
        "old",
        True,
        True,
    ),
    WholeForm("flow-plain", (), "{{ref: {ref}, seen: {old}}}", "old", True, False),
    WholeForm("flow-absent", (), "{{ref: {ref}}}", "null", False, False, "separator"),
    # The trailing comma this spelling already writes sits inside the entry's marker, so the
    # appended pair lands after a separator that is already there and writes none of its own.
    WholeForm("flow-trailing-comma", (), "{{ref: {ref}, }}", "null", False, False),
    WholeForm("flow-empty-seen", (), "{{ref: {ref}, seen: }}", "null", True, False),
    WholeForm("flow-explicit-key", (), "{{ref: {ref}, ? seen}}", "null", True, False),
    WholeForm("flow-double-quoted", (), '{{ref: {ref}, seen: "{old}"}}', "old", True, False),
    WholeForm("flow-tagged", (), "{{ref: {ref}, seen: !!str {old}}}", "old", True, False),
    WholeForm("flow-anchored", (), "{{ref: {ref}, seen: &{anchor} {old}}}", "old", True, True),
    WholeForm("flow-omap", (), "!!omap [{{ref: {ref}}}, {{seen: {old}}}]", "old", True, False),
    WholeForm("flow-omap-absent", (), "!!omap [{{ref: {ref}}}]", "null", False, False, "item"),
)

FLOW_FORMS = tuple(form for form in WHOLE_FORMS if form.inline is not None)

ROOT_EXTRAS = (
    ("title", ("title: Document {index}",), "Document {index}"),
    ("layer", ("layer: design",), "design"),
    ("authority", ("authority: derived",), "derived"),
    ("tickets", ("tickets:", "  - GTX-1"), ["GTX-1"]),
    ("tickets", ("tickets: [GTX-1, GTX-2]",), ["GTX-1", "GTX-2"]),
)


# --------------------------------------------------------------------------------------------
# Building entries and documents from the tables.
# --------------------------------------------------------------------------------------------


def _fields(index: int) -> dict[str, str]:
    """Return the per-entry substitutions every spelling template is rendered with."""
    head = f"up-{index}#s{index}"
    return {
        "index": str(index),
        "ref": head,
        "head": head,
        "tail": f"part{index}",
        "old": f"old{index:04d}",
        "anchor": f"seen{index}",
        "note": f"note{index}",
        "seen_note": f"seen-note{index}",
    }


def _seen_member_offsets(lines: tuple[str, ...]) -> tuple[int, ...]:
    """Return which of ``lines`` hold the ``seen`` member, which is what an edit may reach.

    Read off the source rather than declared, because it is an observation about text this file
    just wrote rather than a claim about the rewriter. Its one precondition is that no generated
    line holds the text ``seen`` unless it is part of the member: the note placeholders spell
    ``seen-note``, and those only ever appear on a line that is already a ``seen`` line.

    The other three places ``edits`` is set stay as they are. It is the model's independent claim
    about one shape's layer 4 footprint, and a single derivation shared across shapes is exactly
    what would let one shape's wrong claim be covered by another shape's being right.
    """
    return tuple(offset for offset, line in enumerate(lines) if "seen" in line)


def _seen_value(kind: SeenKind, old: str) -> str | None:
    """Return the value a ``seen`` spelling of ``kind`` loads as.

    A dict rather than a chain of comparisons, so a kind no row declares raises here instead of
    falling through to the plain text. The fall-through was the quiet failure: a typo in a new
    row's ``value`` made the oracle expect the old value, which is what most spellings load to
    anyway, so the row passed while claiming nothing.
    """
    values: dict[str, str | None] = {
        "null": None,
        "empty-string": "",
        "old": old,
        "old-newline": f"{old}\n",
        "old-blank-line": f"{old}\n\n",
    }
    return values[kind]


def _ref_value(fields: dict[str, str], ref_form: RefForm) -> str:
    """Return the value a ``ref`` spelling loads as, its folding and chomping applied."""
    written = f"{fields['head']} {fields['tail']}" if ref_form.split else fields["ref"]
    return f"{written}{ref_form.trailing}"


def _combined_entry(index: int, ref_form: RefForm, seen_form: SeenForm) -> Entry:
    """Build an entry from one ``ref`` spelling and one ``seen`` spelling."""
    fields = _fields(index)
    pad = " " * ref_form.key_indent
    lines = [template.format(**fields) for template in ref_form.templates]
    lines.extend(f"{pad}{template.format(**fields)}" for template in seen_form.templates)
    # Each position writes its own comment text, so the layer 3 comment assertion is a claim
    # about that position rather than about either copy of one shared text. The `ref` comment
    # sits outside the allowed footprint and the `seen` comment inside it, so a single text
    # would let the pinned copy answer for the one a rewrite can actually drop.
    notes = [f"# {fields['note']}"] if ref_form.note else []
    if seen_form.note:
        notes.append(f"# {fields['seen_note']}")
    # Every edit an in-place replacement plans lands on the member's own lines: the value, the
    # properties written above it, and the block-scalar header comment that moves onto the
    # replacement. A member that is absent, or an explicit key with no `:` to write after,
    # takes a line of its own just past the entry instead.
    written = len(ref_form.templates)
    edits = tuple(range(written, written + len(seen_form.templates)))
    appends = not seen_form.present or seen_form.appends_own_line
    return Entry(
        f"{ref_form.name}+{seen_form.name}",
        tuple(lines),
        None,
        _ref_value(fields, ref_form),
        _seen_value(seen_form.value, fields["old"]),
        seen_form.present,
        fields["anchor"] if seen_form.anchored else None,
        tuple(notes),
        (),
        marker=_style_marker(lines[0]),
        edits=edits,
        appends=appends,
        site=(ref_form.site or "{head}").format(**fields),
        written_lines=seen_form.written_lines,
    )


def _style_marker(opening: str) -> str | None:
    """Return the opening of an entry's source that no allowed edit may restyle.

    The run stops at the first place an edit can reach, which is the ``seen`` member or the
    bracket a flow collection closes on, since an appended member is written just inside it.
    The punctuation that opens the entry is kept rather than trimmed, because that is what
    tells the two collection styles apart: a block entry is recognized by the sequence dash
    its keys hang off, and a flow entry by the bracket it opens on, so restyling either into
    the other loses the marker instead of leaving it as a substring of the replacement.
    """
    cut = min(
        (
            index
            for index in (opening.find("seen"), opening.find("}"), opening.find("]"))
            if index != -1
        ),
        default=len(opening),
    )
    return opening[:cut] or None


def _flow_key_head(inline: str, marker: str) -> str:
    """Return the flow source an edit may only start writing after.

    The marker stops where an edit can first reach, which for a ``seen`` member the author
    already wrote is that member's key. The key is source rather than something the edit
    supplies, so a replacement of the value has to write past it: the key, the ``:`` that
    follows it and the spaces the author left between that colon and the value all join the
    fixed opening, which leaves the region starting at the value itself and a respelled key or
    a widened separator caught rather than absorbed.

    An entry whose ``seen`` member is absent has no key to preserve, since the edit writes the
    whole pair, so its region begins where the marker ends and the punctuation budget is what
    bounds what lands there.

    Args:
        inline: The entry's flow source as the generator wrote it.
        marker: The opening of that source no allowed edit may restyle.

    Returns:
        The prefix of ``inline`` that has to come back verbatim.
    """
    key = inline.find("seen", len(marker))
    if key == -1:
        return marker
    stop = key + len("seen")
    if inline[stop : stop + 1] != ":":
        return inline[:stop]
    rest = inline[stop + 1 :]
    return inline[: stop + 1 + len(rest) - len(rest.lstrip(" "))]


def _flow_pin(inline: str, writes: FlowEditKind) -> FlowPin:
    """Return what an edited flow entry's source still has to look like.

    Three parts, and the middle one is the only place an edit may land. The head fixes the
    opening and the ``seen`` key an in-place edit writes past, the brackets the entry closes
    on fix the end, and between them sits the region the edit is free inside of. Which
    brackets stay behind the edit follows from what it writes, which is what
    ``FLOW_EDIT_INDICATORS`` records.

    Free inside is not unbounded: the region carries a punctuation budget, so a rewrite that
    spelled an inert separator into an entry, or restyled a bracket the loaded value does not
    depend on, is caught even though every semantic oracle would accept it. The budget is
    counted rather than positioned, because what an edit may add is known exactly while where
    inside the region it writes is the rewriter's own choice.

    Args:
        inline: The entry's flow source as the generator wrote it.
        writes: Which ``FLOW_EDIT_INDICATORS`` kind an edit of this spelling is.

    Returns:
        The pattern the source has to match and the budget its edit region has to keep.
    """
    closing = inline[len(inline.rstrip("}]")) :]
    if writes == "item":
        closing = closing[-1:]
    head = _flow_key_head(inline, _style_marker(inline) or "")
    region = inline[len(head) : len(inline) - len(closing)]
    written = "".join(char for char in region if char in FLOW_INDICATORS)
    # A head that ends in a space ends on a separator the author wrote, and an edit writes past
    # a separator rather than over it, so what follows starts the value rather than widening
    # the run in front of it. A head that ends anywhere else is followed by the edit's own text.
    # Both forms end on a non-space for the same reason at the other end: the region closes on
    # the last character the edit emitted, so space between that and the bracket the entry
    # closes on is source the rewrite added. The budget cannot see it, since space is not an
    # indicator, and no semantic oracle can either, since a flow scalar loads the same with or
    # without it. Every region a rewrite writes here is at least one character wide, which is
    # what lets the two ends be claimed of the same group.
    region_pattern = r"(\S(?:.*?\S)?)" if head.endswith(" ") else r"(.*?\S)"
    return FlowPin(
        f"{re.escape(head)}{region_pattern}{re.escape(closing)}",
        written + FLOW_EDIT_INDICATORS[writes],
    )


def _whole_entry(index: int, form: WholeForm) -> Entry:
    """Build an entry written as one indivisible spelling, block or flow."""
    fields = _fields(index)
    lines = tuple(template.format(**fields) for template in form.templates)
    inline = None if form.inline is None else form.inline.format(**fields)
    if inline is not None:
        # A flow entry is one line however its sequence carries it, so its own line is both
        # the whole span and the only place an edit can land: even an appended member is
        # written just inside the bracket rather than on a line of its own.
        lines = (f"- {inline}",)
        edits: tuple[int, ...] = (0,)
        appends = False
    else:
        edits = _seen_member_offsets(lines)
        appends = not form.present or form.appends_own_line
    return Entry(
        form.name,
        lines,
        inline,
        fields["ref"],
        _seen_value(form.value, fields["old"]),
        form.present,
        fields["anchor"] if form.anchored else None,
        (),
        (),
        marker=_style_marker(inline if inline is not None else lines[0]),
        edits=edits,
        appends=appends,
        pin=None if inline is None else _flow_pin(inline, form.writes),
        site=fields["head"],
        written_lines=form.written_lines,
    )


def _indent(lines: tuple[str, ...], pad: int) -> list[str]:
    """Return ``lines`` shifted right by ``pad`` columns, leaving empty lines empty."""
    return [f"{' ' * pad}{line}" if line else line for line in lines]


def _block_sequence(
    entries: tuple[Entry, ...], pad: int
) -> tuple[list[str], list[tuple[int, int]]]:
    """Render entries as a block sequence, returning the lines and each entry's line span."""
    lines: list[str] = []
    spans: list[tuple[int, int]] = []
    for entry in entries:
        start = len(lines)
        lines.extend(_indent(entry.lines, pad))
        spans.append((start, len(lines)))
    return lines, spans


def _member_lines(head: list[str], entries: tuple[Entry, ...], carrier: str, pad: int) -> list[str]:
    """Render the ``derives_from`` member under ``head``, in the requested carrier shape.

    The head is the key as its own lines, since layer 2 lets a root key be written either
    plainly or as an explicit ``? key`` / ``: value`` pair, which spreads it over two. A flow
    carrier is written onto the last of them, the line carrying the ``:`` its value follows.
    """
    if carrier == "flow":
        inlines = FLOW_SEPARATOR.join(entry.inline or "" for entry in entries)
        return [*head[:-1], f"{head[-1]} [{inlines}]"]
    body, _ = _block_sequence(entries, pad)
    return [*head, *body]


def _root_key_value(extra: tuple[str, tuple[str, ...], object], index: int) -> object:
    """Return the loaded value of one optional root member."""
    value = extra[2]
    return value.format(index=index) if isinstance(value, str) else value


# --------------------------------------------------------------------------------------------
# Rendering and reading documents.
# --------------------------------------------------------------------------------------------


def _with_ending(text: str, ending: EnvelopeEnding) -> str:
    """Return ``text`` rewritten in the requested document line ending."""
    if ending == "mixed":
        return text.replace("\n", "\r\n", 1)
    return text if ending == "\n" else text.replace("\n", ending)


def _document_ending(text: str) -> EnvelopeEnding:
    """Return the ending a document is written in, by the same rule the rewriter classifies by.

    This is the one helper here whose body is the rewriter's, and it is not a mirror of it. The
    two consume different arguments and answer different questions: production reads the
    **input** to decide which ending to restore, while this reads the **output** and the answer
    is compared against ``env.ending``, which the model declared before the document was ever
    rendered. An inverted rule in production still fails that comparison.

    The bodies also differ where it matters. Production returns ``"\\n"`` for a file written with
    mixed endings, because normalizing is the outcome it wants; this returns ``"mixed"``, because
    "how was this document written" is what the envelope assertion asks. Collapsing that last
    line into production's would make the mixed-normalization claim vacuous, so the two are kept
    apart deliberately rather than by oversight.
    """
    if "\r\n" in text and not set(text.replace("\r\n", "")) & {"\r", "\n"}:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "mixed" if "\r" in text else "\n"


def _parts(text: str) -> FrontmatterParts:
    """Split a document into its frontmatter pieces after normalizing its line endings."""
    parts = split_frontmatter_parts(normalize_newlines(text), DOC)
    assert parts is not None
    return parts


def _meta_lines(text: str) -> list[str]:
    """Return the frontmatter block of a document as a list of lines."""
    raw_meta = _parts(text).raw_meta
    return raw_meta.split("\n")[:-1] if raw_meta else []


def _reload(text: str) -> object:
    """Reload a document's frontmatter through the project's own strict boundary loader.

    The corpus reuses `frontmatter_parser`'s loader rather than building an equivalent one, so
    "strictly loadable" here means what the product means by it. Spelling the parser choice a
    second time would let the two drift apart silently.
    """
    return frontmatter_parser._LOADER.load(_parts(text).raw_meta)


def _rewrite_bytes(text: str, updates: dict[str, str]) -> list[Rewrite]:
    """Drive the production planner over one in-memory document."""
    before = text.encode("utf-8")
    return plan_rewrites({DOC: updates}, lambda _path: before)


# --------------------------------------------------------------------------------------------
# Assertions shared by the properties.
# --------------------------------------------------------------------------------------------


def _carries_control_char(value: object) -> bool:
    """Whether a constructed value holds a C0 control, DEL, or a C1 control.

    Spelled out from code points rather than imported from ``text_utils``, so the corpus keeps
    an oracle independent of the predicate the product refuses with.
    """
    return isinstance(value, str) and any(
        ord(char) < 0x20 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F for char in value
    )


def _refused_values(document: Document) -> list[object]:
    """Every value in this document AD-35 refuses, read off the model rather than the source.

    A block scalar taking clip or keep chomping constructs a trailing line break, which is what
    puts most of these documents on the refused side: the spelling is written for the rewriter
    and the value it happens to construct is what the strict load reads.
    """
    values: list[object] = list(document.root.values())
    for entry in document.entries:
        values.extend((entry.ref, entry.seen))
    return [value for value in values if _carries_control_char(value)]


def _assert_strict_disposition(document: Document, text: str) -> None:
    """Assert the strict load puts this document on the side of AD-35 its own values put it.

    A failure here is a generator bug rather than a rewriter one: family 1 declares that it
    generates only the strict tracked-document column of layer 2, and this is the machine
    check of that claim.

    AD-35 narrows that column without narrowing the reread column beside it, so the claim has
    two sides. A document whose ``id``, ``title``, ``tickets``, ``ref``, or ``seen`` constructs
    a control character is refused as a lattice document while the rewriter still round-trips
    it byte-correctly, and is asserted to be refused rather than dropped from the corpus, which
    would take the rewriter coverage for its spelling with it.
    """
    parts = split_frontmatter_parts(normalize_newlines(text), DOC)
    assert parts is not None, "family 1 document has no frontmatter block"
    if _refused_values(document):
        with pytest.raises(FrontmatterError):
            parse_meta(parts.raw_meta, DOC)
        _CLAIMS["control-value-refused"] += 1
        return
    assert parse_meta(parts.raw_meta, DOC).disposition == "tracked"


def _find_run(haystack: list[str], needle: tuple[str, ...], start: int, budget: int) -> int | None:
    """Return where ``needle`` reappears, no further past ``start`` than ``budget`` lines.

    Searching forward without a bound would let a line the model never allowed be spliced in
    ahead of protected source and then skipped over as if it were part of the replacement. The
    bound is what the model predicts can stand in that gap: one line per allowed line the
    rewrite consumed, since a replacement may shrink a member but never grows past the lines
    it was given, plus one per insert point declared there.
    """
    width = len(needle)
    for index in range(start, min(start + budget, len(haystack) - width) + 1):
        if tuple(haystack[index : index + width]) == needle:
            return index
    return None


def _unallowed_runs(
    lines: list[str], allowed: set[int], inserts: set[int]
) -> list[tuple[int, tuple[str, ...]]]:
    """Group the lines outside the allowed footprint into contiguous runs.

    A run also breaks at a permitted insert point, so a line spliced into the middle of
    otherwise untouched source has to be accounted for by the model rather than absorbed as
    if it had landed in a gap.
    """
    runs: list[tuple[int, tuple[str, ...]]] = []
    current: list[str] = []
    start = 0
    for index, line in enumerate(lines):
        if current and (index in allowed or index in inserts):
            runs.append((start, tuple(current)))
            current = []
        if index in allowed:
            continue
        if not current:
            start = index
        current.append(line)
    if current:
        runs.append((start, tuple(current)))
    return runs


def _assert_footprint_confined(
    before: list[str], after: list[str], allowed: set[int], inserts: set[int]
) -> None:
    """Assert every line outside the predicted footprint survives, in order and unchanged.

    Little is asserted about the allowed regions themselves: layer 4 permits several different
    edits there, and the semantic oracle is what decides whether the right one landed. What is
    asserted is that no other line changed, disappeared, or had anything spliced into the
    middle of it, and that each protected run comes back no further along than the lines the
    model handed the rewrite could have pushed it. That last bound is what stops a line
    appearing where the model declared no insert point, which is otherwise indistinguishable
    from source the search simply walked past.

    Inside a member the rewrite shrank, that bound still leaves room a blank line could sit in
    unnoticed, so the blank lines are counted as well. Every value written here is a one-line
    plain scalar, so a replaced member keeps none of the blanks it was written with and the
    rewrite writes none of its own: the blanks left are exactly the ones outside the footprint.
    That premise characterizes the current writer rather than AD-31, which promises the source
    around a member back without saying what style the member itself is rewritten in, so a
    writer that emitted a multi-line value would need this count re-recorded rather than fixed.

    Past the last protected run the same bound applies rather than nothing at all. A document
    whose last line is the member being rewritten, which is the commonest shape there is, has
    no protected run to end on, and without the bound anything at all could follow the edit.
    """
    blank = sum(1 for index, line in enumerate(before) if not line.strip() and index not in allowed)
    assert sum(1 for line in after if not line.strip()) == blank, (
        f"the rewrite changed how many blank lines the block carries: {blank} survive the edit"
    )
    position, consumed = 0, 0
    for start, run in _unallowed_runs(before, allowed, inserts):
        gap = range(consumed, start)
        budget = len(allowed & set(gap)) + len(inserts & set(range(consumed, start + 1)))
        found = _find_run(after, run, position, budget)
        assert found is not None, f"lines outside the allowed footprint changed: {run!r}"
        position = found + len(run)
        consumed = start + len(run)
    tail = len(allowed & set(range(consumed, len(before))))
    tail += len(inserts & set(range(consumed, len(before) + 1)))
    assert len(after) - position <= tail, "content was added after the last protected line"


def _entry_regions(document: Document, raw_meta: str) -> list[str | None]:
    """Return the slice of the rewritten block each entry occupies.

    An entry's line numbers do not survive a rewrite, since a replaced member may take fewer
    lines than it was written on and an appended one takes an extra, so the entries are located
    by their sites instead: a text each one writes and no other line in the document does. The
    slice runs to the next entry's site, which is what makes a claim about an entry's own
    source rather than about the block as a whole.

    Args:
        document: The generated model.
        raw_meta: The rewritten frontmatter block.

    None means one thing only, that the model declared no site for this entry. A site the model
    did declare and the rewrite then destroyed is a failure here rather than a None, because the
    two are indistinguishable to the caller and the second is exactly the case a skip must not
    absorb: an entry located by its ``ref`` head, which no layer 4 edit may touch, cannot lose it
    to a correct rewrite.

    Returns:
        One slice per entry, in order, and None for an entry with no site to locate it by.
    """
    for entry in document.entries:
        assert entry.site is None or entry.site in raw_meta, (
            f"{entry.name} lost the text it is located by: {entry.site!r}"
        )
    sites = [-1 if entry.site is None else raw_meta.find(entry.site) for entry in document.entries]
    regions: list[str | None] = []
    for position, start in enumerate(sites):
        later = (found for found in sites[position + 1 :] if found > start)
        regions.append(None if start == -1 else raw_meta[start : min(later, default=len(raw_meta))])
    return regions


def _note_opening(entry: Entry, note: str) -> str:
    """Return the source a comment is written after, up to the nearest point an edit may reach.

    Membership of an entry is as far as a region can pin a comment, and a comment can move
    inside one: it is written at the end of a member's line, and the line below it is the
    member's value, which is a line an edit may land on. A rewrite that lifted the comment onto
    a line of its own under the member would keep the entry's comment count and the entry it
    sits in, so what pins it is the source in front of it instead.

    How much of that source is the author's differs by line. A comment on the ``ref`` line sits
    outside the footprint, so everything before it has to come back; one on a ``seen`` line sits
    past the member head and in front of a value the edit replaces, so the head is all of it
    that survives the edit and all this may claim.

    Either way the run is read back no further than the entry's own site, since that is where
    the region the comment is looked for in begins and source above it is another claim's.

    Args:
        entry: The entry that wrote the comment.
        note: The comment text, which each spelling writes its own of.

    Returns:
        The text the comment has to be written after, its own indentation dropped.
    """
    offset = next(index for index, line in enumerate(entry.lines) if note in line)
    line = entry.lines[offset]
    head = _member_head(line) if entry.edits is not None and offset in entry.edits else None
    opening = head if head is not None else line[: line.index(note)].lstrip()
    site = entry.site
    return opening[opening.index(site) :] if site is not None and site in opening else opening


def _assert_comments_kept_in_place(document: Document, raw_meta: str) -> None:
    """Assert the block's comments come back, each at the position in its entry it was written.

    Surviving somewhere in the block is too weak a claim once two entries carry comments and
    both are rewritten. Their texts differ, so a dropped comment is caught, but one lifted off
    a rewritten member and written onto the other leaves both texts behind and would pass: the
    line it moved to is inside the allowed footprint as well, and the loaded mapping never sees
    a comment at all. Each entry's comments are therefore looked for between that entry's own
    site and the next entry's, which is the span the author wrote them in.

    Landing in the right entry leaves the position inside it open, and a comment can move there
    too, so each one is also read against the source it was written after, which
    ``_note_opening`` derives.

    Preserving what the author wrote leaves the other half of the claim open, which is that a
    rewrite writes no comment of its own. A ``#`` opens a comment only at the start of a line or
    after a space, the rule ``_uncommented`` reads by, and no value this file writes carries a
    ``#`` after a space; the ones inside a ``ref`` separate it from its section and open nothing.
    So the count of comment-opening hashes is fixed, and a rewrite that neither drops nor invents
    one leaves it alone.

    Args:
        document: The generated model, whose entries carry the comments they were written with.
        raw_meta: The rewritten frontmatter block.
    """
    for note in document.notes:
        assert note in raw_meta, f"comment {note!r} was dropped"
    written = sum(line.count("#") for line in document.meta_lines)
    assert raw_meta.count("#") == written, (
        f"the rewrite changed how many comments the block opens: {written} were written"
    )
    for entry, region in zip(document.entries, _entry_regions(document, raw_meta), strict=True):
        if not entry.notes:
            continue
        assert region is not None, f"{entry.name} lost the site its comments hang off"
        for note in entry.notes:
            assert note in region, f"comment {note!r} left the entry that wrote it"
            opening = _note_opening(entry, note)
            written = [
                line[: line.index(note)].lstrip() for line in region.split("\n") if note in line
            ]
            assert any(before.startswith(opening) for before in written), (
                f"comment {note!r} moved off the source it was written after: {opening!r} "
                f"opens none of the lines carrying it, which open {written!r}"
            )
            _CLAIMS["comment-at-its-own-site"] += 1


def _assert_envelope_preserved(document: Document, before: str, after: str) -> None:
    """Assert layer 3: the envelope, comments, key order and line ending come back as read."""
    env = document.envelope
    old, new = _parts(before), _parts(after)
    assert new.prefix == old.prefix == BOM * env.boms
    assert new.open_fence == old.open_fence
    assert new.close_fence == old.close_fence
    assert new.close_fence_newline == old.close_fence_newline
    assert new.body == old.body
    expected_ending = "\n" if env.ending == "mixed" else env.ending
    assert _document_ending(after) == expected_ending
    if env.directive is not None:
        assert f"%YAML {env.directive}" in new.raw_meta
        assert "--- !!map" in new.raw_meta
    _assert_comments_kept_in_place(document, new.raw_meta)


def _member_head(line: str) -> str | None:
    """Return one member line's opening, up to where its value starts, or None if it opens none.

    The key, the colon after it and the spaces the author left before the value, all of which a
    replacement of that value has to write past. The line's own indentation is dropped, since a
    carrier indents an entry by whatever its shape needs and a member indented wrong loads
    differently or not at all.

    An allowed line either opens a member or continues a value the rewrite replaces outright,
    and only the first kind has an opening to pin: requiring a value line to come back would
    forbid the very edit layer 4 allows. The two are told apart by the indicators, since no
    value this generator writes carries a ``:`` or a ``?``, so a line with neither is scalar
    content. One that did would be read here as a key and fail loudly, rather than quietly
    widening what the claim lets a rewrite do.
    """
    line = line.lstrip()
    colon = line.find(":")
    if colon == -1:
        return line if "?" in line else None
    rest = line[colon + 1 :]
    return line[: colon + 1 + len(rest) - len(rest.lstrip(" "))]


def _member_heads(entry: Entry) -> tuple[str, ...]:
    """Return the openings of the member lines a block edit lands on.

    A head is read off every line the edit may land on rather than off the first one alone,
    because a member written as an explicit pair spreads that opening over two lines: the
    ``? seen`` key on one, and on the next the ``:`` its value is written after. Reading only
    the first would leave that separator unclaimed, which is the one an in-place replacement
    actually writes past.

    Returns nothing where there is no such line to read: an entry whose whole span the model
    leaves to the rewriter, one with no ``seen`` member written for an edit to land on, and a
    flow entry, whose key its own pin carries instead.
    """
    if entry.pin is not None or not entry.edits:
        return ()
    heads = (_member_head(entry.lines[offset]) for offset in entry.edits)
    return tuple(head for head in heads if head is not None)


def _assert_styles_preserved(document: Document, after: str, updates: dict[str, str]) -> None:
    """Assert layer 3 byte-local preservation for the parts the line footprint cannot pin.

    Four claims, all about source the semantic oracle would let a rewrite restyle freely. An
    entry's opening keeps the punctuation its collection style is recognized by. A line written
    in flow style, which the footprint can only allow or forbid whole, comes back matching
    character for character everywhere no edit was allowed to land: its carrier's brackets and
    separators, its untouched entries, and the member key and the punctuation around the value
    each edited entry was rewritten at. And a block member, whose whole line the footprint can
    only allow or forbid, still opens with the key the author spelled it with and the spaces
    they left after it, since layer 4 replaces that member's value rather than the line it
    sits on. That claim is made of every line an edit may land on, since a member spelled as an
    explicit pair carries its key on one line and the separator its value follows on the next.

    The fourth closes the other end of a block edit. An in-place replacement writes the value
    the member ends on, so space left past it is source the rewrite added rather than source it
    wrote over, and nothing else here sees it: the head claim pins the opening and stops, the
    footprint hands the whole line to the rewriter, and a plain scalar loads the same whether or
    not space follows it. It is made of the whole block rather than of the edited lines alone,
    because the source writes no content followed by space for it to be read against, which is
    the premise asserted alongside it rather than left to a reader. A line of nothing but space
    is outside the claim, since a keep-chomped scalar's blank line is written at the entry's own
    indentation and carries no value for a rewrite to have written past; the footprint counts
    those lines instead.

    The block claim is made of every entry the model gives a member to read it off, rather than
    only of the entries an update was applied to. An entry no edit may reach satisfies it for
    free, since nothing inside it may change, and restricting it to the applied ones would miss
    the case in between: a relocated anchor definition is written onto a member of an entry no
    update named, whose key and separator are the author's source just the same.
    """
    raw_meta = _parts(after).raw_meta
    # Counted rather than merely looked for, because a marker is not unique to the entry that
    # declared it: every block ``!!omap`` entry opens ``- !!omap``, so two of them in one
    # document would satisfy each other's claim and one could be restyled away unseen. The
    # claim is a count rather than a position because a region cannot serve here: it begins at
    # an entry's site, which for a multi-line entry is written after its marker and for an omap
    # entry is a line below it entirely. Layer 3 forbids the rewrite reducing the count, and
    # anything above it passes. No mutation isolates this claim today: every marker the tables
    # write sits on a line outside the footprint, so the footprint assertion reaches a restyled
    # one first. The count is what keeps the claim true on its own terms for a marker that one
    # day opens a line an edit may land on.
    for marker, count in Counter(
        entry.marker for entry in document.entries if entry.marker is not None
    ).items():
        assert raw_meta.count(marker) >= count, (
            f"an entry was restyled: {marker!r} is written {raw_meta.count(marker)} times "
            f"where {count} entries opened with it"
        )
    for entry, region in zip(document.entries, _entry_regions(document, raw_meta), strict=True):
        if region is None:
            continue
        for head in _member_heads(entry):
            followed = [
                line.lstrip()[len(head) :]
                for line in region.split("\n")
                if line.lstrip().startswith(head)
            ]
            assert followed, f"{entry.name} restyled the key it rewrote past: {head!r}"
            assert not head.endswith(" ") or not followed[0].startswith(" "), (
                f"{entry.name} widened the separator it wrote its value after: {head!r}"
            )
            _CLAIMS["member-head"] += 1
    lines = _meta_lines(after)
    assert not [line for line in document.meta_lines if line.strip() and line != line.rstrip()], (
        "a spelling writes content followed by space, which the claim below reads as absent"
    )
    padded = [line for line in lines if line.strip() and line != line.rstrip()]
    assert not padded, f"a rewrite left space past the value it wrote: {padded!r}"
    for pattern, budgets in document.flow_lines(updates):
        matches = (re.fullmatch(pattern, line) for line in lines)
        match = next((found for found in matches if found is not None), None)
        assert match is not None, f"a flow line was restyled: no line matches {pattern!r}"
        for region, budget in zip(match.groups(), budgets, strict=True):
            spent = "".join(char for char in region if char in FLOW_INDICATORS)
            assert sorted(spent) == sorted(budget), (
                f"an edit restyled flow punctuation: wrote {region!r}, "
                f"whose indicators {spent!r} are not the allowed {budget!r}"
            )
            # The budget cannot see space, since space is no indicator, and neither can the
            # semantic oracle, since a flow scalar loads the same however much of it surrounds
            # the scalar. What bounds it is where it may sit: an edit writes a space only as
            # the one separating a pair it appended from what came before, or the one after
            # the key indicator it wrote, so every space in the region follows the punctuation
            # it was written after. Space anywhere else is source the edit added around a value
            # rather than the value itself, which the region's own ends catch only at its far
            # edge and not inside an appended item.
            #
            # A tab is separation YAML accepts in a flow collection exactly where a space is,
            # so it loads the same and no oracle above sees it, but it is not what an edit
            # writes: the separation a rewrite emits is one space. It is therefore out of
            # place everywhere rather than merely outside the two positions a space is in,
            # which is why the two characters are tested by one rule and not one predicate.
            loose = [
                index
                for index, char in enumerate(region)
                if char in FLOW_SEPARATION
                and (char != " " or index == 0 or region[index - 1] not in ",:")
            ]
            assert not loose, (
                "an edit wrote separation the source does not carry: "
                f"{region!r} is loose at {loose}"
            )
            _CLAIMS["flow-line"] += 1


def _uncommented(line: str) -> str:
    """Return ``line`` with any comment it ends on cut off.

    A ``#`` opens one only at the start of a line or after space, which is what keeps the one
    inside a ``ref`` from reading as a comment. No value written anywhere in this file carries a
    ``#`` after a space, so nothing a rewrite writes is cut here.
    """
    for index, char in enumerate(line):
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _value_opening(raw_meta: str, start: int) -> str:
    """Return the source opening the value written at ``start``, back to its own separator.

    A node property does not have to share a line with the value it opens, so reading the hash's
    own line answers only for the properties written there. The run is followed back across line
    breaks instead, to the ``:`` the member's value is written after, which is the point past
    which everything belongs to the value rather than to the key.

    Comments are cut out of it rather than counted. Layer 4 lets the author's comment stay
    between a key and the value under it, so a comment there is preserved source, while a
    property hidden in front of one is not: cutting the comment is what leaves the property
    visible instead of ending the search at it.

    Args:
        raw_meta: The rewritten frontmatter block.
        start: Where in it the written value begins.

    Returns:
        The source between the value and the separator opening it, comments removed.
    """
    opening: list[str] = []
    for line in reversed(raw_meta[:start].split("\n")):
        bare = _uncommented(line)
        separator = bare.rfind(":")
        if separator != -1:
            opening.append(bare[separator + 1 :])
            break
        opening.append(bare)
    return "".join(reversed(opening))


def _assert_replacements_stay_bare(after: str, updates: dict[str, str]) -> None:
    """Assert every hash a rewrite wrote lands bare, with no node property in front of it.

    Layer 4 has replacement consume the replaced value's node properties: the anchor and tag
    opening a ``seen`` are dropped with the run of space they leave, since a tag kept would
    retype the new hash and an anchor kept would bind a name to a value the author never wrote.
    No other assertion in this file sees that. The semantic oracle loads ``!!str hash`` and
    ``&name hash`` to the same string a bare hash gives, the footprint leaves the whole line an
    edit lands on to the rewriter, and the style claims read the source a rewrite wrote past
    rather than the text it wrote.

    The claim is made of the hash rather than of the line it sits on, because a line may carry a
    property the rewrite is entitled to write: a relocated anchor opens the displaced old value,
    and a key may be spelled through an alias. What is pinned is the whole opening of the value
    node, which layer 2 lets a property be written anywhere in: the run from the hash back to the
    ``:`` its member's value follows, line breaks included, has to be blank.
    """
    raw_meta = _parts(after).raw_meta
    for value in updates.values():
        for match in re.finditer(re.escape(value), raw_meta):
            written = _value_opening(raw_meta, match.start())
            assert not written.strip(), (
                f"a rewrite wrote {written!r} in front of the hash replacing {value!r}, "
                "whose node properties layer 4 has it consume"
            )


def _assert_relocation(document: Document, raw_meta: str, updates: dict[str, str]) -> None:
    """Assert a displaced anchored value landed where layer 4 says, spelled as it says.

    Two outcomes, and which one applies is decided by the model rather than read off the result,
    so neither can stand in for the other. Where an alias below the definition is not itself
    being rewritten, the displaced value is re-emitted at it and the anchor is defined once more.
    Where every alias in scope is itself being rewritten there is nothing left reading the old
    value, so the anchor goes with it and neither the definition nor an alias survives.

    The claim is opt-in through ``Entry.displaced`` because "the first alias below the
    definition" is not the rule everywhere: a reused anchor name rebinds at each definition, so
    that shape declares nothing here and is pinned by its own test.

    Args:
        document: The generated model.
        raw_meta: The rewritten frontmatter block.
        updates: The updates the rewrite was asked for.
    """
    applied = document.applied(updates)
    regions = _entry_regions(document, raw_meta)
    for entry in document.entries:
        if entry.anchor is None or entry.displaced is None or entry.ref not in applied:
            continue
        alias = f"*{entry.anchor}"
        readers = [
            other
            for other in document.entries
            if other is not entry and any(alias in line for line in other.lines)
        ]
        surviving = [other for other in readers if other.ref not in applied]
        if not surviving:
            dropped = f"{entry.name} kept the anchor {entry.anchor!r} after every alias reading "
            dropped += "it was rewritten too, so nothing is left for it to name"
            assert f"&{entry.anchor}" not in raw_meta, dropped
            assert alias not in raw_meta, dropped
            _CLAIMS["relocation-drop"] += 1
            continue
        landed = regions[document.entries.index(surviving[0])]
        assert landed is not None, f"{surviving[0].name} lost the site it is located by"
        assert entry.displaced in landed, (
            f"{entry.name} did not relocate {entry.displaced!r} onto "
            f"{surviving[0].name}, whose source is now {landed!r}"
        )
        assert raw_meta.count(f"&{entry.anchor}") == 1, (
            f"the anchor {entry.anchor!r} is defined "
            f"{raw_meta.count(f'&{entry.anchor}')} times after the relocation"
        )
        _CLAIMS["relocation"] += 1


def _assert_supported_round_trip(document: Document, updates: dict[str, str]) -> None:
    """Assert a layer 2 document rewrites correctly, preserving layer 3 and confining layer 4."""
    text = document.render()
    _assert_strict_disposition(document, text)
    expected_applied = document.applied(updates)
    assert expected_applied, "the generator must force at least one applicable update"

    rewrites = _rewrite_bytes(text, updates)

    assert len(rewrites) == 1
    after = rewrites[0].after.decode("utf-8")
    assert rewrites[0].applied == expected_applied
    assert _reload(after) == document.expected(updates)
    _assert_envelope_preserved(document, text, after)
    _assert_styles_preserved(document, after, updates)
    _assert_replacements_stay_bare(after, updates)
    _assert_relocation(document, _parts(after).raw_meta, updates)
    allowed, inserts = document.footprint(updates)
    _assert_footprint_confined(_meta_lines(text), _meta_lines(after), allowed, inserts)
    written = document.written_lines(updates)
    assert written is None or len(_meta_lines(after)) == written, (
        f"the rewrite left the block {len(_meta_lines(after))} lines long, "
        f"where the edits it was allowed write {written}"
    )
    if document.key_order is not None:
        reloaded = _reload(after)
        assert isinstance(reloaded, dict)
        assert tuple(reloaded) == document.key_order


# --------------------------------------------------------------------------------------------
# Strategies.
# --------------------------------------------------------------------------------------------

BODIES = ("", "body\n", "prose\n---\nnot frontmatter\n", "# Heading\n\ntext\n")
FENCES = ("---", " ---", "--- ", "  ---  ")


@st.composite
def envelopes(draw, *, endings=("\n",), directives=(None,)) -> Envelope:
    """Draw one layer 2a envelope, independent of the frontmatter it wraps."""
    trailing = draw(st.booleans())
    return Envelope(
        draw(st.integers(min_value=0, max_value=2)),
        draw(st.sampled_from(FENCES)),
        draw(st.sampled_from(FENCES)),
        trailing,
        draw(st.sampled_from(BODIES)) if trailing else "",
        draw(st.sampled_from(endings)),
        draw(st.sampled_from(directives)),
    )


@st.composite
def block_entries(draw, index: int) -> Entry:
    """Draw one entry written in a shape a block sequence can carry."""
    if draw(st.booleans()):
        return _whole_entry(index, draw(st.sampled_from(WHOLE_FORMS)))
    return _combined_entry(
        index, draw(st.sampled_from(REF_FORMS)), draw(st.sampled_from(SEEN_FORMS))
    )


@st.composite
def flow_entries(draw, index: int) -> Entry:
    """Draw one entry written in a shape a flow sequence can carry."""
    return _whole_entry(index, draw(st.sampled_from(FLOW_FORMS)))


def _updates(draw, entries: tuple[Entry, ...]) -> dict[str, str]:
    """Draw the planned updates, always including one that actually applies."""
    updates = {entries[0].ref: f"new{0:04x}beef"}
    for index, entry in enumerate(entries[1:], start=1):
        if draw(st.booleans()):
            updates[entry.ref] = f"new{index:04x}beef"
    return updates


def _root_members(draw, index_base: int) -> list[tuple[str, list[str], object]]:
    """Draw the optional root members, each as its key, its source lines, and its value."""
    members: list[tuple[str, list[str], object]] = []
    chosen = draw(st.lists(st.sampled_from(ROOT_EXTRAS), max_size=2, unique_by=lambda e: e[0]))
    for extra in chosen:
        lines = [line.format(index=index_base) for line in extra[1]]
        members.append((extra[0], lines, _root_key_value(extra, index_base)))
    return members


@st.composite
def block_root_documents(draw) -> Document:
    """Draw a document whose root is a block mapping, the commonest supported shape."""
    count = draw(st.integers(min_value=1, max_value=3))
    entries = tuple(draw(block_entries(index)) for index in range(count))
    carrier = "flow" if all(entry.inline for entry in entries) and draw(st.booleans()) else "block"
    pad = draw(st.sampled_from((0, 2)))
    id_lines = draw(st.sampled_from((["id: doc"], ["? id", ": doc"])))
    # Layer 2 allows the explicit pair at any root key, so it is drawn for the key the entries
    # are located under as well as for ``id``. The two are not the same claim: locating a
    # sequence under a key spelled that way is what the rewriter has to keep doing, and no
    # other generator writes the spelling with the literal key, only through an alias.
    key_lines = draw(st.sampled_from((["derives_from:"], ["? derives_from", ":"])))
    members = _root_members(draw, count)
    comment = draw(st.sampled_from(("", "# root note")))

    lines: list[str] = []
    if comment:
        lines.append(comment)
    lines.extend(id_lines)
    for _, extra_lines, _ in members:
        lines.extend(extra_lines)
    body, spans = _block_sequence(entries, pad)
    offset = len(lines) + len(key_lines)
    # A flow carrier shares its line with the entries it holds, so the whole line is inside the
    # allowed footprint and the flow-line assertion is what pins it. A block carrier sits on a
    # line of its own outside that footprint, which the footprint assertion already pins.
    if carrier == "flow":
        lines.extend(_member_lines(key_lines, entries, "flow", pad))
        spans = [(offset - 1, offset)] * count
    else:
        lines.extend(key_lines)
        lines.extend(body)
        spans = [(start + offset, stop + offset) for start, stop in spans]

    root: dict[str, object] = {"id": "doc"}
    for key, _, value in members:
        root[key] = value
    order = ("id", *(key for key, _, _ in members), "derives_from")
    notes = [comment] if comment else []
    for entry in entries:
        notes.extend(entry.notes)
    assembled = Document(
        tuple(lines), entries, tuple(spans), root, order, _flat_envelope(), tuple(notes)
    )
    return _finish(draw, assembled)


def _finish(draw, document: Document) -> Document:
    """Attach a drawn envelope to an assembled document, shifting spans past any directive."""
    envelope = draw(envelopes())
    lines, spans = document.meta_lines, document.spans
    if envelope.directive is not None:
        lines = (f"%YAML {envelope.directive}", "--- !!map", *lines)
        spans = tuple((start + 2, stop + 2) for start, stop in spans)
    return replace(document, meta_lines=lines, spans=spans, envelope=envelope)


@st.composite
def ordered_map_root_documents(draw) -> Document:
    """Draw a document whose root is an ``!!omap`` or a flow mapping."""
    count = draw(st.integers(min_value=1, max_value=2))
    style = draw(st.sampled_from(("omap-block", "omap-flow", "flow-map")))
    if style == "omap-block":
        entries = tuple(draw(block_entries(index)) for index in range(count))
        return _ordered_map_block(draw, entries)
    entries = tuple(draw(flow_entries(index)) for index in range(count))
    inlines = FLOW_SEPARATOR.join(entry.inline or "" for entry in entries)
    if style == "omap-flow":
        line = f"!!omap [{{id: doc}}, {{derives_from: [{inlines}]}}]"
    else:
        line = f"{{id: doc, derives_from: [{inlines}]}}"
    spans = tuple((0, 1) for _ in entries)
    order = ("id", "derives_from")
    return _finish(
        draw,
        Document((line,), entries, spans, {"id": "doc"}, order, _flat_envelope(), ()),
    )


def _ordered_map_block(draw, entries: tuple[Entry, ...]) -> Document:
    """Assemble an ``!!omap`` root written in block style around the given entries."""
    pad = draw(st.sampled_from((0, 2)))
    body, spans = _block_sequence(entries, pad)
    lines = ["!!omap", "- id: doc", "- derives_from:", *_indent(tuple(body), 2)]
    offset = 3
    spans = tuple((start + offset, stop + offset) for start, stop in spans)
    notes = tuple(note for entry in entries for note in entry.notes)
    order = ("id", "derives_from")
    return _finish(
        draw, Document(tuple(lines), entries, spans, {"id": "doc"}, order, _flat_envelope(), notes)
    )


@st.composite
def alias_and_merge_documents(draw) -> Document:
    """Draw the alias-, anchor- and merge-heavy shapes layer 2 keeps in its strict column.

    The reused-anchor shape used to move between this pool and the family 2c safe-outcome
    union depending on which parser the strict boundary happened to be running. The strict
    boundary now pins the pure parser (AD-33), so the shape is strictly loadable on every leg
    and belongs here unconditionally.
    """
    builders = {
        "aliased-entry": _aliased_entry_document,
        "relocating-anchor": _relocating_anchor_document,
        "relocating-null": _relocating_null_document,
        "merge": _merge_document,
        "tagged-merge": _merge_document,
        "entry-merge": _entry_merge_document,
        "tagged-entry-merge": _entry_merge_document,
        "entry-inherits-seen": _inherited_seen_document,
        "tagged-entry-inherits-seen": _inherited_seen_document,
        "alias-spelled-entry-key": _alias_spelled_key_document,
        "alias-spelled-ref-key": _alias_spelled_key_document,
        "alias-spelled-root-key": _alias_spelled_key_document,
        "alias-spelled-derives-key": _alias_spelled_key_document,
        "reused-anchor": _reused_anchor_document,
    }
    shape = draw(st.sampled_from(tuple(builders)))
    return builders[shape](draw, shape)


def _entry(
    index: int,
    lines: tuple[str, ...],
    seen: str | None,
    anchor: str | None,
    *,
    flow: bool = False,
) -> Entry:
    """Build an entry whose source is spelled out rather than composed from the tables.

    An entry written across several lines is modelled the way a table-built one is: the ``seen``
    member is the only line an edit may land on, so the lines the caller wrote above it stay
    outside the footprint whether or not a marker or a style claim also covers them. An entry
    written on one line has no narrower footprint than its span, so it keeps the whole-span
    allowance and its opening is what the marker pins.

    A one-line entry written in flow style needs more than that allowance, since the whole line
    is inside it and the marker stops where the edit begins: everything between the ``seen`` key
    and the bracket the entry closes on would be the rewriter's to restyle. Such an entry carries
    its own flow pin instead, the way a table-built flow entry does, which is what holds the
    member syntax around the value it replaces.

    Args:
        index: The entry's position, which its substituted fields are numbered by.
        lines: The entry's source lines, as templates.
        seen: The value its ``seen`` member loads as, or None when it writes none.
        anchor: The anchor name written on its ``seen`` value, when it carries one.
        flow: Whether the one line it is written on is a flow collection to be pinned.

    Returns:
        The modelled entry.
    """
    fields = _fields(index)
    written = tuple(line.format(**fields) for line in lines)
    edits = _seen_member_offsets(written)
    inline = written[0][len("- ") :] if flow else None
    return Entry(
        "explicit",
        written,
        inline,
        fields["ref"],
        seen,
        seen is not None,
        anchor,
        (),
        (),
        marker=_style_marker(written[0]),
        edits=edits if len(written) > 1 else None,
        pin=None if inline is None else _flow_pin(inline, "none"),
        site=fields["head"],
        written_lines=1 if len(written) > 1 else None,
    )


def _aliased_entry_document(draw, _shape: str) -> Document:
    """An entry written as an alias to another entry, which shares its loaded mapping."""
    style = draw(st.sampled_from(("flow", "block")))
    if style == "flow":
        first = _entry(0, ("- &edge {{ref: {ref}, seen: {old}}}",), "old0000", None, flow=True)
    else:
        first = _entry(0, ("- &edge", "    ref: {ref}", "    seen: {old}"), "old0000", None)
    second = Entry("alias-entry", ("- *edge",), None, first.ref, first.seen, True, None, ())
    lines = ["id: doc", "derives_from:", *first.lines, *second.lines]
    spans = ((2, 2 + len(first.lines)), (2 + len(first.lines), len(lines)))
    order = ("id", "derives_from")
    entries = (first, second)
    return _finish(
        draw, Document(tuple(lines), entries, spans, {"id": "doc"}, order, _flat_envelope(), ())
    )


def _reused_anchor_document(draw, _shape: str) -> Document:
    """Draw the reused-anchor shape under an envelope."""
    return _finish(draw, _reused_anchor_pair())


def _reused_anchor_pair() -> Document:
    """A reused anchor name whose later definition rebinds the alias sites below it."""
    first = _entry(0, ("- ref: {ref}", "  seen: &shared {old}"), "old0000", "shared")
    second = _entry(1, ("- ref: {ref}", "  seen: &shared {old}"), "old0001", "shared")
    third = Entry(
        "alias-of-rebound",
        ("- ref: up-2#s2", "  seen: *shared"),
        None,
        "up-2#s2",
        "old0001",
        True,
        None,
        (),
        edits=(1,),
        written_lines=1,
    )
    lines = ["id: doc", "derives_from:", *first.lines, *second.lines, *third.lines]
    entries = (first, second, third)
    spans = ((2, 4), (4, 6), (6, 8))
    order = ("id", "derives_from")
    return Document(tuple(lines), entries, spans, {"id": "doc"}, order, _flat_envelope(), ())


NEL = "\u0085"
CSI = "\u009b"

# The values an anchored ``seen`` is relocated as, each written the way the author spelled it and
# paired with what it constructs to. The loaded value is written out rather than derived: the
# obvious derivation, ``unicode_escape``, is not a YAML double-quoted decoder, since it has no
# ``\/``, ``\N``, ``\_``, ``\L`` or ``\P`` and reads non-ASCII as latin-1, so it happens to agree
# with YAML for the rows below and would quietly disagree for a row using any of those.
# One table rather than two: the deterministic test and the drawn shape make the same claim about
# the same builder, and they had drifted into deriving the expected value two different ways.
RELOCATED_CONTENTS = (
    ('"old0000"', "old0000", "plain"),
    ('"p\\u0085q"', f"p{NEL}q", "nel"),
    ('"a\\u009bb"', f"a{CSI}b", "c1-control"),
    ('"p\\u0085q\\u009b\\tr"', f"p{NEL}q{CSI}\tr", "nel-and-c1-and-tab"),
    ('"has \\"quote\\" and \\\\ slash"', 'has "quote" and \\ slash', "quotes-and-backslashes"),
    ('"tab\\there"', "tab\there", "tab"),
)


def _relocating_anchor_pair(
    written: str, value: object, *, source: str | None = None, displaced: str | None = None
) -> Document:
    """Build an anchored ``seen`` and the alias site its old value is relocated onto.

    The anchored entry is modelled member by member, the way a table-built block entry is: its
    ``seen`` is the one line an edit may land on, and the opening it hangs off comes back
    verbatim. Leaving it to the whole-span allowance instead would let a rewrite restyle the
    ``ref`` beside it, which no claim here would see: the relocation is what this shape is
    generated for, and it reaches the ``seen`` member alone.

    The alias site is modelled the same way, since only its value is an alias: layer 4 replaces
    that node with the value the anchored member displaced, and writes it past the ``seen`` key
    the author spelled there like any other member it rewrites.

    Args:
        written: The anchored ``seen`` value as the author spelled it, quoting included.
        value: The value that source loads as, at both sites.
        source: The whole anchored member value, for the spellings ``&shared {written}`` cannot
            write. The bare anchor is the one that needs it, since it carries no value at all
            and would otherwise be written with a trailing space.
        displaced: What the relocation has to re-emit at the alias site. Defaults to the source
            as written, which is right wherever the value comes back spelled as the author wrote
            it, and is given explicitly where layer 4 re-spells it.

    Returns:
        The two-entry document, with the footprint of a relocation modelled on it.
    """
    aliased = ("- ref: up-1#s1", "  seen: *shared")
    member = source if source is not None else f"&shared {written}"
    anchored = ("- ref: up-0#s0", f"  seen: {member}")
    first = Entry(
        "anchored-seen",
        anchored,
        None,
        "up-0#s0",
        value,
        True,
        "shared",
        (),
        (),
        marker=_style_marker(anchored[0]),
        edits=(1,),
        site="up-0#s0",
        written_lines=1,
        displaced=displaced if displaced is not None else f"&shared {written}",
    )
    second = Entry(
        "alias-seen",
        aliased,
        None,
        "up-1#s1",
        value,
        True,
        None,
        (),
        (),
        marker=_style_marker(aliased[0]),
        edits=(1,),
        site="up-1#s1",
        written_lines=1,
    )
    lines = ["id: doc", "derives_from:", *first.lines, *second.lines]
    return Document(
        tuple(lines),
        (first, second),
        ((2, 4), (4, 6)),
        {"id": "doc"},
        ("id", "derives_from"),
        _flat_envelope(),
        (),
    )


def _relocating_anchor_document(draw, _shape: str) -> Document:
    """An anchored ``seen`` whose replacement relocates the old value onto its alias site."""
    written, value, _ = draw(st.sampled_from(RELOCATED_CONTENTS))
    return _finish(draw, _relocating_anchor_pair(written, value))


# The three ways a null is written under an anchor. Layer 4 re-emits a displaced null in its own
# spelling rather than through the tag lifecycle, so all three relocate as `&shared null` however
# they were written, and the semantic oracle cannot tell the three apart: each loads as None at
# both sites whichever of them the rewrite emitted.
NULL_ANCHOR_SOURCES = ("&shared null", "&shared ~", "&shared")


def _relocating_null_document(draw, _shape: str) -> Document:
    """An anchored null whose replacement relocates it onto its alias site.

    Strict-column, unlike the bool and the multi-line tagged scalar in the recovery pool, because
    ``RawEdge.seen`` admits null at both the anchored member and the alias site.
    """
    source = draw(st.sampled_from(NULL_ANCHOR_SOURCES))
    return _finish(draw, _relocating_anchor_pair("", None, source=source, displaced="&shared null"))


def _merge_document(draw, shape: str) -> Document:
    """A ``derives_from`` supplied by a merge key, in the plain and the tagged spelling."""
    key = "<<" if shape == "merge" else "!!merge inherited"
    inner = "ref: up-0#s0, seen: old0000"
    inline = f"{{{inner}}}"
    lines = ["id: doc", f"{key}: {{derives_from: [{inline}]}}"]
    merged = Entry(
        "merge-supplied",
        (),
        inline,
        "up-0#s0",
        "old0000",
        True,
        None,
        (),
        (),
        marker=_style_marker(inline),
        edits=None,
        appends=False,
        pin=_flow_pin(inline, "none"),
    )
    assembled = Document(
        tuple(lines), (merged,), ((1, 2),), {"id": "doc"}, None, _flat_envelope(), ()
    )
    return _finish(draw, assembled)


# One row per way an entry's members can be split between a merge key and the entry itself,
# which layer 2 declares at the `Entry` key spelling row: either member may arrive through a
# merge, and an own member shadows a merged one of the same name. Each row is the entry's source
# lines, the `seen` it loads as, the offsets an edit may land on, and whether the edit instead
# takes a line of its own past the entry. A merged `seen` is written to rather than edited in
# place: the rewriter gives the entry a `seen` of its own that shadows the merge, which leaves
# the merge source alone and so leaves every other entry reading it alone too.
ENTRY_MERGE_SHAPES = (
    ("own-seen", ("- {key}: {{ref: {ref}}}", "  seen: {old}"), "old0000", (1,), False),
    ("no-seen", ("- {key}: {{ref: {ref}}}",), None, (), True),
    ("merged-seen", ("- {key}: {{seen: {old}}}", "  ref: {ref}"), "old0000", (), True),
    ("merged-pair", ("- {key}: {{ref: {ref}, seen: {old}}}",), "old0000", (), True),
    (
        "merged-seen-shadowed",
        ("- {key}: {{seen: merged00}}", "  ref: {ref}", "  seen: {old}"),
        "old0000",
        (2,),
        False,
    ),
)


def _entry_merge_document(draw, shape: str) -> Document:
    """An entry whose members arrive through a merge key, in either merge spelling.

    The merge line is the entry's own source and no part of what an update rewrites: the value
    it names lives in the mapping the merge pulls in, and the ``seen`` this update lands on is
    either a member of the entry itself or one written on a line of its own just past it. The
    footprint is modelled that way rather than left to the whole-span allowance, which would
    let a rewrite restyle the merge key beside the edit with nothing here to see it.

    Which members the merge supplies is drawn rather than fixed, since layer 2 declares both of
    them at that row and they reach planning differently: an entry spelling its own ``seen`` is
    edited where it stands, while one reading a merged ``seen`` is written a member that shadows
    it. Generating only the first would leave the second's planning path unreached from here.
    """
    key = "<<" if shape == "entry-merge" else "!!merge inherited"
    name, templates, seen, edits, appends = draw(st.sampled_from(ENTRY_MERGE_SHAPES))
    fields = _fields(0)
    entry_lines = tuple(line.format(key=key, **fields) for line in templates)
    entry = Entry(
        f"{shape}-{name}",
        entry_lines,
        None,
        fields["ref"],
        seen,
        seen is not None,
        None,
        (),
        (),
        marker=_style_marker(entry_lines[0]),
        edits=edits,
        appends=appends,
        site=fields["head"],
        written_lines=1,
    )
    lines = ("id: doc", "derives_from:", *_indent(entry.lines, 2))
    order = ("id", "derives_from")
    assembled = Document(
        lines, (entry,), ((2, len(lines)),), {"id": "doc"}, order, _flat_envelope(), ()
    )
    return _finish(draw, assembled)


def _inherited_seen_document(draw, shape: str) -> Document:
    """Two entries where the second reads the first's ``seen`` through a merge key.

    This is the one shape where writing a hash at one entry changes another, since the loader
    flattens a merge into a copy rather than sharing the object an alias would. Updating only
    the source therefore has to leave the second entry alone in source and still change what it
    loads as, while an update naming the second writes it a ``seen`` that shadows the merged one
    from then on. Both are drawn, since ``_updates`` decides whether the second is named.
    """
    key = "<<" if shape == "entry-inherits-seen" else "!!merge inherited"
    source = Entry(
        "merge-source",
        ("- &source", "  ref: up-0#s0", "  seen: old0000"),
        None,
        "up-0#s0",
        "old0000",
        True,
        None,
        (),
        (),
        marker=_style_marker("- &source"),
        edits=(2,),
        appends=False,
        site="up-0#s0",
        written_lines=1,
    )
    inheritor = Entry(
        "merge-inheritor",
        (f"- {key}: *source", "  ref: up-1#s1"),
        None,
        "up-1#s1",
        "old0000",
        True,
        None,
        (),
        (),
        marker=_style_marker(f"- {key}: *source"),
        edits=(),
        appends=True,
        site="up-1#s1",
        written_lines=1,
        inherits="up-0#s0",
    )
    lines = (
        "id: doc",
        "derives_from:",
        *_indent(source.lines, 2),
        *_indent(inheritor.lines, 2),
    )
    spans = ((2, 2 + len(source.lines)), (2 + len(source.lines), len(lines)))
    order = ("id", "derives_from")
    assembled = Document(
        lines, (source, inheritor), spans, {"id": "doc"}, order, _flat_envelope(), ()
    )
    return _finish(draw, assembled)


def _alias_spelled_key_document(draw, shape: str) -> Document:
    """A mapping key spelled through an alias, at every position AD-31 declares one at.

    The four positions are the entry's ``seen`` key, the entry's ``ref`` key, the root ``id``
    key and the root ``derives_from`` key, each drawn in both spellings the subset admits: the
    explicit ``? *name`` and ``: value`` pair, and ``*name : value`` with the space before the
    colon. The bare ``*name:`` form does not scan and so is not generated.

    The anchor a key alias reads has to be defined on a value ``NodeMeta`` allows, which is
    what keeps this spelling inside the strict column rather than the reread-only one.
    """
    explicit = draw(st.booleans())
    root: dict[str, object] = {"id": "doc"}
    if shape == "alias-spelled-entry-key":
        member = ["? *keyname", ": old0000"] if explicit else ["*keyname : old0000"]
        entry_lines = ("- ref: up-0#s0", *(f"  {line}" for line in member))
        entry = Entry(
            "alias-spelled-seen-key",
            entry_lines,
            None,
            "up-0#s0",
            "old0000",
            True,
            None,
            (),
            (),
            marker=_style_marker(entry_lines[0]),
            # The alias spells the key, not the value, so the rewrite lands on the member the
            # same way it does for an ordinary explicit pair: the `ref` line above it is
            # untouched, and the whole entry is not the rewriter's to restyle.
            edits=tuple(range(1, len(entry_lines))),
            site="up-0#s0",
            # The explicit spelling keeps the aliased key on a line of its own, so the member is
            # written on two lines afterwards the way any explicit pair is.
            written_lines=2 if explicit else 1,
        )
        lines = ("id: doc", "title: &keyname seen", "derives_from:", *_indent(entry.lines, 2))
        root["title"] = "seen"
        order: tuple[str, ...] = ("id", "title", "derives_from")
        spans = ((3, len(lines)),)
    elif shape == "alias-spelled-ref-key":
        entry_lines = (
            ("- ? *keyname", "  : up-0#s0", "  seen: old0000")
            if explicit
            else ("- *keyname : up-0#s0", "  seen: old0000")
        )
        entry = Entry(
            "alias-spelled-ref-key",
            entry_lines,
            None,
            "up-0#s0",
            "old0000",
            True,
            None,
            (),
            (),
            marker=_style_marker(entry_lines[0]),
            # Only the `seen` member is rewritten; the aliased `ref` key above it, in either
            # spelling, stays exactly as it was written.
            edits=(len(entry_lines) - 1,),
            site="up-0#s0",
            written_lines=1,
        )
        lines = ("id: doc", "title: &keyname ref", "derives_from:", *_indent(entry.lines, 2))
        root["title"] = "ref"
        order = ("id", "title", "derives_from")
        spans = ((3, len(lines)),)
    elif shape == "alias-spelled-derives-key":
        entry = _entry(0, ("- ref: {ref}", "  seen: {old}"), "old0000", None)
        # The block sequence hangs under the aliased key, so the entry is indented beneath it
        # rather than sitting at the root column the plain spelling puts it at.
        head = ("? *dfkey", ":") if explicit else ("*dfkey :",)
        lines = ("id: doc", "title: &dfkey derives_from", *head, *_indent(entry.lines, 2))
        root["title"] = "derives_from"
        order = ("id", "title", "derives_from")
        spans = ((2 + len(head), len(lines)),)
    else:
        entry = _entry(0, ("- ref: {ref}", "  seen: {old}"), "old0000", None)
        head = ("? *idkey", ": doc") if explicit else ("*idkey : doc",)
        lines = ("title: &idkey id", *head, "derives_from:", *_indent(entry.lines, 2))
        root["title"] = "id"
        order = ("title", "id", "derives_from")
        spans = ((len(head) + 2, len(lines)),)
    assembled = Document(lines, (entry,), spans, root, order, _flat_envelope(), ())
    return _finish(draw, assembled)


def _flat_envelope() -> Envelope:
    """Return the plain LF envelope the explicitly spelled documents are rendered with."""
    return Envelope(0, "---", "---", True, "body\n", "\n", None)


# --------------------------------------------------------------------------------------------
# Family 1: supported writes.
# --------------------------------------------------------------------------------------------


@FUZZ_SETTINGS
@given(data=st.data())
def test_block_root_documents_round_trip(data) -> None:
    document = data.draw(block_root_documents())
    updates = _updates(data.draw, document.entries)
    _assert_supported_round_trip(document, updates)


@FUZZ_SETTINGS
@given(data=st.data())
def test_ordered_map_and_flow_roots_round_trip(data) -> None:
    document = data.draw(ordered_map_root_documents())
    updates = _updates(data.draw, document.entries)
    _assert_supported_round_trip(document, updates)


@EXPECT_REUSED_ANCHOR
@FUZZ_SETTINGS
@given(data=st.data())
def test_alias_and_merge_shapes_round_trip(data) -> None:
    """Rewrite one entry of a shared-node shape, or every entry of it.

    Updating every entry is what reaches the case where an anchored value has nowhere to be
    relocated to, because the alias that would have received it is being rewritten in the same
    pass. The rewriter drops the anchor there rather than republishing a value nothing reads,
    and with a single target that arm was unreachable. Drawn against the single-target case
    rather than replacing it, so the commoner shape keeps half the examples.
    """
    document = data.draw(alias_and_merge_documents())
    target = data.draw(st.integers(min_value=0, max_value=len(document.entries) - 1))
    refs = (
        tuple(entry.ref for entry in document.entries)
        if data.draw(st.booleans())
        else (document.entries[target].ref,)
    )
    # dict.fromkeys, not a set: the aliased-entry shape gives its two entries the same ref, and
    # the hash written has to stay a function of position rather than of iteration order.
    updates = {ref: f"new{index:04x}beef" for index, ref in enumerate(dict.fromkeys(refs))}
    _assert_supported_round_trip(document, updates)


@FUZZ_SETTINGS
@given(data=st.data())
def test_envelopes_and_line_endings_round_trip(data) -> None:
    count = data.draw(st.integers(min_value=1, max_value=2))
    entries = tuple(data.draw(block_entries(index)) for index in range(count))
    lines = ["id: doc", "derives_from:"]
    body, spans = _block_sequence(entries, 2)
    lines.extend(body)
    spans = tuple((start + 2, stop + 2) for start, stop in spans)
    envelope = data.draw(
        envelopes(endings=("\n", "\r\n", "\r", "mixed"), directives=(None, "1.1", "1.2"))
    )
    meta = tuple(lines)
    if envelope.directive is not None:
        meta = (f"%YAML {envelope.directive}", "--- !!map", *meta)
        spans = tuple((start + 2, stop + 2) for start, stop in spans)
    notes = tuple(note for entry in entries for note in entry.notes)
    document = Document(
        meta, entries, spans, {"id": "doc"}, ("id", "derives_from"), envelope, notes
    )
    _assert_supported_round_trip(document, _updates(data.draw, entries))


@pytest.mark.parametrize("seen_form", SEEN_FORMS, ids=lambda form: form.name)
@pytest.mark.parametrize("ref_form", REF_FORMS, ids=lambda form: form.name)
def test_every_ref_and_seen_spelling_pair_round_trips(
    ref_form: RefForm, seen_form: SeenForm
) -> None:
    """Cover the layer 2 spelling tables exhaustively, which sampling alone cannot promise."""
    entry = _combined_entry(0, ref_form, seen_form)
    _assert_supported_round_trip(_single_entry_document(entry), {entry.ref: "new0000beef"})


def _commented_pairs() -> tuple[tuple[RefForm, SeenForm], ...]:
    """Return every spelling pair where at least one side writes a comment.

    Built at collection time so each pair is its own test item and a failure names the two
    spellings that produced it. The plain form is drawn in on each side as the neutral partner,
    so a commented spelling is exercised against an uncommented one as well as against another
    commented one, but the pair where neither carries a comment has nothing to observe.
    """
    refs = tuple(form for form in REF_FORMS if form.note or form.name == "plain")
    seens = tuple(form for form in SEEN_FORMS if form.note or form.name == "plain")
    return tuple(
        (ref_form, seen_form)
        for ref_form in refs
        for seen_form in seens
        if ref_form.note or seen_form.note
    )


COMMENTED_PAIRS = _commented_pairs()


@pytest.mark.parametrize(
    ("ref_form", "seen_form"),
    COMMENTED_PAIRS,
    ids=lambda form: form.name,
)
def test_every_commented_spelling_keeps_its_comment_at_its_own_site(
    ref_form: RefForm, seen_form: SeenForm
) -> None:
    """Rewrite two commented members at once, the only shape a comment can move inside of.

    One entry cannot tell a comment that moved from one that was kept: the only place it could
    move to is outside the block, and mere presence catches that. Two rewritten members that
    each carry a comment is what makes the site an observable, so every spelling that writes
    one is generated at both positions and both entries are updated in the same pass.
    """
    entries = tuple(_combined_entry(index, ref_form, seen_form) for index in range(2))
    updates = {entry.ref: f"new{index:04x}beef" for index, entry in enumerate(entries)}
    _assert_supported_round_trip(_entry_pair_document(entries), updates)


@pytest.mark.parametrize("form", WHOLE_FORMS, ids=lambda form: form.name)
def test_every_whole_entry_spelling_round_trips(form: WholeForm) -> None:
    """Cover every entry spelling that is written as one indivisible shape."""
    entry = _whole_entry(0, form)
    _assert_supported_round_trip(_single_entry_document(entry), {entry.ref: "new0000beef"})


def _anchor_definitions(lines: tuple[str, ...]) -> list[str]:
    """Return every anchor name a frontmatter block defines, in source order."""
    names: list[str] = []
    for line in lines:
        for piece in line.split("&")[1:]:
            name = piece.split(" ")[0].split(",")[0].split("}")[0].split("]")[0]
            if name:
                names.append(name)
    return names


def test_the_spelling_tables_never_define_one_anchor_name_twice() -> None:
    """Guard the tables against growing a duplicate anchor definition by accident.

    A reused anchor name rebinds at each definition, so a table entry that grew one would
    silently change which value an alias elsewhere in the same block reads, and every spelling
    built from that entry would be asserting against a document nobody wrote on purpose. The
    shape is modelled deliberately elsewhere, by ``_reused_anchor_pair``, which is where it
    belongs. Two entries are built so the per-entry naming scheme is checked as well as each
    spelling on its own.
    """
    offenders: list[str] = []
    for ref_form in REF_FORMS:
        for seen_form in SEEN_FORMS:
            pair = tuple(
                line
                for index in (0, 1)
                for line in _combined_entry(index, ref_form, seen_form).lines
            )
            names = _anchor_definitions(pair)
            if len(names) != len(set(names)):
                offenders.append(f"{ref_form.name}+{seen_form.name}: {names}")
    for form in WHOLE_FORMS:
        pair = tuple(line for index in (0, 1) for line in _whole_entry(index, form).lines)
        names = _anchor_definitions(pair)
        if len(names) != len(set(names)):
            offenders.append(f"{form.name}: {names}")
    # Accumulated rather than asserted in the loop: this guard is about the tables as a whole,
    # and a naming scheme that broke would break for many spellings at once, so stopping at the
    # first would name one and hide the rest. It stays a single test for the same reason.
    assert not offenders, f"these spellings define one anchor name twice: {offenders}"


def test_the_spelling_tables_still_carry_every_dimension_the_assertions_read() -> None:
    """Keep each conditional claim from going vacuous because the last row feeding it was cut.

    Most of the layer 3 claims are made only of the shapes that can carry them: a comment is
    checked where a spelling writes one, a flow line where an entry is written in flow style, a
    member head where the model gives an entry one to read. Each skip is right for its shape,
    and together they mean a claim can stop being made everywhere without a single test going
    red. This is the floor under that: the corpus still has to contain something to make each
    claim of.
    """
    assert any(form.note for form in REF_FORMS), "no ref spelling writes a comment"
    assert any(form.site is not None for form in REF_FORMS), (
        "no ref spelling escapes its value, so every entry is locatable by source that spells "
        "its ref out and nothing reads what the loader constructed instead"
    )
    assert any(form.note for form in SEEN_FORMS), "no seen spelling writes a comment"
    assert any(form.anchored for form in SEEN_FORMS), "no seen spelling carries an anchor"
    assert any(form.written_lines == 2 for form in SEEN_FORMS), (
        "no seen spelling keeps source above its value, so nothing exercises a two-line member"
    )
    assert any(form.value in {"null", "empty-string"} for form in SEEN_FORMS), (
        "no seen spelling writes an empty value, so the property-removal edits are unreached"
    )
    assert FLOW_FORMS, "no entry spelling is written in flow style"
    assert any(form.writes != "none" for form in FLOW_FORMS), (
        "no flow spelling makes the rewrite write an indicator, so the budget is never spent"
    )
    assert COMMENTED_PAIRS, "no spelling pair carries a comment for the site claim to be made of"


def test_every_flow_spelling_produces_a_pin_that_bounds_something() -> None:
    """Keep the flow claim from going quiet through a shape whose spans are merely right.

    ``Document.__post_init__`` catches a span that is wrong. It cannot catch a shape whose
    spans are right and whose entries all carry no pin, which disables the flow-line assertion
    just as completely and just as silently, because ``flow_lines`` returns nothing to check
    rather than failing. This asserts each flow spelling really does produce one, and that the
    pin bounds something rather than matching anything.
    """
    for form in FLOW_FORMS:
        entry = _whole_entry(0, form)
        document = _single_entry_document(entry)
        pins = document.flow_lines({entry.ref: "new0000beef"})
        assert pins, f"{form.name} produced no flow pin, so its line is unbounded"
        for pattern, budgets in pins:
            assert "(" in pattern, f"{form.name} pinned no editable region: {pattern!r}"
            assert budgets, f"{form.name} pinned a region with no indicator budget"


def test_the_declared_vocabularies_and_the_ones_the_tables_use_are_the_same() -> None:
    """Catch a declared kind no row uses, and a row using a kind nothing declares.

    ``_seen_value`` raises on a kind it does not know, so the second direction already fails
    loudly at build time. The first is the quiet one: a value left in the ``Literal`` after the
    last row using it was cut reads as covered and is not.
    """
    used = {form.value for form in SEEN_FORMS} | {form.value for form in WHOLE_FORMS}
    assert used == SEEN_KINDS, f"declared {sorted(SEEN_KINDS)}, used {sorted(used)}"
    written = {form.writes for form in WHOLE_FORMS}
    assert written == FLOW_EDIT_KINDS, f"declared {sorted(FLOW_EDIT_KINDS)}, used {sorted(written)}"
    assert set(FLOW_EDIT_INDICATORS) == FLOW_EDIT_KINDS, (
        "the indicator table and the flow-edit vocabulary have drifted apart"
    )


def _entry_pair_document(entries: tuple[Entry, ...]) -> Document:
    """Wrap several entries in one block sequence under the plainest supported root."""
    body, spans = _block_sequence(entries, 2)
    return Document(
        ("id: doc", "derives_from:", *body),
        entries,
        tuple((start + 2, stop + 2) for start, stop in spans),
        {"id": "doc"},
        ("id", "derives_from"),
        _flat_envelope(),
        tuple(note for entry in entries for note in entry.notes),
    )


def _single_entry_document(entry: Entry, root_lines: tuple[str, ...] = ("id: doc",)) -> Document:
    """Wrap one entry in the plainest supported block-mapping root and LF envelope."""
    lines = [*root_lines, "derives_from:", *_indent(entry.lines, 2)]
    start = len(root_lines) + 1
    return Document(
        tuple(lines),
        (entry,),
        ((start, len(lines)),),
        {"id": "doc"},
        ("id", "derives_from"),
        _flat_envelope(),
        entry.notes,
    )


# --------------------------------------------------------------------------------------------
# Family 2a: refusals, generated at the exact scope AD-31 layer 5 guarantees each one at.
# --------------------------------------------------------------------------------------------

# Each row carries the message its refusal has to give, because this pool produces three
# different ones and a single shared pattern would let any row answer for any other. Only the
# project's own prose is pinned: everything after ``to reconcile: `` is the loader's, and it is
# worded differently across the ruamel releases and accelerator cells CI runs.
UNREADABLE_ON_ANY_REREAD = (
    (
        "unclosed-fence",
        "id: doc\nderives_from:\n  - ref: up-0#s0\n",
        False,
        r"unclosed YAML frontmatter in 'doc\.md'",
    ),
    (
        "unparseable-flow",
        "id: doc\nderives_from: [1, 2\n",
        True,
        r"cannot parse frontmatter of 'doc\.md' to reconcile: ",
    ),
    (
        "unparseable-indent",
        "id: doc\n  stray: 1\nderives_from: []\n",
        True,
        r"cannot parse frontmatter of 'doc\.md' to reconcile: ",
    ),
    (
        "root-block-sequence",
        "- one\n- two\n",
        True,
        r"frontmatter of 'doc\.md' is not a mapping; cannot reconcile",
    ),
    (
        "root-flow-sequence",
        "[one, two]\n",
        True,
        r"frontmatter of 'doc\.md' is not a mapping; cannot reconcile",
    ),
    (
        "root-bare-scalar",
        "just prose\n",
        True,
        r"frontmatter of 'doc\.md' is not a mapping; cannot reconcile",
    ),
    (
        "root-quoted-scalar",
        '"just prose"\n',
        True,
        r"frontmatter of 'doc\.md' is not a mapping; cannot reconcile",
    ),
)

NON_LIST_DERIVES_FROM = (
    "derives_from: 5",
    "derives_from: text",
    "derives_from: {up-0#s0: old}",
    "derives_from: !!omap [{up-0#s0: old}]",
    "derives_from: true",
)

NON_MAPPING_ENTRIES = ("- plainstring", "- 5", "- [1, 2]", "- null", "- ~")

NON_STRING_REFS = (
    ("- ref: [a, b]", "  seen: old0000"),
    ("- ref: 5", "  seen: old0000"),
    ("- ref: {a: b}", "  seen: old0000"),
    ("- ref:", "  seen: old0000"),
    ("- seen: old0000",),
)

COLLECTION_SEENS = ("[1, 2]", "{a: b}", "!!omap [{a: b}]")


# A leaked internal: a bare ``_name`` that is not reached through an attribute or a module path.
_INTERNAL_NAME = re.compile(r"(?<![\w.])_[A-Za-z]\w*")


def _assert_clean_refusal(error: UnreadableDocError, name: str = "") -> None:
    """Assert a refusal is a project sentence about this document, not a leaked internal.

    ``UnreadableDocError`` carries its code from its own constructor, so asserting the code says
    nothing that the exception type has not already said. What is worth pinning is the property
    the code stands for: that the refusal names the document and reads as prose a user can act
    on. A refusal with no message at all, or one carrying an internal type name, satisfies every
    type-level claim and fails a reader.

    A ``<`` cannot be banned outright: the loader's own tail, which the project prefix hands
    through, contains ``in "<unicode string>"``.
    """
    message = str(error)
    where = f" [{name}]" if name else ""
    assert message.strip(), f"a refusal carried no message at all{where}"
    assert "doc.md" in message, f"a refusal did not name the document{where}: {message!r}"
    assert not _INTERNAL_NAME.search(message), f"a refusal leaked an internal name: {message!r}"
    assert "<class " not in message, f"a refusal leaked a class repr: {message!r}"
    assert "object at 0x" not in message, f"a refusal leaked an object repr: {message!r}"


def _fenced(meta: str, envelope: Envelope) -> str:
    """Render an arbitrary frontmatter block inside a layer 2a envelope."""
    text = f"{envelope.open_fence}\n{meta}{envelope.close_fence}"
    if envelope.trailing_newline:
        text = f"{text}\n{envelope.body}"
    return _with_ending(BOM * envelope.boms + text, envelope.ending)


@FUZZ_SETTINGS
@given(data=st.data())
def test_any_reread_refuses_a_broken_block(data) -> None:
    name, meta, closed, message = data.draw(st.sampled_from(UNREADABLE_ON_ANY_REREAD))
    envelope = data.draw(envelopes(endings=("\n", "\r\n", "\r")))
    text = _fenced(meta, envelope) if closed else _with_ending(f"---\n{meta}body\n", "\n")

    with pytest.raises(UnreadableDocError, match=message) as error:
        _rewrite_bytes(text, {"up-0#s0": "new0000beef"})

    _assert_clean_refusal(error.value, name)


@FUZZ_SETTINGS
@given(data=st.data())
def test_a_mapping_root_refuses_a_derives_from_that_is_not_a_list(data) -> None:
    member = data.draw(st.sampled_from(NON_LIST_DERIVES_FROM))
    envelope = data.draw(envelopes())
    text = _fenced(f"id: doc\n{member}\n", envelope)

    with pytest.raises(UnreadableDocError, match="is not a list"):
        _rewrite_bytes(text, {"up-0#s0": "new0000beef"})


@FUZZ_SETTINGS
@given(data=st.data())
def test_reached_planning_refuses_a_malformed_entry(data) -> None:
    # The two pools refuse for different reasons and say so differently, so each carries the
    # message it has to give rather than sharing one pattern loose enough to cover both.
    not_a_mapping = r"frontmatter derives_from entry in 'doc\.md' is not a mapping"
    not_a_string = r"frontmatter derives_from entry ref in 'doc\.md' is not a string"
    bad, message = data.draw(
        st.one_of(
            st.sampled_from(NON_MAPPING_ENTRIES).map(lambda line: ((line,), not_a_mapping)),
            st.sampled_from(NON_STRING_REFS).map(lambda rows: (rows, not_a_string)),
        )
    )
    good = ("- ref: up-9#s9", "  seen: old0009")
    before = data.draw(st.booleans())
    entries = [*bad, *good] if before else [*good, *bad]
    # The refusal is scoped to planning being reached, not to the bad entry being targeted,
    # so the update deliberately varies between one that matches and one that matches nothing.
    updates = data.draw(st.sampled_from(({"up-9#s9": "new0000beef"}, {"gone#none": "new0000beef"})))
    text = _fenced(
        "id: doc\nderives_from:\n" + "".join(f"  {line}\n" for line in entries),
        data.draw(envelopes()),
    )

    with pytest.raises(UnreadableDocError, match=message) as error:
        _rewrite_bytes(text, updates)

    _assert_clean_refusal(error.value)


@FUZZ_SETTINGS
@given(data=st.data())
def test_a_targeted_entry_refuses_a_collection_seen(data) -> None:
    collection = data.draw(st.sampled_from(COLLECTION_SEENS))
    text = _fenced(
        f"id: doc\nderives_from:\n  - ref: up-0#s0\n    seen: {collection}\n",
        data.draw(envelopes()),
    )

    with pytest.raises(UnreadableDocError, match="entry seen is malformed"):
        _rewrite_bytes(text, {"up-0#s0": "new0000beef"})


@FUZZ_SETTINGS
@given(data=st.data())
def test_an_untargeted_collection_seen_neither_refuses_nor_is_rewritten(data) -> None:
    collection = data.draw(st.sampled_from(COLLECTION_SEENS))
    meta = (
        "id: doc\nderives_from:\n"
        "  - ref: up-0#s0\n    seen: old0000\n"
        f"  - ref: up-1#s1\n    seen: {collection}\n"
    )
    text = _fenced(meta, data.draw(envelopes()))

    rewrites = _rewrite_bytes(text, {"up-0#s0": "new0000beef"})

    assert len(rewrites) == 1
    after = rewrites[0].after.decode("utf-8")
    assert rewrites[0].applied == frozenset({"up-0#s0"})
    assert f"seen: {collection}" in _parts(after).raw_meta
    assert "seen: new0000beef" in _parts(after).raw_meta


@FUZZ_SETTINGS
@given(data=st.data())
def test_an_applied_update_refuses_self_referential_frontmatter(data) -> None:
    cycle = data.draw(st.sampled_from(("loop: &loop\n  self: *loop\n", "loop: &loop\n  - *loop\n")))
    text = _fenced(
        f"id: doc\n{cycle}derives_from:\n  - ref: up-0#s0\n    seen: old0000\n",
        data.draw(envelopes()),
    )

    with pytest.raises(UnreadableDocError, match="self-referential"):
        _rewrite_bytes(text, {"up-0#s0": "new0000beef"})


# --------------------------------------------------------------------------------------------
# Family 2b: documented no-ops, which are never refusals.
# --------------------------------------------------------------------------------------------

NO_OP_METAS = (
    ("null-root", "null\n"),
    ("tilde-root", "~\n"),
    ("empty-block", ""),
    ("absent-derives-from", "id: doc\ntitle: only prose\n"),
    ("null-derives-from", "id: doc\nderives_from:\n"),
    ("explicit-null-derives-from", "id: doc\nderives_from: null\n"),
    ("empty-derives-from", "id: doc\nderives_from: []\n"),
    ("no-matching-ref", "id: doc\nderives_from:\n  - ref: other#s0\n    seen: old0000\n"),
    ("already-holds-the-hash", "id: doc\nderives_from:\n  - ref: up-0#s0\n    seen: new0000beef\n"),
)

UNFENCED_DOCUMENTS = ("", "just prose\n", "# Heading\n\ntext\n", "not --- a fence\n")


@FUZZ_SETTINGS
@given(data=st.data())
def test_documented_no_ops_produce_no_rewrite(data) -> None:
    name, meta = data.draw(st.sampled_from(NO_OP_METAS))
    text = _fenced(meta, data.draw(envelopes(endings=("\n", "\r\n", "\r"))))

    assert _rewrite_bytes(text, {"up-0#s0": "new0000beef"}) == [], name
    assert apply_reconcile(normalize_newlines(text), {"up-0#s0": "new0000beef"}, DOC) == (
        normalize_newlines(text),
        set(),
    )


@FUZZ_SETTINGS
@given(text=st.sampled_from(UNFENCED_DOCUMENTS))
def test_a_document_with_no_opening_fence_produces_no_rewrite(text: str) -> None:
    assert _rewrite_bytes(text, {"up-0#s0": "new0000beef"}) == []
    assert apply_reconcile(text, {"up-0#s0": "new0000beef"}, DOC) == (text, set())


# --------------------------------------------------------------------------------------------
# Family 2c: non-contractual defensive recovery, accepted as a union of safe outcomes.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryShape:
    """One reread-only shape, and which arm of the safe-outcome union it takes today.

    ``arm`` is characterization, not contract. AD-31 layer 5 deliberately does not guarantee that
    recovery succeeds, so the union stays the pass or fail claim and this records only which arm
    each shape currently lands on. Without it the union is unfalsifiable in the direction that
    matters: a change that stopped recovering anything at all would take every shape to the no-op
    arm, which the union admits, and pass in silence.

    Changing a row is therefore a deliberate re-record to be argued for in the commit, exactly as
    family 3 treats the two ordered-map behaviors, and never a way to make a red suite green.

    Attributes:
        name: How the shape is named in a failure, since a ``Document`` carries no name and two
            shapes give their first entry the same one.
        document: The generated model.
        arm: The outcome this shape takes today, one of ``ARMS``.
        message: For a row recorded as a refusal, the message the refusal has to give.
    """

    name: str
    document: Document
    arm: str
    message: str | None = None


ARMS = ("rewrite", "refusal", "no-op")


def _recovery_shapes() -> tuple[RecoveryShape, ...]:
    """Build the reread-only shapes strict validation rejects, each with its own model.

    Every shape here is reread-only on every leg. A reused anchor name used to join this pool
    wherever the optional accelerator made the strict boundary refuse it; the strict boundary
    now pins the pure parser (AD-33), so that shape is strictly loadable everywhere and family
    1 owns it outright.

    Every shape here but one models the footprint of its update member by member rather than
    taking the whole-span allowance. Recovery is held to the footprint alone, since layer 3 is a
    claim about layer 2 documents, so the whole-span allowance is the only thing standing between
    a rewrite and the source beside the member it edits: the ``ref`` it is found by, an extra
    member the contract says survives untouched, and the member an appended ``seen`` is written
    after. What the union leaves open is whether the rewrite happens at all, not how far it
    reaches once it does.

    The exception is the aliased entry, which keeps the whole-span allowance a family 1 alias
    site keeps, because layer 4 lets a rewrite expand an alias site into a local mapping rather
    than edit the shared node behind it. There is no narrower member-level claim to make about a
    site the contract says may be replaced outright.
    """
    return (
        RecoveryShape("extra-root-key", _recovery_extra_root_key(), "rewrite"),
        RecoveryShape("extra-entry-key", _recovery_extra_entry_key(), "rewrite"),
        RecoveryShape(
            "non-string-seen-tagged-int", _recovery_non_string_seen("!!int 5", 5), "rewrite"
        ),
        RecoveryShape("non-string-seen-int", _recovery_non_string_seen("5", 5), "rewrite"),
        RecoveryShape("non-string-seen-bool", _recovery_non_string_seen("true", True), "rewrite"),
        RecoveryShape("non-string-seen-float", _recovery_non_string_seen("1.5", 1.5), "rewrite"),
        RecoveryShape(
            "non-string-seen-date",
            _recovery_non_string_seen("2026-01-01", date(2026, 1, 1)),
            "rewrite",
        ),
        RecoveryShape(
            "relocated-non-string-seen", _recovery_relocated_non_string_seen(), "rewrite"
        ),
        RecoveryShape("relocated-bool-seen", _recovery_relocated_bool_seen(), "rewrite"),
        RecoveryShape(
            "relocated-multi-line-tagged-seen",
            _recovery_relocated_multi_line_tagged_seen(),
            "rewrite",
        ),
        RecoveryShape("aliased-derives-from", _recovery_aliased_derives_from(), "rewrite"),
        RecoveryShape("aliased-entry", _recovery_aliased_entry(), "rewrite"),
        RecoveryShape(
            "trailing-block-scalar-member", _recovery_trailing_block_scalar_member(), "rewrite"
        ),
        RecoveryShape("multi-line-flow-member", _recovery_multi_line_flow_member(), "rewrite"),
        RecoveryShape("null-member-empty", _recovery_null_member("note:", None), "rewrite"),
        RecoveryShape(
            "null-member-explicit-null", _recovery_null_member("note: null", None), "rewrite"
        ),
    )


def _recovery_extra_root_key() -> Document:
    """A root carrying a key ``NodeMeta`` does not declare.

    ``NodeMeta`` is built with ``extra="forbid"``, so any key beyond its own fails strict
    validation. The reread reads only ``derives_from`` and leaves the rest alone, which is what
    makes the extra key a recovery shape rather than a refusal.
    """
    entry = _entry(0, ("- ref: {ref}", "  seen: {old}"), "old0000", None)
    lines = ("id: doc", "extra: kept", "derives_from:", *_indent(entry.lines, 2))
    return Document(
        lines,
        (entry,),
        ((3, len(lines)),),
        {"id": "doc", "extra": "kept"},
        ("id", "extra", "derives_from"),
        _flat_envelope(),
        (),
    )


def _recovery_extra_entry_key() -> Document:
    """An entry carrying a member beyond ``ref`` and ``seen``.

    ``RawEdge`` forbids extras too, so the entry fails strict validation while the reread
    tolerates the member and has to write past it without disturbing it.
    """
    entry = Entry(
        "entry-extra-key",
        ("- ref: up-0#s0", "  seen: old0000", "  extra: kept"),
        None,
        "up-0#s0",
        "old0000",
        True,
        None,
        (),
        (("extra", "kept"),),
        edits=(1,),
    )
    return _single_entry_document(entry)


def _recovery_non_string_seen(written: str, value: object) -> Document:
    """A ``seen`` whose constructed value is not a string.

    ``RawEdge.seen`` is ``str | None`` under ``strict=True``, so an int, a bool, a float, a date
    or an explicitly tagged scalar is refused by the strict load however it is spelled. The
    reread accepts whatever the safe constructor builds, which is the concurrent-edit case.
    """
    entry = Entry(
        f"non-string-seen-{written}",
        ("- ref: up-0#s0", f"  seen: {written}"),
        None,
        "up-0#s0",
        value,
        True,
        None,
        (),
        edits=(1,),
    )
    return _single_entry_document(entry)


def _recovery_relocated_non_string_seen() -> Document:
    """A non-string ``seen`` under an anchor, with an alias site its old value relocates onto.

    Reread-only for the same ``RawEdge.seen`` reason as the plain non-string shapes, and it
    additionally reaches the tag lifecycle: the displaced value has to be re-emitted at the
    alias site under its own type rather than through ``str``.
    """
    first = Entry(
        "anchored-int-seen",
        ("- ref: up-0#s0", "  seen: &shared !!int 5"),
        None,
        "up-0#s0",
        5,
        True,
        "shared",
        (),
        edits=(1,),
    )
    second = Entry(
        "alias-int-seen",
        ("- ref: up-1#s1", "  seen: *shared"),
        None,
        "up-1#s1",
        5,
        True,
        None,
        (),
        edits=(1,),
    )
    lines = ("id: doc", "derives_from:", *first.lines, *second.lines)
    return Document(
        lines,
        (first, second),
        ((2, 4), (4, 6)),
        {"id": "doc"},
        ("id", "derives_from"),
        _flat_envelope(),
        (),
    )


def _recovery_relocated_bool_seen() -> Document:
    """An anchored bool ``seen``, relocated in its own spelling rather than under its tag.

    Reread-only because ``RawEdge.seen`` is ``str | None``. Layer 4 gives a bool and a null the
    arm before the tag lifecycle: they carry their type in their own spelling, so re-emitting a
    tag would only add a token the reload does not need. The semantic oracle cannot see the
    difference, since ``&shared true`` and a quoted or tagged spelling of it reload alike.
    """
    return _relocating_anchor_pair("true", True, displaced="&shared true")


def _recovery_relocated_multi_line_tagged_seen() -> Document:
    """An anchored multi-line tagged scalar, re-emitted quoted under the same tag.

    Reread-only for the same ``RawEdge.seen`` reason. This is the far arm of the tag lifecycle:
    a multi-line scalar cannot be relocated as written into the one line the alias site holds,
    so layer 4 re-emits it quoted and keeps the tag, which is what stops the value being retyped
    to a string. Reload-equal to the unquoted spelling, so only ``displaced`` sees it.
    """
    anchored = ("- ref: up-0#s0", "  seen: &shared !!int |", "    42")
    aliased = ("- ref: up-1#s1", "  seen: *shared")
    first = Entry(
        "anchored-multi-line-tagged-seen",
        anchored,
        None,
        "up-0#s0",
        42,
        True,
        "shared",
        (),
        (),
        marker=_style_marker(anchored[0]),
        # Both lines of the scalar, since the replacement consumes the whole member it stands
        # for, not just the line its header is written on.
        edits=(1, 2),
        site="up-0#s0",
        written_lines=1,
        displaced='&shared !!int "42\\n"',
    )
    second = Entry(
        "alias-multi-line-tagged-seen",
        aliased,
        None,
        "up-1#s1",
        42,
        True,
        None,
        (),
        (),
        marker=_style_marker(aliased[0]),
        edits=(1,),
        site="up-1#s1",
        written_lines=1,
    )
    lines = ("id: doc", "derives_from:", *first.lines, *second.lines)
    return Document(
        lines,
        (first, second),
        ((2, 5), (5, 7)),
        {"id": "doc"},
        ("id", "derives_from"),
        _flat_envelope(),
        (),
    )


def _recovery_aliased_derives_from() -> Document:
    """A ``derives_from`` reached through an alias to a sequence defined elsewhere.

    Not strictly reachable at all: the anchor has to be defined under some carrier key, and
    ``NodeMeta`` forbids the extra key that would hold it. AD-31 records this as reread-only for
    exactly that reason. The carrier key is a mirror, so it follows every update.
    """
    entry = _entry(0, ("- ref: {ref}", "  seen: {old}"), "old0000", None)
    lines = ("base: &edges", *_indent(entry.lines, 2), "id: doc", "derives_from: *edges")
    return Document(
        lines,
        (entry,),
        ((1, 3),),
        {"id": "doc"},
        None,
        _flat_envelope(),
        (),
        ("base",),
    )


def _recovery_aliased_entry() -> Document:
    """An entry spelled as an alias to a mapping defined under another root key.

    The entry itself is a layer 2 spelling, but the node it names has to live somewhere, and the
    root key holding it is one ``NodeMeta`` forbids. This is the shape that keeps the whole-span
    allowance, since layer 4 lets the rewrite expand the alias site rather than edit the shared
    node behind it.
    """
    entry = Entry(
        "alias-to-a-node-elsewhere",
        ("- *edge",),
        None,
        "up-0#s0",
        "old0000",
        True,
        None,
        (),
    )
    lines = ("id: doc", "shared: &edge {ref: up-0#s0, seen: old0000}", "derives_from:", "  - *edge")
    return Document(
        lines,
        (entry,),
        ((3, 4),),
        {"id": "doc", "shared": {"ref": "up-0#s0", "seen": "old0000"}},
        ("id", "shared", "derives_from"),
        _flat_envelope(),
        (),
    )


def _recovery_trailing_block_scalar_member() -> Document:
    """An entry whose extra member is a block scalar running to the end of the entry.

    Reread-only twice over: the member is an extra ``RawEdge`` forbids, and it is the landing
    layout an appended ``seen`` has to follow, which the strict column excludes.
    """
    entry = Entry(
        "trailing-block-scalar-member",
        ("- ref: up-0#s0", "  note: |-", "    two", "    lines"),
        None,
        "up-0#s0",
        None,
        False,
        None,
        (),
        (("note", "two\nlines"),),
        edits=(),
        appends=True,
    )
    return _single_entry_document(entry)


def _recovery_multi_line_flow_member() -> Document:
    """An entry whose extra member is a flow collection written across several lines.

    The same extra-member refusal as the trailing block scalar, with the landing point for an
    appended ``seen`` spread over the lines the collection closes on.
    """
    entry = Entry(
        "multi-line-flow-member",
        ("- ref: up-0#s0", "  tags: [", "    one,", "    two", "  ]"),
        None,
        "up-0#s0",
        None,
        False,
        None,
        (),
        (("tags", ["one", "two"]),),
        edits=(),
        appends=True,
    )
    return _single_entry_document(entry)


def _recovery_null_member(written: str, value: object) -> Document:
    """An entry whose extra member is an implicit or explicit null.

    An extra ``RawEdge`` forbids, and the one whose written form gives an appended ``seen`` the
    least to land after, since the member occupies a key and no value.
    """
    entry = Entry(
        f"null-member-{written}",
        ("- ref: up-0#s0", f"  {written}"),
        None,
        "up-0#s0",
        None,
        False,
        None,
        (),
        (("note", value),),
        edits=(),
        appends=True,
    )
    return _single_entry_document(entry)


def _assert_not_strictly_tracked(text: str) -> None:
    """Assert the strict path rejects this shape, so it really is the reread-only column."""
    parts = split_frontmatter_parts(normalize_newlines(text), DOC)
    assert parts is not None
    try:
        disposition = parse_meta(parts.raw_meta, DOC).disposition
    except (FrontmatterError, UnreadableDocError):
        return
    assert disposition != "tracked", "a recovery shape must not pass strict validation"


def _assert_safe_recovery(document: Document, updates: dict[str, str]) -> str:
    """Assert a reread-only shape lands on one of the three outcomes the union admits.

    Requiring success would promote non-contractual recovery into a commitment, so this only
    rejects a crash, a wrong value, and an edit outside the layer 4 footprint.

    Returns:
        Which arm of the union the shape took, so the caller can pin it. Two of the three admit
        anything by design, and an assertion that admits anything cannot report a regression, so
        the arm has to leave this function for the claim to be worth making.
    """
    text = document.render()
    try:
        rewrites = _rewrite_bytes(text, updates)
    except UnreadableDocError as error:
        # A clean project-level refusal is one of the three outcomes the union admits; any
        # other exception type escapes here and fails the caller as the crash it is. Clean is
        # asserted rather than assumed, since an empty or internal-leaking message would
        # otherwise satisfy this arm.
        _assert_clean_refusal(error)
        return "refusal"

    if not rewrites:
        return "no-op"
    after = rewrites[0].after.decode("utf-8")
    assert rewrites[0].applied == document.applied(updates)
    assert _reload(after) == document.expected(updates)
    # Layer 3 is a claim about layer 2 documents, so recovery is held only to the layer 4
    # footprint and to meaning the same thing afterwards. Consuming the replaced value's node
    # properties is part of that footprint rather than of layer 3, and this column is where a
    # tagged non-string ``seen`` is generated, so the bare-hash claim is made here too.
    allowed, inserts = document.footprint(updates)
    _assert_footprint_confined(_meta_lines(text), _meta_lines(after), allowed, inserts)
    _assert_replacements_stay_bare(after, updates)
    return "rewrite"


@EXPECT_REUSED_ANCHOR
@FUZZ_SETTINGS
@given(data=st.data())
def test_defensive_recovery_stays_inside_the_safe_outcome_union(data) -> None:
    """A reread-only shape may no-op, refuse cleanly, or rewrite correctly, and nothing else.

    The union is the contract. The recorded arm alongside it is what makes the union observable:
    losing defensive recovery altogether takes every shape to the no-op arm, which the union
    admits, so without the record the strongest thing this file asserts could stop being
    asserted with nothing to show for it.
    """
    shape = data.draw(st.sampled_from(_recovery_shapes()))
    updates = {shape.document.entries[0].ref: "new0000beef"}
    _assert_not_strictly_tracked(shape.document.render())

    observed = _assert_safe_recovery(shape.document, updates)

    assert observed == shape.arm, (
        f"{shape.name} now recovers by {observed!r} where this table records {shape.arm!r}. "
        "The union above is the contract and it still holds, so this is not a rewriter defect "
        "on its own; re-record the row deliberately and say why, rather than widening it."
    )
    _CLAIMS["recovery-arm"] += 1


def test_the_recovery_table_records_a_known_arm_for_every_shape() -> None:
    """Keep the arm a real claim, with no wildcard and no shape left unrecorded."""
    shapes = _recovery_shapes()
    assert shapes, "the recovery pool must not be empty"
    unknown = sorted({shape.arm for shape in shapes} - set(ARMS))
    assert not unknown, f"these are not arms of the union: {unknown}"
    names = [shape.name for shape in shapes]
    assert len(names) == len(set(names)), f"two recovery shapes share a name: {names}"


# --------------------------------------------------------------------------------------------
# Family 3: current bounded ordered-map behavior, pinned rather than guaranteed.
# --------------------------------------------------------------------------------------------

MALFORMED_ORDERED_MAPS = (
    pytest.param(
        "id: doc\nderives_from:\n  - !!omap\n    - ref: up-0#s0\n      seen: old0000\n",
        id="entry-item-holds-two-pairs",
    ),
    pytest.param(
        "id: doc\nderives_from:\n  - !!omap\n    - ref: up-0#s0\n    - plain\n",
        id="entry-item-is-a-scalar",
    ),
    pytest.param(
        "!!omap\n- id: doc\n  extra: kept\n- derives_from:\n    - ref: up-0#s0\n",
        id="root-item-holds-two-pairs",
    ),
    pytest.param("!!omap\n- id: doc\n- plain\n", id="root-item-is-a-scalar"),
)

MERGES_INSIDE_ORDERED_MAPS = (
    pytest.param(
        "id: doc\nbase: &b\n  seen: old0000\nderives_from:\n"
        "  - !!omap\n    - ref: up-0#s0\n    - <<: *b\n",
        id="merge-in-an-entry",
    ),
    pytest.param(
        "!!omap\n- id: doc\n- <<: {derives_from: [{ref: up-0#s0, seen: old0000}]}\n",
        id="merge-in-the-root",
    ),
    pytest.param(
        "id: doc\nderives_from:\n  - !!omap\n    - ref: up-0#s0\n"
        "    - !!merge x: {seen: old0000}\n",
        id="tagged-merge-in-an-entry",
    ),
)


@pytest.mark.parametrize("meta", MALFORMED_ORDERED_MAPS)
@pytest.mark.parametrize("updates", [{"up-0#s0": "new0000beef"}, {"gone#none": "new0000beef"}])
def test_a_malformed_ordered_map_is_reported_as_an_unreadable_document(
    meta: str, updates: dict[str, str]
) -> None:
    """Pin current bounded loader behavior: the safe constructor's refusal surfaces as ours.

    AD-31 records this as current behavior rather than a guaranteed refusal, so the pin is the
    stable observable: a ``ProjectError`` subtype carrying its code and naming the document,
    which is what the CLI's handler renders an exit from. The loader's own message is not
    pinned, since it differs across the ``ruamel.yaml`` releases and accelerator cells CI runs.
    """
    text = f"---\n{meta}---\nbody\n"

    with pytest.raises(
        UnreadableDocError, match=r"cannot parse frontmatter of 'doc\.md' to reconcile: "
    ) as error:
        apply_reconcile(text, updates, DOC)

    assert isinstance(error.value, ProjectError)
    _assert_clean_refusal(error.value)


@pytest.mark.parametrize("meta", MERGES_INSIDE_ORDERED_MAPS)
def test_a_merge_inside_an_ordered_map_is_reported_as_an_unreadable_document(meta: str) -> None:
    """Pin current bounded loader behavior: an ordered map never flattens a merge key.

    The loader builds an ordered map from its items rather than through mapping construction,
    so the merge tag reaches the constructor with nothing to build it and the document is
    refused at load rather than reconciled. Pinned at the type and code for the same
    cross-parser reason as the malformed case above.
    """
    text = f"---\n{meta}---\nbody\n"

    with pytest.raises(
        UnreadableDocError, match=r"cannot parse frontmatter of 'doc\.md' to reconcile: "
    ) as error:
        apply_reconcile(text, {"up-0#s0": "new0000beef"}, DOC)

    _assert_clean_refusal(error.value)


# --------------------------------------------------------------------------------------------
# Deterministic regression cases for the shapes the properties must never stop covering.
# --------------------------------------------------------------------------------------------

PLAIN_META = "id: doc\nderives_from:\n  - ref: up-0#s0\n    seen: old0000\n"
PLAIN_REWRITTEN = "id: doc\nderives_from:\n  - ref: up-0#s0\n    seen: new0000beef\n"


@pytest.mark.parametrize(
    "ending",
    [pytest.param("\n", id="uniform-lf"), pytest.param("\r\n", id="uniform-crlf")],
)
def test_a_uniform_line_ending_survives_the_rewrite(ending: EnvelopeEnding) -> None:
    before = _with_ending(f"---\n{PLAIN_META}---\nbody\n", ending)

    rewrites = _rewrite_bytes(before, {"up-0#s0": "new0000beef"})

    assert len(rewrites) == 1
    expected = _with_ending(f"---\n{PLAIN_REWRITTEN}---\nbody\n", ending)
    assert rewrites[0].after.decode("utf-8") == expected


def test_a_uniform_lone_carriage_return_document_survives_the_rewrite() -> None:
    before = _with_ending(f"---\n{PLAIN_META}---\nbody\n", "\r")

    rewrites = _rewrite_bytes(before, {"up-0#s0": "new0000beef"})

    assert len(rewrites) == 1
    after = rewrites[0].after.decode("utf-8")
    assert "\n" not in after
    assert after == _with_ending(f"---\n{PLAIN_REWRITTEN}---\nbody\n", "\r")


def test_a_document_that_mixes_line_endings_is_normalized_to_lf() -> None:
    before = f"---\r\n{PLAIN_META}---\nbody\n"

    rewrites = _rewrite_bytes(before, {"up-0#s0": "new0000beef"})

    assert len(rewrites) == 1
    after = rewrites[0].after.decode("utf-8")
    assert "\r" not in after
    assert after == f"---\n{PLAIN_REWRITTEN}---\nbody\n"


@EXPECT_REUSED_ANCHOR
def test_a_reused_anchor_keeps_an_alias_bound_to_its_later_definition() -> None:
    """A later definition rebinds the name, so relocating the first value must pass the alias by.

    Both reads AD-31 layer 2 splits its columns by run on the pure parser, which warns about a
    reused anchor name and rebinds it, so this document is a layer 2 one on every leg. The
    rewrite is therefore mandatory and both rebinding invariants are asserted unconditionally.
    This used to branch on which parser the strict boundary happened to be running, with the
    reread-only arm admitting a clean refusal or a no-op as readily as a rewrite; pinning the
    strict boundary is what removed that arm.
    """
    document = _reused_anchor_pair()
    updates = {document.entries[0].ref: "new0000beef"}

    _assert_supported_round_trip(document, updates)
    _assert_anchor_rebinding_survived(_rewrite_bytes(document.render(), updates))


def test_the_pure_reread_warns_about_a_reused_anchor_name() -> None:
    """Assert the warning AD-31 declares this spelling carries is really raised.

    Reused anchors are supported rather than refused, and on this path the warning ruamel
    raises about one reaches the caller unchanged, on both ``yaml-compatibility`` legs now that
    both reads run on the pure parser. The strict boundary is the one that differs: it captures
    the warning and re-reports the fact against the discovered path so a warm cache replays it
    (AD-29, AD-33), which a reread with no file to name and no cache to consult cannot do. The
    tests above silence the warning to keep it out of their summaries, so pinning it here is
    what keeps that silencing honest.
    """
    document = _reused_anchor_pair()

    with pytest.warns(ReusedAnchorWarning):
        _rewrite_bytes(document.render(), {document.entries[0].ref: "new0000beef"})


def _assert_anchor_rebinding_survived(rewrites: list[Rewrite]) -> None:
    """Assert a rewrite left the alias reading the later definition of a reused anchor name."""
    if not rewrites:
        return
    after = rewrites[0].after.decode("utf-8")
    # The relocation must not land on the alias site, which reads the second definition.
    assert "seen: &shared old0001" in after
    assert "seen: *shared" in after


@pytest.mark.parametrize(
    ("written", "value"),
    [pytest.param(written, value, id=name) for written, value, name in RELOCATED_CONTENTS],
)
def test_an_anchored_seen_relocates_as_escapes_and_reparses(written: str, value: str) -> None:
    """A displaced anchored value is re-emitted escape-only, so the alias site still reparses."""
    document = _relocating_anchor_pair(written, value)

    _assert_supported_round_trip(document, {"up-0#s0": "new0000beef"})

    after = _rewrite_bytes(document.render(), {"up-0#s0": "new0000beef"})[0].after.decode("utf-8")
    assert f"seen: &shared {written}" in after
    assert not any(0x7F <= ord(char) <= 0x9F for char in after)


def _omap_item_alias_document() -> Document:
    """Two ordered-map entries, the second spelling its ``seen`` through the first's anchor."""
    first = Entry(
        "omap-anchor-definition",
        ("- !!omap", "  - ref: up-0#s0", "  - &shared_pair {seen: old0000}"),
        None,
        "up-0#s0",
        "old0000",
        True,
        None,
        (),
        (),
        marker="- !!omap",
        edits=(2,),
        site="up-0#s0",
        written_lines=1,
    )
    second = Entry(
        "omap-item-alias",
        ("- !!omap", "  - ref: up-1#s1", "  - *shared_pair"),
        None,
        "up-1#s1",
        "old0000",
        True,
        None,
        (),
        (),
        marker="- !!omap",
        edits=(2,),
        site="up-1#s1",
        written_lines=1,
    )
    lines = ("id: doc", "derives_from:", *_indent(first.lines, 2), *_indent(second.lines, 2))
    return Document(
        lines,
        (first, second),
        ((2, 5), (5, 8)),
        {"id": "doc"},
        ("id", "derives_from"),
        _flat_envelope(),
        (),
    )


def test_an_ordered_map_item_alias_is_expanded_into_a_local_pair() -> None:
    """Layer 4's other alias detachment: a one-pair item written where the alias stood.

    An ordered map item holds exactly one pair, and a merge cannot stand in for one the way it
    can for a whole aliased entry, so the rewrite writes the pair out at the item and leaves the
    shared definition alone.

    Deterministic rather than drawn, and the reason is a real limit of the pool. Targeting the
    *definition* entry of this shape is a layer 5 reproduce refusal, since writing into the
    shared node would change the aliasing entry too, and the property above draws its target
    freely. Expressing that would need a way to say which of a shape's entries may be targeted,
    used by this shape alone. The refusal is pinned below instead.
    """
    document = _omap_item_alias_document()
    updates = {"up-1#s1": "new0001beef"}

    _assert_supported_round_trip(document, updates)

    after = _rewrite_bytes(document.render(), updates)[0].after.decode("utf-8")
    assert "  - {seen: new0001beef}" in after
    assert "  - &shared_pair {seen: old0000}" in after


def test_targeting_the_definition_an_ordered_map_item_aliases_is_refused() -> None:
    """The layer 5 reproduce refusal that keeps the shape above out of the drawn pool.

    Writing at the definition would change the entry aliasing it as well, so the planned
    frontmatter is not what the edits reproduce and the rewrite is refused rather than published.
    """
    document = _omap_item_alias_document()

    with pytest.raises(
        UnreadableDocError, match=r"would not reproduce the derives_from entries"
    ) as error:
        _rewrite_bytes(document.render(), {"up-0#s0": "new0000beef"})

    _assert_clean_refusal(error.value)


def test_a_byte_order_mark_run_is_reattached_verbatim() -> None:
    for count in (1, 2, 3):
        before = f"{BOM * count}---\n{PLAIN_META}---\nbody\n"

        rewrites = _rewrite_bytes(before, {"up-0#s0": "new0000beef"})

        assert len(rewrites) == 1
        expected = f"{BOM * count}---\n{PLAIN_REWRITTEN}---\nbody\n"
        assert rewrites[0].after.decode("utf-8") == expected


@pytest.mark.parametrize("version", ["1.1", "1.2"])
def test_the_yaml_directive_envelope_survives_the_rewrite(version: str) -> None:
    before = f"---\n%YAML {version}\n--- !!map\n{PLAIN_META}---\nbody\n"

    rewrites = _rewrite_bytes(before, {"up-0#s0": "new0000beef"})

    assert len(rewrites) == 1
    after = rewrites[0].after.decode("utf-8")
    assert after == f"---\n%YAML {version}\n--- !!map\n{PLAIN_REWRITTEN}---\nbody\n"


# --------------------------------------------------------------------------------------------
# The corpus floor. Last in the file, so it reads what everything above it actually asserted.
# --------------------------------------------------------------------------------------------


def test_every_claim_the_assertions_make_fires_somewhere_in_this_corpus() -> None:
    """Fail when a conditional claim is made nowhere, which no other test here can see.

    Most layer 3 claims are guarded by the shape they apply to, so each one is skipped far more
    often than it fires and every skip is correct on its own. What no single test can notice is
    all of them skipping: a generator that stopped producing flow entries, or a model field that
    stopped being set, would take a claim to zero with the suite still green.

    This depends on session state, which is the honest cost of measuring the corpus rather than
    a shape. Running a subset with ``-k`` can leave a claim at zero for a reason that is not a
    defect, so it skips when nothing above it ran at all, and the table floor further up is the
    guard that holds under any selection.
    """
    if not _CLAIMS:
        pytest.skip("this floor measures the properties above; none of them were selected")
    missing = sorted(name for name in _REQUIRED_CLAIMS if not _CLAIMS[name])
    assert not missing, (
        f"{missing} is asserted nowhere in the generated corpus, so every shape that could "
        "carry it skipped it and a rewriter defect in it would pass unseen"
    )
