#!/usr/bin/env python3
"""Differential fuzzer comparing CI shell taint verdicts against real Bash execution.

The scanner's contract is one-directional: if Bash would execute a command whose name matches the
authored ``doc[-_.]+lattice`` marker, the scanner must refuse the body. This tool generates shell
bodies from a compositional grammar, executes each under real Bash with tracing enabled, and
reports every body Bash executes the marker in that the scanner nonetheless certified.

Over-refusals, where the scanner refuses a body Bash never runs the marker in, are reported
separately. They are a usability signal rather than a soundness failure.

Execution detection reads Bash's own xtrace on a dedicated descriptor, so an attempted marker
command counts even when no such executable exists, and trace output never mixes with a command's
stderr.
"""

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from doc_lattice.github_ci.shell_scanner import scan_doc_lattice_invocations

_MARKER_COMMAND = re.compile(r"(?:^|/)doc[-_.]+lattice$", re.IGNORECASE)
_TRACE_PREFIX = re.compile(r"^\++\s*")
_ASSIGNMENT_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_DEFAULT_ITERATIONS = 2_000
_DEFAULT_TIMEOUT_SECONDS = 10
_DEFAULT_JOBS = 8
_TRACE_BYTE_LIMIT = 512_000
_SHRINK_DIMENSIONS = ("wrapper", "carrier", "producer", "sink")
# Position of each grammar dimension in Recipe.signature(), which is what the baseline file
# records and what report() breaks down. Mirrored by signature() itself; a reorder there has
# to be a reorder here.
_SIGNATURE_DIMENSIONS = ("producer", "carrier", "sink", "wrapper")
# Separators the generated fragment pairs can compose, so a stub exists for every reachable name.
_MARKER_SEPARATORS = ("-", "_", ".", "--")

# Fragment pairs whose concatenation matches the marker, and pairs whose concatenation does not.
# Both are generated so the fuzzer exercises refusal and certification in the same run.
_MARKER_FRAGMENTS = (
    ("doc-", "lattice"),
    ("doc", "-lattice"),
    ("doc_", "lattice"),
    ("doc.", "lattice"),
    ("do", "c-lattice"),
    ("doc-l", "attice"),
    ("doc--", "lattice"),
)
_INERT_FRAGMENTS = (
    ("saf", "e"),
    ("doc", "umentation"),
    ("lat", "tice"),
    ("hello", "world"),
)

# Each template binds fragment @A@ into variable @V@. @F@ is a scratch file, @N@ a per-case tag.
_PRODUCERS: tuple[tuple[str, str], ...] = (
    ("plain", "@V@=@A@"),
    ("quoted", '@V@="@A@"'),
    ("declare", "declare @V@=@A@"),
    ("export", "export @V@=@A@"),
    ("readonly", "readonly @V@=@A@"),
    ("typeset", "typeset @V@=@A@"),
    ("declare-dynamic", '@V@x=@A@; declare @V@="$@V@x"'),
    ("cmdsub-printf", "@V@=$(printf %s @A@)"),
    ("cmdsub-echo", "@V@=$(echo @A@)"),
    ("file-cat", "printf %s @A@ > @F@; @V@=$(cat @F@)"),
    ("file-read", "printf %s @A@ > @F@; @V@=$(< @F@)"),
    ("read-herestring", "read @V@ <<< @A@"),
    ("append", "@V@=; @V@+=@A@"),
    ("eval-assign", "eval '@V@=@A@'"),
    ("pipe-cmdsub", "@V@=$(printf %s @A@ | cat)"),
    ("positional", 'set -- @A@; @V@="$1"'),
    ("function-stdout", "p@N@(){ printf %s @A@; }; @V@=$(p@N@)"),
    ("array-element", "a@N@=(@A@); @V@=${a@N@[0]}"),
)

# Each template yields the expression text spliced into a sink, and may emit setup before it.
_CARRIERS: tuple[tuple[str, str, str], ...] = (
    ("direct", "", "$@V@"),
    ("braced", "", "${@V@}"),
    ("strip-prefix", "", "${@V@#zz}"),
    ("strip-suffix", "", "${@V@%zz}"),
    ("substitute", "", "${@V@/zz/yy}"),
    ("default", "", "${@V@:-zz}"),
    ("offset", "", "${@V@:0}"),
    ("alternate", "", "${@V@:+$@V@}"),
    ("via-file", 'printf %s "$@V@" > @F2@', "$(cat @F2@)"),
    ("via-pipe", '@W@=$(printf %s "$@V@" | cat)', "$@W@"),
    ("via-function", 'g@N@(){ printf %s "$1"; }; @W@=$(g@N@ "$@V@")', "$@W@"),
    ("via-eval", "eval '@W@='\"$@V@\"", "$@W@"),
    ("via-append", '@W@=; @W@+="$@V@"', "$@W@"),
    ("via-procsub", '@W@=$(cat <(printf %s "$@V@"))', "$@W@"),
)

# Each template consumes expression @E@ and trailing fragment @B@ at an execution sink.
_SINKS: tuple[tuple[str, str], ...] = (
    ("eval", 'eval "@E@@B@ reconcile"'),
    ("bash-c", 'bash -c "@E@@B@ reconcile"'),
    ("sh-c", 'sh -c "@E@@B@ reconcile"'),
    ("pipe-bash", "printf '%s\\n' \"@E@@B@ reconcile\" | bash"),
    ("script-file", "printf '%s\\n' \"@E@@B@ reconcile\" > @F3@; bash @F3@"),
    ("source-file", "printf '%s\\n' \"@E@@B@ reconcile\" > @F3@; source @F3@"),
    ("herestring", 'bash <<< "@E@@B@ reconcile"'),
    ("heredoc", "bash <<EOF@N@\n@E@@B@ reconcile\nEOF@N@"),
    ("head-position", '"@E@@B@" reconcile'),
    ("timeout-wrapper", 'timeout 60 bash -c "@E@@B@ reconcile"'),
    ("nested-cmdsub", 'eval "$(printf \'%s\' "@E@@B@ reconcile")"'),
    ("stdin-redirect", "printf '%s\\n' \"@E@@B@ reconcile\" > @F3@; bash < @F3@"),
)

# Each template wraps the assembled body, which is spliced at @BODY@.
_WRAPPERS: tuple[tuple[str, str], ...] = (
    ("none", "@BODY@"),
    ("for-newline", "for w@N@ in a; do\n@BODY@\ndone"),
    ("for-sameline", "for w@N@ in a; do @BODY@; done"),
    ("if-then", "if true; then\n@BODY@\nfi"),
    ("if-then-sameline", "if true; then @BODY@; fi"),
    ("if-else", "if false; then :; else @BODY@; fi"),
    ("while", "while :; do @BODY@; break; done"),
    ("function", "w@N@(){\n@BODY@\n}; w@N@"),
    ("function-arg", "w@N@(){\n@BODY@\n}; w@N@ doc-"),
    ("subshell", "(\n@BODY@\n)"),
    ("brace-group", "{\n@BODY@\n}"),
    ("case-arm", "case x in x)\n@BODY@\n;; esac"),
    ("until", "until false; do @BODY@; break; done"),
)


@dataclass(frozen=True, slots=True)
class Recipe:
    """The grammar choices that produced one generated body."""

    producer: str
    carrier: str
    sink: str
    wrapper: str
    fragments: str
    marker_bearing: bool

    def signature(self) -> tuple[str, ...]:
        """Return the dimension tuple used for deduplication and shrinking."""
        # Explicit field access for speed; `_SIGNATURE_DIMENSIONS` mirrors this order by name.
        return (self.producer, self.carrier, self.sink, self.wrapper)


@dataclass(frozen=True, slots=True)
class Case:
    """One generated shell body and the recipe that produced it."""

    script: str
    recipe: Recipe


@dataclass(frozen=True, slots=True)
class Outcome:
    """The differential result for one case."""

    case: Case
    certified: bool
    executed: bool
    reason: str | None
    timed_out: bool

    @property
    def false_certification(self) -> bool:
        """Return whether Bash ran the marker in a body the scanner certified."""
        return self.certified and self.executed

    @property
    def over_refusal(self) -> bool:
        """Return whether the scanner refused a body Bash never ran the marker in."""
        return not self.certified and not self.executed and not self.timed_out


def _substitute(template: str, tag: int, fragment_a: str, fragment_b: str) -> str:
    """Expand the placeholder alphabet used by the grammar templates.

    Args:
        template: Template text containing ``@NAME@`` placeholders.
        tag: Per-case integer keeping generated names and heredoc delimiters distinct.
        fragment_a: The fragment bound by the producer.
        fragment_b: The fragment appended at the sink.

    Returns:
        The template with every placeholder replaced.
    """
    replacements = {
        "@V@": f"v{tag}",
        "@W@": f"w{tag}b",
        "@F@": f"f{tag}.txt",
        "@F2@": f"f{tag}b.txt",
        "@F3@": f"f{tag}c.sh",
        "@N@": str(tag),
        "@A@": fragment_a,
        "@B@": fragment_b,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def build_case(recipe: Recipe, tag: int) -> Case:
    """Assemble one shell body from a recipe.

    Args:
        recipe: The grammar choices to realize.
        tag: Per-case integer keeping generated names distinct.

    Returns:
        The generated case.
    """
    fragment_a, fragment_b = _lookup_fragments(recipe.fragments)
    producer = dict(_PRODUCERS)[recipe.producer]
    carrier_setup, carrier_expression = {
        name: (setup, expression) for name, setup, expression in _CARRIERS
    }[recipe.carrier]
    sink = dict(_SINKS)[recipe.sink]
    wrapper = dict(_WRAPPERS)[recipe.wrapper]

    lines = [_substitute(producer, tag, fragment_a, fragment_b)]
    if carrier_setup:
        lines.append(_substitute(carrier_setup, tag, fragment_a, fragment_b))
    expression = _substitute(carrier_expression, tag, fragment_a, fragment_b)
    lines.append(_substitute(sink.replace("@E@", expression), tag, fragment_a, fragment_b))
    body = "\n".join(lines)
    script = _substitute(wrapper.replace("@BODY@", body), tag, fragment_a, fragment_b)
    return Case(script=script, recipe=recipe)


def _lookup_fragments(key: str) -> tuple[str, str]:
    """Return the fragment pair named by a recipe key."""
    for pair in (*_MARKER_FRAGMENTS, *_INERT_FRAGMENTS):
        if _fragment_key(pair) == key:
            return pair
    message = f"unknown fragment pair {key}"
    raise ValueError(message)


def _fragment_key(pair: tuple[str, str]) -> str:
    """Return the stable name of a fragment pair."""
    return f"{pair[0]}|{pair[1]}"


def generate(rng: random.Random, count: int) -> list[Case]:
    """Draw distinct recipes from the grammar and realize them.

    Args:
        rng: Seeded generator so a run is reproducible from its seed.
        count: Number of cases requested.

    Returns:
        The generated cases, deduplicated by recipe.
    """
    fragment_keys = [(_fragment_key(pair), True) for pair in _MARKER_FRAGMENTS] + [
        (_fragment_key(pair), False) for pair in _INERT_FRAGMENTS
    ]
    seen: set[Recipe] = set()
    cases: list[Case] = []
    attempts = 0
    budget = count * 20
    while len(cases) < count and attempts < budget:
        attempts += 1
        fragments, marker_bearing = rng.choice(fragment_keys)
        recipe = Recipe(
            producer=rng.choice(_PRODUCERS)[0],
            carrier=rng.choice(_CARRIERS)[0],
            sink=rng.choice(_SINKS)[0],
            wrapper=rng.choice(_WRAPPERS)[0],
            fragments=fragments,
            marker_bearing=marker_bearing,
        )
        if recipe in seen:
            continue
        seen.add(recipe)
        cases.append(build_case(recipe, tag=len(cases)))
    return cases


def _trace_runs_marker(trace: str) -> bool:
    """Return whether an xtrace log shows a marker-named command being executed.

    Bash writes one trace line per executed simple command, after expansion, so a composed name is
    visible even when no such executable exists. Leading assignment words are skipped because a
    temporary-environment prefix is not the command being run.

    Args:
        trace: Captured xtrace output.

    Returns:
        Whether any traced command's name matches the marker.
    """
    for raw_line in trace.splitlines():
        line = _TRACE_PREFIX.sub("", raw_line)
        if not line:
            continue
        words = line.split()
        for word in words:
            if _ASSIGNMENT_WORD.match(word):
                continue
            if _MARKER_COMMAND.search(word.strip("\"'")):
                return True
            break
    return False


def execute(case: Case, bash: str, timeout: float) -> tuple[bool, bool]:
    """Run one generated body under real Bash and report marker execution.

    Args:
        case: The generated case.
        bash: Path to the Bash interpreter.
        timeout: Seconds allowed before the body is killed.

    Returns:
        Whether the marker executed, and whether the body timed out.
    """
    workdir = Path(tempfile.mkdtemp(prefix="fuzz-taint-"))
    try:
        bin_dir = workdir / "bin"
        bin_dir.mkdir()
        probe = workdir / "marker-ran"
        # Two independent detectors. The stub records its own invocation, which catches execution
        # inside any child process regardless of which shell runs it. The xtrace log catches an
        # attempted marker command whose name no stub provides. Either one is sufficient.
        for separator in _MARKER_SEPARATORS:
            stub = bin_dir / f"doc{separator}lattice"
            stub.write_text('#!/bin/sh\n: > "$MARKER_PROBE"\nexit 0\n', encoding="utf-8")
            stub.chmod(0o755)
        body = workdir / "body.sh"
        body.write_text(case.script + "\n", encoding="utf-8")
        trace_path = workdir / "trace.log"
        trace_fd = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            environment = {
                "PATH": f"{bin_dir}:{os.defpath}",
                "HOME": str(workdir),
                "SHELL": bash,
                "MARKER_PROBE": str(probe),
                # Exported so a child Bash keeps tracing into the same descriptor, which a
                # ``bash -c`` or ``bash script.sh`` sink would otherwise execute untraced.
                "SHELLOPTS": "xtrace",
                "BASH_XTRACEFD": str(trace_fd),
                "LC_ALL": "C",
            }
            timed_out = False
            try:
                subprocess.run(
                    [bash, "-x", str(body)],
                    cwd=workdir,
                    env=environment,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    pass_fds=(trace_fd,),
                )
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            os.close(trace_fd)
        trace = trace_path.read_text(encoding="utf-8", errors="replace")[:_TRACE_BYTE_LIMIT]
        return probe.exists() or _trace_runs_marker(trace), timed_out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def evaluate(case: Case, bash: str, timeout: float) -> Outcome:
    """Compare the scanner verdict for one case against real Bash behavior.

    Args:
        case: The generated case.
        bash: Path to the Bash interpreter.
        timeout: Seconds allowed before the body is killed.

    Returns:
        The differential outcome.
    """
    result = scan_doc_lattice_invocations(case.script)
    executed, timed_out = execute(case, bash, timeout)
    return Outcome(
        case=case,
        certified=result.incomplete_reason is None,
        executed=executed,
        reason=result.incomplete_reason,
        timed_out=timed_out,
    )


def shrink(outcome: Outcome, bash: str, timeout: float) -> Outcome:
    """Reduce a false certification to a smaller recipe that still reproduces it.

    Each dimension is greedily replaced with its simplest alternative, keeping any replacement
    that still certifies while Bash runs the marker. This turns a report of many similar recipes
    into a small number of distinct root causes.

    Args:
        outcome: A confirmed false certification.
        bash: Path to the Bash interpreter.
        timeout: Seconds allowed per trial body.

    Returns:
        The smallest reproducing outcome found.
    """
    simplest = {
        "wrapper": "none",
        "carrier": "direct",
        "producer": "plain",
        "sink": "eval",
    }
    best = outcome
    for dimension in _SHRINK_DIMENSIONS:
        current = getattr(best.case.recipe, dimension)
        candidate_value = simplest[dimension]
        if current == candidate_value:
            continue
        recipe = _replace_dimension(best.case.recipe, dimension, candidate_value)
        trial = evaluate(build_case(recipe, tag=0), bash, timeout)
        if trial.false_certification:
            best = trial
    return best


def _replace_dimension(recipe: Recipe, dimension: str, value: str) -> Recipe:
    """Return a recipe with one grammar dimension replaced."""
    return replace(recipe, **{dimension: value})


def load_baseline(path: Path | None) -> set[tuple[str, ...]]:
    """Read known-failing signatures so a run reports only new soundness failures."""
    if path is None or not path.is_file():
        return set()
    signatures = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            signatures.add(tuple(entry.split("\t")))
    return signatures


def write_baseline(path: Path, signatures: set[tuple[str, ...]]) -> None:
    """Record failing signatures so later runs can gate on regressions only.

    The file is replaced, not merged: it ends up holding exactly the signatures this run
    reproduced, so a regeneration at a different seed or iteration count drops every accepted
    signature the previous capture found and this one did not draw. Regenerate at the seed and
    scale the baseline was captured at, which CLAUDE.md names.

    Args:
        path: Baseline file to overwrite.
        signatures: The dimension tuples this run found to certify falsely.
    """
    lines = [
        "# doc-lattice shell taint fuzzer baseline",
        "# " + "\t".join(_SIGNATURE_DIMENSIONS),
    ]
    lines.extend("\t".join(signature) for signature in sorted(signatures))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def report(outcomes: list[Outcome], baseline: set[tuple[str, ...]], *, verbose: bool) -> int:
    """Print the differential summary and return the count of new soundness failures.

    Args:
        outcomes: Every evaluated outcome.
        baseline: Known-failing signatures to exclude from the failure count.
        verbose: Whether to list over-refusals as well.

    Returns:
        The number of distinct new false-certification signatures.
    """
    failures = [outcome for outcome in outcomes if outcome.false_certification]
    over_refusals = [outcome for outcome in outcomes if outcome.over_refusal]
    timeouts = [outcome for outcome in outcomes if outcome.timed_out]
    marker_cases = [outcome for outcome in outcomes if outcome.case.recipe.marker_bearing]

    print(f"cases evaluated         {len(outcomes)}")
    print(f"  marker-bearing        {len(marker_cases)}")
    print(f"  inert                 {len(outcomes) - len(marker_cases)}")
    print(f"bash executed marker    {sum(1 for o in outcomes if o.executed)}")
    print(f"scanner refused         {sum(1 for o in outcomes if not o.certified)}")
    print(f"timeouts                {len(timeouts)}")
    print()

    unique: dict[tuple[str, ...], Outcome] = {}
    for outcome in failures:
        unique.setdefault(outcome.case.recipe.signature(), outcome)
    new = {key: value for key, value in unique.items() if key not in baseline}

    print(f"FALSE CERTIFICATIONS    {len(failures)} cases, {len(unique)} distinct recipes")
    print(f"  new vs baseline       {len(new)}")
    if unique:
        for index, dimension in enumerate(_SIGNATURE_DIMENSIONS):
            counts = Counter(signature[index] for signature in unique)
            ranked = ", ".join(f"{name}={count}" for name, count in counts.most_common(5))
            print(f"  by {dimension:9} {ranked}")
    print()

    for signature, outcome in sorted(new.items()):
        print(f"--- NEW {'/'.join(signature)}")
        for line in outcome.case.script.splitlines():
            print(f"    {line}")
    if new:
        print()

    print(f"over-refusals           {len(over_refusals)} cases")
    if verbose:
        seen: set[tuple[str, ...]] = set()
        for outcome in over_refusals:
            signature = outcome.case.recipe.signature()
            if signature in seen:
                continue
            seen.add(signature)
            print(f"--- OVER-REFUSAL {'/'.join(signature)}: {outcome.reason}")
            for line in outcome.case.script.splitlines():
                print(f"    {line}")
    return len(new)


_SELF_CHECK_CASES: tuple[tuple[str, str, bool], ...] = (
    ("parent eval", 'A=doc-; eval "${A}lattice reconcile"', True),
    ("child bash -c", 'A=doc-; bash -c "${A}lattice reconcile"', True),
    ("child script file", 'A=doc-; printf "%s\\n" "${A}lattice reconcile" > s.sh; bash s.sh', True),
    ("child pipe to bash", 'A=doc-; printf "%s\\n" "${A}lattice reconcile" | bash', True),
    ("child sh -c", 'A=doc-; sh -c "${A}lattice reconcile"', True),
    ("head position", 'A=doc-; "${A}lattice" reconcile', True),
    ("unstubbed spelling", 'A=doc---; "${A}lattice" reconcile', True),
    ("marker-free", 'A=safe; eval "${A}thing reconcile"', False),
    ("printed not executed", "echo doc-lattice reconcile", False),
    ("composed not executed", 'A=doc-; printf "%s\\n" "${A}lattice"', False),
)


def self_check(bash: str, timeout: float) -> int:
    """Verify the execution detector against cases with known Bash behavior.

    A differential fuzzer is only as trustworthy as its oracle. This checks both directions,
    including sinks that run the marker inside a child process and bodies that merely mention the
    marker without executing it.

    Args:
        bash: Path to the Bash interpreter.
        timeout: Seconds allowed per body.

    Returns:
        The number of detector failures.
    """
    placeholder = Recipe("", "", "", "", "", marker_bearing=True)
    failures = 0
    for label, script, expected in _SELF_CHECK_CASES:
        executed, _ = execute(Case(script=script, recipe=placeholder), bash, timeout)
        status = "ok  " if executed == expected else "FAIL"
        failures += int(executed != expected)
        print(f"{status} executed={executed!s:5} expected={expected!s:5}  {label}")
    print("\nharness OK" if not failures else f"\n{failures} detector failures")
    return failures


def main() -> int:
    """Run the differential fuzzer and return a process exit status."""
    parser = argparse.ArgumentParser(
        description="Differentially test CI shell taint verdicts against real Bash execution."
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=_DEFAULT_ITERATIONS,
        help="cases to request from the grammar",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="seed for the grammar; a baseline is specific to the seed it was captured at",
    )
    parser.add_argument("--jobs", type=int, default=_DEFAULT_JOBS, help="parallel Bash executions")
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help="seconds allowed per generated body",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="TSV of known-failing signatures to exclude from the failure count, and the file "
        "--write-baseline rewrites",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="replace the --baseline file with this run's failing signatures; without --baseline "
        "there is no file to write and the flag does nothing",
    )
    parser.add_argument(
        "--no-shrink",
        action="store_true",
        help="report full recipes instead of shrinking each false certification",
    )
    parser.add_argument("--verbose", action="store_true", help="list over-refusals as well")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="validate the execution oracle and exit",
    )
    arguments = parser.parse_args()

    bash = shutil.which("bash", path=os.defpath)
    if bash is None:
        print("bash is required for differential execution", file=sys.stderr)
        return 2

    if arguments.self_check:
        return 1 if self_check(bash, arguments.timeout) else 0

    rng = random.Random(arguments.seed)
    cases = generate(rng, arguments.iterations)
    print(f"generated {len(cases)} distinct recipes from seed {arguments.seed}\n")

    with ThreadPoolExecutor(max_workers=arguments.jobs) as pool:
        outcomes = list(pool.map(lambda case: evaluate(case, bash, arguments.timeout), cases))

    if not arguments.no_shrink:
        shrunk: list[Outcome] = []
        for outcome in outcomes:
            shrunk.append(
                shrink(outcome, bash, arguments.timeout) if outcome.false_certification else outcome
            )
        outcomes = shrunk

    baseline = load_baseline(arguments.baseline)
    new_failures = report(outcomes, baseline, verbose=arguments.verbose)

    if arguments.write_baseline and arguments.baseline is not None:
        signatures = {
            outcome.case.recipe.signature() for outcome in outcomes if outcome.false_certification
        }
        write_baseline(arguments.baseline, signatures)
        print(f"\nwrote {len(signatures)} signatures to {arguments.baseline}")
        return 0

    return 1 if new_failures else 0


if __name__ == "__main__":
    sys.exit(main())
