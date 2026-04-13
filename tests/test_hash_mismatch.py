"""Tests for request_hash mismatch handling."""
import pytest
from godel._events import Event, EventStatus
from godel._event_log import EventLog
from godel._replay import (
    ReplayWalker, ReplayMatch, MismatchPolicy,
    set_mismatch_policy, handle_hash_mismatch,
    _cascade_invalidate,
)
import asyncio


@pytest.fixture(autouse=True)
def reset_policy():
    set_mismatch_policy(None)
    yield
    set_mismatch_policy(None)


def _build_log(tmp_path, events_data):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    created = []
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
        created.append(e)
    log.close()
    return EventLog.load("test-run", runs_dir=str(tmp_path)), created


def test_matching_hash_no_mismatch(tmp_path):
    log, _ = _build_log(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",), "request": {"cmd": "echo hi"}, "finish": True, "response": {}},
    ])
    walker = ReplayWalker(log)
    req_hash = Event.compute_request_hash({"cmd": "echo hi"})
    match = walker.try_match(("s1",), 0, 0, "run", request_hash=req_hash)
    assert match.hit is True
    assert match.hash_mismatch is False


def test_mismatched_hash_detected(tmp_path):
    log, _ = _build_log(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",), "request": {"cmd": "echo old"}, "finish": True, "response": {}},
    ])
    walker = ReplayWalker(log)
    new_hash = Event.compute_request_hash({"cmd": "echo new"})
    match = walker.try_match(("s1",), 0, 0, "run", request_hash=new_hash)
    assert match.hit is True
    assert match.hash_mismatch is True


def test_abort_policy_raises(tmp_path):
    log, events = _build_log(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",), "request": {"cmd": "old"}, "finish": True, "response": {}},
    ])
    walker = ReplayWalker(log)
    new_hash = Event.compute_request_hash({"cmd": "new"})
    match = walker.try_match(("s1",), 0, 0, "run", request_hash=new_hash)

    set_mismatch_policy(MismatchPolicy.ABORT)
    from godel._exceptions import ResumeError
    with pytest.raises(ResumeError, match="mismatch"):
        asyncio.run(handle_hash_mismatch(match, log))


def test_continue_policy_accepts(tmp_path):
    log, events = _build_log(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",), "request": {"cmd": "old"}, "finish": True, "response": {}},
    ])
    walker = ReplayWalker(log)
    new_hash = Event.compute_request_hash({"cmd": "new"})
    match = walker.try_match(("s1",), 0, 0, "run", request_hash=new_hash)

    set_mismatch_policy(MismatchPolicy.CONTINUE)
    result = asyncio.run(handle_hash_mismatch(match, log))
    assert result == MismatchPolicy.CONTINUE


def test_invalidate_policy_cascades(tmp_path):
    log = EventLog("test-run", runs_dir=str(tmp_path))
    e1 = log.emit_started(op="WORKFLOW_STARTED", step_path=(), request={})
    log.emit_finished(e1.event_id, response={})
    e2 = log.emit_started(op="run", step_path=("s1",), request={"cmd": "old"})
    log.emit_finished(e2.event_id, response={})
    # e2 has no children, but test the mechanics
    log.close()

    loaded = EventLog.load("test-run", runs_dir=str(tmp_path))
    _cascade_invalidate(loaded, e2.event_id)
    event = loaded.get_event(e2.event_id)
    assert event.status == EventStatus.INVALIDATED


def test_default_policy_is_abort(tmp_path):
    """Without explicit policy, default is abort."""
    log, _ = _build_log(tmp_path, [
        {"op": "WORKFLOW_STARTED", "finish": True},
        {"op": "run", "step_path": ("s1",), "request": {"cmd": "old"}, "finish": True, "response": {}},
    ])
    walker = ReplayWalker(log)
    match = walker.try_match(("s1",), 0, 0, "run", request_hash="different")
    match = ReplayMatch(hit=True, event=log.all_events()[1], cached_response={}, status=EventStatus.FINISHED, hash_mismatch=True)

    from godel._exceptions import ResumeError
    with pytest.raises(ResumeError):
        asyncio.run(handle_hash_mismatch(match, log))
