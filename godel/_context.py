from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from godel._event_log import EventLog
    from godel._replay import ReplayWalker


@dataclass
class WorkflowContext:
    run_id: str
    step_stack: list[str] = field(default_factory=list)
    event_log: "EventLog | None" = None
    _invocation_counts: dict = field(default_factory=dict)
    _step_local_seq: dict = field(default_factory=dict)
    replay_walker: "ReplayWalker | None" = None
    source_file: str = ""
    _event_id_stack: list[str] = field(default_factory=list)
    # Ordered list of event_ids for every @step that reached FINISHED.
    # Populated by the @step decorator; used by last_step_event_id().
    # NOTE (WARN-2): when steps run inside parallel(), branches append to this
    # shared list in asyncio task-scheduling order, which is non-deterministic.
    # Do not rely on the absolute positions of parallel-branch entries — only
    # use last_step_event_id() from sequential (non-parallel) step boundaries.
    _step_event_history: list[str] = field(default_factory=list)
    # Per-branch replay suppress flag.  Mirrors event_log._replay_suppress but
    # is scoped to THIS context only (not shared across parallel branches).
    # Set to True when suppress is active at context creation / branch entry.
    # Cleared to False when THIS branch (or a child of it) exits replay mode.
    # Used by @step to correctly choose cached vs ephemeral event_id even when
    # a sibling parallel branch has already cleared the shared event_log flag.
    _local_replay_suppress: bool = False

    def last_step_event_id(self, n: int = 1) -> str:
        """Return the n-th most recent completed step event_id.

        n=1 returns the most recently completed step, n=2 returns the one
        before that, etc.

        During replay the returned ID is the *original persisted* event_id
        from the cached log, not the ephemeral event_id emitted during replay
        re-execution.  This means the returned ID is always a valid key in
        the persistent audit log regardless of whether the workflow is running
        fresh or resuming.

        Parallel-step caveat (WARN-2): steps that execute inside parallel()
        append to the shared history in asyncio task-scheduling order, which
        is non-deterministic.  Only use this method from sequential (non-
        parallel) step boundaries if you need stable positional semantics.

        Raises:
            ValueError: if n < 1
            IndexError: if fewer than n steps have reached their FINISHED
                boundary (i.e. not enough completed steps in history yet).
        """
        if n < 1:
            raise ValueError(f"n must be >= 1, got {n!r}")
        history = self._step_event_history
        if n > len(history):
            raise IndexError(
                f"Only {len(history)} step(s) have reached FINISHED in this "
                f"workflow run so far; cannot retrieve n={n}"
            )
        return history[-n]

    @property
    def current_parent_event_id(self) -> str | None:
        """Return the event_id of the innermost enclosing scope (step, fork, or workflow)."""
        return self._event_id_stack[-1] if self._event_id_stack else None

    def push_event_scope(self, event_id: str) -> None:
        self._event_id_stack.append(event_id)

    def pop_event_scope(self) -> str | None:
        return self._event_id_stack.pop() if self._event_id_stack else None

    def next_op_position(self) -> tuple[int, int]:
        """Return (invocation_seq, step_local_seq) for the next leaf operation.

        The invocation_seq is the enclosing step's count (before increment).
        The step_local_seq auto-increments so consecutive ops get distinct keys.
        """
        step_path = tuple(self.step_stack)
        # The @step decorator sets _invocation_counts[path] = old + 1,
        # so the step's own invocation_seq (old) = current value - 1.
        inv = max(0, self._invocation_counts.get(step_path, 1) - 1)
        local_seq = self._step_local_seq.get(step_path, 0)
        self._step_local_seq[step_path] = local_seq + 1
        return inv, local_seq


_current_workflow: ContextVar[WorkflowContext | None] = ContextVar(
    "godel_workflow", default=None
)
_privileged: ContextVar[bool] = ContextVar("godel_privileged", default=False)
_pending_replay: ContextVar = ContextVar("godel_pending_replay", default=None)
_on_run_start: ContextVar = ContextVar("godel_on_run_start", default=None)

# _current_stream_path tracks the nesting path of subprocess/agent launches.
# Each launch site reads this contextvar on the *launching coroutine* (or
# thread, for any future thread-pool dispatch) to compute the parent path,
# then appends a fresh ULID to form the child path.  The child path is
# stamped onto the Event by value at launch time — downstream consumers
# reading the persisted log never query the contextvar.  This is the ONLY
# contextvar used for stream-path propagation; cross-thread propagation (if
# ever needed) must use contextvars.copy_context() + ctx.run(fn).
_current_stream_path: ContextVar[list[str]] = ContextVar(
    "godel_stream_path", default=[]
)

# _current_transcript holds the active TranscriptWriter for the current workflow
# run, or None when no transcript is open.  Set by the @workflow / @step
# decorators when capture_stdout=True is active.  @step decorators that opt
# into capture_stdout read this to find the shared per-run writer.
_current_transcript: ContextVar = ContextVar(
    "godel_transcript", default=None
)


def get_event_log():
    """Retrieve the EventLog from the current workflow context."""
    ctx = _current_workflow.get()
    if ctx is None or ctx.event_log is None:
        raise RuntimeError("get_event_log() called outside a @workflow")
    return ctx.event_log
