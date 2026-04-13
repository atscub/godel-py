"""Deterministic replacements for non-deterministic stdlib functions.

These record their results in the event log on first call.
In M3 (replay), they will return the recorded value instead of executing.
"""
from __future__ import annotations

import secrets
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
