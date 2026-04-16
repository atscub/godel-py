"""Replay engine for deterministic resume."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum

from godel._events import Event, EventStatus
from godel._event_log import EventLog


def _clear_local_suppress() -> None:
    """Clear _local_replay_suppress on the current WorkflowContext (if any).

    Called alongside clearing event_log._replay_suppress so that the per-branch
    flag stays in sync.  We import _current_workflow lazily to avoid circular
    imports (context → replay → context would be circular if done at module level).

    Correctness invariant: this function reads _current_workflow.get() to find the
    per-branch WorkflowContext.  The isolation that makes this safe in parallel()
    blocks is the explicit ``token = _current_workflow.set(branch_ctx)`` /
    ``reset(token)`` pattern in ``_run_branch()`` (_decorators.py), which ensures
    that while a branch coroutine is executing, _current_workflow always returns
    that branch's own WorkflowContext — never a sibling's.  Clearing
    _local_replay_suppress here therefore only affects the branch that crossed the
    replay/live boundary; sibling branches retain their own _local_replay_suppress
    independently.

    Note: asyncio.gather()'s copy_context() also creates a snapshot at
    task-creation time, but that snapshot still carries the parent context's value
    of _current_workflow (before set() is called), so it is NOT the mechanism
    providing per-branch isolation for _current_workflow specifically.
    """
    try:
        from godel._context import _current_workflow  # local import avoids circular dep
        ctx = _current_workflow.get()
        if ctx is not None:
            ctx._local_replay_suppress = False
    except Exception:
        pass  # guard: never let replay bookkeeping crash the workflow


@dataclass
class ReplayMatch:
    """Result of consulting the replay walker."""
    hit: bool
    event: Event | None = None
    cached_response: dict | None = None
    status: EventStatus | None = None
    hash_mismatch: bool = False


class MismatchPolicy(Enum):
    CONTINUE = "continue"
    INVALIDATE = "invalidate"
    ABORT = "abort"


class ReplayWalker:
    """Cursor-based DAG traversal for deterministic replay.

    On resume, the workflow re-executes from the top. Each primitive calls
    walker.try_match() with its logical position. If a matching FINISHED
    event exists, the cached response is returned. Otherwise the primitive
    executes for real.
    """

    def __init__(self, event_log: EventLog):
        self._log = event_log
        self._events = event_log.all_events()
        self._index: dict[tuple, Event] = {}
        self._build_index()
        self._replaying = True

    def _build_index(self):
        """Index events by logical position for O(1) lookup."""
        for event in self._events:
            if event.op in ("REWIND", "PAUSED"):
                continue  # metadata events; never replayable
            if event.status == EventStatus.INVALIDATED:
                continue
            key = (event.step_path, event.invocation_seq, event.step_local_seq, event.op)
            # Last write wins (FINISHED overwrites STARTED)
            self._index[key] = event

    def try_match(
        self,
        step_path: tuple[str, ...],
        invocation_seq: int,
        step_local_seq: int,
        op: str,
        request_hash: str = "",
    ) -> ReplayMatch:
        """Consult the log for a cached result at this position."""
        key = (step_path, invocation_seq, step_local_seq, op)
        event = self._index.get(key)

        if event is None:
            self._replaying = False
            self._log._replay_suppress = False
            _clear_local_suppress()
            return ReplayMatch(hit=False)

        hash_mismatch = bool(request_hash and event.request_hash and event.request_hash != request_hash)

        if event.status == EventStatus.FINISHED:
            return ReplayMatch(
                hit=True,
                event=event,
                cached_response=event.response,
                status=EventStatus.FINISHED,
                hash_mismatch=hash_mismatch,
            )
        elif event.status == EventStatus.STARTED:
            return ReplayMatch(
                hit=True,
                event=event,
                cached_response=None,
                status=EventStatus.STARTED,
                hash_mismatch=hash_mismatch,
            )
        else:
            self._replaying = False
            self._log._replay_suppress = False
            _clear_local_suppress()
            return ReplayMatch(hit=False)

    @property
    def is_replaying(self) -> bool:
        return self._replaying

    def get_workflow_args(self) -> dict:
        """Extract the original workflow args from WORKFLOW_STARTED event.

        Returns the request dict from the first valid WORKFLOW_STARTED event.

        Raises:
            ValueError: if the event was logged with ``args_repr_only=True``,
                meaning the original run used non-JSON-serialisable args that
                were only captured as repr strings.  In that case programmatic
                resume (calling the workflow function directly) must be used
                instead of ``godel resume``.

        Legacy format note: if the log contains old-style ``args`` as a repr
        string (without ``args_repr_only``) a warning is emitted and the
        ``args`` / ``kwargs`` keys are normalised to empty list/dict so callers
        receive a consistent structure.
        """
        for event in self._events:
            if event.op == "WORKFLOW_STARTED" and event.status != EventStatus.INVALIDATED:
                req = event.request
                if req.get("args_repr_only"):
                    raise ValueError(
                        "This run used non-serialisable args; programmatic resume only. "
                        "Call the @workflow function directly with the original args."
                    )
                # Detect legacy format: args stored as repr string (not a list) OR
                # kwargs stored as repr string (not a dict).  Both fields are guarded
                # symmetrically: old loggers stored them together as repr, but a
                # hypothetical mixed-state log (args ok, kwargs broken) is also caught.
                if ("args" in req and not isinstance(req.get("args"), list)) or \
                   ("kwargs" in req and not isinstance(req.get("kwargs"), dict)):
                    sys.stderr.write(
                        "[godel] WARNING: audit log uses legacy repr-string args format; "
                        "original workflow args cannot be recovered for resume. "
                        "Treating as no-arg invocation.\n"
                    )
                    # Return a copy with normalised empty structures so callers
                    # (resume_cmd) can still recover source_file etc.
                    normalised = dict(req)
                    normalised["args"] = []
                    normalised["kwargs"] = {}
                    return normalised
                return req
        return {}


_mismatch_policy: MismatchPolicy | None = None  # None = interactive


def set_mismatch_policy(policy: MismatchPolicy | None):
    """Set the global mismatch policy (for CLI --on-mismatch flag)."""
    global _mismatch_policy
    _mismatch_policy = policy


def get_mismatch_policy() -> MismatchPolicy | None:
    return _mismatch_policy


# ---------------------------------------------------------------------------
# Assume-idempotent override — set by `godel resume --assume-idempotent`
# ---------------------------------------------------------------------------
# C2 fix: use a ContextVar instead of a bare module global so that concurrent
# or sequential workflow invocations within the same process each see their
# own value.  ``set_assume_idempotent_all`` sets both the ContextVar (for
# the current async context, e.g. the CLI coroutine that calls the workflow)
# and a process-level fallback sentinel so that callers that check the value
# outside an async context (e.g. legacy tests) still work.
#
# Isolation guarantee: asyncio tasks inherit the ContextVar snapshot at
# task-creation time (copy_context).  When ``resume_cmd`` sets the ContextVar
# True, only the coroutine tree rooted at that call sees True.  A subsequent
# workflow invoked in a separate asyncio.run() (new event loop ⇒ new context
# snapshot) or a test that never calls set_assume_idempotent_all starts with
# the default value False.
#
# The ``@workflow`` wrapper resets the ContextVar to False at entry (after
# capturing any caller-supplied True value) so that rewind loop re-iterations
# and nested workflow calls always start from a clean state.

from contextvars import ContextVar as _ContextVar

_assume_idempotent_cv: _ContextVar[bool] = _ContextVar("_assume_idempotent_all", default=False)


def set_assume_idempotent_all(value: bool) -> None:
    """Enable/disable the assume-idempotent override for the current context.

    When True, all STARTED-only run() and agent() entries are treated as safe
    to re-execute, regardless of per-call or per-step idempotent flags.
    Used by ``godel resume --assume-idempotent`` with a WARNING.

    The value is stored in a ContextVar so concurrent workflow invocations
    within the same process each see their own value (C2 fix).
    """
    _assume_idempotent_cv.set(value)


def get_assume_idempotent_all() -> bool:
    """Return the current assume-idempotent override for this context."""
    return _assume_idempotent_cv.get()


# ---------------------------------------------------------------------------
# Source-edit policy — detects edits to cached @step function bodies
# ---------------------------------------------------------------------------

class SourceEditPolicy(Enum):
    WARN = "warn"
    ABORT = "abort"
    IGNORE = "ignore"


_source_edit_policy: SourceEditPolicy = SourceEditPolicy.WARN


def set_source_edit_policy(policy: SourceEditPolicy):
    """Set the global source-edit policy (for CLI --on-source-edit flag)."""
    global _source_edit_policy
    _source_edit_policy = policy


def get_source_edit_policy() -> SourceEditPolicy:
    return _source_edit_policy


async def check_source_edit(
    walker: "ReplayWalker",
    *,
    step_path: tuple[str, ...],
    invocation_seq: int,
    current_source_hash: str,
    step_name: str,
) -> None:
    """Check whether a cached step's source has been edited.

    Looks up the FINISHED event for this step in the replay index.  If the
    event's recorded ``source_hash`` differs from ``current_source_hash``,
    applies the current ``SourceEditPolicy``:

    - IGNORE: do nothing.
    - WARN: print a yellow warning to stderr naming the step and event_id,
      then continue.
    - ABORT: raise ``SourceEditedError`` with a rewind suggestion.

    Called by the ``@step`` wrapper *before* emitting a new STARTED event,
    so the warning/abort fires at the earliest detectable moment.
    """
    if not current_source_hash:
        return

    key = (step_path, invocation_seq, 0, "step.enter")
    cached_event = walker._index.get(key)

    if cached_event is None:
        return  # uncached tail — no guardrail needed

    if cached_event.status != EventStatus.FINISHED:
        return  # boundary step (STARTED only) — let it re-execute normally

    cached_hash = cached_event.request.get("source_hash", "")
    if not cached_hash:
        return  # old log without source_hash — treat as "no information"

    if cached_hash == current_source_hash:
        return  # no change

    # Source was edited after this step completed.
    policy = _source_edit_policy
    event_id = cached_event.event_id

    if policy == SourceEditPolicy.IGNORE:
        return

    if policy == SourceEditPolicy.WARN:
        import sys
        try:
            import click
            msg = (
                f"[godel] WARNING: source of step '{step_name}' has changed since "
                f"it was cached (event {event_id}). Replaying cached result. "
                f"To re-execute, run: godel rewind --to {event_id}"
            )
            click.echo(click.style(msg, fg="yellow"), err=True)
        except ImportError:
            import sys as _sys
            _sys.stderr.write(
                f"[godel] WARNING: source of step '{step_name}' has changed since "
                f"it was cached (event {event_id}). Replaying cached result. "
                f"To re-execute, run: godel rewind --to {event_id}\n"
            )
        return

    # ABORT
    from godel._exceptions import SourceEditedError
    raise SourceEditedError(
        f"Source of step '{step_name}' has been edited since it was cached "
        f"(event {event_id}). Cannot safely replay the cached result.",
        step_name=step_name,
        event_id=event_id,
    )


async def handle_hash_mismatch(match: ReplayMatch, event_log) -> MismatchPolicy:
    """Handle a request_hash mismatch during replay."""
    event = match.event

    policy = _mismatch_policy
    if policy is None:
        # Non-interactive default: abort
        policy = MismatchPolicy.ABORT

    if policy == MismatchPolicy.ABORT:
        from godel._exceptions import ResumeError
        raise ResumeError(
            f"request_hash mismatch at {event.op} "
            f"(step {'/'.join(event.step_path) if event.step_path else '(root)'}, "
            f"seq {event.seq}). Code has changed since the cached run. "
            f"Use --on-mismatch=continue or --on-mismatch=invalidate to override."
        )
    elif policy == MismatchPolicy.INVALIDATE:
        _cascade_invalidate(event_log, event.event_id)

    return policy


def _cascade_invalidate(event_log, from_event_id: str):
    """Mark event and all descendants as INVALIDATED."""
    from godel._events import EventStatus

    to_invalidate = [from_event_id]
    visited = set()
    while to_invalidate:
        eid = to_invalidate.pop()
        if eid in visited:
            continue
        visited.add(eid)
        event = event_log.get_event(eid)
        if event and event.status != EventStatus.INVALIDATED:
            event.status = EventStatus.INVALIDATED
            event_log._append_event(event)
            to_invalidate.extend(event.children_ids)
