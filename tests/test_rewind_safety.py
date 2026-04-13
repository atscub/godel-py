"""Tests for rewind safety table enforcement and edge-case behavior."""
from __future__ import annotations

import pytest

from godel._event_log import EventLog
from godel._events import EventStatus
from godel._exceptions import RewindUnsafe
from godel._rewind import apply_rewind


def test_rewind_refuses_non_idempotent_run(tmp_path):
    """Non-idempotent run() in the invalidation subtree raises RewindUnsafe."""
    log = EventLog("test-unsafe", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="run",
        step_path=("s",),
        request={"cmd": "rm -rf /tmp/x", "idempotent": False},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={"stdout": ""})

    with pytest.raises(RewindUnsafe):
        apply_rewind(log, [a.event_id], "test")

    log.close()


def test_rewind_allows_idempotent_run(tmp_path):
    """run(..., idempotent=True) in the invalidation subtree is allowed."""
    log = EventLog("test-safe", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="run",
        step_path=("s",),
        request={"cmd": "echo hello", "idempotent": True},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={"stdout": "hello"})

    # Should not raise
    result = apply_rewind(log, [a.event_id], "test")
    assert result["invalidated_count"] >= 1

    log.close()


def test_rewind_allows_agent_call(tmp_path):
    """agent.call events are always safe to invalidate."""
    log = EventLog("test-agent", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="agent.call",
        step_path=("s",),
        request={"prompt": "hello"},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={"text": "hi"})

    result = apply_rewind(log, [a.event_id], "test")
    assert result["invalidated_count"] >= 1

    log.close()


def test_rewind_allows_det_ops(tmp_path):
    """det.now, det.random, det.uuid4 events are always safe to invalidate."""
    log = EventLog("test-det", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    for det_op in ["det.now", "det.random", "det.uuid4"]:
        b = log.emit_started(
            op=det_op,
            step_path=("s",),
            request={},
            parent_event_id=a.event_id,
        )
        log.emit_finished(b.event_id, response={"value": "x"})

    result = apply_rewind(log, [a.event_id], "test")
    assert result["invalidated_count"] >= 3

    log.close()


def test_rewind_error_includes_cmd(tmp_path):
    """RewindUnsafe carries the cmd string for actionable diagnosis."""
    log = EventLog("test-cmd", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="run",
        step_path=("s",),
        request={"cmd": "dangerous-cmd", "idempotent": False},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={})

    with pytest.raises(RewindUnsafe) as exc_info:
        apply_rewind(log, [a.event_id])

    assert exc_info.value.cmd == "dangerous-cmd"
    assert exc_info.value.event_id == b.event_id
    assert exc_info.value.step_path == ("s",)

    log.close()


def test_rewind_error_no_mutation_on_refusal(tmp_path):
    """Safety check happens before any graph mutation — log is unchanged on refusal."""
    log = EventLog("test-no-mutate", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="run",
        step_path=("s",),
        request={"cmd": "drop table users", "idempotent": False},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={})

    with pytest.raises(RewindUnsafe):
        apply_rewind(log, [a.event_id])

    # The graph must be completely untouched
    a_after = log.get_event(a.event_id)
    assert a_after.status == EventStatus.FINISHED
    assert b.event_id in a_after.children_ids

    b_after = log.get_event(b.event_id)
    assert b_after.status == EventStatus.FINISHED

    log.close()


def test_rewind_run_missing_idempotent_key_is_refused(tmp_path):
    """run() with no idempotent key at all defaults to unsafe (non-idempotent)."""
    log = EventLog("test-default-unsafe", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="run",
        step_path=("s",),
        request={"cmd": "make deploy"},  # no idempotent key
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={})

    with pytest.raises(RewindUnsafe):
        apply_rewind(log, [a.event_id])

    log.close()


def test_rewind_idempotent_integer_one_is_refused(tmp_path):
    """run(idempotent=1) must be refused — only idempotent=True (bool) is safe."""
    log = EventLog("test-idempotent-int", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="run",
        step_path=("s",),
        request={"cmd": "echo hi", "idempotent": 1},  # integer, not bool True
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={})

    with pytest.raises(RewindUnsafe):
        apply_rewind(log, [a.event_id])

    log.close()


def test_rewind_print_and_input_are_safe(tmp_path):
    """print and input ops are safe to invalidate."""
    log = EventLog("test-print-input", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    for safe_op in ["print", "input"]:
        b = log.emit_started(
            op=safe_op,
            step_path=("s",),
            request={"text": "hello"},
            parent_event_id=a.event_id,
        )
        log.emit_finished(b.event_id, response={})

    # Should not raise
    result = apply_rewind(log, [a.event_id], "test")
    assert result["invalidated_count"] >= 2

    log.close()


# ---------------------------------------------------------------------------
# WARN-3: Empty target_ids guard
# ---------------------------------------------------------------------------

def test_apply_rewind_empty_target_ids_raises(tmp_path):
    """apply_rewind() with an empty target_ids list must raise ValueError.

    Previously it would silently emit a vacuous REWIND event and return
    invalidated_count=0.  The guard ensures callers cannot accidentally no-op
    without getting a clear diagnostic.
    """
    log = EventLog("test-empty-targets", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    with pytest.raises(ValueError, match="empty target_ids"):
        apply_rewind(log, [], "accidental empty call")

    # The graph must be completely untouched — no REWIND event was emitted
    events_after = list(log.all_events())
    rewind_events = [e for e in events_after if e.op == "REWIND"]
    assert not rewind_events, "No REWIND event should be emitted for empty target_ids"

    log.close()


# ---------------------------------------------------------------------------
# WARN-1: Double-rewind on the same target — explicit already_rewound_ids
# ---------------------------------------------------------------------------

def test_double_rewind_same_target_returns_already_rewound_ids(tmp_path):
    """Rewinding to an already-rewound target returns it in already_rewound_ids.

    The second call is a no-op for that target (invalidated_count stays 0 for
    the already-rewound target) but callers can now distinguish this from a
    genuine successful rewind by inspecting already_rewound_ids.
    """
    log = EventLog("test-double-rewind", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="agent.call",
        step_path=("s",),
        request={"prompt": "hello"},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={"text": "hi"})

    # First rewind: invalidates b, returns invalidated_count=1
    result1 = apply_rewind(log, [a.event_id], "first rewind")
    assert result1["invalidated_count"] == 1
    assert b.event_id in result1["invalidated_ids"]
    assert result1["already_rewound_ids"] == []

    # Second rewind to the same target: a is still FINISHED (stays), but has no
    # children, so invalidated_count=0.  already_rewound_ids stays empty because
    # the *target* (a) is still FINISHED — it was not the invalidated node.
    # This tests the "no children left to invalidate" case.
    result2 = apply_rewind(log, [a.event_id], "second rewind, nothing left")
    assert result2["invalidated_count"] == 0
    assert result2["invalidated_ids"] == []
    assert result2["already_rewound_ids"] == []

    log.close()


def test_double_rewind_already_invalidated_target_reported(tmp_path):
    """If the rewind TARGET itself has been invalidated, it appears in already_rewound_ids.

    This happens when a cascade from a prior rewind also invalidated the node
    that a second caller tries to use as a target.
    """
    log = EventLog("test-already-invalidated-target", runs_dir=str(tmp_path))

    # root -> a -> b
    root = log.emit_started(op="step.enter", step_path=(), request={})
    log.emit_finished(root.event_id, response={})

    a = log.emit_started(
        op="step.enter",
        step_path=("a",),
        request={},
        parent_event_id=root.event_id,
    )
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="agent.call",
        step_path=("a",),
        request={"prompt": "x"},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={"text": "y"})

    # Rewind root -> invalidates a (and b transitively)
    result1 = apply_rewind(log, [root.event_id], "cut from root")
    assert a.event_id in result1["invalidated_ids"]
    assert b.event_id in result1["invalidated_ids"]

    # Now attempt to rewind using a (which is now INVALIDATED) as target
    result2 = apply_rewind(log, [a.event_id], "attempt rewind to invalidated node")
    assert result2["invalidated_count"] == 0
    assert a.event_id in result2["already_rewound_ids"], (
        "Invalidated target must appear in already_rewound_ids so callers can "
        "detect this silent no-op instead of mistaking count=0 for success"
    )

    log.close()


# ---------------------------------------------------------------------------
# WARN-2: Two REWIND events per rewind — phase field distinguishes them
# ---------------------------------------------------------------------------

def test_apply_rewind_emits_outcome_phase_event(tmp_path):
    """apply_rewind() alone emits exactly one REWIND event with phase='outcome'.

    When called directly (not through the rewind() primitive), apply_rewind()
    emits a single outcome-phase REWIND event.  The two-event scenario (intent
    + outcome) only occurs when the full rewind() -> RewindSignal -> @workflow ->
    apply_rewind() path is taken, tested separately below.
    """
    log = EventLog("test-rewind-phase-outcome", runs_dir=str(tmp_path))

    a = log.emit_started(op="step.enter", step_path=("s",), request={})
    log.emit_finished(a.event_id, response={})

    b = log.emit_started(
        op="agent.call",
        step_path=("s",),
        request={"prompt": "p"},
        parent_event_id=a.event_id,
    )
    log.emit_finished(b.event_id, response={"text": "r"})

    apply_rewind(log, [a.event_id], "phase test")

    rewind_events = [e for e in log.all_events() if e.op == "REWIND"]
    assert len(rewind_events) == 1, (
        f"Direct apply_rewind() call must emit exactly 1 REWIND event, "
        f"got {len(rewind_events)}: {[e.event_id for e in rewind_events]}"
    )
    assert rewind_events[0].request.get("phase") == "outcome", (
        f"Direct apply_rewind() event must have phase='outcome', "
        f"got {rewind_events[0].request.get('phase')!r}"
    )

    log.close()


def test_full_rewind_path_emits_intent_and_outcome_phases(tmp_path, monkeypatch):
    """Full rewind() -> @workflow path emits two REWIND events: intent then outcome.

    rewind() emits phase='intent' before raising RewindSignal.
    @workflow catches the signal and calls apply_rewind(), which emits phase='outcome'.
    The two events are distinguishable and both appear in the audit log.
    """
    import asyncio
    import json
    from pathlib import Path
    from godel import workflow, step
    from godel._rewind import rewind

    monkeypatch.chdir(tmp_path)
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def s():
            return 1

        val = await s()
        ctx = __import__("godel._context", fromlist=["_current_workflow"])._current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            target = ctx.last_step_event_id(1)
            await rewind(to=target, reason="warn2-phase-test")
        return val

    asyncio.run(wf())

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log file found"

    rewind_events = []
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("op") == "REWIND" and d.get("status") == "FINISHED":
                rewind_events.append(d)

    phases = [e.get("request", {}).get("phase") for e in rewind_events]
    assert "intent" in phases, (
        f"Expected a phase='intent' REWIND event from rewind() primitive; "
        f"got phases: {phases}"
    )
    assert "outcome" in phases, (
        f"Expected a phase='outcome' REWIND event from apply_rewind(); "
        f"got phases: {phases}"
    )
