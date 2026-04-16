"""Deterministic replacements for non-deterministic stdlib functions.

These record their results in the event log on first call.
In M3 (replay), they will return the recorded value instead of executing.
"""
from __future__ import annotations

import asyncio
import math
import secrets
import time as _time
import uuid as _uuid
from datetime import datetime, timezone

from godel._context import _current_workflow


def now() -> str:
    """Return current UTC time as ISO string. Records in event log."""
    ctx = _current_workflow.get()
    if ctx is None:
        raise RuntimeError("godel.det.now() must be called inside a @workflow")

    inv_seq, local_seq = ctx.next_op_position()

    # Replay guard
    if ctx.replay_walker:
        from godel._events import Event, EventStatus
        req_hash = Event.compute_request_hash({})
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="det.now",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            return match.cached_response.get("value")

    value = datetime.now(timezone.utc).isoformat()

    if ctx.event_log:
        event = ctx.event_log.emit_started(
            op="det.now",
            step_path=tuple(ctx.step_stack),
            request={},
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )
        ctx.event_log.emit_finished(event.event_id, response={"value": value})

    return value


def random() -> float:
    """Return a random float in [0, 1). Records in event log."""
    ctx = _current_workflow.get()
    if ctx is None:
        raise RuntimeError("godel.det.random() must be called inside a @workflow")

    inv_seq, local_seq = ctx.next_op_position()

    # Replay guard
    if ctx.replay_walker:
        from godel._events import Event, EventStatus
        req_hash = Event.compute_request_hash({})
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="det.random",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            return match.cached_response.get("value")

    value = secrets.SystemRandom().random()

    if ctx.event_log:
        event = ctx.event_log.emit_started(
            op="det.random",
            step_path=tuple(ctx.step_stack),
            request={},
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )
        ctx.event_log.emit_finished(event.event_id, response={"value": value})

    return value


def uuid4() -> str:
    """Return a new UUID4 as string. Records in event log."""
    ctx = _current_workflow.get()
    if ctx is None:
        raise RuntimeError("godel.det.uuid4() must be called inside a @workflow")

    inv_seq, local_seq = ctx.next_op_position()

    # Replay guard
    if ctx.replay_walker:
        from godel._events import Event, EventStatus
        req_hash = Event.compute_request_hash({})
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="det.uuid4",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            return match.cached_response.get("value")

    value = str(_uuid.uuid4())

    if ctx.event_log:
        event = ctx.event_log.emit_started(
            op="det.uuid4",
            step_path=tuple(ctx.step_stack),
            request={},
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )
        ctx.event_log.emit_finished(event.event_id, response={"value": value})

    return value


async def sleep(seconds: float) -> None:
    """Deterministic audited sleep.

    Records the sleep in the event log on first call so that replay skips
    the actual wait entirely (returns immediately without sleeping).

    This is the primitive used internally by ``@retry`` exponential backoff.
    It may also be used directly in workflow code when you need a recorded
    sleep that is guaranteed to be a no-op on replay.

    Args:
        seconds: Duration to sleep. Must be a finite non-negative number.

    Raises:
        RuntimeError: If called outside a ``@workflow``.
        ValueError: If *seconds* is negative, NaN, or not finite.
    """
    try:
        seconds_f = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"godel.det.sleep() requires a numeric duration, got {seconds!r}"
        ) from exc
    if math.isnan(seconds_f) or not math.isfinite(seconds_f) or seconds_f < 0:
        raise ValueError(
            f"godel.det.sleep() requires a finite non-negative duration, got {seconds!r}"
        )
    seconds = seconds_f

    ctx = _current_workflow.get()
    if ctx is None:
        raise RuntimeError("godel.det.sleep() must be called inside a @workflow")

    inv_seq, local_seq = ctx.next_op_position()

    # Replay guard — on a finished cache hit, skip the actual sleep entirely.
    resume_remaining: float | None = None
    if ctx.replay_walker:
        from godel._events import Event, EventStatus
        req = {"seconds": seconds}
        req_hash = Event.compute_request_hash(req)
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="det.sleep",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            return
        # STARTED-only: workflow was interrupted mid-sleep.  The STARTED event
        # was written but FINISHED was never recorded.  We must:
        #   1. Clear _replay_suppress so the new FINISHED is persisted to disk.
        #   2. Sleep only the *remaining* duration (time not yet elapsed).
        # This mirrors the same pattern used by godel.io.sleep for consistency.
        if match.hit and match.status == EventStatus.STARTED and match.event is not None:
            # Clear replay-suppress so the FINISHED we emit below reaches disk.
            if ctx.event_log is not None:
                ctx.event_log._replay_suppress = False
            ctx._local_replay_suppress = False
            # Compute remaining sleep duration from the original ts_start.
            ts_start = match.event.ts_start
            if ts_start:
                try:
                    started_at = datetime.fromisoformat(ts_start)
                    now_dt = datetime.now(started_at.tzinfo)
                    already = (now_dt - started_at).total_seconds()
                    resume_remaining = max(0.0, seconds - already)
                except (ValueError, TypeError):
                    resume_remaining = None

    if ctx.event_log:
        event = ctx.event_log.emit_started(
            op="det.sleep",
            step_path=tuple(ctx.step_stack),
            request={"seconds": seconds},
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )
        sleep_for = resume_remaining if resume_remaining is not None else seconds
        t0 = _time.monotonic()
        try:
            await asyncio.sleep(sleep_for)
        except BaseException as exc:
            ctx.event_log.emit_failed(
                event.event_id,
                error=f"sleep interrupted: {type(exc).__name__}",
                error_type=type(exc).__name__,
            )
            raise
        elapsed = _time.monotonic() - t0
        response: dict = {"seconds": seconds, "elapsed": elapsed}
        if resume_remaining is not None:
            response["resumed"] = True
            response["remaining_slept"] = elapsed
        ctx.event_log.emit_finished(event.event_id, response=response)
    else:
        sleep_for = resume_remaining if resume_remaining is not None else seconds
        await asyncio.sleep(sleep_for)
