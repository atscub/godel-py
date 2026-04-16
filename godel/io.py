"""Async print/input/sleep/read_text/write_text shadows with audit event emission."""
import asyncio
import contextvars
import math
import os
import sys
import tempfile
import time as _time
from datetime import datetime
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


# Maximum UTF-8 byte size of file content embedded in an audit-log snapshot.
# Content is truncated for storage/display but the FULL content is hashed for
# replay matching, so the truncation marker never affects determinism. Keeps
# the JSONL manageable when workflows touch large files.
#
# Measured in BYTES (UTF-8-encoded), not characters, so that workflows
# processing CJK / emoji text do not produce disproportionately large audit
# entries — a char budget on CJK text can inflate the JSONL ~3x.
_CONTENT_LOG_LIMIT_BYTES = 64 * 1024
# Backwards-compatible alias; the byte-based name is canonical.
_CONTENT_LOG_LIMIT = _CONTENT_LOG_LIMIT_BYTES


def _truncate_for_log(s: str, limit_bytes: int = _CONTENT_LOG_LIMIT_BYTES) -> str:
    """Truncate *s* for audit-log embedding by UTF-8 byte size.

    The limit applies to the UTF-8 encoding; the truncation marker notes how
    many source characters were dropped (easier for humans to reason about
    than byte counts).
    """
    encoded = s.encode("utf-8", errors="replace")
    if len(encoded) <= limit_bytes:
        return s
    # Decode the prefix, dropping the trailing partial codepoint that the
    # byte-cut may have split.  errors="ignore" drops at most 3 trailing bytes.
    kept = encoded[:limit_bytes].decode("utf-8", errors="ignore")
    dropped_chars = len(s) - len(kept)
    return kept + f"\n... [{dropped_chars} chars truncated from audit log]"


def _normalize_path(path: str) -> str:
    """Resolve *path* to an absolute form so replay matches are cwd-independent.

    ``~`` is expanded. Missing intermediate components are tolerated (we do not
    require the target to exist yet for ``write_text``). This is the key stored
    in the audit log and used for request-hash computation.

    ``Path.resolve(strict=False)`` raises ``RuntimeError`` on symlink cycles
    (Python 3.10) rather than ``OSError`` — the former slips past
    ``except OSError`` handlers and, crucially, fires BEFORE the audit event
    is emitted, so the failure would go unaudited.  We normalise to
    ``OSError`` so every pre-emit filesystem error surfaces identically.
    """
    p = Path(path).expanduser()
    try:
        # Path.resolve(strict=False) collapses .. and absolutises even if the
        # target doesn't exist yet — required for write_text to new files.
        return str(p.resolve(strict=False))
    except RuntimeError as exc:
        # Symlink cycle (or other recursion inside pathlib). Re-raise as
        # OSError so callers / audit hooks see a filesystem error.
        raise OSError(f"failed to resolve path {path!r}: {exc}") from exc


def _safe_emit_failed(event_log, event_id: str, exc: BaseException, step_path: tuple) -> None:
    """Call ``event_log.emit_failed`` without masking the caller's exception.

    Used from ``except BaseException`` in read_text / write_text.  If the
    log-write itself fails (disk full, log file closed, DB contention), we
    must NOT replace the original exception with the log failure — the
    caller needs to see the real error.  Swallowing is correct here because
    the caller is about to re-raise the original ``exc``.
    """
    try:
        event_log.emit_failed(
            event_id,
            str(exc),
            error_type=type(exc).__name__,
            step_path=step_path,
        )
    except Exception:
        # Best-effort audit — never let a log-write failure replace the
        # real exception.  The original error is re-raised by the caller.
        pass


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


async def sleep(seconds: float) -> None:
    """Audited async sleep. Emits sleep event with requested and actual elapsed duration.

    On replay (FINISHED cache hit), returns immediately without sleeping.

    On resume from a mid-sleep crash (STARTED-only event), sleeps only the
    *remaining* duration computed from the original ``ts_start``, not the full
    requested duration. A new FINISHED event is emitted.

    Raises:
        ValueError: if *seconds* is negative, NaN, or not finite.
    """
    try:
        seconds_f = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"godel.sleep() requires a numeric duration, got {seconds!r}"
        ) from exc
    if math.isnan(seconds_f) or not math.isfinite(seconds_f) or seconds_f < 0:
        raise ValueError(
            f"godel.sleep() requires a finite non-negative duration, got {seconds!r}"
        )
    seconds = seconds_f

    ctx = _current_workflow.get()

    inv_seq, local_seq = (0, 0)
    if ctx:
        inv_seq, local_seq = ctx.next_op_position()

    resume_remaining: float | None = None
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
        if match.hit and match.status == EventStatus.STARTED and match.event is not None:
            ts_start = match.event.ts_start
            if ts_start:
                try:
                    started_at = datetime.fromisoformat(ts_start)
                    now = datetime.now(started_at.tzinfo)
                    already = (now - started_at).total_seconds()
                    resume_remaining = max(0.0, seconds - already)
                except (ValueError, TypeError):
                    resume_remaining = None

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

    sleep_for = resume_remaining if resume_remaining is not None else seconds

    t0 = _time.monotonic()
    try:
        await asyncio.sleep(sleep_for)
    except BaseException as exc:
        if event and ctx and ctx.event_log:
            ctx.event_log.emit_failed(
                event.event_id,
                error=f"sleep interrupted: {type(exc).__name__}",
                error_type=type(exc).__name__,
            )
        raise
    elapsed = _time.monotonic() - t0

    if event:
        response = {"elapsed": elapsed}
        if resume_remaining is not None:
            response["resumed"] = True
            response["remaining_slept"] = elapsed
        ctx.event_log.emit_finished(event.event_id, response=response)


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

    Permission preservation: ``mkstemp`` creates the temp file with mode
    ``0o600`` for security.  If the target already exists we capture its
    mode via ``os.stat`` and ``os.chmod`` the temp file to match BEFORE
    ``os.replace``, so an existing ``0o644`` / ``0o755`` file does not
    silently lose group/world read+execute bits on overwrite.  A missing
    or unstat-able target leaves the default ``0o600`` in place (matches
    the security posture of a fresh write).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Snapshot the existing file's mode (if any) so we can preserve it across
    # the replace.  Use os.stat (follows symlinks) because os.replace also
    # follows the final target semantics on POSIX.
    existing_mode: int | None = None
    try:
        existing_mode = os.stat(str(target)).st_mode & 0o7777
    except (FileNotFoundError, NotADirectoryError):
        existing_mode = None  # fresh write — keep the secure default 0o600
    except OSError:
        existing_mode = None  # permission denied to stat → fall back

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
        if existing_mode is not None:
            # Restore the previous file's permission bits on the new inode.
            try:
                os.chmod(tmp_name, existing_mode)
            except OSError:
                # Non-fatal: filesystems like FAT, or restricted sandboxes,
                # may refuse chmod.  Proceed with the rename — preserving
                # content integrity matters more than permission fidelity
                # on such exotic backends.
                pass
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
            # _safe_emit_failed swallows any log-write failure so that the
            # original exception (exc) is what ultimately propagates — never
            # masked by an audit-log side effect.
            _safe_emit_failed(
                ctx.event_log,
                event.event_id,
                exc,
                tuple(ctx.step_stack) if ctx else (),
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
        # Single-write STARTED emit with the CORRECT full-content hash.
        # We compute the hash from the full ``req`` (including untruncated
        # content) BEFORE emitting, so the first — and only — JSONL
        # snapshot for this STARTED state already carries the hash that
        # replay will compare against.  The stored ``request`` field is
        # the truncated form so the log stays bounded even if we crash
        # before FINISHED.
        #
        # We use ``emit_started`` with the truncated request (cheap hash
        # input) and then override ``event.request_hash`` in place.  The
        # ``_append_event`` inside ``emit_started`` has already written
        # one snapshot with the truncated-input hash; that would leave a
        # misleading hash on the wire until FINISHED overwrites it
        # (W4).  To avoid the stale snapshot, we suppress the internal
        # append via ``_replay_suppress`` for the duration of the
        # ``emit_started`` call, then manually append once with the
        # corrected hash.
        from godel._events import Event as _Event
        full_hash = _Event.compute_request_hash(req)
        started_req = dict(req)
        started_req["content"] = _truncate_for_log(content)

        log = ctx.event_log
        # Suppress the JSONL write inside emit_started so only our single
        # corrected-hash snapshot is persisted.  The in-memory Event is
        # still created & registered in _events_by_id (required for
        # FINISHED/FAILED to find it later).
        prev_suppress = log._replay_suppress
        log._replay_suppress = True
        try:
            event = log.emit_started(
                op="write_text",
                step_path=tuple(ctx.step_stack),
                request=started_req,
                invocation_seq=inv_seq,
                step_local_seq=local_seq,
                parent_event_id=ctx.current_parent_event_id,
            )
        finally:
            log._replay_suppress = prev_suppress
        # Patch the hash to the FULL-content hash, then append once.
        event.request_hash = full_hash
        log._append_event(event)

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
            _safe_emit_failed(
                ctx.event_log,
                event.event_id,
                exc,
                tuple(ctx.step_stack) if ctx else (),
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
