"""Tests for structured error metadata in the audit log (tickets awl-4m0, awl-6mo)."""
import asyncio
import json
import pytest
from godel import workflow, step
from godel._decorators import parallel
from godel._run import run, CommandFailure
from godel._event_log import EventLog
from godel._events import EventStatus


def test_failed_step_has_structured_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def failing():
            raise ValueError("test error")
        await failing()

    with pytest.raises(ValueError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))

    failed = [e for e in log.all_events()
              if e.status == EventStatus.FAILED and e.op == "step.enter"]
    assert len(failed) >= 1

    resp = failed[0].response
    assert resp["error_type"] == "ValueError"
    assert "failing" in resp["step_path"]
    assert resp["source_location"] != ""
    log.close()


def test_failed_run_has_structured_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def s():
            await run("false")
        await s()

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))

    failed = [e for e in log.all_events()
              if e.status == EventStatus.FAILED and e.op == "step.enter"]
    assert len(failed) >= 1
    resp = failed[0].response
    assert resp["error_type"] == "CommandFailure"
    log.close()


def test_emit_failed_backward_compat(tmp_path, monkeypatch):
    """Existing callers with just (event_id, error_str) still work."""
    monkeypatch.chdir(tmp_path)
    log = EventLog("test-compat", runs_dir=str(tmp_path / "runs"))
    e = log.emit_started(op="test", step_path=(), request={})
    # Old calling convention — should still work
    log.emit_failed(e.event_id, "something broke")
    assert log.get_event(e.event_id).status == EventStatus.FAILED
    assert "something broke" in log.get_event(e.event_id).response["error"]
    log.close()


def test_emit_failed_keyword_params(tmp_path, monkeypatch):
    """New keyword-only params build a structured response dict."""
    monkeypatch.chdir(tmp_path)
    log = EventLog("test-kwonly", runs_dir=str(tmp_path / "runs"))
    e = log.emit_started(op="test", step_path=(), request={})
    log.emit_failed(
        e.event_id,
        "something broke",
        error_type="ValueError",
        step_path=("main", "validate"),
        source_location="workflow.py:42",
        remediation_hint="check inputs",
    )
    ev = log.get_event(e.event_id)
    assert ev.status == EventStatus.FAILED
    resp = ev.response
    assert resp["error"] == "something broke"
    assert resp["error_type"] == "ValueError"
    assert resp["step_path"] == ["main", "validate"]
    assert resp["source_location"] == "workflow.py:42"
    assert resp["remediation_hint"] == "check inputs"
    log.close()


def test_error_metadata_in_jsonl(tmp_path, monkeypatch):
    """Verify the JSONL file contains structured error fields."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def s():
            raise RuntimeError("boom")
        await s()

    with pytest.raises(RuntimeError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    jsonl = (tmp_path / "runs" / f"{run_id}.jsonl").read_text()
    lines = [json.loads(ln) for ln in jsonl.strip().split("\n")]

    failed_lines = [ln for ln in lines if ln.get("status") == "FAILED" and ln.get("op") == "step.enter"]
    assert len(failed_lines) >= 1

    resp = failed_lines[-1]["response"]  # last snapshot wins
    assert resp["error_type"] == "RuntimeError"
    assert "step_path" in resp
    assert "source_location" in resp


def test_workflow_failed_event_has_structured_error(tmp_path, monkeypatch):
    """WORKFLOW_STARTED event also gets structured error info on failure."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        raise KeyError("missing key")

    with pytest.raises(KeyError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))

    failed = [e for e in log.all_events()
              if e.status == EventStatus.FAILED and e.op == "WORKFLOW_STARTED"]
    assert len(failed) >= 1

    resp = failed[0].response
    assert resp["error_type"] == "KeyError"
    assert resp["source_location"] != ""
    log.close()


def test_emit_failed_default_error_type(tmp_path, monkeypatch):
    """When error_type not passed, defaults to 'Exception'."""
    monkeypatch.chdir(tmp_path)
    log = EventLog("test-default-type", runs_dir=str(tmp_path / "runs"))
    e = log.emit_started(op="test", step_path=(), request={})
    log.emit_failed(e.event_id, "oops")
    resp = log.get_event(e.event_id).response
    assert resp["error_type"] == "Exception"
    assert resp["step_path"] == []
    assert resp["source_location"] == ""
    assert resp["remediation_hint"] == ""
    log.close()


def test_failed_step_response_has_five_keys(tmp_path, monkeypatch):
    """@step FAILED response must match the M5 contract: exactly 5 keys (WARN-1)."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def failing():
            raise RuntimeError("oops")
        await failing()

    with pytest.raises(RuntimeError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    failed = [e for e in log.all_events()
              if e.status == EventStatus.FAILED and e.op == "step.enter"]
    assert len(failed) >= 1
    resp = failed[0].response
    # M5 contract: exactly these 5 keys, no context_marker leaking in
    assert set(resp.keys()) == {"error", "error_type", "step_path", "source_location", "remediation_hint"}
    log.close()


def test_failed_step_non_ascii_error(tmp_path, monkeypatch):
    """FAILED event round-trips correctly when error message contains non-ASCII (NIT)."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def unicode_fail():
            raise ValueError("日本語エラー: \u00e9\u00e0\u00fc")
        await unicode_fail()

    with pytest.raises(ValueError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    # Read from JSONL to verify round-trip survives JSON encode/decode
    jsonl = (tmp_path / "runs" / f"{run_id}.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(ln) for ln in jsonl.strip().split("\n")]
    failed_lines = [ln for ln in lines if ln.get("status") == "FAILED" and ln.get("op") == "step.enter"]
    assert len(failed_lines) >= 1
    resp = failed_lines[-1]["response"]
    assert "日本語エラー" in resp["error"]
    assert "\u00e9" in resp["error"]


def test_failed_nested_steps_step_path(tmp_path, monkeypatch):
    """step_path in FAILED event reflects full nesting depth (NIT)."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def outer():
            @step
            async def inner():
                raise RuntimeError("deep failure")
            await inner()
        await outer()

    with pytest.raises(RuntimeError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    failed = [e for e in log.all_events()
              if e.status == EventStatus.FAILED and e.op == "step.enter"]
    # The innermost step.enter failure should carry the full nested path
    inner_fail = [e for e in failed if "inner" in (e.response or {}).get("step_path", [])]
    assert len(inner_fail) >= 1
    resp = inner_fail[0].response
    assert resp["step_path"] == ["outer", "inner"]
    assert resp["error_type"] == "RuntimeError"
    log.close()


def test_parallel_branch_failure_fork_join_have_error_type(tmp_path, monkeypatch):
    """FORK and JOIN FAILED events carry error_type from the failing branch (WARN-4)."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def good():
            return "ok"

        @step
        async def bad():
            raise ValueError("branch exploded")

        await parallel(good(), bad())

    with pytest.raises(ValueError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    fork_failed = [e for e in log.all_events()
                   if e.status == EventStatus.FAILED and e.op == "FORK"]
    join_failed = [e for e in log.all_events()
                   if e.status == EventStatus.FAILED and e.op == "JOIN"]
    assert len(fork_failed) >= 1, "FORK event should be FAILED"
    assert len(join_failed) >= 1, "JOIN event should be FAILED"
    assert fork_failed[0].response["error_type"] == "ValueError"
    assert join_failed[0].response["error_type"] == "ValueError"
    log.close()


def test_run_primitive_emit_failed_has_commandfailure_type(tmp_path, monkeypatch):
    """run() emit_failed carries error_type=CommandFailure not generic Exception (WARN-2)."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def s():
            await run("false")
        await s()

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    # run() emits a "run" op FAILED event
    run_failed = [e for e in log.all_events()
                  if e.status == EventStatus.FAILED and e.op == "run"]
    assert len(run_failed) >= 1
    assert run_failed[0].response["error_type"] == "CommandFailure"
    log.close()


def test_source_location_points_to_user_code(tmp_path, monkeypatch):
    """source_location in FAILED event should point to user code, not library internals (WARN-6)."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def s():
            raise RuntimeError("user error")  # this line is in test code, not godel internals
        await s()

    with pytest.raises(RuntimeError):
        asyncio.run(wf())

    run_id = wf._last_run_id
    log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    failed = [e for e in log.all_events()
              if e.status == EventStatus.FAILED and e.op == "step.enter"]
    assert len(failed) >= 1
    source_loc = failed[0].response["source_location"]
    assert source_loc != ""
    # Should point to this test file, NOT godel library internals like _decorators.py
    assert "_decorators.py" not in source_loc
    assert "_run.py" not in source_loc
    log.close()
