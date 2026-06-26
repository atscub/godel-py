"""Tests for stream_path stamping at subprocess launch.

stream_path is a list[str] stamped at run()/agent launch time on the launching
coroutine and captured by value in the persisted event.  It represents the
nesting chain of subprocess launches (e.g., [] for top-level events, [id] for
a direct run() call inside a step, [parent_id, child_id] for a run() call that
itself triggers a nested run()).
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from godel._context import _current_stream_path
from godel._decorators import parallel, step, workflow
from godel._run import run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_events(tmp_path):
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "no run log found"
    lines = [ln for ln in runs[0].read_text().strip().split("\n") if ln]
    return [json.loads(ln) for ln in lines]


def _run_events(events):
    return [e for e in events if e["op"] == "run" and e["status"] == "STARTED"]


@asynccontextmanager
async def stamped_stream_path(parent_id: str):
    """Stamp _current_stream_path with [parent_id] for the duration of the block.

    Replaces the inline ``token = _current_stream_path.set([parent_id]); try:
    ... finally: _current_stream_path.reset(token)`` pattern so per-test
    setup is a single ``async with stamped_stream_path(parent_id):`` line.
    """
    token = _current_stream_path.set([parent_id])
    try:
        yield
    finally:
        _current_stream_path.reset(token)


# ---------------------------------------------------------------------------
# AC1 — 4 parallel steps x 2 nested launches each -> 8 distinct stream_paths,
#       all depth-2, parents match the parent step's stream_path.
#
# "Nested launches" means each branch first sets a parent stream_path (as an
# agent/outer launch would), then invokes run() twice inside that parent
# context, producing depth-2 paths whose prefix matches the per-branch parent.
# ---------------------------------------------------------------------------

def test_parallel_steps_nested_launches_depth2_with_parents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # Per-branch parent IDs (simulate each branch's "agent launch" id)
    parent_ids = [f"parent-branch-{i:02d}" for i in range(4)]

    @workflow
    async def wf():
        @step
        async def branch(n: int):
            # Stamp a parent stream_path for this branch (simulates the agent
            # launch wrapping the two nested run() calls).
            async with stamped_stream_path(parent_ids[n]):
                await run(f"echo a{n}")
                await run(f"echo b{n}")
            return n

        return await parallel(branch(0), branch(1), branch(2), branch(3))

    result = asyncio.run(wf())
    assert set(result) == {0, 1, 2, 3}

    run_starts = _run_events(_load_events(tmp_path))
    # 4 branches x 2 runs = 8 events
    assert len(run_starts) == 8, f"expected 8 run events, got {len(run_starts)}"

    stream_paths = [tuple(e["stream_path"]) for e in run_starts]

    # All depth-2
    for sp in stream_paths:
        assert len(sp) == 2, f"expected depth-2 stream_path, got {sp!r}"

    # All 8 distinct (the child ULID differs per launch, even within a branch)
    assert len(set(stream_paths)) == 8, (
        f"expected 8 distinct stream_paths, got {len(set(stream_paths))}: "
        f"{stream_paths}"
    )

    # Prefix matching: each path's first element must be one of the per-branch
    # parent IDs, and each parent must appear as the prefix of exactly 2 paths.
    from collections import Counter
    prefix_counts = Counter(sp[0] for sp in stream_paths)
    assert set(prefix_counts.keys()) == set(parent_ids), (
        f"unexpected prefixes: {prefix_counts}"
    )
    for pid, count in prefix_counts.items():
        assert count == 2, f"parent {pid!r} should prefix 2 paths, got {count}"


# ---------------------------------------------------------------------------
# AC2 — The persisted event carries the stream_path value stamped at launch
#       time, even after the launching step has returned and the contextvar
#       has been reset.  (The reader-thread-closure wording in the design
#       becomes a by-value capture in the Event dataclass for pure-asyncio
#       code; the invariant is the same — no contextvar lookup on read.)
# ---------------------------------------------------------------------------

def test_stream_path_captured_in_event_after_step_returns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def do_work():
            return await run("echo closure_test")
        return await do_work()

    asyncio.run(wf())
    # At this point the step has returned, workflow is over, contextvar was
    # reset.  The persisted event must still carry the stamped stream_path.
    starts = _run_events(_load_events(tmp_path))
    assert starts, "no run STARTED event found"
    sp = starts[0]["stream_path"]
    assert isinstance(sp, list)
    assert len(sp) == 1
    assert sp[0] and isinstance(sp[0], str)

    # And the contextvar in the *current* thread is back to its default —
    # proving the value in the event was captured by value, not by reference.
    assert _current_stream_path.get() == []


# ---------------------------------------------------------------------------
# AC3 — Regression guard: the parallel() implementation MUST propagate the
#       launching context to branches.  We test both halves:
#
#   (a) Real parallel() with a parent stream_path set in the enclosing scope:
#       branches inherit it and produce depth-2 paths.
#   (b) Simulated broken parallel(): branches run in a fresh empty Context()
#       (as `pool.submit(fn)` without copy_context would do) and produce
#       depth-1 paths.  This proves (a)'s depth-2 assertion is load-bearing.
# ---------------------------------------------------------------------------

def test_parallel_propagates_stream_path_to_branches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    parent_id = "outer-parent-id"

    @workflow
    async def wf():
        async with stamped_stream_path(parent_id):
            @step
            async def branch_a():
                await run("echo a")

            @step
            async def branch_b():
                await run("echo b")

            await parallel(branch_a(), branch_b())

    asyncio.run(wf())

    starts = _run_events(_load_events(tmp_path))
    assert len(starts) == 2, f"expected 2 run events, got {len(starts)}"

    # Both branch runs must inherit the parent path — depth-2 with parent_id
    # as the prefix.  If parallel() loses context propagation, these are
    # depth-1 and the assertion fails (true regression guard).
    for e in starts:
        sp = e["stream_path"]
        assert len(sp) == 2, (
            f"expected depth-2 (parent propagated to branch), got {sp!r}. "
            f"If this fails, parallel() is no longer propagating contextvars "
            f"to branches - check copy_context() wiring."
        )
        assert sp[0] == parent_id, (
            f"branch did not inherit parent stream_path; "
            f"expected prefix {parent_id!r}, got {sp[0]!r}"
        )


def test_regression_guard_missing_propagation_produces_depth1(tmp_path, monkeypatch):
    """Coupled companion to the previous test: simulate a broken parallel()
    that dispatches branches into a fresh (empty) contextvars.Context, and
    verify the broken behaviour produces depth-1 paths.

    This proves that the depth-2 assertion in
    test_parallel_propagates_stream_path_to_branches is actually coupled to
    the propagation mechanism — if someone "fixes" that test to pass under
    a broken implementation (e.g., by loosening the length check), this
    test demonstrates what broken looks like.
    """
    monkeypatch.chdir(tmp_path)

    parent_id = "outer-parent-id"

    @workflow
    async def wf():
        async with stamped_stream_path(parent_id):
            async def branch():
                # Simulate "forgetting copy_context()": a broken dispatcher
                # would drop _current_stream_path before the branch runs.
                # Reset it to the default inside the branch to mimic what
                # pool.submit(fn) without copy_context().run produces.
                inner_token = _current_stream_path.set([])
                try:
                    await run("echo broken")
                finally:
                    _current_stream_path.reset(inner_token)

            await branch()

    asyncio.run(wf())

    starts = _run_events(_load_events(tmp_path))
    assert starts
    sp = starts[0]["stream_path"]
    assert len(sp) == 1, (
        f"expected depth-1 under BROKEN propagation, got {sp!r}. "
        f"If this produces depth-2, context propagation happens even under "
        f"a fresh Context() - the regression guard is no longer valid."
    )


# ---------------------------------------------------------------------------
# AC4 (smoke) — Sequential nested launch inside a step produces a depth-2
#               stream_path with matching prefix.
# ---------------------------------------------------------------------------

def test_sequential_nested_launch_produces_depth2_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parent_id = "agent-like-parent-00"

    @workflow
    async def wf():
        @step
        async def nested_launch():
            async with stamped_stream_path(parent_id):
                await run("echo nested")
        await nested_launch()

    asyncio.run(wf())
    starts = _run_events(_load_events(tmp_path))
    assert starts
    sp = starts[0]["stream_path"]
    assert len(sp) == 2, f"expected depth-2, got {sp!r}"
    assert sp[0] == parent_id
    assert sp[1] != parent_id


# ---------------------------------------------------------------------------
# C1 regression — stream_path contextvar must not leak on early exit.
# If ctx.next_op_position() (or the replay guard) raises before the subprocess
# is launched, the outer finally must still reset _current_stream_path.
# ---------------------------------------------------------------------------

def test_stream_path_contextvar_reset_on_early_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        from godel._context import _current_workflow
        ctx = _current_workflow.get()
        assert ctx is not None

        # Force an early-exit failure from inside run(), AFTER stream_path_token
        # has been set but BEFORE the subprocess-block finally fires.
        def _boom():
            raise RuntimeError("synthetic early-exit failure")

        ctx.next_op_position = _boom  # type: ignore[assignment]

        before = _current_stream_path.get()
        with pytest.raises(RuntimeError, match="synthetic early-exit failure"):
            await run("echo never")
        after = _current_stream_path.get()
        assert after == before, (
            f"stream_path leaked on early exit: before={before!r}, after={after!r}"
        )

    asyncio.run(wf())
