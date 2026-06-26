"""Tests for agent.call event instrumentation."""
import asyncio
import json
from unittest.mock import patch, AsyncMock
from godel._decorators import workflow
from godel.agents._claude import claude_code
from godel._run import CommandResult, CommandFailure
import pytest


def _mock_run_result(stdout='{"result": "test response"}', returncode=0):
    return CommandResult(stdout=stdout, stderr="", returncode=returncode)


def test_agent_call_emits_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._common.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _mock_run_result()
            return await agent("Write hello world")

    asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    assert len(agent_events) >= 1
    started = [e for e in agent_events if e["status"] == "STARTED"]
    assert len(started) == 1
    assert started[0]["request"]["model"] == "sonnet"
    assert "hello world" in started[0]["request"]["prompt"].lower()


def test_agent_call_emits_finished(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._common.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _mock_run_result()
            return await agent("test prompt")

    asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    finished = [e for e in agent_events if e["status"] == "FINISHED"]
    assert len(finished) == 1
    assert "type" in finished[0]["response"]


def test_agent_call_emits_failed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._common.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = CommandFailure("claude failed", returncode=1)
            return await agent("test prompt")

    with pytest.raises(CommandFailure):
        asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]
    failed = [e for e in agent_events if e["status"] == "FAILED"]
    assert len(failed) == 1


def test_agent_call_cancelled_emits_failed(tmp_path, monkeypatch):
    """CancelledError must emit FAILED and must not leave event stuck in STARTED."""
    monkeypatch.chdir(tmp_path)

    # Set up the workflow context so the event log is initialised, then
    # cancel the agent task while it is blocked inside _invoke.
    async def _run():
        from godel._decorators import workflow
        from godel.agents._claude import claude_code

        ready = asyncio.Event()
        asyncio.Event()

        @workflow
        async def wf():
            agent = claude_code(model="sonnet")

            async def _blocking_run(*args, **kwargs):
                ready.set()
                # Block until cancelled — simulates a slow CLI call.
                await asyncio.sleep(999)

            with patch("godel.agents._common.run", side_effect=_blocking_run):
                return await agent("test prompt")

        task = asyncio.create_task(wf())
        # Wait until the agent is actually blocked inside _invoke, then cancel.
        await ready.wait()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "expected at least one run log file"
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]

    # There must be a FAILED event for the cancelled call.
    failed = [e for e in agent_events if e["status"] == "FAILED"]
    assert len(failed) >= 1, "expected FAILED event for cancelled agent.call"

    # The FAILED event must record CancelledError as the error type.
    # error_type is nested inside the response dict emitted by emit_failed.
    assert any(
        "CancelledError" in (e.get("response") or {}).get("error_type", "")
        for e in failed
    ), "FAILED event should name CancelledError"

    # No agent.call event may be left in STARTED (each STARTED must have a matching close).
    started_ids = {e["event_id"] for e in agent_events if e["status"] == "STARTED"}
    closed_ids = {
        e["event_id"]
        for e in agent_events
        if e["status"] in ("FINISHED", "FAILED")
    }
    stuck = started_ids - closed_ids
    assert not stuck, f"agent.call events stuck in STARTED: {stuck}"


def test_agent_call_emit_failed_logging_error_does_not_mask_original(
    tmp_path, monkeypatch
):
    """If emit_failed itself raises, the original exception must still propagate.

    Pins the C2 contract: logging failures are swallowed (best-effort audit)
    but never mask the real error the caller needs to see.
    """
    monkeypatch.chdir(tmp_path)

    from godel._event_log import EventLog


    def broken_emit_failed(self, *args, **kwargs):
        raise RuntimeError("disk full — simulated log write failure")

    class OriginalError(Exception):
        pass

    @workflow
    async def wf():
        agent = claude_code(model="sonnet")
        with patch("godel.agents._common.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = OriginalError("real cause")
            # Patch emit_failed only around the call so STARTED still gets
            # persisted normally via emit_started.
            with patch.object(EventLog, "emit_failed", broken_emit_failed):
                return await agent("test prompt")

    # The ORIGINAL exception must reach the caller — not the logging error.
    with pytest.raises(OriginalError, match="real cause"):
        asyncio.run(wf())


def test_agent_call_generatorexit_does_not_emit_failed(tmp_path, monkeypatch):
    """GeneratorExit must propagate WITHOUT emitting a FAILED event.

    Rationale: GeneratorExit (and KeyboardInterrupt / SystemExit) are
    BaseException subclasses that indicate coroutine destruction or process
    teardown — they are intentionally outside the
    ``(Exception, CancelledError)`` catch scope.  Attempting to write to the
    audit log during coroutine ``.close()`` is both unsafe and semantically
    wrong (the call was not a failure, the coroutine was simply closed).

    This test drives the agent's ``__call__`` coroutine manually (no event
    loop) so we can inject GeneratorExit at a well-defined await point via
    ``coro.close()`` without triggering contextvar-reset races that would
    otherwise happen if we cancelled a Task on the running loop.
    """
    monkeypatch.chdir(tmp_path)

    from godel._context import _current_workflow, WorkflowContext
    from godel._event_log import EventLog
    import uuid

    run_id = str(uuid.uuid4())
    event_log = EventLog(run_id, runs_dir=str(tmp_path / "runs"))
    ctx = WorkflowContext(run_id=run_id, event_log=event_log)
    token = _current_workflow.set(ctx)
    try:
        agent = claude_code(model="sonnet")

        # _execute will await on an asyncio.Future that never resolves, giving
        # us a clean suspension point inside the __call__ try/except where we
        # can inject GeneratorExit via coro.close().
        async def _hanging_execute(prompt, *, schema=None):
            asyncio.get_event_loop().create_future() if False else None
            # Use a bare await that yields forever without needing a loop.
            while True:
                # `await` on an object with __await__ that yields once lets
                # us suspend the coroutine for manual driving.
                await _suspend()

        async def _suspend():
            # A bare await that yields None once per send(None).
            class _Awaitable:
                def __await__(self):
                    yield None
            await _Awaitable()

        # Patch _execute on the instance so it suspends forever.
        agent._execute = _hanging_execute  # type: ignore[assignment]

        coro = agent.__call__("test prompt")
        # Drive the coroutine to its first suspension inside _execute.
        try:
            coro.send(None)
        except StopIteration:
            raise AssertionError("coroutine finished unexpectedly")

        # Inject GeneratorExit at the current suspension point.
        # coro.close() raises GeneratorExit inside the coroutine; if our
        # except clause were `except BaseException` it would swallow it and
        # write a FAILED event before re-raising.  With the correct scope
        # (Exception, CancelledError), GeneratorExit propagates untouched.
        coro.close()
    finally:
        _current_workflow.reset(token)
        event_log.close()

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "expected at least one run log file"
    lines = runs[0].read_text().strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    agent_events = [e for e in events if e["op"] == "agent.call"]

    # STARTED must have been emitted (the call did begin).
    assert any(e["status"] == "STARTED" for e in agent_events), agent_events

    # Crucially: NO FAILED event for the GeneratorExit.  This pins the
    # intentional scope of the except clause so a future widening to
    # `except BaseException` will fail this test.
    failed = [e for e in agent_events if e["status"] == "FAILED"]
    assert not failed, (
        "GeneratorExit must not emit FAILED — the except clause is scoped "
        "to (Exception, CancelledError) on purpose. Got: "
        f"{[e.get('response') for e in failed]}"
    )
