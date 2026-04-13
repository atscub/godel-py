"""Tests for run() primitive."""
import asyncio
import time
import pytest
from godel._run import run, CommandResult, CommandFailure
from godel._context import _privileged
from godel._decorators import workflow, parallel


def test_run_echo():
    @workflow
    async def wf():
        result = await run("echo hello")
        assert isinstance(result, CommandResult)
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.returncode == 0
        return result

    asyncio.run(wf())


def test_run_nonzero():
    @workflow
    async def wf():
        with pytest.raises(CommandFailure) as exc_info:
            await run("exit 1")
        assert exc_info.value.returncode == 1

    asyncio.run(wf())


def test_run_timeout():
    @workflow
    async def wf():
        with pytest.raises(CommandFailure, match="timed out"):
            await run("sleep 10", timeout=0.1)

    asyncio.run(wf())


def test_run_privileged_flag():
    flags = []

    @workflow
    async def wf():
        flags.append(_privileged.get())  # before
        result = await run("echo check")
        flags.append(_privileged.get())  # after
        return result

    asyncio.run(wf())
    assert flags == [False, False]  # _privileged resets after run


def test_run_concurrent():
    @workflow
    async def wf():
        start = time.monotonic()
        await parallel(
            run("sleep 0.1"),
            run("sleep 0.1"),
        )
        elapsed = time.monotonic() - start
        assert elapsed < 0.18  # concurrent

    asyncio.run(wf())


def test_run_captures_stderr():
    @workflow
    async def wf():
        result = await run("echo err >&2; echo out")
        assert "out" in result.stdout
        assert "err" in result.stderr

    asyncio.run(wf())
