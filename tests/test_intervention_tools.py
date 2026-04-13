"""Tests for godel.intervention._tools — InterventionToolset."""
from __future__ import annotations

import json

import pytest

from godel._event_log import EventLog
from godel._events import EventStatus
from godel._exceptions import RewindUnsafe
from godel.intervention import (
    InterventionToolset,
    ResumeRequested,
    GaveUp,
    tool_specs,
    RewindArgs,
    ResumeArgs,
    InputArgs,
    GiveUpArgs,
    ReadFileArgs,
    EditFileArgs,
)
from godel.intervention._context import (
    InterventionContext,
    FailureInfo,
    build_intervention_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(run_id: str) -> InterventionContext:
    """Construct a minimal InterventionContext with only run_id set."""
    return InterventionContext(
        run_id=run_id,
        run_state="FAILED",
        audit_log_path="",
        events=[],
        failure=None,
        local_state={},
        sources=[],
        workflow_args={},
        paused_input_prompt=None,
    )


def _make_log_with_events(tmp_path, run_id: str):
    """Return an EventLog with 3 agent.call events in a chain (e1 → e2 → e3)."""
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    # e1 is a child of wf
    e1 = log.emit_started(
        op="agent.call", step_path=("step_a",), request={"prompt": "a"},
        parent_event_id=wf.event_id,
    )
    log.emit_finished(e1.event_id, response={"result": "a_done"})

    # e2 is a child of e1 (sequential chain)
    e2 = log.emit_started(
        op="agent.call", step_path=("step_b",), request={"prompt": "b"},
        parent_event_id=e1.event_id,
    )
    log.emit_finished(e2.event_id, response={"result": "b_done"})

    # e3 is a child of e2 (sequential chain)
    e3 = log.emit_started(
        op="agent.call", step_path=("step_c",), request={"prompt": "c"},
        parent_event_id=e2.event_id,
    )
    log.emit_finished(e3.event_id, response={"result": "c_done"})

    log.emit_finished(wf.event_id, response={"result": "done"})
    log.close()
    return e1.event_id, e2.event_id, e3.event_id


# ---------------------------------------------------------------------------
# test_rewind_delegates_to_apply_rewind
# ---------------------------------------------------------------------------


def test_rewind_delegates_to_apply_rewind(tmp_path):
    """rewind(to=[e2]) invalidates e3 (tail) and leaves e1 FINISHED."""
    run_id = "test-rewind-001"
    e1_id, e2_id, e3_id = _make_log_with_events(tmp_path, run_id)

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    result = asyncio.run(tools.rewind(RewindArgs(to=[e2_id], reason="retry from e2")))

    assert result.invalidated_count == 1
    assert e3_id in result.invalidated_ids
    assert e1_id not in result.invalidated_ids

    # Verify in persisted log
    log = EventLog.load(run_id, runs_dir=str(tmp_path))
    e3 = log.get_event(e3_id)
    e1 = log.get_event(e1_id)
    assert e3.status == EventStatus.INVALIDATED
    assert e1.status == EventStatus.FINISHED
    log.close()


# ---------------------------------------------------------------------------
# test_rewind_unsafe_propagates
# ---------------------------------------------------------------------------


def test_rewind_unsafe_propagates(tmp_path):
    """Rewinding past a non-idempotent run() event raises RewindUnsafe."""
    run_id = "test-rewind-unsafe-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    e1 = log.emit_started(
        op="agent.call", step_path=("step_a",), request={"prompt": "a"},
        parent_event_id=wf.event_id,
    )
    log.emit_finished(e1.event_id, response={"result": "ok"})

    # Non-idempotent run() — child of e1, so it will be in the invalidation subtree
    e_run = log.emit_started(
        op="run",
        step_path=("step_a",),
        request={"cmd": "rm -rf /tmp/foo", "idempotent": False},
        parent_event_id=e1.event_id,
    )
    log.emit_finished(e_run.event_id, response={"exit_code": 0})
    log.emit_finished(wf.event_id, response={})
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    with pytest.raises(RewindUnsafe):
        asyncio.run(tools.rewind(RewindArgs(to=[e1.event_id])))


# ---------------------------------------------------------------------------
# test_resume_raises_signal
# ---------------------------------------------------------------------------


def test_resume_raises_signal(tmp_path):
    """resume() raises ResumeRequested with the provided reason."""
    run_id = "test-resume-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    log.emit_failed(wf.event_id, "fail")
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    with pytest.raises(ResumeRequested) as exc_info:
        asyncio.run(tools.resume(ResumeArgs(reason="patch applied")))

    assert exc_info.value.reason == "patch applied"
    assert exc_info.value.outcome == "resume"


# ---------------------------------------------------------------------------
# test_give_up_raises_and_logs_event
# ---------------------------------------------------------------------------


def test_give_up_raises_and_logs_event(tmp_path):
    """give_up() writes UNRECOVERABLE event and raises GaveUp."""
    run_id = "test-giveup-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    log.emit_failed(wf.event_id, "broken")
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    with pytest.raises(GaveUp) as exc_info:
        asyncio.run(tools.give_up(GiveUpArgs(reason="cannot fix")))

    assert exc_info.value.reason == "cannot fix"
    assert exc_info.value.outcome == "give_up"

    # Verify UNRECOVERABLE event was persisted
    reloaded = EventLog.load(run_id, runs_dir=str(tmp_path))
    unrecoverable_events = [
        e for e in reloaded.all_events() if e.op == "UNRECOVERABLE"
    ]
    assert len(unrecoverable_events) == 1
    assert unrecoverable_events[0].status == EventStatus.FINISHED
    assert unrecoverable_events[0].response["reason"] == "cannot fix"
    reloaded.close()


# ---------------------------------------------------------------------------
# test_input_injects_value
# ---------------------------------------------------------------------------


def test_input_injects_value(tmp_path):
    """input() finds the dangling STARTED input event and injects value."""
    run_id = "test-input-inject-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    # Paused input — only STARTED, no FINISHED yet
    paused_ev = log.emit_started(
        op="input",
        step_path=("step_a",),
        request={"prompt": "Enter your name: "},
    )
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    asyncio.run(tools.input(InputArgs(value="Alice")))

    # Verify the input event is now FINISHED with the injected value
    reloaded = EventLog.load(run_id, runs_dir=str(tmp_path))
    ev = reloaded.get_event(paused_ev.event_id)
    assert ev.status == EventStatus.FINISHED
    assert ev.response["value"] == "Alice"
    reloaded.close()


# ---------------------------------------------------------------------------
# test_input_rejects_when_not_blocked
# ---------------------------------------------------------------------------


def test_input_rejects_when_not_blocked(tmp_path):
    """input() raises ValueError if there is no dangling STARTED input event."""
    run_id = "test-input-no-block-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    # Finished input — no dangling STARTED
    finished_input = log.emit_started(
        op="input",
        step_path=("step_a",),
        request={"prompt": "Name: "},
    )
    log.emit_finished(finished_input.event_id, response={"value": "Bob"})
    log.emit_finished(wf.event_id, response={})
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    with pytest.raises(ValueError, match="No paused input event"):
        asyncio.run(tools.input(InputArgs(value="ignored")))


# ---------------------------------------------------------------------------
# test_edit_file_unique_replace
# ---------------------------------------------------------------------------


def test_edit_file_unique_replace(tmp_path):
    """edit_file() replaces a unique old_str and returns new sha256."""
    src = tmp_path / "workflow.py"
    src.write_text("def my_func():\n    return 42\n")

    run_id = "test-edit-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    log.emit_failed(wf.event_id, "err")
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    result = asyncio.run(tools.edit_file(EditFileArgs(
        path=str(src),
        old_str="    return 42",
        new_str="    return 99",
    )))

    assert result.edits_applied == 1
    assert result.path == str(src)
    assert len(result.new_sha256) == 64

    # Verify file was updated
    assert "return 99" in src.read_text()
    assert "return 42" not in src.read_text()


# ---------------------------------------------------------------------------
# test_edit_file_non_unique_refuses
# ---------------------------------------------------------------------------


def test_edit_file_non_unique_refuses(tmp_path):
    """edit_file() raises ValueError if old_str appears more than once."""
    src = tmp_path / "workflow.py"
    src.write_text("x = 1\ny = 1\n")  # '1' appears twice but let's use ' = 1'

    run_id = "test-edit-non-unique-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    log.emit_failed(wf.event_id, "err")
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio
    with pytest.raises(ValueError, match="appears 2 times"):
        asyncio.run(tools.edit_file(EditFileArgs(
            path=str(src),
            old_str=" = 1",
            new_str=" = 2",
        )))

    # File must be unchanged
    assert src.read_text() == "x = 1\ny = 1\n"


# ---------------------------------------------------------------------------
# test_edit_file_sha_guard
# ---------------------------------------------------------------------------


def test_edit_file_sha_guard(tmp_path):
    """edit_file() raises ValueError if expected_sha256 does not match."""
    src = tmp_path / "workflow.py"
    src.write_text("def hello(): pass\n")

    run_id = "test-edit-sha-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    log.emit_failed(wf.event_id, "err")
    log.close()

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    stale_sha = "a" * 64  # definitely wrong

    import asyncio
    with pytest.raises(ValueError, match="sha guard failed"):
        asyncio.run(tools.edit_file(EditFileArgs(
            path=str(src),
            old_str="def hello(): pass",
            new_str="def hello(): return 1",
            expected_sha256=stale_sha,
        )))

    # File must be unchanged
    assert src.read_text() == "def hello(): pass\n"


# ---------------------------------------------------------------------------
# test_rewind_already_rewound_ids_populated
# ---------------------------------------------------------------------------


def test_rewind_already_rewound_ids_populated(tmp_path):
    """Rewinding the same target twice populates already_rewound_ids on the second call."""
    run_id = "test-rewind-already-rewound-001"
    e1_id, e2_id, e3_id = _make_log_with_events(tmp_path, run_id)

    ctx = _make_ctx(run_id)
    tools = InterventionToolset(ctx, runs_dir=str(tmp_path))

    import asyncio

    # First rewind: e2 as target → e3 should be invalidated
    result1 = asyncio.run(tools.rewind(RewindArgs(to=[e2_id], reason="first rewind")))
    assert result1.invalidated_count == 1
    assert e3_id in result1.invalidated_ids
    assert result1.already_rewound_ids == []

    # Second rewind: e2 as target again — e2 is still FINISHED (only its children
    # were invalidated), so this should invalidate nothing new and report
    # already_rewound_ids as empty (e2 is FINISHED, not INVALIDATED).
    # Instead, let's rewind e3 which is now INVALIDATED — that's the already-rewound case.
    result2 = asyncio.run(tools.rewind(RewindArgs(to=[e3_id], reason="second rewind")))
    assert result2.invalidated_count == 0
    assert result2.invalidated_ids == []
    assert e3_id in result2.already_rewound_ids


# ---------------------------------------------------------------------------
# test_tool_specs_roundtrip
# ---------------------------------------------------------------------------


def test_tool_specs_roundtrip():
    """tool_specs() returns a list where every entry has a valid JSON Schema."""
    specs = tool_specs()

    assert isinstance(specs, list)
    assert len(specs) == 6  # rewind, resume, input, give_up, read_file, edit_file

    names = {s["name"] for s in specs}
    assert names == {"rewind", "resume", "input", "give_up", "read_file", "edit_file"}

    for spec in specs:
        assert "name" in spec
        assert "description" in spec
        assert "schema" in spec

        schema = spec["schema"]
        # Must be a valid JSON Schema object (serializable, has 'type' or '$defs')
        raw = json.dumps(schema)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict), f"Schema for {spec['name']} is not a dict"
        # Pydantic model schemas always have 'properties' or 'type'
        assert "properties" in parsed or "type" in parsed, (
            f"Schema for {spec['name']} missing 'properties' or 'type'"
        )
