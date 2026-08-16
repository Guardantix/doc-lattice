"""Round-trip fuzz gate for the reconcile frontmatter rewriter's declared subset (AD-31).

The properties here generate documents from the AD-31 layer 2 matrix by writable position and
by load phase, rather than from a flat bag of YAML features, because support is conditional on
both. Family 1 generates only the strict tracked-document column plus the layer 2a envelope and
demands a correct rewrite. Family 2 generates the layer 5 outcomes at the exact scope each one
is guaranteed at, and accepts a safe outcome union for the defensive reread column. Family 3
pins the two ordered-map behaviors AD-31 assigns to this gate as current bounded behavior.

Every expectation is computed from the generated model, never from production planning code, so
a rewriter defect cannot make its own oracle agree with it. No property asserts anything
stricter than AD-31: the mutation footprint is checked against the allowances the model predicts
for the shape it generated, never against a universal one-line diff, and syntax outside layer 2
is never required to be refused.
"""

import re
import warnings
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from doc_lattice.error_types import ConfigError, ProjectError, UnreadableDocError
from doc_lattice.frontmatter_parser import parse_meta, split_frontmatter_parts
from doc_lattice.hashing import normalize_newlines
from doc_lattice.reconcile import Rewrite, apply_reconcile, plan_rewrites
from doc_lattice.yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader

DOC = Path("doc.md")
BOM = chr(0xFEFF)
# The spacing every flow carrier in this file writes between two entries. It is named so that
# every generator writes the same one, which is what lets the flow-line assertion recover the
# carrier's own source by cutting the entries it knows out of the line it rendered.
FLOW_SEPARATOR = ", "
# The characters that punctuate a flow collection. A rewrite that adds or drops one of these
# outside the value it was asked to write has restyled source layer 3 says it may not touch,
# even where the document still loads as the very same mapping.
FLOW_INDICATORS = ",{}[]"
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


def _strict_load_accepts_reused_anchors() -> bool:
    """Report whether the strict load accepts a document defining one anchor name twice.

    The two reads AD-31 layer 2 splits its columns by do not run on the same parser. The
    reread inside ``apply_reconcile`` pins the pure Python one, which AD-26 makes part of the
    compatibility surface, and it warns about a reused anchor name and rebinds it. The strict
    load goes through ``SafeYamlLoader``, which is deliberately not pinned and switches to the
    optional ``ruamel.yaml.clib`` accelerator whenever anything installs it; that composer
    rejects a duplicate anchor definition outright. So a reused-anchor document sits in the
    strict column only where the accelerator is absent, and is reread-only where it is not.
    An alias reading a name defined once is accepted by both and needs no probe.

    Returns:
        True when a duplicate anchor definition loads through the strict boundary.
    """
    with warnings.catch_warnings():
        # The pure parser warns on the duplicate it accepts, which is the answer, not a fault.
        warnings.simplefilter("ignore")
        try:
            SafeYamlLoader().load("first: &name 1\nsecond: &name 2\n")
        except YAML_LOAD_ERRORS:
            return False
    return True


REUSED_ANCHORS_ARE_STRICT = _strict_load_accepts_reused_anchors()


# --------------------------------------------------------------------------------------------
# The typed syntax model: spellings by writable position, and the documents built from them.
# --------------------------------------------------------------------------------------------


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
    """

    name: str
    templates: tuple[str, ...]
    key_indent: int
    split: bool
    trailing: str
    note: bool


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
    """

    name: str
    templates: tuple[str, ...]
    value: str
    present: bool
    anchored: bool
    note: bool


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
    """

    name: str
    templates: tuple[str, ...]
    inline: str | None
    value: str
    present: bool
    anchored: bool
    writes: str = "none"


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
        site: Text that occurs in this entry's source and nowhere else in the document, which
            is what locates the entry in the rewritten block so a claim about its own source is
            read inside it rather than anywhere in the frontmatter: where its comments came
            back, and what the member an edit landed on still opens with. It is None for an
            entry no such claim is made about.
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
    marker: str | None = None
    edits: tuple[int, ...] | None = None
    appends: bool = False
    pin: FlowPin | None = None
    site: str | None = None


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
    ending: str
    directive: str | None


@dataclass(frozen=True, slots=True)
class Document:
    """A generated document, its independent semantics, and the footprint a rewrite may take.

    Attributes:
        meta_lines: The frontmatter block between the fences, one string per line.
        entries: The generated entries, in ``derives_from`` order.
        spans: Half-open ``meta_lines`` ranges each entry occupies.
        root: The root members other than ``derives_from``, as the loader builds them.
        key_order: The root key order the reload has to show, or None when a merge makes the
            order a loader detail rather than a preservation claim.
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
        """Return the mapping the rewritten frontmatter has to load as."""
        entries: list[dict[str, object]] = []
        for entry in self.entries:
            seen, present = entry.seen, entry.present
            planned = updates.get(entry.ref)
            if planned is not None and planned != seen:
                seen, present = planned, True
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
    # YAML lets the two header indicators be written in either order, so all four headers are
    # spelled out rather than left to stand for one another. A clipped one joins them because
    # the indicator has to compose with a retained line break as well as a stripped one.
    SeenForm("literal-indent-indicator", ("seen: |2-", "  {old}"), "old", True, False, False),
    SeenForm("folded-indent-indicator", ("seen: >2-", "  {old}"), "old", True, False, False),
    SeenForm("literal-indent-after-chomp", ("seen: |-2", "  {old}"), "old", True, False, False),
    SeenForm("folded-indent-after-chomp", ("seen: >-2", "  {old}"), "old", True, False, False),
    SeenForm(
        "literal-indent-indicator-clipped",
        ("seen: |2", "  {old}"),
        "old-newline",
        True,
        False,
        False,
    ),
    SeenForm(
        "literal-header-comment", ("seen: |- # {seen_note}", "  {old}"), "old", True, False, True
    ),
    SeenForm("explicit-pair", ("? seen", ": {old}"), "old", True, False, False),
    SeenForm("explicit-key-no-value", ("? seen",), "null", True, False, False),
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
    ),
    SeenForm("tag-on-its-own-line", ("seen:", "  !!str {old}"), "old", True, False, False),
    SeenForm(
        "both-properties-on-their-own-lines",
        ("seen: &{anchor} # {seen_note}", "  !!str", "  {old}"),
        "old",
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


def _seen_value(kind: str, old: str) -> str | None:
    """Return the value a ``seen`` spelling of ``kind`` loads as."""
    if kind == "null":
        return None
    if kind == "old-newline":
        return f"{old}\n"
    return f"{old}\n\n" if kind == "old-blank-line" else old


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
    appends = not seen_form.present or seen_form.name == "explicit-key-no-value"
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
        _style_marker(lines[0]),
        edits,
        appends,
        site=fields["head"],
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


def _flow_pin(inline: str, writes: str) -> FlowPin:
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
    opening = r"(\S.*?)" if head.endswith(" ") else "(.*?)"
    return FlowPin(
        f"{re.escape(head)}{opening}{re.escape(closing)}", written + FLOW_EDIT_INDICATORS[writes]
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
        edits = tuple(offset for offset, line in enumerate(lines) if "seen" in line)
        appends = not form.present or "explicit-key" in form.name
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
        _style_marker(inline if inline is not None else lines[0]),
        edits,
        appends,
        None if inline is None else _flow_pin(inline, form.writes),
        fields["head"],
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


def _with_ending(text: str, ending: str) -> str:
    """Return ``text`` rewritten in the requested document line ending."""
    if ending == "mixed":
        return text.replace("\n", "\r\n", 1)
    return text if ending == "\n" else text.replace("\n", ending)


def _document_ending(text: str) -> str:
    """Return the ending a document is written in, mirroring the rewriter's own rule."""
    if "\r\n" in text and not set(text.replace("\r\n", "")) & {"\r", "\n"}:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "mixed" if "\r" in text else "\n"


def _parts(text: str):
    """Split a document into its frontmatter pieces after normalizing its line endings."""
    parts = split_frontmatter_parts(normalize_newlines(text), DOC)
    assert parts is not None
    return parts


def _meta_lines(text: str) -> list[str]:
    """Return the frontmatter block of a document as a list of lines."""
    raw_meta = _parts(text).raw_meta
    return raw_meta.split("\n")[:-1] if raw_meta else []


def _reload(text: str) -> object:
    """Reload a document's frontmatter through the project's safe loader."""
    return SafeYamlLoader().load(_parts(text).raw_meta)


def _rewrite_bytes(text: str, updates: dict[str, str]):
    """Drive the production planner over one in-memory document."""
    before = text.encode("utf-8")
    return plan_rewrites({DOC: updates}, lambda _path: before)


# --------------------------------------------------------------------------------------------
# Assertions shared by the properties.
# --------------------------------------------------------------------------------------------


def _assert_strict_tracked(text: str) -> None:
    """Assert the real strict load classifies this document as a tracked node.

    A failure here is a generator bug rather than a rewriter one: family 1 declares that it
    generates only the strict tracked-document column of layer 2, and this is the machine
    check of that claim.
    """
    parts = split_frontmatter_parts(normalize_newlines(text), DOC)
    assert parts is not None, "family 1 document has no frontmatter block"
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

    Returns:
        One slice per entry, in order, and None for an entry with no site to locate it by.
    """
    sites = [-1 if entry.site is None else raw_meta.find(entry.site) for entry in document.entries]
    regions: list[str | None] = []
    for position, start in enumerate(sites):
        later = (found for found in sites[position + 1 :] if found > start)
        regions.append(None if start == -1 else raw_meta[start : min(later, default=len(raw_meta))])
    return regions


def _assert_comments_kept_in_place(document: Document, raw_meta: str) -> None:
    """Assert the block's comments come back, each entry's own inside the entry that wrote it.

    Surviving somewhere in the block is too weak a claim once two entries carry comments and
    both are rewritten. Their texts differ, so a dropped comment is caught, but one lifted off
    a rewritten member and written onto the other leaves both texts behind and would pass: the
    line it moved to is inside the allowed footprint as well, and the loaded mapping never sees
    a comment at all. Each entry's comments are therefore looked for between that entry's own
    site and the next entry's, which is the span the author wrote them in.

    Preserving what the author wrote leaves the other half of the claim open, which is that a
    rewrite writes no comment of its own. Nothing this generator puts in a value carries a
    ``#``, so every one in the block either separates a ref from its section or opens a
    comment, and a rewrite that neither drops nor invents one leaves that count alone.

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


def _member_heads(entry: Entry) -> tuple[str, ...]:
    """Return the openings of the member lines a block edit lands on, up to where a value starts.

    The key, the colon after it and the spaces the author left before the value, all of which a
    replacement of that value has to write past. A head is read off every line the edit may land
    on rather than off the first one alone, because a member written as an explicit pair spreads
    that opening over two lines: the ``? seen`` key on one, and on the next the ``:`` its value
    is written after. Reading only the first would leave that separator unclaimed, which is the
    one an in-place replacement actually writes past. Each head's own indentation is dropped,
    since a carrier indents an entry by whatever its shape needs and a member indented wrong
    loads differently or not at all.

    An allowed line either opens a member or continues a value the rewrite replaces outright,
    and only the first kind has an opening to pin: requiring a value line to come back would
    forbid the very edit layer 4 allows. The two are told apart by the indicators, since no
    value this generator writes carries a ``:`` or a ``?``, so a line with neither is scalar
    content. One that did would be claimed here as a key and fail loudly, rather than quietly
    widening what the claim lets a rewrite do.

    Returns nothing where there is no such line to read: an entry whose whole span the model
    leaves to the rewriter, one with no ``seen`` member written for an edit to land on, and a
    flow entry, whose key its own pin carries instead.
    """
    if entry.pin is not None or not entry.edits:
        return ()
    heads: list[str] = []
    for offset in entry.edits:
        line = entry.lines[offset].lstrip()
        colon = line.find(":")
        if colon == -1:
            if "?" in line:
                heads.append(line)
            continue
        rest = line[colon + 1 :]
        heads.append(line[: colon + 1 + len(rest) - len(rest.lstrip(" "))])
    return tuple(heads)


def _assert_styles_preserved(document: Document, after: str, updates: dict[str, str]) -> None:
    """Assert layer 3 byte-local preservation for the parts the line footprint cannot pin.

    Three claims, all about source the semantic oracle would let a rewrite restyle freely. An
    entry's opening keeps the punctuation its collection style is recognized by. A line written
    in flow style, which the footprint can only allow or forbid whole, comes back matching
    character for character everywhere no edit was allowed to land: its carrier's brackets and
    separators, its untouched entries, and the member key and the punctuation around the value
    each edited entry was rewritten at. And a block member, whose whole line the footprint can
    only allow or forbid, still opens with the key the author spelled it with and the spaces
    they left after it, since layer 4 replaces that member's value rather than the line it
    sits on. That last claim is made of every line an edit may land on, since a member spelled
    as an explicit pair carries its key on one line and the separator its value follows on the
    next.

    The block claim is made of every entry the model gives a member to read it off, rather than
    only of the entries an update was applied to. An entry no edit may reach satisfies it for
    free, since nothing inside it may change, and restricting it to the applied ones would miss
    the case in between: a relocated anchor definition is written onto a member of an entry no
    update named, whose key and separator are the author's source just the same.
    """
    raw_meta = _parts(after).raw_meta
    for entry in document.entries:
        if entry.marker is not None:
            assert entry.marker in raw_meta, f"{entry.name} was restyled: {entry.marker!r}"
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
    lines = _meta_lines(after)
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


def _assert_replacements_stay_bare(after: str, updates: dict[str, str]) -> None:
    """Assert every hash a rewrite wrote lands bare, with no node property in front of it.

    Layer 4 has replacement consume the replaced value's node properties: the anchor and tag
    opening a ``seen`` are dropped with the run of space they leave, since a tag kept would
    retype the new hash and an anchor kept would bind a name to a value the author never wrote.
    Nothing else in the suite sees that. The semantic oracle loads ``!!str hash`` and
    ``&name hash`` to the same string a bare hash gives, the footprint leaves the whole line an
    edit lands on to the rewriter, and the style claims read the source a rewrite wrote past
    rather than the text it wrote.

    The claim is made of the hash rather than of the line it sits on, because a line may carry a
    property the rewrite is entitled to write: a relocated anchor opens the displaced old value,
    and a key may be spelled through an alias. Only the run between the hash and the separator in
    front of it is pinned, and it has to be spaces alone. A hash with no separator before it sits
    on a line of its own, whose whole opening is the indentation its member was written at.
    """
    raw_meta = _parts(after).raw_meta
    for value in updates.values():
        for match in re.finditer(re.escape(value), raw_meta):
            opening = raw_meta[raw_meta.rfind("\n", 0, match.start()) + 1 : match.start()]
            written = opening[opening.rfind(":") + 1 :]
            assert not written.strip(" "), (
                f"a rewrite wrote {written!r} in front of the hash replacing {value!r}, "
                "whose node properties layer 4 has it consume"
            )


def _assert_supported_round_trip(document: Document, updates: dict[str, str]) -> None:
    """Assert a layer 2 document rewrites correctly, preserving layer 3 and confining layer 4."""
    text = document.render()
    _assert_strict_tracked(text)
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
    allowed, inserts = document.footprint(updates)
    _assert_footprint_confined(_meta_lines(text), _meta_lines(after), allowed, inserts)
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

    The reused-anchor shape is the one entry here whose column depends on which parser the
    strict boundary is running, so it joins the pool only where the probe says it is strictly
    loadable. Everywhere else it is generated through the family 2c safe-outcome union
    instead, which is where a reread-only shape belongs, rather than being dropped.
    """
    builders = {
        "aliased-entry": _aliased_entry_document,
        "relocating-anchor": _relocating_anchor_document,
        "merge": _merge_document,
        "tagged-merge": _merge_document,
        "entry-merge": _entry_merge_document,
        "tagged-entry-merge": _entry_merge_document,
        "alias-spelled-entry-key": _alias_spelled_key_document,
        "alias-spelled-ref-key": _alias_spelled_key_document,
        "alias-spelled-root-key": _alias_spelled_key_document,
        "alias-spelled-derives-key": _alias_spelled_key_document,
    }
    if REUSED_ANCHORS_ARE_STRICT:
        builders["reused-anchor"] = _reused_anchor_document
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
    edits = tuple(offset for offset, line in enumerate(written) if "seen" in line)
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
        _style_marker(written[0]),
        edits if len(written) > 1 else None,
        pin=None if inline is None else _flow_pin(inline, "none"),
        site=fields["head"],
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


def _reused_anchor_document(_draw, _shape: str) -> Document:
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
    )
    lines = ["id: doc", "derives_from:", *first.lines, *second.lines, *third.lines]
    entries = (first, second, third)
    spans = ((2, 4), (4, 6), (6, 8))
    order = ("id", "derives_from")
    return Document(tuple(lines), entries, spans, {"id": "doc"}, order, _flat_envelope(), ())


SCALAR_CONTENTS = (
    "old0000",
    "p\\u0085q",
    "a\\u009bb",
    'has \\"quote\\"',
    "back\\\\slash",
    "tab\\there",
)


def _relocating_anchor_pair(written: str, value: str) -> Document:
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

    Returns:
        The two-entry document, with the footprint of a relocation modelled on it.
    """
    aliased = ("- ref: up-1#s1", "  seen: *shared")
    anchored = ("- ref: up-0#s0", f"  seen: &shared {written}")
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
        _style_marker(anchored[0]),
        (1,),
        site="up-0#s0",
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
        _style_marker(aliased[0]),
        (1,),
        site="up-1#s1",
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
    content = draw(st.sampled_from(SCALAR_CONTENTS))
    value = content.encode("utf-8").decode("unicode_escape")
    return _relocating_anchor_pair(f'"{content}"', value)


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
        _style_marker(inline),
        None,
        False,
        _flow_pin(inline, "none"),
    )
    assembled = Document(
        tuple(lines), (merged,), ((1, 2),), {"id": "doc"}, None, _flat_envelope(), ()
    )
    return _finish(draw, assembled)


def _entry_merge_document(draw, shape: str) -> Document:
    """An entry whose ``ref`` arrives through a merge key, in either merge spelling.

    The merge line is the entry's own source and no part of what an update rewrites: the value
    it names lives in the mapping the merge pulls in, and the ``seen`` this update lands on is
    either a member of the entry itself or one written on a line of its own just past it. The
    footprint is modelled that way rather than left to the whole-span allowance, which would
    let a rewrite restyle the merge key beside the edit with nothing here to see it.
    """
    key = "<<" if shape == "entry-merge" else "!!merge inherited"
    spelled = draw(st.booleans())
    entry_lines = [f"- {key}: {{ref: up-0#s0}}"]
    if spelled:
        entry_lines.append("  seen: old0000")
    entry = Entry(
        f"{shape}-{'with' if spelled else 'without'}-own-seen",
        tuple(entry_lines),
        None,
        "up-0#s0",
        "old0000" if spelled else None,
        spelled,
        None,
        (),
        (),
        _style_marker(entry_lines[0]),
        (1,) if spelled else (),
        not spelled,
        site="up-0#s0",
    )
    lines = ("id: doc", "derives_from:", *_indent(entry.lines, 2))
    order = ("id", "derives_from")
    assembled = Document(
        lines, (entry,), ((2, len(lines)),), {"id": "doc"}, order, _flat_envelope(), ()
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
            _style_marker(entry_lines[0]),
            # The alias spells the key, not the value, so the rewrite lands on the member the
            # same way it does for an ordinary explicit pair: the `ref` line above it is
            # untouched, and the whole entry is not the rewriter's to restyle.
            tuple(range(1, len(entry_lines))),
            site="up-0#s0",
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
            _style_marker(entry_lines[0]),
            # Only the `seen` member is rewritten; the aliased `ref` key above it, in either
            # spelling, stays exactly as it was written.
            (len(entry_lines) - 1,),
            site="up-0#s0",
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


@FUZZ_SETTINGS
@given(data=st.data())
def test_alias_and_merge_shapes_round_trip(data) -> None:
    document = data.draw(alias_and_merge_documents())
    target = data.draw(st.integers(min_value=0, max_value=len(document.entries) - 1))
    updates = {document.entries[target].ref: "new0000beef"}
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


@pytest.mark.parametrize("ref_form", REF_FORMS, ids=lambda form: form.name)
def test_every_ref_and_seen_spelling_pair_round_trips(ref_form: RefForm) -> None:
    """Cover the layer 2 spelling tables exhaustively, which sampling alone cannot promise."""
    for seen_form in SEEN_FORMS:
        entry = _combined_entry(0, ref_form, seen_form)
        _assert_supported_round_trip(_single_entry_document(entry), {entry.ref: "new0000beef"})


def test_every_commented_spelling_keeps_its_comment_at_its_own_site() -> None:
    """Rewrite two commented members at once, the only shape a comment can move inside of.

    One entry cannot tell a comment that moved from one that was kept: the only place it could
    move to is outside the block, and mere presence catches that. Two rewritten members that
    each carry a comment is what makes the site an observable, so every spelling that writes
    one is generated at both positions and both entries are updated in the same pass.
    """
    ref_forms = tuple(form for form in REF_FORMS if form.note or form.name == "plain")
    seen_forms = tuple(form for form in SEEN_FORMS if form.note or form.name == "plain")
    for ref_form in ref_forms:
        for seen_form in seen_forms:
            if not (ref_form.note or seen_form.note):
                continue
            entries = tuple(_combined_entry(index, ref_form, seen_form) for index in range(2))
            updates = {entry.ref: f"new{index:04x}beef" for index, entry in enumerate(entries)}
            _assert_supported_round_trip(_entry_pair_document(entries), updates)


def test_every_whole_entry_spelling_round_trips() -> None:
    """Cover every entry spelling that is written as one indivisible shape."""
    for form in WHOLE_FORMS:
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

    A reused anchor name is the one construct whose AD-31 column depends on which parser the
    strict boundary runs, so a table entry that introduced one would pass wherever the pure
    parser is in use and fail wherever the optional accelerator is installed. Two entries are
    built so the per-entry naming scheme is checked as well as each spelling on its own.
    """
    for ref_form in REF_FORMS:
        for seen_form in SEEN_FORMS:
            pair = tuple(
                line
                for index in (0, 1)
                for line in _combined_entry(index, ref_form, seen_form).lines
            )
            names = _anchor_definitions(pair)
            assert len(names) == len(set(names)), f"{ref_form.name}+{seen_form.name}: {names}"
    for form in WHOLE_FORMS:
        pair = tuple(line for index in (0, 1) for line in _whole_entry(index, form).lines)
        names = _anchor_definitions(pair)
        assert len(names) == len(set(names)), f"{form.name}: {names}"


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

UNREADABLE_ON_ANY_REREAD = (
    ("unclosed-fence", "id: doc\nderives_from:\n  - ref: up-0#s0\n", False),
    ("unparseable-flow", "id: doc\nderives_from: [1, 2\n", True),
    ("unparseable-indent", "id: doc\n  stray: 1\nderives_from: []\n", True),
    ("root-block-sequence", "- one\n- two\n", True),
    ("root-flow-sequence", "[one, two]\n", True),
    ("root-bare-scalar", "just prose\n", True),
    ("root-quoted-scalar", '"just prose"\n', True),
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


def _fenced(meta: str, envelope: Envelope) -> str:
    """Render an arbitrary frontmatter block inside a layer 2a envelope."""
    text = f"{envelope.open_fence}\n{meta}{envelope.close_fence}"
    if envelope.trailing_newline:
        text = f"{text}\n{envelope.body}"
    return _with_ending(BOM * envelope.boms + text, envelope.ending)


@FUZZ_SETTINGS
@given(data=st.data())
def test_any_reread_refuses_a_broken_block(data) -> None:
    name, meta, closed = data.draw(st.sampled_from(UNREADABLE_ON_ANY_REREAD))
    envelope = data.draw(envelopes(endings=("\n", "\r\n", "\r")))
    text = _fenced(meta, envelope) if closed else _with_ending(f"---\n{meta}body\n", "\n")

    with pytest.raises(UnreadableDocError) as error:
        _rewrite_bytes(text, {"up-0#s0": "new0000beef"})

    assert error.value.code == "UNREADABLE_DOC", name


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
    bad = data.draw(
        st.one_of(
            st.sampled_from(NON_MAPPING_ENTRIES).map(lambda line: (line,)),
            st.sampled_from(NON_STRING_REFS),
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

    with pytest.raises(UnreadableDocError) as error:
        _rewrite_bytes(text, updates)

    assert error.value.code == "UNREADABLE_DOC"


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


def _recovery_documents() -> tuple[Document, ...]:
    """Build the reread-only shapes strict validation rejects, each with its own model.

    A reused anchor name joins this pool exactly where the strict boundary refuses it, which
    is where the optional accelerator is installed. That keeps the shape generated on every
    leg: family 1 owns it under the pure parser, this union owns it under the C one.

    Every shape here models the footprint of its update member by member rather than taking the
    whole-span allowance. Recovery is held to the footprint alone, since layer 3 is a claim
    about layer 2 documents, so the whole-span allowance is the only thing standing between a
    rewrite and the source beside the member it edits: the ``ref`` it is found by, an extra
    member the contract says survives untouched, and the member an appended ``seen`` is written
    after. What the union leaves open is whether the rewrite happens at all, not how far it
    reaches once it does.
    """
    conditional = () if REUSED_ANCHORS_ARE_STRICT else (_reused_anchor_document(None, ""),)
    return (
        *conditional,
        _recovery_extra_root_key(),
        _recovery_extra_entry_key(),
        _recovery_non_string_seen("!!int 5", 5),
        _recovery_non_string_seen("5", 5),
        _recovery_non_string_seen("true", True),
        _recovery_non_string_seen("1.5", 1.5),
        _recovery_non_string_seen("2026-01-01", date(2026, 1, 1)),
        _recovery_relocated_non_string_seen(),
        _recovery_aliased_derives_from(),
        _recovery_aliased_entry(),
        _recovery_trailing_block_scalar_member(),
        _recovery_multi_line_flow_member(),
        _recovery_null_member("note:", None),
        _recovery_null_member("note: null", None),
    )


def _recovery_extra_root_key() -> Document:
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


def _recovery_aliased_derives_from() -> Document:
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
    except (ConfigError, UnreadableDocError):
        return
    assert disposition != "tracked", "a recovery shape must not pass strict validation"


def _assert_safe_recovery(document: Document, updates: dict[str, str]) -> None:
    """Assert a reread-only shape lands on one of the three outcomes the union admits.

    Requiring success would promote non-contractual recovery into a commitment, so this only
    rejects a crash, a wrong value, and an edit outside the layer 4 footprint.
    """
    text = document.render()
    try:
        rewrites = _rewrite_bytes(text, updates)
    except UnreadableDocError:
        # A clean project-level refusal is one of the three outcomes the union admits; any
        # other exception type escapes here and fails the caller as the crash it is.
        return

    if not rewrites:
        return
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


@FUZZ_SETTINGS
@given(data=st.data())
def test_defensive_recovery_stays_inside_the_safe_outcome_union(data) -> None:
    """A reread-only shape may no-op, refuse cleanly, or rewrite correctly, and nothing else."""
    document = data.draw(st.sampled_from(_recovery_documents()))
    updates = {document.entries[0].ref: "new0000beef"}
    _assert_not_strictly_tracked(document.render())
    _assert_safe_recovery(document, updates)


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
    """Pin current bounded loader behavior: the safe constructor's refusal reaches the CLI clean.

    AD-31 records this as current behavior rather than a guaranteed refusal, so the pin is the
    stable observable, the project error type and its code, not the loader's own message, which
    differs across the ``ruamel.yaml`` releases and accelerator cells CI runs.
    """
    text = f"---\n{meta}---\nbody\n"

    with pytest.raises(UnreadableDocError) as error:
        apply_reconcile(text, updates, DOC)

    assert error.value.code == "UNREADABLE_DOC"
    assert isinstance(error.value, ProjectError)
    assert "doc.md" in str(error.value)


@pytest.mark.parametrize("meta", MERGES_INSIDE_ORDERED_MAPS)
def test_a_merge_inside_an_ordered_map_is_reported_as_an_unreadable_document(meta: str) -> None:
    """Pin current bounded loader behavior: an ordered map never flattens a merge key.

    The loader builds an ordered map from its items rather than through mapping construction,
    so the merge tag reaches the constructor with nothing to build it and the document is
    refused at load rather than reconciled. Pinned at the type and code for the same
    cross-parser reason as the malformed case above.
    """
    text = f"---\n{meta}---\nbody\n"

    with pytest.raises(UnreadableDocError) as error:
        apply_reconcile(text, {"up-0#s0": "new0000beef"}, DOC)

    assert error.value.code == "UNREADABLE_DOC"


# --------------------------------------------------------------------------------------------
# Deterministic regression cases for the shapes the properties must never stop covering.
# --------------------------------------------------------------------------------------------

PLAIN_META = "id: doc\nderives_from:\n  - ref: up-0#s0\n    seen: old0000\n"
PLAIN_REWRITTEN = "id: doc\nderives_from:\n  - ref: up-0#s0\n    seen: new0000beef\n"


@pytest.mark.parametrize(
    "ending",
    [pytest.param("\n", id="uniform-lf"), pytest.param("\r\n", id="uniform-crlf")],
)
def test_a_uniform_line_ending_survives_the_rewrite(ending: str) -> None:
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


def test_a_reused_anchor_keeps_an_alias_bound_to_its_later_definition() -> None:
    """A later definition rebinds the name, so relocating the first value must pass the alias by.

    Which AD-31 column this document sits in depends on the parser the strict boundary runs:
    the optional ``ruamel.yaml.clib`` composer rejects a duplicate anchor definition, while
    the pure one warns and rebinds.

    Where the strict load accepts it the document is a layer 2 one, so the rewrite is
    mandatory and the two rebinding invariants below are asserted unconditionally. Where the
    strict load refuses it the document is reread-only, and layer 5 admits a clean refusal or
    a no-op as readily as a rewrite; the invariants are then asserted only on a rewrite that
    was actually produced, because demanding one would promote non-contractual recovery into
    a commitment. The safe-outcome union above has already judged the other two outcomes.
    """
    document = _reused_anchor_document(None, "reused-anchor")
    updates = {document.entries[0].ref: "new0000beef"}

    if REUSED_ANCHORS_ARE_STRICT:
        _assert_supported_round_trip(document, updates)
        _assert_anchor_rebinding_survived(_rewrite_bytes(document.render(), updates))
        return

    _assert_not_strictly_tracked(document.render())
    _assert_safe_recovery(document, updates)
    try:
        rewrites = _rewrite_bytes(document.render(), updates)
    except UnreadableDocError:
        return
    _assert_anchor_rebinding_survived(rewrites)


def _assert_anchor_rebinding_survived(rewrites: list[Rewrite]) -> None:
    """Assert a rewrite left the alias reading the later definition of a reused anchor name."""
    if not rewrites:
        return
    after = rewrites[0].after.decode("utf-8")
    # The relocation must not land on the alias site, which reads the second definition.
    assert "seen: &shared old0001" in after
    assert "seen: *shared" in after


NEL = "\u0085"
CSI = "\u009b"

RELOCATED_CONTENTS = (
    pytest.param('"p\\u0085q"', f"p{NEL}q", id="nel"),
    pytest.param('"a\\u009bb"', f"a{CSI}b", id="c1-control"),
    pytest.param('"p\\u0085q\\u009b\\tr"', f"p{NEL}q{CSI}\tr", id="nel-and-c1-and-tab"),
    pytest.param(
        '"has \\"quote\\" and \\\\ slash"',
        'has "quote" and \\ slash',
        id="quotes-and-backslashes",
    ),
)


@pytest.mark.parametrize(("written", "value"), RELOCATED_CONTENTS)
def test_an_anchored_seen_relocates_as_escapes_and_reparses(written: str, value: str) -> None:
    """A displaced anchored value is re-emitted escape-only, so the alias site still reparses."""
    document = _relocating_anchor_pair(written, value)

    _assert_supported_round_trip(document, {"up-0#s0": "new0000beef"})

    after = _rewrite_bytes(document.render(), {"up-0#s0": "new0000beef"})[0].after.decode("utf-8")
    assert f"seen: &shared {written}" in after
    assert not any(0x7F <= ord(char) <= 0x9F for char in after)


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
