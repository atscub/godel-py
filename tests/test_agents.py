"""Tests for claude_code agent factory."""
import asyncio
import json
from unittest.mock import AsyncMock, patch
from dataclasses import dataclass
import pytest
from pydantic import BaseModel

from godel.agents._claude import claude_code, SchemaValidationFailure, _ClaudeCodeAgent
from godel._run import CommandResult, CommandFailure
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


# ---------------------------------------------------------------------------
# REVIEW FIX C1: system_prompt must appear in the agent.call event log entry
# for the first call (audit log must record the prompt actually sent to CLI).
# ---------------------------------------------------------------------------

def test_claude_code_system_prompt_recorded_in_event_log(tmp_path, monkeypatch):
    """The first agent.call event logs the *combined* prompt (system + user)."""
    monkeypatch.chdir(tmp_path)

    async def fake_run(cmd, **kwargs):
        return CommandResult(
            stdout='{"result": "ok", "session_id": "s1"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=fake_run):
            agent = claude_code(system_prompt="SYS-BRIEF-XYZ")
            await agent("USER-MSG-ABC")
            await agent("USER-MSG-DEF")

    asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "No event log written"
    events = [json.loads(line) for line in runs[0].read_text().strip().split("\n")]
    agent_started = [
        e for e in events if e["op"] == "agent.call" and e["status"] == "STARTED"
    ]
    assert len(agent_started) == 2
    # First call: system briefing is part of the logged prompt.
    first_prompt = agent_started[0]["request"]["prompt"]
    assert "SYS-BRIEF-XYZ" in first_prompt, (
        f"First agent.call prompt must contain system briefing, got: {first_prompt!r}"
    )
    assert "USER-MSG-ABC" in first_prompt
    # Second call: system briefing is NOT in the logged prompt.
    second_prompt = agent_started[1]["request"]["prompt"]
    assert "SYS-BRIEF-XYZ" not in second_prompt, (
        f"Second agent.call prompt must NOT contain system briefing, got: {second_prompt!r}"
    )
    assert "USER-MSG-DEF" in second_prompt


# ---------------------------------------------------------------------------
# REVIEW FIX C2: if the first CLI call fails, _system_prompt_sent must stay
# False so that retries still carry the briefing.
# ---------------------------------------------------------------------------

def test_claude_code_system_prompt_preserved_across_first_call_failure():
    """First call fails → _system_prompt_sent stays False → retry re-prepends."""
    cmds: list[str] = []
    call = 0

    async def flaky_run(cmd, **kwargs):
        nonlocal call
        cmds.append(cmd)
        call += 1
        if call == 1:
            raise CommandFailure("transient CLI crash", returncode=1)
        return CommandResult(
            stdout='{"result": "ok", "session_id": "s1"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=flaky_run):
            agent = claude_code(system_prompt="RETRY-BRIEF")
            assert agent._system_prompt_sent is False
            with pytest.raises(CommandFailure):
                await agent("first try")
            # Flag must still be False so the briefing travels with the retry.
            assert agent._system_prompt_sent is False
            await agent("retry")
            # Now (and only now) flipped.
            assert agent._system_prompt_sent is True

    asyncio.run(wf())
    assert len(cmds) == 2
    # Both the failed first call and the successful retry carry the briefing.
    assert "RETRY-BRIEF" in cmds[0]
    assert "RETRY-BRIEF" in cmds[1]


# ---------------------------------------------------------------------------
# REVIEW FIX W2: whitespace-only system_prompt is normalised to "no prompt"
# ---------------------------------------------------------------------------

def test_claude_code_whitespace_system_prompt_is_ignored():
    """system_prompt='   ' is treated as no system prompt (no prepend, no flag)."""
    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(
            stdout='{"result": "ok"}', stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(system_prompt="   \n  \t  ")
            assert agent._system_prompt is None
            await agent("real prompt")

    asyncio.run(wf())
    assert len(cmds) == 1
    # The leading whitespace wrapper must NOT appear in the command; only the
    # caller's real prompt text.
    assert "real prompt" in cmds[0]
    # Bare '\n\n' from the prepend formatter should not be present.
    # (We can't easily check for "no whitespace" but we can check the
    # shlex-quoted prompt does not start with whitespace.)
    import shlex
    assert shlex.quote("real prompt") in cmds[0]


def test_claude_code_empty_system_prompt_is_ignored():
    """system_prompt='' is treated as no system prompt."""
    agent = claude_code(system_prompt="")
    assert agent._system_prompt is None


def test_claude_code_system_prompt_is_stripped():
    """Leading / trailing whitespace is stripped at construction."""
    agent = claude_code(system_prompt="  hello world  \n")
    assert agent._system_prompt == "hello world"


# ---------------------------------------------------------------------------
# REVIEW FIX W3: system_prompt works correctly with schema=... (structured)
# ---------------------------------------------------------------------------

def test_claude_code_system_prompt_with_schema_path():
    """system_prompt is prepended exactly once on the first schema-structured call."""
    cmds: list[str] = []
    call = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call
        cmds.append(cmd)
        call += 1
        return CommandResult(
            stdout=json.dumps({"result": '{"value": 42}', "session_id": "s1"}),
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(system_prompt="BRIEF-W3")
            r1 = await agent("first", schema=MyModel)
            r2 = await agent("second", schema=MyModel)
            assert isinstance(r1, MyModel) and r1.value == 42
            assert isinstance(r2, MyModel) and r2.value == 42

    asyncio.run(wf())
    assert len(cmds) == 2
    # First call: carries the briefing AND schema boilerplate.
    assert "BRIEF-W3" in cmds[0]
    assert "JSON" in cmds[0]  # schema boilerplate keyword
    # Second call: briefing absent, schema boilerplate still there.
    assert "BRIEF-W3" not in cmds[1]
    assert "JSON" in cmds[1]


# ---------------------------------------------------------------------------
# REVIEW FIX W4: concurrent calls on a system_prompt agent — exactly ONE
# prepend across all calls (the lock serialises them, and only the first one
# through flips the flag).
# ---------------------------------------------------------------------------

def test_claude_code_system_prompt_concurrent_calls_prepend_once():
    """Three concurrent calls → briefing appears in exactly one command."""
    cmds: list[str] = []

    async def slow_run(cmd, **kwargs):
        cmds.append(cmd)
        await asyncio.sleep(0.005)
        return CommandResult(
            stdout='{"result": "ok", "session_id": "s"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=slow_run):
            agent = claude_code(system_prompt="ONCE-ONLY-BRIEF")
            await asyncio.gather(agent("a"), agent("b"), agent("c"))

    asyncio.run(wf())
    assert len(cmds) == 3
    occurrences = sum("ONCE-ONLY-BRIEF" in c for c in cmds)
    assert occurrences == 1, (
        f"System prompt must appear in exactly one of the three concurrent "
        f"commands; got {occurrences}. Commands: {cmds!r}"
    )


# ---------------------------------------------------------------------------
# REVIEW FIX W1: resumed session (session_id already set) must NOT re-prepend
# the system_prompt even if _system_prompt_sent is False (as it would be after
# workflow resume reconstructs the agent instance from scratch).
# ---------------------------------------------------------------------------

def test_claude_code_system_prompt_not_prepended_when_session_already_set():
    """When _session_id is already populated (resume scenario), system_prompt
    is NOT re-prepended even though _system_prompt_sent is still False."""
    cmds: list[str] = []
    call = 0

    async def capture_run(cmd, **kwargs):
        nonlocal call
        cmds.append(cmd)
        call += 1
        return CommandResult(
            stdout=f'{{"result": "r{call}", "session_id": "resumed-sess"}}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            # Simulate a resumed agent: freshly constructed but already has a
            # session_id from the replay of prior completed calls.
            agent = claude_code(system_prompt="RESUME-BRIEF")
            assert agent._system_prompt_sent is False
            # Inject the session id as the replay would have done.
            agent._session_id = "resumed-sess"

            await agent("post-resume call")

    asyncio.run(wf())
    assert len(cmds) == 1
    # The session already carries the briefing — must NOT be prepended again.
    assert "RESUME-BRIEF" not in cmds[0], (
        f"system_prompt must not be re-prepended when session_id is already set; "
        f"got: {cmds[0]!r}"
    )
    assert "post-resume call" in cmds[0]


# ---------------------------------------------------------------------------
# godel-py-7nq: session_id ctor param + accessor
# ---------------------------------------------------------------------------

def test_claude_code_session_id_ctor_emits_resume_on_first_call():
    """claude_code(session_id='abc') passes --resume abc on the very first call."""
    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(
            stdout='{"result": "ok", "session_id": "abc"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(session_id="abc")
            await agent("continue the task")

    asyncio.run(wf())
    assert len(cmds) == 1
    assert "--resume abc" in cmds[0], (
        f"Expected '--resume abc' in command, got: {cmds[0]!r}"
    )


def test_session_id_property_pre_and_post_call():
    """agent.session_id returns ctor value pre-call and updated id post-call."""
    async def capture_run(cmd, **kwargs):
        return CommandResult(
            stdout='{"result": "ok", "session_id": "new-sess"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(session_id="initial-sess")
            assert agent.session_id == "initial-sess"
            await agent("do something")
            assert agent.session_id == "new-sess"

    asyncio.run(wf())


def test_session_id_property_none_before_any_call():
    """agent.session_id is None when no session_id supplied and no call made."""
    agent = claude_code()
    assert agent.session_id is None


def test_system_prompt_not_prepended_when_session_id_supplied_at_ctor():
    """When session_id is set at ctor time, system_prompt is NOT prepended."""
    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        return CommandResult(
            stdout='{"result": "ok", "session_id": "s99"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(system_prompt="SECRET-BRIEF", session_id="s99")
            # _system_prompt_sent must be True already because session carries it
            assert agent._system_prompt_sent is True
            await agent("task prompt")

    asyncio.run(wf())
    assert len(cmds) == 1
    assert "SECRET-BRIEF" not in cmds[0], (
        f"system_prompt must not be prepended when session_id is supplied; "
        f"got: {cmds[0]!r}"
    )
    assert "task prompt" in cmds[0]


def test_empty_string_session_id_normalised_to_none():
    """Empty and whitespace-only session_id values are normalised to None."""
    agent_empty = claude_code(session_id="")
    assert agent_empty.session_id is None
    assert agent_empty._system_prompt_sent is False  # no session → not pre-sent

    agent_ws = claude_code(session_id="   \t  ")
    assert agent_ws.session_id is None
    assert agent_ws._system_prompt_sent is False


def test_copilot_session_id_ctor_emits_resume_on_first_call():
    """copilot(session_id='cp-sess') passes --resume=cp-sess on the first call."""
    from godel.agents._copilot import copilot

    cmds: list[str] = []

    async def capture_run(cmd, **kwargs):
        cmds.append(cmd)
        # Minimal copilot JSONL response
        return CommandResult(
            stdout='{"type":"result","sessionId":"cp-sess"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = copilot(session_id="cp-sess")
            assert agent.session_id == "cp-sess"
            await agent("continue")

    asyncio.run(wf())
    assert len(cmds) == 1
    assert "--resume=" in cmds[0], (
        f"Expected '--resume=...' in copilot command, got: {cmds[0]!r}"
    )
    assert "cp-sess" in cmds[0]


def test_replay_overrides_ctor_session_id():
    """Workflow replay post-call overwrites the ctor-supplied session_id.

    When the inner run() call replays a FINISHED event it returns the cached
    CLI stdout; _parse_output extracts the session_id from that cached response
    and _invoke stores it on self._session_id — overwriting the ctor value.
    This confirms replay-vs-ctor precedence without a full event-log replay.
    """
    async def capture_run(cmd, **kwargs):
        # CLI returns a different session_id than what was supplied at ctor time
        return CommandResult(
            stdout='{"result": "replayed", "session_id": "replay-sess"}',
            stderr="", returncode=0,
        )

    @workflow
    async def wf():
        with patch("godel.agents._common.run", new=capture_run):
            agent = claude_code(session_id="ctor-sess")
            assert agent.session_id == "ctor-sess"
            await agent("prompt")
            # After the call the CLI-returned id takes precedence
            assert agent.session_id == "replay-sess", (
                f"Expected 'replay-sess' after call, got: {agent.session_id!r}"
            )

    asyncio.run(wf())
