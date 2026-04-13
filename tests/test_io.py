"""Tests for async print/input shadows."""
import asyncio
import io
import sys
import pytest
from unittest.mock import patch
from godel.io import print as aprint, input as ainput
from godel._decorators import workflow


def test_aprint_writes_to_stdout():
    buf = io.StringIO()

    @workflow
    async def wf():
        with patch.object(sys, "stdout", buf):
            await aprint("hello", "world")

    asyncio.run(wf())
    assert buf.getvalue() == "hello world\n"


def test_aprint_custom_sep_end():
    buf = io.StringIO()

    @workflow
    async def wf():
        with patch.object(sys, "stdout", buf):
            await aprint("a", "b", sep="-", end="!")

    asyncio.run(wf())
    assert buf.getvalue() == "a-b!"


def test_ainput_reads_from_stdin():
    @workflow
    async def wf():
        with patch.object(sys, "stdin", io.StringIO("Alice\n")):
            with patch.object(sys, "stdout", io.StringIO()):
                result = await ainput("Name? ")
        return result

    assert asyncio.run(wf()) == "Alice"


def test_ainput_prompt_written():
    buf = io.StringIO()

    @workflow
    async def wf():
        with patch.object(sys, "stdout", buf):
            with patch.object(sys, "stdin", io.StringIO("x\n")):
                await ainput("enter: ")

    asyncio.run(wf())
    assert "enter: " in buf.getvalue()
