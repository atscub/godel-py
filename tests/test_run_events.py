"""Tests for run() event instrumentation."""
import asyncio
import json
from godel._decorators import workflow
from godel._run import run, CommandFailure
import pytest


def test_run_emits_started_finished(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return await run("echo hello")

    result = asyncio.run(wf())
    assert result.stdout.strip() == "hello"

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    run_events = [e for e in events if e["op"] == "run"]
    assert len(run_events) == 2  # STARTED + FINISHED
    assert run_events[0]["status"] == "STARTED"
    assert run_events[1]["status"] == "FINISHED"
    assert "hello" in run_events[1]["response"]["stdout"]


def test_run_emits_failed_on_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return await run("exit 1")

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    run_events = [e for e in events if e["op"] == "run"]
    assert any(e["status"] == "FAILED" for e in run_events)


def test_run_request_contains_cmd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return await run("echo test", cwd="/tmp", timeout=10.0, idempotent=True)

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    run_start = [e for e in events if e["op"] == "run" and e["status"] == "STARTED"][0]
    assert run_start["request"]["cmd"] == "echo test"
    assert run_start["request"]["cwd"] == "/tmp"
    assert run_start["request"]["timeout"] == 10.0
    assert run_start["request"]["idempotent"] is True


def test_run_truncates_large_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        # Generate output larger than 1000 chars
        return await run("python3 -c \"print('x' * 2000)\"")

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    run_fin = [e for e in events if e["op"] == "run" and e["status"] == "FINISHED"][0]
    assert len(run_fin["response"]["stdout"]) == 1000
