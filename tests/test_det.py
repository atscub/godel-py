"""Tests for godel.det deterministic replacements."""
import asyncio
import json
import pytest
from godel._decorators import workflow
from godel import det


def test_det_now_returns_iso(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.now()

    result = asyncio.run(wf())
    assert "T" in result  # ISO format
    assert "+" in result or "Z" in result  # timezone


def test_det_now_records_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.now()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    det_events = [e for e in events if e["op"] == "det.now"]
    assert len(det_events) == 2  # STARTED + FINISHED
    assert "value" in det_events[1]["response"]


def test_det_random_returns_float(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.random()

    result = asyncio.run(wf())
    assert isinstance(result, float)
    assert 0.0 <= result < 1.0


def test_det_random_records_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.random()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    det_events = [e for e in events if e["op"] == "det.random"]
    assert len(det_events) == 2


def test_det_uuid4_returns_valid_uuid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.uuid4()

    result = asyncio.run(wf())
    import uuid
    uuid.UUID(result)  # should not raise


def test_det_uuid4_records_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return det.uuid4()

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    det_events = [e for e in events if e["op"] == "det.uuid4"]
    assert len(det_events) == 2


def test_det_outside_workflow_raises():
    with pytest.raises(RuntimeError, match="inside a @workflow"):
        det.now()
    with pytest.raises(RuntimeError, match="inside a @workflow"):
        det.random()
    with pytest.raises(RuntimeError, match="inside a @workflow"):
        det.uuid4()
