"""Tests for ReplayWalker class."""
from godel._events import Event, EventStatus
from godel._event_log import EventLog
from godel._replay import ReplayWalker


def _build_log_with_events(tmp_path, events_data):
    """Build an EventLog with pre-defined events for testing."""
    log = EventLog("test-run", runs_dir=str(tmp_path))
    for ed in events_data:
        e = log.emit_started(
            op=ed["op"],
            step_path=ed.get("step_path", ()),
            request=ed.get("request", {}),
            invocation_seq=ed.get("invocation_seq", 0),
            step_local_seq=ed.get("step_local_seq", 0),
        )
        if ed.get("finish"):
            log.emit_finished(e.event_id, response=ed.get("response", {}))
        elif ed.get("fail"):
            log.emit_failed(e.event_id, ed.get("error", "test error"))
    log.close()
    return EventLog.load("test-run", runs_dir=str(tmp_path))


def test_finished_event_returns_cached(tmp_path):
    log = _build_log_with_events(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True, "response": {"result": "ok"}},
        {"op": "run", "step_path": ("s1",), "finish": True, "response": {"stdout": "hello"}},
    ])
    walker = ReplayWalker(log)
    match = walker.try_match(("s1",), 0, 0, "run")
    assert match.hit is True
    assert match.status == EventStatus.FINISHED
    assert match.cached_response == {"stdout": "hello"}


def test_started_only_returns_started(tmp_path):
    log = _build_log_with_events(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",)},  # no finish
    ])
    walker = ReplayWalker(log)
    match = walker.try_match(("s1",), 0, 0, "run")
    assert match.hit is True
    assert match.status == EventStatus.STARTED
    assert match.cached_response is None


def test_no_match_returns_miss(tmp_path):
    log = _build_log_with_events(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
    ])
    walker = ReplayWalker(log)
    match = walker.try_match(("s1",), 0, 0, "run")
    assert match.hit is False


def test_is_replaying_transitions(tmp_path):
    log = _build_log_with_events(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",), "finish": True, "response": {}},
    ])
    walker = ReplayWalker(log)
    assert walker.is_replaying is True
    walker.try_match(("s1",), 0, 0, "run")  # hit
    assert walker.is_replaying is True
    walker.try_match(("s2",), 0, 0, "run")  # miss
    assert walker.is_replaying is False


def test_hash_mismatch_detected(tmp_path):
    log = _build_log_with_events(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",), "request": {"cmd": "old"}, "finish": True, "response": {}},
    ])
    walker = ReplayWalker(log)
    # Try with different request hash
    new_hash = Event.compute_request_hash({"cmd": "new"})
    match = walker.try_match(("s1",), 0, 0, "run", request_hash=new_hash)
    assert match.hit is True
    assert match.hash_mismatch is True


def test_get_workflow_args(tmp_path):
    log = _build_log_with_events(tmp_path, [
        {"op": "WORKFLOW_STARTED", "request": {"function": "my_wf", "args": "()"}, "finish": True},
    ])
    walker = ReplayWalker(log)
    args = walker.get_workflow_args()
    assert args["function"] == "my_wf"


def test_invalidated_events_skipped(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    e = log.emit_started(op="WORKFLOW_STARTED", step_path=(), request={})
    log.emit_finished(e.event_id, response={})
    e2 = log.emit_started(op="run", step_path=("s1",), request={})
    log.emit_finished(e2.event_id, response={"cached": True})
    # Manually invalidate
    e2.status = EventStatus.INVALIDATED
    log._append_event(e2)
    log.close()

    loaded = EventLog.load("test-run", runs_dir=str(tmp_path))
    walker = ReplayWalker(loaded)
    match = walker.try_match(("s1",), 0, 0, "run")
    # INVALIDATED event should not match
    assert match.hit is False


def test_loop_invocation_seq(tmp_path):
    """Same step_path, different invocation_seq -- should match independently."""
    log = _build_log_with_events(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "step.enter", "step_path": ("loop_step",), "invocation_seq": 0, "finish": True, "response": {"i": 0}},
        {"op": "step.enter", "step_path": ("loop_step",), "invocation_seq": 1, "finish": True, "response": {"i": 1}},
    ])
    walker = ReplayWalker(log)
    m0 = walker.try_match(("loop_step",), 0, 0, "step.enter")
    m1 = walker.try_match(("loop_step",), 1, 0, "step.enter")
    assert m0.hit is True
    assert m0.cached_response == {"i": 0}
    assert m1.hit is True
    assert m1.cached_response == {"i": 1}
