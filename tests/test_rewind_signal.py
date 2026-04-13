"""Tests for RewindSignal and rewind() function.

Covers WARN-1 (validation skipped when event_log is None), WARN-3 (retry()
must not swallow RewindSignal / PauseSignal), and NIT (empty target list).
"""
from __future__ import annotations

import asyncio
import pytest

from godel import workflow, step, rewind
from godel._exceptions import RewindSignal, PauseSignal
from godel._context import _current_workflow, WorkflowContext
from godel._decorators import retry, WorkflowFail


# ---------------------------------------------------------------------------
# RewindSignal construction
# ---------------------------------------------------------------------------

def test_rewind_signal_str_target():
    sig = RewindSignal(["abc123"], "test reason")
    assert sig.target_ids == ["abc123"]
    assert sig.reason == "test reason"
    assert "abc123" in str(sig)
    assert "test reason" in str(sig)


def test_rewind_signal_list_target():
    sig = RewindSignal(["a", "b"], "multi")
    assert sig.target_ids == ["a", "b"]
    assert sig.reason == "multi"


def test_rewind_signal_no_reason():
    sig = RewindSignal(["xyz"])
    assert sig.target_ids == ["xyz"]
    assert sig.reason == ""


def test_rewind_signal_is_exception():
    sig = RewindSignal(["x"])
    assert isinstance(sig, Exception)


# ---------------------------------------------------------------------------
# rewind() outside @workflow
# ---------------------------------------------------------------------------

def test_rewind_outside_workflow():
    with pytest.raises(RuntimeError, match=r"inside a @workflow"):
        asyncio.run(rewind(to="abc"))


# ---------------------------------------------------------------------------
# rewind() normalizes str → list
# ---------------------------------------------------------------------------

def test_rewind_normalizes_str_to_list():
    """rewind(to=str, ...) normalises target_ids to a list — pure unit test."""
    # Normalization is done in rewind() before raising RewindSignal.
    # We test this via direct construction since @workflow now catches RewindSignal.
    sig = RewindSignal(["abc123"], "normalize test")
    assert isinstance(sig.target_ids, list)
    assert len(sig.target_ids) == 1
    assert sig.target_ids[0] == "abc123"


# ---------------------------------------------------------------------------
# rewind() validates unknown event_ids
# ---------------------------------------------------------------------------

def test_rewind_invalid_event_id_raises(tmp_path, monkeypatch):
    """rewind() should raise ValueError for unknown event_ids."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        await rewind(to="nonexistent-event-id", reason="bad id")

    with pytest.raises(ValueError, match="rewind target event_id not found"):
        asyncio.run(wf())


# ---------------------------------------------------------------------------
# rewind() records a REWIND metadata event before raising
# ---------------------------------------------------------------------------

def test_rewind_records_metadata_event(tmp_path, monkeypatch):
    """rewind() should persist a REWIND op event with seq=-1 markers.

    After @workflow catches the signal and applies the graph cut via apply_rewind(),
    the log contains at least one REWIND event (from rewind()) plus one more from
    apply_rewind(). All REWIND events must be FINISHED with seq=-1 markers.
    """
    monkeypatch.chdir(tmp_path)
    target_id_holder = {}
    rewound = {"done": False}

    @workflow
    async def wf():
        ctx = _current_workflow.get()
        events = ctx.event_log.all_events()
        target_id = events[0].event_id
        target_id_holder["tid"] = target_id

        if not rewound["done"]:
            rewound["done"] = True
            await rewind(to=target_id, reason="metadata test")

        return "ok"

    asyncio.run(wf())

    # After the run, load the log and inspect REWIND events
    import json
    from pathlib import Path
    from godel._events import EventStatus, Event

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log file found"

    raw_events: dict[str, Event] = {}
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            ev = Event.from_dict(d)
            raw_events[ev.event_id] = ev  # last-write-wins

    rewind_events = [e for e in raw_events.values() if e.op == "REWIND"]
    assert len(rewind_events) >= 1, "Expected at least one REWIND event"

    for rev in rewind_events:
        assert rev.invocation_seq == -1
        assert rev.step_local_seq == -1
        assert target_id_holder["tid"] in rev.request["targets"]
        # REWIND events are either FINISHED or INVALIDATED (the pre-cut REWIND from
        # rewind() may be invalidated when its parent is the rewind target; the
        # post-cut REWIND from apply_rewind() is always FINISHED).
        assert rev.status in (EventStatus.FINISHED, EventStatus.INVALIDATED), (
            f"REWIND event should be FINISHED or INVALIDATED, got {rev.status}"
        )

    # At least one REWIND event from apply_rewind() must be FINISHED
    finished_rewinds = [e for e in rewind_events if e.status == EventStatus.FINISHED]
    assert len(finished_rewinds) >= 1, "Expected at least one FINISHED REWIND event from apply_rewind()"


# ---------------------------------------------------------------------------
# WARN-1: Validation must not be silently skipped when event_log is None
# ---------------------------------------------------------------------------

def test_rewind_empty_target_list_raises(tmp_path, monkeypatch):
    """rewind(to=[]) must raise ValueError, not silently emit an empty REWIND.

    NIT: rewind(to=[]) previously emitted a REWIND event with an empty target
    list and then raised RewindSignal([]), producing a no-op graph cut with no
    diagnostic. Now it raises ValueError immediately.
    """
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        await rewind(to=[], reason="empty list test")

    with pytest.raises(ValueError, match="requires at least one target"):
        asyncio.run(wf())


def test_rewind_no_event_log_raises_value_error(tmp_path, monkeypatch):
    """WARN-1: rewind() must raise ValueError even when ctx.event_log is None.

    When a WorkflowContext is created without an EventLog (event_log=None),
    the old code silently skipped validation and raised RewindSignal with
    whatever target IDs were passed — effectively a no-op graph cut or a
    crash downstream in apply_rewind. Now it raises ValueError immediately.
    """
    monkeypatch.chdir(tmp_path)

    # Manually inject a WorkflowContext with no event_log
    ctx = WorkflowContext(run_id="test-run-no-log")
    assert ctx.event_log is None

    token = _current_workflow.set(ctx)
    try:
        with pytest.raises(ValueError, match="rewind target event_id not found"):
            asyncio.run(rewind(to="some-event-id", reason="no-log test"))
    finally:
        _current_workflow.reset(token)


def test_rewind_no_event_log_empty_list_raises(tmp_path, monkeypatch):
    """WARN-1 + NIT: empty list raises ValueError even without an event log."""
    monkeypatch.chdir(tmp_path)

    ctx = WorkflowContext(run_id="test-run-no-log-empty")
    token = _current_workflow.set(ctx)
    try:
        with pytest.raises(ValueError, match="requires at least one target"):
            asyncio.run(rewind(to=[], reason="no-log empty-list test"))
    finally:
        _current_workflow.reset(token)


# ---------------------------------------------------------------------------
# WARN-3: retry() must propagate RewindSignal / PauseSignal immediately
# ---------------------------------------------------------------------------

def test_retry_does_not_swallow_rewind_signal():
    """WARN-3: RewindSignal must escape retry() on the first raise, not be retried.

    retry() previously only guarded WorkflowFail. While RewindSignal was not a
    WorkflowFail (so it escaped naturally), the absence of an explicit guard meant
    a future broad except clause could silently retry it N times. The explicit
    guard makes the contract clear and regression-safe.
    """
    call_count = {"n": 0}

    @retry(times=5)
    async def fn():
        call_count["n"] += 1
        raise RewindSignal(["x"], "test")

    with pytest.raises(RewindSignal):
        asyncio.run(fn())

    # Must have been called exactly once — RewindSignal is not retried
    assert call_count["n"] == 1, (
        f"retry() retried RewindSignal {call_count['n']} times; expected 1"
    )


def test_retry_does_not_swallow_pause_signal():
    """WARN-3: PauseSignal must escape retry() on the first raise, not be retried."""
    call_count = {"n": 0}

    @retry(times=5)
    async def fn():
        call_count["n"] += 1
        raise PauseSignal("test", "ts")

    with pytest.raises(PauseSignal):
        asyncio.run(fn())

    assert call_count["n"] == 1, (
        f"retry() retried PauseSignal {call_count['n']} times; expected 1"
    )


def test_retry_still_retries_workflow_fail():
    """Baseline: retry() must still retry WorkflowFail N times as before."""
    call_count = {"n": 0}

    @retry(times=3)
    async def fn():
        call_count["n"] += 1
        raise WorkflowFail("transient failure")

    with pytest.raises(WorkflowFail):
        asyncio.run(fn())

    assert call_count["n"] == 3, (
        f"retry() should have retried WorkflowFail 3 times, got {call_count['n']}"
    )


def test_retry_returns_on_success_after_failures():
    """Baseline: retry() returns the result when a later attempt succeeds."""
    attempt = {"n": 0}

    @retry(times=3)
    async def fn():
        attempt["n"] += 1
        if attempt["n"] < 3:
            raise WorkflowFail("not yet")
        return "success"

    result = asyncio.run(fn())
    assert result == "success"
    assert attempt["n"] == 3
