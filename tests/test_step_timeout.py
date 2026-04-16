"""Tests for @step(timeout=N) — per-step wall-clock cancellation."""
import asyncio
import pytest
from godel._decorators import workflow, step, retry, WorkflowFail
from godel._exceptions import StepTimeout


# ---------------------------------------------------------------------------
# Basic timeout behaviour
# ---------------------------------------------------------------------------

def test_step_timeout_raises_step_timeout():
    """A step body that exceeds the timeout raises StepTimeout."""

    @step(timeout=0.05)
    async def slow_step():
        await asyncio.sleep(10)

    @workflow
    async def wf():
        await slow_step()

    with pytest.raises(StepTimeout) as exc_info:
        asyncio.run(wf())

    err = exc_info.value
    assert err.step_name == "slow_step"
    assert err.timeout_seconds == 0.05


def test_step_timeout_message():
    """StepTimeout message mentions the step name and timeout duration."""

    @step(timeout=0.05)
    async def slow():
        await asyncio.sleep(10)

    @workflow
    async def wf():
        await slow()

    with pytest.raises(StepTimeout, match="slow"):
        asyncio.run(wf())


def test_step_finishes_before_timeout_ok():
    """A step that finishes before the timeout behaves normally."""

    @step(timeout=5.0)
    async def fast_step():
        return 42

    @workflow
    async def wf():
        return await fast_step()

    assert asyncio.run(wf()) == 42


def test_step_no_timeout_default_behavior():
    """Omitting timeout preserves existing behavior (no cancellation)."""

    @step
    async def normal_step():
        return "ok"

    @workflow
    async def wf():
        return await normal_step()

    assert asyncio.run(wf()) == "ok"


# ---------------------------------------------------------------------------
# StepTimeout is a GodelError subclass
# ---------------------------------------------------------------------------

def test_step_timeout_is_godel_error():
    from godel._exceptions import GodelError

    err = StepTimeout("msg", step_name="foo", timeout_seconds=1.0)
    assert isinstance(err, GodelError)
    assert err.step_name == "foo"
    assert err.timeout_seconds == 1.0


# ---------------------------------------------------------------------------
# Event log records FAILED with error_type=StepTimeout
# ---------------------------------------------------------------------------

def test_step_timeout_emits_failed_event(tmp_path, monkeypatch):
    """Timed-out step is recorded as FAILED with error_type='StepTimeout'."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GODEL_RUNS_DIR", str(tmp_path / "runs"))

    @step(timeout=0.05)
    async def slow():
        await asyncio.sleep(10)

    @workflow
    async def wf():
        await slow()

    with pytest.raises(StepTimeout):
        asyncio.run(wf())

    # Inspect the event log
    run_id = wf._last_run_id
    import json
    from pathlib import Path
    runs_dir = tmp_path / "runs"
    log_files = list(runs_dir.glob("*.jsonl"))
    assert log_files, "Expected at least one .jsonl log file"

    events = []
    for log_file in log_files:
        for line in log_file.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))

    step_events = [e for e in events if e.get("op") == "step.enter"]
    failed_step_events = [
        e for e in step_events
        if e.get("status") == "FAILED"
    ]
    assert failed_step_events, "Expected a FAILED step.enter event"

    failed = failed_step_events[0]
    response = failed.get("response", {})
    assert response.get("error_type") == "StepTimeout", (
        f"Expected error_type='StepTimeout', got {response.get('error_type')!r}"
    )


# ---------------------------------------------------------------------------
# @retry composes with @step(timeout=N)
# ---------------------------------------------------------------------------

def test_step_timeout_propagates_through_retry():
    """StepTimeout propagates cleanly through @retry (not swallowed or mangled)."""

    @retry(times=3)
    @step(timeout=0.05)
    async def slow_retried():
        await asyncio.sleep(10)

    @workflow
    async def wf():
        await slow_retried()

    # StepTimeout is not a WorkflowFail so retry does not catch it;
    # it should propagate unchanged.
    with pytest.raises(StepTimeout):
        asyncio.run(wf())


def test_step_with_timeout_and_retry_on_workflow_fail():
    """@retry(times=N) retries WorkflowFail raised inside a timed step normally."""
    call_count = 0

    @retry(times=3)
    @step(timeout=5.0)
    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise WorkflowFail("not yet")
        return "done"

    @workflow
    async def wf():
        return await flaky()

    result = asyncio.run(wf())
    assert result == "done"
    assert call_count == 3


# ---------------------------------------------------------------------------
# StepTimeout is exported from the public godel namespace
# ---------------------------------------------------------------------------

def test_step_timeout_exported_from_godel():
    import godel
    assert hasattr(godel, "StepTimeout")
    assert godel.StepTimeout is StepTimeout
