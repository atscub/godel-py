"""Async print/input/sleep shadows with audit event emission."""
import asyncio
import sys
import time as _time

from godel._context import _current_workflow


async def print(*values: object, sep: str = " ", end: str = "\n") -> None:
    """Async fire-and-forget write to caller stdout. Emits print event."""
    text = sep.join(str(v) for v in values) + end

    ctx = _current_workflow.get()

    inv_seq, local_seq = (0, 0)
    if ctx:
        inv_seq, local_seq = ctx.next_op_position()

    # Replay guard
    if ctx and ctx.replay_walker:
        from godel._events import Event, EventStatus
        req = {"text": text}
        req_hash = Event.compute_request_hash(req)
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="print",
            request_hash=req_hash,
        )
        if match.hit:
            # Still display the text — print is a display-only side effect.
            # Only skip audit-log emission.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, sys.stdout.write, text)
            await loop.run_in_executor(None, sys.stdout.flush)
            return

    event = None
    if ctx and ctx.event_log:
        event = ctx.event_log.emit_started(
            op="print",
            step_path=tuple(ctx.step_stack),
            request={"text": text},
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )
    if ctx and ctx.stream_agents and ctx.transcript is not None:
        from godel._context import _current_stream_path
        ctx.transcript.write_event(
            "print",
            step_path=tuple(ctx.step_stack),
            stream_path=list(_current_stream_path.get() or []),
            text=text.rstrip("\n"),
        )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, sys.stdout.write, text)
    await loop.run_in_executor(None, sys.stdout.flush)

    if event:
        ctx.event_log.emit_finished(event.event_id, response={})


async def input(prompt: str = "", *, schema=None):
    """Async blocking read from caller stdin. Emits input event."""
    ctx = _current_workflow.get()

    inv_seq, local_seq = (0, 0)
    if ctx:
        inv_seq, local_seq = ctx.next_op_position()

    # Replay guard
    if ctx and ctx.replay_walker:
        from godel._events import Event, EventStatus
        req = {"prompt": prompt}
        req_hash = Event.compute_request_hash(req)
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="input",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            return match.cached_response.get("value", "")

    event = None
    if ctx and ctx.event_log:
        event = ctx.event_log.emit_started(
            op="input",
            step_path=tuple(ctx.step_stack),
            request={"prompt": prompt},
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )

    loop = asyncio.get_running_loop()
    if prompt:
        await loop.run_in_executor(None, sys.stdout.write, prompt)
        await loop.run_in_executor(None, sys.stdout.flush)
    line = await loop.run_in_executor(None, sys.stdin.readline)
    result = line.rstrip("\n")

    if event:
        ctx.event_log.emit_finished(event.event_id, response={"value": result})
    return result


async def sleep(seconds: float) -> None:
    """Audited async sleep. Emits sleep event with requested and actual elapsed duration.

    On replay, returns immediately without sleeping (cached event hit).
    """
    ctx = _current_workflow.get()

    inv_seq, local_seq = (0, 0)
    if ctx:
        inv_seq, local_seq = ctx.next_op_position()

    # Replay guard — skip the real sleep, return immediately
    if ctx and ctx.replay_walker:
        from godel._events import Event, EventStatus
        req = {"seconds": seconds}
        req_hash = Event.compute_request_hash(req)
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="sleep",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            return

    event = None
    if ctx and ctx.event_log:
        event = ctx.event_log.emit_started(
            op="sleep",
            step_path=tuple(ctx.step_stack),
            request={"seconds": seconds},
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )

    t0 = _time.monotonic()
    await asyncio.sleep(seconds)
    elapsed = _time.monotonic() - t0

    if event:
        ctx.event_log.emit_finished(event.event_id, response={"elapsed": elapsed})
