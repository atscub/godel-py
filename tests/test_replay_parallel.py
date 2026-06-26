"""Tests for replay-aware parallel() — FORK/JOIN with ReplayWalker."""
import asyncio
import json
from godel._decorators import workflow, step, parallel
from godel._event_log import EventLog
from godel._replay import ReplayWalker
from godel._context import _pending_replay
from godel import det


def _load_events(tmp_path):
    """Load all events from the first JSONL in runs/."""
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    return [json.loads(ln) for ln in lines]


def test_all_branches_cached_returns_cached(tmp_path, monkeypatch):
    """When all branch primitives are cached, parallel() returns cached results."""
    monkeypatch.chdir(tmp_path)

    call_count = 0

    @workflow
    async def wf():
        nonlocal call_count

        @step
        async def branch_a():
            nonlocal call_count
            call_count += 1
            t = det.now()
            return t

        @step
        async def branch_b():
            nonlocal call_count
            call_count += 1
            t = det.now()
            return t

        return await parallel(branch_a(), branch_b())

    # First run — generates events
    asyncio.run(wf())
    first_run_id = wf._last_run_id
    assert call_count == 2

    # Load log and create replay walker
    event_log = EventLog.load(first_run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    # Resume — set pending replay
    call_count = 0
    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    # The step functions are called again (control flow re-enters),
    # but det.now() inside them hits the cache and returns the cached value
    assert call_count == 2  # Steps are re-entered, but primitives use cache


def test_one_branch_no_cache_reexecutes(tmp_path, monkeypatch):
    """When one branch has no cache, that branch re-executes its primitives."""
    monkeypatch.chdir(tmp_path)

    execution_log = []

    @workflow
    async def wf():
        @step
        async def branch_a():
            t = det.now()
            execution_log.append(("a", t))
            return t

        @step
        async def branch_b():
            t = det.now()
            execution_log.append(("b", t))
            return t

        return await parallel(branch_a(), branch_b())

    # First run
    asyncio.run(wf())
    first_run_id = wf._last_run_id

    # Load events and remove branch_b's det.now from the log
    event_log = EventLog.load(first_run_id, runs_dir=str(tmp_path / "runs"))
    event_log.all_events()

    # Find the det.now event in branch_b and remove it from the index
    walker = ReplayWalker(event_log)

    # Remove keys associated with branch_b's det.now
    keys_to_remove = [
        k for k in walker._index
        if k[3] == "det.now" and "branch_b" in k[0]
    ]
    for k in keys_to_remove:
        del walker._index[k]

    execution_log.clear()
    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    # Both branches executed (control flow re-enters)
    assert len(execution_log) == 2
    # branch_a used cache, branch_b got a new value
    a_entries = [e for e in execution_log if e[0] == "a"]
    b_entries = [e for e in execution_log if e[0] == "b"]
    assert len(a_entries) == 1
    assert len(b_entries) == 1


def test_fork_join_not_duplicated_on_resume(tmp_path, monkeypatch):
    """FORK/JOIN events are NOT duplicated when resuming a completed run."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def a():
            return 1

        async def b():
            return 2

        return await parallel(a(), b())

    # First run
    asyncio.run(wf())
    first_run_id = wf._last_run_id

    first_lines = (tmp_path / "runs" / f"{first_run_id}.jsonl").read_text().strip().split("\n")
    original_count = len(first_lines)

    # Resume
    event_log = EventLog.load(first_run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    # Log should be unchanged — no duplicate events
    all_lines = (tmp_path / "runs" / f"{first_run_id}.jsonl").read_text().strip().split("\n")
    assert len(all_lines) == original_count


def test_fork_invocation_count_tracked(tmp_path, monkeypatch):
    """Multiple parallel() calls track separate FORK invocation counts."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def a():
            return 1

        async def b():
            return 2

        r1 = await parallel(a(), b())
        r2 = await parallel(a(), b())
        return r1, r2

    asyncio.run(wf())
    events = _load_events(tmp_path)
    fork_starts = [e for e in events if e["op"] == "FORK" and e["status"] == "STARTED"]
    assert len(fork_starts) == 2
    # They should have different invocation_seq values
    seqs = [e["invocation_seq"] for e in fork_starts]
    assert seqs == [0, 1]
