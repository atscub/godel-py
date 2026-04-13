"""Tests for the copilot agent factory.

Mirrors the patterns in test_agents.py / test_agent_events.py.
All tests mock godel._run.run — no real CLI is invoked.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

import godel.agents
from godel.agents._copilot import copilot, _CopilotAgent, _EXTRACTION_MODEL
from godel.agents._common import SchemaValidationFailure
from godel._run import CommandResult, CommandFailure
from godel._decorators import workflow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MyModel(BaseModel):
    value: int


def _mock_run_returning(stdout: str):
    """Return a mock for run() that always yields the given stdout."""
    async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        return CommandResult(stdout=stdout, stderr="", returncode=0)
    return fake_run


def _mock_run_side_effect(exc):
    """Return a mock for run() that always raises exc."""
    async def failing_run(cmd, *, cwd=None, timeout=None, idempotent=False):
        raise exc
    return failing_run


# ---------------------------------------------------------------------------
# Factory / construction
# ---------------------------------------------------------------------------

def test_copilot_returns_agent():
    agent = copilot()
    assert isinstance(agent, _CopilotAgent)


def test_copilot_factory_defaults():
    agent = copilot()
    assert agent._model == "default"
    assert agent._cwd is None
    assert agent._tools is None
    assert agent._skip_permissions is False


def test_copilot_factory_kwargs():
    agent = copilot(model="sonnet", cwd="/tmp", tools=["bash"], skip_permissions=True)
    assert agent._model == "sonnet"
    assert agent._cwd == "/tmp"
    assert agent._tools == ["bash"]
    assert agent._skip_permissions is True


# ---------------------------------------------------------------------------
# CRITICAL-2: _CopilotAgent not exported from godel.agents namespace
# ---------------------------------------------------------------------------

def test_copilot_agent_class_not_in_public_namespace():
    """_CopilotAgent must NOT be exported from godel.agents (leading-underscore private)."""
    assert not hasattr(godel.agents, "_CopilotAgent"), (
        "_CopilotAgent should not be in godel.agents.__init__ exports"
    )


# ---------------------------------------------------------------------------
# Basic delegation through run()
# ---------------------------------------------------------------------------

def test_copilot_delegates_to_run():
    """copilot() calls run(), not subprocess directly."""
    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=_mock_run_returning("hello world")):
            agent = copilot()
            result = await agent("say hello")
            assert result == "hello world"

    asyncio.run(wf())


def test_copilot_passes_cwd_to_run():
    """run() is called with the cwd from the factory."""
    captured: list[dict] = []

    async def capturing_run(cmd, *, cwd=None, **kwargs):
        captured.append({"cmd": cmd, "cwd": cwd})
        return CommandResult(stdout="ok", stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capturing_run):
            agent = copilot(cwd="/workspace")
            await agent("do something")

    asyncio.run(wf())
    assert captured[0]["cwd"] == "/workspace"


# ---------------------------------------------------------------------------
# Command-line construction
# ---------------------------------------------------------------------------

def test_copilot_model_alias_in_command():
    """Model aliases are resolved to real model IDs in the CLI command."""
    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(stdout="ok", stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot(model="sonnet")
            await agent("test")

    asyncio.run(wf())
    assert "claude-sonnet-4.5" in cmds[0]


def test_copilot_default_model_in_command():
    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(stdout="ok", stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot(model="default")
            await agent("test")

    asyncio.run(wf())
    assert "gpt-5" in cmds[0]


def test_copilot_tools_in_command():
    """--allow-tool flags appear when tools are specified."""
    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(stdout="ok", stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot(tools=["bash", "python"])
            await agent("test")

    asyncio.run(wf())
    assert "--allow-tool" in cmds[0]
    assert "bash" in cmds[0]
    assert "python" in cmds[0]


def test_copilot_no_allow_all_tools_by_default():
    """--allow-all-tools is NOT in the command unless skip_permissions=True."""
    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(stdout="ok", stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot()
            await agent("test")

    asyncio.run(wf())
    assert "--allow-all-tools" not in cmds[0]


# ---------------------------------------------------------------------------
# CRITICAL-2: skip_permissions=True forwarded to extraction fallback
# ---------------------------------------------------------------------------

def test_skip_permissions_adds_allow_all_tools():
    """skip_permissions=True → --allow-all-tools appears in every run() call."""
    cmds: list[str] = []
    call_count = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call_count
        cmds.append(cmd)
        call_count += 1
        # First call returns natural-language text; second call is the
        # extraction fallback.
        if call_count == 1:
            return CommandResult(stdout="The answer is forty two.", stderr="", returncode=0)
        # Extraction fallback returns valid JSON.
        return CommandResult(stdout='{"value": 42}', stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot(skip_permissions=True)
            return await agent("give me a number", schema=MyModel)

    result = asyncio.run(wf())
    assert isinstance(result, MyModel)
    assert result.value == 42
    # Both the primary call and the extraction fallback must carry --allow-all-tools.
    assert len(cmds) == 2, f"Expected 2 run() calls, got {len(cmds)}"
    for cmd in cmds:
        assert "--allow-all-tools" in cmd, (
            f"--allow-all-tools missing from command: {cmd!r}"
        )


def test_skip_permissions_false_no_allow_all_tools_in_fallback():
    """skip_permissions=False → --allow-all-tools absent from the fallback call too."""
    cmds: list[str] = []
    call_count = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call_count
        cmds.append(cmd)
        call_count += 1
        if call_count == 1:
            return CommandResult(stdout="The answer is forty two.", stderr="", returncode=0)
        return CommandResult(stdout='{"value": 42}', stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot(skip_permissions=False)
            return await agent("give me a number", schema=MyModel)

    asyncio.run(wf())
    assert len(cmds) == 2
    for cmd in cmds:
        assert "--allow-all-tools" not in cmd


# ---------------------------------------------------------------------------
# NIT-1: skip_permissions=True combined with tools=[...] — verify extraction
# call does NOT forward tools flags, but does forward --allow-all-tools.
# ---------------------------------------------------------------------------

def test_skip_permissions_with_tools_extraction_has_no_tool_flags():
    """skip_permissions=True + tools=['bash'] → primary call has both
    --allow-all-tools AND --allow-tool bash; extraction fallback has
    --allow-all-tools but NOT --allow-tool bash.

    The extraction call is a pure JSON-parsing prompt — it needs no file tools.
    This test guards against a future refactor that accidentally forwards
    self._tools into the extraction command.
    """
    cmds: list[str] = []
    call_count = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call_count
        cmds.append(cmd)
        call_count += 1
        if call_count == 1:
            # Return natural language to force the extraction fallback.
            return CommandResult(stdout="The value is 7.", stderr="", returncode=0)
        return CommandResult(stdout='{"value": 7}', stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot(skip_permissions=True, tools=["bash"])
            return await agent("give me a number", schema=MyModel)

    result = asyncio.run(wf())
    assert isinstance(result, MyModel)
    assert result.value == 7
    assert len(cmds) == 2, f"Expected 2 run() calls, got {len(cmds)}"

    primary_cmd, extraction_cmd = cmds[0], cmds[1]

    # Primary: has both --allow-all-tools and --allow-tool bash
    assert "--allow-all-tools" in primary_cmd, (
        f"--allow-all-tools missing from primary command: {primary_cmd!r}"
    )
    assert "--allow-tool" in primary_cmd, (
        f"--allow-tool missing from primary command: {primary_cmd!r}"
    )
    assert "bash" in primary_cmd

    # Extraction: has --allow-all-tools but NOT --allow-tool flags
    assert "--allow-all-tools" in extraction_cmd, (
        f"--allow-all-tools missing from extraction command: {extraction_cmd!r}"
    )
    assert "--allow-tool" not in extraction_cmd, (
        f"--allow-tool should NOT be in extraction command: {extraction_cmd!r}"
    )


# ---------------------------------------------------------------------------
# Schema coercion
# ---------------------------------------------------------------------------

def test_copilot_schema_raw_json():
    """Direct JSON in stdout → parsed and validated."""
    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=_mock_run_returning('{"value": 7}')):
            agent = copilot()
            result = await agent("give me a number", schema=MyModel)
            assert isinstance(result, MyModel)
            assert result.value == 7

    asyncio.run(wf())


def test_copilot_schema_fenced_json():
    """JSON inside a markdown fence → extracted and validated."""
    fenced = "Here is your answer:\n```json\n{\"value\": 13}\n```\nDone."

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=_mock_run_returning(fenced)):
            agent = copilot()
            result = await agent("give me a number", schema=MyModel)
            assert isinstance(result, MyModel)
            assert result.value == 13

    asyncio.run(wf())


def test_copilot_schema_fallback_extraction():
    """Natural-language response → extraction fallback → parsed successfully."""
    call_count = 0

    async def two_step_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return CommandResult(stdout="The value is 99.", stderr="", returncode=0)
        # Extraction call returns clean JSON.
        return CommandResult(stdout='{"value": 99}', stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=two_step_run):
            agent = copilot()
            result = await agent("give me a number", schema=MyModel)
            assert isinstance(result, MyModel)
            assert result.value == 99

    asyncio.run(wf())
    assert call_count == 2  # primary + extraction fallback


def test_copilot_schema_fallback_uses_extraction_model():
    """The extraction fallback call uses _EXTRACTION_MODEL."""
    cmds: list[str] = []
    call_count = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call_count
        cmds.append(cmd)
        call_count += 1
        if call_count == 1:
            return CommandResult(stdout="some natural language", stderr="", returncode=0)
        return CommandResult(stdout='{"value": 1}', stderr="", returncode=0)

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot()
            await agent("test", schema=MyModel)

    asyncio.run(wf())
    assert len(cmds) == 2
    assert _EXTRACTION_MODEL in cmds[1], (
        f"Extraction fallback must use {_EXTRACTION_MODEL!r}, got: {cmds[1]!r}"
    )


def test_copilot_schema_total_failure_raises():
    """When all coercion strategies fail, SchemaValidationFailure is raised."""
    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=_mock_run_returning("not json at all")):
            agent = copilot()
            with pytest.raises(SchemaValidationFailure):
                await agent("give me a number", schema=MyModel)

    asyncio.run(wf())


# ---------------------------------------------------------------------------
# NIT-3: shlex.quote on complex tool names with special shell characters
# ---------------------------------------------------------------------------

def test_complex_tool_name_is_shell_quoted():
    """Tool names with shell-special characters (parens, globs) are correctly
    quoted so the CLI receives them verbatim.

    shlex.quote wraps the value in single quotes, e.g.
    ``shell(git:*)`` → ``'shell(git:*)'``.  Verify the raw token appears
    in the command string in its shlex-quoted form.
    """
    import shlex

    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(stdout="ok", stderr="", returncode=0)

    complex_tool = "shell(git:*)"

    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=capture_run):
            agent = copilot(tools=[complex_tool])
            await agent("test")

    asyncio.run(wf())
    assert len(cmds) == 1
    quoted_tool = shlex.quote(complex_tool)  # → "'shell(git:*)'"
    assert quoted_tool in cmds[0], (
        f"Expected shell-quoted tool {quoted_tool!r} in command: {cmds[0]!r}"
    )
    # The unquoted form must NOT appear bare (would be unsafe).
    assert complex_tool not in cmds[0].replace(quoted_tool, ""), (
        f"Unquoted tool name found in command: {cmds[0]!r}"
    )


# ---------------------------------------------------------------------------
# CRITICAL-1: type-identity — SchemaValidationFailure must be the SAME class
# ---------------------------------------------------------------------------

def test_schema_validation_failure_type_identity():
    """isinstance check across godel.agents namespace must work.

    This is the regression guard for CRITICAL-1: a local SchemaValidationFailure
    defined in _copilot.py would have a different type identity from
    godel.agents.SchemaValidationFailure and break user code that does:
        except godel.agents.SchemaValidationFailure:
    """
    @workflow
    async def wf():
        with patch("godel.agents._copilot.run", new=_mock_run_returning("not json")):
            agent = copilot()
            try:
                await agent("test", schema=MyModel)
            except godel.agents.SchemaValidationFailure as exc:
                return exc
            pytest.fail("SchemaValidationFailure was not raised")

    exc = asyncio.run(wf())
    assert isinstance(exc, godel.agents.SchemaValidationFailure), (
        "SchemaValidationFailure raised by copilot agent must be the same class "
        "as godel.agents.SchemaValidationFailure"
    )
    # Also verify it carries the raw text.
    assert isinstance(exc.raw, str)


# ---------------------------------------------------------------------------
# Error path — emit_failed
# ---------------------------------------------------------------------------

def test_copilot_error_path_emits_failed(tmp_path, monkeypatch):
    """A run() failure → event log records a FAILED entry."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = copilot(model="default")
        with patch(
            "godel.agents._copilot.run",
            new=AsyncMock(side_effect=CommandFailure("copilot failed", returncode=1)),
        ):
            return await agent("test prompt")

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "No event log written"
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    failed = [e for e in agent_events if e["status"] == "FAILED"]
    assert len(failed) == 1
    # error_type lives inside the response dict, not at the top level of the event.
    assert failed[0]["response"]["error_type"] == "CommandFailure"


def test_copilot_success_emits_started_finished(tmp_path, monkeypatch):
    """Successful call → event log has paired STARTED / FINISHED entries."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = copilot(model="default")
        with patch(
            "godel.agents._copilot.run",
            new=AsyncMock(
                return_value=CommandResult(stdout="hello", stderr="", returncode=0)
            ),
        ):
            return await agent("say hello")

    asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    started = [e for e in agent_events if e["status"] == "STARTED"]
    finished = [e for e in agent_events if e["status"] == "FINISHED"]
    assert len(started) == 1
    assert len(finished) == 1
    assert started[0]["request"]["model"] == "default"
