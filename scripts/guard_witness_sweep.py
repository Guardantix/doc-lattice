#!/usr/bin/env python3
"""Search for authored inputs that reach a still-unclassified fail-closed guard origin.

AD-20 requires every guard origin in the CI shell scanner to carry executable evidence: either a
`ReachableWitness` naming an input that reaches it through the public scan path, or an
`InvariantWitness` explaining why authored input cannot. This tool finds the first kind.

It works in two modes, and the second is what unsticks the first.

**Sweep.** Drive `scan_doc_lattice_invocations` over a corpus, once per shrunk cap, and record
which guard origin each combination reports. Shrinking one cap at a time keeps the attribution
unambiguous: the reported origin is the guard that cap governs. The corpus is the recorded replay
inventory plus bodies from the fuzzer's compositional grammar, which between them cover the
shapes the scanner was built against.

**Trace.** For one candidate script, report which guard-holding *functions* it executes at all. A
sweep that finds nothing says only that the corpus never reached the machinery; the trace says how
far it got, so the next candidate can be aimed one level deeper. Locating
`eval 'X=${Y=q}'` as the shape that reaches the eval-syntax assignment and decision recorders,
where a plain `eval 'X=${Y}'` does not, came from this mode rather than from any sweep.

The functions between the guards separate two shapes too, and `--trace-all` keeps them: it reports
every function in a guarded module, most of which hold no guard. That is the wider, noisier view,
worth reaching for once the filtered one stops distinguishing two candidates.

Neither mode classifies anything on its own. A row it prints is a candidate: paste it into
`tests/guard_witnesses.py`, where the suite then holds it to returning that exact identifier, and
delete that origin's record from the debt snapshot, since a default sweep reports only guards that
are still frozen there and the gate refuses one that is both classified and frozen.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import random
import sys
from pathlib import Path
from typing import TYPE_CHECKING

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

import check_guard_inventory  # noqa: E402
import fuzz_shell_taint  # noqa: E402

from doc_lattice.github_ci.shell_guards import GuardRefusal, ScanLimits  # noqa: E402
from doc_lattice.github_ci.shell_scanner import scan_doc_lattice_invocations  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

REPLAY_INVENTORY = "tests/fixtures/github_ci_checkpoint/replay_inventory.json"
RECORDER_SCRIPT = "scripts/checkpoint_record_scanner_inputs.py"
DEBT_PATH = "tests/fixtures/shell_guard_debt.json"
SCANNER_MODULE = "src/doc_lattice/github_ci/shell_scanner.py"
PRODUCTION = "production"
SEEDS = 4
ITERATIONS = 400
MAX_LENGTH = 600
SHRINK = (0, 1, 2, 3)
LINE_LIMIT = 100
ROW_INDENT = "        "
BMP_MAX = 0xFFFF
"""Highest code point a Python literal spells with the four-digit escape."""

Reach = dict[str, tuple[str, str]]
"""Guard origin identifier -> (limits label, the shortest script that reached it)."""


def caps_slots(limits: type) -> dict[str, str]:
    """Return which field of `limits` each caps class is constructed into.

    Read off the values a default instance holds rather than the field annotations, which are text
    under `from __future__ import annotations`: an annotation written `TaintLimits | None`, or
    under an alias, still names the same class but no longer spells it. The grid that mints a label
    and the renderer that has to place it back both read this one derivation, so they cannot
    disagree about where a caps value belongs.

    Args:
        limits: Dataclass whose fields carry the caps values.

    Returns:
        Caps class name mapped to the keyword that carries it.
    """
    defaults = limits()
    return {
        type(getattr(defaults, field.name)).__name__: field.name
        for field in dataclasses.fields(defaults)
    }


def limits_slots() -> dict[str, str]:
    """Return which `ScanLimits` field each caps class is constructed into.

    Returns:
        Caps class name mapped to the keyword that carries it.
    """
    return caps_slots(ScanLimits)


def limits_field_count() -> int:
    """Return how many distinct caps a single-cap sweep shrinks.

    Returns:
        The combined field count of every caps value `ScanLimits` carries.
    """
    defaults = ScanLimits()
    return sum(
        len(dataclasses.fields(getattr(defaults, field.name)))
        for field in dataclasses.fields(defaults)
    )


def limits_grid(values: Sequence[int] = SHRINK) -> list[tuple[str, ScanLimits]]:
    """Return one configuration per cap per shrink value, plus the unshrunk one.

    Only one cap is shrunk at a time, so whichever guard refuses is the guard that cap governs.
    Earlier entries shrink less: production comes first and each cap's values descend, which is
    the preference order `sweep` resolves ties with.

    Args:
        values: Shrink values to try for each cap.

    Returns:
        Labelled scan-limits configurations.
    """
    ordered = sorted(values, reverse=True)
    defaults = ScanLimits()
    grid: list[tuple[str, ScanLimits]] = [(PRODUCTION, defaults)]
    for name, slot in caps_slots(ScanLimits).items():
        caps = type(getattr(defaults, slot))
        for field in dataclasses.fields(caps):
            for value in ordered:
                grid.append(
                    (
                        f"{name}({field.name}={value})",
                        ScanLimits(**{slot: caps(**{field.name: value})}),
                    )
                )
    return grid


def scanner_checkout() -> Path:
    """Return the repository the scanner this process executes was imported from.

    Everything a run reads describes the guards of one revision: the debt snapshot names the ones a
    sweep is looking for, and the recorded inventory supplies the names a trace accepts. The
    scanner that decides which guards exist at all is imported statically, and so are the corpus
    grammar and the inventory checker. Reading the tree from anywhere else would filter a run
    against guards it never executed, and reach the read tree does not record would be dropped with
    no diagnostic, which reads exactly like a candidate that reaches nothing.

    Derived from the imported module rather than from this file's location for the reason
    `guarded_filenames` records: the package can resolve somewhere other than the tree beside this
    script, and that copy is the one whose guards a run observes.

    Returns:
        Repository root containing the imported guard package.

    Raises:
        ValueError: If the imported scanner reports no source file to locate a checkout from, or
            resolves outside a checkout, since everything else a run reads lives only in one.
    """
    relative = Path(SCANNER_MODULE)
    dotted = ".".join(relative.with_suffix("").parts[1:])
    source = importlib.import_module(dotted).__file__
    if source is None:
        message = "the imported scanner reports no source file to locate its checkout from"
        raise ValueError(message)
    # Walked back by the recorded path's own depth, so moving the guard package within the tree
    # moves the derivation with it instead of silently naming a directory inside the package.
    # Clamped to the shallowest ancestor a flattened copy has, since walking back off the end
    # raises an index traceback naming nothing where the refusal below explains the case.
    parents = Path(source).resolve().parents
    root = parents[min(len(relative.parts) - 1, len(parents) - 1)]
    if not (root / relative).exists():
        # An installed copy walks back to a site-packages ancestor holding none of the debt
        # snapshot, replay inventory or guard sources, and the tool takes no root to correct that
        # with. Said here rather than as a missing-file traceback from whichever read got there
        # first, which names a path nothing explains.
        message = (
            f"{root} holds no {SCANNER_MODULE}: the scanner was imported from an installed copy "
            "rather than a source checkout, and the debt snapshot, replay corpus and guard "
            "sources a run reads exist only in the checkout. Run against one, with the package "
            "installed editable."
        )
        raise ValueError(message)
    return root


def unclassified_ids(root: Path) -> frozenset[str]:
    """Return the guard identifiers still frozen as rollout debt.

    Args:
        root: Repository root holding the debt snapshot.

    Returns:
        Identifiers with no witness yet.
    """
    payload = json.loads((root / DEBT_PATH).read_text(encoding="utf-8"))
    return frozenset(record["origin_id"] for record in payload["records"])


def load_corpus(
    root: Path,
    *,
    seeds: int = SEEDS,
    iterations: int = ITERATIONS,
    max_length: int = MAX_LENGTH,
    extra: Path | None = None,
) -> list[str]:
    """Return the scripts to sweep, shortest first.

    Args:
        root: Repository root holding the replay inventory.
        seeds: How many fuzzer seeds to generate bodies from.
        iterations: Bodies to request per seed.
        max_length: Drop generated scripts longer than this; a witness has to stay reviewable.
        extra: Optional JSON file holding a list of hand-authored candidates.

    Returns:
        Deduplicated scripts ordered by length.

    Raises:
        ValueError: If the replay inventory carries no recorded scripts, or the extra file is not
            a list of scripts, or holds a candidate the length filter would drop, since a sweep
            that never scanned it prints the same nothing as one that scanned it and reached no
            guard.
    """
    corpus: set[str] = set()
    inventory = json.loads((root / REPLAY_INVENTORY).read_text(encoding="utf-8"))
    entries = inventory.get("entries") if isinstance(inventory, dict) else None
    # Named here rather than left as a bare KeyError from a restructured fixture, which reports
    # neither the file that no longer holds what a sweep reads nor the script that writes it.
    # An empty list is refused with it: every per-entry check below passes vacuously, so a sweep
    # would search the generated half alone and report a script count that reads like the whole
    # corpus. The recorded half is the shapes the scanner was built against, and dropping it in
    # silence turns "reached nothing" into an answer about a corpus nobody asked to run.
    if (
        not isinstance(entries, list)
        or not entries
        or not all(
            isinstance(entry, dict) and isinstance(entry.get("source"), str) for entry in entries
        )
    ):
        message = (
            f"{root / REPLAY_INVENTORY} holds no entries carrying a 'source' script, so the "
            "recorded half of the corpus cannot be read; regenerate it with "
            f"{RECORDER_SCRIPT}"
        )
        raise ValueError(message)
    corpus.update(entry["source"] for entry in entries)
    for seed in range(seeds):
        for case in fuzz_shell_taint.generate(random.Random(seed), iterations):
            corpus.add(case.script)
    if extra is not None:
        candidates = json.loads(extra.read_text(encoding="utf-8"))
        # A bare JSON string would otherwise update the set with its characters and a JSON object
        # with its keys, so the sweep would quietly search something nobody authored.
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, str) for candidate in candidates
        ):
            message = f"{extra} must hold a JSON list of scripts"
            raise ValueError(message)
        overlong = [candidate for candidate in candidates if len(candidate) > max_length]
        if overlong:
            # The filter below bounds what the grammar generates. Applied to what somebody wrote by
            # hand it removes the one candidate the run was for, and reports it as a corpus that
            # reached nothing.
            message = (
                f"{extra} holds {len(overlong)} candidate(s) longer than the {max_length}"
                "-character --max-length; raise it rather than sweeping without them"
            )
            raise ValueError(message)
        corpus.update(candidates)
    # The text tie-break keeps equal-length scripts in a deterministic order, so a sweep prints
    # the same witness rows under every PYTHONHASHSEED.
    return sorted(
        (script for script in corpus if len(script) <= max_length),
        key=lambda script: (len(script), script),
    )


def sweep(
    corpus: Iterable[str],
    grid: Sequence[tuple[str, ScanLimits]],
    *,
    wanted: frozenset[str] | None = None,
    found: Reach | None = None,
) -> Reach:
    """Return the shortest script that reaches each guard origin the corpus can reach.

    Args:
        corpus: Scripts to drive through the public scan path.
        grid: Labelled scan-limits configurations to try.
        wanted: Restrict the result to these identifiers, or None for every guard reached.
        found: Accumulator to record reach into. A caller that owns it still holds every origin
            reached so far when a scan raises something no configuration was expected to raise,
            rather than losing a multi-minute run's rows to the traceback.

    Returns:
        Guard identifier mapped to the labelled configuration and script that reached it. Scripts
        no configuration could parse are counted on stderr rather than dropped in silence.
    """
    scripts = list(corpus)
    found = {} if found is None else found
    best: dict[str, tuple[int, int]] = {}
    scanned: set[str] = set()
    skipped: set[str] = set()
    for rank, (label, limits) in enumerate(grid):
        for script in scripts:
            try:
                result = scan_doc_lattice_invocations(script, limits=limits)
            except RecursionError:
                # Counted rather than dropped: a candidate no configuration can parse is scored by
                # none of them, and silence about it reads as a candidate that reached nothing,
                # which is the opposite conclusion about the same shape.
                skipped.add(script)
                continue
            scanned.add(script)
            verdict = result.verdict
            if not isinstance(verdict, GuardRefusal):
                continue
            if wanted is not None and verdict.origin_id not in wanted:
                continue
            # Prefer the shortest script, then the earliest grid entry, so a witness stays
            # readable and says as little about the caps as it can get away with: the grid puts
            # production first and orders each cap's values least-shrunk first.
            key = (len(script), rank)
            if verdict.origin_id not in best or key < best[verdict.origin_id]:
                best[verdict.origin_id] = key
                found[verdict.origin_id] = (label, script)
    # Only the bodies no configuration scanned: recursion depth is a property of the script and
    # the caps together, so a body one shrunk entry cannot parse is still scored by the rest, and
    # counting it here sends the operator after coverage the sweep already had.
    unscanned = skipped - scanned
    if unscanned:
        sys.stderr.write(
            f"skipped {len(unscanned)} scripts no configuration could parse within the "
            "interpreter's recursion limit\n"
        )
    return found


def guarded_modules(root: Path) -> tuple[str, ...]:
    """Return the modules a trace records frames from.

    Taken from the gate's own guarded surface rather than a list kept here, for the reason
    `guard_owning_functions` records at one level down: a list here is a second allowlist, and a
    guard module missing from it contributes no frames while the inventory still names its
    origins, so every one of them is intersected away with no diagnostic. That surface is the
    discovered modules together with the checker's allowlist, so a module reaches this tool while
    it is still being classified, which is when a trace is worth running.

    Args:
        root: Repository root to read the guard package from.

    Returns:
        Repository-relative module paths in path order.
    """
    return check_guard_inventory.repository_guarded_modules(root)


def guarded_filenames(root: Path) -> frozenset[str]:
    """Return the source filenames CPython reports for frames in the guarded modules.

    Read from the imported modules rather than composed from the root, so the tracer still matches
    when the checkout is reached through a symlink or the package resolves to an installed copy
    instead of the tree beside this script. A composed path that failed to match would make every
    trace report an empty set, which reads exactly like a candidate that reaches no guard
    machinery at all.

    Args:
        root: Repository root the guarded modules are read from.

    Returns:
        Every spelling of a guarded module's filename a traced frame may carry.

    Raises:
        ModuleNotFoundError: If a guarded module cannot be imported, since tracing the rest would
            report a partial reach as the whole one.
    """
    names: set[str] = set()
    for module in guarded_modules(root):
        # Derived from the recorded path rather than a hard-coded package prefix, so a guarded
        # module that moves within the tree still resolves to the module actually imported.
        dotted = ".".join(Path(module).with_suffix("").parts[1:])
        imported = importlib.import_module(dotted)
        source = imported.__file__
        if source is not None:
            names.add(source)
            names.add(str(Path(source).resolve()))
        names.add(str(root / module))
    return frozenset(names)


def guard_owning_functions(root: Path) -> frozenset[tuple[str, str]]:
    """Return the functions that construct a guard refusal, each with its module.

    Taken from the gate's own inventory rather than a list kept here, so a guard that moves takes
    its name with it. The inventory derives the name from the source tree and the tracer reads it
    off a running frame, and the two agree because neither spells a nested function's `<locals>`.

    Carried with the module the inventory records, since a qualified name alone does not identify
    a function: `_is_function_positional_parameter` is defined in both guarded modules today, so a
    bare-name intersection would accept reach into one of them as evidence for a guard the other
    one owns, and the pasted witness then fails on an origin the shape never reaches.

    Args:
        root: Repository root holding the guarded modules.

    Returns:
        One (module filename, qualified name) pair per function holding at least one guard origin.

    Raises:
        ValueError: If the inventory reports no origin at all, which would filter every trace down
            to nothing and read exactly like a candidate that reaches no guard machinery.
    """
    records = check_guard_inventory.repository_origin_records(root)
    if not records:
        message = f"{root} reports no guard origins to filter a trace against"
        raise ValueError(message)
    return frozenset((record.path, record.qualname) for record in records)


def trace_guard_functions(script: str, limits: ScanLimits | None = None) -> set[tuple[str, str]]:
    """Return the guarded-module functions one script executes.

    Args:
        script: Candidate Bash source.
        limits: Optional shrunk caps.

    Returns:
        (Module filename, qualified name) pairs, with nested-function `<locals>` markers removed.
        The module is the filename the inventory records a guard under, so a reach and an origin
        are compared as the same function rather than as the same name.
    """
    # The tree is derived here rather than accepted, for the reason `main` records: the frames a
    # run records and the names it accepts have to describe one revision, and that is the revision
    # whose scanner this process executes.
    guarded = guarded_filenames(scanner_checkout())
    reached: set[tuple[str, str]] = set()
    # Restore whatever hook was active, not None: coverage collection and debuggers also use
    # settrace, and clearing it would silently disable them for the rest of the process.
    previous = sys.gettrace()

    def trace(frame, event, argument):  # noqa: ANN001, ANN202 - CPython tracer signature
        if event == "call" and frame.f_code.co_filename in guarded:
            reached.add(
                (
                    Path(frame.f_code.co_filename).name,
                    frame.f_code.co_qualname.replace(".<locals>", ""),
                )
            )
        if previous is None:
            return None
        # Restoring that hook afterwards protects only what runs later: for the length of the scan
        # it is replaced, and these are the frames a settrace-based coverage run or a debugger
        # session most needs to see. A global hook's return value is the local trace function for
        # the frame, so handing back what it returns keeps its line and return events flowing too.
        result = previous(frame, event, argument)
        # Delegating to it is not enough to keep receiving events. `coverage.CTracer` re-installs
        # itself at the C level when it is invoked through Python dispatch, so the call above hands
        # the global hook away and every frame below this one is dispatched past this function: the
        # reach comes back holding the scan's entry frame and nothing under it. Take the hook back
        # each time it is taken, which leaves the ambient hook collecting and this one dispatching.
        if sys.gettrace() is not trace:
            sys.settrace(trace)
        return result

    sys.settrace(trace)
    try:
        scan_doc_lattice_invocations(script, limits=limits)
    except RecursionError:
        # Report how far the candidate got instead of losing the whole trace to a traceback. The
        # sweep skips these bodies for the same reason, and a partial reach is still the signal
        # this mode exists to give.
        #
        # Said out loud, because a truncated reach prints exactly what a candidate that genuinely
        # stops there prints, and read as the second it becomes an invariant justification for a
        # guard this shape was still walking toward.
        sys.stderr.write(
            "the scan did not finish within the interpreter's recursion limit: the reach below "
            "is how far this candidate got, not everything it would have reached\n"
        )
        return reached
    finally:
        sys.settrace(previous)
    return reached


def _literal(text: str) -> str:
    """Return `text` as a double-quoted Python literal any stdout can carry.

    Args:
        text: Identifier or script to embed in a rendered row.

    Returns:
        Source text for the literal, in ASCII.
    """
    # json.dumps escapes exactly what a double-quoted Python literal needs escaped, so the row
    # stays valid for a script that itself contains quotes. ensure_ascii must stay off: a \uXXXX
    # surrogate pair is one character in JSON but two in a Python literal.
    #
    # Every non-ASCII character is then escaped the way Python spells it, astral characters
    # included, so no candidate can make the single write that emits the whole sweep fail on a
    # stdout that is not UTF-8 and cost the run every row it found. A lone surrogate, which
    # json.loads accepts from an --extra candidate and UTF-8 cannot encode at all, escapes the same
    # way and round-trips.
    return "".join(_escaped(character) for character in json.dumps(text, ensure_ascii=False))


def _escaped(character: str) -> str:
    """Return one character as the ASCII source Python reads it back from.

    Args:
        character: A single character of an encoded literal.

    Returns:
        The character itself, or its escape.
    """
    if character.isascii():
        return character
    point = ord(character)
    return f"\\u{point:04x}" if point <= BMP_MAX else f"\\U{point:08x}"


def _literal_lines(text: str, indent: str, suffix: str) -> list[str]:
    """Return `text` as literal lines that fit the repository's line limit.

    A row wider than the limit fails the lint hook the moment it is pasted, and `ruff format`
    cannot split a string literal, so the split happens here. Implicitly concatenated parts are one
    literal to the parser, so a wrapped row still carries the exact script the sweep scanned.

    Args:
        text: Identifier or script to embed in a rendered row.
        indent: Leading whitespace each line carries.
        suffix: Trailing source the last line carries.

    Returns:
        Complete source lines, one literal each.
    """
    budget = LINE_LIMIT - len(indent) - len(suffix)
    parts: list[str] = []
    part = ""
    for character in text:
        # Measured on the encoded part rather than the raw one, since an escape can be twelve
        # characters wide for one that reads as a single character here.
        if part and len(_literal(part + character)) > budget:
            parts.append(part)
            part = character
        else:
            part += character
    parts.append(part)
    return [f"{indent}{_literal(part)}" for part in parts[:-1]] + [
        f"{indent}{_literal(parts[-1])}{suffix}"
    ]


def render_rows(found: Reach) -> str:
    """Return paste-ready `ReachableWitness` rows for a sweep result.

    Args:
        found: Guard identifier mapped to its labelled configuration and script.

    Returns:
        Registry source text. Every row still has to earn its place: the suite holds it to
        returning that exact identifier through the public scan path.

    Raises:
        ValueError: If a label names no caps class, since there is no field to construct it into
            and a guessed one renders a row that parses as arithmetic rather than failing.
    """
    slots = limits_slots()
    lines: list[str] = []
    for origin_id in sorted(found):
        label, script = found[origin_id]
        lines.append("    ReachableWitness(")
        lines.extend(_literal_lines(origin_id, ROW_INDENT, ","))
        lines.extend(_literal_lines(script, ROW_INDENT, ","))
        if label != PRODUCTION:
            field = slots.get(label.split("(", 1)[0])
            if field is None:
                message = (
                    f"{label!r} names no caps class of ScanLimits, so there is no field to render "
                    f"it into; expected one of {', '.join(sorted(slots))} or {PRODUCTION!r}"
                )
                raise ValueError(message)
            lines.append(f"{ROW_INDENT}limits=ScanLimits({field}={label}),")
        lines.append("    ),")
    return "\n".join(lines) + "\n" if lines else ""


def _nonnegative(text: str, reason: str) -> int:
    """Return `text` as an integer, refusing a negative one.

    Args:
        text: The value as spelled on the command line.
        reason: What a negative value would mean here, completing the refusal after the value.

    Returns:
        The value.

    Raises:
        ArgumentTypeError: If the value is negative.
    """
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"{value} {reason}")
    return value


def nonnegative_count(text: str) -> int:
    """Return `text` as a count of scripts, refusing a negative one.

    The three options this converts all size the corpus, and Python spells a negative size as an
    empty one rather than as an error: `range(-1)` walks no grammar and a negative length filter
    drops every script. Left to argparse's `int`, a mistyped value sweeps the recorded half alone,
    or nothing at all, and reports a script count that reads like the run that was asked for.

    Args:
        text: The value as spelled on the command line.

    Returns:
        The value as a count.

    Raises:
        ArgumentTypeError: If the value is negative, since no corpus has a negative size.
    """
    return _nonnegative(text, "is not a count of scripts; a corpus cannot be smaller than empty")


def nonnegative_cap(text: str) -> int:
    """Return `text` as a cap to shrink to, refusing a negative one.

    A cap bounds something a scan counts, so zero is the smallest one there is and a negative value
    is not a tighter bound but a degenerate one: the count exceeds it before the scan has read
    anything. Every row that comes back is then a guard refusing on the shape of its cap rather
    than on the shape of an input, down to the empty script rendering a paste-ready witness for the
    recursion-depth guard, which passes the suite while attesting nothing about authored input.
    The same configurations refuse early enough to mask the guards a zero cap reaches with a real
    script, so a negative value also loses reach the run had, with nothing printed to say so.

    Args:
        text: The value as spelled on the command line.

    Returns:
        The value as a cap.

    Raises:
        ArgumentTypeError: If the value is negative, since no scan counts fewer than none.
    """
    return _nonnegative(text, "is not a cap; a scan cannot count fewer than none")


def main(argv: list[str] | None = None) -> int:
    """Search for witnesses, or trace one candidate script.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit status.
    """
    # No option selects the tree to search: `scanner_checkout` decides it, because a run only
    # means anything against the revision whose scanner, corpus grammar and inventory checker this
    # process imported.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", metavar="SCRIPT", help="report the guards one script reaches")
    parser.add_argument(
        "--trace-all",
        action="store_true",
        default=None,
        help="trace every guarded-module function, not only the ones holding a guard",
    )
    parser.add_argument(
        "--seeds",
        type=nonnegative_count,
        help=f"fuzzer seeds to generate bodies from, one grammar walk each (default {SEEDS})",
    )
    parser.add_argument(
        "--iterations",
        type=nonnegative_count,
        help=f"bodies to request per seed (default {ITERATIONS})",
    )
    parser.add_argument(
        "--max-length",
        type=nonnegative_count,
        help=f"drop generated scripts longer than this many characters (default {MAX_LENGTH})",
    )
    parser.add_argument("--extra", type=Path, help="JSON list of hand-authored candidates")
    parser.add_argument(
        "--shrink",
        type=nonnegative_cap,
        # At least one value, because a bare `--shrink` binds an empty list rather than the
        # default, which collapses the grid to production caps and reports every resource-bound
        # guard as unreached by a run the operator believes searched each cap shrunk.
        nargs="+",
        help=(
            "cap values to shrink each cap to, one cap at a time and lower being more shrunk; "
            f"searched least-shrunk first so a witness says as little about the caps as it can "
            f"(default {' '.join(str(value) for value in SHRINK)})"
        ),
    )
    parser.add_argument(
        "--all-guards",
        action="store_true",
        default=None,
        help="report every guard reached, not only the still-unclassified ones",
    )
    arguments = parser.parse_args(argv)
    # An option the selected mode does not read is refused rather than ignored: accepted and
    # dropped, `--shrink` reports the reach under production caps for a run the operator believes
    # was shrunk, and `--trace-all` alone runs the multi-minute sweep instead of the trace asked
    # for. Both are answers about something other than the question.
    sweep_only = ("seeds", "iterations", "max_length", "extra", "shrink", "all_guards")
    if arguments.trace is None:
        if arguments.trace_all is not None:
            parser.error("--trace-all describes a trace, and --trace was not given")
    else:
        given = [name for name in sweep_only if getattr(arguments, name) is not None]
        if given:
            spelled = ", ".join(f"--{name.replace('_', '-')}" for name in given)
            parser.error(
                f"{spelled} describes the sweep, not the trace --trace runs; to trace under "
                "shrunk caps, call trace_guard_functions(script, limits) directly"
            )
    root = scanner_checkout()

    if arguments.trace is not None:
        reached = trace_guard_functions(arguments.trace)
        if not arguments.trace_all:
            reached &= guard_owning_functions(root)
        # The module decided the intersection; the names alone are what a candidate is aimed with.
        for name in sorted({qualname for _module, qualname in reached}):
            sys.stdout.write(f"{name}\n")
        return 0

    wanted = None if arguments.all_guards else unclassified_ids(root)
    corpus = load_corpus(
        root,
        seeds=SEEDS if arguments.seeds is None else arguments.seeds,
        iterations=ITERATIONS if arguments.iterations is None else arguments.iterations,
        max_length=MAX_LENGTH if arguments.max_length is None else arguments.max_length,
        extra=arguments.extra,
    )
    grid = limits_grid(SHRINK if arguments.shrink is None else tuple(arguments.shrink))
    sys.stderr.write(f"sweeping {len(corpus)} scripts over {len(grid)} configurations\n")
    # Rows are buffered until the sweep ends, so the accumulator is owned here and rendered even
    # when a scan raises something no configuration was expected to raise. The failure still
    # propagates; what it no longer takes with it is every origin the run had already reached,
    # which is a multi-minute search and exactly the shape a witness hunt exists to find.
    found: Reach = {}
    try:
        sweep(corpus, grid, wanted=wanted, found=found)
        sys.stderr.write(f"reached {len(found)} guard origins\n")
    finally:
        sys.stdout.write(render_rows(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
