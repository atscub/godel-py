"""run() primitive — the single audited escape hatch for subprocess execution."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass

from godel._context import _privileged, _current_stream_path, _line_observer, _step_idempotent
from godel._decorators import WorkflowFail
from godel._exceptions import _render_context_marker

# Read limit for asyncio.StreamReader.readuntil() — 4 MB so lines up to that
# size are handled natively.  LimitOverrunError is caught and handled below
# for lines that exceed this.
_READUNTIL_LIMIT = 4 * 1024 * 1024


async def _drain_stream(
    stream: asyncio.StreamReader,
    on_line=None,
) -> bytes:
    """Read *stream* line-by-line, calling *on_line(line_bytes)* per line.

    Returns the concatenated bytes of all lines (byte-identical to what
    ``proc.communicate()`` would have returned for the same stream).

    Lines longer than ``_READUNTIL_LIMIT`` bytes are handled by falling back
    to a manual chunked read until the next newline (or EOF).  This avoids
    ``asyncio.LimitOverrunError`` while keeping the contract that every
    contiguous block up to a newline is passed to *on_line* as one call.
    """
    buf = bytearray()
    while True:
        try:
            line = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            # EOF in the middle of a (possibly empty) partial line.
            line = exc.partial
            if line:
                buf += line
                if on_line is not None:
                    on_line(bytes(line))
            break
        except asyncio.LimitOverrunError as exc:
            # Line exceeds the internal StreamReader buffer limit.
            # Consume what's available, then keep reading until we find \n.
            oversized = bytearray(await stream.read(exc.consumed))
            while True:
                try:
                    tail = await stream.readuntil(b"\n")
                    oversized += tail
                    break
                except asyncio.IncompleteReadError as inner_exc:
                    oversized += inner_exc.partial
                    break
                except asyncio.LimitOverrunError as inner_exc:
                    oversized += await stream.read(inner_exc.consumed)
            line = bytes(oversized)
            buf += line
            if on_line is not None:
                on_line(line)
            continue

        buf += line
        if on_line is not None:
            on_line(bytes(line))

    return bytes(buf)


async def _kill_process_group(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    """Terminate a process and its entire process group.

    On POSIX, sends SIGTERM to the process group (``-pgid``), waits *grace*
    seconds, then sends SIGKILL to any survivors.  On Windows, kills the
    process directly (no process-group signalling API in asyncio on Windows).
    """
    if proc.returncode is not None:
        return  # already finished

    if sys.platform == "win32":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        return

    # POSIX path — signal the entire process group.
    # Catch OSError broadly (not just ProcessLookupError) because restricted
    # container environments (rootless podman, strict seccomp, some k8s pods)
    # can raise PermissionError (EPERM) from getpgid/killpg.
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # Process already gone or signalling denied — nothing we can do.
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        # Grace period exhausted — force-kill survivors.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, OSError):
            pass


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class CommandFailure(WorkflowFail):
    """Raised when a ``run()`` call exits with a non-zero return code or times out.

    Inherits from :class:`WorkflowFail` so that the ``@workflow`` decorator
    catches it as a recognised workflow-level failure.  Also carries the same
    structured context fields as :class:`~godel._exceptions.GodelError`
    (``step_path``, ``source_location``, ``remediation_hint``) and reuses its
    ``_context_marker`` / ``__str__`` logic via a shared helper imported from
    ``_exceptions``.

    .. note::
        ``CommandFailure`` is intentionally **not** a subclass of
        :class:`~godel._exceptions.GodelError` — it lives in the
        ``WorkflowFail`` hierarchy so that callers catching either branch work
        correctly.  The structured context is provided by delegating to
        :func:`~godel._exceptions._render_context_marker`.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        step_path: tuple[str, ...] = (),
        source_location: str = "",
        remediation_hint: str = "",
    ):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.step_path = step_path
        self.source_location = source_location
        self.remediation_hint = remediation_hint

    def _context_marker(self) -> str:
        # Delegate to the shared helper — keeps behaviour in sync with GodelError.
        return _render_context_marker(self.step_path, self.source_location, self.remediation_hint)

    def __str__(self) -> str:
        base = super().__str__()
        marker = self._context_marker()
        if marker:
            return f"{base} {marker}" if base else marker
        return base


async def run(cmd: str, *, cwd: str | None = None, timeout: float | None = None, idempotent: bool = False) -> CommandResult:
    from godel._context import _current_workflow
    from ulid import ULID

    # Emit STARTED event (before privileged subprocess)
    ctx = _current_workflow.get()

    # Stamp stream_path at launch time on the calling thread.
    # _current_stream_path is read here (on the launching coroutine/thread) to
    # capture the parent path; a fresh ULID is appended to form this launch's
    # path.  The new path is set as the contextvar for the duration of the
    # subprocess so nested run() calls inside agents produce depth-2+ paths.
    parent_stream_path = _current_stream_path.get()
    launch_id = str(ULID())
    new_stream_path = parent_stream_path + [launch_id]
    stream_path_token = _current_stream_path.set(new_stream_path)

    # Outer try/finally guarantees stream_path_token is reset even if
    # ctx.next_op_position() or the replay guard raises (e.g., UnsafeResumeError).
    # Without this the contextvar would leak into the caller's context on early
    # exits from anywhere between the set() above and the subprocess try block.
    try:
        inv_seq, local_seq = (0, 0)
        if ctx:
            inv_seq, local_seq = ctx.next_op_position()

        # Track which idempotency source (if any) promoted a STARTED-only entry
        # here, so the re-emitted STARTED event can be annotated for audit-log
        # traceability (C3 fix).
        _promoted_source: str = ""

        # Replay guard
        if ctx and ctx.replay_walker:
            from godel._events import Event, EventStatus
            req = {"cmd": cmd, "cwd": cwd, "timeout": timeout, "idempotent": idempotent}
            req_hash = Event.compute_request_hash(req)
            match = ctx.replay_walker.try_match(
                step_path=tuple(ctx.step_stack),
                invocation_seq=inv_seq,
                step_local_seq=local_seq,
                op="run",
                request_hash=req_hash,
            )
            if match.hit and match.status == EventStatus.FINISHED:
                resp = match.cached_response
                return CommandResult(
                    stdout=resp.get("stdout", ""),
                    stderr=resp.get("stderr", ""),
                    returncode=resp.get("returncode", 0),
                )
            elif match.hit and match.status == EventStatus.STARTED:
                # Determine effective idempotency and WHICH source granted it.
                # Precedence: per-call kwarg > enclosing @step flag > global
                # resume override.  The source string is persisted on the
                # re-emitted STARTED event below so postmortem can trace why
                # a STARTED-only entry was promoted to re-execution.
                from godel._replay import get_assume_idempotent_all
                if idempotent:
                    _promoted_source = "idempotent_kwarg"
                elif _step_idempotent.get():
                    _promoted_source = "step_idempotent"
                elif get_assume_idempotent_all():
                    _promoted_source = "resume_flag"
                else:
                    _promoted_source = ""

                if not _promoted_source:
                    from godel._exceptions import UnsafeResumeError
                    raise UnsafeResumeError(
                        f"run() has STARTED-only state and is not marked idempotent",
                        cmd=cmd,
                        step_path=tuple(ctx.step_stack),
                    )
            # STARTED + idempotent (any source), or no match: fall through to execute

        event = None
        if ctx and ctx.event_log:
            _req_payload = {"cmd": cmd, "cwd": cwd, "timeout": timeout, "idempotent": idempotent}
            # Annotate the re-emitted STARTED event with the idempotency source
            # that promoted it.  Only set when we actually promoted a prior
            # STARTED-only entry (not on fresh first executions) — C3 fix.
            if _promoted_source:
                _req_payload["assumed_idempotent_source"] = _promoted_source
            event = ctx.event_log.emit_started(
                op="run",
                step_path=tuple(ctx.step_stack),
                request=_req_payload,
                invocation_seq=inv_seq,
                step_local_seq=local_seq,
                parent_event_id=ctx.current_parent_event_id,
                stream_path=new_stream_path,
            )
        # Surface the command to the transcript so live watchers can show what
        # shell input produced the stdout that follows.  Skipped when
        # streaming is disabled (GODEL_STREAM_AGENTS=0 / --no-stream).
        if ctx and ctx.stream_agents and ctx.transcript is not None:
            ctx.transcript.write_event(
                "run.start",
                step_path=tuple(ctx.step_stack),
                stream_path=new_stream_path,
                cmd=cmd,
                cwd=cwd or "",
            )

        token = _privileged.set(True)
        try:
            # Isolate each child in its own process group so that a SIGTERM /
            # SIGKILL sent to the workflow process does not leak into agent
            # subprocesses.  We signal the process *group* explicitly in the
            # cancel / timeout paths below.
            _popen_kwargs: dict = {}
            if sys.platform == "win32":
                import subprocess as _subprocess
                # Access the constant directly: if a future Windows build
                # somehow lacks it, a loud AttributeError is preferable to
                # silently degrading to no process-group isolation.
                _popen_kwargs["creationflags"] = _subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                _popen_kwargs["start_new_session"] = True

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                limit=_READUNTIL_LIMIT,
                **_popen_kwargs,
            )
            # Determine per-line callback for stdout.
            observer = _line_observer.get()
            streaming_ctx = ctx and ctx.stream_agents and ctx.transcript

            if observer is not None:
                # Observer owns each line — suppress raw stdout events.
                stdout_on_line = observer
            elif streaming_ctx:
                # No observer but streaming is active: emit a raw stdout event
                # per line to the transcript (the watch model handles op=stdout).
                step_path_for_stream = tuple(ctx.step_stack) if ctx else ()

                def stdout_on_line(line: bytes) -> None:
                    ctx.transcript.write_event(
                        "stdout",
                        step_path=step_path_for_stream,
                        stream_path=new_stream_path,
                        line=line.decode("utf-8", errors="replace").rstrip("\n"),
                    )
            else:
                stdout_on_line = None

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    asyncio.gather(
                        _drain_stream(proc.stdout, on_line=stdout_on_line),
                        _drain_stream(proc.stderr),
                    ),
                    timeout=timeout,
                )
                # Bound proc.wait() so a zombie/D-state process can't hang us.
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.CancelledError:
                await asyncio.shield(_kill_process_group(proc))
                raise
            except asyncio.TimeoutError:
                await asyncio.shield(_kill_process_group(proc))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            _drain_stream(proc.stdout),
                            _drain_stream(proc.stderr),
                        ),
                        timeout=5,
                    )
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except Exception:
                    pass
                step_path = tuple(ctx.step_stack) if ctx else ()
                error_msg = f"command timed out after {timeout}s: {cmd}"
                if event:
                    ctx.event_log.emit_failed(
                        event.event_id,
                        error_msg,
                        error_type="CommandFailure",
                        step_path=step_path,
                    )
                raise CommandFailure(
                    error_msg,
                    step_path=step_path,
                    remediation_hint=f"Increase the timeout parameter or optimize the command to complete faster.",
                )
            except BaseException:
                # Observer callback or transcript writer raised — don't leak the
                # child process. Kill and reap before re-raising.
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except Exception:
                    pass
                raise
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                step_path = tuple(ctx.step_stack) if ctx else ()
                error_msg = f"command failed (exit {proc.returncode}): {cmd}"
                if event:
                    ctx.event_log.emit_failed(
                        event.event_id,
                        error_msg,
                        error_type="CommandFailure",
                        step_path=step_path,
                    )
                raise CommandFailure(
                    f"command failed (exit {proc.returncode}): {cmd}",
                    stdout=stdout,
                    stderr=stderr,
                    returncode=proc.returncode,
                    step_path=step_path,
                    remediation_hint=f"Check stderr output and verify the command exits with 0. stderr: {stderr[:200]!r}",
                )
            if event:
                ctx.event_log.emit_finished(event.event_id, response={
                    "stdout": stdout[:1000],
                    "stderr": stderr[:1000],
                    "returncode": proc.returncode,
                })
            return CommandResult(stdout=stdout, stderr=stderr, returncode=proc.returncode)
        finally:
            _privileged.reset(token)
    finally:
        _current_stream_path.reset(stream_path_token)
