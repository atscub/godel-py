"""Tests for apply_rewind() — graph-cut + cascade invalidation."""
from __future__ import annotations

import pytest

from godel._event_log import EventLog
from godel._events import EventStatus
from godel._rewind import apply_rewind


def _make_chain(tmp_path, run_id="test-chain"):
    """Build a chain A → B → C → D in the event log and return (log, a, b, c, d)."""
    log = EventLog(run_id, runs_dir=str(tmp_path))

    a = log.emit_started(op="STEP", step_path=("A",), request={"step": "A"})
    log.emit_finished(a.event_id, response={"result": "A done"})

    b = log.emit_started(
        op="STEP",
        step_path=("B",),
        request={"step": "B"},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={"result": "B done"})

    c = log.emit_started(
        op="STEP",
        step_path=("C",),
        request={"step": "C"},
        parent_event_id=b.event_id,
    )
    log.emit_finished(c.event_id, response={"result": "C done"})

    d = log.emit_started(
        op="STEP",
        step_path=("D",),
        request={"step": "D"},
        parent_event_id=c.event_id,
    )
    log.emit_finished(d.event_id, response={"result": "D done"})

    return log, a, b, c, d


# ---------------------------------------------------------------------------
# test_rewind_chain
# ---------------------------------------------------------------------------

def test_rewind_chain(tmp_path):
    """Rewind to B: C and D are invalidated, B stays FINISHED with empty children, A untouched."""
    log, a, b, c, d = _make_chain(tmp_path)

    result = apply_rewind(log, [b.event_id], reason="test chain rewind")

    # C and D should be invalidated
    assert log.get_event(c.event_id).status == EventStatus.INVALIDATED
    assert log.get_event(d.event_id).status == EventStatus.INVALIDATED

    # B stays FINISHED but children cleared
    b_after = log.get_event(b.event_id)
    assert b_after.status == EventStatus.FINISHED
    assert b_after.children_ids == []

    # A is untouched
    a_after = log.get_event(a.event_id)
    assert a_after.status == EventStatus.FINISHED
    assert b.event_id in a_after.children_ids

    # Return value counts invalidated nodes (C and D)
    assert result["invalidated_count"] == 2
    assert c.event_id in result["invalidated_ids"]
    assert d.event_id in result["invalidated_ids"]

    log.close()


# ---------------------------------------------------------------------------
# test_rewind_invalid_target
# ---------------------------------------------------------------------------

def test_rewind_invalid_target(tmp_path):
    """ValueError raised for nonexistent target event_id."""
    log = EventLog("test-invalid", runs_dir=str(tmp_path))
    event = log.emit_started(op="STEP", step_path=("A",), request={})
    log.emit_finished(event.event_id, response={})

    with pytest.raises(ValueError, match="rewind target event_id not found"):
        apply_rewind(log, ["nonexistent-id"], reason="bad")

    log.close()


# ---------------------------------------------------------------------------
# test_rewind_records_metadata_event
# ---------------------------------------------------------------------------

def test_rewind_records_metadata_event(tmp_path):
    """A REWIND op event is present and FINISHED in the log after apply_rewind."""
    log, a, b, c, d = _make_chain(tmp_path, run_id="test-meta")

    apply_rewind(log, [b.event_id], reason="metadata check")

    all_events = log.all_events()
    rewind_events = [e for e in all_events if e.op == "REWIND"]
    assert len(rewind_events) == 1

    rev = rewind_events[0]
    assert rev.status == EventStatus.FINISHED
    assert rev.invocation_seq == -1
    assert rev.step_local_seq == -1
    assert b.event_id in rev.request["targets"]
    assert rev.request["reason"] == "metadata check"

    log.close()


# ---------------------------------------------------------------------------
# test_rewind_is_append_only
# ---------------------------------------------------------------------------

def test_rewind_is_append_only(tmp_path):
    """The JSONL file has MORE lines after apply_rewind (never fewer)."""
    log, a, b, c, d = _make_chain(tmp_path, run_id="test-append")
    log.close()

    jsonl_path = tmp_path / "test-append.jsonl"
    lines_before = len([ln for ln in jsonl_path.read_text().splitlines() if ln.strip()])

    # Reopen and apply rewind
    log2 = EventLog.load("test-append", runs_dir=str(tmp_path))
    apply_rewind(log2, [b.event_id], reason="append-only check")
    log2.close()

    lines_after = len([ln for ln in jsonl_path.read_text().splitlines() if ln.strip()])
    assert lines_after > lines_before, (
        f"Expected more lines after rewind, got {lines_after} vs {lines_before} before"
    )


# ---------------------------------------------------------------------------
# test_rewind_leaf
# ---------------------------------------------------------------------------

def test_rewind_leaf(tmp_path):
    """Rewind to a leaf node (no children): nothing is invalidated, leaf stays FINISHED."""
    log, a, b, c, d = _make_chain(tmp_path, run_id="test-leaf")

    result = apply_rewind(log, [d.event_id], reason="leaf rewind")

    # D stays FINISHED, children_ids is already []
    d_after = log.get_event(d.event_id)
    assert d_after.status == EventStatus.FINISHED
    assert d_after.children_ids == []

    # Nothing is invalidated
    assert result["invalidated_count"] == 0
    assert result["invalidated_ids"] == []

    # Parents are untouched
    assert log.get_event(c.event_id).status == EventStatus.FINISHED

    log.close()


# ---------------------------------------------------------------------------
# test_rewind_diamond_dag
# ---------------------------------------------------------------------------

def test_rewind_diamond_dag(tmp_path):
    """Rewind over a diamond: A→B, A→C, B→D, C→D — D must be invalidated exactly once."""
    log = EventLog("test-diamond", runs_dir=str(tmp_path))

    a = log.emit_started(op="STEP", step_path=("A",), request={"step": "A"})
    log.emit_finished(a.event_id, response={"result": "A done"})

    b = log.emit_started(op="STEP", step_path=("B",), request={"step": "B"},
                         parent_event_id=a.event_id)
    log.emit_finished(b.event_id, response={"result": "B done"})

    c = log.emit_started(op="STEP", step_path=("C",), request={"step": "C"},
                         parent_event_id=a.event_id)
    log.emit_finished(c.event_id, response={"result": "C done"})

    # D is a child of both B and C (diamond convergence)
    d = log.emit_started(op="STEP", step_path=("D",), request={"step": "D"},
                         parent_event_id=b.event_id)
    log.emit_finished(d.event_id, response={"result": "D done"})
    # Also register D as a child of C to model the diamond
    c_event = log.get_event(c.event_id)
    c_event.children_ids.append(d.event_id)
    log._append_event(c_event)

    # Rewind to A: B, C, D should all be invalidated
    result = apply_rewind(log, [a.event_id], reason="diamond rewind")

    assert log.get_event(b.event_id).status == EventStatus.INVALIDATED
    assert log.get_event(c.event_id).status == EventStatus.INVALIDATED
    assert log.get_event(d.event_id).status == EventStatus.INVALIDATED

    # A stays FINISHED with cleared children
    a_after = log.get_event(a.event_id)
    assert a_after.status == EventStatus.FINISHED
    assert a_after.children_ids == []

    # D must appear exactly once in invalidated_ids (not twice)
    assert result["invalidated_ids"].count(d.event_id) == 1
    assert result["invalidated_count"] == 3  # B, C, D

    log.close()


# ---------------------------------------------------------------------------
# test_rewind_double_call
# ---------------------------------------------------------------------------

def test_rewind_double_call(tmp_path):
    """Calling apply_rewind twice on the same target is idempotent.

    The second call should be a no-op (target stays FINISHED, children already
    cleared) and must NOT re-invalidate anything or corrupt the log.
    """
    log, a, b, c, d = _make_chain(tmp_path, run_id="test-double")

    result1 = apply_rewind(log, [b.event_id], reason="first rewind")
    assert result1["invalidated_count"] == 2  # C and D

    # State after first rewind
    assert log.get_event(c.event_id).status == EventStatus.INVALIDATED
    assert log.get_event(d.event_id).status == EventStatus.INVALIDATED
    b_after = log.get_event(b.event_id)
    assert b_after.status == EventStatus.FINISHED
    assert b_after.children_ids == []

    # Second call: B is still FINISHED (not INVALIDATED), children_ids is []
    result2 = apply_rewind(log, [b.event_id], reason="second rewind")

    # Must be a no-op — nothing new should be invalidated
    assert result2["invalidated_count"] == 0
    assert result2["invalidated_ids"] == []

    # Statuses must not have changed
    assert log.get_event(c.event_id).status == EventStatus.INVALIDATED
    assert log.get_event(d.event_id).status == EventStatus.INVALIDATED
    assert log.get_event(b.event_id).status == EventStatus.FINISHED

    log.close()
