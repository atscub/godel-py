"""Tests for decorator options: capture_stdout, redact.

Covers acceptance criteria from godel-py-5pl.8:
- Round-trip: values passed to @workflow / @step are retrievable from metadata.
- Invalid combos rejected before execution:
    * capture_stdout + parallel → ConfigError
    * non-callable in redact → TypeError
- Default-off: all options off by default (safe state preserved).

Agent streaming is no longer a decorator option — it is always on and
can only be disabled by the caller via ``godel run --no-stream`` (which
sets ``GODEL_STREAM_AGENTS=0`` in the environment).
"""
import asyncio
import pytest

from godel._decorators import workflow, step, parallel
from godel._exceptions import ConfigError, GodelError


# ---------------------------------------------------------------------------
# @workflow option round-trips
# ---------------------------------------------------------------------------


def test_workflow_defaults():
    """@workflow with no options stores safe defaults."""

    @workflow
    async def wf():
        return 1

    opts = wf._workflow_options
    assert opts["capture_stdout"] is False
    assert opts["redact"] == []


def test_workflow_capture_stdout():
    @workflow(capture_stdout=True)
    async def wf():
        return 1

    assert wf._workflow_options["capture_stdout"] is True


def test_workflow_redact_stored():
    def redactor(s):
        return s.replace("secret", "***")

    @workflow(redact=[redactor])
    async def wf():
        return 1

    assert wf._workflow_options["redact"] == [redactor]


def test_workflow_redact_multiple():
    def r1(s):
        return s
    def r2(s):
        return s

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
    def r(s):
        return s

    @workflow(capture_stdout=True, redact=[r])
    async def wf():
        return 1

    opts = wf._workflow_options
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


# ---------------------------------------------------------------------------
# Regression tests for adversarial-review pass 1
# ---------------------------------------------------------------------------


def test_step_preserves_iscoroutinefunction():
    """Regression (C1 / W1): @step-decorated functions must still register as
    coroutine functions so that asyncio / @workflow stacking work correctly.
    """

    @step
    async def plain_step():
        return 1

    @step(capture_stdout=False)
    async def parameterised_step():
        return 2

    assert asyncio.iscoroutinefunction(plain_step) is True
    assert asyncio.iscoroutinefunction(parameterised_step) is True


def test_workflow_on_step_stacking():
    """Regression (C1 / W3): @workflow @step stacking must not raise TypeError.

    Before the C1 fix, @workflow's async-function check rejected the
    @step-decorated callable because ``step_caller`` was a plain sync function
    that lost the coroutine-function sentinel.
    """

    @workflow
    @step
    async def stacked():
        return 42

    # The stacked callable is both a step and a workflow; calling it should
    # execute the body and return the result without TypeError at decoration.
    assert asyncio.run(stacked()) == 42


def test_redact_zero_arg_callable_rejected():
    """Regression (C3 / W2): redactors with zero required args must be rejected."""
    with pytest.raises(TypeError, match=r"redact\[0\].*positional"):

        @workflow(redact=[lambda: "x"])
        async def wf():
            return 1


def test_redact_two_arg_callable_rejected():
    """Regression (C3 / W2): redactors with two required args must be rejected."""
    with pytest.raises(TypeError, match=r"redact\[0\].*positional"):

        @workflow(redact=[lambda a, b: a + b])
        async def wf():
            return 1


def test_redact_default_arg_counts_as_optional():
    """A redactor with (s, extra='x') has one required positional arg; accept it."""

    @workflow(redact=[lambda s, extra="y": s])
    async def wf():
        return 1

    assert len(wf._workflow_options["redact"]) == 1


def test_redact_variadic_callable_accepted():
    """A redactor defined as ``lambda *a: ...`` is acceptable (generic shim)."""

    @workflow(redact=[lambda *a: a[0] if a else ""])
    async def wf():
        return 1

    assert len(wf._workflow_options["redact"]) == 1


def test_redact_builtin_callable_accepted():
    """A C-implemented callable without an inspectable signature is not rejected.

    Some built-ins raise ValueError from inspect.signature; the validator must
    skip the arity check in that case rather than reject outright.
    """

    @workflow(redact=[str])
    async def wf():
        return 1

    assert wf._workflow_options["redact"] == [str]


def test_redact_wrong_arity_at_index_1():
    """A valid redactor followed by a wrong-arity one must still be rejected,
    and the error message must point at the offending index."""
    with pytest.raises(TypeError, match=r"redact\[1\].*positional"):

        @workflow(redact=[lambda s: s, lambda: "x"])
        async def wf():
            return 1


# ---------------------------------------------------------------------------
# ConfigError hierarchy
# ---------------------------------------------------------------------------

def test_config_error_is_godel_error():
    """ConfigError must be a GodelError subclass for catch-all compatibility."""
    err = ConfigError("bad combo")
    assert isinstance(err, GodelError)


def test_config_error_catchable_as_godel_error():
    """except GodelError: must catch ConfigError."""
    with pytest.raises(GodelError):
        raise ConfigError("incompatible options")
