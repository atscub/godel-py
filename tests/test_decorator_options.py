"""Tests for decorator options: stream_agents, capture_stdout, redact.

Covers acceptance criteria from godel-py-5pl.8:
- Round-trip: values passed to @workflow / @step are retrievable from metadata.
- Invalid combos rejected before execution:
    * capture_stdout + parallel → ConfigError
    * non-callable in redact → TypeError
- Default-off: all options off by default (safe state preserved).
"""
import asyncio
import pytest

from godel._decorators import workflow, step, parallel
from godel._exceptions import ConfigError


# ---------------------------------------------------------------------------
# @workflow option round-trips
# ---------------------------------------------------------------------------


def test_workflow_defaults():
    """@workflow with no options stores safe defaults."""

    @workflow
    async def wf():
        return 1

    opts = wf._workflow_options
    assert opts["stream_agents"] is False
    assert opts["capture_stdout"] is False
    assert opts["redact"] == []


def test_workflow_stream_agents():
    @workflow(stream_agents=True)
    async def wf():
        return 1

    assert wf._workflow_options["stream_agents"] is True


def test_workflow_capture_stdout():
    @workflow(capture_stdout=True)
    async def wf():
        return 1

    assert wf._workflow_options["capture_stdout"] is True


def test_workflow_redact_stored():
    redactor = lambda s: s.replace("secret", "***")

    @workflow(redact=[redactor])
    async def wf():
        return 1

    assert wf._workflow_options["redact"] == [redactor]


def test_workflow_redact_multiple():
    r1 = lambda s: s
    r2 = lambda s: s

    @workflow(redact=[r1, r2])
    async def wf():
        return 1

    assert wf._workflow_options["redact"] == [r1, r2]


def test_workflow_redact_empty_list():
    @workflow(redact=[])
    async def wf():
        return 1

    assert wf._workflow_options["redact"] == []


def test_workflow_all_options():
    r = lambda s: s

    @workflow(stream_agents=True, capture_stdout=True, redact=[r])
    async def wf():
        return 1

    opts = wf._workflow_options
    assert opts["stream_agents"] is True
    assert opts["capture_stdout"] is True
    assert opts["redact"] == [r]


# ---------------------------------------------------------------------------
# @step option round-trips
# ---------------------------------------------------------------------------


def test_step_defaults():
    """@step with no options stores safe defaults."""

    @step
    async def s():
        return 1

    assert s._step_options["capture_stdout"] is False


def test_step_capture_stdout():
    @step(capture_stdout=True)
    async def s():
        return 1

    assert s._step_options["capture_stdout"] is True


def test_step_capture_stdout_false_explicit():
    @step(capture_stdout=False)
    async def s():
        return 1

    assert s._step_options["capture_stdout"] is False


# ---------------------------------------------------------------------------
# Validation: non-callable in redact → TypeError at decoration time
# ---------------------------------------------------------------------------


def test_workflow_redact_non_callable_raises_type_error():
    with pytest.raises(TypeError, match="redact\\[0\\].*callable"):

        @workflow(redact=["not_a_callable"])
        async def wf():
            return 1


def test_workflow_redact_non_callable_at_index_1():
    with pytest.raises(TypeError, match="redact\\[1\\].*callable"):

        @workflow(redact=[lambda s: s, 42])
        async def wf():
            return 1


def test_workflow_redact_none_values_rejected():
    with pytest.raises(TypeError, match="redact\\[0\\].*callable"):

        @workflow(redact=[None])
        async def wf():
            return 1


# ---------------------------------------------------------------------------
# Validation: capture_stdout + parallel → ConfigError at parallel() call time
# ---------------------------------------------------------------------------


def test_capture_stdout_in_parallel_raises_config_error():
    """A step with capture_stdout=True must be rejected when passed to parallel()."""

    @step(capture_stdout=True)
    async def my_step():
        return 1

    @workflow
    async def wf():
        await parallel(my_step(), my_step())

    with pytest.raises(ConfigError, match="capture_stdout"):
        asyncio.run(wf())


def test_capture_stdout_false_in_parallel_is_allowed():
    """A step with capture_stdout=False (default) must work fine inside parallel()."""

    @step(capture_stdout=False)
    async def my_step():
        return 1

    @workflow
    async def wf():
        return await parallel(my_step(), my_step())

    result = asyncio.run(wf())
    assert result == (1, 1)


def test_step_without_capture_stdout_in_parallel_is_allowed():
    """A bare @step (no options) must work fine inside parallel()."""

    @step
    async def my_step():
        return 2

    @workflow
    async def wf():
        return await parallel(my_step(), my_step())

    result = asyncio.run(wf())
    assert result == (2, 2)


# ---------------------------------------------------------------------------
# Backward-compatibility: bare @workflow still works (no parentheses)
# ---------------------------------------------------------------------------


def test_workflow_bare_decorator_runs():
    """@workflow (bare, no parens) still works and has correct defaults."""

    @workflow
    async def wf():
        return 99

    assert asyncio.run(wf()) == 99
    opts = wf._workflow_options
    assert opts["stream_agents"] is False
    assert opts["capture_stdout"] is False
    assert opts["redact"] == []


def test_workflow_bare_decorator_is_workflow():
    @workflow
    async def wf():
        return 1

    assert wf._is_workflow is True


# ---------------------------------------------------------------------------
# Backward-compatibility: @step still runs inside @workflow unchanged
# ---------------------------------------------------------------------------


def test_step_still_runs_in_workflow():
    @step
    async def add(a, b):
        return a + b

    @workflow
    async def wf():
        return await add(3, 4)

    assert asyncio.run(wf()) == 7


def test_step_with_options_still_runs_in_workflow():
    @step(capture_stdout=False)
    async def add(a, b):
        return a + b

    @workflow
    async def wf():
        return await add(10, 20)

    assert asyncio.run(wf()) == 30
