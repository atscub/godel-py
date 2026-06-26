"""Tests for godel.det deterministic replacements."""
import asyncio
import json
import pytest
from godel._decorators import workflow
from godel import det


def test_det_now_returns_iso(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.now()

    result = asyncio.run(wf())
    assert "T" in result  # ISO format
    assert "+" in result or "Z" in result  # timezone


def test_det_now_records_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.now()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    det_events = [e for e in events if e["op"] == "det.now"]
    assert len(det_events) == 2  # STARTED + FINISHED
    assert "value" in det_events[1]["response"]


def test_det_random_returns_float(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.random()

    result = asyncio.run(wf())
    assert isinstance(result, float)
    assert 0.0 <= result < 1.0


def test_det_random_records_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.random()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    det_events = [e for e in events if e["op"] == "det.random"]
    assert len(det_events) == 2


def test_det_uuid4_returns_valid_uuid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.uuid4()

    result = asyncio.run(wf())
    import uuid
    uuid.UUID(result)  # should not raise


def test_det_uuid4_records_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.uuid4()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    det_events = [e for e in events if e["op"] == "det.uuid4"]
    assert len(det_events) == 2


def test_det_outside_workflow_raises():
    with pytest.raises(RuntimeError, match="inside a @workflow"):
        det.now()
    with pytest.raises(RuntimeError, match="inside a @workflow"):
        det.random()
    with pytest.raises(RuntimeError, match="inside a @workflow"):
        det.uuid4()


# ---------------------------------------------------------------------------
# WARN-2: det.sleep is publicly callable from workflow code
# ---------------------------------------------------------------------------

def test_det_sleep_callable_from_workflow(tmp_path, monkeypatch):
    """godel.det.sleep(0.01) is callable inside a @workflow and emits FINISHED."""
    import godel
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        await godel.det.sleep(0.01)

    asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "expected at least one run log"
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    sleep_events = [e for e in events if e["op"] == "det.sleep"]
    statuses = [e["status"] for e in sleep_events]
    assert "FINISHED" in statuses, f"expected FINISHED det.sleep event, got: {statuses}"


# ---------------------------------------------------------------------------
# CRITICAL-1: STARTED-only resume emits FINISHED + does not re-sleep forever
# ---------------------------------------------------------------------------

def test_det_sleep_started_only_resume_emits_finished(tmp_path):
    """Resume with STARTED-only det.sleep: FINISHED is written even when _replay_suppress=True."""
    import time
    from datetime import datetime, timedelta, timezone
    from godel._context import WorkflowContext, _current_workflow
    from godel._event_log import EventLog
    from godel._replay import ReplayWalker

    run_id = "det-sleep-started-only"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    started = log.emit_started(
        op="det.sleep",
        step_path=(),
        request={"seconds": 10.0},
        invocation_seq=0,
        step_local_seq=0,
    )
    # Backdate ts_start so remainder is ~0 (fast test).
    started.ts_start = (
        datetime.now(timezone.utc) - timedelta(seconds=60)
    ).isoformat()
    log._append_event(started)
    log.close()

    loaded = EventLog.load(run_id, runs_dir=str(tmp_path))

    # Simulate the real resume path: _replay_suppress starts True.
    walker = ReplayWalker(loaded)
    ctx = WorkflowContext(
        run_id=loaded._run_id,
        event_log=loaded,
        replay_walker=walker,
        _local_replay_suppress=True,
    )
    loaded._replay_suppress = True  # mirror what @workflow does on resume
    _current_workflow.set(ctx)

    try:
        t0 = time.monotonic()
        asyncio.run(det.sleep(10.0))
        elapsed = time.monotonic() - t0
        # Should complete quickly (remainder is ~0, not re-sleeping full 10s).
        assert elapsed < 2.0, f"took {elapsed:.2f}s — looks like infinite re-sleep"
    finally:
        _current_workflow.set(None)

    # FINISHED must be written to the persisted log.
    reopened = EventLog.load(run_id, runs_dir=str(tmp_path))
    sleep_events = [e for e in reopened.all_events() if e.op == "det.sleep"]
    statuses = [e.status.value for e in sleep_events]
    assert "FINISHED" in statuses, f"FINISHED not in log after resume: {statuses}"


# ---------------------------------------------------------------------------
# CRITICAL-2: backoff validation in retry()
# ---------------------------------------------------------------------------

def test_retry_negative_backoff_seconds_raises():
    """@retry(3, backoff_seconds=-1) raises ValueError at decoration time."""
    from godel._decorators import retry, step, WorkflowFail
    with pytest.raises(ValueError, match="backoff_seconds"):
        @retry(3, backoff_seconds=-1)
        @step
        async def flaky():
            raise WorkflowFail("fail")


def test_retry_negative_backoff_multiplier_raises():
    """@retry(3, backoff_multiplier=-2) raises ValueError at decoration time."""
    from godel._decorators import retry, step, WorkflowFail
    with pytest.raises(ValueError, match="backoff_multiplier"):
        @retry(3, backoff_multiplier=-2)
        @step
        async def flaky():
            raise WorkflowFail("fail")
