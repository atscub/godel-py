"""Tests for workflow event emission."""
import asyncio

from godel._decorators import workflow
from godel._context import get_event_log
import pytest


@workflow
async def sample_workflow():
    return 42


@workflow
async def failing_workflow():
    raise ValueError("test error")


def test_workflow_emits_started(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(sample_workflow())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert len(runs) == 1


def test_workflow_started_is_first_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(sample_workflow())
    import json
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    first = json.loads(lines[0])
    assert first["op"] == "WORKFLOW_STARTED"
    assert first["status"] == "STARTED"


def test_workflow_finished_on_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(sample_workflow())
    import json
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    # Find the FINISHED snapshot of WORKFLOW_STARTED
    wf_events = [e for e in events if e["op"] == "WORKFLOW_STARTED"]
    assert any(e["status"] == "FINISHED" for e in wf_events)


def test_workflow_failed_on_exception(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(failing_workflow())
    import json
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    wf_events = [e for e in events if e["op"] == "WORKFLOW_STARTED"]
    assert any(e["status"] == "FAILED" for e in wf_events)


def test_get_event_log_inside_workflow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    captured_log = None

    @workflow
    async def wf():
        nonlocal captured_log
        captured_log = get_event_log()
        return "ok"

    asyncio.run(wf())
    assert captured_log is not None


def test_get_event_log_outside_workflow():
    with pytest.raises(RuntimeError, match="outside"):
        get_event_log()


def test_last_run_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(sample_workflow())
    assert hasattr(sample_workflow, "_last_run_id")
    assert sample_workflow._last_run_id is not None
