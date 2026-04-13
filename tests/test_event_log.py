"""Tests for EventLog class."""
import json
from godel._event_log import EventLog
from godel._events import EventStatus


def test_emit_started(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    event = log.emit_started(op="test", step_path=("a",), request={"x": 1})
    assert event.status == EventStatus.STARTED
    assert event.op == "test"
    assert event.step_path == ("a",)
    assert event.seq == 0
    assert event.request_hash != ""
    assert event.ts_start != ""
    log.close()


def test_emit_finished(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    event = log.emit_started(op="test", step_path=(), request={})
    log.emit_finished(event.event_id, response={"result": "ok"})
    assert event.status == EventStatus.FINISHED
    assert event.response == {"result": "ok"}
    assert event.ts_end is not None
    log.close()


def test_emit_failed(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    event = log.emit_started(op="test", step_path=(), request={})
    log.emit_failed(event.event_id, "boom")
    assert event.status == EventStatus.FAILED
    # Response now includes structured fields; verify the error message is present
    assert event.response["error"] == "boom"
    log.close()


def test_jsonl_lines(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    e1 = log.emit_started(op="a", step_path=(), request={})
    log.emit_finished(e1.event_id, response={})
    e2 = log.emit_started(op="b", step_path=(), request={})
    log.emit_finished(e2.event_id, response={})
    log.close()

    lines = (tmp_path / "test-run.jsonl").read_text().strip().split("\n")
    assert len(lines) == 4  # 2 STARTED + 2 FINISHED


def test_load_roundtrip(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    e1 = log.emit_started(op="a", step_path=("s1",), request={"x": 1})
    log.emit_finished(e1.event_id, response={"y": 2})
    e2 = log.emit_started(op="b", step_path=(), request={})
    log.emit_failed(
        e2.event_id, "err",
        error_type="ValueError",
        step_path=("s1",),
        source_location="workflow.py:10",
        remediation_hint="fix it",
    )
    log.close()

    loaded = EventLog.load("test-run", runs_dir=str(tmp_path))
    events = loaded.all_events()
    assert len(events) == 2
    assert events[0].status == EventStatus.FINISHED
    assert events[0].response == {"y": 2}
    assert events[1].status == EventStatus.FAILED
    # Assert FAILED response shape survives deserialization round-trip
    resp = events[1].response
    assert resp["error"] == "err"
    assert resp["error_type"] == "ValueError"
    assert resp["step_path"] == ["s1"]
    assert resp["source_location"] == "workflow.py:10"
    assert resp["remediation_hint"] == "fix it"
    loaded.close()


def test_emit_failed_response_kwarg_mutex(tmp_path):
    """Passing response= alongside keyword params raises AssertionError (WARN-5)."""
    log = EventLog("test-run", runs_dir=str(tmp_path))
    e = log.emit_started(op="test", step_path=(), request={})
    import pytest
    with pytest.raises(AssertionError, match="ignored when response= is provided"):
        log.emit_failed(
            e.event_id, "boom",
            response={"error": "boom"},
            error_type="ValueError",  # should trigger assertion
        )
    log.close()


def test_runs_dir_created(tmp_path):
    runs = tmp_path / "subdir" / "runs"
    log = EventLog("test-run", runs_dir=str(runs))
    assert runs.exists()
    log.close()


def test_seq_increments(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    e1 = log.emit_started(op="a", step_path=(), request={})
    e2 = log.emit_started(op="b", step_path=(), request={})
    assert e1.seq == 0
    assert e2.seq == 1
    log.close()


def test_get_event(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    event = log.emit_started(op="test", step_path=(), request={})
    assert log.get_event(event.event_id) is event
    assert log.get_event("nonexistent") is None
    log.close()


def test_all_events_order(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    e1 = log.emit_started(op="first", step_path=(), request={})
    e2 = log.emit_started(op="second", step_path=(), request={})
    events = log.all_events()
    assert events[0].op == "first"
    assert events[1].op == "second"
    log.close()
