"""Tests for godel.read_text / godel.write_text audited file I/O primitives."""
from __future__ import annotations

import asyncio
import pytest

from godel.io import read_text, write_text
from godel._context import WorkflowContext, _current_workflow
from godel._event_log import EventLog
from godel._events import EventStatus
from godel._replay import ReplayWalker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log_with_events(tmp_path, events: list[dict]) -> EventLog:
    """Create an EventLog, emit events into it, close, and reload."""
    run_id = "test-file-io-run"
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
# read_text — live (no replay context)
# ---------------------------------------------------------------------------

class TestReadTextLive:
    def test_reads_file_content(self, tmp_path):
        """read_text reads and returns the file content."""
        test_file = tmp_path / "hello.txt"
        test_file.write_text("hello world")

        result = asyncio.run(read_text(str(test_file)))
        assert result == "hello world"

    def test_records_event_in_log(self, tmp_path):
        """read_text emits a FINISHED event in the audit log with content."""
        test_file = tmp_path / "data.txt"
        test_file.write_text("some content")

        run_id = "test-read-live"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        asyncio.run(read_text(str(test_file)))

        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text"]
        # In-memory list holds one entry per event (mutated STARTED → FINISHED)
        assert len(read_events) == 1
        assert read_events[0].status == EventStatus.FINISHED
        assert read_events[0].response["content"] == "some content"
        assert read_events[0].request["path"] == str(test_file)

    def test_records_path_in_request(self, tmp_path):
        """read_text records the path in the event request."""
        test_file = tmp_path / "audit.txt"
        test_file.write_text("audited")

        run_id = "test-read-path"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        asyncio.run(read_text(str(test_file)))

        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text"]
        assert any(e.request.get("path") == str(test_file) for e in read_events)


# ---------------------------------------------------------------------------
# read_text — replay
# ---------------------------------------------------------------------------

class TestReadTextReplay:
    def test_returns_cached_content(self, tmp_path):
        """On replay, read_text returns cached content without touching the FS."""
        target_path = str(tmp_path / "cached.txt")

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "read_text",
                "finish": True,
                "request": {"path": target_path},
                "response": {"content": "cached content"},
            },
        ])
        _install_replay_ctx(loaded)

        # File does NOT exist on disk — must come from cache
        result = asyncio.run(read_text(target_path))
        assert result == "cached content"

    def test_no_disk_access_on_replay(self, tmp_path):
        """read_text on replay skips filesystem entirely — even for non-existent paths."""
        nonexistent = str(tmp_path / "does_not_exist.txt")

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "read_text",
                "finish": True,
                "request": {"path": nonexistent},
                "response": {"content": "replay only"},
            },
        ])
        _install_replay_ctx(loaded)

        result = asyncio.run(read_text(nonexistent))
        assert result == "replay only"


# ---------------------------------------------------------------------------
# write_text — live (no replay context)
# ---------------------------------------------------------------------------

class TestWriteTextLive:
    def test_writes_file_content(self, tmp_path):
        """write_text writes content to the specified path."""
        target = tmp_path / "output.txt"
        asyncio.run(write_text(str(target), "written content"))
        assert target.read_text() == "written content"

    def test_records_event_in_log(self, tmp_path):
        """write_text emits a FINISHED event in the audit log."""
        target = tmp_path / "out.txt"
        run_id = "test-write-live"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        asyncio.run(write_text(str(target), "log me"))

        events = log.all_events()
        write_events = [e for e in events if e.op == "write_text"]
        # In-memory list holds one entry per event (mutated STARTED → FINISHED)
        assert len(write_events) == 1
        assert write_events[0].status == EventStatus.FINISHED
        assert write_events[0].request["path"] == str(target)
        assert write_events[0].request["content"] == "log me"

    def test_records_path_and_content_in_request(self, tmp_path):
        """write_text records both path and content in the event request."""
        target = tmp_path / "recorded.txt"
        run_id = "test-write-request"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        asyncio.run(write_text(str(target), "the content"))

        events = log.all_events()
        write_events = [e for e in events if e.op == "write_text"]
        req = write_events[0].request
        assert req["path"] == str(target)
        assert req["content"] == "the content"


# ---------------------------------------------------------------------------
# write_text — replay
# ---------------------------------------------------------------------------

class TestWriteTextReplay:
    def test_skips_write_on_replay(self, tmp_path):
        """On replay, write_text does NOT write to the filesystem."""
        target_path = str(tmp_path / "should_not_exist.txt")

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "write_text",
                "finish": True,
                "request": {"path": target_path, "content": "written"},
                "response": {},
            },
        ])
        _install_replay_ctx(loaded)

        asyncio.run(write_text(target_path, "written"))

        # File should NOT have been written during replay
        from pathlib import Path
        assert not Path(target_path).exists()

    def test_started_only_raises_unsafe_resume_error(self, tmp_path):
        """write_text in STARTED-only state raises UnsafeResumeError."""
        from godel._exceptions import UnsafeResumeError

        target_path = str(tmp_path / "partial.txt")

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "write_text",
                "finish": False,  # STARTED but never FINISHED
                "request": {"path": target_path, "content": "partial"},
                "response": {},
            },
        ])
        _install_replay_ctx(loaded)

        with pytest.raises(UnsafeResumeError):
            asyncio.run(write_text(target_path, "partial"))


# ---------------------------------------------------------------------------
# Op names appear in formatter registry (godel show)
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_read_text_formatter_registered(self):
        from godel._formatters import FORMATTERS
        assert "read_text" in FORMATTERS

    def test_write_text_formatter_registered(self):
        from godel._formatters import FORMATTERS
        assert "write_text" in FORMATTERS

    def test_read_text_formatter_renders_base_line(self, tmp_path):
        """read_text formatter produces a non-empty string."""
        from godel._formatters import FORMATTERS
        from godel._events import Event, EventStatus
        event = Event(
            event_id="AABBCCDD00112233",
            run_id="test",
            seq=0,
            op="read_text",
            status=EventStatus.FINISHED,
            request={"path": "/tmp/foo.txt"},
            response={"content": "hello"},
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
        )
        line = FORMATTERS["read_text"](event)
        assert "read_text" in line
        assert "FINISHED" in line

    def test_write_text_formatter_renders_base_line(self, tmp_path):
        """write_text formatter produces a non-empty string."""
        from godel._formatters import FORMATTERS
        from godel._events import Event, EventStatus
        event = Event(
            event_id="AABBCCDD00112233",
            run_id="test",
            seq=0,
            op="write_text",
            status=EventStatus.FINISHED,
            request={"path": "/tmp/bar.txt", "content": "data"},
            response={},
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
        )
        line = FORMATTERS["write_text"](event)
        assert "write_text" in line
        assert "FINISHED" in line


# ---------------------------------------------------------------------------
# Public API exports
# ---------------------------------------------------------------------------

class TestExports:
    def test_read_text_exported_from_godel(self):
        import godel
        assert callable(godel.read_text)

    def test_write_text_exported_from_godel(self):
        import godel
        assert callable(godel.write_text)

    def test_read_text_in_all(self):
        import godel
        assert "read_text" in godel.__all__

    def test_write_text_in_all(self):
        import godel
        assert "write_text" in godel.__all__


# ---------------------------------------------------------------------------
# Strict mode compatibility — write_text bypasses audit hook via _privileged
# Must run in a subprocess because sys.addaudithook() is permanent.
# ---------------------------------------------------------------------------

class TestStrictModeCompatibility:
    def test_write_text_bypasses_strict_audit_hook(self, tmp_path):
        """write_text uses _privileged so it is not blocked by strict mode.

        Runs in a child process because sys.addaudithook() is a one-way
        operation that would contaminate subsequent tests in the same process.
        """
        import subprocess
        import sys
        from pathlib import Path

        project_root = str(Path(__file__).parent.parent)
        target = tmp_path / "strict_test.txt"

        code = f"""
import sys
sys.path.insert(0, {project_root!r})
from godel._strict_audit import install_audit_hook
install_audit_hook()

import asyncio
from godel.io import write_text

asyncio.run(write_text({str(target)!r}, "allowed"))

content = open({str(target)!r}).read()
assert content == "allowed", f"unexpected content: {{content!r}}"
print("ok")
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"
        assert "ok" in result.stdout
