"""Tests for godel._tail — async tail iterator."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from godel._events import Event, EventStatus
from godel._tail import tail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event_dict(
    event_id: str,
    op: str,
    status: str = "STARTED",
    run_id: str = "test-run",
    seq: int = 0,
    step_path: list | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "run_id": run_id,
        "seq": seq,
        "children_ids": [],
        "step_path": step_path or [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": op,
        "request_hash": "",
        "request": {},
        "response": None,
        "status": status,
        "ts_start": "2026-01-01T00:00:00+00:00",
        "ts_end": "2026-01-01T00:00:01+00:00" if status in ("FINISHED", "FAILED") else None,
    }


def _write_event(path: Path, d: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(d) + "\n")
        f.flush()


def _write_events(path: Path, events: list[dict]) -> None:
    for e in events:
        _write_event(path, e)


async def _collect(coro, n: int | None = None, timeout: float = 5.0) -> list[Event]:
    """Collect up to *n* events from an async iterator (or all if n is None)."""
    results = []
    async def _inner():
        async for event in coro:
            results.append(event)
            if n is not None and len(results) >= n:
                return
    await asyncio.wait_for(_inner(), timeout=timeout)
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tail_reads_existing_and_live(tmp_path):
    """Tail yields events already in the file, then live appends."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-existing"
    path = runs_dir / f"{run_id}.jsonl"

    # Pre-write one event
    e1 = _make_event_dict("EVT0001", "WORKFLOW_STARTED", seq=0)
    _write_event(path, e1)

    async def _run():
        events = []
        # Start tail — will pick up existing event and then we append a second
        gen = tail(run_id, runs_dir=runs_dir, follow=True, poll_interval=0.01)

        async def _collect_two():
            async for ev in gen:
                events.append(ev)
                if len(events) == 1:
                    # Append second event while iterator is live
                    e2 = _make_event_dict("EVT0002", "step.enter", seq=1)
                    _write_event(path, e2)
                if len(events) >= 2:
                    return
        await asyncio.wait_for(_collect_two(), timeout=5.0)
        return events

    result = asyncio.run(_run())
    assert len(result) == 2
    assert result[0].event_id == "EVT0001"
    assert result[1].event_id == "EVT0002"


def test_tail_waits_for_file(tmp_path):
    """Tail waits for the file to be created before yielding events."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-wait"
    path = runs_dir / f"{run_id}.jsonl"

    async def _run():
        events = []
        gen = tail(run_id, runs_dir=runs_dir, follow=False, poll_interval=0.02)

        async def _write_delayed():
            await asyncio.sleep(0.05)
            e1 = _make_event_dict("EVT0010", "WORKFLOW_STARTED", status="FINISHED", seq=0)
            # Also write a terminal WORKFLOW_STARTED so follow=False and stop_on_terminal fire
            _write_event(path, e1)

        async def _collect_one():
            async for ev in gen:
                events.append(ev)

        write_task = asyncio.create_task(_write_delayed())
        collect_task = asyncio.create_task(_collect_one())
        await asyncio.wait_for(asyncio.gather(write_task, collect_task), timeout=5.0)
        return events

    result = asyncio.run(_run())
    assert len(result) >= 1
    assert result[0].event_id == "EVT0010"


def test_tail_terminates_on_workflow_finished(tmp_path):
    """Iterator stops when WORKFLOW_STARTED reaches FINISHED (stop_on_terminal=True)."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-terminal"
    path = runs_dir / f"{run_id}.jsonl"

    # Write: step event (STARTED), then WORKFLOW_STARTED FINISHED
    events = [
        _make_event_dict("EVT0020", "step.enter", status="STARTED", seq=0),
        _make_event_dict("EVT0021", "WORKFLOW_STARTED", status="FINISHED", seq=1),
        # This event should NOT be yielded — iterator already stopped
        _make_event_dict("EVT0022", "step.enter", status="FINISHED", seq=2),
    ]
    _write_events(path, events)

    result = asyncio.run(_collect(
        tail(run_id, runs_dir=runs_dir, follow=True, stop_on_terminal=True, poll_interval=0.01),
    ))

    event_ids = [e.event_id for e in result]
    assert "EVT0021" in event_ids
    assert "EVT0022" not in event_ids


def test_tail_no_stop_on_terminal(tmp_path):
    """With stop_on_terminal=False, follow=False, all events are read."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-no-terminal"
    path = runs_dir / f"{run_id}.jsonl"

    events = [
        _make_event_dict("EVT0030", "WORKFLOW_STARTED", status="FINISHED", seq=0),
        _make_event_dict("EVT0031", "step.enter", status="FINISHED", seq=1),
    ]
    _write_events(path, events)

    result = asyncio.run(_collect(
        tail(run_id, runs_dir=runs_dir, follow=False, stop_on_terminal=False, poll_interval=0.01),
    ))
    assert len(result) == 2


def test_tail_handles_partial_line(tmp_path):
    """A partial JSON line written to disk is buffered; completion triggers yield."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-partial"
    path = runs_dir / f"{run_id}.jsonl"

    # Write a complete event first so we can verify ordering
    e1 = _make_event_dict("EVT0040", "WORKFLOW_STARTED", status="FINISHED", seq=0)
    line = json.dumps(e1)

    # Write half the line without a newline
    with open(path, "w") as f:
        f.write(line[:len(line) // 2])
        f.flush()

    async def _run():
        events = []
        gen = tail(run_id, runs_dir=runs_dir, follow=True, stop_on_terminal=True, poll_interval=0.02)

        async def _complete_write():
            await asyncio.sleep(0.05)
            with open(path, "a") as f:
                f.write(line[len(line) // 2:] + "\n")
                f.flush()

        async def _collect_one():
            async for ev in gen:
                events.append(ev)

        write_task = asyncio.create_task(_complete_write())
        collect_task = asyncio.create_task(_collect_one())
        await asyncio.wait_for(asyncio.gather(write_task, collect_task), timeout=5.0)
        return events

    result = asyncio.run(_run())
    assert len(result) == 1
    assert result[0].event_id == "EVT0040"


def test_tail_ambiguous_prefix_raises(tmp_path):
    """tail() raises ValueError when prefix matches multiple files."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    # Create two files with the same prefix
    (runs_dir / "shared-abc.jsonl").write_text("")
    (runs_dir / "shared-xyz.jsonl").write_text("")

    async def _run():
        events = []
        async for ev in tail("shared", runs_dir=runs_dir, follow=False, poll_interval=0.01):
            events.append(ev)
        return events

    with pytest.raises(ValueError, match='Ambiguous prefix "shared"'):
        asyncio.run(_run())


def test_tail_rotation_reopens(tmp_path):
    """Tail reopens the file when it is truncated below the current read position.

    We write several events to make the file large, then overwrite with a
    single short event so ``current_size < tell`` triggers the reopen.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-rotate"
    path = runs_dir / f"{run_id}.jsonl"

    # Write enough events to advance the file position well past the short
    # replacement that will be written on rotation.
    initial_events = [
        _make_event_dict(f"EVT005{i}", "step.enter", status="STARTED", seq=i)
        for i in range(8)
    ]
    _write_events(path, initial_events)

    async def _run():
        events = []

        async def _truncate_and_write():
            await asyncio.sleep(0.15)
            # Overwrite with a single short line — new size < old tell position
            short = {"event_id": "EVT0060", "run_id": "test-run", "seq": 0,
                     "children_ids": [], "step_path": [], "invocation_seq": 0,
                     "step_local_seq": 0, "op": "WORKFLOW_STARTED",
                     "request_hash": "", "request": {}, "response": None,
                     "status": "FINISHED",
                     "ts_start": "2026-01-01T00:00:00+00:00",
                     "ts_end": "2026-01-01T00:00:01+00:00"}
            with open(path, "w") as f:
                f.write(json.dumps(short) + "\n")
                f.flush()

        async def _collect_rotated():
            # stop_on_terminal=True: iterator stops after EVT0060 WORKFLOW_STARTED FINISHED
            async for ev in tail(run_id, runs_dir=runs_dir, follow=True,
                                 stop_on_terminal=True, poll_interval=0.02):
                events.append(ev)

        trunc_task = asyncio.create_task(_truncate_and_write())
        collect_task = asyncio.create_task(_collect_rotated())
        await asyncio.wait_for(asyncio.gather(trunc_task, collect_task), timeout=5.0)
        return events

    result = asyncio.run(_run())
    ids = [e.event_id for e in result]
    # All initial events should be present
    for i in range(8):
        assert f"EVT005{i}" in ids, f"EVT005{i} missing from {ids}"
    # The rotated event should appear after reopen
    assert "EVT0060" in ids


def test_tail_no_follow_exits_at_eof(tmp_path):
    """With follow=False, iterator stops at current EOF."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-no-follow"
    path = runs_dir / f"{run_id}.jsonl"

    events = [
        _make_event_dict("EVT0060", "step.enter", status="STARTED", seq=0),
        _make_event_dict("EVT0061", "step.enter", status="FINISHED", seq=1),
    ]
    _write_events(path, events)

    result = asyncio.run(_collect(
        tail(run_id, runs_dir=runs_dir, follow=False, stop_on_terminal=False, poll_interval=0.01),
    ))
    assert len(result) == 2
