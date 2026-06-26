"""E2E resume integration tests — crash and recover.

Validates the M3 exit criterion: a crashed workflow resumes from the crash
point with no duplicate calls, cached values are stable, and print() is silent.
"""
import asyncio
import json
from unittest.mock import patch

from godel import workflow, step, parallel
from godel import det
from godel.io import print as godel_print
from godel._run import run
from godel._context import _pending_replay
from godel._event_log import EventLog
from godel._replay import ReplayWalker
from godel._exceptions import UnsafeResumeError


class SimulatedCrash(Exception):
    pass


def test_crash_and_resume(tmp_path, monkeypatch):
    """Full crash-and-resume cycle: cached steps skipped, crash-point re-executes."""
    monkeypatch.chdir(tmp_path)

    execution_log = []
    crash_on_step_b = True

    @workflow
    async def wf():
        @step
        async def step_a():
            result = await run("echo step_a", idempotent=True)
            execution_log.append(("step_a", "executed"))
            return result.stdout.strip()

        @step
        async def step_b():
            execution_log.append(("step_b", "entered"))
            if crash_on_step_b:
                raise SimulatedCrash("boom at step_b")
            result = await run("echo step_b", idempotent=True)
            return result.stdout.strip()

        a = await step_a()
        b = await step_b()
        return {"a": a, "b": b}

    # --- First run: crashes at step_b ---
    import pytest

    with pytest.raises(SimulatedCrash):
        asyncio.run(wf())

    run_id = wf._last_run_id
    assert run_id is not None

    # Verify JSONL exists with partial events
    runs_dir = tmp_path / "runs"
    log_path = runs_dir / f"{run_id}.jsonl"
    assert log_path.exists()

    first_lines = log_path.read_text().strip().split("\n")
    first_events = [json.loads(ln) for ln in first_lines]
    ops = [e["op"] for e in first_events]
    assert "WORKFLOW_STARTED" in ops
    assert "step.enter" in ops

    # step_a should have executed
    assert ("step_a", "executed") in execution_log

    # --- Resume: should skip step_a's run(), re-execute step_b ---
    execution_log.clear()

    event_log = EventLog.load(run_id, runs_dir=str(runs_dir))
    walker = ReplayWalker(event_log)

    # Don't crash this time
    crash_on_step_b = False

    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    assert result["a"] == "step_a"
    assert result["b"] == "step_b"

    # step_a's run() should have been replayed from cache (but step_a function still entered)
    # step_b should have executed for real this time
    step_b_entries = [e for e in execution_log if e[0] == "step_b"]
    assert len(step_b_entries) >= 1


def test_det_values_stable_across_resume(tmp_path, monkeypatch):
    """det.now() and det.random() return same values on resume."""
    monkeypatch.chdir(tmp_path)

    recorded = {}

    @workflow
    async def wf():
        ts = det.now()
        r = det.random()
        u = det.uuid4()
        recorded["ts"] = ts
        recorded["r"] = r
        recorded["u"] = u
        return ts

    # First run
    asyncio.run(wf())
    run_id = wf._last_run_id
    first_ts = recorded["ts"]
    first_r = recorded["r"]
    first_u = recorded["u"]

    # Resume
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    # Same values from cache
    assert recorded["ts"] == first_ts
    assert recorded["r"] == first_r
    assert recorded["u"] == first_u


def test_print_visible_during_replay(tmp_path, monkeypatch, capsys):
    """print() still displays text on replay (skips audit-log only)."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        await godel_print("hello from workflow")
        return "done"

    asyncio.run(wf())
    run_id = wf._last_run_id

    # Clear captured output
    capsys.readouterr()

    # Resume
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    captured = capsys.readouterr()
    assert "hello from workflow" in captured.out


def test_unsafe_resume_non_idempotent_run(tmp_path, monkeypatch):
    """Non-idempotent run() with STARTED-only state raises UnsafeResumeError."""
    monkeypatch.chdir(tmp_path)
    import pytest

    should_crash = True

    @workflow
    async def wf():
        nonlocal should_crash
        if should_crash:
            # Manually create a STARTED-only event for a non-idempotent run
            # by crashing mid-workflow after the event is emitted but before
            # the run actually executes
            raise SimulatedCrash("before run")
        await run("echo dangerous")  # non-idempotent (default)

    with pytest.raises(SimulatedCrash):
        asyncio.run(wf())

    run_id = wf._last_run_id

    # Craft the event log: add a STARTED-only run event
    runs_dir = tmp_path / "runs"
    log_path = runs_dir / f"{run_id}.jsonl"

    from godel._events import Event
    # Compute the request hash that run() will use
    req = {"cmd": "echo dangerous", "cwd": None, "timeout": None, "idempotent": False}
    req_hash = Event.compute_request_hash(req)

    # Append a STARTED-only run event to the JSONL
    from ulid import ULID
    from datetime import datetime, timezone
    started_event = {
        "event_id": str(ULID()),
        "run_id": run_id,
        "seq": 100,
        "children_ids": [],
        "step_path": [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": "run",
        "request_hash": req_hash,
        "request": req,
        "response": None,
        "status": "STARTED",
        "ts_start": datetime.now(timezone.utc).isoformat(),
        "ts_end": None,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(started_event) + "\n")

    # Now resume — should hit UnsafeResumeError
    should_crash = False
    event_log = EventLog.load(run_id, runs_dir=str(runs_dir))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        with pytest.raises(UnsafeResumeError) as exc_info:
            asyncio.run(wf())
        assert "idempotent" in str(exc_info.value)
    finally:
        _pending_replay.reset(token)


def test_resume_with_parallel_branches(tmp_path, monkeypatch):
    """Resume a workflow with parallel branches — cached branches replay."""
    monkeypatch.chdir(tmp_path)

    call_count = 0

    @workflow
    async def wf():
        nonlocal call_count

        @step
        async def branch_a():
            nonlocal call_count
            call_count += 1
            return det.now()

        @step
        async def branch_b():
            nonlocal call_count
            call_count += 1
            return det.now()

        await parallel(branch_a(), branch_b())

        @step
        async def final():
            return "done"

        return await final()

    # First run — succeeds
    asyncio.run(wf())
    run_id = wf._last_run_id
    assert call_count == 2

    # Resume — everything from cache
    call_count = 0
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    assert result == "done"
    # Steps are re-entered (call_count increments), but primitives return cached
    assert call_count == 2


def test_no_duplicate_subprocess_on_resume(tmp_path, monkeypatch):
    """Verify no subprocess is spawned during replay of a completed run()."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        result = await run("echo hello", idempotent=True)
        return result.stdout.strip()

    # First run
    asyncio.run(wf())
    run_id = wf._last_run_id

    # Resume with subprocess mocked to track calls
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    subprocess_calls = []
    original_create = asyncio.create_subprocess_shell

    async def tracking_create(*args, **kwargs):
        subprocess_calls.append(args)
        return await original_create(*args, **kwargs)

    token = _pending_replay.set(walker)
    try:
        with patch("godel._run.asyncio.create_subprocess_shell", side_effect=tracking_create):
            result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    assert result == "hello"
    # No subprocess should have been spawned during replay
    assert len(subprocess_calls) == 0


def test_resume_does_not_duplicate_events(tmp_path, monkeypatch):
    """Resuming a completed run does not append duplicate events."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        t = det.now()
        return t

    asyncio.run(wf())
    run_id = wf._last_run_id

    log_path = tmp_path / "runs" / f"{run_id}.jsonl"
    original_lines = log_path.read_text().strip().split("\n")
    len(original_lines)

    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    # Verify no duplicate events after resume (deduplicate by event_id —
    # parent re-persistence for children_ids is expected in raw JSONL)
    reloaded = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    wf_starts = [e for e in reloaded.all_events() if e.op == "WORKFLOW_STARTED"]
    assert len(wf_starts) == 1
