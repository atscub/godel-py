"""Tests for WorkflowContext.last_step_event_id helper."""
import asyncio
import pytest
from godel._decorators import workflow, step, parallel
from godel._context import _current_workflow, _pending_replay
from godel._event_log import EventLog
from godel._replay import ReplayWalker


def test_last_step_event_id_after_three_steps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    collected = {}

    @workflow
    async def wf():
        @step
        async def step_a():
            return "a"

        @step
        async def step_b():
            return "b"

        @step
        async def step_c():
            return "c"

        await step_a()
        await step_b()
        await step_c()

        ctx = _current_workflow.get()
        collected["n1"] = ctx.last_step_event_id(1)
        collected["n2"] = ctx.last_step_event_id(2)
        collected["n3"] = ctx.last_step_event_id(3)
        collected["history"] = list(ctx._step_event_history)

    asyncio.run(wf())

    n1 = collected["n1"]
    n2 = collected["n2"]
    n3 = collected["n3"]
    history = collected["history"]

    # All three should be distinct valid IDs
    assert len({n1, n2, n3}) == 3

    # n1 is the most recent (step_c), n3 is the oldest (step_a)
    assert n1 == history[-1]
    assert n2 == history[-2]
    assert n3 == history[-3]

    # All IDs are non-empty strings
    for eid in (n1, n2, n3):
        assert isinstance(eid, str) and eid


def test_last_step_event_id_out_of_range(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    collected = {}

    @workflow
    async def wf():
        @step
        async def only_step():
            return 42

        await only_step()

        ctx = _current_workflow.get()
        try:
            ctx.last_step_event_id(99)
            collected["raised"] = False
        except IndexError:
            collected["raised"] = True

    asyncio.run(wf())
    assert collected["raised"] is True


def test_last_step_event_id_n_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    collected = {}

    @workflow
    async def wf():
        @step
        async def some_step():
            return 0

        await some_step()

        ctx = _current_workflow.get()
        try:
            ctx.last_step_event_id(0)
            collected["raised"] = False
        except ValueError:
            collected["raised"] = True

    asyncio.run(wf())
    assert collected["raised"] is True


def test_last_step_event_id_n_negative(tmp_path, monkeypatch):
    """WARN-4: n=-1 must raise ValueError (guard covers all n < 1, not just n=0)."""
    monkeypatch.chdir(tmp_path)

    collected = {}

    @workflow
    async def wf():
        @step
        async def some_step():
            return 0

        await some_step()

        ctx = _current_workflow.get()
        try:
            ctx.last_step_event_id(-1)
            collected["raised"] = False
        except ValueError:
            collected["raised"] = True

    asyncio.run(wf())
    assert collected["raised"] is True


def test_last_step_event_id_empty_history(tmp_path, monkeypatch):
    """WARN-4: n=1 with no completed steps must raise IndexError (most natural OOB case)."""
    monkeypatch.chdir(tmp_path)

    collected = {}

    @workflow
    async def wf():
        ctx = _current_workflow.get()
        try:
            ctx.last_step_event_id(1)
            collected["raised"] = False
        except IndexError:
            collected["raised"] = True

    asyncio.run(wf())
    assert collected["raised"] is True


def test_failed_step_does_not_append_to_history(tmp_path, monkeypatch):
    """WARN-3: a step that raises must NOT append its event_id to _step_event_history."""
    monkeypatch.chdir(tmp_path)

    collected = {}

    @workflow
    async def wf():
        @step
        async def good_step():
            return "ok"

        @step
        async def bad_step():
            raise RuntimeError("intentional failure")

        await good_step()
        try:
            await bad_step()
        except RuntimeError:
            pass

        ctx = _current_workflow.get()
        collected["history_len"] = len(ctx._step_event_history)

    asyncio.run(wf())

    # Only good_step should be in history — bad_step must NOT have been appended.
    assert collected["history_len"] == 1


def test_parallel_mixed_cached_race_last_step_event_id(tmp_path, monkeypatch):
    """Regression test: parallel() with one cached branch and one live branch.

    When Branch A is fully cached and Branch B reaches a non-cached boundary
    (clearing _replay_suppress=False) during the parallel() await, Branch A
    must still return the original *persisted* event_id from last_step_event_id()
    — NOT the ephemeral replay event_id.

    This is the WARN-1 race-condition fix for parallel-mixed-cached scenario:
    suppress_at_entry is snapshotted BEFORE the await so a sibling branch
    clearing _replay_suppress cannot corrupt Branch A's history entry.
    """
    monkeypatch.chdir(tmp_path)

    collected: dict = {}

    @workflow
    async def wf():
        @step
        async def branch_a():
            return "a_result"

        @step
        async def branch_b():
            return "b_result"

        await parallel(branch_a(), branch_b())

        ctx = _current_workflow.get()
        # Record history so we can inspect it after replay
        collected["history"] = list(ctx._step_event_history)

    # First run: establish the cached log
    asyncio.run(wf())
    run_id = wf._last_run_id
    history_first = list(collected["history"])

    # Both branches should have appended their event IDs
    assert len(history_first) == 2, (
        f"Expected 2 history entries after first run, got {history_first}"
    )
    assert all(history_first), f"All history entries should be non-empty: {history_first}"

    # Load the persisted log and build a walker; look up canonical IDs by step
    # name so we are not dependent on _step_event_history insertion order (which
    # is non-deterministic for parallel branches).
    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    a_key = next(
        k for k in walker._index
        if k[3] == "step.enter" and "branch_a" in k[0]
    )
    b_key = next(
        k for k in walker._index
        if k[3] == "step.enter" and "branch_b" in k[0]
    )
    cached_a_id = walker._index[a_key].event_id
    cached_b_id = walker._index[b_key].event_id

    # Sanity: both IDs must appear in the first-run history (order-independent)
    assert set(history_first) == {cached_a_id, cached_b_id}, (
        f"First-run history {history_first} does not match persisted IDs "
        f"{{cached_a_id={cached_a_id!r}, cached_b_id={cached_b_id!r}}}"
    )

    # Tamper so branch_b has NO cached boundary (simulate non-cached branch):
    # remove branch_b's step.enter entry from the index so it falls through to
    # live execution.
    keys_to_remove = [
        k for k in walker._index
        if k[3] == "step.enter" and "branch_b" in k[0]
    ]
    for k in keys_to_remove:
        del walker._index[k]

    collected.clear()

    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    history_replay = collected["history"]

    # There must still be 2 entries (branch_a cached, branch_b live)
    assert len(history_replay) == 2, (
        f"Expected 2 history entries on replay, got {history_replay}"
    )

    history_replay_set = set(history_replay)

    # CRITICAL: branch_a was fully cached — its original persisted event_id must
    # still appear in history, regardless of insertion order.
    assert cached_a_id in history_replay_set, (
        f"branch_a (cached) history entry missing after parallel replay: "
        f"expected {cached_a_id!r} in {history_replay}. "
        "This indicates the parallel-mixed-cached race condition is not fixed: "
        "branch_b's live execution cleared _replay_suppress and branch_a "
        "appended its ephemeral event_id instead of the cached persisted one."
    )

    # branch_b was live (non-cached boundary) — its new event_id must differ
    # from the original persisted one (order-independent check).
    assert cached_b_id not in history_replay_set, (
        f"branch_b had no cache entry so it should have produced a new event_id, "
        f"but {cached_b_id!r} still appears in {history_replay}"
    )

    event_log.close()
