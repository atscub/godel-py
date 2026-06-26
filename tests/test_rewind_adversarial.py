"""Adversarial rewind tests: parallel, nested-workflow, and LOOP scenarios.

These tests guard against regressions introduced when RewindSignal propagates
through asyncio.gather (parallel branches), nested @workflow calls, and LOOP
bodies — all contexts where the signal used to be mishandled.
"""
from __future__ import annotations

import asyncio

from godel import workflow, step, parallel
from godel._context import _current_workflow
from godel._rewind import rewind


# ---------------------------------------------------------------------------
# Rewind inside parallel()
# ---------------------------------------------------------------------------

def test_rewind_inside_parallel_does_not_corrupt_audit_log(tmp_path, monkeypatch):
    """RewindSignal raised inside a parallel branch must NOT emit_failed on FORK/JOIN.

    Before the fix, asyncio.gather returned the RewindSignal as a result element;
    the isinstance(r, Exception) check treated it as a branch failure, causing
    FORK and JOIN events to be permanently FAILED in the audit log.

    After the fix: FORK/JOIN are emitted as FINISHED (or left for apply_rewind to
    invalidate), and the signal propagates to @workflow which applies the graph cut.
    """
    monkeypatch.chdir(tmp_path)
    rewound = {"done": False}
    call_log = []

    @workflow
    async def wf():
        nonlocal rewound

        @step
        async def branch_a():
            call_log.append("a")
            return "a"

        @step
        async def branch_b():
            call_log.append("b")
            ctx = _current_workflow.get()
            if not rewound["done"]:
                rewound["done"] = True
                # Rewind to branch_a's event — forces re-execution of both branches
                target = ctx.last_step_event_id(1)
                await rewind(to=target, reason="rewind from parallel branch")
            return "b"

        await parallel(branch_a(), branch_b())
        return "done"

    result = asyncio.run(wf())
    assert result == "done"
    assert rewound["done"] is True
    # branch_b should have run at least twice (once triggering rewind, once succeeding)
    assert call_log.count("b") >= 2

    # Verify no FAILED FORK/JOIN events remain in the log
    import json

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log file found"

    events_by_id: dict = {}
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            events_by_id[d["event_id"]] = d

    for ev in events_by_id.values():
        if ev.get("op") in ("FORK", "JOIN"):
            assert ev.get("status") != "FAILED", (
                f"FORK/JOIN event {ev['event_id']} must not be FAILED after rewind-inside-parallel; "
                f"got status={ev.get('status')}"
            )


def test_rewind_inside_parallel_all_branches_complete_after_rewind(tmp_path, monkeypatch):
    """After rewind-inside-parallel the workflow completes cleanly."""
    monkeypatch.chdir(tmp_path)
    call_counts = {"a": 0, "b": 0}
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def branch_a():
            call_counts["a"] += 1
            return call_counts["a"]

        @step
        async def branch_b():
            call_counts["b"] += 1
            ctx = _current_workflow.get()
            if not rewound["done"]:
                rewound["done"] = True
                target = ctx.last_step_event_id(1)
                await rewind(to=target, reason="retry parallel")
            return call_counts["b"]

        results = await parallel(branch_a(), branch_b())
        return results

    results = asyncio.run(wf())
    assert isinstance(results, tuple)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# Rewind inside nested @workflow
# ---------------------------------------------------------------------------

def test_rewind_inside_nested_workflow(tmp_path, monkeypatch):
    """RewindSignal raised in a nested @workflow is caught by that workflow's own loop.

    The outer workflow should see a normal return value, not an unhandled RewindSignal.
    """
    monkeypatch.chdir(tmp_path)
    inner_calls = {"n": 0}
    rewound = {"done": False}

    @workflow
    async def inner_wf():
        @step
        async def inner_step():
            inner_calls["n"] += 1
            return inner_calls["n"]

        result = await inner_step()

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            target = ctx.last_step_event_id(1)
            await rewind(to=target, reason="inner rewind")

        return result

    @workflow
    async def outer_wf():
        inner_result = await inner_wf()
        return f"outer-got-{inner_result}"

    result = asyncio.run(outer_wf())
    assert result.startswith("outer-got-")
    assert inner_calls["n"] == 2  # inner step ran twice due to rewind
    assert rewound["done"] is True


# ---------------------------------------------------------------------------
# Rewind inside a LOOP (Python while loop used as LOOP equivalent)
# ---------------------------------------------------------------------------

def test_rewind_inside_loop(tmp_path, monkeypatch):
    """Rewind raised inside a loop body propagates to @workflow correctly.

    The workflow uses a Python loop (equivalent to Godel's LOOP construct).
    After the rewind the workflow re-executes from the beginning and should
    eventually succeed.
    """
    monkeypatch.chdir(tmp_path)
    iteration = {"n": 0}
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def loop_step():
            iteration["n"] += 1
            return iteration["n"]

        results = []
        for _i in range(3):
            val = await loop_step()
            results.append(val)

            ctx = _current_workflow.get()
            if not rewound["done"] and _i == 1:
                rewound["done"] = True
                # Rewind to the very first loop_step event
                target = ctx.last_step_event_id(2)
                await rewind(to=target, reason="rewind mid-loop")

        return results

    result = asyncio.run(wf())
    assert isinstance(result, list)
    assert len(result) == 3
    assert rewound["done"] is True
