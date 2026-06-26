"""Tests for @step event instrumentation."""
import asyncio
import json
from godel._decorators import workflow, step


def test_step_emits_started_finished(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def my_step():
            return "hello"
        return await my_step()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    step_events = [e for e in events if e["op"] == "step.enter"]
    assert len(step_events) == 2  # STARTED + FINISHED snapshots
    assert step_events[0]["status"] == "STARTED"
    assert step_events[1]["status"] == "FINISHED"


def test_step_emits_failed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def bad_step():
            raise ValueError("boom")
        return await bad_step()

    import pytest
    with pytest.raises(ValueError):
        asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    step_events = [e for e in events if e["op"] == "step.enter"]
    assert any(e["status"] == "FAILED" for e in step_events)


def test_nested_step_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def outer():
            @step
            async def inner():
                return "deep"
            return await inner()
        return await outer()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    step_events = [e for e in events if e["op"] == "step.enter" and e["status"] == "STARTED"]
    paths = [tuple(e["step_path"]) for e in step_events]
    assert ("outer",) in paths
    assert ("outer", "inner") in paths


def test_invocation_seq_increments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def repeated():
            return "ok"
        await repeated()
        await repeated()
        return "done"

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    step_starts = [e for e in events if e["op"] == "step.enter" and e["status"] == "STARTED"]
    inv_seqs = [e["invocation_seq"] for e in step_starts]
    assert inv_seqs == [0, 1]


def test_existing_decorator_tests_still_pass(tmp_path, monkeypatch):
    """Ensure backward compat -- step still works as before."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def s():
            return 42
        return await s()

    result = asyncio.run(wf())
    assert result == 42
