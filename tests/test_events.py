"""Tests for Event dataclass and EventStatus enum."""
from godel._events import Event, EventStatus


def test_event_status_values():
    assert EventStatus.STARTED.value == "STARTED"
    assert EventStatus.FINISHED.value == "FINISHED"
    assert EventStatus.FAILED.value == "FAILED"
    assert EventStatus.INVALIDATED.value == "INVALIDATED"
    assert EventStatus.SUSPENDED.value == "SUSPENDED"


def test_event_creation():
    e = Event(event_id="test-id", run_id="run-1", seq=0, op="test")
    assert e.event_id == "test-id"
    assert e.run_id == "run-1"
    assert e.seq == 0
    assert e.children_ids == []
    assert e.step_path == ()
    assert e.status == EventStatus.STARTED


def test_to_dict_from_dict_roundtrip():
    e = Event(
        event_id="test-id",
        run_id="run-1",
        seq=5,
        children_ids=["c1", "c2"],
        step_path=("workflow", "step_a"),
        invocation_seq=1,
        step_local_seq=2,
        op="run",
        request_hash="abc123",
        request={"cmd": "echo hi"},
        response={"stdout": "hi"},
        status=EventStatus.FINISHED,
        ts_start="2026-01-01T00:00:00Z",
        ts_end="2026-01-01T00:00:01Z",
    )
    d = e.to_dict()
    assert isinstance(d["step_path"], list)
    assert d["status"] == "FINISHED"

    e2 = Event.from_dict(d)
    assert e2.event_id == e.event_id
    assert e2.step_path == e.step_path
    assert isinstance(e2.step_path, tuple)
    assert e2.status == EventStatus.FINISHED
    assert e2.response == e.response
    assert e2.to_dict() == d


def test_compute_request_hash_deterministic():
    r1 = {"b": 2, "a": 1}
    r2 = {"a": 1, "b": 2}
    h1 = Event.compute_request_hash(r1)
    h2 = Event.compute_request_hash(r2)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_compute_request_hash_different_inputs():
    h1 = Event.compute_request_hash({"a": 1})
    h2 = Event.compute_request_hash({"a": 2})
    assert h1 != h2
