"""Stdout capture — pipe-per-step fd-level redirect with transcript wiring.

Usage::

    from godel._stdout_capture import capture
    from godel._transcript import TranscriptWriter

    with TranscriptWriter(run_dir) as tw:
        with capture(step_path=("my_step",), stream_path=[], transcript=tw):
            print("hi")   # lands in transcript as op="stdout"

Mechanism
---------
1. ``os.pipe()`` creates a read/write pipe pair.
2. ``os.dup2(w, 1)`` replaces fd 1 (stdout) with the write end.
3. A daemon reader thread consumes the read end line-by-line, writing each
   line to the ``TranscriptWriter`` as a ``stdout`` event.
4. On exit (success or exception), fd 1 is restored via ``os.dup2(saved, 1)``
   and the reader thread is joined with a 1-second timeout.

This is an **fd-level** redirect — subprocess children launched from inside a
captured step inherit the swapped fd 1 automatically.

Parallel incompatibility
------------------------
fd 1 is process-global.  Two concurrent captures racing to ``dup2(w, 1)``
would interleave their redirects.  The ``parallel()`` decorator already raises
``ConfigError`` when a ``capture_stdout=True`` step is passed to it; this
module enforces no additional check.

``GODEL_NO_CAPTURE=1`` escape hatch
------------------------------------
When the env var ``GODEL_NO_CAPTURE`` is set to any non-empty value, the
context manager becomes a no-op: fd 1 is never swapped, no thread is started,
and no ``stdout`` events are emitted.  This is useful for:

* Debugging with ``breakpoint()`` / ``pdb`` (the primary use case — see the
  warning in ``docs/stdout-capture.md``).
* Running inside a test harness that captures stdout at the process level.
* Diagnosing hangs where the capture pipe itself is suspected.

Warning
-------
``breakpoint()``, ``pdb.set_trace()``, and any tool that writes prompts to
stdout while reading from stdin will not display correctly inside a captured
step.  Use ``GODEL_NO_CAPTURE=1`` whenever you need a debugger.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)


@runtime_checkable
class _TranscriptProto(Protocol):
    """Minimal duck-type required by ``capture()``."""

    def write_event(
        self,
        op: str,
        step_path: list[str] | tuple[str, ...] | None = None,
        stream_path: list[str] | tuple[str, ...] | None = None,
        **extra: object,
    ) -> int: ...


@contextmanager
def capture(
    step_path: tuple[str, ...] | list[str],
    stream_path: list[str],
    transcript: "_TranscriptProto",
):
    """Context manager that redirects fd 1 into a pipe and writes each line
    to *transcript* as a ``stdout`` event tagged with *step_path* and
    *stream_path*.

    Becomes a no-op when ``GODEL_NO_CAPTURE`` env var is set to a non-empty
    value.

    Parameters
    ----------
    step_path:
        The step's path tuple, e.g. ``("analyse",)``.
    stream_path:
        The current stream path list (may be empty).
    transcript:
        Any object with a ``write_event(op, step_path, stream_path, **extra)``
        method — typically a :class:`~godel._transcript.TranscriptWriter`.
    """
    # Escape hatch: no-op when GODEL_NO_CAPTURE is set to any non-empty value.
    if os.environ.get("GODEL_NO_CAPTURE"):
        yield
        return

    r, w = os.pipe()
    # Save the real stdout fd so we can restore it in the finally block.
    saved = os.dup(1)
    # Point fd 1 at the write end of the pipe.
    os.dup2(w, 1)
    # Close the extra reference to the write end; fd 1 is the only writer now.
    os.close(w)

    # Snapshot immutable copies for the thread closure.
    _step_path = list(step_path)
    _stream_path = list(stream_path)

    def _reader_loop() -> None:
        """Consume the read end of the pipe line-by-line until EOF.

        EOF arrives when every reference to the write end is closed — i.e.
        after ``os.dup2(saved, 1)`` has replaced fd 1 back to the real stdout
        and ``os.close(saved)`` has dropped our saved copy.
        """
        # Open in text mode with line buffering and replace-mode error handling
        # so binary noise from child processes doesn't kill the thread.
        with os.fdopen(r, "r", buffering=1, errors="replace") as pipe_r:
            for line in pipe_r:
                # NIT-1: strip both \n and bare \r (e.g. progress bars that
                # emit carriage-return-terminated lines) so trailing control
                # chars do not leak into the transcript chunk field.
                chunk = line.rstrip("\r\n")
                try:
                    transcript.write_event(
                        "stdout",
                        step_path=_step_path,
                        stream_path=_stream_path,
                        chunk=chunk,
                    )
                except Exception:
                    # Never let transcript errors propagate into the reader
                    # thread — a failed write must not crash the step.
                    pass

    t = threading.Thread(target=_reader_loop, daemon=True, name="godel-stdout-capture")
    t.start()

    try:
        yield
    finally:
        # Restore fd 1 to the real stdout BEFORE closing the saved fd.
        # Sequence matters:
        #   1. dup2(saved, 1) → fd 1 now points at real stdout again.
        #   2. close(saved)   → the last open reference to the real stdout
        #      (held in `saved`) is released — but fd 1 still holds it.
        # After step 2 the write end of the pipe has no open file descriptors
        # pointing at it, so the reader thread's `for line in pipe_r:` loop
        # sees EOF and exits cleanly.
        os.dup2(saved, 1)
        os.close(saved)
        # Wait up to 1 second for the reader to drain.
        t.join(timeout=1.0)
        # WARN-3: if the reader thread did not exit within the join timeout
        # (e.g. a transcript.write_event call is wedged), surface a warning so
        # the silent expiry is observable in logs.  The thread remains daemon
        # so it will not block interpreter shutdown.
        if t.is_alive():
            _logger.warning(
                "godel stdout capture reader thread did not exit within 1s "
                "after fd 1 restore (step_path=%r, stream_path=%r); "
                "thread left running as daemon.",
                _step_path,
                _stream_path,
            )
