"""Tests for godel resume re-entering a PAUSED run cleanly.

Covers:
- Round-trip pause → resume with no duplicate step execution (step.enter event_id audit)
- Pause sentinel is cleared after resume_cmd entry
- PAUSED event is excluded from ReplayWalker._index
- Resuming a non-paused (crash-stopped) log still works (regression)
- PAUSED event_id is rejected as a rewind target (ValueError)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from godel import workflow, step
from godel._context import _pending_replay
from godel._event_log import EventLog
from godel._events import EventStatus
from godel._exceptions import PauseSignal
from godel._pause import write_pause_request
from godel._replay import ReplayWalker

PROJECT_ROOT = str(Path(__file__).parent.parent)


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )


def _all_events_by_id(log_path: Path) -> dict:
    """Read all JSONL events, last-write-wins per event_id."""
    from godel._events import Event
    raw_map: dict[str, Event] = {}
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            ev = Event.from_dict(d)
            raw_map[ev.event_id] = ev
    return raw_map


# ---------------------------------------------------------------------------
# Test 1: Round-trip — no duplicate step execution after pause → resume
# ---------------------------------------------------------------------------

def test_pause_resume_round_trip_no_duplicate_steps(tmp_path, monkeypatch):
    """3-step workflow paused at step_b; resume completes cleanly.

    Assertions:
    - step_b and step_c each produce a FINISHED step.enter event (ran live once).
    - step_a's FINISHED step.enter event_id is unchanged (no new event emitted).
    - No second WORKFLOW_STARTED event is created on resume.
    - The run completes with the expected result.

    Note on replay semantics: @step bodies always execute during the replay
    phase (their primitives use cached results). "No re-execution" means no NEW
    step.enter FINISHED events are emitted for already-cached steps — verified
    by checking that step_a's event_id is stable and there is exactly one
    FINISHED step.enter event for step_a in the final log.
    """
    monkeypatch.chdir(tmp_path)

    step_b_ran_live: dict[str, bool] = {"b": False, "c": False}
    # Flag to control pause injection — set to True only for the initial run.
    # The workflow body checks this flag so that write_pause_request is NOT
    # called again during the resume run (simulating an external `godel pause`
    # request rather than an in-workflow call).
    should_pause: dict[str, bool] = {"active": True}

    @workflow
    async def wf():
        @step
        async def step_a():
            return "a"

        @step
        async def step_b():
            step_b_ran_live["b"] = True
            return "b"

        @step
        async def step_c():
            step_b_ran_live["c"] = True
            return "c"

        await step_a()
        # Simulate external `godel pause` request: write sentinel only on the
        # initial run (not on resume). The sentinel fires at step_b boundary.
        if should_pause["active"]:
            from godel._context import _current_workflow
            ctx = _current_workflow.get()
            write_pause_request(ctx.run_id)
        await step_b()
        await step_c()
        return "done"

    # --- Initial run (pauses at step_b boundary) ---
    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    run_id = wf._last_run_id
    # Confirm step_b and step_c didn't run on initial (paused) run
    assert not step_b_ran_live["b"], "step_b should not have run before pause"
    assert not step_b_ran_live["c"], "step_c should not have run before pause"
    # Disable pause injection for the resume run
    should_pause["active"] = False

    # Capture step_a's FINISHED event_id from the log
    log_path = tmp_path / "runs" / f"{run_id}.jsonl"
    initial_events = _all_events_by_id(log_path)
    paused_events = [e for e in initial_events.values() if e.op == "PAUSED"]
    assert len(paused_events) == 1, "Expected exactly one PAUSED event after initial run"

    step_a_finished = next(
        (e for e in initial_events.values()
         if e.op == "step.enter" and e.step_path == ("step_a",)
         and e.status == EventStatus.FINISHED),
        None,
    )
    assert step_a_finished is not None, "step_a FINISHED event not found"
    original_step_a_event_id = step_a_finished.event_id

    # --- Resume ---
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    assert result == "done", f"Resume did not complete cleanly: {result!r}"

    # step_b and step_c must have run live on resume
    assert step_b_ran_live["b"], "step_b did not run on resume"
    assert step_b_ran_live["c"], "step_c did not run on resume"

    # step_a's FINISHED event_id must be unchanged (no new event emitted)
    reloaded = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    step_a_events_after = [
        e for e in reloaded.all_events()
        if e.op == "step.enter" and e.step_path == ("step_a",)
        and e.status == EventStatus.FINISHED
    ]
    step_a_event_ids_after = {e.event_id for e in step_a_events_after}
    assert original_step_a_event_id in step_a_event_ids_after, (
        "step_a FINISHED event_id changed after resume"
    )
    assert len(step_a_event_ids_after) == 1, (
        f"Multiple distinct step_a FINISHED event_ids after resume: {step_a_event_ids_after}"
    )

    # No second WORKFLOW_STARTED event
    wf_starts = [e for e in reloaded.all_events() if e.op == "WORKFLOW_STARTED"]
    assert len(wf_starts) == 1, (
        f"Expected 1 WORKFLOW_STARTED event, got {len(wf_starts)}"
    )

    # step_b and step_c must each have exactly one FINISHED step.enter event
    for step_name in ("step_b", "step_c"):
        finished = [
            e for e in reloaded.all_events()
            if e.op == "step.enter" and e.step_path == (step_name,)
            and e.status == EventStatus.FINISHED
        ]
        assert len(finished) == 1, (
            f"Expected 1 FINISHED step.enter for {step_name}, got {len(finished)}"
        )

    reloaded.close()


# ---------------------------------------------------------------------------
# Test 2: resume_cmd clears the pause sentinel
# ---------------------------------------------------------------------------

PAUSE_RESUME_WORKFLOW = '''\
from godel import workflow, step

@workflow
async def wf():
    @step
    async def step_a():
        return "a"

    @step
    async def step_b():
        return "b"

    await step_a()
    await step_b()
    return "done"
'''


def test_resume_clears_pause_sentinel(tmp_path):
    """After godel resume completes, the pause sentinel file is absent."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(PAUSE_RESUME_WORKFLOW)

    # First run — produces a valid JSONL
    result = _run_godel(["run", "--no-strict", str(wf_file)], cwd=str(tmp_path))
    assert result.returncode == 0, f"Initial run failed: {result.stderr}"

    # Extract run_id
    run_id = None
    for line in result.stderr.strip().split("\n"):
        if line.startswith("[godel] run ") and "completed" not in line and "resume" not in line:
            run_id = line.split("run ")[1].strip()
            break
    assert run_id is not None, f"Could not find run_id in: {result.stderr}"

    # Write a pause sentinel manually (simulating a prior pause)
    runs_dir = tmp_path / "runs"
    write_pause_request(run_id, reason="test pause", runs_dir=str(runs_dir))
    sentinel = runs_dir / f"{run_id}.pause"
    assert sentinel.exists(), "Sentinel should exist before resume"

    # Resume
    result2 = _run_godel(
        ["resume", "--no-strict", run_id[:8], str(wf_file)],
        cwd=str(tmp_path),
    )
    assert result2.returncode == 0, f"Resume failed: {result2.stderr}"

    # Sentinel must be gone
    assert not sentinel.exists(), (
        "Pause sentinel still present after resume_cmd — should have been cleared"
    )


# ---------------------------------------------------------------------------
# Test 3: PAUSED event excluded from ReplayWalker._index
# ---------------------------------------------------------------------------

def test_paused_event_excluded_from_replay_index(tmp_path, monkeypatch):
    """Unit test: a PAUSED event in the log must not appear in _index."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def only():
            return "x"

        from godel._context import _current_workflow
        ctx = _current_workflow.get()
        write_pause_request(ctx.run_id)
        await only()

    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    run_id = wf._last_run_id

    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    # The _index must contain no PAUSED entries
    paused_in_index = [
        key for key in walker._index
        if key[3] == "PAUSED"  # key = (step_path, invocation_seq, step_local_seq, op)
    ]
    assert paused_in_index == [], (
        f"PAUSED events should not appear in ReplayWalker._index, found: {paused_in_index}"
    )

    event_log.close()


# ---------------------------------------------------------------------------
# Test 4: Resume from non-paused (crash-stopped) log still works (regression)
# ---------------------------------------------------------------------------

def test_resume_from_non_paused_log_still_works(tmp_path, monkeypatch):
    """Resuming a run that stopped without a PAUSED event (e.g. crash) completes."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def step_a():
            return "a"

        @step
        async def step_b():
            return "b"

        await step_a()
        await step_b()
        return "done"

    asyncio.run(wf())
    run_id = wf._last_run_id

    # Confirm no PAUSED event in log
    log_path = tmp_path / "runs" / f"{run_id}.jsonl"
    events = _all_events_by_id(log_path)
    assert not any(e.op == "PAUSED" for e in events.values()), (
        "Unexpected PAUSED event in clean-completion log"
    )

    # Resume
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)
    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    assert result == "done"


# ---------------------------------------------------------------------------
# Test 5: PAUSED event_id is rejected as a rewind target
# ---------------------------------------------------------------------------

def test_rewind_rejects_paused_target(tmp_path, monkeypatch):
    """Passing a PAUSED event's event_id as a rewind target raises ValueError."""
    from godel._rewind import apply_rewind

    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def only():
            return "x"

        from godel._context import _current_workflow
        ctx = _current_workflow.get()
        write_pause_request(ctx.run_id)
        await only()

    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    run_id = wf._last_run_id

    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    paused_event = next(
        (e for e in event_log.all_events() if e.op == "PAUSED"),
        None,
    )
    assert paused_event is not None, "Expected a PAUSED event in log"

    with pytest.raises(ValueError, match="Cannot rewind to a PAUSED metadata event"):
        apply_rewind(event_log, [paused_event.event_id], reason="test")

    event_log.close()


# ---------------------------------------------------------------------------
# Test 6: PAUSED event_id is rejected as a rewind target via programmatic rewind()
# ---------------------------------------------------------------------------

def test_rewind_fn_rejects_paused_target(tmp_path, monkeypatch):
    """godel.rewind(to=<paused_event_id>) raises ValueError inside a workflow."""
    from godel._rewind import rewind as rewind_fn

    monkeypatch.chdir(tmp_path)

    # First run: pause after only step
    @workflow
    async def wf_pauser():
        @step
        async def only():
            return "x"

        from godel._context import _current_workflow
        ctx = _current_workflow.get()
        write_pause_request(ctx.run_id)
        await only()

    with pytest.raises(PauseSignal):
        asyncio.run(wf_pauser())

    run_id = wf_pauser._last_run_id
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    paused_ev = next(
        (e for e in event_log.all_events() if e.op == "PAUSED"),
        None,
    )
    assert paused_ev is not None
    paused_event_id = paused_ev.event_id
    event_log.close()

    # Second workflow that attempts to rewind to the PAUSED event
    @workflow
    async def wf_rewinder():
        @step
        async def setup():
            return "setup"

        await setup()
        await rewind_fn(to=paused_event_id, reason="test rejection")

    # Resume from the paused log so the context has a valid event_log
    event_log2 = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log2)
    token = _pending_replay.set(walker)
    try:
        with pytest.raises(ValueError, match="Cannot rewind to a PAUSED metadata event"):
            asyncio.run(wf_rewinder())
    finally:
        _pending_replay.reset(token)
        event_log2.close()
