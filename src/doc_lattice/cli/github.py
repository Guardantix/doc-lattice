"""GitHub Actions workflow-command encoding and the annotation writers built on it.

This is a leaf of the command-line package by construction. ``cli/output.py`` imports the
tool-error exit code from ``cli/errors.py``, so the error boundary cannot reach back into
``output`` for an encoder without closing an import cycle. Keeping the encoder and both of its
writers here is what lets the drift renderers and the error boundary share one spelling of the
``::error`` line: see AD-41.
"""

from collections.abc import Iterable
from pathlib import Path

from rich.markup import escape

from ..error_types import DocumentError, exception_details
from ..path_utils import format_path_for_display
from .runtime import CliRuntime


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


def github_annotation(path: Path, root: Path, title: str, message: str) -> str:
    """Render one ``::error`` GitHub Actions annotation for a finding.

    The ``file`` property is emitted relative to ``root`` so GitHub Actions can attach
    the annotation to the offending document in the pull request diff. When ``path``
    falls outside ``root``, the absolute path is used instead of raising.

    Args:
        path: Absolute path of the source document.
        root: Base for relative path reporting, chosen by ``CliRuntime.annotation_root``.
        title: Annotation title, before escaping.
        message: Annotation message, before escaping.

    Returns:
        A single escaped workflow-command line.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return (
        f"::error file={escape_github_property(str(relative))},"
        f"title={escape_github_property(title)}::{escape_github_message(message)}"
    )


def write_annotations(runtime: CliRuntime, items: Iterable[tuple[Path, str, str]]) -> None:
    """Emit one ``::error`` annotation per item, then report the ones GitHub will not attach.

    The two halves are one call because the warning is only correct over exactly the paths that
    were annotated. Left to the caller, that pairing is a convention every annotating site has
    to re-establish, and the site that forgets it fails its gate with nothing on the pull-request
    diff and nothing in the log saying why -- which is the whole failure the warning exists for.

    Args:
        runtime: Active invocation state.
        items: One ``(path, title, message)`` triple per annotation, in emission order.

    Raises:
        PipeClosed: If the reader on stdout departed before a write completed.
    """
    annotated: list[Path] = []
    for path, title, message in items:
        runtime.write_stdout(github_annotation(path, runtime.annotation_root(path), title, message))
        annotated.append(path)
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
    write_annotations(runtime, [(exc.source, f"doc-lattice {exc.code}", exception_details(exc))])


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
