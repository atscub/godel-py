"""Workflow and step decorators, WorkflowFail, parallel, retry."""
import asyncio
import functools
import hashlib
import inspect
import json as _json
import os
import traceback
import uuid
from typing import Awaitable, Callable, TypeVar

from godel._context import (
    WorkflowContext,
    _current_workflow,
    _on_run_start,
    _current_stream_path,
    _current_transcript,
)
from godel._exceptions import PauseSignal, RewindSignal

T = TypeVar("T")


class _StepCoroutine:
    """Awaitable wrapper around a step coroutine that carries step metadata.

    When a ``@step``-decorated function is called, it returns one of these
    objects instead of a bare coroutine.  This lets ``parallel()`` inspect
    ``_step_options`` on each awaitable *before* scheduling execution,
    enabling registration-time (pre-execution) validation.

    ``_StepCoroutine`` is a transparent proxy: it implements ``__await__`` by
    delegating to the wrapped coroutine, so it is usable anywhere a coroutine
    or awaitable is expected (including ``await`` expressions and
    ``asyncio.gather``).
    """

    __slots__ = ("_coro", "_step_options")

    def __init__(self, coro, step_options: dict):
        self._coro = coro
        self._step_options = step_options

    def __await__(self):
        return self._coro.__await__()

    def close(self):
        return self._coro.close()

    def send(self, value):
        return self._coro.send(value)

    def throw(self, *args):
        return self._coro.throw(*args)

    # Support PEP 585-style generic subscripting (e.g. _StepCoroutine[int])
    # purely for type-hint ergonomics; has no effect on iscoroutine() checks
    # (those are satisfied by the wrapped coroutine's own behaviour when used
    # via __await__).
    def __class_getitem__(cls, item):
        return cls

# Directory containing the godel library source — used to filter out
# internal frames when computing source_location so it points to user code.
_GODEL_LIB_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep


def _user_source_location(tb_frames: traceback.StackSummary) -> str:
    """Return the source location of the outermost user-code frame.

    Walks the traceback from the innermost frame outward and returns the
    first frame whose filename does *not* live inside the godel library
    package.  Falls back to the innermost frame if all frames are library
    internals (e.g. unit-test helpers that raise directly).
    """
    if not tb_frames:
        return ""
    for frame in reversed(tb_frames):
        frame_path = os.path.abspath(frame.filename) if frame.filename else ""
        if not frame_path.startswith(_GODEL_LIB_DIR):
            return f"{frame.filename}:{frame.lineno}"
    # All frames are library internals — fall back to innermost frame
    last = tb_frames[-1]
    return f"{last.filename}:{last.lineno}"


class WorkflowFail(Exception):
    pass


def workflow(
    fn=None,
    *,
    stream_agents: bool = False,
    capture_stdout: bool = False,
    redact: list[Callable] | None = None,
):
    """Decorator that marks an async function as a Godel workflow.

    Can be applied as ``@workflow`` (bare) or ``@workflow(...)`` with options:

    Args:
        stream_agents: When ``True``, agent responses will be streamed to the
            transcript writer as they arrive (instead of buffered).  Defaults
            to ``False`` (buffered).
        capture_stdout: When ``True``, stdout emitted during the workflow is
            captured and attached to the event log.  Defaults to ``False``.
        redact: A list of callables (redactors).  Each callable receives a
            string and returns the redacted version.  Applied to event payloads
            before they are written to the audit log.  Defaults to ``None``
            (no redaction).

    Raises:
        TypeError: If ``redact`` contains a non-callable entry.
    """
    # Validate redact entries at decoration time (not runtime).
    # Each redactor must be callable AND accept exactly one positional
    # argument (the string to be redacted).  We also accept variadic
    # callables (e.g. ``lambda *a: ...``) so plugin authors can share one
    # generic shim across multiple redactor slots.  Arity is checked via
    # ``inspect.signature`` so wrong-signature callables fail at decoration
    # time rather than silently passing validation and blowing up later
    # when the first event payload is redacted.
    if redact is not None:
        for i, entry in enumerate(redact):
            if not callable(entry):
                raise TypeError(
                    f"@workflow redact[{i}] must be callable, got {type(entry).__name__!r}"
                )
            try:
                sig = inspect.signature(entry)
            except (TypeError, ValueError):
                # Built-ins and some C-implemented callables don't expose a
                # signature; skip arity check rather than reject outright.
                continue
            params = list(sig.parameters.values())
            # Count the positional-capable parameters (POSITIONAL_ONLY,
            # POSITIONAL_OR_KEYWORD) that have no default, plus VAR_POSITIONAL.
            required_positional = [
                p for p in params
                if p.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                and p.default is inspect.Parameter.empty
            ]
            has_var_positional = any(
                p.kind is inspect.Parameter.VAR_POSITIONAL for p in params
            )
            # Acceptable arities: exactly one required positional, OR
            # zero required positionals with *args, OR *args alone.
            # Anything else (0-arg, 2-arg, required-kwarg-only) is rejected.
            if has_var_positional and len(required_positional) <= 1:
                continue
            if len(required_positional) == 1:
                continue
            raise TypeError(
                f"@workflow redact[{i}] must accept exactly one positional "
                f"argument (the string to redact), got signature {sig}"
            )

    def _make_workflow(fn):
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"@workflow requires an async function, got {fn.__name__}")

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            from godel._event_log import EventLog
            from godel._context import _pending_replay
            from godel._rewind import apply_rewind
            from godel._replay import ReplayWalker

            replay_walker = _pending_replay.get()

            # Workflow-level capture: create a TranscriptWriter for the whole run
            # and install it in _current_transcript so nested @step decorators with
            # capture_stdout=True can share it.  Also wrap the entire workflow body
            # in the fd-level capture context so that code directly inside @workflow
            # (outside any @step) is also captured.
            _wf_transcript = None
            _wf_transcript_token = None
            _wf_owns_transcript = False  # True only when WE created the TranscriptWriter
            _wf_owned_tmpdir: str | None = None
            if capture_stdout:
                from godel._stdout_capture import capture as _wf_capture
                # Reuse an already-injected transcript (e.g. from tests or a
                # parent workflow); only create a new one if none is active.
                _existing_transcript = _current_transcript.get()
                if _existing_transcript is not None:
                    _wf_transcript = _existing_transcript
                else:
                    import tempfile
                    from godel._transcript import TranscriptWriter
                    _wf_owned_tmpdir = tempfile.mkdtemp(prefix="godel-wf-capture-")
                    _wf_transcript = TranscriptWriter(_wf_owned_tmpdir)
                    _wf_transcript_token = _current_transcript.set(_wf_transcript)
                    _wf_owns_transcript = True

            if replay_walker:
                # Resume: reuse existing run_id, append to same log
                run_id = replay_walker._log._run_id
                event_log = replay_walker._log  # reuse the loaded log (already open for append)
                event_log._replay_suppress = True  # suppress writes during replay phase
                # Clear any pause sentinel so the first live @step after replay
                # does not immediately re-pause (idempotent — no-op if absent).
                try:
                    from godel._pause import clear_pause_request
                    clear_pause_request(run_id)
                except OSError:
                    pass
            else:
                run_id = str(uuid.uuid4())
                event_log = EventLog(run_id)

            source_file = getattr(fn, '_source_file', '') or getattr(wrapper, '_source_file', '')

            # Create a TranscriptWriter for advisory streaming events when
            # stream_agents=True.  We place the transcript in a run-specific
            # subdirectory (runs/<run_id>/) so it is co-located with and
            # trivially correlated to the audit log by run_id.
            # The writer is closed in the finally block below.
            transcript = None
            if stream_agents:
                import os as _os
                from godel._transcript import TranscriptWriter
                _transcript_dir = _os.path.join(
                    str(event_log._file_path.parent), run_id
                )
                transcript = TranscriptWriter(_transcript_dir, run_id=run_id)

            max_rewinds = 100
            rewind_count = 0
            result = None
            start_event = None

            try:
                while True:
                    ctx = WorkflowContext(
                        run_id=run_id,
                        event_log=event_log,
                        replay_walker=replay_walker,
                        source_file=source_file,
                        _local_replay_suppress=event_log._replay_suppress,
                        stream_agents=stream_agents,
                        transcript=transcript,
                    )
                    token = _current_workflow.set(ctx)

                    # Notify CLI of run start (run_id + log path) — only on first iteration
                    if rewind_count == 0:
                        on_start = _on_run_start.get(None)
                        if on_start:
                            on_start(run_id, str(event_log._file_path))

                    # Emit WORKFLOW_STARTED only on first run (not on rewind re-invocations
                    # and not when resuming a run that already has a WORKFLOW_STARTED).
                    if rewind_count == 0 and replay_walker is None:
                        # Attempt to store args/kwargs as JSON-serialisable structures so
                        # resume can recover them programmatically.  CLI callers always pass
                        # strings, so this succeeds by default.  Programmatic callers that
                        # pass non-JSON-serialisable values (e.g. custom objects) fall back
                        # to a repr string with an 'args_repr_only': True sentinel so the
                        # resume command can detect and reject the run gracefully.
                        try:
                            _json.dumps({"args": list(args), "kwargs": dict(kwargs)})
                            _wf_args_payload = {"args": list(args), "kwargs": dict(kwargs)}
                        except (TypeError, ValueError):
                            _wf_args_payload = {
                                "args": repr(args),
                                "kwargs": repr(kwargs),
                                "args_repr_only": True,
                            }
                        start_event = event_log.emit_started(
                            op="WORKFLOW_STARTED",
                            step_path=(),
                            request={
                                "function": fn.__name__,
                                **_wf_args_payload,
                                "source_file": source_file,
                            },
                        )
                        ctx.push_event_scope(start_event.event_id)
                    else:
                        # After rewind, or on resume from an existing log: reuse the
                        # original WORKFLOW_STARTED event (same event_id, no new event).
                        wf_events = [e for e in event_log.all_events() if e.op == "WORKFLOW_STARTED"]
                        if wf_events:
                            start_event = wf_events[0]
                            ctx.push_event_scope(start_event.event_id)

                    try:
                        if capture_stdout and _wf_transcript is not None:
                            from godel._stdout_capture import capture as _wf_cap
                            _wf_stream_path = list(_current_stream_path.get() or [])
                            with _wf_cap(step_path=[], stream_path=_wf_stream_path, transcript=_wf_transcript):
                                result = await fn(*args, **kwargs)
                        else:
                            result = await fn(*args, **kwargs)
                        if start_event is not None:
                            event_log.emit_finished(start_event.event_id, response={"result": repr(result)})
                        break  # Success — exit the rewind loop

                    except RewindSignal as sig:
                        rewind_count += 1
                        if rewind_count >= max_rewinds:
                            raise RuntimeError(f"Exceeded maximum rewind count ({max_rewinds})")

                        # Apply the graph cut
                        apply_rewind(event_log, sig.target_ids, sig.reason)

                        # Build a new ReplayWalker from the modified log
                        replay_walker = ReplayWalker(event_log)
                        event_log._replay_suppress = True

                        # Reset context var before creating a fresh one next iteration
                        _current_workflow.reset(token)
                        token = None
                        continue  # Re-invoke the workflow

                    except PauseSignal as sig:
                        # Emit a PAUSED metadata event on the WORKFLOW_STARTED scope
                        if start_event is not None:
                            from godel._events import EventStatus
                            event_log._replay_suppress = False
                            event_log.emit_started(
                                op="PAUSED",
                                step_path=(),
                                request={"reason": sig.reason, "requested_ts": sig.request_ts},
                                invocation_seq=-1,
                                step_local_seq=-1,
                                parent_event_id=start_event.event_id,
                            )
                            # Finish the WORKFLOW_STARTED event with PAUSED status
                            event_log.emit_finished(
                                start_event.event_id,
                                response={"result": "paused", "reason": sig.reason},
                                status=EventStatus.PAUSED,
                            )
                            event_log._file.flush()
                        raise

                    except Exception as exc:
                        if start_event is not None:
                            tb_frames = traceback.extract_tb(exc.__traceback__)
                            source_loc = _user_source_location(tb_frames)
                            event_log.emit_failed(
                                start_event.event_id,
                                str(exc),
                                error_type=type(exc).__name__,
                                source_location=source_loc,
                            )
                        raise
                    finally:
                        ctx.pop_event_scope()
                        if token is not None:
                            _current_workflow.reset(token)

                return result

            finally:
                wrapper._last_run_id = run_id
                event_log._replay_suppress = False
                event_log.close()
                if transcript is not None:
                    transcript.close()
                try:
                    from godel._pause import clear_pause_request
                    clear_pause_request(run_id)
                except OSError:
                    pass
                if _wf_owns_transcript and _wf_transcript is not None:
                    try:
                        _wf_transcript.close()
                    except Exception:
                        pass
                    # NIT-2: remove the temp directory we created for this run.
                    if _wf_owned_tmpdir is not None:
                        try:
                            import shutil as _shutil
                            _shutil.rmtree(_wf_owned_tmpdir, ignore_errors=True)
                        except Exception:
                            pass
                if _wf_transcript_token is not None:
                    _current_transcript.reset(_wf_transcript_token)

        wrapper._is_workflow = True
        wrapper._workflow_options = {
            "stream_agents": stream_agents,
            "capture_stdout": capture_stdout,
            "redact": list(redact) if redact is not None else [],
        }
        return wrapper

    # Support both @workflow (bare) and @workflow(...) (with options).
    # When called as @workflow with no parentheses, fn is the decorated function.
    # When called as @workflow(...), fn is None and we return _make_workflow.
    if fn is not None:
        return _make_workflow(fn)
    return _make_workflow


def step(fn=None, *, name=None, idempotent=False, capture_stdout: bool = False):
    """Decorator that marks an async function as a workflow step.

    Can be applied as ``@step`` (bare) or ``@step(...)`` with options:

    Args:
        name: Override the step name used in the event log.  Defaults to the
            function name.
        idempotent: When ``True``, the step is safe to re-execute after an
            interrupted run.  Defaults to ``False``.
        capture_stdout: When ``True``, stdout emitted during this step is
            captured and attached to the step's event log entry.  Cannot be
            ``True`` for steps used inside a ``parallel()`` block.
            Defaults to ``False``.
    """
    def decorator(fn):
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"@step requires an async function, got {fn.__name__}")
        step_name = name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = _current_workflow.get()
            if ctx is None:
                raise RuntimeError(f"@step {step_name!r} called outside a @workflow")
            ctx.step_stack.append(step_name)
            step_path = tuple(ctx.step_stack)

            # Check for a pause request at this step boundary.
            # Skipped during replay (replay is offline log catchup; pause is live-only).
            if not (ctx.replay_walker is not None and ctx.replay_walker.is_replaying):
                from godel._pause import check_pause_request
                check_pause_request(ctx.run_id)

            # Compute source_hash for this step's function body.
            # Used by the source-edit guardrail on resume to detect whether a
            # cached step's code was modified between the original run and now.
            # source_hash is intentionally excluded from request_hash (see
            # Event._HASH_EXCLUDE_KEYS) so it doesn't trigger --on-mismatch.
            #
            # Normalization: trailing whitespace is stripped from each line and
            # consecutive blank lines are collapsed to a single blank line before
            # hashing.  This prevents whitespace-only edits (e.g. an editor that
            # trims trailing spaces on save) from silently flipping the hash and
            # triggering a spurious source-edit warning.
            #
            # Known limitation (triple-quoted string interiors): the per-line
            # rstrip() is applied to ALL lines including lines inside multi-line
            # string literals.  An edit that adds or removes trailing whitespace
            # ONLY inside a triple-quoted string will not change the normalised
            # hash — this is a false negative (the guardrail will not fire).
            # This is acceptable for a guardrail meant to catch typical code
            # edits; it mirrors the known-limitation for import changes below.
            #
            # Known limitation (import changes): inspect.getsource captures only
            # the function body itself, not the enclosing module's import
            # statements.  If a step delegates to a helper and only the helper's
            # import changes (e.g. swapping ``from lib_v1 import foo`` for
            # ``from lib_v2 import foo``), the hash will NOT change and the
            # guardrail will not fire — the cached result will be silently
            # replayed even though the step's effective behaviour has changed.
            # To catch import-level changes you must rewind manually or use
            # the --on-source-edit=abort policy together with a broader test suite.
            try:
                src = inspect.getsource(fn)
                # Normalise: strip trailing whitespace per line, collapse runs of
                # blank lines, strip leading/trailing blank lines, then re-join so
                # only meaningful code affects the hash.  This prevents whitespace-
                # only edits (e.g. an editor that trims trailing spaces on save, or
                # adds/removes a trailing newline) from silently flipping the hash
                # and triggering a spurious source-edit warning.
                #
                # NOTE: rstrip() is applied to all lines including lines inside
                # triple-quoted string literals — see known limitation above.
                lines = src.splitlines()
                normalised_lines: list[str] = []
                prev_blank = False
                for line in lines:
                    stripped = line.rstrip()
                    is_blank = stripped == ""
                    if is_blank and prev_blank:
                        continue  # collapse consecutive blank lines
                    normalised_lines.append(stripped)
                    prev_blank = is_blank
                # Strip trailing blank lines so a lone trailing newline in the
                # source doesn't produce a different hash than no trailing newline.
                while normalised_lines and normalised_lines[-1] == "":
                    normalised_lines.pop()
                normalised_src = "\n".join(normalised_lines)
                source_hash = hashlib.sha256(normalised_src.encode()).hexdigest()
            except (OSError, TypeError):
                source_hash = ""

            # Source-edit guardrail: if the replay walker has a FINISHED event
            # for this step and its recorded source_hash differs from the current
            # one, consult the source-edit policy before proceeding.
            if ctx.replay_walker is not None and source_hash:
                from godel._replay import check_source_edit
                await check_source_edit(
                    ctx.replay_walker,
                    step_path=step_path,
                    invocation_seq=ctx._invocation_counts.get(step_path, 0),
                    current_source_hash=source_hash,
                    step_name=step_name,
                )

            # Track invocation count for this step_path
            inv_count = ctx._invocation_counts.get(step_path, 0)
            ctx._invocation_counts[step_path] = inv_count + 1
            ctx._step_local_seq[step_path] = 0

            # If this step has no cached FINISHED result in the replay index,
            # we have crossed the boundary from replay phase into live execution.
            # Turn off _replay_suppress so that all events from this step onward
            # are written to the log (including step.enter itself).
            #
            # Also capture the cached event_id (if any) here, before suppression
            # is potentially lifted below.  This is used by the history-append
            # logic (WARN-1 fix): during replay the step emits a fresh event_id
            # that is NOT written to the persisted log.  Callers who use
            # last_step_event_id() to look up cached results must receive the
            # original persisted event_id, not the ephemeral replay one.
            cached_step_event_id: str | None = None
            suppress_at_entry: bool = False
            if (
                ctx.replay_walker is not None
                and ctx.event_log is not None
                and ctx._local_replay_suppress
            ):
                suppress_at_entry = True
                step_key = (step_path, inv_count, 0, "step.enter")
                cached_step = ctx.replay_walker._index.get(step_key)
                from godel._events import EventStatus as _ES
                if cached_step is None or cached_step.status != _ES.FINISHED:
                    ctx.replay_walker._replaying = False
                    ctx.event_log._replay_suppress = False
                    ctx._local_replay_suppress = False
                    suppress_at_entry = False
                else:
                    # This step is fully cached — record the original event_id
                    # so we can append it to _step_event_history instead of the
                    # ephemeral replay event_id that emit_started will generate.
                    cached_step_event_id = cached_step.event_id

            event = None
            if ctx.event_log:
                event = ctx.event_log.emit_started(
                    op="step.enter",
                    step_path=step_path,
                    request={"name": step_name, "args": repr(args), "kwargs": repr(kwargs), "source_hash": source_hash},
                    invocation_seq=inv_count,
                    step_local_seq=0,
                    parent_event_id=ctx.current_parent_event_id,
                )
                ctx.push_event_scope(event.event_id)

            try:
                # When capture_stdout=True, wrap the step body in the fd-level
                # stdout capture context manager so that print() output and child
                # process stdout are routed to the transcript as "stdout" events.
                # The transcript writer is taken from the _current_transcript
                # contextvar (set by the enclosing @workflow when it also opts in,
                # or created lazily here for step-level capture).
                if capture_stdout:
                    import shutil
                    import tempfile
                    from godel._stdout_capture import capture as _capture
                    from godel._transcript import TranscriptWriter
                    tw = _current_transcript.get()
                    _owns_transcript = False
                    _owned_tmpdir: str | None = None
                    if tw is None:
                        # No workflow-level transcript: create a temp one for this step.
                        _owned_tmpdir = tempfile.mkdtemp(prefix="godel-capture-")
                        tw = TranscriptWriter(_owned_tmpdir, run_id=str(step_path))
                        _owns_transcript = True
                    _stream_path = list(_current_stream_path.get() or [])
                    try:
                        with _capture(step_path=list(step_path), stream_path=_stream_path, transcript=tw):
                            result = await fn(*args, **kwargs)
                    finally:
                        # WARN-1 fix: close/cleanup the owned transcript even
                        # when fn() raised, so the TranscriptWriter file handle
                        # and mkdtemp dir are not leaked on the exception path.
                        if _owns_transcript:
                            try:
                                tw.close()
                            except Exception:
                                pass
                            # NIT-2: remove the temp directory we created.
                            if _owned_tmpdir is not None:
                                try:
                                    shutil.rmtree(_owned_tmpdir, ignore_errors=True)
                                except Exception:
                                    pass
                else:
                    result = await fn(*args, **kwargs)
                if event:
                    ctx.event_log.emit_finished(event.event_id, response={"result": repr(result)})
                    # WARN-1 fix: during replay, event.event_id is a fresh ephemeral
                    # ULID that is NOT written to the persisted log (_replay_suppress
                    # was True when emit_started ran AND was never turned off by any
                    # child of THIS branch).  Append the original cached event_id so
                    # that last_step_event_id() returns a persisted ID.
                    #
                    # Use ctx._local_replay_suppress (per-branch flag) instead of the
                    # shared event_log._replay_suppress.  In parallel() blocks, each
                    # branch context gets its own _local_replay_suppress that is only
                    # cleared when THIS branch (or its children) exits replay mode.
                    # A sibling branch clearing the shared flag does NOT affect our
                    # _local_replay_suppress — fixing the parallel race condition.
                    history_id = (
                        cached_step_event_id
                        if (cached_step_event_id is not None
                            and ctx._local_replay_suppress)
                        else event.event_id
                    )
                    ctx._step_event_history.append(history_id)
                return result
            except Exception as exc:
                # PauseSignal and RewindSignal are control-flow signals, not step
                # failures.  Re-raise immediately so the outer @workflow handler
                # catches them cleanly.  Emitting emit_failed here would mark the
                # enclosing step as FAILED in the audit log even though the step was
                # only paused (WARN-2 fix).
                if isinstance(exc, (PauseSignal, RewindSignal)):
                    raise

                # Extract source location from traceback, preferring user-code frames
                tb_frames = traceback.extract_tb(exc.__traceback__)
                source_location = _user_source_location(tb_frames)

                # Attach step_path and source_location to non-GodelError exceptions if not set.
                # Wrap in try/except: frozen dataclasses and other immutable exception types
                # raise AttributeError (FrozenInstanceError) on attribute assignment, which would
                # otherwise mask the original exception and propagate the wrong type to callers.
                from godel._exceptions import GodelError
                if not isinstance(exc, GodelError):
                    if not getattr(exc, "step_path", None):
                        try:
                            exc.step_path = step_path
                        except AttributeError:
                            pass
                    if not getattr(exc, "source_location", None):
                        try:
                            exc.source_location = source_location
                        except AttributeError:
                            pass

                if event:
                    ctx.event_log.emit_failed(
                        event.event_id,
                        str(exc),
                        error_type=type(exc).__name__,
                        step_path=list(step_path),
                        source_location=source_location,
                        remediation_hint=getattr(exc, "remediation_hint", ""),
                    )
                raise
            finally:
                if event:
                    ctx.pop_event_scope()
                ctx.step_stack.pop()

        _opts = {"capture_stdout": capture_stdout}

        # Wrap the async wrapper so that calling the step-decorated function
        # returns a _StepCoroutine instead of a bare coroutine.  This allows
        # parallel() to inspect _step_options before scheduling execution.
        @functools.wraps(fn)
        def step_caller(*args, **kwargs):
            return _StepCoroutine(wrapper(*args, **kwargs), _opts)

        # Preserve coroutine-function identity so that
        # ``asyncio.iscoroutinefunction(step_fn)`` continues to return ``True``
        # after decoration.  Without this, ``@workflow @step`` stacking breaks
        # because @workflow's async-function check raises TypeError.
        # asyncio.coroutines._is_coroutine is the public-ish sentinel asyncio
        # uses to recognise coroutine functions (see CPython asyncio/coroutines.py).
        step_caller._is_coroutine = asyncio.coroutines._is_coroutine

        step_caller._is_step = True
        step_caller._idempotent = idempotent
        step_caller._step_options = _opts
        return step_caller

    return decorator(fn) if fn is not None else decorator


async def parallel(*aws: Awaitable[T]) -> tuple:
    """Run awaitables concurrently inside a workflow, emitting FORK/JOIN events.

    Each awaitable runs in an isolated WorkflowContext copy so that concurrent
    branches cannot corrupt each other's step_path or event_id_stack at
    await-point interleaving.  Results are gathered via ``asyncio.gather`` with
    ``return_exceptions=True`` so every branch runs to completion regardless of
    whether siblings raise.

    Re-raise behaviour
    ------------------
    After gathering, results are partitioned into three buckets:

    * **RewindSignal** — a control-flow signal (subclass of ``Exception``), not
      a true branch failure.  FORK and JOIN are finished cleanly (so the audit
      log stays coherent) and the first ``RewindSignal`` is re-raised so the
      enclosing ``@workflow`` decorator can invoke ``apply_rewind`` and perform
      the graph cut.  **Priority rule:** if any branch raises ``RewindSignal``,
      the rewind path wins even when other branches also raised real
      exceptions — those non-rewind exceptions are discarded so the graph cut
      can proceed deterministically.
    * **Other exceptions** — FORK and JOIN are failed, then the first exception
      is re-raised to the caller.
    * **No exception** — FORK and JOIN are finished and the results tuple is
      returned normally.

    If called outside a ``@workflow`` context (no ``EventLog`` attached) the
    function falls back to a bare ``asyncio.gather`` with no event emission.

    Raises:
        ConfigError: If any of the supplied awaitables is a coroutine whose
            underlying ``@step`` function was decorated with
            ``capture_stdout=True``.  Parallel-safe per-step stdout capture is
            not supported; each branch would require its own pipe.
    """
    from godel._exceptions import ConfigError

    # Validate that no step in the parallel block has capture_stdout=True.
    # _StepCoroutine objects carry _step_options, set by the @step decorator.
    offender = None
    for aw in aws:
        step_opts = getattr(aw, '_step_options', None)
        if step_opts and step_opts.get("capture_stdout"):
            offender = aw
            break

    if offender is not None:
        # Close all awaitables so that no coroutines are left unawaited,
        # then raise ConfigError.
        for aw in aws:
            try:
                aw.close()
            except Exception:
                pass
        raise ConfigError(
            "capture_stdout=True is not allowed inside parallel() — "
            "each branch would require its own pipe. "
            "Use capture_stdout on the enclosing @workflow instead."
        )

    ctx = _current_workflow.get()

    if not ctx or not ctx.event_log:
        results = await asyncio.gather(*aws)
        return tuple(results)

    # Track FORK invocation count for replay positioning
    step_path = tuple(ctx.step_stack)
    fork_key = (*step_path, "__FORK__")
    fork_inv = ctx._invocation_counts.get(fork_key, 0)
    ctx._invocation_counts[fork_key] = fork_inv + 1

    # On resume, if replay_walker has a matching FINISHED FORK, the branch
    # primitives will each handle their own replay from cache.  We still
    # emit new FORK/JOIN events (new event_ids, new seq numbers).

    # FORK is a child of the enclosing scope
    parent_eid = ctx.current_parent_event_id

    # Emit FORK
    fork_event = ctx.event_log.emit_started(
        op="FORK",
        step_path=step_path,
        request={"branches": len(aws)},
        invocation_seq=fork_inv,
        parent_event_id=parent_eid,
    )

    # Branches are children of FORK.
    # Each branch gets its own WorkflowContext with independent stacks
    # so concurrent branches don't corrupt each other's step_path or
    # event_id_stack at await-point interleaving.
    ctx.push_event_scope(fork_event.event_id)

    async def _run_branch(coro):
        branch_ctx = WorkflowContext(
            run_id=ctx.run_id,
            step_stack=list(ctx.step_stack),                 # independent copy
            event_log=ctx.event_log,                         # shared
            _invocation_counts=ctx._invocation_counts,       # shared (keyed by unique paths)
            _step_local_seq=ctx._step_local_seq,             # shared
            replay_walker=ctx.replay_walker,                 # shared
            source_file=ctx.source_file,
            _event_id_stack=list(ctx._event_id_stack),       # independent copy
            _step_event_history=ctx._step_event_history,     # shared; append order across branches is non-deterministic
            # Per-branch suppress flag: each branch independently tracks whether
            # it is still in replay mode.  Snapshots the shared flag at fork time.
            _local_replay_suppress=ctx.event_log._replay_suppress,
        )
        token = _current_workflow.set(branch_ctx)
        try:
            return await coro
        finally:
            _current_workflow.reset(token)

    # Run all branches concurrently with isolated stacks.
    # asyncio.gather() propagates the current context snapshot to each child
    # Task (CPython 3.7+), so _current_stream_path and other contextvars are
    # automatically inherited from the fork point without explicit copy_context.
    # If future changes move branch execution to a thread pool, use
    # `ctx = contextvars.copy_context(); pool.submit(ctx.run, fn, ...)` instead.
    results = await asyncio.gather(
        *[_run_branch(aw) for aw in aws], return_exceptions=True
    )

    # Pop FORK scope before emitting JOIN
    ctx.pop_event_scope()

    # Partition results into three buckets:
    #   rewind_signals — control-flow signals for graph-cut; highest priority
    #   pause_signals  — control-flow signals for clean pause; second priority
    #   exceptions     — real branch failures; always recorded in the audit log
    #
    # RewindSignal is a control-flow mechanism, not a branch failure — do NOT
    # emit_failed on FORK/JOIN for it. Instead emit_finished and re-raise so
    # @workflow catches it and apply_rewind cascades invalidation normally.
    #
    # PauseSignal wins over real exceptions in terms of re-raise priority, but
    # MUST NOT silently drop real exceptions. When both pause_signals and
    # exceptions are present, we emit FAILED events for the exception branches
    # into the audit log (so the failure is permanently recorded) and THEN
    # re-raise PauseSignal so the workflow can pause cleanly. (CRITICAL-1 fix)
    rewind_signals: list[RewindSignal] = [r for r in results if isinstance(r, RewindSignal)]
    pause_signals: list[PauseSignal] = [r for r in results if isinstance(r, PauseSignal)]
    exceptions: list[Exception] = [
        r for r in results
        if isinstance(r, Exception)
        and not isinstance(r, (RewindSignal, PauseSignal))
    ]

    # JOIN is a sibling of FORK (same parent)
    join_event = ctx.event_log.emit_started(
        op="JOIN",
        step_path=step_path,
        request={"fork_id": fork_event.event_id, "branches": len(aws)},
        invocation_seq=fork_inv,
        parent_event_id=parent_eid,
    )

    if rewind_signals:
        # At least one branch signalled a rewind — finish FORK/JOIN cleanly so the
        # audit log is not corrupted, then propagate the RewindSignal upward so
        # @workflow can handle the graph cut and re-invocation.
        ctx.event_log.emit_finished(fork_event.event_id, response={"branches": len(aws)})
        ctx.event_log.emit_finished(join_event.event_id, response={"branches": len(aws)})
        raise rewind_signals[0]
    elif pause_signals:
        # At least one branch paused. Before re-raising PauseSignal, record any
        # real exceptions so they are not silently dropped from the audit log.
        # The FORK/JOIN are marked FAILED if there are also real exceptions,
        # otherwise PAUSED (via finished with paused status).
        if exceptions:
            first_exc = exceptions[0]
            tb_frames = traceback.extract_tb(first_exc.__traceback__)
            source_loc = _user_source_location(tb_frames)
            ctx.event_log.emit_failed(
                fork_event.event_id,
                f"{len(exceptions)} branch(es) failed alongside pause",
                error_type=type(first_exc).__name__,
                source_location=source_loc,
            )
            ctx.event_log.emit_failed(
                join_event.event_id,
                f"{len(exceptions)} branch(es) failed alongside pause",
                error_type=type(first_exc).__name__,
                source_location=source_loc,
            )
        else:
            ctx.event_log.emit_finished(fork_event.event_id, response={"branches": len(aws)})
            ctx.event_log.emit_finished(join_event.event_id, response={"branches": len(aws)})
        raise pause_signals[0]
    elif exceptions:
        first_exc = exceptions[0]
        tb_frames = traceback.extract_tb(first_exc.__traceback__)
        source_loc = _user_source_location(tb_frames)
        ctx.event_log.emit_failed(
            fork_event.event_id,
            f"{len(exceptions)} branch(es) failed",
            error_type=type(first_exc).__name__,
            source_location=source_loc,
        )
        ctx.event_log.emit_failed(
            join_event.event_id,
            f"{len(exceptions)} branch(es) failed",
            error_type=type(first_exc).__name__,
            source_location=source_loc,
        )
        raise first_exc  # re-raise first exception
    else:
        ctx.event_log.emit_finished(fork_event.event_id, response={"branches": len(aws)})
        ctx.event_log.emit_finished(join_event.event_id, response={"branches": len(aws)})

    return tuple(results)


def retry(times: int):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for _ in range(times):
                try:
                    return await fn(*args, **kwargs)
                except (RewindSignal, PauseSignal):
                    # Control-flow signals must never be retried — propagate immediately
                    # so the enclosing @workflow decorator can handle them correctly.
                    # WARN-3: without this guard, a future broad except clause would
                    # silently retry the signal N times before it escapes.
                    raise
                except WorkflowFail as e:
                    last_exc = e
            raise last_exc

        return wrapper

    return decorator
