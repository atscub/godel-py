"""Tests for replay guards in all primitives: run, print, input, det.now, det.random, det.uuid4."""
from __future__ import annotations

import asyncio
import pytest

from godel._context import WorkflowContext, _current_workflow
from godel._event_log import EventLog
from godel._events import Event, EventStatus
from godel._replay import ReplayWalker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log_with_events(tmp_path, events: list[dict]) -> EventLog:
    """Create an EventLog, emit events into it, close, and reload."""
    run_id = "test-replay-run"
    log = EventLog(run_id, runs_dir=str(tmp_path))

    for ev in events:
        started = log.emit_started(
            op=ev["op"],
            step_path=ev.get("step_path", ()),
            request=ev.get("request", {}),
            invocation_seq=ev.get("invocation_seq", 0),
            step_local_seq=ev.get("step_local_seq", 0),
        )
        if ev.get("finish", False):
            log.emit_finished(started.event_id, response=ev.get("response", {}))

    log.close()
    return EventLog.load(run_id, runs_dir=str(tmp_path))


def _install_replay_ctx(loaded_log: EventLog) -> WorkflowContext:
    """Build a WorkflowContext with a ReplayWalker and install it."""
    walker = ReplayWalker(loaded_log)
    ctx = WorkflowContext(
        run_id=loaded_log._run_id,
        event_log=loaded_log,
        replay_walker=walker,
    )
    _current_workflow.set(ctx)
    return ctx


@pytest.fixture(autouse=True)
def _cleanup_ctx():
    """Reset _current_workflow after each test to avoid leaking."""
    yield
    _current_workflow.set(None)


# ---------------------------------------------------------------------------
# det.now() replay
# ---------------------------------------------------------------------------

class TestDetNowReplay:
    def test_returns_cached_value(self, tmp_path):
        from godel import det

        loaded = _make_log_with_events(tmp_path, [
            {"op": "det.now", "finish": True, "response": {"value": "2026-01-01T00:00:00+00:00"}},
        ])
        _install_replay_ctx(loaded)

        result = det.now()
        assert result == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# det.random() replay
# ---------------------------------------------------------------------------

class TestDetRandomReplay:
    def test_returns_cached_value(self, tmp_path):
        from godel import det

        loaded = _make_log_with_events(tmp_path, [
            {"op": "det.random", "finish": True, "response": {"value": 0.42}},
        ])
        _install_replay_ctx(loaded)

        result = det.random()
        assert result == 0.42


# ---------------------------------------------------------------------------
# det.uuid4() replay
# ---------------------------------------------------------------------------

class TestDetUuid4Replay:
    def test_returns_cached_value(self, tmp_path):
        from godel import det

        loaded = _make_log_with_events(tmp_path, [
            {"op": "det.uuid4", "finish": True, "response": {"value": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}},
        ])
        _install_replay_ctx(loaded)

        result = det.uuid4()
        assert result == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ---------------------------------------------------------------------------
# print() replay — still displays text, skips audit-log emission
# ---------------------------------------------------------------------------

class TestPrintReplay:
    def test_visible_during_replay(self, tmp_path, capsys):
        from godel import io

        req = {"text": "hello\n"}
        loaded = _make_log_with_events(tmp_path, [
            {"op": "print", "finish": True, "request": req, "response": {}},
        ])
        _install_replay_ctx(loaded)

        asyncio.run(io.print("hello"))
        captured = capsys.readouterr()
        assert captured.out == "hello\n"


# ---------------------------------------------------------------------------
# input() replay — returns cached value
# ---------------------------------------------------------------------------

class TestInputReplay:
    def test_returns_cached_value(self, tmp_path):
        from godel import io

        req = {"prompt": "Name? "}
        loaded = _make_log_with_events(tmp_path, [
            {"op": "input", "finish": True, "request": req, "response": {"value": "Alice"}},
        ])
        _install_replay_ctx(loaded)

        result = asyncio.run(io.input("Name? "))
        assert result == "Alice"


# ---------------------------------------------------------------------------
# run() replay — FINISHED returns cached CommandResult
# ---------------------------------------------------------------------------

class TestRunReplay:
    def test_finished_returns_cached(self, tmp_path):
        from godel._run import run, CommandResult

        req = {"cmd": "echo hi", "cwd": None, "timeout": None, "idempotent": False}
        loaded = _make_log_with_events(tmp_path, [
            {"op": "run", "finish": True, "request": req,
             "response": {"stdout": "hi\n", "stderr": "", "returncode": 0}},
        ])
        _install_replay_ctx(loaded)

        result = asyncio.run(run("echo hi"))
        assert isinstance(result, CommandResult)
        assert result.stdout == "hi\n"
        assert result.returncode == 0

    def test_started_only_non_idempotent_raises(self, tmp_path):
        from godel._run import run
        from godel._exceptions import UnsafeResumeError

        req = {"cmd": "rm -rf /", "cwd": None, "timeout": None, "idempotent": False}
        loaded = _make_log_with_events(tmp_path, [
            {"op": "run", "finish": False, "request": req},
        ])
        _install_replay_ctx(loaded)

        with pytest.raises(UnsafeResumeError):
            asyncio.run(run("rm -rf /"))

    def test_started_only_idempotent_falls_through(self, tmp_path):
        from godel._run import run

        req = {"cmd": "echo safe", "cwd": None, "timeout": None, "idempotent": True}
        loaded = _make_log_with_events(tmp_path, [
            {"op": "run", "finish": False, "request": req},
        ])
        _install_replay_ctx(loaded)

        # With idempotent=True, STARTED-only should fall through and actually execute
        result = asyncio.run(run("echo safe", idempotent=True))
        assert "safe" in result.stdout
