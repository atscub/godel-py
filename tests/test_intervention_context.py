"""Tests for godel.intervention — build_intervention_context."""
from __future__ import annotations

import json

import pytest

from godel._event_log import EventLog
from godel.intervention import InterventionContext, build_intervention_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_crashed_run(tmp_path) -> str:
    """Write a JSONL log that mimics a crashed workflow and return run_id."""
    run_id = "run-crashed-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    # WORKFLOW_STARTED
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={
            "function": "my_workflow",
            "args": "()",
            "kwargs": "{}",
            "source_file": "",
        },
    )
    # A step that exits cleanly
    step1 = log.emit_started(
        op="step.exit",
        step_path=("fetch_data",),
        request={"tool": "fetch"},
    )
    log.emit_finished(step1.event_id, response={"rows": 42})
    # A step that fails
    step2 = log.emit_started(
        op="step.exit",
        step_path=("process_data",),
        request={"tool": "process"},
    )
    log.emit_failed(
        step2.event_id,
        "Division by zero",
        error_type="ZeroDivisionError",
        source_location="workflow.py:55",
        remediation_hint="check denominator",
    )
    # WORKFLOW_STARTED fails too
    log.emit_failed(
        wf.event_id,
        "Division by zero",
        error_type="ZeroDivisionError",
        source_location="workflow.py:55",
    )
    log.close()
    return run_id


def _make_clean_run(tmp_path) -> str:
    """Write a JSONL log for a successfully completed workflow."""
    run_id = "run-clean-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={
            "function": "my_workflow",
            "args": "()",
            "kwargs": "{}",
            "source_file": "",
        },
    )
    step1 = log.emit_started(
        op="step.exit",
        step_path=("do_thing",),
        request={},
    )
    log.emit_finished(step1.event_id, response={"ok": True})
    log.emit_finished(wf.event_id, response={"result": "done"})
    log.close()
    return run_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_context_from_crashed_run(tmp_path):
    """Crashed run → state=FAILED, failure.error populated."""
    run_id = _make_crashed_run(tmp_path)
    ctx = build_intervention_context(run_id, runs_dir=str(tmp_path))

    assert isinstance(ctx, InterventionContext)
    assert ctx.run_state == "FAILED"
    assert ctx.failure is not None
    assert ctx.failure.error == "Division by zero"
    assert ctx.failure.error_type == "ZeroDivisionError"
    assert ctx.failure.source_location == "workflow.py:55"
    assert ctx.run_id == run_id
    assert len(ctx.events) > 0


def test_build_context_from_clean_run(tmp_path):
    """Completed run → state=FINISHED, failure is None."""
    run_id = _make_clean_run(tmp_path)
    ctx = build_intervention_context(run_id, runs_dir=str(tmp_path))

    assert ctx.run_state == "FINISHED"
    assert ctx.failure is None
    assert ctx.run_id == run_id


def test_source_file_captured(tmp_path):
    """sources[0].content contains the workflow function name when source_file is set."""
    # Write a fake source file
    src_file = tmp_path / "my_workflow.py"
    src_file.write_text("async def my_workflow():\n    pass\n")

    run_id = "run-src-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={
            "function": "my_workflow",
            "args": "()",
            "kwargs": "{}",
            "source_file": str(src_file),
        },
    )
    log.emit_finished(wf.event_id, response={"result": "ok"})
    log.close()

    ctx = build_intervention_context(run_id, runs_dir=str(tmp_path))
    assert len(ctx.sources) == 1
    assert "my_workflow" in ctx.sources[0].content
    assert ctx.sources[0].sha256 != ""
    assert len(ctx.sources[0].sha256) == 64  # sha256 hex digest


def test_local_state_snapshot_has_step_returns(tmp_path):
    """Two completed step.exit events → two entries in local_state."""
    run_id = "run-steps-001"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    wf = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf", "args": "()", "kwargs": "{}", "source_file": ""},
    )
    s1 = log.emit_started(op="step.exit", step_path=("step_a",), request={})
    log.emit_finished(s1.event_id, response={"value": 1})
    s2 = log.emit_started(op="step.exit", step_path=("step_b",), request={})
    log.emit_finished(s2.event_id, response={"value": 2})
    log.emit_finished(wf.event_id, response={"result": "done"})
    log.close()

    ctx = build_intervention_context(run_id, runs_dir=str(tmp_path))
    returns = ctx.local_state["last_step_returns"]
    assert "step_a" in returns
    assert "step_b" in returns
    assert returns["step_a"] == {"value": 1}
    assert returns["step_b"] == {"value": 2}
    # recent_step_event_ids should have both events
    recent = ctx.local_state["recent_step_event_ids"]
    assert len(recent) == 2


def test_to_json_roundtrip(tmp_path):
    """to_json() produces valid JSON; events[0].op matches first event op."""
    run_id = _make_clean_run(tmp_path)
    ctx = build_intervention_context(run_id, runs_dir=str(tmp_path))

    raw = ctx.to_json()
    parsed = json.loads(raw)

    assert parsed["run_id"] == run_id
    assert parsed["run_state"] == "FINISHED"
    assert isinstance(parsed["events"], list)
    assert len(parsed["events"]) > 0
    assert parsed["events"][0]["op"] == ctx.events[0].op
    assert parsed["failure"] is None


def test_missing_runs_dir_raises(tmp_path):
    """Nonexistent run_id → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        build_intervention_context("nonexistent-run", runs_dir=str(tmp_path))
