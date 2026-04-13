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
