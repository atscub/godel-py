"""Async print/input/read_text/write_text shadows with audit event emission."""
import asyncio
import contextvars
import os
import sys
import tempfile
from pathlib import Path

from godel._context import _current_workflow, _privileged

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


# Maximum bytes of file content embedded in an audit-log snapshot. Content is
# truncated for storage/display but the FULL content is hashed for replay
# matching, so the truncation marker never affects determinism. Keeps the
# JSONL manageable when workflows touch large files.
_CONTENT_LOG_LIMIT = 64 * 1024


def _truncate_for_log(s: str, limit: int = _CONTENT_LOG_LIMIT) -> str:
    """Truncate *s* for audit-log embedding, preserving length info."""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [{len(s) - limit} chars truncated from audit log]"


def _normalize_path(path: str) -> str:
    """Resolve *path* to an absolute form so replay matches are cwd-independent.

    ``~`` is expanded. Missing intermediate components are tolerated (we do not
    require the target to exist yet for ``write_text``). This is the key stored
    in the audit log and used for request-hash computation.
    """
    p = Path(path).expanduser()
    # Path.resolve(strict=False) collapses .. and absolutises even if the
    # target doesn't exist yet — required for write_text to new files.
    return str(p.resolve(strict=False))


def _warn(message: str) -> None:
    """Emit a warning to stderr, coloured if click is available."""
    try:
        import click
        click.echo(click.style(f"[godel] WARNING: {message}", fg="yellow"), err=True)
    except Exception:
        sys.stderr.write(f"[godel] WARNING: {message}\n")


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


# ---------------------------------------------------------------------------
# File I/O primitives — audited, deterministic-replay-compatible.
# ---------------------------------------------------------------------------

def _read_text_sync(path: str, encoding: str) -> str:
    """Sync helper: read file with explicit encoding."""
    return Path(path).read_text(encoding=encoding)


def _write_text_atomic(path: str, content: str, encoding: str) -> None:
    """Sync helper: atomic write via tempfile + os.replace.

    Writes to a sibling temp file in the same directory (so rename is atomic
    on POSIX when the target filesystem is a single device), fsyncs, then
    ``os.replace``s into place. SIGKILL/OOM/disk-full mid-write leaves the
    original file untouched.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # delete=False so the file survives the contextmanager exit; we rename it
    # into place and clean up on error.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # Best-effort cleanup of the temp file if the rename never happened.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


async def read_text(path: str, *, encoding: str = "utf-8") -> str:
    """Read a file and record content in the audit log.

    The resolved absolute path (``~`` expanded, ``..`` collapsed) is stored
    in the log, so replay matches are independent of the caller's cwd. The
    full content is hashed for replay matching, but the embedded snapshot
    of the content is truncated at ``_CONTENT_LOG_LIMIT`` bytes for
    storage efficiency — display-only truncation, never affects determinism.

    On replay, returns cached content without touching the filesystem.
    Mismatch policy (--on-mismatch) applies when the resolved path changes.

    Args:
        path: Source path. Relative paths are resolved against the current
            working directory at call time.
        encoding: Text encoding (default ``utf-8``). Matches the default used
            by ``Path.read_text`` when ``PYTHONUTF8`` is enabled, but passing
            it explicitly removes platform-locale ambiguity.
    """
    ctx = _current_workflow.get()

    inv_seq, local_seq = (0, 0)
    if ctx:
        inv_seq, local_seq = ctx.next_op_position()

    resolved_path = _normalize_path(path)
    # Hash participates in replay-match; encoding included so swapping
    # utf-8 → latin-1 is a detectable change.
    req = {"path": resolved_path, "encoding": encoding}

    # Replay guard
    if ctx and ctx.replay_walker:
        from godel._events import Event, EventStatus
        req_hash = Event.compute_request_hash(req)
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="read_text",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            if match.hash_mismatch:
                from godel._replay import handle_hash_mismatch, MismatchPolicy
                policy = await handle_hash_mismatch(match, ctx.event_log)
                # If policy is CONTINUE, warn about the stale cache — we are
                # about to return OLD content for a path whose args changed.
                if policy == MismatchPolicy.CONTINUE:
                    _warn(
                        f"read_text({resolved_path!r}) replay hash mismatch: "
                        f"returning cached content from original run "
                        f"(--on-mismatch=continue). Re-reading the file would "
                        f"require --on-mismatch=invalidate."
                    )
            return match.cached_response.get("content", "")
        # STARTED-only for read_text: fall through and re-read. Reads are
        # idempotent so this is safe — unlike write_text, a partial STARTED
        # read cannot have corrupted external state. The new attempt either
        # succeeds (adding a FINISHED snapshot) or emits a FAILED event.
        # (Document asymmetry with write_text.)

    event = None
    if ctx and ctx.event_log:
        event = ctx.event_log.emit_started(
            op="read_text",
            step_path=tuple(ctx.step_stack),
            request=req,
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )

    # Privileged I/O — run in the executor so the event loop doesn't block
    # on multi-MB files. contextvars.copy_context().run() propagates the
    # _privileged flag into the thread so the strict-mode audit hook allows
    # the open call.
    token = _privileged.set(True)
    try:
        cv_ctx = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(
            None, lambda: cv_ctx.run(_read_text_sync, resolved_path, encoding)
        )
    except BaseException as exc:
        _privileged.reset(token)
        if event:
            ctx.event_log.emit_failed(
                event.event_id,
                str(exc),
                error_type=type(exc).__name__,
                step_path=tuple(ctx.step_stack) if ctx else (),
            )
        raise
    _privileged.reset(token)

    if event:
        ctx.event_log.emit_finished(
            event.event_id,
            response={
                "content": _truncate_for_log(content),
                "bytes_read": len(content.encode(encoding, errors="replace")),
            },
        )
    return content


async def write_text(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Write content to a file (atomic) and record the op in the audit log.

    Uses write-then-rename via tempfile in the target directory, so SIGKILL /
    OOM / disk-full mid-write never leaves the destination partially written.
    The FULL content participates in request-hash matching so that a change
    in either the path or the content is detected on replay, but the stored
    snapshot of the content is truncated at ``_CONTENT_LOG_LIMIT`` for
    storage efficiency (display-only truncation, never affects determinism).

    On replay, the write is skipped (filesystem is not touched). A STARTED-only
    match raises :class:`UnsafeResumeError` because a partial write may have
    corrupted the target — asymmetric with ``read_text`` which safely re-reads.

    Args:
        path: Destination path. Parent directories are created as needed.
        content: Text to write.
        encoding: Text encoding used to encode *content* (default ``utf-8``).
    """
    ctx = _current_workflow.get()

    inv_seq, local_seq = (0, 0)
    if ctx:
        inv_seq, local_seq = ctx.next_op_position()

    resolved_path = _normalize_path(path)
    # FULL content in req → drives the request_hash. The log snapshot is
    # truncated separately below so hash matching remains stable even for
    # multi-MB writes.
    req = {"path": resolved_path, "content": content, "encoding": encoding}

    # Replay guard
    if ctx and ctx.replay_walker:
        from godel._events import Event, EventStatus
        req_hash = Event.compute_request_hash(req)
        match = ctx.replay_walker.try_match(
            step_path=tuple(ctx.step_stack),
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            op="write_text",
            request_hash=req_hash,
        )
        if match.hit and match.status == EventStatus.FINISHED:
            if match.hash_mismatch:
                from godel._replay import handle_hash_mismatch, MismatchPolicy
                policy = await handle_hash_mismatch(match, ctx.event_log)
                # On CONTINUE: emit a warning so operators aren't blindsided
                # that the skipped replay write does not reflect the new
                # content. INVALIDATE already clears the cached event, so
                # execution will fall through to a fresh write below.
                if policy == MismatchPolicy.CONTINUE:
                    _warn(
                        f"write_text({resolved_path!r}) replay hash mismatch: "
                        f"skipping write entirely (--on-mismatch=continue). "
                        f"New content will NOT be written to disk. Use "
                        f"--on-mismatch=invalidate to re-execute the write."
                    )
                    return
                elif policy == MismatchPolicy.INVALIDATE:
                    # The cached event is now INVALIDATED; fall through to
                    # actually execute the write with the new content.
                    pass
                else:
                    # ABORT raised; should not reach here.
                    return
            else:
                # Clean cache hit — skip the write.
                return
        elif match.hit and match.status == EventStatus.STARTED:
            # STARTED-only: write may have partially happened. Cannot safely
            # re-execute without explicit opt-in. read_text tolerates this
            # (reads are idempotent); writes are not.
            from godel._exceptions import UnsafeResumeError
            raise UnsafeResumeError(
                f"write_text() has STARTED-only state (write may be partial)",
                cmd=f"write_text({resolved_path!r})",
                step_path=tuple(ctx.step_stack),
            )

    event = None
    if ctx and ctx.event_log:
        # Truncate content for the STARTED snapshot as well — a huge STARTED
        # entry lingering in the log (crash before FINISHED) should also be
        # bounded. The request_hash must be computed from the FULL req so
        # that replay matching is stable, regardless of the display truncation
        # applied to the stored request dict.
        from godel._events import Event as _Event
        full_hash = _Event.compute_request_hash(req)
        started_req = dict(req)
        started_req["content"] = _truncate_for_log(content)
        event = ctx.event_log.emit_started(
            op="write_text",
            step_path=tuple(ctx.step_stack),
            request=started_req,
            invocation_seq=inv_seq,
            step_local_seq=local_seq,
            parent_event_id=ctx.current_parent_event_id,
        )
        # Override the hash: emit_started used the truncated request. Swap
        # in the full-content hash so replay matching is deterministic.
        event.request_hash = full_hash
        ctx.event_log._append_event(event)

    token = _privileged.set(True)
    try:
        cv_ctx = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: cv_ctx.run(_write_text_atomic, resolved_path, content, encoding),
        )
    except BaseException as exc:
        _privileged.reset(token)
        if event:
            ctx.event_log.emit_failed(
                event.event_id,
                str(exc),
                error_type=type(exc).__name__,
                step_path=tuple(ctx.step_stack) if ctx else (),
            )
        raise
    _privileged.reset(token)

    if event:
        ctx.event_log.emit_finished(
            event.event_id,
            response={
                "path": resolved_path,
                "bytes_written": len(content.encode(encoding, errors="replace")),
            },
        )
