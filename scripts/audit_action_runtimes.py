#!/usr/bin/env python3
"""Report the deprecation annotations a completed workflow run's jobs carry.

When an executed action targets a runtime GitHub has deprecated, the runner attaches a warning
annotation to the check run of the job that executed it. That annotation is the only signal
naming an action whose *published* pin is current but whose *runtime* is not, so Dependabot
never produces a pull request for it. This script reads a finished run's jobs and each job's
annotations through ``gh api``, writes a job summary, and exits non-zero when any of them names
a deprecation. AD-42 in ARCHITECTURE.md records why this runs alongside Dependabot rather than
instead of it.

The script is deliberately stdlib-only and imports nothing from ``doc_lattice``, so the auditing
workflow can run it under ``uv run --no-project`` without resolving or installing the project.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

# The GitHub REST maximum, and therefore the page size that tells a caller a listing is complete:
# a page holding fewer items than this is the last one.
PAGE_SIZE = 100
# `annotation_level` values worth acting on. A `notice` is informational -- the publish job's
# attestation notice is one -- and never reports a runtime that is going away.
ACTIONABLE_LEVELS = frozenset({"warning", "failure"})
# Matched against the lowered message. Deliberately a stem rather than a Node-specific regex:
# the runner images are deprecated in the same words, and the next runtime deprecation will be
# worded differently from this one.
DEPRECATION_MARKER = "deprecat"
# The conclusion of a job the runner never started. Its check run exists and reports as
# completed, so only the conclusion separates it from a job that ran and passed.
_SKIPPED = "skipped"

# Spelled out rather than imported from `doc_lattice.constants`, which this script cannot reach
# under `--no-project`. These are the two pins `constants.py` ships to adopters, so a bump of
# either is the coupled multi-file edit RELEASING.md describes rather than a workflow edit.
_POINTER = (
    "The pins naming those actions are stale. When the action is `actions/checkout` or "
    "`astral-sh/setup-uv`, the bump is coupled across the files RELEASING.md, under "
    '"Keeping the action pins current", lists. Any other action is a workflow-only bump.'
)
_TABLE_HEADER = ("| Job | Level | Annotation |", "| --- | --- | --- |")
_NO_FINDINGS = "No deprecation annotations."
# Worded exactly as `scripts/check_action_pin_correspondence.py` words it, so the two audits read
# the same way in a log when either one's report is what failed.
_REPORT_FAILED = "the report could not be written"
# How a reporting write fails for reasons that are not the audit's answer. `OSError` is the disk
# or the path. `UnicodeEncodeError` is a stream whose encoding cannot carry the text: every field
# the summary renders -- workflow name, branch, job name, annotation message -- is upstream text,
# and a console under an ASCII encoding raises rather than writing it. It is a `ValueError`, so
# guarding `OSError` alone would let exactly the inversion this guard exists to prevent back in.
_REPORT_FAILURES = (OSError, UnicodeEncodeError)


class AuditError(RuntimeError):
    """A run, job, or annotation payload the audit cannot interpret."""


@dataclass(frozen=True)
class Run:
    """Identity of the workflow run being audited.

    Attributes:
        name: The source workflow's ``name:`` value, such as ``CI``.
        run_id: The run's numeric id.
        html_url: Web address of the run.
        event: The event that triggered the run.
        head_branch: Branch the run was dispatched against, or None when it has none.
        run_attempt: Which attempt of the run this is, counting from one.
    """

    name: str
    run_id: int
    html_url: str
    event: str
    head_branch: str | None
    run_attempt: int


@dataclass(frozen=True)
class Job:
    """One job of the run being audited.

    Attributes:
        id: The job id, which is also the id of the check run holding its annotations.
        name: The job's display name.
        html_url: Web address of the job's log.
        status: Lifecycle state, such as ``completed``.
        conclusion: Outcome once completed, or None while it is not.
    """

    id: int
    name: str
    html_url: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class Annotation:
    """One annotation the runner attached to a job's check run.

    Attributes:
        level: The ``annotation_level``, such as ``warning``.
        message: The annotation text.
        path: Repository path the annotation points at, or None when it names none.
        start_line: Line the annotation points at, or None when it names none.
    """

    level: str
    message: str
    path: str | None
    start_line: int | None


@dataclass(frozen=True)
class Finding:
    """A deprecation annotation together with the job that carried it.

    Attributes:
        job: The job whose check run holds the annotation.
        annotation: The annotation that matched.
    """

    job: Job
    annotation: Annotation


def fetch_json(path: str) -> object:
    """Return the JSON body ``gh api <path>`` prints.

    ``gh`` is preinstalled on GitHub-hosted runners and reads ``GH_TOKEN`` from the environment,
    so the workflow supplies credentials without this script handling one. Every network call in
    the script goes through here, which is what lets the tests drive it with a plain callable.

    Args:
        path: An API path such as ``/repos/OWNER/REPO/actions/runs/1``.

    Returns:
        The decoded JSON body, which callers narrow at their own boundary.

    Raises:
        AuditError: If ``gh`` fails, or succeeds but does not print decodable JSON. Exiting
            here instead would bypass ``main``'s handler, which is what renders a failure as an
            ``::error`` annotation -- and a bad token or a 404 is the likeliest failure there is.
    """
    result = subprocess.run(("gh", "api", path), check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise AuditError(result.stderr.strip() or f"gh api {path} failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AuditError(f"gh api {path} did not return JSON: {error}") from error


def _require_mapping(value: object, context: str) -> dict[str, object]:
    """Return `value` as a JSON object, or raise naming the payload that was not one."""
    if not isinstance(value, dict):
        raise AuditError(f"{context}: expected a JSON object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _require_list(value: object, context: str) -> list[object]:
    """Return `value` as a JSON array, or raise naming the payload that was not one."""
    if not isinstance(value, list):
        raise AuditError(f"{context}: expected a JSON array, got {type(value).__name__}")
    return list(value)


def _optional_str(payload: dict[str, object], key: str, context: str) -> str | None:
    """Return a string field, or None when the API rendered it as JSON null."""
    value = payload.get(key)
    if value is None or isinstance(value, str):
        return value
    raise AuditError(f"{context}: field {key!r} is not a string")


def _require_str(payload: dict[str, object], key: str, context: str) -> str:
    """Return a string field, or raise when it is absent, null, or another type."""
    value = _optional_str(payload, key, context)
    if value is None:
        raise AuditError(f"{context}: field {key!r} is missing")
    return value


def _optional_int(payload: dict[str, object], key: str, context: str) -> int | None:
    """Return an integer field, or None when the API rendered it as JSON null.

    ``bool`` is rejected explicitly because it is a subclass of ``int``, so a payload sending
    ``true`` for an id would otherwise pass through as ``1``.
    """
    value = payload.get(key)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise AuditError(f"{context}: field {key!r} is not an integer")


def _require_int(payload: dict[str, object], key: str, context: str) -> int:
    """Return an integer field, or raise when it is absent, null, or another type."""
    value = _optional_int(payload, key, context)
    if value is None:
        raise AuditError(f"{context}: field {key!r} is missing")
    return value


def parse_run(payload: object) -> Run:
    """Narrow a run payload into a `Run`.

    Args:
        payload: The decoded body of ``GET /repos/{repo}/actions/runs/{id}``.

    Returns:
        The run's identity.

    Raises:
        AuditError: If the payload is not an object carrying the fields the summary names.
    """
    run = _require_mapping(payload, "run")
    return Run(
        name=_require_str(run, "name", "run"),
        run_id=_require_int(run, "id", "run"),
        html_url=_require_str(run, "html_url", "run"),
        event=_require_str(run, "event", "run"),
        head_branch=_optional_str(run, "head_branch", "run"),
        run_attempt=_require_int(run, "run_attempt", "run"),
    )


def parse_job(payload: object) -> Job:
    """Narrow one element of a jobs page into a `Job`.

    Args:
        payload: One entry of the ``jobs`` array.

    Returns:
        The job's identity and lifecycle state.

    Raises:
        AuditError: If the entry is not an object carrying the fields the audit reads.
    """
    job = _require_mapping(payload, "job")
    return Job(
        id=_require_int(job, "id", "job"),
        name=_require_str(job, "name", "job"),
        html_url=_require_str(job, "html_url", "job"),
        status=_require_str(job, "status", "job"),
        conclusion=_optional_str(job, "conclusion", "job"),
    )


def parse_annotation(payload: object) -> Annotation:
    """Narrow one element of an annotations page into an `Annotation`.

    Args:
        payload: One entry of the annotations array.

    Returns:
        The annotation's level, text, and location.

    Raises:
        AuditError: If the entry is not an object carrying a level and a message.
    """
    annotation = _require_mapping(payload, "annotation")
    return Annotation(
        level=_require_str(annotation, "annotation_level", "annotation"),
        message=_require_str(annotation, "message", "annotation"),
        path=_optional_str(annotation, "path", "annotation"),
        start_line=_optional_int(annotation, "start_line", "annotation"),
    )


def paginate(fetch: Callable[[str], object], path: str, key: str | None = None) -> list[object]:
    """Return every item of a paginated listing, following pages until one is short.

    The Link header ``gh`` would expose is not available through a decoded body, so pages are
    requested by number and the listing ends where a page holds fewer than `PAGE_SIZE` items.
    That costs one extra request when a listing divides exactly, and never misses a page.

    Args:
        fetch: The transport to call with each page's path.
        path: The API path without a query string.
        key: The object key holding the array, or None when the body is the array itself.

    Returns:
        The concatenated items, in the order the API returned them.

    Raises:
        AuditError: If a page is not shaped as the endpoint documents.
    """
    items: list[object] = []
    page = 1
    while True:
        payload = fetch(f"{path}?per_page={PAGE_SIZE}&page={page}")
        if key is None:
            batch = _require_list(payload, path)
        else:
            batch = _require_list(_require_mapping(payload, path).get(key), f"{path}.{key}")
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        page += 1


def is_deprecation(annotation: Annotation) -> bool:
    """Report whether an annotation announces a deprecated runtime.

    The test is a level plus a word stem rather than a pattern matching the current Node
    wording: matching every warning would report the setup-uv cache-reservation noise on each
    clean run, and matching only Node would miss the runner-image deprecations and whatever the
    next runtime deprecation is called.

    Args:
        annotation: The annotation to test.

    Returns:
        True when the annotation is a warning or failure whose text names a deprecation.
    """
    return (
        annotation.level in ACTIONABLE_LEVELS and DEPRECATION_MARKER in annotation.message.lower()
    )


def collect_findings(
    jobs_with_annotations: Iterable[tuple[Job, Sequence[Annotation]]],
) -> list[Finding]:
    """Pair every deprecation annotation with the job that carried it.

    Args:
        jobs_with_annotations: Each audited job and the annotations its check run holds.

    Returns:
        One finding per matching annotation, in the order the jobs were supplied.
    """
    return [
        Finding(job=job, annotation=annotation)
        for job, annotations in jobs_with_annotations
        for annotation in annotations
        if is_deprecation(annotation)
    ]


def _flatten(text: str) -> str:
    """Return upstream text collapsed onto one line.

    Annotation messages are upstream text that may carry line breaks, and both renderings below
    are line-oriented: a break would end a table row early and split a log line in two.
    """
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _cell(text: str) -> str:
    """Return text safe to place inside a Markdown table cell.

    A pipe would end the cell early. This is the table's escaping alone, so a change to it
    cannot reach the plain log line ``describe`` renders.
    """
    return _flatten(text).replace("|", r"\|")


def describe(finding: Finding) -> str:
    """Return a one-line rendering of a finding for the workflow log.

    Args:
        finding: The finding to describe.

    Returns:
        A plain line naming the job, the level, the annotation's location, and its text.
    """
    location = f"{finding.annotation.path or '?'}:{finding.annotation.start_line or '?'}"
    return (
        f"{finding.job.name}: {finding.annotation.level}: "
        f"{location}: {_flatten(finding.annotation.message)}"
    )


def render_summary(run: Run, findings: Sequence[Finding], jobs_audited: int) -> str:
    """Render the job summary for one audited run.

    Args:
        run: Identity of the run that was audited.
        findings: Every deprecation annotation found, possibly none.
        jobs_audited: How many completed jobs the audit read annotations for.

    Returns:
        GitHub-flavored Markdown, ending in a newline.
    """
    branch = run.head_branch or "no branch"
    lines = [
        f"## Action runtime audit: {run.name} run {run.run_id}",
        "",
        f"[View the source run]({run.html_url}) -- event `{run.event}`, branch `{branch}`, "
        f"attempt {run.run_attempt}.",
        "",
        f"Jobs audited: {jobs_audited}.",
        "",
    ]
    if not findings:
        lines.append(_NO_FINDINGS)
    else:
        lines.extend(_TABLE_HEADER)
        lines.extend(
            f"| [{_cell(finding.job.name)}]({finding.job.html_url}) "
            f"| {finding.annotation.level} "
            f"| {_cell(finding.annotation.message)} |"
            for finding in findings
        )
        lines.extend(("", _POINTER))
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line, defaulting the repository to the runner's own environment."""
    parser = argparse.ArgumentParser(
        description="Report deprecation annotations on a completed workflow run."
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="Repository as OWNER/REPO; defaults to GITHUB_REPOSITORY.",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        required=True,
        help="Id of the completed run to audit.",
    )
    args = parser.parse_args(argv)
    if not args.repository:
        parser.error("--repository is required when GITHUB_REPOSITORY is not set")
    return args


def _audit(
    fetch: Callable[[str], object], repository: str, run_id: int
) -> tuple[Run, list[Finding], int]:
    """Read one run and return its identity, its findings, and how many jobs were audited.

    Two kinds of job are passed over rather than read, because each costs a request that cannot
    produce a finding. A job that has not completed has no settled annotations: the
    `workflow_run` trigger fires on completion of the run, but a cancelled run can still carry
    jobs the runner never finished. A job the runner skipped executed no action at all, so it
    can carry no runtime deprecation -- the same reasoning the workflow applies to a skipped
    source run, one layer down. A `cancelled` or `failure` conclusion is read normally: those
    jobs ran steps, and a deprecation warning from one of them is exactly what this looks for.
    """
    run = parse_run(fetch(f"/repos/{repository}/actions/runs/{run_id}"))
    jobs = [
        parse_job(entry)
        for entry in paginate(fetch, f"/repos/{repository}/actions/runs/{run_id}/jobs", "jobs")
    ]
    readable = [job for job in jobs if job.status == "completed" and job.conclusion != _SKIPPED]
    audited = [
        (
            job,
            [
                parse_annotation(entry)
                for entry in paginate(fetch, f"/repos/{repository}/check-runs/{job.id}/annotations")
            ],
        )
        for job in readable
    ]
    return run, collect_findings(audited), len(readable)


def _append(path: str, text: str) -> None:
    """Append text to a file, creating it when it does not exist."""
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(text)


def _guarded_write(write: Callable[[], None]) -> bool:
    """Attempt one reporting write, and report whether it took.

    A write here can fail for the ordinary reasons any write can -- a full runner disk, a
    ``GITHUB_STEP_SUMMARY`` path that is not writable, a stream whose encoding cannot carry
    upstream annotation text -- and letting that escape would end the run on a traceback carrying
    the interpreter's exit 1, which is this script's *finding* code. A clean audit would then
    report as a deprecated runtime. So the write is guarded and its failure is answered in the
    exit code instead, by the caller.

    This is one write rather than the whole report because the two callers report different
    things. `emit` composes it per channel so that a channel that fails costs only itself, while
    ``main``'s audit-failure branch uses it directly: no report exists to compose there, because
    the audit never returned one.

    Args:
        write: The write to attempt, taking no arguments.

    Returns:
        True when the write took, False when it failed for one of `_REPORT_FAILURES`.
    """
    try:
        write()
    except _REPORT_FAILURES as error:
        # Suppressed rather than raised: this is the report of a failed report, so the channel it
        # would travel on may be the one already known to be unreliable. The exit code carries it.
        with contextlib.suppress(*_REPORT_FAILURES):
            print(f"::error::infrastructure failure: {_REPORT_FAILED}: {error}", file=sys.stderr)
        return False
    return True


def emit(summary: str, findings: Sequence[Finding]) -> bool:
    """Write the run's report to every channel, and report whether all of them took it.

    Each channel is guarded on its own and every one is attempted whatever the ones before it
    did, so a stdout that cannot be written does not also cost the log lines and the file. The
    results are collected first and combined afterwards, which is what keeps a boolean
    accumulator from short-circuiting a later channel away.

    The channels are ordered by what a reader loses if a later one fails: the summary and the
    per-finding lines first, and the step-summary file last, so a file that cannot be written
    costs only itself.

    Args:
        summary: The rendered job summary.
        findings: Every deprecation annotation found, for the per-finding log lines.

    Returns:
        True when every channel took the report, False when one of them failed.
    """
    # Flushed before the per-finding lines below, because a piped stdout is block-buffered and an
    # unflushed summary would otherwise land after them in the workflow log.
    attempts = [_guarded_write(partial(print, summary, end="", flush=True))]
    attempts.extend(
        _guarded_write(partial(print, describe(finding), file=sys.stderr)) for finding in findings
    )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        attempts.append(_guarded_write(partial(_append, summary_path, summary)))
    return all(attempts)


def main(argv: Sequence[str] | None = None, fetch: Callable[[str], object] = fetch_json) -> int:
    """Audit one completed run and report its deprecation annotations.

    Args:
        argv: Command-line arguments, or None to read ``sys.argv``.
        fetch: The transport to read the API through.

    Returns:
        1 when any deprecation annotation was found, 2 when none was but the audit could not be
        performed or its report could not be written, and 0 otherwise. A finding outranks both:
        the deprecated runtime is actionable now, and letting a failed write of a report that
        already reached the log mask it would report the weaker of the two answers.
    """
    args = _parse_args(argv)
    try:
        run, findings, jobs_audited = _audit(fetch, args.repository, args.run_id)
    except (AuditError, OSError) as error:
        _guarded_write(partial(print, f"::error::{error}", file=sys.stderr))
        return 2
    reported = emit(render_summary(run, findings, jobs_audited), findings)
    if findings:
        return 1
    return 0 if reported else 2


if __name__ == "__main__":
    sys.exit(main())
