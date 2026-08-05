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
CHECKER_SCRIPT = "scripts/check_guard_inventory.py"
SCANNER_MODULE = "src/doc_lattice/github_ci/shell_scanner.py"
PRODUCTION = "production"
SEED_COUNT = 4
ITERATIONS = 400
MAX_LENGTH = 600
SHRINK = (0, 1, 2, 3)
LINE_LIMIT = 100
ROW_INDENT = "        "
BMP_MAX = 0xFFFF
"""Highest code point a Python literal spells with the four-digit escape."""

IMPORT_FAILURES = (ImportError, SyntaxError, NameError, AttributeError, TypeError, ValueError)
"""What executing a half-written guarded module raises, as `guarded_filenames` reports it.

The module body runs on import, so the failure is not only the two an unfinished file suggests: a
typo'd constant raises NameError, a not-yet-written helper referenced from a decorator or a
module-level call raises AttributeError or TypeError, and a dataclass or enum rejecting its own
declaration raises ValueError. Named rather than caught as `Exception`, which the repository rules
forbid, and kept here so the reason the tuple is this wide is written once.
"""

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

    Raises:
        ValueError: If two fields carry the same caps class, since the class name is what the grid
            mints a label from and what the renderer places one back with. Collapsed to a single
            entry, one of the fields would never be shrunk at all, and the guards its caps govern
            would be reported as unreached by a run that never configured them, while a row for the
            field that survived names caps the sweep did not run under.
    """
    defaults = limits()
    slots: dict[str, str] = {}
    for field in dataclasses.fields(defaults):
        name = type(getattr(defaults, field.name)).__name__
        if name in slots:
            message = (
                f"{name} is carried by both {slots[name]!r} and {field.name!r} of "
                f"{type(defaults).__name__}, so a label naming it cannot say which field it came "
                "from; give one of them a caps class of its own"
            )
            raise ValueError(message)
        slots[name] = field.name
    return slots


def limits_slots() -> dict[str, str]:
    """Return which `ScanLimits` field each caps class is constructed into.

    Returns:
        Caps class name mapped to the keyword that carries it.
    """
    return caps_slots(ScanLimits)


def limits_grid(values: Sequence[int] = SHRINK) -> list[tuple[str, ScanLimits]]:
    """Return one configuration per cap per shrink value, plus the unshrunk one.

    Only one cap is shrunk at a time, so whichever guard refuses is the guard that cap governs.
    Earlier entries shrink less: production comes first and each cap's values descend, which is
    the preference order `sweep` resolves ties with.

    Args:
        values: Shrink values to try for each cap. Repeats are searched once: a duplicate mints an
            identical configuration under an identical label, which loses the tie-break to the
            first one and can only cost the run another pass over the whole corpus.

    Returns:
        Labelled scan-limits configurations.
    """
    ordered = sorted(set(values), reverse=True)
    defaults = ScanLimits()
    grid: list[tuple[str, ScanLimits]] = [(PRODUCTION, defaults)]
    for name, slot in limits_slots().items():
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

    Raises:
        ValueError: If the snapshot records no identifier a default sweep can look for, for the
            reason `load_corpus` refuses an empty replay inventory: a sweep filters every origin it
            reaches against this set, so an empty one discards the whole run and prints what a
            corpus that reached nothing prints. A restructured snapshot is refused with it, rather
            than left as a bare KeyError naming neither the file nor the gate that maintains it.
    """
    identity = check_guard_inventory.IDENTITY_FIELD
    payload = json.loads((root / DEBT_PATH).read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if (
        not isinstance(records, list)
        or not records
        or not all(
            isinstance(record, dict) and isinstance(record.get(identity), str) for record in records
        )
    ):
        message = (
            f"{root / DEBT_PATH} holds no records carrying a {identity!r} for a sweep to look "
            "for: either every guard is classified, and the reach a run still has is asked for "
            f"with --all-guards, or the snapshot no longer holds what {CHECKER_SCRIPT} freezes"
        )
        raise ValueError(message)
    return frozenset(record[identity] for record in records)


def load_corpus(
    root: Path,
    *,
    seeds: int = SEED_COUNT,
    iterations: int = ITERATIONS,
    max_length: int = MAX_LENGTH,
    extra: Path | None = None,
) -> list[str]:
    """Return the scripts to sweep, shortest first.

    Args:
        root: Repository root holding the replay inventory.
        seeds: How many fuzzer seeds to generate bodies from.
        iterations: Bodies to request per seed.
        max_length: Drop scripts longer than this, recorded as well as generated; a witness has to
            stay reviewable, and a body no operator would paste into a witness record is one the
            sweep cannot report an answer with. Recorded drops are counted on stderr, since the
            recorded half is the shapes the scanner was built against.
        extra: Optional JSON file holding a list of hand-authored candidates.

    Returns:
        Deduplicated scripts ordered by length.

    Raises:
        ValueError: If the replay inventory carries no recorded scripts, or the extra file cannot
            be read, or is not a list of scripts, or holds a candidate the length filter would
            drop, since a sweep that never scanned it prints the same nothing as one that scanned
            it and reached no guard.
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
    recorded = {entry["source"] for entry in entries}
    corpus.update(recorded)
    for seed in range(seeds):
        for case in fuzz_shell_taint.generate(random.Random(seed), iterations):
            corpus.add(case.script)
    if extra is not None:
        try:
            candidates = json.loads(extra.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            # The one input on this path the operator wrote themselves is the one a bare traceback
            # names worst: a decoder error reports a line and column of a file the message never
            # names, and a missing path reports neither the option that read it nor what it holds.
            # Every other read here refuses with both, and the check below already validates this
            # file, only after the read has already had to succeed.
            message = (
                f"{extra} could not be read as the JSON list of scripts --extra takes ({error!r})"
            )
            raise ValueError(message) from error
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
    # A recorded script the filter removes is reported for the reason the refusal above gives for
    # an authored one: the recorded half is the shapes the scanner was built against, and one
    # dropped in silence leaves a sweep that never scanned it printing what a sweep that scanned it
    # and reached no guard prints. Counted rather than refused, and rather than exempted from the
    # cap the way an authored candidate cannot be: the inventory records bodies far past any
    # reviewable witness, and a body past the cap loses the (length, rank) tie-break to anything
    # shorter reaching the same guard, so admitting one can only add a row no operator would paste.
    # The one entry the current inventory drops is 52,798 characters and reaches ten origins that
    # the capped corpus already reaches with witnesses of 6 to 28 characters.
    dropped = sum(1 for script in recorded if len(script) > max_length)
    if dropped:
        sys.stderr.write(
            f"dropped {dropped} recorded script(s) longer than the {max_length}-character "
            "--max-length; raise it to sweep them\n"
        )
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
    # Scored from the rows the accumulator already holds rather than from an empty map, since it is
    # documented as caller-owned: a caller sweeping a corpus in chunks into one accumulator would
    # otherwise let the first reach of the second call overwrite a better row from the first with
    # no comparison at all. A label this grid does not mint came from a grid this call cannot
    # score, and ranking it last is not neutrality but a verdict: the first entry this grid mints
    # then displaces it, so a production reach recorded by an earlier call loses to a shrunk-cap
    # reach here and the registry pins the weaker claim, which is the inversion the ordering below
    # exists to prevent. Kept instead, since a row nothing here can compare against is evidence the
    # caller already holds; a caller that wants it re-scored sweeps into a fresh accumulator.
    ranks: dict[str, int] = {}
    for rank, (label, _limits) in enumerate(grid):
        ranks.setdefault(label, rank)
    best: dict[str, tuple[int, int]] = {
        origin_id: (ranks.get(label, -1), len(script))
        for origin_id, (label, script) in found.items()
    }
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
            # Prefer the earliest grid entry, then the shortest script, so a witness says as
            # little about the caps as it can get away with and stays readable within that: the
            # grid puts production first and orders each cap's values least-shrunk first, and a
            # guard authored input reaches under production caps is strictly stronger evidence
            # than the same guard reached under a shrunk one. Keyed the other way round, a short
            # script under a shrunk cap discards a production reach the run had, and the registry
            # then pins the weaker claim, which is a resource bound rather than a reachable shape.
            key = (rank, len(script))
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

    A module the tree no longer holds is skipped rather than refused. The surface unions the
    checker's hand-kept allowlist with what it discovers, so an entry that names a module a
    contributor has just moved outlives the file by exactly the edit that adds the new path to the
    allowlist. That module contributes no frames, and no origins either, since the inventory a
    trace filters against is discovered from the same tree, so nothing is intersected away by
    passing over it. Refused instead, it would abort every trace during a guard move, with a
    message diagnosing a deleted module as a half-written one.

    Args:
        root: Repository root the guarded modules are read from.

    Returns:
        Every spelling of a guarded module's filename a traced frame may carry.

    Raises:
        ValueError: If a guarded module the tree holds cannot be imported, since tracing the rest
            would report a partial reach as the whole one, and the module's own traceback says
            nothing about why the tool stopped.
    """
    names: set[str] = set()
    for module in guarded_modules(root):
        if not (root / module).exists():
            continue
        # Derived from the recorded path rather than a hard-coded package prefix, so a guarded
        # module that moves within the tree still resolves to the module actually imported.
        parts = Path(module).with_suffix("").parts[1:]
        # A package initializer names the package. Imported as `package.__init__` CPython builds a
        # second module object and runs the initializer a second time, so a re-export of the guard
        # protocol from it, which is what puts it on the discovered surface, would abort every run
        # over a package layout change that has nothing to do with the guards being searched for.
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        dotted = ".".join(parts)
        try:
            imported = importlib.import_module(dotted)
        except IMPORT_FAILURES as error:
            # The gate reads the guard package as inert source text and discovers any module in it
            # that mentions the protocol, one still being written included. That is exactly when a
            # trace is worth running, so the refusal names the module and says the trace could not
            # run, rather than handing back a half-written module's own traceback.
            message = (
                f"{module} could not be imported to record its frames ({error!r}); the traced "
                "surface is discovered from the source tree, so a module still being written "
                "reaches this tool, and tracing the rest would report a partial reach as the "
                "whole one"
            )
            raise ValueError(message) from error
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
        Source text for the literal, in ASCII, quoted the way `ruff format` would leave it.
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
    encoded = "".join(_escaped(character) for character in json.dumps(text, ensure_ascii=False))
    # A double-quoted literal that escapes a quote it could have avoided is rewritten by
    # `ruff format`, which the pre-commit hook runs, so a row emitted that way is rewritten the
    # moment it is pasted for the same reason an over-wide one is: paste-ready means the hooks
    # leave it alone. The formatter's rule is to switch quoting only when it removes every escape,
    # which is exactly a script carrying a double quote and no apostrophe. Replayed here rather
    # than left to the hook so the row a sweep prints is the row the registry ends up holding.
    #
    # Only the escaped quotes are unescaped: json doubles a backslash, so every remaining `\"` in
    # the body is one Python spells raw under the new quoting, and no apostrophe is present to
    # need escaping in its place. Decided per part, since the formatter quotes each piece of an
    # implicit concatenation on its own.
    if '"' in text and "'" not in text:
        return "'" + encoded[1:-1].replace('\\"', '"') + "'"
    return encoded


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


def _limits_lines(field: str, label: str) -> list[str]:
    """Return the `limits=` row lines, wrapped to the repository's line limit.

    The literals either side of it are wrapped for the reason `_literal_lines` records, and the
    caps line is the one row line wide enough to need it too: `--shrink` takes any non-negative
    integer, and a wide one mints a label no single line can carry. `ruff format` would split the
    nested call, but only after the row has already failed the lint hook it was pasted to satisfy.
    Each wrapped call keeps a trailing comma, which is what holds the split open through a format.

    Args:
        field: `ScanLimits` keyword the caps value is constructed into.
        label: Caps label the grid minted, spelled `CapsClass(field=value)`.

    Returns:
        Complete source lines carrying the caps value.

    Raises:
        ValueError: If the caps value alone is wider than the line limit, since nothing splits an
            integer literal and a row nobody can paste is not a row.
    """
    flat = f"{ROW_INDENT}limits=ScanLimits({field}={label}),"
    if len(flat) <= LINE_LIMIT:
        return [flat]
    nested = f"{ROW_INDENT}    {field}={label},"
    if len(nested) <= LINE_LIMIT:
        return [f"{ROW_INDENT}limits=ScanLimits(", nested, f"{ROW_INDENT}),"]
    caps, _, argument = label.partition("(")
    assignment = f"{ROW_INDENT}        {argument.removesuffix(')')},"
    if len(assignment) > LINE_LIMIT:
        message = (
            f"{label!r} is wider than the {LINE_LIMIT}-character line limit even on a line of its "
            "own, so no row carrying it can be pasted; sweep with a cap value that fits"
        )
        raise ValueError(message)
    return [
        f"{ROW_INDENT}limits=ScanLimits(",
        f"{ROW_INDENT}    {field}={caps}(",
        assignment,
        f"{ROW_INDENT}    ),",
        f"{ROW_INDENT}),",
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
            and a guessed one renders a row that parses as arithmetic rather than failing, or if
            the caps value is too wide to render within the line limit at all.
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
            lines.extend(_limits_lines(field, label))
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

    The options this converts size the corpus, and Python spells a negative size as an empty one
    rather than as an error: `range(-1)` walks no grammar. Left to argparse's `int`, a mistyped
    value sweeps the recorded half alone, or nothing at all, and reports a script count that reads
    like the run that was asked for.

    Args:
        text: The value as spelled on the command line.

    Returns:
        The value as a count.

    Raises:
        ArgumentTypeError: If the value is negative, since no corpus has a negative size.
    """
    return _nonnegative(text, "is not a count of scripts; a corpus cannot be smaller than empty")


def nonnegative_length(text: str) -> int:
    """Return `text` as a script length in characters, refusing a negative one.

    Kept apart from the corpus-size converter because the refusal is the only thing an operator
    reads: a negative length filter drops every script, exactly as a negative size empties the
    corpus, but naming that a count of scripts describes another option's domain and says nothing
    about the quantity that was actually mistyped.

    Args:
        text: The value as spelled on the command line.

    Returns:
        The value as a length in characters.

    Raises:
        ArgumentTypeError: If the value is negative, since no script is shorter than empty.
    """
    return _nonnegative(text, "is not a length in characters; no script is shorter than empty")


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


def _deliver(text: str, described: str) -> int:
    """Write a run's whole result to stdout, reporting a sink that could not take it.

    The write is fallible: piping a several-minute run into `head` closes stdout part way through
    it. Raised from a sweep's rescue block that failure would replace the one the sweep is
    propagating, which is the diagnosis, and deliver no rows either, so the recovery would cost
    both. Reported instead, and answered with a failing status, since a run that could not deliver
    its result exits like one that found nothing.

    Flushed inside the same guard, because a buffered write is not delivery. A result smaller than
    the stream's buffer, which is every sweep that finds a handful of rows, reaches no syscall
    here at all: the failure would surface at interpreter shutdown, as an ignored exception on the
    final flush, long after this returned a status saying the rows were written.

    Args:
        text: The complete result, already rendered.
        described: What the result holds, completing the refusal after "could not write".

    Returns:
        Process exit status.
    """
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError as error:
        sys.stderr.write(f"could not write {described}: {error}\n")
        return 1
    return 0


def _resolved_checkout(parser: argparse.ArgumentParser) -> Path:
    """Return the checkout a run searches, reporting an unlocatable one as a usage error.

    Reported the way the reads a sweep is configured from are, and for the same reason: the refusal
    already names what went wrong and tells the operator to run against a source checkout with the
    package installed editable. Left to propagate it delivers that advice under a traceback out of
    the documented command, which reads as a broken tool rather than as a usage error, and it
    reaches every mode before any of them has done any work.

    Args:
        parser: Parser to report an unlocatable checkout through.

    Returns:
        Repository root containing the imported guard package.
    """
    try:
        return scanner_checkout()
    except ValueError as error:
        parser.error(str(error))


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
        help=(
            "how many fuzzer seeds to generate bodies from, walked from seed 0 upward, "
            f"one grammar walk each (default {SEED_COUNT})"
        ),
    )
    parser.add_argument(
        "--iterations",
        type=nonnegative_count,
        help=f"bodies to request per seed (default {ITERATIONS})",
    )
    parser.add_argument(
        "--max-length",
        type=nonnegative_length,
        help=(
            "drop scripts longer than this many characters, recorded as well as generated; "
            f"recorded drops are counted on stderr (default {MAX_LENGTH})"
        ),
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
    root = _resolved_checkout(parser)

    if arguments.trace is not None:
        reached = trace_guard_functions(arguments.trace)
        if not arguments.trace_all:
            reached &= guard_owning_functions(root)
        # Reported with the module the intersection was decided on, not the qualified name alone:
        # `_is_function_positional_parameter` is defined in both guarded modules today, so a bare
        # name leaves the operator aiming the next candidate at whichever module's guard they read
        # it as, and collapses two reaches into one row of a count this mode exists to give.
        names = sorted(reached)
        # Counted on stderr the way a sweep counts its origins. A candidate that reaches nothing
        # otherwise prints an empty stdout and exits zero, which is what a run that never traced
        # prints too, and the difference is the whole answer this mode was asked for.
        surface = "guarded-module" if arguments.trace_all else "guard-holding"
        sys.stderr.write(f"reached {len(names)} {surface} functions\n")
        # Guarded and flushed for the reason the sweep's own write is: piping a trace into `head`
        # closes stdout, and an unguarded write answers with a traceback where the sweep answers
        # with a diagnosis and a failing status.
        return _deliver(
            "".join(f"{module}:{qualname}\n" for module, qualname in names),
            f"the {len(names)} functions this trace reached",
        )

    try:
        wanted = None if arguments.all_guards else unclassified_ids(root)
        corpus = load_corpus(
            root,
            seeds=SEED_COUNT if arguments.seeds is None else arguments.seeds,
            iterations=ITERATIONS if arguments.iterations is None else arguments.iterations,
            max_length=MAX_LENGTH if arguments.max_length is None else arguments.max_length,
            extra=arguments.extra,
        )
    except ValueError as error:
        # These read the inputs a run is configured from, and each refusal already names the file
        # and what writes it. Reported the way a mistyped option is, since a traceback out of the
        # first read reads as a broken tool: the snapshot emptying is the end state AD-20 drives
        # toward, and an unreadable --extra file is something the operator wrote a moment ago.
        parser.error(str(error))
    grid = limits_grid(SHRINK if arguments.shrink is None else tuple(arguments.shrink))
    sys.stderr.write(f"sweeping {len(corpus)} scripts over {len(grid)} configurations\n")
    # Rows are buffered until the sweep ends, so the accumulator is owned here and rendered even
    # when a scan raises something no configuration was expected to raise. The failure still
    # propagates; what it no longer takes with it is every origin the run had already reached,
    # which is a multi-minute search and exactly the shape a witness hunt exists to find.
    found: Reach = {}
    status = 1
    try:
        sweep(corpus, grid, wanted=wanted, found=found)
        sys.stderr.write(f"reached {len(found)} guard origins\n")
    finally:
        # Rendering is fallible too, and from inside this block: a cap value wider than a line
        # carries no row, and `render_rows` says so with a ValueError. Raised from here it would do
        # what the write below is caught to stop it doing, replacing the failure the sweep is
        # propagating, which is the diagnosis, while delivering no rows either.
        described = f"the {len(found)} rows this run found"
        try:
            rows = render_rows(found)
        except ValueError as error:
            sys.stderr.write(f"could not render {described}: {error}\n")
        else:
            status = _deliver(rows, described)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
