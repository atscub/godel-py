"""Tests for agent.call event instrumentation."""
import asyncio
import json
from unittest.mock import patch, AsyncMock
from godel._decorators import workflow
from godel.agents._claude import claude_code
from godel._run import CommandResult, CommandFailure
import pytest


def _mock_run_result(stdout='{"result": "test response"}', returncode=0):
    return CommandResult(stdout=stdout, stderr="", returncode=returncode)


def test_agent_call_emits_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._claude.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _mock_run_result()
            return await agent("Write hello world")

    result = asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    assert len(agent_events) >= 1
    started = [e for e in agent_events if e["status"] == "STARTED"]
    assert len(started) == 1
    assert started[0]["request"]["model"] == "sonnet"
    assert "hello world" in started[0]["request"]["prompt"].lower()


def test_agent_call_emits_finished(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._claude.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _mock_run_result()
            return await agent("test prompt")

    asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    finished = [e for e in agent_events if e["status"] == "FINISHED"]
    assert len(finished) == 1
    assert "type" in finished[0]["response"]


def test_agent_call_emits_failed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._claude.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = CommandFailure("claude failed", returncode=1)
            return await agent("test prompt")

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    failed = [e for e in agent_events if e["status"] == "FAILED"]
    assert len(failed) == 1


def test_agent_call_cancelled_emits_failed(tmp_path, monkeypatch):
    """CancelledError must emit FAILED and must not leave event stuck in STARTED."""
    monkeypatch.chdir(tmp_path)

    # Set up the workflow context so the event log is initialised, then
    # cancel the agent task while it is blocked inside _invoke.
    async def _run():
        from godel._decorators import workflow
        from godel.agents._claude import claude_code

        ready = asyncio.Event()
        cancel_done = asyncio.Event()

        @workflow
        async def wf():
            agent = claude_code(model="sonnet")

            async def _blocking_run(*args, **kwargs):
                ready.set()
                # Block until cancelled — simulates a slow CLI call.
                await asyncio.sleep(999)

            with patch("godel.agents._claude.run", side_effect=_blocking_run):
                return await agent("test prompt")

        task = asyncio.create_task(wf())
        # Wait until the agent is actually blocked inside _invoke, then cancel.
        await ready.wait()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "expected at least one run log file"
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]

    # There must be a FAILED event for the cancelled call.
    failed = [e for e in agent_events if e["status"] == "FAILED"]
    assert len(failed) >= 1, "expected FAILED event for cancelled agent.call"

    # The FAILED event must record CancelledError as the error type.
    # error_type is nested inside the response dict emitted by emit_failed.
    assert any(
        "CancelledError" in (e.get("response") or {}).get("error_type", "")
        for e in failed
    ), "FAILED event should name CancelledError"

    # No agent.call event may be left in STARTED (each STARTED must have a matching close).
    started_ids = {e["event_id"] for e in agent_events if e["status"] == "STARTED"}
    closed_ids = {
        e["event_id"]
        for e in agent_events
        if e["status"] in ("FINISHED", "FAILED")
    }
    stuck = started_ids - closed_ids
    assert not stuck, f"agent.call events stuck in STARTED: {stuck}"
