"""Tests for @workflow, @step, WorkflowFail, parallel, retry."""
import asyncio
import time
import pytest
from godel._decorators import workflow, step, WorkflowFail, parallel, retry
from godel._context import _current_workflow


def test_workflow_runs():
    @workflow
    async def my_wf():
        return 42

    assert asyncio.run(my_wf()) == 42


def test_workflow_requires_async():
    with pytest.raises(TypeError, match="async function"):

        @workflow
        def sync_fn():
            pass


def test_workflow_sets_context():
    ctx_during = None

    @workflow
    async def my_wf():
        nonlocal ctx_during
        ctx_during = _current_workflow.get()
        return True

    asyncio.run(my_wf())
    assert ctx_during is not None
    assert ctx_during.run_id  # non-empty
    assert _current_workflow.get() is None  # cleaned up


def test_step_outside_workflow_raises():
    @step
    async def my_step():
        pass

    with pytest.raises(RuntimeError, match="outside a @workflow"):
        asyncio.run(my_step())


def test_step_inside_workflow():
    @step
    async def add(a, b):
        return a + b

    @workflow
    async def my_wf():
        return await add(3, 4)

    assert asyncio.run(my_wf()) == 7


def test_step_with_name():
    recorded = []

    @step(name="custom_name")
    async def my_step():
        ctx = _current_workflow.get()
        recorded.append(list(ctx.step_stack))

    @workflow
    async def my_wf():
        await my_step()

    asyncio.run(my_wf())
    assert recorded == [["custom_name"]]


def test_workflow_fail_propagates():
    @step
    async def failing():
        raise WorkflowFail("broken")

    @workflow
    async def my_wf():
        await failing()

    with pytest.raises(WorkflowFail, match="broken"):
        asyncio.run(my_wf())


def test_retry_retries_then_raises():
    call_count = 0

    @retry(3)
    @step
    async def flaky():
        nonlocal call_count
        call_count += 1
        raise WorkflowFail("fail")

    @workflow
    async def my_wf():
        await flaky()

    with pytest.raises(WorkflowFail):
        asyncio.run(my_wf())
    assert call_count == 3


def test_retry_succeeds_on_second_try():
    call_count = 0

    @retry(3)
    @step
    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise WorkflowFail("fail")
        return "ok"

    @workflow
    async def my_wf():
        return await flaky()

    assert asyncio.run(my_wf()) == "ok"
    assert call_count == 2


def test_parallel_concurrent():
    @workflow
    async def my_wf():
        async def slow(n):
            await asyncio.sleep(0.1)
            return n

        start = time.monotonic()
        results = await parallel(slow(1), slow(2))
        elapsed = time.monotonic() - start
        assert elapsed < 0.18  # concurrent, not sequential (0.2)
        return results

    result = asyncio.run(my_wf())
    assert result == (1, 2)
