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
    import json
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


# ---------------------------------------------------------------------------
# CRITICAL-1: inner @step must emit FAILED (not stay STARTED) on outer timeout
# ---------------------------------------------------------------------------

def test_timeout_cancels_inner_step_emits_failed(tmp_path, monkeypatch):
    """Outer @step(timeout=0.1) cancels inner @step(sleep=1s); inner step must
    have a FAILED event in the log — not an orphaned STARTED entry."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GODEL_RUNS_DIR", str(tmp_path / "runs"))

    @step
    async def inner_step():
        await asyncio.sleep(1.0)

    @step(timeout=0.1)
    async def outer_step():
        await inner_step()

    @workflow
    async def wf():
        await outer_step()

    with pytest.raises(StepTimeout):
        asyncio.run(wf())

    import json
    runs_dir = tmp_path / "runs"
    log_files = list(runs_dir.glob("*.jsonl"))
    assert log_files, "Expected at least one .jsonl log file"

    events = []
    for log_file in log_files:
        for line in log_file.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))

    # Find all step.enter events for the inner step.
    # The request field uses "name" (not "step_name") for step events.
    inner_events = [
        e for e in events
        if e.get("op") == "step.enter"
        and e.get("request", {}).get("name") == "inner_step"
    ]
    assert inner_events, "Expected step.enter events for inner_step"

    # The inner step must NOT be left with only a STARTED status —
    # it must have a corresponding FAILED event.
    failed_inner = [e for e in inner_events if e.get("status") == "FAILED"]
    assert failed_inner, (
        "inner_step must emit a FAILED event when cancelled by outer timeout; "
        f"got statuses: {[e.get('status') for e in inner_events]}"
    )
    assert failed_inner[0].get("response", {}).get("error_type") == "Cancelled"


# ---------------------------------------------------------------------------
# CRITICAL-2: run() must emit FAILED on CancelledError from outer timeout
# ---------------------------------------------------------------------------

def test_timeout_cancels_run_emits_failed(tmp_path, monkeypatch):
    """@step(timeout=0.1) that calls run(['sleep', '5']) must emit a FAILED
    event for the run() with error_type='Cancelled'."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GODEL_RUNS_DIR", str(tmp_path / "runs"))

    from godel._run import run

    @step(timeout=0.1)
    async def step_with_run():
        await run("sleep 5")

    @workflow
    async def wf():
        await step_with_run()

    with pytest.raises(StepTimeout):
        asyncio.run(wf())

    import json
    runs_dir = tmp_path / "runs"
    log_files = list(runs_dir.glob("*.jsonl"))
    assert log_files, "Expected at least one .jsonl log file"

    events = []
    for log_file in log_files:
        for line in log_file.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))

    # Find run events
    run_events = [e for e in events if e.get("op") == "run"]
    assert run_events, "Expected at least one run event"

    # The run() call must have a FAILED event with error_type='Cancelled'
    failed_run_events = [
        e for e in run_events
        if e.get("status") == "FAILED"
        and e.get("response", {}).get("error_type") == "Cancelled"
    ]
    assert failed_run_events, (
        "run() must emit FAILED with error_type='Cancelled' when cancelled by "
        f"outer step timeout; got run event statuses: "
        f"{[e.get('status') for e in run_events]}"
    )
