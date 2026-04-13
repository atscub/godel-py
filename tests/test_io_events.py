"""Tests for print/input event instrumentation."""
import asyncio
import io
import json
import sys
from godel._decorators import workflow
from godel.io import print as godel_print, input as godel_input


def test_print_emits_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        await godel_print("hello world")
        return "done"

    asyncio.run(wf())
    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    print_events = [e for e in events if e["op"] == "print"]
    assert len(print_events) == 2  # STARTED + FINISHED
    assert print_events[0]["status"] == "STARTED"
    assert "hello world" in print_events[0]["request"]["text"]


def test_input_emits_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("user answer\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        return await godel_input("Question? ")

    result = asyncio.run(wf())
    assert result == "user answer"

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    input_events = [e for e in events if e["op"] == "input"]
    assert len(input_events) == 2  # STARTED + FINISHED
    assert input_events[0]["request"]["prompt"] == "Question? "
    assert input_events[1]["response"]["value"] == "user answer"


def test_print_works_outside_workflow(monkeypatch):
    """print should work without a workflow (no events, no crash)."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    asyncio.run(godel_print("no workflow"))
    # Just verify no exception


def test_input_works_outside_workflow(monkeypatch):
    """input should work without a workflow (no events, no crash)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("test\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    result = asyncio.run(godel_input("prompt: "))
    assert result == "test"
