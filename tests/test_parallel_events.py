"""Tests for FORK/JOIN event instrumentation in parallel()."""
import asyncio
import json
from godel._decorators import workflow, step, parallel
from godel._exceptions import PauseSignal
from godel._pause import write_pause_request
from godel._context import _current_workflow
import pytest


def test_parallel_emits_fork_join(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def a():
            return 1
        async def b():
            return 2
        return await parallel(a(), b())

    result = asyncio.run(wf())
    assert result == (1, 2)

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    ops = [e["op"] for e in events]
    assert "FORK" in ops
    assert "JOIN" in ops


def test_fork_has_branches_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def a(): return 1
        async def b(): return 2
        async def c(): return 3
        return await parallel(a(), b(), c())

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    fork_starts = [e for e in events if e["op"] == "FORK" and e["status"] == "STARTED"]
    assert fork_starts[0]["request"]["branches"] == 3


def test_join_references_fork(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def a(): return 1
        async def b(): return 2
        return await parallel(a(), b())

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    fork_starts = [e for e in events if e["op"] == "FORK" and e["status"] == "STARTED"]
    join_starts = [e for e in events if e["op"] == "JOIN" and e["status"] == "STARTED"]
    assert join_starts[0]["request"]["fork_id"] == fork_starts[0]["event_id"]


def test_parallel_with_steps_emits_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def branch_a():
            return "a"
        @step
        async def branch_b():
            return "b"
        return await parallel(branch_a(), branch_b())

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    ops = [e["op"] for e in events if e["status"] == "STARTED"]
    assert "FORK" in ops
    assert "JOIN" in ops
    assert "step.enter" in ops


def test_parallel_without_event_log():
    """parallel() works without a workflow context."""
    async def run():
        async def a(): return 1
        async def b(): return 2
        return await parallel(a(), b())
    result = asyncio.run(run())
    assert result == (1, 2)


def test_parallel_mixed_pause_and_exception_preserves_failure(tmp_path, monkeypatch):
    """CRITICAL-1: parallel() with one branch raising PauseSignal and another
    raising a real exception must:
    1. Re-raise PauseSignal (pause wins for control flow), AND
    2. Record the real exception in the audit log as a FAILED event.

    Plain coroutines are used so each branch raises exactly one signal type
    without the @step entry check_pause_request interfering.
    """
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def raises_pause():
            raise PauseSignal(reason="test pause")

        async def raises_value_error():
            raise ValueError("real error that must not be lost")

        # parallel() receives both; pause_signals=[PauseSignal],
        # exceptions=[ValueError].  FAILED events must be emitted before re-raise.
        await parallel(raises_pause(), raises_value_error())

    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    # Audit log must contain a FAILED event for the ValueError branch
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "expected at least one run log"
    lines = [l for l in runs[0].read_text().strip().split("\n") if l]
    events = [json.loads(l) for l in lines]
    failed_events = [e for e in events if e.get("status") == "FAILED"]
    assert failed_events, (
        "audit log must contain at least one FAILED event when a real exception "
        "occurs alongside a PauseSignal in parallel()"
    )
    # At least one FAILED event should describe the mixed-pause failure
    all_errors = " ".join(
        e.get("response", {}).get("error", "") for e in failed_events
    )
    assert "failed" in all_errors.lower(), (
        f"expected 'failed' in error messages, got: {all_errors!r}"
    )


def test_pause_signal_in_nested_step_does_not_mark_outer_step_failed(tmp_path, monkeypatch):
    """WARN-2: When PauseSignal propagates through an outer @step, the outer
    step's audit event must NOT be marked FAILED — the pause path should
    simply not emit a FAILED event for it.
    """
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def outer():
            @step
            async def inner():
                ctx = _current_workflow.get()
                write_pause_request(ctx.run_id)
                # inner() returns normally; PauseSignal fires at the NEXT step
                # entry boundary — we need another step call inside outer to
                # trigger the pause.

            @step
            async def trigger():
                pass  # check_pause_request at entry raises PauseSignal

            await inner()
            await trigger()

        await outer()

    with pytest.raises(PauseSignal):
        asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs
    lines = [l for l in runs[0].read_text().strip().split("\n") if l]
    events = [json.loads(l) for l in lines]

    # Find step.enter events for "outer"
    outer_events = [
        e for e in events
        if e.get("op") == "step.enter"
        and e.get("request", {}).get("name") == "outer"
    ]
    assert outer_events, "expected at least one step.enter event for 'outer'"
    for e in outer_events:
        assert e.get("status") != "FAILED", (
            f"outer step must not be marked FAILED when PauseSignal propagates "
            f"through it; got status={e.get('status')!r}"
        )


# ---------------------------------------------------------------------------
# parallel() signature: variadic args, returns tuple
# ---------------------------------------------------------------------------

def test_parallel_returns_tuple():
    """parallel() return type is tuple, not list."""
    async def run():
        async def a(): return 1
        async def b(): return 2
        return await parallel(a(), b())
    result = asyncio.run(run())
    assert isinstance(result, tuple)


def test_parallel_variadic_args():
    """parallel() accepts individual positional args."""
    async def run():
        async def task(n): return n * 10
        return await parallel(task(1), task(2), task(3))
    result = asyncio.run(run())
    assert result == (10, 20, 30)


def test_parallel_splat_from_comprehension():
    """parallel(*[coro for ...]) works — the documented usage pattern."""
    async def run():
        async def task(n): return n ** 2
        return await parallel(*[task(i) for i in range(5)])
    result = asyncio.run(run())
    assert result == (0, 1, 4, 9, 16)
    assert isinstance(result, tuple)
