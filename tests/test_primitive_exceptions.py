"""Tests for M5: primitives raise structured exceptions with godel context markers."""
from __future__ import annotations

import asyncio
import pytest

from godel._run import run, CommandFailure
from godel._decorators import workflow, step
from godel._event_log import EventLog


# ---------------------------------------------------------------------------
# test_command_failure_has_step_path
# ---------------------------------------------------------------------------

def test_command_failure_has_step_path():
    """run('false') inside a @step raises CommandFailure with step_path set."""
    caught = []

    @step
    async def inner_step():
        await run("false")

    @workflow
    async def wf():
        try:
            await inner_step()
        except CommandFailure as exc:
            caught.append(exc)
            raise

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    assert len(caught) == 1
    exc = caught[0]
    # step_path must include the step name
    assert exc.step_path == ("inner_step",), f"step_path was {exc.step_path!r}"
    # context marker must mention the step
    s = str(exc)
    assert "[godel:" in s, f"no context marker in: {s!r}"
    assert "step=inner_step" in s, f"step not in marker: {s!r}"


# ---------------------------------------------------------------------------
# test_failed_event_has_structured_info
# ---------------------------------------------------------------------------

def test_failed_event_has_structured_info():
    """After a @step failure, the step.enter event's response contains structured error info."""
    last_run_id = []

    @step
    async def failing_step():
        await run("false")

    @workflow
    async def wf():
        await failing_step()

    with pytest.raises(Exception):
        asyncio.run(wf())

    # Retrieve the last run's event log
    run_id = wf._last_run_id
    log = EventLog.load(run_id)

    # Find the step.enter event
    step_events = [e for e in log.all_events() if e.op == "step.enter"]
    assert step_events, "No step.enter events found"
    failed_step = next((e for e in step_events if e.status.value == "FAILED"), None)
    assert failed_step is not None, "No FAILED step.enter event found"

    resp = failed_step.response
    assert resp is not None, "FAILED step.enter event has no response"
    assert "error_type" in resp, f"response missing error_type: {resp}"
    assert "step_path" in resp, f"response missing step_path: {resp}"
    assert resp["error_type"] == "CommandFailure"
    assert resp["step_path"] == ["failing_step"]


# ---------------------------------------------------------------------------
# test_command_failure_remediation_hint
# ---------------------------------------------------------------------------

def test_command_failure_remediation_hint():
    """CommandFailure from a non-zero exit has a non-empty remediation_hint."""
    caught = []

    @step
    async def bad_step():
        await run("exit 42")

    @workflow
    async def wf():
        try:
            await bad_step()
        except CommandFailure as exc:
            caught.append(exc)
            raise

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    assert caught
    exc = caught[0]
    assert exc.remediation_hint, "remediation_hint should be non-empty"
    assert exc.returncode == 42


# ---------------------------------------------------------------------------
# test_timeout_failure_has_step_path
# ---------------------------------------------------------------------------

def test_timeout_failure_has_step_path():
    """Timeout CommandFailure carries step_path and remediation_hint."""
    caught = []

    @step
    async def slow_step():
        await run("sleep 10", timeout=0.05)

    @workflow
    async def wf():
        try:
            await slow_step()
        except CommandFailure as exc:
            caught.append(exc)
            raise

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    assert caught
    exc = caught[0]
    assert "timed out" in str(exc).lower()
    assert exc.step_path == ("slow_step",)
    assert exc.remediation_hint, "remediation_hint should be non-empty for timeout"
    assert "timeout" in exc.remediation_hint.lower() or "Increase" in exc.remediation_hint


# ---------------------------------------------------------------------------
# test_nested_step_path_depth
# ---------------------------------------------------------------------------

def test_nested_step_path_depth(tmp_path, monkeypatch):
    """When a step is called inside another step the step_path reflects both names."""
    monkeypatch.chdir(tmp_path)
    caught = []

    @step
    async def inner():
        await run("false")

    @step
    async def outer():
        await inner()

    @workflow
    async def wf():
        try:
            await outer()
        except Exception as exc:
            caught.append(exc)
            raise

    with pytest.raises(Exception):
        asyncio.run(wf())

    assert caught
    exc = caught[0]
    # The exception is re-raised by @step at the inner level, so step_path is
    # ("outer", "inner") at raise time. The outer @step does NOT re-enrich
    # step_path once it is already set, so we see the innermost full path.
    assert exc.step_path == ("outer", "inner"), f"step_path was {exc.step_path!r}"


# ---------------------------------------------------------------------------
# test_parallel_branch_failure_emits_failed_events
# ---------------------------------------------------------------------------

def test_parallel_branch_failure_emits_failed_events(tmp_path, monkeypatch):
    """When a parallel branch raises, FORK and JOIN events are emitted as FAILED
    and carry error_type + source_location in their response."""
    monkeypatch.chdir(tmp_path)
    from godel._decorators import parallel
    from godel._event_log import EventLog

    @step
    async def bad_branch():
        await run("false")

    @step
    async def good_branch():
        return "ok"

    @workflow
    async def wf():
        await parallel(bad_branch(), good_branch())

    with pytest.raises(Exception):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id)
    all_events = log.all_events()
    log.close()

    failed_fork = [e for e in all_events if e.op == "FORK" and e.status.value == "FAILED"]
    failed_join = [e for e in all_events if e.op == "JOIN" and e.status.value == "FAILED"]

    assert failed_fork, "FORK event should be FAILED when a branch raises"
    assert failed_join, "JOIN event should be FAILED when a branch raises"

    # The FAILED response must carry error_type
    fork_resp = failed_fork[0].response
    assert fork_resp is not None, "FAILED FORK event has no response"
    assert "error_type" in fork_resp, f"FORK response missing error_type: {fork_resp}"


# ---------------------------------------------------------------------------
# test_step_exception_without_dict_does_not_raise
# ---------------------------------------------------------------------------

def test_step_exception_without_dict_does_not_raise(tmp_path, monkeypatch):
    """Exceptions whose class uses __slots__ (no __dict__) must not cause the
    @step wrapper to raise AttributeError while trying to attach context fields.
    The original exception should propagate cleanly."""
    monkeypatch.chdir(tmp_path)

    class SlottedException(Exception):
        """Exception with __slots__ — attribute assignment will raise AttributeError."""
        __slots__ = ()

    @step
    async def bad_step():
        raise SlottedException("slots only")

    @workflow
    async def wf():
        await bad_step()

    # Must propagate SlottedException, not an AttributeError from the wrapper
    with pytest.raises(SlottedException, match="slots only"):
        asyncio.run(wf())
