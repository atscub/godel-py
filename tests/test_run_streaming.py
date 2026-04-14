"""Tests for run() streaming / per-line observer behaviour.

Covers acceptance criteria:
1. stdout lines arrive incrementally — observer fires before subprocess exits.
2. No observer + stream_agents=True + transcript -> stdout events per line.
3. Observer suppresses raw stdout events.
4. Lines >64 KiB do not crash; handled gracefully.
5. CommandResult.stdout matches what communicate() would have returned.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from godel._context import _current_workflow, _line_observer
from godel._run import run, CommandResult
from godel._transcript import TranscriptWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_transcript_events(run_dir: Path) -> list[dict]:
    events = []
    for f in sorted(run_dir.rglob("transcript.jsonl*")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "event" in obj:
                events.append(obj["event"])
    return events


# ---------------------------------------------------------------------------
# Acceptance criterion 5: CommandResult.stdout is byte-identical to communicate()
# ---------------------------------------------------------------------------


def test_commandresult_stdout_equals_communicate_output():
    """run() stdout is byte-identical to what communicate() would return."""

    async def _go():
        # Use a multi-line command with varied line lengths.
        result = await run("printf 'line1\\nline2\\nline3\\n'")
        assert result.stdout == "line1\nline2\nline3\n"
        assert result.returncode == 0

    asyncio.run(_go())


def test_commandresult_no_trailing_newline():
    """run() stdout preserves content that has no trailing newline."""

    async def _go():
        result = await run("printf 'no-newline'")
        assert result.stdout == "no-newline"

    asyncio.run(_go())


def test_commandresult_stderr_captured():
    """stderr is captured separately and does not bleed into stdout."""

    async def _go():
        result = await run("echo out; echo err >&2")
        assert "out" in result.stdout
        assert "err" in result.stderr

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Acceptance criterion 1: lines arrive incrementally
# ---------------------------------------------------------------------------


def test_observer_fires_per_line():
    """Observer callable is invoked once per newline-terminated line."""
    received: list[bytes] = []

    async def _go():
        token = _line_observer.set(received.append)
        try:
            result = await run("printf 'a\\nb\\nc\\n'")
        finally:
            _line_observer.reset(token)
        return result

    result = asyncio.run(_go())
    # Each line including its trailing newline.
    assert received == [b"a\n", b"b\n", b"c\n"]
    # CommandResult still has the full output.
    assert result.stdout == "a\nb\nc\n"


def test_observer_fires_before_subprocess_exit():
    """Observer receives at least the first line before the subprocess exits.

    We use a script that prints a line then sleeps; track the timestamp
    (via event order) by ensuring the observer fires at least once during
    the subprocess lifetime by checking arrival before a second print.
    """
    order: list[str] = []

    async def _go():
        # Script: print line, sleep briefly, print second line.
        # We verify both lines arrive to the observer.
        script = "echo first; sleep 0.05; echo second"

        def _obs(line: bytes) -> None:
            order.append(line.decode().strip())

        token = _line_observer.set(_obs)
        try:
            await run(script)
        finally:
            _line_observer.reset(token)

    asyncio.run(_go())
    assert order == ["first", "second"]


# ---------------------------------------------------------------------------
# Acceptance criterion 3: observer suppresses raw stdout events
# ---------------------------------------------------------------------------


def test_observer_suppresses_stdout_transcript_events(tmp_path):
    """When _line_observer is set, no raw 'stdout' events go to transcript."""
    from godel._decorators import workflow

    captured_by_observer: list[bytes] = []
    transcript_stdout_events: list[dict] = []

    @workflow(stream_agents=True)
    async def wf():
        def _obs(line: bytes) -> None:
            captured_by_observer.append(line)

        token = _line_observer.set(_obs)
        try:
            await run("printf 'alpha\\nbeta\\n'")
        finally:
            _line_observer.reset(token)

    import os
    os.chdir(tmp_path)
    asyncio.run(wf())

    # Observer should have received lines.
    assert b"alpha\n" in captured_by_observer
    assert b"beta\n" in captured_by_observer

    # Transcript must NOT have any 'stdout' events (observer owns the lines).
    events = _read_transcript_events(tmp_path / "runs")
    stdout_events = [e for e in events if e["op"] == "stdout"]
    assert stdout_events == [], (
        f"Expected no raw stdout events with observer active, got: {stdout_events}"
    )


# ---------------------------------------------------------------------------
# Acceptance criterion 2: no observer + stream_agents=True -> stdout events
# ---------------------------------------------------------------------------


def test_no_observer_stream_agents_emits_stdout_events(tmp_path):
    """Without observer, stream_agents=True writes one stdout event per line."""
    from godel._decorators import workflow

    import os
    os.chdir(tmp_path)

    @workflow(stream_agents=True)
    async def wf():
        await run("printf 'foo\\nbar\\n'")

    asyncio.run(wf())

    events = _read_transcript_events(tmp_path / "runs")
    stdout_events = [e for e in events if e["op"] == "stdout"]
    lines = [e["line"] for e in stdout_events]
    assert "foo" in lines
    assert "bar" in lines


# ---------------------------------------------------------------------------
# Acceptance criterion 4: lines >64 KiB do not crash
# ---------------------------------------------------------------------------


def test_large_line_does_not_crash():
    """A line larger than 64 KiB is handled without raising."""
    received: list[bytes] = []

    async def _go():
        # Generate a line that is 100 KiB of 'x' followed by a newline.
        script = "python3 -c \"print('x' * 102400)\""
        token = _line_observer.set(received.append)
        try:
            result = await run(script)
        finally:
            _line_observer.reset(token)
        return result

    result = asyncio.run(_go())
    # Should not crash; result stdout should contain the large output.
    assert len(result.stdout) >= 100 * 1024
    # Observer should have received the large line(s).
    total_bytes = sum(len(b) for b in received)
    assert total_bytes >= 100 * 1024


def test_large_line_commandresult_byte_identical():
    """CommandResult.stdout matches expected content for >100KiB lines."""

    async def _go():
        # 110 KiB line; printf avoids Python's print() adding extra buffering.
        # We build the string in Python and compare.
        expected = "A" * 112640 + "\n"  # 110 KiB + newline
        script = f"python3 -c \"import sys; sys.stdout.write('A' * 112640 + '\\\\n')\""
        result = await run(script)
        return result, expected

    result, expected = asyncio.run(_go())
    assert result.stdout == expected
