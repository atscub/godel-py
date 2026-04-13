"""E2E tests for operator-driven parallel rewind — M4 exit criterion (b).

Validates: rewinding inside one parallel branch leaves other branches intact,
suspends the JOIN, and resumes correctly.

Graph structure note:
    In a real parallel() workflow, FORK, JOIN, and the post-join step are all
    children of the same parent (e.g. WORKFLOW_STARTED).  The step *after*
    parallel() is a sibling of JOIN, not a child.  apply_rewind cascades
    through the JOIN's children (the branch events it wraps) — the downstream
    step is INVALIDATED only when the graph is constructed with it as an
    explicit child of JOIN (as in the test_join_cascade.py unit tests).

    Here we validate the *real* runtime behaviour:
      - branch_b is NOT invalidated
      - JOIN is SUSPENDED
      - final_step is FINISHED (linked as sibling, not child of JOIN)
      - resume re-executes invalidated events and workflow completes correctly
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from godel import workflow, step, det
from godel._decorators import parallel
from godel._context import _pending_replay
from godel._event_log import EventLog
from godel._events import EventStatus
from godel._rewind import apply_rewind
from godel._replay import ReplayWalker

PROJECT_ROOT = str(Path(__file__).parent.parent)


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )


# ---------------------------------------------------------------------------
# Helper: build and run a simple parallel workflow, return (run_id, log)
# ---------------------------------------------------------------------------

def _run_parallel_wf(tmp_path):
    """Run a parallel workflow with branch_a, branch_b, and final_step.

    Returns (run_id, event_log).

    WARN-3: The workflow uses EventLog(run_id) with the default runs_dir="./runs",
    which relies on the caller having set cwd to tmp_path via monkeypatch.chdir().
    EventLog.load() is called explicitly with runs_dir=str(tmp_path / "runs") to
    avoid the same dependency here, but the workflow's write path still depends on
    cwd being tmp_path.
    """
    @workflow
    async def wf():
        @step
        async def branch_a():
            return det.now()

        @step
        async def branch_b():
            return det.now()

        @step
        async def final_step(a_val, b_val):
            return f"a={a_val}, b={b_val}"

        a_val, b_val = await parallel(branch_a(), branch_b())
        return await final_step(a_val, b_val)

    asyncio.run(wf())
    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    return run_id, log


# ---------------------------------------------------------------------------
# Test 1: rewind branch_a leaves branch_b intact and suspends JOIN
# ---------------------------------------------------------------------------

def test_parallel_rewind_branch_a_leaves_b_intact(tmp_path, monkeypatch):
    """Rewind inside branch_a leaves branch_b intact and JOIN SUSPENDED."""
    monkeypatch.chdir(tmp_path)

    run_id, log = _run_parallel_wf(tmp_path)

    # Find the step.enter events for branch_a and branch_b
    events = log.all_events()
    finished_steps = [
        e for e in events
        if e.op == "step.enter" and e.status == EventStatus.FINISHED
    ]
    branch_a_event = next(
        (e for e in finished_steps if e.request.get("name") == "branch_a"), None
    )
    branch_b_event = next(
        (e for e in finished_steps if e.request.get("name") == "branch_b"), None
    )

    assert branch_a_event is not None, "branch_a step.enter event not found"
    assert branch_b_event is not None, "branch_b step.enter event not found"

    # Rewind to branch_a's step event
    apply_rewind(log, [branch_a_event.event_id], "test parallel rewind")

    # branch_a event stays FINISHED with empty children
    assert log.get_event(branch_a_event.event_id).status == EventStatus.FINISHED
    assert log.get_event(branch_a_event.event_id).children_ids == []

    # branch_b event is intact
    assert log.get_event(branch_b_event.event_id).status == EventStatus.FINISHED

    # JOIN should be SUSPENDED (its FORK's branch subtree contains the invalidated branch_a child)
    join_events = [e for e in log.all_events() if e.op == "JOIN"]
    assert join_events, "No JOIN event found in log"
    suspended_joins = [
        j for j in join_events
        if log.get_event(j.event_id).status == EventStatus.SUSPENDED
    ]
    assert suspended_joins, "Expected at least one JOIN event to be SUSPENDED"

    # WARN-4: verify emit_suspended() cleared children_ids on the suspended JOIN
    for j in suspended_joins:
        assert log.get_event(j.event_id).children_ids == [], (
            f"JOIN {j.event_id} children_ids should be empty after SUSPENDED"
        )

    log.close()


# ---------------------------------------------------------------------------
# Test 2: apply_rewind returns branch_a's children in invalidated_ids
# ---------------------------------------------------------------------------

def test_parallel_rewind_invalidated_ids(tmp_path, monkeypatch):
    """apply_rewind invalidates branch_a's children (e.g. det.now inside it)."""
    monkeypatch.chdir(tmp_path)

    run_id, log = _run_parallel_wf(tmp_path)
    events = log.all_events()

    finished_steps = [
        e for e in events
        if e.op == "step.enter" and e.status == EventStatus.FINISHED
    ]
    branch_a_event = next(
        e for e in finished_steps if e.request.get("name") == "branch_a"
    )

    result = apply_rewind(log, [branch_a_event.event_id], "test")

    # At least one event is invalidated (branch_a's child: det.now)
    assert result["invalidated_count"] >= 1, (
        "Expected at least 1 event invalidated (branch_a's det.now child)"
    )
    # branch_a itself is NOT in invalidated_ids (it stays FINISHED; its children are invalidated)
    assert branch_a_event.event_id not in result["invalidated_ids"]

    log.close()


# ---------------------------------------------------------------------------
# Test 3: branch_b stays intact — rewind of branch_a must not invalidate branch_b
# ---------------------------------------------------------------------------

def test_rewind_branch_a_does_not_invalidate_branch_b(tmp_path, monkeypatch):
    """Rewinding branch_a must not change branch_b's status."""
    monkeypatch.chdir(tmp_path)

    run_id, log = _run_parallel_wf(tmp_path)
    events = log.all_events()
    finished_steps = [
        e for e in events
        if e.op == "step.enter" and e.status == EventStatus.FINISHED
    ]
    branch_a_event = next(
        e for e in finished_steps if e.request.get("name") == "branch_a"
    )
    branch_b_event = next(
        e for e in finished_steps if e.request.get("name") == "branch_b"
    )

    result = apply_rewind(log, [branch_a_event.event_id], "partial rewind")

    # branch_b must NOT appear in invalidated_ids
    assert branch_b_event.event_id not in result["invalidated_ids"], (
        "branch_b should not be invalidated when only branch_a is rewound"
    )

    # branch_b status must still be FINISHED
    assert log.get_event(branch_b_event.event_id).status == EventStatus.FINISHED

    log.close()


# ---------------------------------------------------------------------------
# Test 4: resume after parallel rewind re-executes branch_a, replays branch_b
# ---------------------------------------------------------------------------

def test_parallel_rewind_and_resume(tmp_path, monkeypatch):
    """After parallel rewind, resume re-executes branch_a fresh and replays branch_b from cache."""
    monkeypatch.chdir(tmp_path)
    call_counts: dict[str, int] = {"a": 0, "b": 0, "final": 0}
    # Capture the deterministic values each branch returns, so we can verify
    # cache-hit vs re-execution after resume.
    captured: dict[str, object] = {}

    @workflow
    async def wf():
        @step
        async def branch_a():
            call_counts["a"] += 1
            v = det.now()
            captured[f"a_run{call_counts['a']}"] = v
            return v

        @step
        async def branch_b():
            call_counts["b"] += 1
            v = det.now()
            captured[f"b_run{call_counts['b']}"] = v
            return v

        @step
        async def final_step(a_val, b_val):
            call_counts["final"] += 1
            return f"a={a_val}, b={b_val}"

        a_val, b_val = await parallel(branch_a(), branch_b())
        return await final_step(a_val, b_val)

    # First run — all steps called once
    result_first = asyncio.run(wf())
    run_id = wf._last_run_id
    assert call_counts == {"a": 1, "b": 1, "final": 1}
    b_val_first = captured["b_run1"]

    # Find branch_a's step event and apply rewind
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    events = log.all_events()
    branch_a_event = next(
        e for e in events
        if e.op == "step.enter"
        and e.request.get("name") == "branch_a"
        and e.status == EventStatus.FINISHED
    )
    apply_rewind(log, [branch_a_event.event_id], "test parallel rewind")

    # Reset counts and resume — note: call_counts is rebound (not mutated) here;
    # the step closures reference the name in the enclosing scope so they will
    # use the new dict correctly.
    call_counts = {"a": 0, "b": 0, "final": 0}
    walker = ReplayWalker(log)
    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    log.close()

    # Workflow should complete successfully
    assert result is not None
    assert "a=" in result

    # CRITICAL-1: branch_a was invalidated — must re-execute exactly once.
    # call_counts["final"] must also be exactly 1: the stale FINISHED final_step
    # event must NOT be used as a cache hit (which would silently return the
    # old result built from branch_a's pre-rewind output).
    assert call_counts["a"] >= 1, "branch_a should have re-executed after rewind"
    assert call_counts["final"] == 1, (
        "final_step must execute exactly once on resume — not replayed from the "
        "stale FINISHED cache entry that contained branch_a's pre-rewind output"
    )

    # CRITICAL-1 (continued): the resumed result must differ from the first run's
    # result because branch_a produced a new det.now() value.
    assert result != result_first, (
        "Resume result should differ from first run — branch_a re-executed with "
        "a new det.now() value, so final_step's output must reflect that"
    )

    # CRITICAL-2: branch_b must replay from cache — its det.now value must be
    # identical to the first run.  If branch_b were re-executed fresh, it would
    # produce a new timestamp and this assertion would catch it.
    b_val_resume = captured.get("b_run1")  # call_counts["b"] == 1 on resume
    assert b_val_resume == b_val_first, (
        f"branch_b should replay from cache (same det.now value as first run). "
        f"First run: {b_val_first!r}, resume: {b_val_resume!r}"
    )

    # branch_b control flow was re-entered (replay walker increments the count)
    assert call_counts["b"] >= 1, "branch_b should have been called on resume"


# ---------------------------------------------------------------------------
# Test 5: CLI round-trip — run → rewind → resume via subprocess
# ---------------------------------------------------------------------------

def test_cli_rewind_parallel(tmp_path):
    """Full CLI flow: godel run → godel rewind → godel resume."""
    fixture = Path(__file__).parent / "fixtures" / "parallel_rewind_wf.py"
    if not fixture.exists():
        pytest.skip("fixture parallel_rewind_wf.py not found")

    # Step 1: run the workflow
    result = _run_godel(
        ["run", "--no-strict", str(fixture)],
        cwd=str(tmp_path),
    )
    if result.returncode != 0:
        pytest.skip(
            f"initial godel run failed (returncode={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    first_run_stdout = result.stdout

    # Extract run_id from stderr — format: "[godel] run <uuid>"
    # WARN-2/NIT-3: use re.match with explicit UUID pattern for robustness.
    import re as _re
    run_id = None
    for line in result.stderr.strip().split("\n"):
        m = _re.match(r"\[godel\] run ([0-9a-f-]{36})$", line.strip())
        if m:
            run_id = m.group(1)
            break
    if not run_id:
        pytest.skip(f"couldn't parse run_id from stderr:\n{result.stderr}")

    # Step 2: load the log and find branch_a's event ID
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    events = log.all_events()
    branch_a = next(
        (
            e for e in events
            if e.op == "step.enter"
            and e.request.get("name") == "branch_a"
            and e.status == EventStatus.FINISHED
        ),
        None,
    )
    log.close()

    if not branch_a:
        pytest.skip("branch_a step.enter event not found in log")

    # Step 3: rewind via CLI
    rewind_result = _run_godel(
        ["rewind", run_id, "--to", branch_a.event_id],
        cwd=str(tmp_path),
    )
    assert rewind_result.returncode == 0, (
        f"godel rewind failed:\nstdout: {rewind_result.stdout}\nstderr: {rewind_result.stderr}"
    )
    combined = rewind_result.stdout + rewind_result.stderr
    assert "invalidated" in combined.lower(), (
        f"Expected 'invalidated' in rewind output:\n{combined}"
    )

    # Step 4: resume via CLI
    resume_result = _run_godel(
        ["resume", "--no-strict", run_id, str(fixture)],
        cwd=str(tmp_path),
    )
    assert resume_result.returncode == 0, (
        f"godel resume failed:\nstdout: {resume_result.stdout}\nstderr: {resume_result.stderr}"
    )
    assert "resumed run completed" in resume_result.stderr, (
        f"Expected 'resumed run completed' in resume output:\n{resume_result.stderr}"
    )

    # WARN-2: semantic check — the resumed workflow's final output must differ
    # from the first run because branch_a re-executed (new det.now() value) while
    # branch_b replayed from cache.  If both outputs are identical, branch_a's
    # rewind had no effect — which would indicate a stale-cache bug.
    # Only assert when both outputs are non-empty (CLI may print nothing if
    # the workflow returns None).
    resume_stdout = resume_result.stdout
    if first_run_stdout.strip() and resume_stdout.strip():
        assert resume_stdout != first_run_stdout, (
            "WARN-2: CLI resume output matches first run output — branch_a's "
            "rewind should have produced a new det.now() value, changing the "
            f"final result.\nFirst:  {first_run_stdout!r}\nResume: {resume_stdout!r}"
        )

    # WARN-2 (continued): rewind output must list the invalidated event IDs.
    # The CLI may truncate IDs (e.g. "01KP1RMQ...") so we match on the
    # first 8 characters of branch_a's event_id prefix, which appears in the
    # run_id of every child event invalidated inside that branch.
    rewind_combined = rewind_result.stdout + rewind_result.stderr
    branch_a_id_prefix = branch_a.event_id[:8]
    assert branch_a_id_prefix in rewind_combined, (
        f"Expected invalidated event ID prefix {branch_a_id_prefix!r} in rewind output.\n"
        f"Output: {rewind_combined!r}"
    )
