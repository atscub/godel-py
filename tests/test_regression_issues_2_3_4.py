"""Regression tests for GitHub issues #2, #3, #4.

#3: Shell-quoting bug in claude_code agent subprocess invocation
#4: read_text 64KB truncation breaks resume for large files
#2: parallel() docs wrong on both input shape and return type
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from godel._run import CommandResult, run
from godel._context import WorkflowContext, _current_workflow
from godel._decorators import workflow, parallel
from godel._event_log import EventLog
from godel._events import EventStatus
from godel._replay import ReplayWalker
from godel.agents._claude import claude_code
from godel.agents._copilot import copilot
from godel.io import read_text, _normalize_path, _CONTENT_LOG_LIMIT_BYTES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _cleanup_ctx():
    yield
    _current_workflow.set(None)


def _make_replay_ctx(tmp_path, events: list[dict]) -> WorkflowContext:
    """Create an EventLog with events, reload it, and install a replay context."""
    run_id = "test-regression-run"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    event_ids = []
    for ev in events:
        started = log.emit_started(
            op=ev["op"],
            step_path=ev.get("step_path", ()),
            request=ev.get("request", {}),
            invocation_seq=ev.get("invocation_seq", 0),
            step_local_seq=ev.get("step_local_seq", 0),
        )
        event_ids.append(started.event_id)
        if ev.get("finish", False):
            log.emit_finished(started.event_id, response=ev.get("response", {}))
    log.close()

    loaded = EventLog.load(run_id, runs_dir=str(tmp_path))
    walker = ReplayWalker(loaded)
    ctx = WorkflowContext(run_id=run_id, event_log=loaded, replay_walker=walker)
    _current_workflow.set(ctx)
    return ctx


# ===========================================================================
# Issue #3: Shell-quoting — prompts with metacharacters must arrive intact
# ===========================================================================

class TestShellQuotingRegression:
    """Verify that prompts with shell metacharacters are not mangled."""

    NASTY_PROMPTS = [
        "Classify: Uber’s Marketing Data Platform team",  # curly apostrophe
        "Run `git log --oneline` and summarize",  # backticks
        "Set $HOME to /tmp and check $PATH",  # dollar signs
        'She said "hello" and it\'s fine',  # mixed quotes
        "price > $100 && stock < 50 | sort",  # pipes, ampersands, gt/lt
        "file.txt; rm -rf /; echo pwned",  # semicolons (command injection)
        "$(whoami) or `id`",  # command substitution
    ]

    @pytest.mark.parametrize("prompt", NASTY_PROMPTS, ids=[
        "curly_apostrophe", "backticks", "dollar_signs", "mixed_quotes",
        "pipes_and_ampersands", "semicolons", "command_substitution",
    ])
    def test_prompt_arrives_as_single_argv_element(self, prompt):
        """The prompt must be a single element in the argv list, not split by shell."""
        agent = claude_code()
        cmd = agent._build_command(
            prompt, "claude-sonnet-4-6",
            tools=None, session_id=None, streaming=False,
        )
        assert isinstance(cmd, list), "Command must be a list, not a string"

        p_idx = cmd.index("-p")
        actual_prompt = cmd[p_idx + 1]
        assert actual_prompt == prompt, (
            f"Prompt was mangled: expected {prompt!r}, got {actual_prompt!r}"
        )

    def test_run_uses_exec_for_list_cmd(self):
        """run() calls create_subprocess_exec (not shell) when cmd is a list."""
        exec_calls = []
        shell_calls = []

        async def fake_exec(*args, **kwargs):
            exec_calls.append(args)

            class FakeProc:
                returncode = 0
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()
                async def wait(self): pass
            proc = FakeProc()
            proc.stdout.feed_data(b"ok\n")
            proc.stdout.feed_eof()
            proc.stderr.feed_data(b"")
            proc.stderr.feed_eof()
            return proc

        async def fake_shell(cmd, **kwargs):
            shell_calls.append(cmd)
            return await fake_exec(cmd, **kwargs)

        async def go():
            with patch("godel._run.asyncio.create_subprocess_exec", side_effect=fake_exec):
                with patch("godel._run.asyncio.create_subprocess_shell", side_effect=fake_shell):
                    await run(["echo", "hello world"])

        asyncio.run(go())
        assert len(exec_calls) == 1, "Expected create_subprocess_exec to be called"
        assert len(shell_calls) == 0, "create_subprocess_shell must NOT be called for list cmd"

    def test_run_uses_shell_for_string_cmd(self):
        """run() still calls create_subprocess_shell for string commands (backward compat)."""
        shell_calls = []

        async def fake_shell(cmd, **kwargs):
            shell_calls.append(cmd)

            class FakeProc:
                returncode = 0
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()
                async def wait(self): pass
            proc = FakeProc()
            proc.stdout.feed_data(b"ok\n")
            proc.stdout.feed_eof()
            proc.stderr.feed_data(b"")
            proc.stderr.feed_eof()
            return proc

        async def go():
            with patch("godel._run.asyncio.create_subprocess_shell", side_effect=fake_shell):
                await run("echo hello")

        asyncio.run(go())
        assert len(shell_calls) == 1

    def test_agent_call_with_metachar_prompt_succeeds(self):
        """End-to-end: agent call with shell metacharacters does not raise."""
        captured = []

        async def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return CommandResult(
                stdout=json.dumps({"result": "classified", "session_id": "s1"}),
                stderr="", returncode=0,
            )

        @workflow
        async def wf():
            with patch("godel.agents._common.run", new=fake_run):
                agent = claude_code(skip_permissions=True)
                result = await agent("Classify: Uber’s Marketing Data Platform team")
                return result

        result = asyncio.run(wf())
        assert result == "classified"
        assert len(captured) == 1
        assert isinstance(captured[0], list)
        # The curly apostrophe must be in the prompt arg, intact
        p_idx = captured[0].index("-p")
        assert "’" in captured[0][p_idx + 1]

    def test_copilot_build_command_returns_list(self):
        """copilot agent also returns list from _build_command."""
        agent = copilot()
        cmd = agent._build_command(
            "test prompt with $VAR", "gpt-5",
            tools=None, session_id=None, streaming=False,
        )
        assert isinstance(cmd, list)
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "test prompt with $VAR"


# ===========================================================================
# Issue #4: read_text 64KB truncation — large files must survive resume
# ===========================================================================

class TestReadTextTruncationRegression:
    """Verify that files >64KB are not silently corrupted on resume."""

    def _make_large_content(self, size_bytes: int = 100 * 1024) -> str:
        """Generate content larger than the 64KB truncation limit."""
        line = '{"id": 12345, "title": "Software Engineer", "company": "Acme Corp"}\n'
        repeats = (size_bytes // len(line)) + 1
        return line * repeats

    def test_reread_mode_returns_full_content_on_resume(self, tmp_path):
        """cache='reread' re-reads from disk, returning full untruncated content."""
        large_content = self._make_large_content()
        assert len(large_content.encode()) > _CONTENT_LOG_LIMIT_BYTES

        target = tmp_path / "large.jsonl"
        target.write_text(large_content)
        resolved = _normalize_path(str(target))

        # Simulate a prior run that recorded a truncated version
        _make_replay_ctx(tmp_path / "logs", [{
            "op": "read_text",
            "finish": True,
            "request": {"path": resolved, "encoding": "utf-8"},
            "response": {
                "content": large_content[:1000] + "\n... [truncated]",
                "bytes_read": len(large_content.encode()),
            },
        }])

        result = asyncio.run(read_text(str(target), cache="reread"))
        assert result == large_content
        assert len(result.encode()) > _CONTENT_LOG_LIMIT_BYTES

    def test_reread_mode_sees_updated_file(self, tmp_path):
        """cache='reread' returns current disk content, not stale cache."""
        target = tmp_path / "data.txt"
        target.write_text("version 2 — updated after original run")
        resolved = _normalize_path(str(target))

        _make_replay_ctx(tmp_path / "logs", [{
            "op": "read_text",
            "finish": True,
            "request": {"path": resolved, "encoding": "utf-8"},
            "response": {"content": "version 1 — original", "bytes_read": 22},
        }])

        result = asyncio.run(read_text(str(target), cache="reread"))
        assert result == "version 2 — updated after original run"

    def test_file_cache_stores_and_retrieves_full_snapshot(self, tmp_path):
        """cache='file' stores a full snapshot and retrieves it on resume."""
        large_content = self._make_large_content()
        assert len(large_content.encode()) > _CONTENT_LOG_LIMIT_BYTES

        target = tmp_path / "big_file.jsonl"
        target.write_text(large_content)

        # First run: read with cache="file" — stores snapshot + log entry
        run_id = "test-file-cache"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        result = asyncio.run(read_text(str(target), cache="file"))
        assert result == large_content

        # Verify snapshot file was created
        cache_dir = tmp_path / "runs" / run_id / "cache"
        assert cache_dir.exists()
        snapshot_files = list(cache_dir.glob("*.content"))
        assert len(snapshot_files) == 1
        assert snapshot_files[0].read_text() == large_content

        # Verify log response has content_ref
        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text" and e.status == EventStatus.FINISHED]
        assert len(read_events) == 1
        assert "content_ref" in read_events[0].response
        content_ref = read_events[0].response["content_ref"]

        log.close()

        # Second run: resume — should read from snapshot, not disk
        target.unlink()  # delete the original file

        loaded = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
        walker = ReplayWalker(loaded)
        ctx2 = WorkflowContext(run_id=run_id, event_log=loaded, replay_walker=walker)
        _current_workflow.set(ctx2)

        replayed = asyncio.run(read_text(str(target), cache="file"))
        assert replayed == large_content
        assert len(replayed.encode()) > _CONTENT_LOG_LIMIT_BYTES

    def test_file_cache_backward_compat_with_inline_content(self, tmp_path):
        """cache='file' falls back to inline content for old logs without content_ref."""
        resolved = _normalize_path(str(tmp_path / "old_format.txt"))

        _make_replay_ctx(tmp_path / "logs", [{
            "op": "read_text",
            "finish": True,
            "request": {"path": resolved, "encoding": "utf-8"},
            "response": {"content": "inline from old log", "bytes_read": 19},
        }])

        result = asyncio.run(read_text(str(tmp_path / "old_format.txt"), cache="file"))
        assert result == "inline from old log"

    def test_invalid_cache_mode_raises(self):
        """Passing an invalid cache mode raises ValueError."""
        with pytest.raises(ValueError, match="cache must be"):
            asyncio.run(read_text("/dev/null", cache="invalid"))

    def test_large_file_jsonl_not_corrupted_on_resume(self, tmp_path):
        """Reproduces the original bug: a 100KB+ JSONL file must not have lines
        cut mid-string when replayed via cache='file'."""
        lines = [
            json.dumps({"id": i, "title": f"Job posting #{i}", "description": "x" * 200})
            for i in range(500)
        ]
        content = "\n".join(lines) + "\n"
        assert len(content.encode()) > _CONTENT_LOG_LIMIT_BYTES

        target = tmp_path / "seen.jsonl"
        target.write_text(content)

        # First run with cache="file"
        run_id = "test-jsonl-roundtrip"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        result = asyncio.run(read_text(str(target), cache="file"))
        log.close()

        # Replay from snapshot
        target.unlink()
        loaded = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
        walker = ReplayWalker(loaded)
        ctx2 = WorkflowContext(run_id=run_id, event_log=loaded, replay_walker=walker)
        _current_workflow.set(ctx2)

        replayed = asyncio.run(read_text(str(target), cache="file"))

        # Every line must parse as valid JSON — no mid-string truncation
        for i, line in enumerate(replayed.strip().split("\n")):
            try:
                obj = json.loads(line)
                assert obj["id"] == i
            except json.JSONDecodeError:
                pytest.fail(f"Line {i} is corrupt (truncated mid-string): {line[:80]!r}...")


# ===========================================================================
# Issue #2: parallel() — variadic args and tuple return type
# ===========================================================================

class TestParallelSignatureRegression:
    """Verify parallel() accepts variadic args and returns a tuple."""

    def test_returns_tuple_not_list(self):
        """parallel() return type is tuple, matching the implementation."""
        async def run():
            async def a(): return 1
            async def b(): return 2
            return await parallel(a(), b())

        result = asyncio.run(run())
        assert isinstance(result, tuple), f"Expected tuple, got {type(result).__name__}"
        assert result == (1, 2)

    def test_variadic_args_accepted(self):
        """parallel() accepts variadic positional args (not a single list)."""
        async def run():
            async def task(n): return n * 10
            return await parallel(task(1), task(2), task(3))

        result = asyncio.run(run())
        assert result == (10, 20, 30)

    def test_splat_from_comprehension(self):
        """parallel(*[coro for ...]) is the correct usage, not parallel([...])."""
        async def run():
            async def task(n): return n ** 2
            return await parallel(*[task(i) for i in range(5)])

        result = asyncio.run(run())
        assert result == (0, 1, 4, 9, 16)
        assert isinstance(result, tuple)
