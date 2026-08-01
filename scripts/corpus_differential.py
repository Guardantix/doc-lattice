#!/usr/bin/env python3
"""Replay the checkpoint corpus against two revisions and report every verdict divergence.

AD-20 leaves one residual open while the frozen-debt window is: a targeted early refusal inserted
levels above a frozen guard's function withdraws that guard while every static gate stays green.
Nothing static sees it, because a fingerprint records the immediate call site's controls, the
reachability rule follows syntactic call edges, and a frozen origin has no witness executing it.

This tool is the dynamic control for that window. It replays one fixed corpus through the public
scan path twice, once per revision of the guard package, and reports every script whose verdict
differs. Direction is irrelevant to it: an over-refusal is a divergence exactly as a new
certification is. The taint fuzzer's gate counts false certifications only, so a change that
refuses more than the base did is invisible to it by construction, and withdrawal by early refusal
is exactly that shape.

The corpus is the frozen replay inventory recorded by `scripts/checkpoint_record_scanner_inputs.py`
plus the bodies a fixed set of fuzzer seeds draws from the compositional grammar in
`scripts/fuzz_shell_taint.py`. Both halves come from the tree holding this file, so the two runs
score the same scripts and only the scanner under test differs.

Two modes, one process each, because two revisions of one package cannot be imported side by side:

    corpus_differential.py record --scanner-root PATH --out FILE
    corpus_differential.py compare --base FILE --candidate FILE
        [--acknowledged FILE] [--base-inventory FILE] [--write-acknowledgements FILE]

An intentional behavior change is acknowledged rather than silenced. An acknowledgement names the
script digest and both verdicts, so it covers exactly the transition it was written for and expires
on its own once the base carries the new behavior. A change that legitimately moves thousands of
verdicts is not transcribed by hand: `--write-acknowledgements` writes the file this comparison
would need, with every reason left empty, and an empty reason is refused when the file is read.

A verdict label carries the refusing guard's origin identifier, which is what makes a refusal that
moves to another origin a divergence rather than two refusals that look alike. Three limits follow
and are disclosed rather than closed: a withdrawal that mints exactly the identifier the deeper
guard would have returned, over exactly the scripts that guard already refuses, moves no label; the
corpus is a fixed sample, so a clean run is evidence about those scripts rather than a statement
about every input the scanner accepts; and this tool is the candidate's in both recordings, so only
the corpus floor is base-owned. AD-22 in ARCHITECTURE.md owns all three.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import random
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from types import ModuleType

_ROOT = Path(__file__).resolve().parents[1]

SUMMARY = "Replay the checkpoint corpus against two revisions and report every verdict divergence."
REPLAY_INVENTORY = "tests/fixtures/github_ci_checkpoint/replay_inventory.json"
ACKNOWLEDGEMENTS = "tests/fixtures/corpus_differential_acknowledgements.json"
SCANNER_MODULE = "doc_lattice.github_ci.shell_scanner"
SCANNER_RELATIVE = "src/doc_lattice/github_ci/shell_scanner.py"
FUZZER_MODULE = "fuzz_shell_taint"
SCHEMA = 1
SEEDS = (1, 2, 3, 4)
ITERATIONS = 5000
REPORT_LIMIT = 25

EXIT_OK = 0
EXIT_DIVERGED = 1
EXIT_REFUSED = 2

REFUSALS = (ValueError, OSError, KeyError)
"""What a refusal to compare arrives as, reported as one line rather than as a traceback.

`ValueError` covers this tool's own refusals and malformed JSON, which raises a subclass of it.
`OSError` covers an input the caller named that is not there, such as a base revision whose
worktree carries no frozen inventory. `KeyError` covers a record or an inventory missing a field
this tool reads. A field the document carries with the wrong shape is turned into a `ValueError` at
the read helpers below rather than being left to surface as some other type further in. Named
rather than caught as `Exception`, which the repository rules forbid.
"""

PROJECTION = ("invocations", "guard_id", "incomplete_reason")
"""The public scan-result surface a verdict label is derived from.

Read off the result rather than off the verdict classes so a revision that renames a verdict
variant is still scored, and checked on the first case of every replay so a revision that drops one
of these is a named refusal instead of a traceback in the middle of a twenty-thousand script run.
`replay` refuses an empty corpus, which is what makes that first case exist.
"""


@dataclasses.dataclass(frozen=True, slots=True)
class CorpusCase:
    """One script the differential scores under both revisions.

    Attributes:
        case_id: Stable name for the script within the corpus.
        digest: SHA-256 of the script, which is what an acknowledgement names.
        source: The literal Bash body handed to the public scan path.
    """

    case_id: str
    digest: str
    source: str


@dataclasses.dataclass(frozen=True, slots=True)
class Divergence:
    """One script the two revisions scored differently.

    Attributes:
        case_id: Stable name of the diverging script.
        digest: SHA-256 of the script.
        source: The literal Bash body.
        base: Verdict label the protected base produced.
        candidate: Verdict label the candidate produced.
    """

    case_id: str
    digest: str
    source: str
    base: str
    candidate: str

    @property
    def transition(self) -> tuple[str, str]:
        """Return the base and candidate labels as the transition they spell."""
        return (self.base, self.candidate)


@dataclasses.dataclass(frozen=True, slots=True)
class Acknowledgement:
    """One divergence the pull request declares intentional.

    Attributes:
        digest: SHA-256 of the script the acknowledgement covers.
        base: Verdict label the base is expected to produce.
        candidate: Verdict label the candidate is expected to produce.
        reason: Why the change is intentional, written for a reviewer.
    """

    digest: str
    base: str
    candidate: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the triple a divergence must match exactly to be acknowledged."""
        return (self.digest, self.base, self.candidate)


def digest_of(source: str) -> str:
    """Return the SHA-256 hex digest of one script.

    Args:
        source: Literal Bash body.

    Returns:
        Lowercase hex digest.
    """
    return hashlib.sha256(source.encode()).hexdigest()


def read_document(path: Path) -> dict[str, object]:
    """Read one JSON document this tool was pointed at.

    Args:
        path: Path to the document.

    Returns:
        The parsed object.

    Raises:
        ValueError: If the file does not hold a JSON object, so a hand-written file of the wrong
            shape is one refusal line rather than whatever type error its first field access
            happens to raise.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        message = f"{path} holds a JSON {type(document).__name__}, not an object"
        raise ValueError(message)
    return document


def read_entries(document: dict[str, object], key: str, path: Path) -> list[object]:
    """Read a named list out of one document.

    Args:
        document: The parsed document.
        key: Name of the list.
        path: Path the document came from, named in a refusal.

    Returns:
        The entries, each read through `read_text`.

    Raises:
        ValueError: If the value is not a list.
    """
    entries = document[key]
    if not isinstance(entries, list):
        message = f"{path} records {key} as a {type(entries).__name__}, not as a list"
        raise ValueError(message)
    return list(entries)


def read_text(entry: object, key: str, path: Path) -> str:
    """Read a named text field out of one object.

    Args:
        entry: The object carrying the field.
        key: Name of the field.
        path: Path the document came from, named in a refusal.

    Returns:
        The field's text.

    Raises:
        ValueError: If the entry is not an object, or the field is absent or is not text.
    """
    if not isinstance(entry, dict):
        message = f"{path} records a {type(entry).__name__} where an object carrying {key} belongs"
        raise ValueError(message)
    fields = {str(name): value for name, value in entry.items()}
    if key not in fields:
        message = f"{path} records an entry carrying no {key}"
        raise ValueError(message)
    value = fields[key]
    if not isinstance(value, str):
        message = f"{path} records {key} as a {type(value).__name__}, not as text"
        raise ValueError(message)
    return value


def inventory_cases(path: Path) -> list[CorpusCase]:
    """Return the frozen replay inventory as corpus cases, in file order.

    Args:
        path: Path to the recorded replay inventory.

    Returns:
        One case per inventory entry.

    Raises:
        ValueError: If the inventory carries a shape this tool cannot read, or if a recorded
            entry's digest does not match its source, since the inventory is what an
            acknowledgement is written against and a drifted entry names the wrong script.
    """
    document = read_document(path)
    cases: list[CorpusCase] = []
    for entry in read_entries(document, "entries", path):
        source = read_text(entry, "source", path)
        recorded = read_text(entry, "sha256", path)
        case_id = read_text(entry, "id", path)
        digest = digest_of(source)
        if digest != recorded:
            message = (
                f"replay inventory entry {case_id} records digest {recorded} for a source that "
                f"hashes to {digest}"
            )
            raise ValueError(message)
        cases.append(CorpusCase(case_id=case_id, digest=digest, source=source))
    return cases


def fuzz_cases(
    generate: Callable[[random.Random, int], Sequence[object]],
    seeds: Sequence[int],
    iterations: int,
) -> list[CorpusCase]:
    """Return the grammar bodies a fixed set of seeds draws, in seed then draw order.

    Args:
        generate: The fuzzer's case generator.
        seeds: Seeds to draw with. Fixed rather than random so two runs of this tool at the same
            revision score the same scripts.
        iterations: Cases requested per seed.

    Returns:
        One case per generated body.
    """
    cases: list[CorpusCase] = []
    for seed in seeds:
        for index, generated in enumerate(generate(random.Random(seed), iterations), start=1):
            source = generated.script  # ty: ignore[unresolved-attribute]
            cases.append(
                CorpusCase(
                    case_id=f"fuzz-{seed:04d}-{index:05d}",
                    digest=digest_of(source),
                    source=source,
                )
            )
    return cases


def deduplicate(cases: Iterable[CorpusCase]) -> list[CorpusCase]:
    """Return the cases with repeated scripts dropped, keeping the first name for each.

    Args:
        cases: Cases in corpus order.

    Returns:
        The first case carrying each distinct script digest.
    """
    seen: set[str] = set()
    unique: list[CorpusCase] = []
    for case in cases:
        if case.digest in seen:
            continue
        seen.add(case.digest)
        unique.append(case)
    return unique


def fuzz_generator() -> Callable[[random.Random, int], Sequence[object]]:
    """Return the grammar generator from the tree holding this file.

    Imported here rather than at module scope because the fuzzer imports the scanner, and a record
    run has to bind the scanner under test first.

    Returns:
        The fuzzer's `generate` function.
    """
    sys.path.insert(0, str(_ROOT / "scripts"))
    return importlib.import_module(FUZZER_MODULE).generate


def build_corpus(
    inventory: Path,
    *,
    generate: Callable[[random.Random, int], Sequence[object]],
    seeds: Sequence[int] = SEEDS,
    iterations: int = ITERATIONS,
) -> list[CorpusCase]:
    """Return the full corpus: the frozen inventory first, then the generated bodies.

    Args:
        inventory: Path to the recorded replay inventory.
        generate: The fuzzer's case generator.
        seeds: Seeds to draw generated bodies with.
        iterations: Cases requested per seed.

    Returns:
        The deduplicated corpus in a deterministic order.
    """
    return deduplicate(
        [*inventory_cases(inventory), *fuzz_cases(generate, seeds, iterations)],
    )


def corpus_digest(cases: Sequence[CorpusCase]) -> str:
    """Return one digest over the corpus contents and their order.

    Args:
        cases: The corpus in replay order.

    Returns:
        Lowercase hex digest binding every script digest and its position.
    """
    joined = "\n".join(f"{case.case_id}\t{case.digest}" for case in cases)
    return hashlib.sha256(joined.encode()).hexdigest()


def load_scanner(root: Path) -> ModuleType:
    """Import the CI shell scanner from one checkout and prove that is where it came from.

    Args:
        root: Repository root whose `src` tree holds the guard package under test.

    Returns:
        The imported scanner module.

    Raises:
        ValueError: If the root holds no scanner source, or if the import resolved somewhere else.
            An installed copy of the distribution resolves ahead of a path entry under some
            layouts, and scoring the wrong revision twice reports a clean differential for a change
            nothing replayed.
    """
    src = (root / "src").resolve()
    if not (root / SCANNER_RELATIVE).exists():
        message = f"{root} holds no {SCANNER_RELATIVE} to replay the corpus against"
        raise ValueError(message)
    sys.path.insert(0, str(src))
    module = importlib.import_module(SCANNER_MODULE)
    source = module.__file__
    if source is None or not Path(source).resolve().is_relative_to(src):
        message = f"{SCANNER_MODULE} resolved to {source}, which is not under {src}"
        raise ValueError(message)
    return module


def check_projection(result: object) -> None:
    """Fail before a replay starts when the result surface a label reads is not there.

    Args:
        result: One scan result from the revision under test.

    Raises:
        ValueError: If the revision's result omits part of the public projection.
    """
    missing = [name for name in PROJECTION if not hasattr(result, name)]
    if missing:
        message = (
            f"the scan result of this revision omits {', '.join(missing)}, so its verdicts cannot "
            "be compared with another revision's"
        )
        raise ValueError(message)


def invocation_label(invocation: object) -> str:
    """Return one certified invocation as stable text.

    Encoded as JSON rather than joined on a separator, so a part that itself carries the separator
    cannot spell the same label as a different invocation and hide a transition inside it.

    Args:
        invocation: One entry of the result's invocation tuple.

    Returns:
        Text naming the invocation's parts.
    """
    parts = list(invocation) if isinstance(invocation, tuple) else [invocation]
    return json.dumps([str(part) for part in parts])


def verdict_label(result: object) -> str:
    """Return the comparable verdict label for one scan result.

    Read through the result's public projections rather than through the verdict classes, so the
    label does not depend on which variant names a revision spells.

    Args:
        result: One scan result.

    Returns:
        `guard:<origin>` for a fail-closed refusal, `marker-detected` for the analysis's own
        refusal, and `certified[...]` with the invocations it certified otherwise.
    """
    guard_id = result.guard_id  # ty: ignore[unresolved-attribute]
    if guard_id is not None:
        return f"guard:{guard_id}"
    if result.incomplete_reason is not None:  # ty: ignore[unresolved-attribute]
        return "marker-detected"
    invocations = result.invocations  # ty: ignore[unresolved-attribute]
    return "certified[" + ",".join(invocation_label(item) for item in invocations) + "]"


def replay(cases: Sequence[CorpusCase], scan: Callable[[str], object]) -> list[dict[str, str]]:
    """Score every corpus case through one revision's public scan path.

    Args:
        cases: The corpus in replay order.
        scan: The revision's public entry point.

    Returns:
        One record per case, carrying its name, digest, source and verdict label.

    Raises:
        ValueError: If the corpus is empty. A run that scored nothing would report no divergence
            for a revision nothing was replayed against, and would never reach the projection
            check that the first case carries.
    """
    if not cases:
        message = "the corpus holds no scripts, so this revision was not replayed against anything"
        raise ValueError(message)
    scored: list[dict[str, str]] = []
    for index, case in enumerate(cases):
        result = scan(case.source)
        if index == 0:
            check_projection(result)
        scored.append(
            {
                "id": case.case_id,
                "sha256": case.digest,
                "source": case.source,
                "verdict": verdict_label(result),
            }
        )
    return scored


def parse_seeds(text: str) -> tuple[int, ...]:
    """Return the seed list a command line spells.

    Args:
        text: Comma-separated seeds, empty for none.

    Returns:
        The seeds in the order given.
    """
    return tuple(int(part) for part in text.split(",") if part.strip())


def record(
    scanner_root: Path,
    inventory: Path,
    *,
    seeds: Sequence[int],
    iterations: int,
) -> dict[str, object]:
    """Replay the corpus against one revision and return the record to write.

    Args:
        scanner_root: Repository root whose guard package is scored.
        inventory: Path to the frozen replay inventory.
        seeds: Seeds to draw generated bodies with.
        iterations: Cases requested per seed.

    Returns:
        The verdict record for that revision.
    """
    scanner = load_scanner(scanner_root)
    corpus = build_corpus(
        inventory,
        generate=fuzz_generator(),
        seeds=seeds,
        iterations=iterations,
    )
    return {
        "schema": SCHEMA,
        "scanner_source": str(Path(scanner.__file__ or "").resolve()),
        "seeds": list(seeds),
        "iterations": iterations,
        "corpus_sha256": corpus_digest(corpus),
        "count": len(corpus),
        "cases": replay(corpus, scanner.scan_doc_lattice_invocations),
    }


def load_record(path: Path) -> dict[str, object]:
    """Read one verdict record and reject a schema this tool does not know.

    Args:
        path: Path to a record written by `record`.

    Returns:
        The parsed record.

    Raises:
        ValueError: If the record carries another schema version, or a shape this tool cannot read.
    """
    document = read_document(path)
    if document.get("schema") != SCHEMA:
        message = f"{path} carries schema {document.get('schema')}, not {SCHEMA}"
        raise ValueError(message)
    read_text(document, "corpus_sha256", path)
    for case in read_entries(document, "cases", path):
        for field in ("id", "sha256", "source", "verdict"):
            read_text(case, field, path)
    return document


def load_acknowledgements(path: Path) -> list[Acknowledgement]:
    """Read the declared intentional divergences.

    Args:
        path: Path to the acknowledgements file.

    Returns:
        One entry per acknowledgement, empty when the file declares none.

    Raises:
        ValueError: If the file carries a shape this tool cannot read, or if an entry carries no
            reason. An acknowledgement is what a reviewer reads instead of the divergence, so one
            without a reason silences rather than declares. That is also what keeps the file
            `--write-acknowledgements` emits from passing a comparison before anybody wrote it.
    """
    document = read_document(path)
    entries: list[Acknowledgement] = []
    for entry in read_entries(document, "acknowledgements", path):
        digest = read_text(entry, "sha256", path)
        reason = read_text(entry, "reason", path).strip()
        if not reason:
            message = f"acknowledgement for {digest} carries no reason"
            raise ValueError(message)
        entries.append(
            Acknowledgement(
                digest=digest,
                base=read_text(entry, "base_verdict", path),
                candidate=read_text(entry, "candidate_verdict", path),
                reason=reason,
            )
        )
    return entries


def align(base: dict[str, object], candidate: dict[str, object]) -> None:
    """Refuse two records that did not score the same corpus.

    Args:
        base: The protected base's record.
        candidate: The candidate's record.

    Raises:
        ValueError: If the corpus digests differ, since a divergence count over two different
            corpora reports neither the behavior change nor its absence.
    """
    if base["corpus_sha256"] != candidate["corpus_sha256"]:
        message = (
            "the two records scored different corpora "
            f"({base['corpus_sha256']} against {candidate['corpus_sha256']}), so no divergence "
            "between them is attributable to the scanner"
        )
        raise ValueError(message)


def check_corpus_retained(candidate: dict[str, object], base_inventory: Path) -> None:
    """Refuse a candidate corpus that dropped a script the base revision froze.

    The tool and the corpus are both candidate-owned, which is what keeps the two runs scoring the
    same scripts. Shrinking the frozen inventory would therefore make a divergence disappear rather
    than be reported, so the base's own copy of the inventory is what decides the floor.

    Args:
        candidate: The candidate's record.
        base_inventory: Path to the base revision's replay inventory.

    Raises:
        ValueError: If any script the base froze is missing from the scored corpus.
    """
    scored = {case["sha256"] for case in candidate["cases"]}  # ty: ignore[not-iterable]
    document = read_document(base_inventory)
    entries = read_entries(document, "entries", base_inventory)
    missing = [
        read_text(entry, "id", base_inventory)
        for entry in entries
        if read_text(entry, "sha256", base_inventory) not in scored
    ]
    if missing:
        message = (
            f"{len(missing)} script(s) the base revision froze were not replayed, starting with "
            f"{', '.join(missing[:5])}; the frozen corpus may grow but not shrink"
        )
        raise ValueError(message)


def divergences(base: dict[str, object], candidate: dict[str, object]) -> list[Divergence]:
    """Return every case the two revisions scored differently.

    Args:
        base: The protected base's record.
        candidate: The candidate's record.

    Returns:
        One entry per diverging case, in corpus order.
    """
    base_by_digest = {case["sha256"]: case for case in base["cases"]}  # ty: ignore[not-iterable]
    found: list[Divergence] = []
    for case in candidate["cases"]:  # ty: ignore[not-iterable]
        scored = base_by_digest.get(case["sha256"])
        if scored is None or scored["verdict"] == case["verdict"]:
            continue
        found.append(
            Divergence(
                case_id=case["id"],
                digest=case["sha256"],
                source=case["source"],
                base=scored["verdict"],
                candidate=case["verdict"],
            )
        )
    return found


def transition_counts(found: Sequence[Divergence]) -> list[tuple[tuple[str, str], int]]:
    """Return how many scripts each verdict transition covers, most frequent first.

    A targeted early refusal shows up here rather than in any single row: it moves every script
    carrying the targeted shape to one guard identifier at once.

    Args:
        found: The divergences.

    Returns:
        Transition and count pairs, ordered by descending count then by transition.
    """
    counts: dict[tuple[str, str], int] = {}
    for divergence in found:
        counts[divergence.transition] = counts.get(divergence.transition, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def report(
    found: Sequence[Divergence],
    acknowledged: Sequence[Acknowledgement],
    *,
    limit: int = REPORT_LIMIT,
) -> tuple[list[Divergence], list[Acknowledgement]]:
    """Print the divergence report and return what it leaves unacknowledged.

    Args:
        found: Every divergence between the two records.
        acknowledged: The declared intentional divergences.
        limit: How many individual rows to print per section.

    Returns:
        The unacknowledged divergences and the acknowledgements nothing matched.
    """
    covered = {entry.key for entry in acknowledged}
    unacknowledged = [
        divergence
        for divergence in found
        if (divergence.digest, divergence.base, divergence.candidate) not in covered
    ]
    observed = {(divergence.digest, divergence.base, divergence.candidate) for divergence in found}
    unmatched = [entry for entry in acknowledged if entry.key not in observed]

    print(f"corpus divergences: {len(found)} ({len(unacknowledged)} unacknowledged)")
    for transition, count in transition_counts(found):
        print(f"  {count:>6}  {transition[0]} -> {transition[1]}")
    for divergence in unacknowledged[:limit]:
        print(f"  {divergence.case_id} {divergence.digest}")
        print(f"    base:      {divergence.base}")
        print(f"    candidate: {divergence.candidate}")
        print(f"    script:    {divergence.source!r}")
    if len(unacknowledged) > limit:
        print(f"  ... and {len(unacknowledged) - limit} more unacknowledged divergence(s)")
    for entry in unmatched[:limit]:
        print(f"  stale acknowledgement, nothing diverged this way: {entry.digest}")
        print(f"    reason on file: {entry.reason}")
    return unacknowledged, unmatched


def acknowledgement_document(
    found: Sequence[Divergence],
    acknowledged: Sequence[Acknowledgement],
) -> dict[str, object]:
    """Return the acknowledgements file this comparison would need.

    A scanner fix that legitimately moves thousands of verdicts is not transcribed by hand, and a
    gate that is impractical to satisfy for an intended change is a gate that gets switched off.
    This writes the entries instead, keeping the reason already on file for a transition that has
    one and leaving the rest empty for the author to write. An empty reason is refused when the
    file is read, so nothing is acknowledged until somebody says why. Transitions the comparison
    did not report are dropped, which is how a stale entry leaves the file.

    Args:
        found: Every divergence between the two records.
        acknowledged: The declared intentional divergences.

    Returns:
        The document to write.
    """
    reasons = {entry.key: entry.reason for entry in acknowledged}
    return {
        "acknowledgements": [
            {
                "sha256": divergence.digest,
                "base_verdict": divergence.base,
                "candidate_verdict": divergence.candidate,
                "reason": reasons.get(
                    (divergence.digest, divergence.base, divergence.candidate), ""
                ),
            }
            for divergence in found
        ]
    }


def _record_command(args: argparse.Namespace) -> int:
    """Run the record mode and write its verdict record.

    Args:
        args: Parsed command line.

    Returns:
        Process exit status.
    """
    document = record(
        Path(args.scanner_root),
        Path(args.inventory),
        seeds=parse_seeds(args.seeds),
        iterations=args.iterations,
    )
    Path(args.out).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"scored {document['count']} script(s) against {document['scanner_source']}")
    return EXIT_OK


def _compare_command(args: argparse.Namespace) -> int:
    """Run the compare mode and report unacknowledged divergence.

    Args:
        args: Parsed command line.

    Returns:
        Process exit status.
    """
    base = load_record(Path(args.base))
    candidate = load_record(Path(args.candidate))
    align(base, candidate)
    if args.base_inventory:
        check_corpus_retained(candidate, Path(args.base_inventory))
    acknowledged = load_acknowledgements(Path(args.acknowledged)) if args.acknowledged else []
    found = divergences(base, candidate)
    unacknowledged, _unmatched = report(found, acknowledged, limit=args.report_limit)
    if args.write_acknowledgements:
        draft = Path(args.write_acknowledgements)
        draft.write_text(
            json.dumps(acknowledgement_document(found, acknowledged), indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"wrote {len(found)} acknowledgement draft(s) to {draft}; each reason is left empty "
            "for the author to write, and an empty reason is refused on read"
        )
    if unacknowledged:
        print(
            "the candidate scores the frozen corpus differently from the protected base; "
            "acknowledge each intentional transition in "
            f"{ACKNOWLEDGEMENTS} or restore the base behavior",
            file=sys.stderr,
        )
        return EXIT_DIVERGED
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Return the command line parser.

    Returns:
        Parser carrying the record and compare modes.
    """
    parser = argparse.ArgumentParser(description=SUMMARY)
    modes = parser.add_subparsers(dest="mode", required=True)

    recorder = modes.add_parser("record", help="score the corpus against one revision")
    recorder.add_argument(
        "--scanner-root",
        required=True,
        help="checkout holding the guard package",
    )
    recorder.add_argument("--out", required=True, help="where to write the verdict record")
    recorder.add_argument(
        "--inventory",
        default=str(_ROOT / REPLAY_INVENTORY),
        help="frozen replay inventory to score",
    )
    recorder.add_argument("--seeds", default=",".join(str(seed) for seed in SEEDS))
    recorder.add_argument("--iterations", type=int, default=ITERATIONS)
    recorder.set_defaults(handler=_record_command)

    comparer = modes.add_parser("compare", help="report divergence between two verdict records")
    comparer.add_argument("--base", required=True, help="the protected base's verdict record")
    comparer.add_argument("--candidate", required=True, help="the candidate's verdict record")
    comparer.add_argument(
        "--acknowledged",
        default="",
        help="declared intentional divergences",
    )
    comparer.add_argument(
        "--base-inventory",
        default="",
        help="the base revision's replay inventory, which the scored corpus may not drop",
    )
    comparer.add_argument(
        "--write-acknowledgements",
        default="",
        help="write the acknowledgements this comparison would need, with the reasons left empty",
    )
    comparer.add_argument("--report-limit", type=int, default=REPORT_LIMIT)
    comparer.set_defaults(handler=_compare_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one mode of the corpus differential.

    Args:
        argv: Command line arguments, defaulting to this process's.

    Returns:
        Process exit status: 0 clean, 1 unacknowledged divergence, 2 a refusal to compare.
    """
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except REFUSALS as error:
        print(f"corpus differential refused: {error}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
