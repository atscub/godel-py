"""Tests for parallel() max_concurrency parameter."""
import asyncio
import json

import pytest

from godel._decorators import parallel, workflow
from godel._exceptions import ConfigError


def test_max_concurrency_limits_concurrent_branches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    peak = 0
    active = 0

    @workflow
    async def wf():
        nonlocal peak, active

        async def branch(i):
            nonlocal peak, active
            active += 1
            if active > peak:
                peak = active
            await asyncio.sleep(0.02)
            active -= 1
            return i

        return await parallel(
            *[branch(i) for i in range(6)], max_concurrency=2
        )

    result = asyncio.run(wf())
    assert len(result) == 6
    assert peak <= 2


def test_max_concurrency_none_is_unlimited(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    peak = 0
    active = 0

    @workflow
    async def wf():
        nonlocal peak, active

        async def branch(i):
            nonlocal peak, active
            active += 1
            if active > peak:
                peak = active
            await asyncio.sleep(0.02)
            active -= 1
            return i

        return await parallel(
            *[branch(i) for i in range(5)], max_concurrency=None
        )

    result = asyncio.run(wf())
    assert len(result) == 5
    assert peak > 2


def test_max_concurrency_one_is_serial(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    peak = 0
    active = 0

    @workflow
    async def wf():
        nonlocal peak, active

        async def branch(i):
            nonlocal peak, active
            active += 1
            if active > peak:
                peak = active
            await asyncio.sleep(0.01)
            active -= 1
            return i

        return await parallel(
            *[branch(i) for i in range(4)], max_concurrency=1
        )

    result = asyncio.run(wf())
    assert len(result) == 4
    assert peak == 1


@pytest.mark.parametrize("bad_value,expected_msg", [
    (0, "must be >= 1"),
    (-1, "must be >= 1"),
    ("5", "must be a positive integer"),
    (True, "must be a positive integer"),
    (2.5, "must be a positive integer"),
])
def test_max_concurrency_invalid_raises(bad_value, expected_msg):
    async def run_test():
        async def branch():
            return 1

        await parallel(branch(), max_concurrency=bad_value)

    with pytest.raises(ConfigError, match=expected_msg):
        asyncio.run(run_test())


def test_max_concurrency_outside_workflow():
    peak = 0
    active = 0

    async def run_test():
        nonlocal peak, active

        async def branch(i):
            nonlocal peak, active
            active += 1
            if active > peak:
                peak = active
            await asyncio.sleep(0.02)
            active -= 1
            return i

        return await parallel(
            *[branch(i) for i in range(6)], max_concurrency=2
        )

    result = asyncio.run(run_test())
    assert len(result) == 6
    assert peak <= 2


def test_max_concurrency_recorded_in_fork_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def a():
            return 1

        async def b():
            return 2

        return await parallel(a(), b(), max_concurrency=3)

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    fork_starts = [
        e for e in events if e["op"] == "FORK" and e["status"] == "STARTED"
    ]
    assert fork_starts[0]["request"]["max_concurrency"] == 3


def test_max_concurrency_preserves_result_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def branch(i):
            await asyncio.sleep(0.01 * (5 - i))
            return i

        return await parallel(
            *[branch(i) for i in range(5)], max_concurrency=2
        )

    result = asyncio.run(wf())
    assert result == (0, 1, 2, 3, 4)


def test_max_concurrency_with_exception(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        async def good():
            await asyncio.sleep(0.01)
            return "ok"

        async def bad():
            raise ValueError("boom")

        return await parallel(good(), bad(), max_concurrency=1)

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(wf())
