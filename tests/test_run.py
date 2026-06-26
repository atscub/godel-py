"""Tests for run() primitive."""
import asyncio
import time
from unittest.mock import patch
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


# ---------------------------------------------------------------------------
# list cmd → create_subprocess_exec (no shell interpretation)
# ---------------------------------------------------------------------------

def test_run_list_cmd_uses_exec():
    """run() calls create_subprocess_exec when cmd is a list."""
    exec_calls = []
    shell_calls = []

    async def fake_exec(*args, **kwargs):
        exec_calls.append(args)

        class FakeProc:
            returncode = 0
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()
            async def wait(self): pass
        proc = FakeProc()
        proc.stdout.feed_data(b"ok\n")
        proc.stdout.feed_eof()
        proc.stderr.feed_data(b"")
        proc.stderr.feed_eof()
        return proc

    async def fake_shell(cmd, **kwargs):
        shell_calls.append(cmd)
        return await fake_exec(cmd, **kwargs)

    async def go():
        with patch("godel._run.asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch("godel._run.asyncio.create_subprocess_shell", side_effect=fake_shell):
                await run(["echo", "hello world"])

    asyncio.run(go())
    assert len(exec_calls) == 1
    assert len(shell_calls) == 0


def test_run_string_cmd_uses_shell():
    """run() still calls create_subprocess_shell for string commands."""
    shell_calls = []

    async def fake_shell(cmd, **kwargs):
        shell_calls.append(cmd)

        class FakeProc:
            returncode = 0
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()
            async def wait(self): pass
        proc = FakeProc()
        proc.stdout.feed_data(b"ok\n")
        proc.stdout.feed_eof()
        proc.stderr.feed_data(b"")
        proc.stderr.feed_eof()
        return proc

    async def go():
        with patch("godel._run.asyncio.create_subprocess_shell", side_effect=fake_shell):
            await run("echo hello")

    asyncio.run(go())
    assert len(shell_calls) == 1


def test_run_list_cmd_passes_args_intact():
    """Arguments in a list cmd reach the subprocess without shell interpretation."""
    @workflow
    async def wf():
        result = await run(["echo", "hello world"])
        assert result.stdout.strip() == "hello world"
        return result

    asyncio.run(wf())
