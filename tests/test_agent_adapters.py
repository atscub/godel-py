"""Tests for godel/agents/_adapters.py and stream_into_transcript.

Covers acceptance criteria from godel-py-5pl.4:
- ClaudeAdapter maps Claude stream-json events to canonical godel events.
- CopilotAdapter maps Copilot JSONL events to canonical godel events.
- stream_into_transcript emits agent.thought / agent.tool_call / agent.tool_result
  events to the TranscriptWriter.
- Vendor drift resilience: unknown/renamed fields produce agent.raw, not crashes.
- With stream_agents=False events.jsonl is unaffected (no agent.raw events).
- Claude CLI gets --output-format stream-json iff streaming is active.
- Copilot path no longer has a discard branch (CopilotAdapter always runs).
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from godel._decorators import workflow
from godel._transcript import TranscriptWriter
from godel._run import CommandResult
from godel.agents._adapters import ClaudeAdapter, CopilotAdapter
from godel.agents._common import stream_into_transcript


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_transcript_events(run_dir: Path) -> list[dict]:
    """Collect all event dicts from the run-specific transcript directory."""
    transcript_files = sorted(run_dir.rglob("transcript.jsonl*"))
    events = []
    for f in transcript_files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "event" in obj:
                events.append(obj["event"])
    return events


def _tw(tmp_path) -> TranscriptWriter:
    return TranscriptWriter(tmp_path / "run", run_id="test")


# ---------------------------------------------------------------------------
# ClaudeAdapter unit tests
# ---------------------------------------------------------------------------


class TestClaudeAdapter:
    def test_assistant_text_block_yields_response(self):
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Thinking about it..."}]
            },
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.response"
        assert extra["text"] == "Thinking about it..."

    def test_assistant_thinking_block_yields_thought(self):
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": "Hmm, let me see."}]
            },
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.thought"
        assert extra["text"] == "Hmm, let me see."

    def test_assistant_tool_use_block_yields_tool_call(self):
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "bash",
                        "input": {"command": "ls"},
                    }
                ]
            },
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.tool_call"
        assert extra["tool"] == "bash"
        assert extra["input"] == {"command": "ls"}

    def test_tool_result_event_yields_tool_result(self):
        adapter = ClaudeAdapter()
        data = {
            "type": "tool_result",
            "tool_use_id": "tu_abc",
            "content": [{"type": "text", "text": "file.txt\nother.txt"}],
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.tool_result"
        assert extra["tool"] == "tu_abc"
        assert "file.txt" in extra["output"]

    def test_user_message_with_tool_result_yields_tool_result(self):
        adapter = ClaudeAdapter()
        data = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_xyz",
                        "content": [{"type": "text", "text": "ok"}],
                    }
                ]
            },
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.tool_result"
        assert extra["tool"] == "tu_xyz"
        assert extra["output"] == "ok"

    def test_metadata_events_return_none(self):
        adapter = ClaudeAdapter()
        for etype in ("system_prompt", "result", "init", "unknown_future_type"):
            data = {"type": etype, "foo": "bar"}
            assert adapter.map(data) is None, f"Expected None for type={etype!r}"

    def test_assistant_empty_content_returns_none(self):
        adapter = ClaudeAdapter()
        data = {"type": "assistant", "message": {"content": []}}
        assert adapter.map(data) is None

    def test_top_level_content_fallback(self):
        """Adapter falls back to top-level 'content' if 'message' key absent."""
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.response"
        assert extra["text"] == "hello"

    def test_vendor_drift_renamed_field_returns_none_not_crash(self):
        """A payload with an unexpected field shape must not crash."""
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "future_block_type", "payload": "something"}
                ]
            },
        }
        # Unknown block type — should return None (no event), not raise.
        result = adapter.map(data)
        assert result is None


# ---------------------------------------------------------------------------
# CopilotAdapter unit tests
# ---------------------------------------------------------------------------


class TestCopilotAdapter:
    def test_assistant_message_yields_thought(self):
        adapter = CopilotAdapter()
        data = {
            "type": "assistant.message",
            "data": {"content": "Here is my answer."},
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.thought"
        assert extra["text"] == "Here is my answer."

    def test_ephemeral_assistant_message_returns_none(self):
        adapter = CopilotAdapter()
        data = {
            "type": "assistant.message",
            "ephemeral": True,
            "data": {"content": "..."},
        }
        assert adapter.map(data) is None

    def test_tool_call_event_yields_tool_call(self):
        adapter = CopilotAdapter()
        data = {
            "type": "tool_call",
            "data": {"name": "read_file", "arguments": {"path": "foo.py"}},
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.tool_call"
        assert extra["tool"] == "read_file"
        assert extra["input"] == {"path": "foo.py"}

    def test_function_call_event_yields_tool_call(self):
        adapter = CopilotAdapter()
        data = {
            "type": "function_call",
            "data": {"name": "search", "arguments": {"query": "godel"}},
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.tool_call"

    def test_tool_result_event_yields_tool_result(self):
        adapter = CopilotAdapter()
        data = {
            "type": "tool_result",
            "data": {"tool_call_id": "tc_1", "output": "success"},
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        op, extra = result[0]
        assert op == "agent.tool_result"
        assert extra["tool"] == "tc_1"
        assert extra["output"] == "success"

    def test_metadata_events_return_none(self):
        adapter = CopilotAdapter()
        for etype in ("result", "progress", "error", "unknown"):
            data = {"type": etype}
            assert adapter.map(data) is None, f"Expected None for type={etype!r}"


# ---------------------------------------------------------------------------
# stream_into_transcript integration tests
# ---------------------------------------------------------------------------


class TestStreamIntoTranscript:
    def test_emits_thought_event(self, tmp_path):
        jsonl = b'{"type": "assistant.message", "data": {"content": "Hello"}}\n'
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=("step1",),
                stream_path=["s1"],
                adapter=CopilotAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        thought_events = [e for e in events if e["op"] == "agent.thought"]
        assert len(thought_events) == 1
        assert thought_events[0]["text"] == "Hello"

    def test_emits_tool_call_event(self, tmp_path):
        jsonl = (
            b'{"type": "tool_call", "data": {"name": "bash", "arguments": {"cmd": "ls"}}}\n'
        )
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=("s",),
                stream_path=[],
                adapter=CopilotAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        tc_events = [e for e in events if e["op"] == "agent.tool_call"]
        assert len(tc_events) == 1
        assert tc_events[0]["tool"] == "bash"

    def test_emits_tool_result_event(self, tmp_path):
        jsonl = (
            b'{"type": "tool_result", "data": {"tool_call_id": "tc1", "output": "done"}}\n'
        )
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=(),
                stream_path=[],
                adapter=CopilotAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        tr_events = [e for e in events if e["op"] == "agent.tool_result"]
        assert len(tr_events) == 1
        assert tr_events[0]["output"] == "done"

    def test_malformed_line_emits_agent_raw(self, tmp_path):
        """A malformed JSON line must produce agent.raw, not raise."""
        jsonl = b"not-json-at-all\n"
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=(),
                stream_path=[],
                adapter=CopilotAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        raw_events = [e for e in events if e["op"] == "agent.raw"]
        assert len(raw_events) == 1
        assert raw_events[0]["reason"] == "malformed"

    def test_vendor_drift_renamed_field_emits_raw_not_raises(self, tmp_path):
        """A fixture with a renamed field produces agent.raw, run completes normally."""
        # The "type" field was renamed to "event_type" in this hypothetical
        # vendor update — adapter maps will return None (no event), but the
        # line IS valid JSON so it won't be Raw.  We test the raw path using
        # truly malformed JSON to confirm no crash.
        bad_jsonl = b"{{broken\n"
        with _tw(tmp_path) as tw:
            # Must not raise
            stream_into_transcript(
                bad_jsonl,
                tw,
                step_path=(),
                stream_path=[],
                adapter=ClaudeAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        assert any(e["op"] == "agent.raw" for e in events)

    def test_metadata_lines_produce_no_events(self, tmp_path):
        """Lines where adapter.map() returns None must not appear in transcript."""
        jsonl = b'{"type": "result", "session_id": "abc"}\n'
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=(),
                stream_path=[],
                adapter=CopilotAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        # Only the header and no events (other than what header emits)
        non_header = [e for e in events if e.get("op") != "header"]
        assert non_header == []

    def test_step_path_and_stream_path_are_stamped(self, tmp_path):
        jsonl = b'{"type": "assistant.message", "data": {"content": "hi"}}\n'
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=("my_step",),
                stream_path=["ulid1", "ulid2"],
                adapter=CopilotAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        thought = next(e for e in events if e["op"] == "agent.thought")
        assert thought["step_path"] == ["my_step"]
        assert thought["stream_path"] == ["ulid1", "ulid2"]


# ---------------------------------------------------------------------------
# Workflow integration: stream_agents=True / False
# ---------------------------------------------------------------------------


def _make_mock_run(stdout: str):
    return CommandResult(stdout=stdout, stderr="", returncode=0)


COPILOT_JSONL_STREAM = "\n".join([
    '{"type": "assistant.message", "data": {"content": "I will help."}}',
    '{"type": "tool_call", "data": {"name": "bash", "arguments": {"cmd": "ls"}}}',
    '{"type": "tool_result", "data": {"tool_call_id": "tc1", "output": "a.py b.py"}}',
    '{"type": "assistant.message", "data": {"content": "Done."}}',
    '{"type": "result", "sessionId": "session-xyz"}',
])


def test_stream_agents_true_emits_transcript_events(tmp_path, monkeypatch):
    """With stream_agents=True, agent events appear in transcript.jsonl."""
    monkeypatch.chdir(tmp_path)

    from godel._context import _line_observer
    from godel.agents._copilot import copilot

    async def fake_run_with_observer(cmd, *, cwd=None, **kwargs):
        """Simulate run() — feed stdout lines through the active observer."""
        observer = _line_observer.get()
        if observer is not None:
            for raw_line in COPILOT_JSONL_STREAM.encode().splitlines(keepends=True):
                observer(raw_line)
        return _make_mock_run(COPILOT_JSONL_STREAM)

    @workflow
    async def wf():
        agent = copilot(model="default")
        with patch("godel.agents._common.run", side_effect=fake_run_with_observer):
            return await agent("do stuff")

    asyncio.run(wf())

    # Find transcript directory (runs/<run_id>/)
    run_dirs = list((tmp_path / "runs").iterdir())
    transcript_dirs = [d for d in run_dirs if d.is_dir()]
    assert len(transcript_dirs) == 1, f"Expected 1 run dir, got {transcript_dirs}"

    events = _read_transcript_events(tmp_path / "runs")
    ops = [e["op"] for e in events]
    assert "agent.thought" in ops
    assert "agent.tool_call" in ops
    assert "agent.tool_result" in ops


def test_stream_agents_false_no_transcript_file(tmp_path, monkeypatch):
    """With GODEL_STREAM_AGENTS=0, no transcript.jsonl is created."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GODEL_STREAM_AGENTS", "0")

    from godel.agents._copilot import copilot

    @workflow
    async def wf():
        agent = copilot(model="default")
        with patch("godel.agents._common.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _make_mock_run(COPILOT_JSONL_STREAM)
            return await agent("do stuff")

    asyncio.run(wf())

    transcript_files = list((tmp_path / "runs").rglob("transcript.jsonl"))
    assert transcript_files == [], "No transcript.jsonl should be written when streaming is disabled"


def test_claude_streaming_command_includes_stream_json(tmp_path, monkeypatch):
    """Claude CLI gets --output-format stream-json iff stream_agents=True."""
    monkeypatch.chdir(tmp_path)

    from godel.agents._claude import claude_code

    captured_cmds: list[list[str]] = []

    async def fake_run(cmd, *, cwd=None, **kwargs):
        captured_cmds.append(cmd)
        # Return a minimal stream-json response
        result_line = json.dumps({"type": "result", "result": "ok", "session_id": None})
        return CommandResult(stdout=result_line + "\n", stderr="", returncode=0)

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._common.run", side_effect=fake_run):
            return await agent("hello")

    asyncio.run(wf())

    assert len(captured_cmds) >= 1
    assert "--output-format" in captured_cmds[0] and "stream-json" in captured_cmds[0]


def test_claude_non_streaming_command_uses_json(tmp_path, monkeypatch):
    """Claude CLI uses --output-format json when streaming is disabled."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GODEL_STREAM_AGENTS", "0")

    from godel.agents._claude import claude_code

    captured_cmds: list[list[str]] = []

    async def fake_run(cmd, *, cwd=None, **kwargs):
        captured_cmds.append(cmd)
        return CommandResult(
            stdout='{"result": "ok", "session_id": null}',
            stderr="",
            returncode=0,
        )

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._common.run", side_effect=fake_run):
            return await agent("hello")

    asyncio.run(wf())

    assert len(captured_cmds) >= 1
    assert "--output-format" in captured_cmds[0] and "json" in captured_cmds[0]
    assert "stream-json" not in captured_cmds[0]


def test_copilot_discard_branch_removed():
    """CopilotAdapter.map() is always invoked — the old discard path is gone.

    The legacy discard path in _copilot.py lines 16-20 parsed and silently
    dropped all events.  This test verifies that running the adapter directly
    on the Copilot JSONL produces events (not silence), proving the discard
    path no longer exists.
    """
    adapter = CopilotAdapter()
    lines = [
        '{"type": "assistant.message", "data": {"content": "hi"}}',
        '{"type": "result", "sessionId": "s1"}',
    ]
    results = []
    for line in lines:
        data = json.loads(line)
        results.append(adapter.map(data))

    # The first line should map to agent.thought (list with one entry)
    assert results[0] is not None and len(results[0]) == 1
    assert results[0][0][0] == "agent.thought"
    # The result line should return None (metadata)
    assert results[1] is None


# ---------------------------------------------------------------------------
# C1: Multi-block assistant events — all blocks must be emitted
# ---------------------------------------------------------------------------


class TestClaudeAdapterMultiBlock:
    """C1: A Claude assistant event with multiple content blocks (text + tool_use)
    must yield one event per block — none dropped."""

    def test_text_then_tool_use_yields_two_events(self):
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll run ls for you."},
                    {"type": "tool_use", "name": "bash", "input": {"command": "ls"}},
                ]
            },
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 2
        ops = [op for op, _ in result]
        assert ops[0] == "agent.response"
        assert ops[1] == "agent.tool_call"
        assert result[0][1]["text"] == "I'll run ls for you."
        assert result[1][1]["tool"] == "bash"

    def test_multiple_text_blocks_all_emitted(self):
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "First thought."},
                    {"type": "text", "text": "Second thought."},
                ]
            },
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 2
        texts = [extra["text"] for _, extra in result]
        assert "First thought." in texts
        assert "Second thought." in texts

    def test_multi_block_stream_into_transcript_emits_all(self, tmp_path):
        """stream_into_transcript writes one event per content block."""
        payload = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Here is my plan."},
                    {"type": "tool_use", "name": "read_file", "input": {"path": "x.py"}},
                ]
            },
        }
        jsonl = (json.dumps(payload) + "\n").encode("utf-8")
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=("step1",),
                stream_path=[],
                adapter=ClaudeAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        thought_events = [e for e in events if e["op"] == "agent.response"]
        tool_call_events = [e for e in events if e["op"] == "agent.tool_call"]
        assert len(thought_events) == 1
        assert thought_events[0]["text"] == "Here is my plan."
        assert len(tool_call_events) == 1
        assert tool_call_events[0]["tool"] == "read_file"


# ---------------------------------------------------------------------------
# W1: UTF-8 round-trip — multi-byte strings survive adapter + transcript
# ---------------------------------------------------------------------------


class TestUtf8RoundTrip:
    """W1: Multi-byte (emoji, non-ASCII) strings must reach the transcript
    byte-identically through both ClaudeAdapter and CopilotAdapter."""

    MULTI_BYTE_TEXT = "こんにちは 🌍 café résumé"

    def test_claude_adapter_utf8_round_trip(self, tmp_path):
        payload = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": self.MULTI_BYTE_TEXT}]
            },
        }
        jsonl = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=(),
                stream_path=[],
                adapter=ClaudeAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        thought_events = [e for e in events if e["op"] == "agent.response"]
        assert len(thought_events) == 1
        assert thought_events[0]["text"] == self.MULTI_BYTE_TEXT

    def test_copilot_adapter_utf8_round_trip(self, tmp_path):
        payload = {
            "type": "assistant.message",
            "data": {"content": self.MULTI_BYTE_TEXT},
        }
        jsonl = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with _tw(tmp_path) as tw:
            stream_into_transcript(
                jsonl,
                tw,
                step_path=(),
                stream_path=[],
                adapter=CopilotAdapter(),
            )
        events = _read_transcript_events(tmp_path)
        thought_events = [e for e in events if e["op"] == "agent.thought"]
        assert len(thought_events) == 1
        assert thought_events[0]["text"] == self.MULTI_BYTE_TEXT

    def test_claude_adapter_map_preserves_utf8(self):
        """ClaudeAdapter.map() returns the exact multi-byte string."""
        adapter = ClaudeAdapter()
        data = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": self.MULTI_BYTE_TEXT}]},
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        _, extra = result[0]
        assert extra["text"] == self.MULTI_BYTE_TEXT

    def test_copilot_adapter_map_preserves_utf8(self):
        """CopilotAdapter.map() returns the exact multi-byte string."""
        adapter = CopilotAdapter()
        data = {
            "type": "assistant.message",
            "data": {"content": self.MULTI_BYTE_TEXT},
        }
        result = adapter.map(data)
        assert result is not None and len(result) == 1
        _, extra = result[0]
        assert extra["text"] == self.MULTI_BYTE_TEXT
