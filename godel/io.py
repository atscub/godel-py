"""Async print/input shadows with audit event emission."""
import asyncio
import os
import sys

from godel._context import _current_workflow

# Sentinel so we warn only once per process about non-TTY stdin with no
# GODEL_AUTO_CHECKPOINT declaration.
_tty_warned: bool = False


def _auto_checkpoint_mode() -> str | None:
    """Return the GODEL_AUTO_CHECKPOINT value, or None if unset/empty."""
    v = os.environ.get("GODEL_AUTO_CHECKPOINT", "").strip()
    return v if v else None


def _maybe_warn_non_tty() -> None:
    """Emit a one-shot stderr warning when stdin is not a TTY and the caller
    has not declared ``GODEL_AUTO_CHECKPOINT``.

    Called lazily on the **live-read path only** (after the replay guard has
    been checked), so replayed ``input()`` hits — which never touch stdin —
    do not trigger a spurious warning.  This surfaces the situation where a
    workflow that calls ``godel.input()`` is about to block on EOF because
    it was run non-interactively without explicitly opting in.
    """
    global _tty_warned
    if _tty_warned:
        return
    if _auto_checkpoint_mode() is not None:
        return  # Caller declared intent — no warning needed.
    try:
        if not sys.stdin.isatty():
            # Flip the sentinel first so the write path is unreachable twice
            # even if an exception interrupts the write below.  asyncio runs
            # this synchronously on the event-loop thread, so there is no
            # concurrency at this call site — the ordering is purely for
            # exception-safety and future-proofing.
            _tty_warned = True
            sys.stderr.write(
                "[godel] warning: godel.input() called but stdin is not a TTY. "
                "To script checkpoint answers, pipe answers or set "
                "GODEL_AUTO_CHECKPOINT=<mode> (e.g. pipe, file, fifo) to "
                "suppress this warning.\n"
            )
            sys.stderr.flush()
    except Exception:
        pass  # Swallow — TTY detection is best-effort.


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
    """Async blocking read from caller stdin. Emits input event.

    ``godel.input()`` reads ``sys.stdin.readline()`` and is therefore
    scriptable from any standard UNIX mechanism:

    - **Pipe**: ``yes '' | godel run wf.py`` or ``echo "yes" | godel run wf.py``
    - **File redirect**: ``godel run wf.py < answers.txt``
    - **FIFO**: ``mkfifo /tmp/ctl && godel run wf.py < /tmp/ctl``

    When running non-interactively set ``GODEL_AUTO_CHECKPOINT=1`` (or any
    non-empty value) to declare intent and suppress the "stdin is not a TTY"
    warning that Godel emits as a safety reminder.  The value is recorded in
    the event's ``request.auto_checkpoint`` field so the audit log reflects
    the scripted mode.
    """
    ctx = _current_workflow.get()

    inv_seq, local_seq = (0, 0)
    if ctx:
        inv_seq, local_seq = ctx.next_op_position()

    # Build request dict; annotate with auto_checkpoint mode when declared.
    auto_cp = _auto_checkpoint_mode()
    req: dict = {"prompt": prompt}
    if auto_cp is not None:
        req["auto_checkpoint"] = auto_cp

    # Replay guard — cache hits never read from stdin, so the non-TTY warning
    # must NOT fire here.
    if ctx and ctx.replay_walker:
        from godel._events import Event, EventStatus
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

    # Only the live-read path can block on EOF — warn lazily, here.
    _maybe_warn_non_tty()

    event = None
    if ctx and ctx.event_log:
        event = ctx.event_log.emit_started(
            op="input",
            step_path=tuple(ctx.step_stack),
            request=req,
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
