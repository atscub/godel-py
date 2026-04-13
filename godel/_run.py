"""run() primitive — the single audited escape hatch for subprocess execution."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from godel._context import _privileged, _current_stream_path
from godel._decorators import WorkflowFail
from godel._exceptions import _render_context_marker


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
            elif match.hit and match.status == EventStatus.STARTED and not idempotent:
                from godel._exceptions import UnsafeResumeError
                raise UnsafeResumeError(
                    f"run() has STARTED-only state and is not marked idempotent",
                    cmd=cmd,
                    step_path=tuple(ctx.step_stack),
                )
            # STARTED + idempotent, or no match: fall through to execute

        event = None
        if ctx and ctx.event_log:
            event = ctx.event_log.emit_started(
                op="run",
                step_path=tuple(ctx.step_stack),
                request={"cmd": cmd, "cwd": cwd, "timeout": timeout, "idempotent": idempotent},
                invocation_seq=inv_seq,
                step_local_seq=local_seq,
                parent_event_id=ctx.current_parent_event_id,
                stream_path=new_stream_path,
            )

        token = _privileged.set(True)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
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
            stdout = stdout_b.decode()
            stderr = stderr_b.decode()
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
