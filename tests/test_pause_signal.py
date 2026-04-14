"""Tests for the pause signaling mechanism.

Covers:
- check_pause_request no-op when absent
- check_pause_request raises PauseSignal when file present
- Step honours pause at boundary: PAUSED event emitted, subsequent step skipped
- Paused WORKFLOW_STARTED parent_event_id is WORKFLOW_STARTED
- Pause during replay is ignored
- EventLog.emit_finished(..., status=EventStatus.PAUSED) unit test
- Atomic write: no .pause.tmp orphan left after successful write
- Atomic write: .pause.tmp cleaned up on write failure
- clear_pause_request removes pre-existing .pause.tmp orphans
"""
from __future__ import annotations

import asyncio
import json

import pytest

from godel import workflow, step
from godel._events import EventStatus
from godel._exceptions import PauseSignal
from godel._pause import check_pause_request, write_pause_request, clear_pause_request


# ---------------------------------------------------------------------------
# Unit tests for _pause helpers
# ---------------------------------------------------------------------------

def test_check_pause_request_no_op_when_absent(tmp_path):
    """check_pause_request is a no-op when no sentinel file exists."""
    # Should not raise
    check_pause_request("nonexistent-run-id", runs_dir=str(tmp_path))


def test_check_pause_request_raises_when_file_present(tmp_path):
    """check_pause_request raises PauseSignal when sentinel file exists."""
    write_pause_request("my-run", reason="user requested", runs_dir=str(tmp_path))
    with pytest.raises(PauseSignal) as exc_info:
        check_pause_request("my-run", runs_dir=str(tmp_path))
    assert exc_info.value.reason == "user requested"


def test_check_pause_request_ignores_corrupt_file(tmp_path):
    """check_pause_request treats corrupt/unreadable JSON as no pause."""
    pause_file = tmp_path / "bad-run.pause"
    pause_file.write_text("not-json{{{")
    # Should not raise
    check_pause_request("bad-run", runs_dir=str(tmp_path))


def test_write_pause_request_creates_file(tmp_path):
    """write_pause_request creates the sentinel file with expected keys."""
    path = write_pause_request("run-abc", reason="test reason", runs_dir=str(tmp_path))
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["reason"] == "test reason"
    assert "requested_ts" in payload


def test_clear_pause_request_removes_file(tmp_path):
    """clear_pause_request removes the file idempotently."""
    write_pause_request("run-xyz", runs_dir=str(tmp_path))
    clear_pause_request("run-xyz", runs_dir=str(tmp_path))
    assert not (tmp_path / "run-xyz.pause").exists()
    # Second call should not raise
    clear_pause_request("run-xyz", runs_dir=str(tmp_path))


def test_write_pause_request_no_tmp_orphan_on_success(tmp_path):
    """After a successful write_pause_request, no *.pause.tmp file remains."""
    write_pause_request("run-clean", runs_dir=str(tmp_path))
    orphans = list(tmp_path.glob("*.pause.tmp"))
    assert orphans == [], f"Unexpected orphan tmp files: {orphans}"
    assert (tmp_path / "run-clean.pause").exists()


def test_clear_pause_request_removes_tmp_orphan(tmp_path):
    """clear_pause_request removes pre-existing *.<run_id>.pause.tmp orphan files."""
    # Simulate an orphan left by a crashed write_pause_request; the suffix
    # must include the run_id so the scoped glob can find it.
    orphan = tmp_path / "tmpXXXXXX.run-orphan.pause.tmp"
    orphan.write_text('{"orphan": true}')
    assert orphan.exists()

    # clear_pause_request should sweep away the orphan even though the
    # .pause file itself was never created
    clear_pause_request("run-orphan", runs_dir=str(tmp_path))
    assert not orphan.exists(), "Orphan .pause.tmp file was not removed by clear_pause_request"


def test_clear_pause_request_does_not_stomp_other_run_tmp(tmp_path):
    """clear_pause_request for run-A must NOT delete a live .pause.tmp for run-B."""
    # Simulate a live temp file from run-B mid-write (new naming convention)
    run_b_tmp = tmp_path / "tmpABCDEF.run-b.pause.tmp"
    run_b_tmp.write_text('{"live": true}')
    assert run_b_tmp.exists()

    # Clearing run-a should leave run-b's temp file untouched
    clear_pause_request("run-a", runs_dir=str(tmp_path))
    assert run_b_tmp.exists(), (
        "clear_pause_request for run-a deleted run-b's .pause.tmp file — cross-run stomp bug"
    )


def test_write_pause_request_cleans_up_tmp_on_failure(tmp_path, monkeypatch):
    """write_pause_request cleans up the temp file if os.replace fails."""
    import os as _os

    original_replace = _os.replace

    def broken_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_os, "replace", broken_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        write_pause_request("run-fail", runs_dir=str(tmp_path))

    # No .pause.tmp orphan should remain
    orphans = list(tmp_path.glob("*.pause.tmp"))
    assert orphans == [], f"Temp file not cleaned up on failure: {orphans}"
    # Final .pause file must not exist either
    assert not (tmp_path / "run-fail.pause").exists()


def test_pause_signal_construction():
    sig = PauseSignal(reason="halted", request_ts="2026-01-01T00:00:00+00:00")
    assert sig.reason == "halted"
    assert sig.request_ts == "2026-01-01T00:00:00+00:00"
    assert "PauseSignal" in str(sig)
    assert isinstance(sig, Exception)


# ---------------------------------------------------------------------------
# Integration tests: pause honoured at @step boundary
# ---------------------------------------------------------------------------

def test_step_honours_pause_at_boundary(tmp_path, monkeypatch):
    """Write sentinel between two steps; assert PAUSED event emitted and
    second step never runs."""
    monkeypatch.chdir(tmp_path)
    second_step_ran = {"ran": False}

    @workflow
    async def wf():
        @step
        async def first():
            return "first"

        @step
        async def second():
            second_step_ran["ran"] = True
            return "second"

        await first()
        # Write pause sentinel AFTER first step completes, before second step enters
        from godel._context import _current_workflow
        ctx = _current_workflow.get()
        write_pause_request(ctx.run_id)
        await second()

    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    assert not second_step_ran["ran"], "second step should not have run after pause"

    # Verify PAUSED event is in the log
    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log file found"

    from godel._events import Event
    raw_events: dict[str, Event] = {}
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            ev = Event.from_dict(d)
            raw_events[ev.event_id] = ev

    paused_events = [e for e in raw_events.values() if e.op == "PAUSED"]
    assert len(paused_events) == 1, f"Expected one PAUSED event, got {len(paused_events)}"


def test_paused_event_parent_is_workflow_started(tmp_path, monkeypatch):
    """The PAUSED metadata event's parent_event_id is the WORKFLOW_STARTED event."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def only():
            return "done"

        from godel._context import _current_workflow
        ctx = _current_workflow.get()
        write_pause_request(ctx.run_id)
        await only()

    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files

    from godel._events import Event
    raw_map: dict[str, Event] = {}
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            ev = Event.from_dict(d)
            raw_map[ev.event_id] = ev  # last-write-wins

    # Use last-write-wins snapshot for WORKFLOW_STARTED (children_ids is updated after child emit)
    wf_started = next((e for e in raw_map.values() if e.op == "WORKFLOW_STARTED"), None)
    assert wf_started is not None

    # The PAUSED event should be a child of WORKFLOW_STARTED
    paused_event = next((e for e in raw_map.values() if e.op == "PAUSED"), None)
    assert paused_event is not None
    assert paused_event.event_id in wf_started.children_ids, (
        f"PAUSED event {paused_event.event_id} not in WORKFLOW_STARTED.children_ids "
        f"{wf_started.children_ids}"
    )


def test_paused_event_has_seq_minus_one(tmp_path, monkeypatch):
    """PAUSED metadata event uses invocation_seq=-1 / step_local_seq=-1."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def only():
            return "x"

        from godel._context import _current_workflow
        ctx = _current_workflow.get()
        write_pause_request(ctx.run_id)
        await only()

    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    from godel._events import Event
    raw_map: dict[str, Event] = {}
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            ev = Event.from_dict(d)
            raw_map[ev.event_id] = ev

    paused_event = next((e for e in raw_map.values() if e.op == "PAUSED"), None)
    assert paused_event is not None
    assert paused_event.invocation_seq == -1
    assert paused_event.step_local_seq == -1


def test_pause_ignored_during_replay(tmp_path, monkeypatch):
    """Pause sentinel written before resume is ignored during replay phase."""
    monkeypatch.chdir(tmp_path)

    # First run: two steps, completes cleanly
    @workflow
    async def wf():
        @step
        async def first():
            return "a"

        @step
        async def second():
            return "b"

        await first()
        await second()
        return "done"

    asyncio.run(wf())
    run_id = wf._last_run_id

    # Write a pause sentinel for the run_id
    write_pause_request(run_id)

    # Resume: during the replay phase, the sentinel should be ignored
    from godel._context import _pending_replay
    from godel._event_log import EventLog
    from godel._replay import ReplayWalker

    from godel._config import load_config
    log = EventLog.load(run_id, runs_dir=str(load_config().runs_dir))
    walker = ReplayWalker(log)
    token = _pending_replay.set(walker)

    @workflow
    async def wf2():
        @step
        async def first():
            return "a"

        @step
        async def second():
            return "b"

        await first()
        await second()
        return "done"

    try:
        result = asyncio.run(wf2())
        assert result == "done"
    finally:
        _pending_replay.reset(token)
        clear_pause_request(run_id)


# ---------------------------------------------------------------------------
# Unit test: emit_finished with custom status
# ---------------------------------------------------------------------------

def test_emit_finished_with_paused_status(tmp_path):
    """EventLog.emit_finished(..., status=EventStatus.PAUSED) persists correctly."""
    from godel._event_log import EventLog

    log = EventLog("test-run-paused", runs_dir=str(tmp_path))
    event = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "test"},
    )
    finished = log.emit_finished(
        event.event_id,
        response={"result": "paused"},
        status=EventStatus.PAUSED,
    )
    assert finished.status == EventStatus.PAUSED
    log.close()

    # Reload and verify persisted
    log2 = EventLog.load("test-run-paused", runs_dir=str(tmp_path))
    ev = log2.get_event(event.event_id)
    assert ev is not None
    assert ev.status == EventStatus.PAUSED
    log2.close()
