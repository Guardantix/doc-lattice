"""GitHub Actions workflow-command encoding and the annotation writers built on it.

This is a leaf of the command-line package by construction. ``cli/output.py`` imports the
tool-error exit code from ``cli/errors.py``, so the error boundary cannot reach back into
``output`` for an encoder without closing an import cycle. Keeping the encoder and both of its
writers here is what lets the drift renderers and the error boundary share one workflow-command
encoder: see AD-41. What they no longer share is one severity. A finding annotated by a command
that does not gate on it is a ``::warning``, so the severity a caller asks for is the one part of
the line that varies, and every escaping rule around it stays in one place.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.markup import escape

from ..error_types import DocumentError, exception_details
from ..path_utils import format_path_for_display
from .runtime import CliRuntime

# The workflow-command severities this package emits. Declared here rather than in constants.py
# because the domain needs only the type: nothing validates a severity at runtime, since every
# value is written by this package rather than read from a document or a config.
AnnotationSeverity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Annotation:
    """One annotation a run asks GitHub to attach to a document.

    Carried as a record rather than a tuple because ``severity`` varies per item within a single
    ``write_annotations`` call: ``lint`` emits its ladder violations at ``error`` and its
    ambiguity findings at ``warning`` in the same run, and that pairing cannot be expressed by a
    call-wide keyword without splitting the call, which
    :func:`warn_unattachable_annotations`'s once-per-run contract forbids.

    ``severity`` is defaulted, so the sites that annotate only what they gate on read exactly as
    they did before it existed.

    ``line`` is declared last and defaulted for the same reason ``severity`` is: every
    existing site constructs an annotation positionally through ``severity``, and a
    document-level link finding has no line to give. GitHub attaches a line-less annotation at
    line 1, which is the closest representation workflow commands allow.
    """

    path: Path
    title: str
    message: str
    severity: AnnotationSeverity = "error"
    line: int | None = None


def escape_github_message(value: str) -> str:
    """Escape a GitHub workflow-command message value.

    Args:
        value: Untrusted message value.

    Returns:
        The workflow-command escaped value.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_github_property(value: str) -> str:
    """Escape a GitHub workflow-command property value.

    Args:
        value: Untrusted property value.

    Returns:
        The workflow-command escaped value.
    """
    return escape_github_message(value).replace(":", "%3A").replace(",", "%2C")


def github_annotation(  # noqa: PLR0913
    path: Path,
    root: Path,
    title: str,
    message: str,
    severity: AnnotationSeverity = "error",
    line: int | None = None,
) -> str:
    """Render one GitHub Actions annotation for a finding.

    The ``file`` property is emitted relative to ``root`` so GitHub Actions can attach
    the annotation to the offending document in the pull request diff. When ``path``
    falls outside ``root``, the absolute path is used instead of raising.

    Args:
        path: Absolute path of the source document.
        root: Base for relative path reporting, chosen by ``CliRuntime.annotation_root``.
        title: Annotation title, before escaping.
        message: Annotation message, before escaping.
        severity: The workflow command to emit. Defaults to ``error``, which is what a
            command annotating exactly what it gates on wants.
        line: The 1-based line to attach at, or None to omit the property.

    Returns:
        A single escaped workflow-command line.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    position = "" if line is None else f",line={line}"
    return (
        f"::{severity} file={escape_github_property(str(relative))}{position},"
        f"title={escape_github_property(title)}::{escape_github_message(message)}"
    )


def write_annotations(runtime: CliRuntime, items: Iterable[Annotation]) -> None:
    """Emit one annotation per item, then report the ones GitHub will not attach.

    The two halves are one call because the warning is only correct over exactly the paths that
    were annotated. Left to the caller, that pairing is a convention every annotating site has
    to re-establish, and the site that forgets it fails its gate with nothing on the pull-request
    diff and nothing in the log saying why -- which is the whole failure the warning exists for.
    It is also why an item carries its own severity: a caller mixing severities has to stay one
    call.

    Args:
        runtime: Active invocation state.
        items: One ``Annotation`` per line, in emission order.

    Raises:
        PipeClosed: If the reader on stdout departed before a write completed.
    """
    annotated: list[Path] = []
    for item in items:
        runtime.write_stdout(
            github_annotation(
                item.path,
                runtime.annotation_root(item.path),
                item.title,
                item.message,
                item.severity,
                line=item.line,
            )
        )
        annotated.append(item.path)
    warn_unattachable_annotations(runtime, annotated)


def write_document_annotation(runtime: CliRuntime, exc: DocumentError) -> None:
    """Emit the ``::error`` annotation for a document-scoped failure that ended the run.

    A drift or ladder finding annotates a document the run classified; this annotates the one
    the run failed on. Both go through ``github_annotation`` against ``annotation_root``, so a
    broken frontmatter lands on the same file in the pull-request diff a stale edge would.

    The title carries the error code rather than a state name, which is what the reader has to
    match on: the codes are the printed domain, and the stderr diagnostic beside this line
    carries the same one.

    Args:
        runtime: Active invocation state.
        exc: The document-scoped failure being reported.

    Raises:
        PipeClosed: If the reader on stdout departed before the write completed. The caller
            emits this before its stderr diagnostic precisely so that refusal reaches AD-40's
            silent 141 instead of being reported as a tool error with a truncated annotation.
    """
    write_annotations(
        runtime, [Annotation(exc.source, f"doc-lattice {exc.code}", exception_details(exc))]
    )


def warn_unattachable_annotations(runtime: CliRuntime, paths: Iterable[Path]) -> None:
    """Warn once when a run emitted annotations GitHub cannot attach to the pull-request diff.

    ``github_annotation`` falls back to an absolute path for a document its base does not
    contain, and GitHub silently drops such an annotation: the gate fails and the pull request
    shows nothing. Reporting it is what keeps that from being undebuggable. The base named is
    always the invocation cwd, because a document contained by the workspace is annotated
    against the workspace and is attachable by construction.

    The warning goes to stderr and fires at most once per run, so stdout stays exactly the
    workflow commands GitHub parses.

    Args:
        runtime: Active invocation state.
        paths: Absolute source-document paths that were annotated during this run.
    """
    outside = sorted(
        {path for path in paths if not path.is_relative_to(runtime.annotation_root(path))}
    )
    if not outside:
        return
    listed = ", ".join(format_path_for_display(path) for path in outside)
    # emoji=False and highlight=False for the reason the load-warning renderer carries them:
    # this line is mostly discovered paths, and Rich would otherwise rewrite a legal `:name:`
    # in one as an icon and recolor the rest.
    runtime.stderr.print(
        f"[yellow]warning[/yellow]: {len(outside)} annotated document(s) fall outside "
        f"{escape(format_path_for_display(runtime.cwd))}, so their annotations use absolute "
        f"paths and will not attach to the pull-request diff: {escape(listed)}",
        soft_wrap=True,
        emoji=False,
        highlight=False,
    )
