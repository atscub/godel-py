"""Tests for agent context overflow detection and auto-recovery."""
import asyncio
import json
from unittest.mock import patch

import pytest

from godel.agents._claude import claude_code, _ClaudeCodeAgent
from godel.agents._copilot import copilot, _CopilotAgent
from godel._run import CommandResult, CommandFailure
from godel._decorators import workflow


# ---------------------------------------------------------------------------
# _is_context_overflow detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stderr_msg", [
    "Error: prompt is too long for this model",
    "Error: context window exceeded",
    "context length exceeded: 200000 > 128000",
    "token limit reached",
    "Error: max_tokens exceeded",
    "maximum context length exceeded",
    "conversation is too long to continue",
    "Error: input is too long",
])
def test_claude_detects_context_overflow(stderr_msg):
    agent = _ClaudeCodeAgent(model="sonnet", cwd=None, tools=None, skip_permissions=False)
    error = CommandFailure("command failed", stderr=stderr_msg, stdout="", returncode=1)
    assert agent._is_context_overflow(error)


@pytest.mark.parametrize("stderr_msg", [
    "Error: prompt is too long for this model",
    "context window exceeded",
    "conversation is too long to continue",
    "Error: input is too long",
])
def test_copilot_detects_context_overflow(stderr_msg):
    agent = _CopilotAgent(model="default", cwd=None, tools=None, skip_permissions=False)
    error = CommandFailure("command failed", stderr=stderr_msg, stdout="", returncode=1)
    assert agent._is_context_overflow(error)


def test_non_overflow_error_not_detected():
    agent = _ClaudeCodeAgent(model="sonnet", cwd=None, tools=None, skip_permissions=False)
    error = CommandFailure("command failed", stderr="network timeout", stdout="", returncode=1)
    assert not agent._is_context_overflow(error)


def test_overflow_detected_in_stdout():
    """Overflow signal may appear in stdout instead of stderr."""
    agent = _ClaudeCodeAgent(model="sonnet", cwd=None, tools=None, skip_permissions=False)
    error = CommandFailure("command failed", stderr="", stdout="context window exceeded", returncode=1)
    assert agent._is_context_overflow(error)


# ---------------------------------------------------------------------------
# Auto-recovery in _invoke()
# ---------------------------------------------------------------------------

def test_context_overflow_triggers_fresh_session_retry():
    """On context overflow, agent clears session and retries with a fresh one."""
    call_count = 0

    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise CommandFailure(
                "context overflow",
                stderr="prompt is too long",
                stdout="",
                returncode=1,
            )
        return CommandResult(
            stdout=json.dumps({"result": "recovered", "session_id": "new-session"}),
            stderr="",
            returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=fake_run):
            agent = claude_code()
            agent._session_id = "old-session"
            result = await agent("test prompt")
            assert result == "recovered"
            assert agent._session_id == "new-session"
            assert call_count == 2

    asyncio.run(wf())


def test_non_overflow_error_propagates():
    """Non-overflow CommandFailure is not retried."""
    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        raise CommandFailure(
            "network error",
            stderr="connection refused",
            stdout="",
            returncode=1,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=fake_run):
            agent = claude_code()
            agent._session_id = "some-session"
            with pytest.raises(CommandFailure, match="network error"):
                await agent("test prompt")

    asyncio.run(wf())


def test_overflow_without_session_propagates():
    """Context overflow with no active session is not retried (no session to clear)."""
    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        raise CommandFailure(
            "overflow",
            stderr="prompt is too long",
            stdout="",
            returncode=1,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=fake_run):
            agent = claude_code()
            assert agent._session_id is None
            with pytest.raises(CommandFailure):
                await agent("test prompt")

    asyncio.run(wf())


def test_system_prompt_redelivered_after_overflow_recovery():
    """After session reset, system prompt is re-sent on the retry call."""
    prompts_seen = []

    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        prompt_idx = cmd.index("-p") + 1
        prompts_seen.append(cmd[prompt_idx])
        if len(prompts_seen) == 1:
            raise CommandFailure(
                "overflow",
                stderr="prompt is too long",
                stdout="",
                returncode=1,
            )
        return CommandResult(
            stdout=json.dumps({"result": "ok", "session_id": "new"}),
            stderr="",
            returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=fake_run):
            agent = claude_code(system_prompt="You are a test agent.")
            agent._session_id = "old-session"
            agent._system_prompt_sent = True
            await agent("do something")
            assert not agent._system_prompt_sent or len(prompts_seen) == 2

    asyncio.run(wf())


def test_retry_does_not_pass_old_session_id():
    """The retry call must not include --resume with the old session."""
    cmds_seen = []

    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        cmds_seen.append(list(cmd))
        if len(cmds_seen) == 1:
            raise CommandFailure(
                "overflow",
                stderr="context window exceeded",
                stdout="",
                returncode=1,
            )
        return CommandResult(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=fake_run):
            agent = claude_code()
            agent._session_id = "old-session-123"
            await agent("test")
            # First call should have --resume
            assert "--resume" in cmds_seen[0]
            # Retry call should NOT have --resume
            assert "--resume" not in cmds_seen[1]

    asyncio.run(wf())
