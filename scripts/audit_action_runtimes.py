#!/usr/bin/env python3
"""Report the deprecation annotations a completed workflow run's jobs carry.

When an executed action targets a runtime GitHub has deprecated, the runner attaches a warning
annotation to the check run of the job that executed it. That annotation is the only signal
naming an action whose *published* pin is current but whose *runtime* is not, so Dependabot
never produces a pull request for it. This script reads a finished run's jobs and each job's
annotations through ``gh api``, writes a job summary, and exits non-zero when any of them names
a deprecation. AD-42 in ARCHITECTURE.md records why this runs alongside Dependabot rather than
instead of it.

Under ``--skip-stale-runs`` a run that started longer ago than `MAX_AUDITABLE_AGE` is reported
and not read. The trigger, not this script, decides that a run may be stale: the automatic
`workflow_run` half takes whatever run GitHub hands it, while a hand dispatch names a run id
deliberately and audits it at any age.

The script is deliberately stdlib-only and imports nothing from ``doc_lattice``, so the auditing
workflow can run it under ``uv run --no-project`` without resolving or installing the project. Its
one local import is the sibling ``scripts/_ci_report.py``, which owns the guarded reporting
mechanics this script shares with ``scripts/check_action_pin_correspondence.py``. That sibling is
stdlib-only for the same reason, and it is reachable under ``--no-project`` because running a
script by path prepends the script's own directory to ``sys.path``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from _ci_report import emit, guarded_write

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
# How long after a run starts its annotations are still worth reading, under `--skip-stale-runs`.
# A run normally completes in minutes, but `workflow_run` fires on completion however late that
# is: a job awaiting a deployment approval sits pending until GitHub expires it after thirty
# days, and the run reaches `completed` then. The annotations it carries name the pins its own
# workflow file held, so past this horizon the audit would report a bump the default branch has
# already made. Seven days is chosen against the one legitimate long tail -- a release approval
# that waits over a weekend -- and stays well inside the thirty-day expiry that produces the
# stale case.
MAX_AUDITABLE_AGE = timedelta(days=7)

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
# What a skipped audit says instead of a job count. It has to be unmistakable for the clean
# report: three outcomes reach the same summary tab, and "found nothing" and "read nothing" are
# the two a reader would otherwise conflate.
_NOT_AUDITED = "Not audited."


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
        run_started_at: When this attempt began, as an aware datetime. Read rather than
            ``created_at`` because a re-run restarts the clock the horizon measures: the
            annotations belong to the attempt that produced them.
    """

    name: str
    run_id: int
    html_url: str
    event: str
    head_branch: str | None
    run_attempt: int
    run_started_at: datetime


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


def _require_timestamp(payload: dict[str, object], key: str, context: str) -> datetime:
    """Return a timestamp field as an aware datetime, or raise when it is not one.

    The raise is what keeps a malformed timestamp inside `main`'s error ladder. ``ValueError``
    is neither exception that ladder answers, so letting `fromisoformat` raise its own would end
    the run on a traceback carrying the interpreter's exit 1 -- which is this script's findings
    code, and would report an unreadable payload as a deprecated runtime.
    """
    value = _require_str(payload, key, context)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise AuditError(f"{context}: field {key!r} is not a timestamp: {error}") from error
    if moment.tzinfo is None:
        raise AuditError(f"{context}: field {key!r} carries no time zone: {value!r}")
    return moment


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
        run_started_at=_require_timestamp(run, "run_started_at", "run"),
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


def _started(run: Run) -> str:
    """Return the run's start as a plain UTC minute.

    The reports name this instead of an elapsed count. An age in whole days is floored, so every
    run between seven and eight days old would read as "started 7 days ago, more than the 7 days"
    -- a report contradicting itself on precisely the runs the horizon has only just caught --
    and rounding the other way would overstate an age instead. A timestamp rounds nothing, and
    it is the more useful half anyway: it is what identifies the run in a log.
    """
    return f"{run.run_started_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}"


def describe_stale(run: Run) -> str:
    """Return a one-line rendering of a skipped audit for the workflow log.

    Args:
        run: Identity of the run that was not audited.

    Returns:
        A plain line naming the run and when it started, in the shape `describe` uses for a
        finding, so a reader of the log alone sees why the step found nothing.
    """
    return (
        f"{run.name} run {run.run_id}: not audited: it started {_started(run)}, "
        f"more than {MAX_AUDITABLE_AGE.days} days ago"
    )


def _heading(run: Run) -> list[str]:
    """Return the summary lines naming the run, shared by every outcome.

    Both reports open with the same identification, and a reader lands on one without knowing
    which. Composing it once is what keeps the skipped report from drifting into a shape that
    reads like a different workflow's.
    """
    branch = run.head_branch or "no branch"
    return [
        f"## Action runtime audit: {run.name} run {run.run_id}",
        "",
        f"[View the source run]({run.html_url}) -- event `{run.event}`, branch `{branch}`, "
        f"attempt {run.run_attempt}.",
        "",
    ]


def render_stale_summary(run: Run) -> str:
    """Render the job summary for a run too old to audit.

    Args:
        run: Identity of the run that was not audited.

    Returns:
        GitHub-flavored Markdown, ending in a newline. It carries no job count and no findings
        table, because neither was established: nothing was read.
    """
    lines = [
        *_heading(run),
        _NOT_AUDITED,
        "",
        f"This run started {_started(run)}, more than the {MAX_AUDITABLE_AGE.days} days a "
        "run's annotations are read for. They name the pins its own workflow file carried, "
        "which a later commit may already have bumped. Dispatch this workflow against the run "
        "id to audit it at any age.",
    ]
    return "\n".join(lines) + "\n"


def render_summary(run: Run, findings: Sequence[Finding], jobs_audited: int) -> str:
    """Render the job summary for one audited run.

    Args:
        run: Identity of the run that was audited.
        findings: Every deprecation annotation found, possibly none.
        jobs_audited: How many completed jobs the audit read annotations for.

    Returns:
        GitHub-flavored Markdown, ending in a newline.
    """
    lines = [
        *_heading(run),
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


def utc_now() -> datetime:
    """Return the current moment, in UTC.

    The one reading of the clock, injected into `main` the way `fetch_json` is, so the horizon
    is testable against fixed inputs and every other function here stays pure. This spells the
    call out rather than importing ``doc_lattice.datetime_utils``, the AD-2 time boundary the
    engine routes through: the auditing workflow runs this file under ``uv run --no-project``,
    where no part of the package is importable.
    """
    return datetime.now(UTC)


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
    parser.add_argument(
        "--skip-stale-runs",
        action="store_true",
        help=(
            f"Report a run that started more than {MAX_AUDITABLE_AGE.days} days ago as stale "
            "and read nothing. Off by default, so a run named by hand is audited at any age."
        ),
    )
    args = parser.parse_args(argv)
    if not args.repository:
        parser.error("--repository is required when GITHUB_REPOSITORY is not set")
    return args


def _read_findings(
    fetch: Callable[[str], object], repository: str, run_id: int
) -> tuple[list[Finding], int]:
    """Read one run's jobs and return its findings and how many jobs were audited.

    Two kinds of job are passed over rather than read, because each costs a request that cannot
    produce a finding. A job that has not completed has no settled annotations: the
    `workflow_run` trigger fires on completion of the run, but a cancelled run can still carry
    jobs the runner never finished. A job the runner skipped executed no action at all, so it
    can carry no runtime deprecation -- the same reasoning the workflow applies to a skipped
    source run, one layer down. A `cancelled` or `failure` conclusion is read normally: those
    jobs ran steps, and a deprecation warning from one of them is exactly what this looks for.
    """
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
    return collect_findings(audited), len(readable)


def main(
    argv: Sequence[str] | None = None,
    fetch: Callable[[str], object] = fetch_json,
    now: Callable[[], datetime] = utc_now,
) -> int:
    """Audit one completed run and report its deprecation annotations.

    The run itself is read first and on its own, because under ``--skip-stale-runs`` its start
    decides whether the jobs are read at all: a run past `MAX_AUDITABLE_AGE` costs no request
    per job to reach an answer that is already known.

    Args:
        argv: Command-line arguments, or None to read ``sys.argv``.
        fetch: The transport to read the API through.
        now: The clock the run's age is measured against.

    Returns:
        1 when any deprecation annotation was found, 2 when none was but the audit could not be
        performed or its report could not be written, and 0 otherwise -- a run skipped as stale
        included, since a run that was never read established no finding. A finding outranks
        both: the deprecated runtime is actionable now, and letting a failed write of a report
        that already reached the log mask it would report the weaker of the two answers.
    """
    args = _parse_args(argv)
    try:
        run = parse_run(fetch(f"/repos/{args.repository}/actions/runs/{args.run_id}"))
        age = now() - run.run_started_at
        if args.skip_stale_runs and age > MAX_AUDITABLE_AGE:
            skipped = emit(render_stale_summary(run), [describe_stale(run)])
            return 0 if skipped else 2
        findings, jobs_audited = _read_findings(fetch, args.repository, args.run_id)
    except (AuditError, OSError) as error:
        guarded_write(print, f"::error::{error}", file=sys.stderr)
        return 2
    summary = render_summary(run, findings, jobs_audited)
    reported = emit(summary, [describe(finding) for finding in findings])
    if findings:
        return 1
    return 0 if reported else 2


if __name__ == "__main__":
    sys.exit(main())
