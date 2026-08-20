"""Broken-pipe primitives with no dependency of their own.

This module imports nothing outside the standard library and reaches no other module in this
package, which is what lets the entry point rest on it from the same unguarded position it
rests on ``error_types`` from. The pre-renderer fallback in ``cli/__init__.py`` runs precisely
when ``rich``, ``typer``, and most of the engine failed to import, so a neutralizer that itself
needed any of them would be unreachable exactly where it is needed. The policy that decides
*which* channel a failed write belongs to lives in ``cli/runtime.py`` with the consoles it
governs; only the two primitives both callers share live here.
"""

import os
from contextlib import suppress

# The POSIX standard descriptors. Written as literals because the standard library exposes no
# constant for them, and used only as the last-resort value for a stream whose own ``fileno()``
# is unavailable -- never in place of asking a stream what it is actually bound to.
STDOUT_FILENO = 1
STDERR_FILENO = 2


class PipeClosed(BrokenPipeError):
    """A write to this process's stdout failed because the reader departed.

    Raised in place of the ``BrokenPipeError`` a stream or ``rich`` produced, and deliberately
    constructed with no ``errno``. Typer's ``_main`` wraps every command in an ``except OSError``
    clause that converts a member of that family whose ``errno`` is ``EPIPE`` into
    ``sys.exit(1)`` -- the code ``check`` and ``lint`` reserve for drift, and the one exit a
    departed reader must never produce. An ``errno`` of ``None`` does not match that test, so
    Typer re-raises this unchanged and the entry point's own handler answers it with 141.

    That distinction is load-bearing rather than incidental: it is the whole reason a direct
    stream write is re-raised as this class instead of propagating as itself. It is pinned by a
    test so a future constructor that passes an ``errno`` through cannot restore the collision
    quietly.
    """


def file_descriptor(stream: object) -> int | None:
    """Read one stream's file descriptor, or None when it does not have one.

    ``io.UnsupportedOperation`` derives from both ``OSError`` and ``ValueError``, which is what
    an in-memory stream raises; a closed stream raises ``ValueError`` on its own; and a stream
    object that never defined the method at all raises ``AttributeError``. All three mean the
    same thing here -- there is no descriptor to neutralize -- so all three answer None rather
    than propagating into a caller that is already handling a failed write.

    Args:
        stream: Any file-like object, typically a console's resolved ``file``.

    Returns:
        The stream's file descriptor, or None when it does not expose one.
    """
    fileno = getattr(stream, "fileno", None)
    if fileno is None:
        return None
    try:
        return int(fileno())
    except (OSError, ValueError, AttributeError):
        return None


def neutralize(fd: int) -> None:
    """Point one file descriptor at ``os.devnull`` so its pending bytes cannot fail again.

    A failed flush does not discard what it could not write. CPython's buffered writer keeps
    those bytes so the caller can retry, and the interpreter's own shutdown flush is that retry:
    it runs after every handler here has finished, fails a second time on the same dead stream,
    and replaces whatever exit code the run decided on with CPython's 120. Redirecting the
    descriptor makes the retry succeed against ``os.devnull`` instead, which is the only point
    at which this process can still influence it.

    Only the descriptor that actually failed is redirected. Rich's own ``on_broken_pipe``
    redirects file descriptor 1 no matter which stream broke, which is precisely how a dead
    *stderr* used to discard the stdout a succeeding command was still computing.

    Every step is guarded because this runs while the process is already failing: exhausting the
    descriptor table or handing in a descriptor that is already closed must not raise a second
    exception over the first.

    Args:
        fd: File descriptor of the stream whose write failed.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, fd)
    except (OSError, ValueError):
        pass
    finally:
        with suppress(OSError):
            os.close(devnull)
