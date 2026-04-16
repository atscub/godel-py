"""Tests for claude_code agent factory."""
import asyncio
import json
from unittest.mock import AsyncMock, patch
from dataclasses import dataclass
import pytest
from pydantic import BaseModel

from godel.agents._claude import claude_code, SchemaValidationFailure, _ClaudeCodeAgent
from godel._run import CommandResult
from godel._decorators import workflow


class MyModel(BaseModel):
    value: int


def _mock_run_returning(stdout: str):
    """Create a mock for run() that returns a CommandResult with the given stdout."""
    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        return CommandResult(stdout=stdout, stderr="", returncode=0)
    return fake_run


def test_claude_code_returns_agent():
    agent = claude_code()
    assert isinstance(agent, _ClaudeCodeAgent)


def test_claude_code_delegates_to_run():
    """Verify claude_code calls run(), not subprocess directly."""
    response = json.dumps({"result": "hello world"})

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=_mock_run_returning(response)):
            agent = claude_code()
            result = await agent("say hello")
            assert result == "hello world"

    asyncio.run(wf())


def test_claude_code_schema_parsing():
    response = json.dumps({"result": '{"value": 42}'})

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=_mock_run_returning(response)):
            agent = claude_code()
            result = await agent("give me a number", schema=MyModel)
            assert isinstance(result, MyModel)
            assert result.value == 42

    asyncio.run(wf())


def test_claude_code_schema_failure():
    response = json.dumps({"result": "not valid json"})

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=_mock_run_returning(response)):
            agent = claude_code()
            with pytest.raises(SchemaValidationFailure):
                await agent("give me a number", schema=MyModel)

    asyncio.run(wf())


def test_claude_code_model_alias():
    """Verify model aliases are resolved correctly."""
    agent = claude_code(model="opus")
    assert agent._model == "opus"  # stored as alias

    cmds = []
    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(stdout='{"result": "ok"}', stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            await agent("test")

    asyncio.run(wf())
    assert "claude-opus-4-6" in cmds[0]


def test_stub_factories_raise():
    from godel.agents import codex
    with pytest.raises(NotImplementedError):
        codex()


def test_copilot_is_no_longer_a_stub():
    """copilot() is now implemented — it should NOT raise NotImplementedError."""
    from godel.agents import copilot
    from godel.agents._copilot import _CopilotAgent
    agent = copilot()
    assert isinstance(agent, _CopilotAgent)


def test_claude_session_id_captured_and_resumed():
    """First call captures session_id; second call passes --resume <id>."""
    cmds: list[str] = []
    call = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call
        cmds.append(cmd)
        call += 1
        return CommandResult(
            stdout=json.dumps({"result": f"r{call}", "session_id": "sess-xyz"}),
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code()
            await agent("one")
            await agent("two")

    asyncio.run(wf())
    assert "--resume" not in cmds[0]
    assert "--resume sess-xyz" in cmds[1]


def test_agent_serializes_concurrent_calls():
    """An agent instance must serialize calls — session state requires it."""
    in_flight = 0
    max_in_flight = 0

    async def slow_run(cmd, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return CommandResult(
            stdout=json.dumps({"result": "ok", "session_id": "s"}),
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=slow_run):
            agent = claude_code()
            await asyncio.gather(agent("a"), agent("b"), agent("c"))

    asyncio.run(wf())
    assert max_in_flight == 1, (
        f"Agent calls must be serialized; saw {max_in_flight} concurrent run() invocations"
    )


# ---------------------------------------------------------------------------
# system_prompt: set once, not repeated per call
# ---------------------------------------------------------------------------

def test_claude_code_system_prompt_accepted_at_construction():
    """claude_code() accepts system_prompt kwarg without error."""
    agent = claude_code(system_prompt="You are the engineer for ticket X.")
    assert agent._system_prompt == "You are the engineer for ticket X."
    assert agent._system_prompt_sent is False


def test_claude_code_system_prompt_prepended_on_first_call():
    """system_prompt is prepended to the first call's prompt."""
    prompts_sent: list[str] = []

    async def capture_run(cmd, **kwargs):
        prompts_sent.append(cmd)
        return CommandResult(
            stdout='{"result": "done", "session_id": "s1"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(system_prompt="SYSTEM: be concise.")
            await agent("do the task")

    asyncio.run(wf())
    assert len(prompts_sent) == 1
    # The raw command string contains the prompt passed to shlex.quote(), so
    # we can check that the system_prompt and original prompt both appear.
    assert "SYSTEM: be concise." in prompts_sent[0]
    assert "do the task" in prompts_sent[0]


def test_claude_code_system_prompt_not_repeated_on_second_call():
    """system_prompt is NOT prepended on the second call."""
    prompts_sent: list[str] = []
    call = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call
        prompts_sent.append(cmd)
        call += 1
        return CommandResult(
            stdout=f'{{"result": "r{call}", "session_id": "s1"}}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(system_prompt="PREAMBLE")
            await agent("first call")
            await agent("second call")

    asyncio.run(wf())
    assert len(prompts_sent) == 2
    # First call: preamble present
    assert "PREAMBLE" in prompts_sent[0]
    # Second call: preamble absent
    assert "PREAMBLE" not in prompts_sent[1]


def test_claude_code_no_system_prompt_unaffected():
    """When system_prompt is not set, behaviour is unchanged."""
    prompts_sent: list[str] = []

    async def capture_run(cmd, **kwargs):
        prompts_sent.append(cmd)
        return CommandResult(
            stdout='{"result": "done"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code()
            await agent("plain prompt")

    asyncio.run(wf())
    assert len(prompts_sent) == 1
    # Only the original prompt — no preamble inserted.
    assert "plain prompt" in prompts_sent[0]


def test_claude_code_system_prompt_sent_flag_tracks_state():
    """_system_prompt_sent flips True after first call, stays True."""
    call = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call
        call += 1
        return CommandResult(
            stdout=f'{{"result": "r{call}", "session_id": "s"}}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(system_prompt="CHECK")
            assert agent._system_prompt_sent is False
            await agent("call 1")
            assert agent._system_prompt_sent is True
            await agent("call 2")
            assert agent._system_prompt_sent is True

    asyncio.run(wf())
