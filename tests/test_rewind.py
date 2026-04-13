"""E2E tests for self-directed rewind — M4 exit criterion (a).

Validates: a self-directed rewind inside a loop unwinds only the target
iteration, leaving prior iterations cached.

Key semantics clarification
----------------------------
``@step`` always re-executes its function body on every pass — there is no
body-level result cache.  The replay cache applies to *primitives* inside
steps (det.now, det.random, det.uuid4, run, print, input).

When ``rewind(to=target_event_id)`` is called:
- The target event itself stays FINISHED.
- The target's *children* (primitives recorded inside that step) are
  INVALIDATED → those primitives execute fresh on the next pass.
- Siblings (other steps at the same level as the target) are NOT invalidated:
  - Steps BEFORE the target: their primitive children remain in the replay
    index → those primitives return cached values on the next pass.
  - Steps AFTER the target: same — their primitive children are not
    invalidated → cached values on the next pass.

The loop test below verifies this by using det.uuid4() inside each step
iteration.  After rewind to iter_1, only iter_1's det.uuid4 child is
invalidated — iter_1 re-samples a new UUID while iter_0 and iter_2 return
their cached UUIDs.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from godel import workflow, step, det
from godel._context import _current_workflow
from godel._rewind import rewind


def test_rewind_in_loop_unwinds_only_target_iteration(tmp_path, monkeypatch):
    """Exit criterion (a): rewind to iter_1 invalidates only iter_1's children.

    Each step iteration records a det.uuid4() value.  After rewind to iter_1:
    - iter_0's det.uuid4 is NOT invalidated → returns the cached UUID (same value)
    - iter_1's det.uuid4 IS invalidated → samples a fresh UUID (different value)
    - iter_2's det.uuid4 is NOT invalidated → returns the cached UUID (same value)

    This confirms the graph cut is precise: only the rewind target's sub-tree
    is invalidated, not its siblings.

    Structural invariant verified via audit log:
    After the rewind completes, the JSONL file is scanned to confirm that
    iter_0's and iter_2's step event IDs are still present with FINISHED status
    (last-write-wins), and that only iter_1's child events (not iter_0's or
    iter_2's) ever carry an INVALIDATED snapshot in the raw log.  This prevents
    a buggy engine that invalidates everything but re-samples the same
    deterministic seed from passing the value-equality checks silently.
    """
    monkeypatch.chdir(tmp_path)

    # recorded_vals[idx] = list of UUID values seen for that iteration
    recorded_vals: dict[int, list[str]] = {0: [], 1: [], 2: []}
    # Capture the step event IDs from the first pass so we can verify them in the log
    step_event_ids: dict[int, str] = {}
    state = {"done": False, "pass": 0}

    @workflow
    async def wf():
        results = []

        for i in range(3):
            @step(name=f"iteration_{i}")
            async def do_work(idx=i):
                val = det.uuid4()
                recorded_vals[idx].append(val)
                return val

            r = await do_work()
            results.append(r)

        ctx = _current_workflow.get()
        state["pass"] += 1

        # Capture step event IDs on the first pass (before rewind) so that after
        # the workflow completes we can verify them in the audit log.
        if state["pass"] == 1:
            step_event_ids[0] = ctx.last_step_event_id(3)  # iter_0 (oldest)
            step_event_ids[1] = ctx.last_step_event_id(2)  # iter_1 (middle)
            step_event_ids[2] = ctx.last_step_event_id(1)  # iter_2 (most recent)

        if not state["done"]:
            state["done"] = True
            # Rewind to iter_1 (stored above) — iter_1's det.uuid4 child is invalidated
            await rewind(to=step_event_ids[1], reason="redo from iteration 1")

        return results

    asyncio.run(wf())

    # iter_0 is BEFORE the rewind target — its det.uuid4 child is NOT invalidated.
    # det.uuid4 replay guard fires on second pass → same cached value both times.
    assert len(recorded_vals[0]) == 2, (
        f"iter_0 should execute twice (step body always runs), got: {recorded_vals[0]}"
    )
    assert recorded_vals[0][0] == recorded_vals[0][1], (
        f"iter_0 det.uuid4 should be cached (same value), got: {recorded_vals[0]}"
    )

    # iter_1 IS the rewind target — its det.uuid4 child IS invalidated.
    # On the second pass, det.uuid4 has no replay match → samples a new UUID.
    assert len(recorded_vals[1]) == 2, (
        f"iter_1 should execute twice (original + after rewind), got: {recorded_vals[1]}"
    )
    assert recorded_vals[1][0] != recorded_vals[1][1], (
        f"iter_1 det.uuid4 should be fresh (different values), got: {recorded_vals[1]}"
    )

    # iter_2 is AFTER the rewind target but is a SIBLING (not a descendant) of iter_1.
    # Its det.uuid4 child is NOT invalidated → same cached value both times.
    assert len(recorded_vals[2]) == 2, (
        f"iter_2 should execute twice (step body always runs), got: {recorded_vals[2]}"
    )
    assert recorded_vals[2][0] == recorded_vals[2][1], (
        f"iter_2 det.uuid4 should be cached (same value), got: {recorded_vals[2]}"
    )

    # --- Structural audit log verification (prevents silent false-positives) ---
    # Load the JSONL and verify the event graph directly, so that a buggy engine
    # that invalidates everything but re-samples the same seed cannot pass silently.
    #
    # Note: apply_rewind clears children_ids on the rewind target before persisting,
    # so the LAST snapshot of iter_1's step event has children_ids=[].  We therefore
    # scan ALL raw lines to find iter_1's EARLIEST snapshot (which still had the
    # original children_ids populated) to learn which child events to check.

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log file found for audit verification"

    # Collect ALL raw snapshots (not deduplicated) to find:
    #   - all_children_of[event_id]: union of all children_ids ever seen for that event
    #     across every snapshot.  apply_rewind clears children_ids on the target and
    #     re-persists, so the last snapshot has children_ids=[].  We take the union
    #     across all snapshots to recover the original child links.
    #   - final_status[event_id]: status from the LAST snapshot (last-write-wins)
    #   - ever_invalidated: every event_id that ever had status="INVALIDATED"
    all_children_of: dict[str, set[str]] = {}
    final_status: dict[str, str] = {}
    ever_invalidated: set[str] = set()

    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            eid = d["event_id"]
            # Union of all children_ids ever seen (captures pre-rewind state)
            if eid not in all_children_of:
                all_children_of[eid] = set()
            all_children_of[eid].update(d.get("children_ids", []))
            # Last-write-wins for status
            final_status[eid] = d.get("status", "")
            if d.get("status") == "INVALIDATED":
                ever_invalidated.add(eid)

    # iter_0 and iter_2 step events must still be FINISHED (not INVALIDATED)
    assert step_event_ids[0] in final_status, (
        f"iter_0 step event ID {step_event_ids[0]} not found in audit log"
    )
    assert final_status[step_event_ids[0]] == "FINISHED", (
        f"iter_0 step event should remain FINISHED after rewind, "
        f"got: {final_status[step_event_ids[0]]}"
    )
    assert step_event_ids[2] in final_status, (
        f"iter_2 step event ID {step_event_ids[2]} not found in audit log"
    )
    assert final_status[step_event_ids[2]] == "FINISHED", (
        f"iter_2 step event should remain FINISHED after rewind, "
        f"got: {final_status[step_event_ids[2]]}"
    )

    # iter_0's and iter_2's primitive child events must NOT appear as INVALIDATED.
    # Use the union of all children_ids snapshots (covers original parent→child links).
    iter_0_children = all_children_of.get(step_event_ids[0], set())
    iter_2_children = all_children_of.get(step_event_ids[2], set())

    assert not (iter_0_children & ever_invalidated), (
        f"iter_0's child events were invalidated — graph cut should not touch them: "
        f"{iter_0_children & ever_invalidated}"
    )
    assert not (iter_2_children & ever_invalidated), (
        f"iter_2's child events were invalidated — graph cut should not touch them: "
        f"{iter_2_children & ever_invalidated}"
    )

    # iter_1's child event(s) MUST appear as INVALIDATED somewhere in the raw log.
    # Use the union across all snapshots — apply_rewind clears children_ids on the
    # target and re-persists, so the latest snapshot has children_ids=[].
    # The union recovers the original child links that were present before the rewind.
    iter_1_children = all_children_of.get(step_event_ids[1], set())
    assert iter_1_children, (
        f"iter_1 step event {step_event_ids[1]} never had child events (det.uuid4) "
        f"in any snapshot — this suggests the primitive was not recorded as a child."
    )
    assert iter_1_children & ever_invalidated, (
        f"iter_1's child event(s) should have been INVALIDATED after rewind, "
        f"but none appear with INVALIDATED status anywhere in the raw log. "
        f"iter_1 children (union across snapshots): {iter_1_children}, "
        f"ever_invalidated: {ever_invalidated}"
    )


def test_ctx_last_step_event_id_returns_correct_ids(tmp_path, monkeypatch):
    """ctx.last_step_event_id(n) returns the correct event for rewind targeting.

    n=1 returns the most recent step, n=2 the one before, n=3 the oldest.
    All returned IDs must be distinct non-empty strings.
    """
    monkeypatch.chdir(tmp_path)
    event_ids: list[str] = []

    @workflow
    async def wf():
        @step
        async def a():
            return 1

        @step
        async def b():
            return 2

        @step
        async def c():
            return 3

        await a()
        await b()
        await c()

        ctx = _current_workflow.get()
        event_ids.append(ctx.last_step_event_id(1))  # c — most recent
        event_ids.append(ctx.last_step_event_id(2))  # b
        event_ids.append(ctx.last_step_event_id(3))  # a — oldest

    asyncio.run(wf())

    assert len(event_ids) == 3, f"Expected 3 event IDs, got {len(event_ids)}"
    assert all(isinstance(eid, str) and eid for eid in event_ids), (
        f"All event IDs must be non-empty strings, got: {event_ids}"
    )
    # All three must be distinct
    assert len(set(event_ids)) == 3, (
        f"All three event IDs must be distinct, got: {event_ids}"
    )


def test_rewind_audit_log_contains_metadata(tmp_path, monkeypatch):
    """Audit log should contain a REWIND metadata event and at least one INVALIDATED snapshot.

    The JSONL file is append-only (last-write-wins per event_id in the loaded
    view), so INVALIDATED events may be overwritten by later FINISHED snapshots.
    We scan the raw JSONL line-by-line to find any INVALIDATED snapshot written
    during the rewind pass.
    """
    monkeypatch.chdir(tmp_path)
    state = {"done": False}

    @workflow
    async def wf():
        @step
        async def s1():
            # Include a primitive so s1 has a child event that can be invalidated
            return det.uuid4()

        @step
        async def s2():
            return det.uuid4()

        await s1()
        await s2()

        ctx = _current_workflow.get()
        if not state["done"]:
            state["done"] = True
            # Rewind to s1 (2nd most recent): s1's det.uuid4 child is invalidated
            target = ctx.last_step_event_id(2)  # s1
            await rewind(to=target, reason="test audit log")

    asyncio.run(wf())

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log file found"

    # Scan raw JSONL lines (not last-write-wins) to find any INVALIDATED snapshot
    raw_lines: list[dict] = []
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw_lines.append(json.loads(line))

    # Must have at least one REWIND event in the final snapshot
    # (last-write-wins view — REWIND events are not overwritten after apply_rewind)
    rewind_events = [e for e in raw_lines if e.get("op") == "REWIND"]
    assert len(rewind_events) >= 1, (
        f"Expected at least one REWIND event in audit log, found none. "
        f"Ops seen: {sorted({e.get('op') for e in raw_lines})}"
    )

    # At least one REWIND event must contain our reason
    reasons_found = [
        e.get("request", {}).get("reason", "") or e.get("response", {}).get("reason", "")
        for e in rewind_events
    ]
    assert any("test audit log" in r for r in reasons_found), (
        f"Expected 'test audit log' in REWIND event reason, got: {reasons_found}"
    )

    # Scan raw lines (not deduplicated) for any INVALIDATED snapshot — apply_rewind
    # emits an INVALIDATED snapshot for s1's det.uuid4 child before the second pass
    # overwrites that event with a new FINISHED snapshot.
    invalidated_lines = [e for e in raw_lines if e.get("status") == "INVALIDATED"]
    assert len(invalidated_lines) > 0, (
        f"Expected at least one INVALIDATED snapshot in the raw JSONL. "
        f"All statuses seen: {[e.get('status') for e in raw_lines]}"
    )


def test_rewind_det_values_stable_before_cut(tmp_path, monkeypatch):
    """Graph-cut precision: only the cut-target's primitives are invalidated.

    Workflow (three steps, each with a det.uuid4() primitive):
    - step A (before_cut): records a det.uuid4() value — BEFORE the rewind target
    - step B (at_cut):     records a det.uuid4() value — IS the rewind target
    - step C (after_cut):  records a det.uuid4() value — sibling AFTER the target

    Rewind to B.  On the second pass:
    (a) A's det.uuid4 is NOT invalidated → replay walker returns the cached UUID
    (b) B's det.uuid4 IS invalidated (B is the target) → new UUID sampled
    (c) C's det.uuid4 is NOT invalidated (C is a sibling of B, not a descendant)
        → replay walker returns the cached UUID

    Per the engine semantics (see module docstring): only the rewind target's
    OWN children are invalidated.  Sibling steps — both before and after the
    target — keep their primitive children in the replay index.

    This test is non-trivial because:
    - B has a primitive child (det.uuid4) that MUST be invalidated.
    - A rewind engine that never invalidates anything would fail assertion (b).
    - The structural audit-log check verifies the graph cut is precisely scoped:
      only B's child events appear with INVALIDATED status; A's and C's children
      must never be invalidated.  This catches a buggy engine that re-samples the
      same deterministic seed after mass-invalidation.
    """
    monkeypatch.chdir(tmp_path)
    state = {"done": False}
    # uuids_X[0] = first-pass value, uuids_X[1] = second-pass value
    uuids_before: list[str] = []
    uuids_at: list[str] = []
    uuids_after: list[str] = []
    # Step event IDs captured on the first pass for audit-log verification
    event_ids: dict[str, str] = {}

    @workflow
    async def wf():
        @step
        async def before_cut():
            val = det.uuid4()
            uuids_before.append(val)
            return val

        @step
        async def at_cut():
            val = det.uuid4()
            uuids_at.append(val)
            return val

        @step
        async def after_cut():
            val = det.uuid4()
            uuids_after.append(val)
            return val

        await before_cut()
        await at_cut()
        await after_cut()

        ctx = _current_workflow.get()

        if not state["done"]:
            state["done"] = True
            # Capture event IDs from the first pass before the rewind
            event_ids["before"] = ctx.last_step_event_id(3)   # before_cut (oldest)
            event_ids["at"] = ctx.last_step_event_id(2)        # at_cut (middle)
            event_ids["after"] = ctx.last_step_event_id(1)     # after_cut (most recent)
            # Rewind to at_cut — it has a det.uuid4 child AND after_cut follows it.
            # This makes the test non-trivial: zero-invalidation engines fail (b).
            await rewind(to=event_ids["at"], reason="test det stability")

    asyncio.run(wf())

    # (a) before_cut: NOT invalidated — sibling before the cut → cached UUID
    assert len(uuids_before) == 2, (
        f"before_cut should execute twice (step body always runs), got: {uuids_before}"
    )
    assert uuids_before[0] == uuids_before[1], (
        f"before_cut det.uuid4 should be cached (step before cut), "
        f"got first={uuids_before[0]!r}, second={uuids_before[1]!r}"
    )

    # (b) at_cut: IS the rewind target → det.uuid4 child invalidated → new UUID
    assert len(uuids_at) == 2, (
        f"at_cut should execute twice (original + after rewind), got: {uuids_at}"
    )
    assert uuids_at[0] != uuids_at[1], (
        f"at_cut det.uuid4 should be fresh (cut target's child invalidated), "
        f"got first={uuids_at[0]!r}, second={uuids_at[1]!r}"
    )

    # (c) after_cut: sibling of at_cut (NOT a descendant) → NOT invalidated → cached UUID
    assert len(uuids_after) == 2, (
        f"after_cut should execute twice (step body always runs), got: {uuids_after}"
    )
    assert uuids_after[0] == uuids_after[1], (
        f"after_cut det.uuid4 should be cached (sibling after cut is NOT invalidated), "
        f"got first={uuids_after[0]!r}, second={uuids_after[1]!r}"
    )

    # --- Structural audit log verification ---
    # Verify the graph cut is precisely scoped at the event-graph level, not just
    # via sampled UUID equality.  This guards against a buggy engine that
    # mass-invalidates but happens to re-sample the same seed.

    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log file found for audit verification"

    # Build two views from the raw JSONL:
    #   - all_children_of: union of all children_ids seen across every snapshot of
    #     each event_id.  apply_rewind clears children_ids on the rewind target
    #     and re-persists it, so the last snapshot of at_cut has children_ids=[].
    #     The union recovers the original child links.
    #   - final_status: last-write-wins per event_id (final state of the graph).
    #   - ever_invalidated: every event_id that ever carried status="INVALIDATED".
    all_children_of: dict[str, set[str]] = {}
    final_status: dict[str, str] = {}
    ever_invalidated: set[str] = set()

    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            eid = d["event_id"]
            if eid not in all_children_of:
                all_children_of[eid] = set()
            all_children_of[eid].update(d.get("children_ids", []))
            final_status[eid] = d.get("status", "")
            if d.get("status") == "INVALIDATED":
                ever_invalidated.add(eid)

    # before_cut step event must remain FINISHED
    assert final_status.get(event_ids["before"]) == "FINISHED", (
        f"before_cut step should remain FINISHED, "
        f"got: {final_status.get(event_ids['before'])}"
    )
    # before_cut's det.uuid4 child must NOT be invalidated
    before_children = all_children_of.get(event_ids["before"], set())
    assert not (before_children & ever_invalidated), (
        f"before_cut's child events should not be invalidated: "
        f"{before_children & ever_invalidated}"
    )

    # at_cut's det.uuid4 child MUST be invalidated (it is the cut target's child).
    # Use the union of all snapshots — the last snapshot has children_ids=[] after rewind.
    at_children = all_children_of.get(event_ids["at"], set())
    assert at_children, (
        f"at_cut step {event_ids['at']} never had child events (det.uuid4) in any snapshot. "
        f"This suggests the primitive was not recorded as a child of at_cut."
    )
    assert at_children & ever_invalidated, (
        f"at_cut's child event(s) should have been INVALIDATED (cut target's primitives), "
        f"but none appear with INVALIDATED status in the raw log. "
        f"at_cut children (union): {at_children}, ever_invalidated: {ever_invalidated}"
    )

    # after_cut's det.uuid4 child must NOT be invalidated (it is a sibling, not a descendant)
    after_children = all_children_of.get(event_ids["after"], set())
    assert not (after_children & ever_invalidated), (
        f"after_cut's child events should not be invalidated (sibling, not descendant): "
        f"{after_children & ever_invalidated}"
    )
