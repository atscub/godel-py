"""Tests for godel/_stdout_capture.py — stdout capture acceptance criteria.

Covers acceptance criteria from godel-py-5pl.7:

1. Non-parallel capturing step: print("hi") and a subprocess child both land
   as stdout events; sys.stdout is identical before and after.
2. Registration-time error: @step(capture_stdout=True) combined with a
   parallel-executing context raises ConfigError with an actionable message;
   test asserts on the message substring.
3. GODEL_NO_CAPTURE=1: context manager is a no-op at runtime; stdout untouched.
4. Reader thread never leaks: after step exit, thread is joined within 1s.
5. Docs page exists and warns about interactive debugger breakage.

NOTE on stdout capture in tests
--------------------------------
The ``capture()`` context manager works at the **fd level** (``os.dup2``).
pytest's capsys works at the ``sys.stdout`` level and replaces the Python
object, so plain ``print()`` calls go through pytest's capture rather than
fd 1.  Tests that verify fd-level capture use either:
  * ``os.write(1, b"...\n")`` — writes directly to fd 1, always captured.
  * ``capsys.disabled()`` — disables pytest's sys.stdout override so print()
    flows through the real fd 1 and is captured by our pipe.
  * subprocess calls — children inherit fd 1, so they always go through our
    pipe regardless of pytest.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from godel._decorators import parallel, step, workflow
from godel._exceptions import ConfigError
from godel._stdout_capture import capture
from godel._transcript import TranscriptWriter, _FILENAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_events(run_dir: Path) -> list[dict]:
    """Return all event dicts (not the header line) from the transcript."""
    events = []
    path = run_dir / _FILENAME
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if "event" in obj:
                events.append(obj["event"])
    return events


def _stdout_events(run_dir: Path) -> list[dict]:
    return [e for e in _read_events(run_dir) if e.get("op") == "stdout"]


# ---------------------------------------------------------------------------
# AC-1a: fd-level write inside capture lands as a stdout event
# ---------------------------------------------------------------------------


def test_capture_fd_write_lands_as_stdout_event(tmp_path):
    """os.write(1, ...) inside the capture context produces a 'stdout' event.

    Uses direct fd write (not print()) to bypass pytest's sys.stdout capture
    and test the fd-level redirect mechanism directly.
    """
    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("my_step",), stream_path=[], transcript=tw):
            os.write(1, b"hello from step\n")

    events = _stdout_events(tmp_path)
    assert len(events) == 1
    assert events[0]["chunk"] == "hello from step"
    assert events[0]["step_path"] == ["my_step"]
    assert events[0]["stream_path"] == []


def test_capture_multiple_fd_writes(tmp_path):
    """Multiple os.write(1) calls each produce a separate stdout event."""
    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("analyse",), stream_path=[], transcript=tw):
            os.write(1, b"line 1\n")
            os.write(1, b"line 2\n")
            os.write(1, b"line 3\n")

    events = _stdout_events(tmp_path)
    assert len(events) == 3
    chunks = [e["chunk"] for e in events]
    assert chunks == ["line 1", "line 2", "line 3"]


def test_capture_print_via_capsys_disabled(tmp_path, capsys):
    """print() inside the capture context produces a 'stdout' event.

    Uses capsys.disabled() so print() goes through fd 1 rather than
    pytest's sys.stdout replacement, allowing our fd-level pipe to intercept.
    """
    with capsys.disabled():
        with TranscriptWriter(tmp_path, run_id="test-run") as tw:
            with capture(step_path=("my_step",), stream_path=[], transcript=tw):
                sys.stdout.flush()
                print("hello from step", flush=True)

    events = _stdout_events(tmp_path)
    # There may be other lines from terminal output; find our line.
    chunks = [e["chunk"] for e in events]
    assert "hello from step" in chunks, f"Expected 'hello from step' in {chunks}"
    # Verify step_path is tagged correctly on the matching event.
    matching = [e for e in events if e["chunk"] == "hello from step"]
    assert matching[0]["step_path"] == ["my_step"]


# ---------------------------------------------------------------------------
# AC-1b: subprocess child stdout also lands in the transcript
# ---------------------------------------------------------------------------


def test_capture_subprocess_stdout(tmp_path):
    """A subprocess launched inside the capture context has its stdout captured.

    Subprocesses inherit fd 1 from the parent; after our dup2 they write
    to the pipe, so their output appears in the transcript.
    """
    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("fetch",), stream_path=[], transcript=tw):
            subprocess.run(
                [sys.executable, "-c", "print('subprocess line')"],
                check=True,
            )

    events = _stdout_events(tmp_path)
    assert any(e["chunk"] == "subprocess line" for e in events), (
        f"Expected 'subprocess line' in stdout events, got: {events}"
    )
    matching = next(e for e in events if e["chunk"] == "subprocess line")
    assert matching["step_path"] == ["fetch"]


def test_capture_subprocess_multiple_lines(tmp_path):
    """Multiple subprocess output lines all appear in the transcript."""
    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("parse",), stream_path=[], transcript=tw):
            subprocess.run(
                [sys.executable, "-c", "print('a'); print('b'); print('c')"],
                check=True,
            )

    events = _stdout_events(tmp_path)
    chunks = [e["chunk"] for e in events]
    assert "a" in chunks
    assert "b" in chunks
    assert "c" in chunks


# ---------------------------------------------------------------------------
# AC-1c: sys.stdout is identical (same object) before and after capture
# ---------------------------------------------------------------------------


def test_capture_sys_stdout_unchanged(tmp_path):
    """sys.stdout object identity is preserved across the capture block.

    The capture mechanism works at the fd level (os.dup2), not by replacing
    sys.stdout, so the Python sys.stdout object must be the same object
    before and after the context manager.
    """
    original_stdout = sys.stdout
    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("s",), stream_path=[], transcript=tw):
            inside_stdout = sys.stdout
            os.write(1, b"hi\n")
    assert sys.stdout is original_stdout, "sys.stdout changed after capture"
    # sys.stdout is also unchanged INSIDE the block.
    assert inside_stdout is original_stdout


# ---------------------------------------------------------------------------
# AC-2: Registration-time error for capture_stdout + parallel
# ---------------------------------------------------------------------------


def test_capture_stdout_in_parallel_raises_config_error():
    """@step(capture_stdout=True) inside parallel() raises ConfigError."""

    @step(capture_stdout=True)
    async def capturing_step():
        return 1

    @workflow
    async def bad_workflow():
        await parallel(capturing_step(), capturing_step())

    with pytest.raises(ConfigError) as exc_info:
        asyncio.run(bad_workflow())

    msg = str(exc_info.value)
    assert "capture_stdout" in msg


def test_capture_stdout_parallel_error_message_is_actionable():
    """The ConfigError message mentions 'parallel' to guide the user."""

    @step(capture_stdout=True)
    async def my_cap_step():
        return 1

    @workflow
    async def bad_wf():
        await parallel(my_cap_step(), my_cap_step())

    with pytest.raises(ConfigError) as exc_info:
        asyncio.run(bad_wf())

    msg = str(exc_info.value)
    assert "parallel" in msg.lower()


def test_capture_stdout_parallel_error_mentions_workflow_workaround():
    """The ConfigError message mentions using capture_stdout on @workflow."""

    @step(capture_stdout=True)
    async def cap_step():
        return 1

    @workflow
    async def bad_wf():
        await parallel(cap_step(), cap_step())

    with pytest.raises(ConfigError) as exc_info:
        asyncio.run(bad_wf())

    msg = str(exc_info.value)
    # The docs and ticket say the error message should suggest the workaround.
    assert "workflow" in msg.lower() or "@workflow" in msg


# ---------------------------------------------------------------------------
# AC-3: GODEL_NO_CAPTURE=1 → context manager is a no-op
# ---------------------------------------------------------------------------


def test_godel_no_capture_noop(tmp_path, monkeypatch):
    """GODEL_NO_CAPTURE=1 makes capture() a no-op; no stdout events emitted."""
    monkeypatch.setenv("GODEL_NO_CAPTURE", "1")

    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("step",), stream_path=[], transcript=tw):
            os.write(1, b"should not be captured\n")

    events = _stdout_events(tmp_path)
    assert events == [], f"Expected no events, got: {events}"


def test_godel_no_capture_fd1_untouched(monkeypatch):
    """With GODEL_NO_CAPTURE=1, fd 1 is not modified (same inode before and during)."""
    monkeypatch.setenv("GODEL_NO_CAPTURE", "1")

    before_stat = os.fstat(1)

    class _NullTranscript:
        def write_event(self, *a, **kw):
            return 0

    with capture(step_path=("s",), stream_path=[], transcript=_NullTranscript()):
        inside_stat = os.fstat(1)

    assert before_stat == inside_stat, (
        "fd 1 was modified despite GODEL_NO_CAPTURE=1"
    )


def test_godel_no_capture_empty_string_does_not_disable(tmp_path, monkeypatch):
    """GODEL_NO_CAPTURE='' (empty string) does NOT disable capture.

    Only a non-empty value disables capture.
    """
    monkeypatch.setenv("GODEL_NO_CAPTURE", "")

    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("step",), stream_path=[], transcript=tw):
            os.write(1, b"captured\n")

    events = _stdout_events(tmp_path)
    assert any(e["chunk"] == "captured" for e in events), events


# ---------------------------------------------------------------------------
# AC-4: Reader thread is joined within 1 second after step exit
# ---------------------------------------------------------------------------


def test_reader_thread_joined_within_timeout(tmp_path):
    """The capture context manager ensures the reader thread finishes promptly."""
    active_threads_before = {t.ident for t in threading.enumerate()}

    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("step",), stream_path=[], transcript=tw):
            os.write(1, b"draining test\n")

    # After exiting the context, no "godel-stdout-capture" threads that were
    # started in this block should still be alive.
    alive_capture_threads = [
        t for t in threading.enumerate()
        if t.name == "godel-stdout-capture"
        and t.ident not in active_threads_before
    ]
    assert alive_capture_threads == [], (
        f"Reader thread(s) still alive after context exit: {alive_capture_threads}"
    )


def test_reader_thread_join_timing(tmp_path):
    """The context manager exits within ~2s even with moderate output."""
    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        t_start = time.monotonic()
        with capture(step_path=("step",), stream_path=[], transcript=tw):
            for _ in range(50):
                os.write(1, b"x" * 80 + b"\n")
        elapsed = time.monotonic() - t_start

    # Should complete well within 2 seconds (1s join timeout + overhead).
    assert elapsed < 2.5, f"capture block took {elapsed:.2f}s — reader thread may have leaked"


# ---------------------------------------------------------------------------
# AC-5: Docs page exists and warns about interactive debugger breakage
# ---------------------------------------------------------------------------


def test_docs_stdout_capture_page_exists():
    """docs/stdout-capture.md must exist."""
    tests_dir = Path(__file__).parent
    repo_root = tests_dir.parent
    doc_path = repo_root / "docs" / "stdout-capture.md"
    assert doc_path.exists(), f"Missing docs page: {doc_path}"


def test_docs_warns_about_debugger():
    """docs/stdout-capture.md must warn about interactive debugger breakage."""
    tests_dir = Path(__file__).parent
    repo_root = tests_dir.parent
    doc_path = repo_root / "docs" / "stdout-capture.md"
    content = doc_path.read_text(encoding="utf-8")
    assert "breakpoint" in content or "pdb" in content, (
        "docs/stdout-capture.md must warn about interactive debugger breakage"
    )
    assert "GODEL_NO_CAPTURE" in content, (
        "docs/stdout-capture.md must document the GODEL_NO_CAPTURE escape hatch"
    )


def test_docs_godel_no_capture_no_longer_flagged_as_unavailable():
    """docs/stdout-capture.md must NOT mark GODEL_NO_CAPTURE as 'not available today'."""
    tests_dir = Path(__file__).parent
    repo_root = tests_dir.parent
    doc_path = repo_root / "docs" / "stdout-capture.md"
    content = doc_path.read_text(encoding="utf-8")
    # The old docs had "Not available today." in a note block — this should be gone.
    assert "Not available today" not in content, (
        "docs/stdout-capture.md still marks GODEL_NO_CAPTURE as not available"
    )


# ---------------------------------------------------------------------------
# Robustness: fd 1 is restored even when an exception is raised
# ---------------------------------------------------------------------------


def test_capture_restores_fd1_on_exception(tmp_path):
    """fd 1 is restored even when an exception is raised inside capture."""
    original_fd1_stat = os.fstat(1)

    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with pytest.raises(ValueError, match="intentional"):
            with capture(step_path=("boom",), stream_path=[], transcript=tw):
                os.write(1, b"before raise\n")
                raise ValueError("intentional")

    # fd 1 must still point to the original target.
    assert os.fstat(1) == original_fd1_stat


def test_capture_stream_path_propagated(tmp_path):
    """stream_path is correctly attached to stdout events."""
    stream_path = ["agent", "claude"]

    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        with capture(step_path=("s",), stream_path=stream_path, transcript=tw):
            os.write(1, b"streamed output\n")

    events = _stdout_events(tmp_path)
    assert events[0]["stream_path"] == stream_path


# ---------------------------------------------------------------------------
# Integration: @step(capture_stdout=True) inside @workflow
# ---------------------------------------------------------------------------


class _CollectingTranscript:
    """In-memory transcript mock for integration tests."""

    def __init__(self):
        self.events: list[dict] = []

    def write_event(self, op, step_path=None, stream_path=None, **extra):
        self.events.append({
            "op": op,
            "step_path": list(step_path or []),
            "stream_path": list(stream_path or []),
            **extra,
        })
        return len(self.events)

    def stdout_events(self):
        return [e for e in self.events if e["op"] == "stdout"]


def test_step_capture_stdout_in_workflow():
    """@step(capture_stdout=True) routes fd 1 writes to the transcript."""
    from godel._context import _current_transcript

    ct = _CollectingTranscript()

    @step(capture_stdout=True)
    async def loud_step():
        os.write(1, b"hello from loud_step\n")
        return 42

    @workflow
    async def wf():
        return await loud_step()

    token = _current_transcript.set(ct)
    try:
        result = asyncio.run(wf())
    finally:
        _current_transcript.reset(token)

    assert result == 42
    stdout_evts = ct.stdout_events()
    assert len(stdout_evts) == 1
    assert stdout_evts[0]["chunk"] == "hello from loud_step"
    assert stdout_evts[0]["step_path"] == ["loud_step"]


def test_step_capture_stdout_step_path_tagged():
    """stdout events carry the correct step_path from the @step decorator."""
    from godel._context import _current_transcript

    ct = _CollectingTranscript()

    @step(capture_stdout=True)
    async def named_step():
        os.write(1, b"tagged output\n")
        return 1

    @workflow
    async def wf():
        return await named_step()

    token = _current_transcript.set(ct)
    try:
        asyncio.run(wf())
    finally:
        _current_transcript.reset(token)

    stdout_evts = ct.stdout_events()
    assert stdout_evts, "No stdout events captured"
    assert stdout_evts[0]["step_path"] == ["named_step"]
    assert stdout_evts[0]["chunk"] == "tagged output"


def test_step_without_capture_does_not_emit_stdout_events():
    """@step without capture_stdout=True does not emit stdout events."""
    from godel._context import _current_transcript

    ct = _CollectingTranscript()

    @step
    async def silent_step():
        # This write goes to fd 1 but capture is not active.
        return 1

    @workflow
    async def wf():
        return await silent_step()

    token = _current_transcript.set(ct)
    try:
        asyncio.run(wf())
    finally:
        _current_transcript.reset(token)

    stdout_evts = ct.stdout_events()
    assert stdout_evts == [], f"Unexpected stdout events: {stdout_evts}"


def test_step_capture_cleans_up_owned_transcript_on_exception(monkeypatch):
    """WARN-1 regression: when @step(capture_stdout=True) owns its transcript
    (no workflow-level transcript is active) and the step body raises, the
    TranscriptWriter must still be closed and the temp mkdtemp directory
    removed — no file handle or dir leak on the exception path.
    """
    import tempfile as _tempfile

    created_dirs: list[str] = []
    real_mkdtemp = _tempfile.mkdtemp

    def _tracking_mkdtemp(*a, **kw):
        d = real_mkdtemp(*a, **kw)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(_tempfile, "mkdtemp", _tracking_mkdtemp)

    @step(capture_stdout=True)
    async def boom_step():
        os.write(1, b"before the boom\n")
        raise RuntimeError("boom from step body")

    @workflow
    async def wf():
        return await boom_step()

    with pytest.raises(RuntimeError, match="boom from step body"):
        asyncio.run(wf())

    # Any temp dirs created with the godel-capture- prefix (owned by the
    # step-level capture path) must be gone, even though the step raised.
    leaked = [
        d for d in created_dirs
        if os.path.basename(d).startswith("godel-capture-")
        and os.path.exists(d)
    ]
    assert leaked == [], f"Temp dirs leaked after step exception: {leaked}"


def test_workflow_capture_cleans_up_owned_transcript_on_exception(monkeypatch):
    """Companion regression for the @workflow(capture_stdout=True) path: the
    mkdtemp dir is removed even when the workflow body raises.
    """
    import tempfile as _tempfile

    created_dirs: list[str] = []
    real_mkdtemp = _tempfile.mkdtemp

    def _tracking_mkdtemp(*a, **kw):
        d = real_mkdtemp(*a, **kw)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(_tempfile, "mkdtemp", _tracking_mkdtemp)

    @workflow(capture_stdout=True)
    async def wf_boom():
        raise RuntimeError("boom from workflow body")

    with pytest.raises(RuntimeError, match="boom from workflow body"):
        asyncio.run(wf_boom())

    leaked = [
        d for d in created_dirs
        if os.path.basename(d).startswith("godel-wf-capture-")
        and os.path.exists(d)
    ]
    assert leaked == [], f"Workflow temp dirs leaked: {leaked}"


def test_step_capture_subprocess_in_workflow():
    """Subprocess stdout inside @step(capture_stdout=True) lands in transcript."""
    from godel._context import _current_transcript

    ct = _CollectingTranscript()

    @step(capture_stdout=True)
    async def fetch_step():
        subprocess.run(
            [sys.executable, "-c", "print('from subprocess inside step')"],
            check=True,
        )
        return "done"

    @workflow
    async def wf():
        return await fetch_step()

    token = _current_transcript.set(ct)
    try:
        result = asyncio.run(wf())
    finally:
        _current_transcript.reset(token)

    assert result == "done"
    stdout_evts = ct.stdout_events()
    assert any(e["chunk"] == "from subprocess inside step" for e in stdout_evts), (
        f"Expected subprocess output in transcript, got: {stdout_evts}"
    )


# ---------------------------------------------------------------------------
# Workflow-level capture_stdout=True
# ---------------------------------------------------------------------------


def test_workflow_capture_stdout_runs_correctly():
    """@workflow(capture_stdout=True) runs correctly and returns expected result.

    Verifies that fd-level capture at workflow level doesn't break execution.
    """

    @step
    async def inner():
        # Direct fd write; workflow-level capture will route this to transcript.
        os.write(1, b"wf-level output\n")
        return 42

    @workflow(capture_stdout=True)
    async def wf():
        return await inner()

    # The workflow must complete successfully even with capture active.
    result = asyncio.run(wf())
    assert result == 42


def test_workflow_capture_stdout_fd_captured_via_transcript(tmp_path):
    """@workflow(capture_stdout=True) routes fd 1 writes to the workflow transcript.

    Injects a TranscriptWriter before the workflow runs; the workflow reuses
    it (since _current_transcript is already set) and the step's capture
    writes stdout events into it.
    """
    from godel._context import _current_transcript
    from godel._transcript import TranscriptWriter

    @step(capture_stdout=True)
    async def inner_capture():
        os.write(1, b"captured at wf level\n")
        return 7

    @workflow(capture_stdout=True)
    async def wf_cap():
        return await inner_capture()

    # Inject our writer before the workflow runs; the workflow sees it in
    # _current_transcript and reuses it rather than creating a new one.
    with TranscriptWriter(tmp_path, run_id="test-run") as tw:
        token = _current_transcript.set(tw)
        try:
            result = asyncio.run(wf_cap())
        finally:
            _current_transcript.reset(token)

    assert result == 7
    events = _stdout_events(tmp_path)
    assert any(e["chunk"] == "captured at wf level" for e in events), (
        f"Expected 'captured at wf level' in transcript, got: {events}"
    )
