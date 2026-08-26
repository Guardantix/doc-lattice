"""Text streams that fail the two ways a CI report's writes actually fail.

`scripts/_ci_report.py` guards every reporting write against a disk or path error and against a
stream whose encoding cannot carry upstream text. Three suites assert against that guard -- the
helper's own, and each script's adapter into it -- and all three need the same two doubles, so
holding one implementation is what keeps a fix to either from landing in one suite and leaving
the others asserting the old mechanism.

Both doubles fail at the *stream* rather than at the call site on purpose. Every write after a
failing one is then a real write through the real stream, so "the later writes were still
attempted" is asserted against what those writes actually produced rather than against a
recording of intent.

The names keep their leading underscore across the move, matching `tests/workflow_helpers.py`.
"""


class _FailsOnceThenWrites:
    """A text stream that raises `OSError` on its first write and delegates every later one."""

    def __init__(self, stream):
        self._stream = stream
        self.failed = False

    def write(self, text: str) -> int:
        if not self.failed:
            self.failed = True
            raise OSError("No space left on device")
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()


class _AsciiOnly:
    """A text stream that refuses non-ASCII, the way a console under an ASCII encoding does.

    `TextIOWrapper.write` raises `UnicodeEncodeError` when its encoding cannot carry the text,
    and that is a `ValueError`. Reproducing the mechanism rather than injecting a hand-built
    exception is what keeps this honest about the failure it names.
    """

    def __init__(self, stream):
        self._stream = stream

    def write(self, text: str) -> int:
        text.encode("ascii")
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()
