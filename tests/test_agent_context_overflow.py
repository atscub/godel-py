"""Tests for agent context overflow detection, ContextOverflowError, and compact()."""
import asyncio
from unittest.mock import patch

import pytest

from godel.agents._claude import claude_code, _ClaudeCodeAgent
from godel.agents._copilot import copilot, _CopilotAgent
from godel._run import CommandFailure, ContextOverflowError
from godel._decorators import workflow


# ---------------------------------------------------------------------------
# ContextOverflowError inheritance
# ---------------------------------------------------------------------------

def test_context_overflow_is_command_failure():
    """ContextOverflowError is catchable as CommandFailure."""
    err = ContextOverflowError("overflow", model="sonnet")
    assert isinstance(err, CommandFailure)
    assert isinstance(err, Exception)


def test_context_overflow_caught_by_command_failure_handler():
    """except CommandFailure catches ContextOverflowError."""
    with pytest.raises(CommandFailure):
        raise ContextOverflowError("overflow", model="sonnet")


def test_context_overflow_carries_fields():
    err = ContextOverflowError(
        "overflow",
        model="sonnet",
        session_id="sess-123",
        stdout="out",
        stderr="err",
        returncode=1,
    )
    assert err.model == "sonnet"
    assert err.session_id == "sess-123"
    assert err.stdout == "out"
    assert err.stderr == "err"
    assert err.returncode == 1


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
# ContextOverflowError raised by _invoke()
# ---------------------------------------------------------------------------

def test_overflow_raises_context_overflow_error():
    """Context overflow raises ContextOverflowError, not bare CommandFailure."""
    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        raise CommandFailure(
            "context overflow",
            stderr="prompt is too long",
            stdout="",
            returncode=1,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=fake_run):
            agent = claude_code()
            agent._session_id = "old-session"
            with pytest.raises(ContextOverflowError) as exc_info:
                await agent("test prompt")
            assert exc_info.value.model == "sonnet"
            assert exc_info.value.session_id == "old-session"
            assert exc_info.value.__cause__ is not None

    asyncio.run(wf())


def test_non_overflow_error_propagates_as_command_failure():
    """Non-overflow CommandFailure is not wrapped."""
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
            with pytest.raises(CommandFailure) as exc_info:
                await agent("test prompt")
            assert not isinstance(exc_info.value, ContextOverflowError)

    asyncio.run(wf())


def test_overflow_without_session_still_raises():
    """Context overflow with no active session still raises ContextOverflowError."""
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
            with pytest.raises(ContextOverflowError) as exc_info:
                await agent("test prompt")
            assert exc_info.value.session_id is None

    asyncio.run(wf())


# ---------------------------------------------------------------------------
# compact()
# ---------------------------------------------------------------------------

def test_compact_not_implemented_on_claude():
    """compact() is not yet implemented — raises NotImplementedError."""
    @workflow
    async def wf():
        agent = claude_code()
        with pytest.raises(NotImplementedError, match="does not implement compact"):
            await agent.compact()

    asyncio.run(wf())


def test_compact_not_implemented_on_copilot():
    @workflow
    async def wf():
        agent = copilot()
        with pytest.raises(NotImplementedError, match="does not implement compact"):
            await agent.compact()

    asyncio.run(wf())
