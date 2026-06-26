"""Tests for godel.read_text / godel.write_text audited file I/O primitives."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from godel.io import read_text, write_text, _CONTENT_LOG_LIMIT, _CONTENT_LOG_LIMIT_BYTES, _normalize_path
from godel._context import WorkflowContext, _current_workflow
from godel._event_log import EventLog
from godel._events import EventStatus
from godel._replay import ReplayWalker, MismatchPolicy, set_mismatch_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolved(p) -> str:
    """Return the absolute-resolved form used internally by the primitives."""
    return _normalize_path(str(p))


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
    """Reset _current_workflow and global mismatch policy after each test."""
    yield
    _current_workflow.set(None)
    set_mismatch_policy(None)


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
        assert len(read_events) == 1
        assert read_events[0].status == EventStatus.FINISHED
        assert read_events[0].response["content"] == "some content"
        assert read_events[0].response["bytes_read"] == len(b"some content")
        # Path stored in resolved/absolute form
        assert read_events[0].request["path"] == _resolved(test_file)
        assert read_events[0].request["encoding"] == "utf-8"

    def test_records_path_in_request(self, tmp_path):
        """read_text records the resolved path in the event request."""
        test_file = tmp_path / "audit.txt"
        test_file.write_text("audited")

        run_id = "test-read-path"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        asyncio.run(read_text(str(test_file)))

        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text"]
        assert any(e.request.get("path") == _resolved(test_file) for e in read_events)

    def test_emits_failed_event_on_nonexistent_file(self, tmp_path):
        """Missing file → FileNotFoundError propagates; event is FAILED, not STARTED."""
        missing = tmp_path / "missing.txt"

        run_id = "test-read-fail"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        with pytest.raises(FileNotFoundError):
            asyncio.run(read_text(str(missing)))

        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text"]
        assert len(read_events) == 1
        assert read_events[0].status == EventStatus.FAILED
        assert read_events[0].response["error_type"] == "FileNotFoundError"

    def test_accepts_encoding_parameter(self, tmp_path):
        """read_text honours the encoding kwarg (latin-1 round-trip)."""
        test_file = tmp_path / "latin.txt"
        test_file.write_bytes("café".encode("latin-1"))

        result = asyncio.run(read_text(str(test_file), encoding="latin-1"))
        assert result == "café"

    def test_raises_unicode_decode_error_on_binary(self, tmp_path):
        """Default utf-8 raises UnicodeDecodeError on invalid bytes; event is FAILED."""
        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"\xff\xfe\xfd\xfc")

        run_id = "test-read-unicode"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        with pytest.raises(UnicodeDecodeError):
            asyncio.run(read_text(str(bad)))

        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text"]
        assert read_events[0].status == EventStatus.FAILED
        assert read_events[0].response["error_type"] == "UnicodeDecodeError"

    def test_expanduser_resolves_tilde(self, tmp_path, monkeypatch):
        """Tilde in path is expanded; resolved path recorded in log."""
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "home_file.txt"
        target.write_text("home sweet home")

        run_id = "test-read-tilde"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        result = asyncio.run(read_text("~/home_file.txt"))
        assert result == "home sweet home"

        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text"]
        # Tilde must have been expanded to an absolute path
        assert not read_events[0].request["path"].startswith("~")
        assert read_events[0].request["path"] == _resolved(target)

    def test_large_content_truncated_in_log_but_returned_in_full(self, tmp_path):
        """Files larger than _CONTENT_LOG_LIMIT are truncated in the log only."""
        big = tmp_path / "big.txt"
        big_content = "X" * (_CONTENT_LOG_LIMIT + 5000)
        big.write_text(big_content)

        run_id = "test-read-big"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        result = asyncio.run(read_text(str(big)))
        # Caller receives FULL content
        assert result == big_content

        events = log.all_events()
        read_events = [e for e in events if e.op == "read_text"]
        stored = read_events[0].response["content"]
        # Log is truncated
        assert len(stored) < len(big_content)
        assert "truncated from audit log" in stored
        # bytes_read reflects the ACTUAL file size, not the truncated snapshot
        assert read_events[0].response["bytes_read"] == len(big_content)


# ---------------------------------------------------------------------------
# read_text — replay
# ---------------------------------------------------------------------------

class TestReadTextReplay:
    def test_reread_reads_from_disk_on_replay(self, tmp_path):
        """replay='reread' (default) re-reads from disk even when log has cached content."""
        target = tmp_path / "cached.txt"
        target.write_text("updated on disk")
        resolved = _normalize_path(str(target))

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "read_text",
                "finish": True,
                "request": {"path": resolved, "encoding": "utf-8", "replay": "reread"},
                "response": {"content": "stale cached content"},
            },
        ])
        _install_replay_ctx(loaded)

        result = asyncio.run(read_text(str(target)))
        assert result == "updated on disk"

    def test_file_cache_returns_inline_content(self, tmp_path):
        """replay='file' returns inline content from the log on replay."""
        nonexistent = str(tmp_path / "does_not_exist.txt")
        resolved = _normalize_path(nonexistent)

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "read_text",
                "finish": True,
                "request": {"path": resolved, "encoding": "utf-8", "replay": "file"},
                "response": {"content": "replay only"},
            },
        ])
        _install_replay_ctx(loaded)

        result = asyncio.run(read_text(nonexistent, replay="file"))
        assert result == "replay only"

    def test_relative_path_matches_cache_despite_cwd_change(self, tmp_path, monkeypatch):
        """Relative paths resolve to absolute; replay matches regardless of cwd."""
        target = tmp_path / "rel.txt"
        target.write_text("via absolute")
        resolved = _normalize_path(str(target))

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "read_text",
                "finish": True,
                "request": {"path": resolved, "encoding": "utf-8", "replay": "reread"},
                "response": {"content": "via absolute"},
            },
        ])
        _install_replay_ctx(loaded)

        other_dir = tmp_path / "other_cwd"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)

        result = asyncio.run(read_text(str(target)))
        assert result == "via absolute"

    def test_continue_policy_warns_on_mismatch(self, tmp_path, capsys):
        """read_text with --on-mismatch=continue warns when returning stale cache."""
        target_path = str(tmp_path / "mismatch.txt")
        resolved = _normalize_path(target_path)

        # Cache has old content; current request would use encoding latin-1
        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "read_text",
                "finish": True,
                "request": {"path": resolved, "encoding": "utf-8", "replay": "file"},
                "response": {"content": "old cached"},
            },
        ])
        _install_replay_ctx(loaded)
        set_mismatch_policy(MismatchPolicy.CONTINUE)

        # replay="file" uses inline content from the log on mismatch+continue
        result = asyncio.run(read_text(target_path, encoding="latin-1", replay="file"))
        assert result == "old cached"
        captured = capsys.readouterr()
        assert "hash mismatch" in captured.err.lower() or "hash mismatch" in captured.out.lower()


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
        """write_text emits a FINISHED event with bytes_written response."""
        target = tmp_path / "out.txt"
        run_id = "test-write-live"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        asyncio.run(write_text(str(target), "log me"))

        events = log.all_events()
        write_events = [e for e in events if e.op == "write_text"]
        assert len(write_events) == 1
        assert write_events[0].status == EventStatus.FINISHED
        assert write_events[0].request["path"] == _resolved(target)
        assert write_events[0].request["content"] == "log me"
        # NIT-1 fix: response carries path + bytes_written
        assert write_events[0].response["path"] == _resolved(target)
        assert write_events[0].response["bytes_written"] == len(b"log me")

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
        assert req["path"] == _resolved(target)
        assert req["content"] == "the content"
        assert req["encoding"] == "utf-8"

    def test_creates_parent_directories(self, tmp_path):
        """write_text creates nested parent directories as needed."""
        deep = tmp_path / "a" / "b" / "c" / "file.txt"
        asyncio.run(write_text(str(deep), "deep"))
        assert deep.read_text() == "deep"

    def test_write_is_atomic(self, tmp_path):
        """write_text writes via a temp file, renaming atomically into place.

        After the call, target exists with the final content and no stray
        .tmp siblings remain in the directory.
        """
        target = tmp_path / "atomic.txt"
        target.write_text("original")  # existing file

        asyncio.run(write_text(str(target), "replaced"))
        assert target.read_text() == "replaced"

        # No leftover .tmp siblings
        strays = [p for p in tmp_path.iterdir() if p.name != "atomic.txt"]
        assert strays == [], f"unexpected leftover files: {strays}"

    def test_emits_failed_event_on_permission_error(self, tmp_path, monkeypatch):
        """Write failure propagates and emits a FAILED event."""
        # Simulate a failure by monkey-patching _write_text_atomic to raise.
        from godel import io as godel_io

        def boom(path, content, encoding):
            raise PermissionError(f"simulated permission denied: {path}")

        monkeypatch.setattr(godel_io, "_write_text_atomic", boom)

        target = tmp_path / "denied.txt"
        run_id = "test-write-fail"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        with pytest.raises(PermissionError):
            asyncio.run(write_text(str(target), "nope"))

        events = log.all_events()
        write_events = [e for e in events if e.op == "write_text"]
        assert len(write_events) == 1
        assert write_events[0].status == EventStatus.FAILED
        assert write_events[0].response["error_type"] == "PermissionError"

    def test_accepts_encoding_parameter(self, tmp_path):
        """write_text honours the encoding kwarg."""
        target = tmp_path / "latin.txt"
        asyncio.run(write_text(str(target), "café", encoding="latin-1"))
        assert target.read_bytes() == "café".encode("latin-1")

    def test_large_content_truncated_in_log_but_written_in_full(self, tmp_path):
        """Files larger than _CONTENT_LOG_LIMIT are truncated in log only, not on disk."""
        target = tmp_path / "big.txt"
        big_content = "Y" * (_CONTENT_LOG_LIMIT + 5000)

        run_id = "test-write-big"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        asyncio.run(write_text(str(target), big_content))

        # On disk: full content
        assert target.read_text() == big_content
        # In log: truncated
        events = log.all_events()
        write_events = [e for e in events if e.op == "write_text"]
        stored = write_events[0].request["content"]
        assert len(stored) < len(big_content)
        assert "truncated from audit log" in stored
        assert write_events[0].response["bytes_written"] == len(big_content.encode("utf-8"))


# ---------------------------------------------------------------------------
# write_text — replay
# ---------------------------------------------------------------------------

class TestWriteTextReplay:
    def test_skips_write_on_replay(self, tmp_path):
        """On replay, write_text does NOT write to the filesystem."""
        target_path = str(tmp_path / "should_not_exist.txt")
        resolved = _normalize_path(target_path)

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "write_text",
                "finish": True,
                "request": {"path": resolved, "content": "written", "encoding": "utf-8"},
                "response": {},
            },
        ])
        _install_replay_ctx(loaded)

        asyncio.run(write_text(target_path, "written"))

        # File should NOT have been written during replay
        assert not Path(target_path).exists()

    def test_started_only_raises_unsafe_resume_error(self, tmp_path):
        """write_text in STARTED-only state raises UnsafeResumeError."""
        from godel._exceptions import UnsafeResumeError

        target_path = str(tmp_path / "partial.txt")
        resolved = _normalize_path(target_path)

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "write_text",
                "finish": False,  # STARTED but never FINISHED
                "request": {"path": resolved, "content": "partial", "encoding": "utf-8"},
                "response": {},
            },
        ])
        _install_replay_ctx(loaded)

        with pytest.raises(UnsafeResumeError):
            asyncio.run(write_text(target_path, "partial"))

    def test_continue_policy_warns_and_skips_write(self, tmp_path, capsys):
        """write_text with --on-mismatch=continue warns; write stays skipped."""
        target_path = str(tmp_path / "mismatch_write.txt")
        resolved = _normalize_path(target_path)

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "write_text",
                "finish": True,
                "request": {"path": resolved, "content": "cached", "encoding": "utf-8"},
                "response": {},
            },
        ])
        _install_replay_ctx(loaded)
        set_mismatch_policy(MismatchPolicy.CONTINUE)

        # Caller passes DIFFERENT content — triggers hash mismatch
        asyncio.run(write_text(target_path, "new content"))

        captured = capsys.readouterr()
        err = captured.err + captured.out
        assert "hash mismatch" in err.lower()
        # Write was skipped — file does not exist
        assert not Path(target_path).exists()

    def test_invalidate_policy_executes_fresh_write(self, tmp_path):
        """write_text with --on-mismatch=invalidate performs the new write."""
        target_path = str(tmp_path / "invalidate_write.txt")
        resolved = _normalize_path(target_path)

        loaded = _make_log_with_events(tmp_path / "logs", [
            {
                "op": "write_text",
                "finish": True,
                "request": {"path": resolved, "content": "old", "encoding": "utf-8"},
                "response": {},
            },
        ])
        _install_replay_ctx(loaded)
        set_mismatch_policy(MismatchPolicy.INVALIDATE)

        asyncio.run(write_text(target_path, "fresh content"))
        assert Path(target_path).read_text() == "fresh content"


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
        from godel._formatters import FORMATTERS
        from godel._events import Event, EventStatus
        event = Event(
            event_id="AABBCCDD00112233",
            run_id="test",
            seq=0,
            op="read_text",
            status=EventStatus.FINISHED,
            request={"path": "/tmp/foo.txt"},
            response={"content": "hello", "bytes_read": 5},
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
        )
        line = FORMATTERS["read_text"](event)
        assert "read_text" in line
        assert "FINISHED" in line

    def test_write_text_formatter_renders_base_line(self, tmp_path):
        from godel._formatters import FORMATTERS
        from godel._events import Event, EventStatus
        event = Event(
            event_id="AABBCCDD00112233",
            run_id="test",
            seq=0,
            op="write_text",
            status=EventStatus.FINISHED,
            request={"path": "/tmp/bar.txt", "content": "data"},
            response={"path": "/tmp/bar.txt", "bytes_written": 4},
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
        Also exercises the contextvars.copy_context() propagation path — the
        executor thread must see _privileged=True or the audit hook blocks.
        """
        import subprocess
        import sys

        project_root = str(Path(__file__).parent.parent)
        target = tmp_path / "strict_test.txt"

        code = f"""
import sys
sys.path.insert(0, {project_root!r})
from godel._strict_audit import install_audit_hook
install_audit_hook()

import asyncio
from godel.io import write_text, read_text

asyncio.run(write_text({str(target)!r}, "allowed"))
content = asyncio.run(read_text({str(target)!r}))
assert content == "allowed", f"unexpected content: {{content!r}}"
print("ok")
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"
        assert "ok" in result.stdout


# ---------------------------------------------------------------------------
# read_text replay modes — reread and file
# ---------------------------------------------------------------------------

def _large_content(size_bytes: int = 100 * 1024) -> str:
    """Generate JSONL content larger than the 64KB truncation limit."""
    line = '{"id": 12345, "title": "Software Engineer", "company": "Acme Corp"}\n'
    return line * ((size_bytes // len(line)) + 1)


class TestReadTextReplayReread:
    def test_returns_full_content_for_large_file(self, tmp_path):
        """replay='reread' returns full untruncated content on resume."""
        content = _large_content()
        target = tmp_path / "large.jsonl"
        target.write_text(content)
        resolved = _normalize_path(str(target))

        loaded = _make_log_with_events(tmp_path / "logs", [{
            "op": "read_text", "finish": True,
            "request": {"path": resolved, "encoding": "utf-8", "replay": "reread"},
            "response": {"content": content[:1000] + "\n... [truncated]", "bytes_read": len(content.encode())},
        }])
        _install_replay_ctx(loaded)

        result = asyncio.run(read_text(str(target), replay="reread"))
        assert result == content
        assert len(result.encode()) > _CONTENT_LOG_LIMIT_BYTES

    def test_sees_updated_file_content(self, tmp_path):
        """replay='reread' returns current disk content, not stale cache."""
        target = tmp_path / "data.txt"
        target.write_text("version 2")
        resolved = _normalize_path(str(target))

        loaded = _make_log_with_events(tmp_path / "logs", [{
            "op": "read_text", "finish": True,
            "request": {"path": resolved, "encoding": "utf-8", "replay": "reread"},
            "response": {"content": "version 1", "bytes_read": 9},
        }])
        _install_replay_ctx(loaded)

        assert asyncio.run(read_text(str(target))) == "version 2"


class TestReadTextReplayFile:
    def test_stores_and_retrieves_full_snapshot(self, tmp_path):
        """replay='file' round-trips a large file through a snapshot."""
        content = _large_content()
        target = tmp_path / "big.jsonl"
        target.write_text(content)

        # First run
        run_id = "test-file-cache"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        result = asyncio.run(read_text(str(target), replay="file"))
        assert result == content

        snap_dir = tmp_path / "runs" / run_id / "snapshots"
        snapshot_files = list(snap_dir.glob("*.content"))
        assert len(snapshot_files) == 1
        assert snapshot_files[0].read_text() == content

        events = [e for e in log.all_events() if e.op == "read_text" and e.status == EventStatus.FINISHED]
        assert "content_ref" in events[0].response
        log.close()

        # Resume — original file deleted
        target.unlink()
        loaded = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
        walker = ReplayWalker(loaded)
        _current_workflow.set(WorkflowContext(run_id=run_id, event_log=loaded, replay_walker=walker))

        replayed = asyncio.run(read_text(str(target), replay="file"))
        assert replayed == content

    def test_backward_compat_with_inline_content(self, tmp_path):
        """replay='file' falls back to inline content for old logs without content_ref."""
        resolved = _normalize_path(str(tmp_path / "old.txt"))

        loaded = _make_log_with_events(tmp_path / "logs", [{
            "op": "read_text", "finish": True,
            "request": {"path": resolved, "encoding": "utf-8", "replay": "file"},
            "response": {"content": "inline from old log", "bytes_read": 19},
        }])
        _install_replay_ctx(loaded)

        assert asyncio.run(read_text(str(tmp_path / "old.txt"), replay="file")) == "inline from old log"

    def test_large_jsonl_not_corrupted_on_resume(self, tmp_path):
        """A 100KB+ JSONL file must not have lines cut mid-string on resume."""
        import json
        lines = [json.dumps({"id": i, "desc": "x" * 200}) for i in range(500)]
        content = "\n".join(lines) + "\n"
        assert len(content.encode()) > _CONTENT_LOG_LIMIT_BYTES

        target = tmp_path / "seen.jsonl"
        target.write_text(content)

        run_id = "test-jsonl-roundtrip"
        log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
        _current_workflow.set(WorkflowContext(run_id=run_id, event_log=log))
        asyncio.run(read_text(str(target), replay="file"))
        log.close()

        target.unlink()
        loaded = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
        _current_workflow.set(WorkflowContext(
            run_id=run_id, event_log=loaded, replay_walker=ReplayWalker(loaded),
        ))

        replayed = asyncio.run(read_text(str(target), replay="file"))
        for i, line in enumerate(replayed.strip().split("\n")):
            obj = json.loads(line)
            assert obj["id"] == i, f"Line {i} has wrong id after resume"


def test_read_text_invalid_replay_mode_raises():
    """Passing an invalid replay mode raises ValueError."""
    with pytest.raises(ValueError, match="replay must be"):
        asyncio.run(read_text("/dev/null", replay="invalid"))


# ---------------------------------------------------------------------------
# Edge cases identified by adversarial review
# ---------------------------------------------------------------------------

class TestReadTextReplayEdgeCases:
    def test_reread_with_hash_mismatch_still_reads_disk(self, tmp_path, capsys):
        """replay='reread' + hash mismatch: re-reads from disk, no stale-cache warning."""
        target = tmp_path / "mismatch.txt"
        target.write_text("current content")
        resolved = _normalize_path(str(target))

        loaded = _make_log_with_events(tmp_path / "logs", [{
            "op": "read_text", "finish": True,
            "request": {"path": resolved, "encoding": "utf-8", "replay": "reread"},
            "response": {"content": "old cached content", "bytes_read": 18},
        }])
        _install_replay_ctx(loaded)
        set_mismatch_policy(MismatchPolicy.CONTINUE)

        result = asyncio.run(read_text(str(target), encoding="latin-1", replay="reread"))
        assert result == "current content"
        captured = capsys.readouterr()
        assert "returning cached content" not in captured.err

    def test_file_replay_corrupted_snapshot_falls_back_to_inline(self, tmp_path):
        """replay='file' with a corrupted snapshot file falls back to inline content."""
        run_id = "test-corrupt-snap"
        runs_dir = tmp_path / "runs"
        log = EventLog(run_id, runs_dir=str(runs_dir))
        started = log.emit_started(
            op="read_text", step_path=(), request={"path": "/fake", "encoding": "utf-8", "replay": "file"},
        )
        event_id = started.event_id
        log.emit_finished(event_id, response={
            "content_ref": event_id,
            "content": "inline fallback",
            "bytes_read": 15,
        })
        # Write a corrupted snapshot (invalid UTF-8 bytes)
        snap_dir = runs_dir / run_id / "snapshots"
        snap_dir.mkdir(parents=True)
        (snap_dir / f"{event_id}.content").write_bytes(b"\x80\x81\x82\xff")
        log.close()

        loaded = EventLog.load(run_id, runs_dir=str(runs_dir))
        walker = ReplayWalker(loaded)
        _current_workflow.set(WorkflowContext(run_id=run_id, event_log=loaded, replay_walker=walker))

        result = asyncio.run(read_text("/fake", replay="file"))
        assert result == "inline fallback"

    def test_file_replay_snapshot_write_failure_still_emits_finished(self, tmp_path):
        """Snapshot write failure (permissions) must not prevent emit_finished."""
        target = tmp_path / "data.txt"
        target.write_text("important content")

        run_id = "test-snap-write-fail"
        runs_dir = tmp_path / "runs"
        log = EventLog(run_id, runs_dir=str(runs_dir))
        ctx = WorkflowContext(run_id=run_id, event_log=log)
        _current_workflow.set(ctx)

        # Make snapshot dir read-only so the write fails
        snap_dir = runs_dir / run_id / "snapshots"
        snap_dir.mkdir(parents=True)
        snap_dir.chmod(0o444)

        try:
            result = asyncio.run(read_text(str(target), replay="file"))
            assert result == "important content"

            events = [e for e in log.all_events() if e.op == "read_text"]
            assert len(events) == 1
            assert events[0].status == EventStatus.FINISHED
            # No content_ref since snapshot write failed
            assert "content_ref" not in events[0].response
            # Inline truncated content is still present
            assert "content" in events[0].response
        finally:
            snap_dir.chmod(0o755)

    def test_file_replay_outside_workflow_context(self, tmp_path):
        """replay='file' outside a @workflow context reads normally, no snapshot."""
        target = tmp_path / "plain.txt"
        target.write_text("no workflow")

        result = asyncio.run(read_text(str(target), replay="file"))
        assert result == "no workflow"
        # No snapshot dir created since there's no event log
        assert not (tmp_path / "snapshots").exists()

    def test_snapshot_dir_not_created_on_read_path(self, tmp_path):
        """_snapshot_dir (read path) does not create the directory."""
        run_id = "test-no-mkdir"
        runs_dir = tmp_path / "runs"
        log = EventLog(run_id, runs_dir=str(runs_dir))

        from godel.io import _snapshot_dir
        d = _snapshot_dir(log)
        assert not d.exists()
        log.close()
