"""Rewind primitive — targeted invalidation of completed workflow steps."""
from __future__ import annotations

from godel._context import _current_workflow
from godel._event_log import EventLog
from godel._events import EventStatus
from godel._exceptions import RewindSignal


async def rewind(*, to: str | list[str], reason: str = "") -> None:
    """Invalidate completed events and re-execute from the cut point.

    Args:
        to: Event ID or list of event IDs to rewind to. The target event(s)
            stay FINISHED; their children are invalidated.
        reason: Human-readable reason for the rewind.

    Raises:
        RewindSignal: Always. Caught by @workflow to apply the graph cut.
        RuntimeError: If called outside a @workflow.
        ValueError: If a target event_id does not exist in the event log.
    """
    ctx = _current_workflow.get()
    if ctx is None:
        raise RuntimeError("rewind() must be called inside a @workflow")

    # Normalize to list
    target_ids = [to] if isinstance(to, str) else list(to)

    # Guard against empty target list before any side-effects or signal raising.
    if not target_ids:
        raise ValueError("rewind() requires at least one target event_id; got an empty list")

    # Validate targets exist in the event log
    if ctx.event_log:
        for tid in target_ids:
            event = ctx.event_log.get_event(tid)
            if event is None:
                raise ValueError(f"rewind target event_id not found: {tid!r}")
            if event.op == "PAUSED":
                raise ValueError(
                    f"Cannot rewind to a PAUSED metadata event: {tid!r}. "
                    "PAUSED events are not replayable checkpoints."
                )
    else:
        raise ValueError(
            f"rewind target event_id not found: {target_ids[0]!r} "
            "(no event log is attached to this workflow context)"
        )

    # Record REWIND as a metadata event (not part of replayable graph).
    # Use step_local_seq=-1 and invocation_seq=-1 so ReplayWalker never
    # matches it (replay index keys use non-negative values).
    # Immediately finish the event so it is never left in STARTED limbo.
    #
    # phase="intent" distinguishes this event from the second REWIND event
    # emitted by apply_rewind() (phase="outcome").  The pair documents what
    # was requested vs. what was actually invalidated, making the audit log
    # unambiguous when two REWIND events appear per rewind operation.
    if ctx.event_log:
        rewind_event = ctx.event_log.emit_started(
            op="REWIND",
            step_path=tuple(ctx.step_stack),
            request={"targets": target_ids, "reason": reason, "phase": "intent"},
            invocation_seq=-1,
            step_local_seq=-1,
            parent_event_id=ctx.current_parent_event_id,
        )
        ctx.event_log.emit_finished(
            rewind_event.event_id,
            response={"targets": target_ids, "reason": reason, "phase": "intent"},
        )

    raise RewindSignal(target_ids, reason)


def _collect_invalidated_subtree(
    event_log: EventLog, event_id: str, result: list[str], seen: set[str]
) -> None:
    """Recursively collect event_id and all INVALIDATED descendants into result.

    Uses a caller-supplied *seen* set for cycle/diamond safety so that shared
    descendants in diamond-shaped DAGs are visited exactly once across all
    calls within the same rewind operation.

    The root node itself is included in *result* if INVALIDATED, making the
    return value of apply_rewind accurate without any manual pre-appending.
    """
    if event_id in seen:
        return
    seen.add(event_id)
    event = event_log.get_event(event_id)
    if event is None:
        return
    if event.status == EventStatus.INVALIDATED:
        result.append(event_id)
    for child_id in event.children_ids:
        _collect_invalidated_subtree(event_log, child_id, result, seen)


def _check_rewind_safety(event_log: EventLog, target_ids: list[str]) -> None:
    """Scan events that would be invalidated and refuse if any are unsafe.

    Safety table (what the rewind safety check considers safe or unsafe):

    +---------------------------------+------------------------------------------+
    | op                              | Safe?                                    |
    +=================================+==========================================+
    | agent.call / agent.*            | Safe — replay re-executes the agent      |
    +---------------------------------+------------------------------------------+
    | det.now / det.random / det.uuid4| Safe — deterministic replay re-samples   |
    +---------------------------------+------------------------------------------+
    | run(..., idempotent=True)        | Safe — caller asserts re-runnability     |
    +---------------------------------+------------------------------------------+
    | run(...) default / idempotent≠True | REFUSED — irreversible side-effects   |
    +---------------------------------+------------------------------------------+
    | print                           | Safe (already sent; replay re-emits)     |
    +---------------------------------+------------------------------------------+
    | input                           | Safe — re-asked on replay if invalidated |
    +---------------------------------+------------------------------------------+
    | step.enter / step.exit          | Safe — control-flow bookkeeping only     |
    +---------------------------------+------------------------------------------+
    | WORKFLOW_STARTED                | Safe — metadata event, no side-effects   |
    +---------------------------------+------------------------------------------+
    | FORK                            | Safe — parallel branch bookkeeping       |
    +---------------------------------+------------------------------------------+
    | JOIN                            | Safe — parallel branch bookkeeping       |
    +---------------------------------+------------------------------------------+
    | REWIND                          | Safe — metadata event, not replayed      |
    +---------------------------------+------------------------------------------+

    Only ``op="run"`` events where ``request["idempotent"] is not True`` are
    refused.  The idempotent check uses identity (``is True``) rather than
    truthiness so that a caller passing ``idempotent=1`` (integer) does not
    silently bypass the guard.

    Raises:
        RewindUnsafe: If any non-idempotent run() would be invalidated.
    """
    from godel._exceptions import RewindUnsafe

    # BFS through all children of each target to find what would be invalidated
    visited: set[str] = set()
    queue: list[str] = []

    for tid in target_ids:
        event = event_log.get_event(tid)
        if event is None or event.status == EventStatus.INVALIDATED:
            continue
        queue.extend(event.children_ids)

    while queue:
        eid = queue.pop(0)
        if eid in visited:
            continue
        visited.add(eid)
        event = event_log.get_event(eid)
        if event is None or event.status == EventStatus.INVALIDATED:
            continue

        # Check safety: non-idempotent run() is unsafe.
        # Use `is True` (identity) not truthiness so idempotent=1 is rejected.
        if event.op == "run" and event.request.get("idempotent") is not True:
            raise RewindUnsafe(
                "Cannot rewind past non-idempotent run() command",
                event_id=event.event_id,
                op=event.op,
                cmd=event.request.get("cmd"),
                step_path=event.step_path,
                source_location="",
                remediation_hint=(
                    "Mark the run() call as idempotent=True if safe to retry, "
                    "or rewind to a point before this command."
                ),
            )

        queue.extend(event.children_ids)


def _build_fork_branch_members(event_log: EventLog) -> dict[str, set[str]]:
    """Pre-pass: for each FORK that has a corresponding JOIN, collect the set of
    all event IDs reachable from that FORK (its branch subtree members).

    This must be called BEFORE the main invalidation loop because the loop clears
    children_ids on rewind targets, making post-mutation traversal incomplete.

    Returns a dict mapping fork_event_id -> set of event_ids in its branch subtree.
    """
    def _collect(el: EventLog, event_id: str, out: set[str], vis: set[str]) -> None:
        if event_id in vis:
            return
        vis.add(event_id)
        ev = el.get_event(event_id)
        if not ev:
            return
        # Stop at JOIN boundary — JOIN and everything beyond it is NOT part of
        # the branch subtree.  Including post-JOIN events in branch membership
        # would cause the JOIN-cascade pass to trigger spuriously, and is wrong
        # by definition: the branch subtree consists of events strictly between
        # FORK and JOIN, exclusive of the JOIN itself.
        if ev.op == "JOIN":
            return
        out.add(event_id)
        for child_id in ev.children_ids:
            _collect(el, child_id, out, vis)

    result: dict[str, set[str]] = {}
    for ev in event_log.all_events():
        if ev.op != "JOIN" or ev.status == EventStatus.INVALIDATED:
            continue
        fork_id = ev.request.get("fork_id")
        if not fork_id or fork_id in result:
            continue
        fork_ev = event_log.get_event(fork_id)
        if not fork_ev:
            continue
        members: set[str] = set()
        vis: set[str] = set()
        for child_id in fork_ev.children_ids:
            _collect(event_log, child_id, members, vis)
        result[fork_id] = members
    return result


def apply_rewind(
    event_log: EventLog, target_ids: list[str], reason: str = ""
) -> dict:
    """Apply a graph-cut: clear children_ids on targets and cascade-invalidate descendants.

    Also detects parallel JOINs whose FORK has a branch with invalidated events,
    transitions them to SUSPENDED, and cascade-invalidates their descendants.

    Args:
        event_log: The EventLog to operate on.
        target_ids: Event IDs to rewind to. Their children are invalidated.
        reason: Human-readable reason for the rewind.

    Returns:
        dict with keys:
          - ``invalidated_count`` (int): number of events invalidated
          - ``invalidated_ids`` (list[str]): IDs of invalidated events
          - ``already_rewound_ids`` (list[str]): target IDs that were already
            INVALIDATED and therefore skipped (no-op for those targets).
            Callers that check ``invalidated_count > 0`` should also inspect
            this field to distinguish a fully-no-op call from a partial one.

    Raises:
        RewindUnsafe: If any non-idempotent run() would be invalidated.
        ValueError: If target_ids is empty, or a target event_id does not exist
            in the event log.

    Two REWIND events in the audit log per call:
        When called from the ``rewind()`` primitive, a first REWIND event is
        emitted by ``rewind()`` itself with ``phase="intent"`` capturing the
        caller's request.  This function then emits a second REWIND event with
        ``phase="outcome"`` capturing the actual invalidated_count.  The two
        events are intentional — intent vs. outcome — and can be correlated
        because they share the same ``targets`` list.  When ``apply_rewind()``
        is called directly (e.g. from the CLI rewind command, not through the
        ``rewind()`` primitive), only the ``phase="outcome"`` event is emitted.
    """
    from godel._replay import _cascade_invalidate

    # WARN-3 guard: empty target_ids is always a caller bug — no event would
    # be invalidated and no REWIND event should be emitted.
    if not target_ids:
        raise ValueError(
            "apply_rewind() called with empty target_ids — nothing to rewind. "
            "Provide at least one target event_id."
        )

    _check_rewind_safety(event_log, target_ids)

    # Snapshot FORK branch memberships before any graph mutation (see docstring).
    fork_branch_members = _build_fork_branch_members(event_log)

    all_invalidated: list[str] = []
    already_rewound_ids: list[str] = []
    # seen tracks every node visited across ALL targets to prevent double-counting
    # shared descendants (e.g. diamond DAGs) and to give O(n) total traversal.
    seen: set[str] = set()

    for target_id in target_ids:
        event = event_log.get_event(target_id)
        if event is None:
            raise ValueError(f"rewind target event_id not found: {target_id!r}")

        if event.op == "PAUSED":
            raise ValueError(
                f"Cannot rewind to a PAUSED metadata event: {target_id!r}. "
                "PAUSED events are not replayable checkpoints."
            )

        if event.status == EventStatus.INVALIDATED:
            # Target was already invalidated by a prior rewind — skip it and
            # record it so callers can distinguish a no-op from a partial rewind.
            already_rewound_ids.append(target_id)
            continue

        # Snapshot children before clearing
        children_snapshot = list(event.children_ids)

        # Clear children_ids and re-persist the target event
        event.children_ids = []
        event_log._append_event(event)

        # Cascade-invalidate all former children first, then collect.
        # Separating the two passes means a shared descendant reached via two
        # different children (diamond DAG) is invalidated exactly once by
        # _cascade_invalidate (it has its own visited set), and then counted
        # exactly once by _collect_invalidated_subtree (via the shared seen set).
        for child_id in children_snapshot:
            _cascade_invalidate(event_log, child_id)

        for child_id in children_snapshot:
            _collect_invalidated_subtree(event_log, child_id, all_invalidated, seen)

    # --- JOIN cascade pass ---
    # For each JOIN whose FORK's pre-snapshot branch subtree contains at least one
    # "disturbed" event — either INVALIDATED or a rewind target (which stays FINISHED
    # but had its children cleared, making the branch incomplete from that point) —
    # transition JOIN to SUSPENDED and cascade-invalidate JOIN's own children.
    # Using the pre-pass snapshot avoids false negatives caused by children_ids being
    # cleared on rewind targets before this check runs.
    #
    # We include the rewind targets themselves in the "disturbed" set so that rewinding
    # to a leaf event inside a branch (which produces no invalidated descendants)
    # still correctly suspends the JOIN.
    invalidated_set = set(all_invalidated)
    # "Disturbed" = invalidated events PLUS rewind targets that are branch members
    # (a rewound target is incomplete — its future children have been erased).
    disturbed_set = invalidated_set | set(target_ids)

    for event in event_log.all_events():
        if event.op != "JOIN" or event.status in (EventStatus.INVALIDATED, EventStatus.SUSPENDED):
            continue
        fork_id = event.request.get("fork_id")
        if not fork_id:
            continue
        if not event_log.get_event(fork_id):
            continue

        branch_members = fork_branch_members.get(fork_id, set())
        if not (branch_members & disturbed_set):
            continue

        # Snapshot JOIN's children before clearing
        join_children = list(event.children_ids)

        # Transition JOIN to SUSPENDED (clears children_ids and persists)
        event_log.emit_suspended(event.event_id)

        # Cascade-invalidate JOIN's former descendants, extending all_invalidated
        for child_id in join_children:
            child = event_log.get_event(child_id)
            if child and child.status != EventStatus.INVALIDATED:
                _cascade_invalidate(event_log, child_id)
                _collect_invalidated_subtree(event_log, child_id, all_invalidated, seen)
        # Keep invalidated_set in sync for nested JOINs (multiple parallel levels)
        invalidated_set.update(all_invalidated)

    # Record a REWIND metadata event with phase="outcome".
    # When called from the rewind() primitive a prior phase="intent" event was
    # already emitted by rewind() itself; this outcome event records what was
    # actually invalidated.  The two events are intentional and distinguishable
    # by their "phase" field (see docstring).
    rewind_event = event_log.emit_started(
        op="REWIND",
        step_path=(),
        request={"targets": target_ids, "reason": reason, "phase": "outcome"},
        invocation_seq=-1,
        step_local_seq=-1,
    )
    event_log.emit_finished(
        rewind_event.event_id,
        response={
            "targets": target_ids,
            "reason": reason,
            "phase": "outcome",
            "invalidated_count": len(all_invalidated),
            "already_rewound_ids": already_rewound_ids,
        },
    )

    return {
        "invalidated_count": len(all_invalidated),
        "invalidated_ids": all_invalidated,
        "already_rewound_ids": already_rewound_ids,
    }
