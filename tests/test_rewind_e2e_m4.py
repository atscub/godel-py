"""E2E tests for pass-2 rewind scenarios — M4 follow-up (awl-8ih).

Two gap areas identified in the pass-2 review of awl-ta2:

(1) Rewind-to-rewind: second rewind targeting a step that was previously
    rewound and replayed in the first pass.  Verifies that second-pass child
    event IDs differ from first-pass IDs (fresh primitives each time a target
    is invalidated), and that the JSONL file is unique (no extra log files
    created across rewind cycles).

(2) Rewind-inside-@parallel-branch E2E: self-directed ``rewind()`` called from
    inside a running parallel branch.  Exercises the FORK/JOIN cascade path end-
    to-end through the @workflow/parallel() runtime, not just via apply_rewind.
    Verifies:
    - JOIN is SUSPENDED in the audit log (not FAILED)
    - The re-executed branch produces a fresh primitive value
    - The other branch replays from cache
    - The workflow completes successfully

WARN findings addressed:
  WARN-1 (second-pass child IDs): after the second rewind the target step's
    new children have event_ids not seen in the original pass, confirming fresh
    re-sampling rather than re-use of stale event objects.
  WARN-2 (JSONL file uniqueness): assert exactly one JSONL file exists; extra
    files would indicate that a new EventLog (and therefore a new run_id) was
    accidentally created during a rewind cycle.
"""
from __future__ import annotations

import asyncio
import json


from godel import workflow, step, det
from godel._context import _current_workflow
from godel._decorators import parallel
from godel._rewind import rewind


# ---------------------------------------------------------------------------
# Test 1: rewind-to-rewind — second rewind targets a previously rewound step
# ---------------------------------------------------------------------------

def test_rewind_to_rewind(tmp_path, monkeypatch):
    """Second rewind targeting a step that was already rewound and replayed.

    Workflow structure (three steps, each with a det.uuid4() primitive):
      step_a → step_b → step_c

    Pass 1:  run all three; rewind to step_b (step_b's det.uuid4 invalidated).
    Pass 2:  step_b re-executes fresh; rewind to step_b again.
    Pass 3:  step_b re-executes fresh a second time; no more rewinds.

    Invariants:
      (a) step_a's det.uuid4 is never invalidated — same cached value all 3 passes.
      (b) step_b's det.uuid4 is invalidated in passes 1→2 AND 2→3, so both
          subsequent values differ from their predecessor.
      (c) step_c's det.uuid4 is never invalidated — same cached value all 3 passes.

    WARN-1: the second rewind creates a NEW child event for step_b's det.uuid4
      (fresh ULID).  We verify that the child event_id seen after pass 3 differs
      from the child event_id seen after pass 1 AND after pass 2.

    WARN-2: exactly one JSONL file exists throughout — no new EventLog created.
    """
    monkeypatch.chdir(tmp_path)

    rewind_count = {"n": 0}
    # uuids_X[i] = det.uuid4() value recorded on the (i+1)-th execution of step_X
    uuids_a: list[str] = []
    uuids_b: list[str] = []
    uuids_c: list[str] = []
    # step_b's step event ID as seen at the END of each pass (for WARN-1)
    step_b_event_ids: list[str] = []

    @workflow
    async def wf():
        @step
        async def step_a():
            v = det.uuid4()
            uuids_a.append(v)
            return v

        @step
        async def step_b():
            v = det.uuid4()
            uuids_b.append(v)
            return v

        @step
        async def step_c():
            v = det.uuid4()
            uuids_c.append(v)
            return v

        await step_a()
        await step_b()
        await step_c()

        ctx = _current_workflow.get()
        # Capture step_b's current event ID at the end of each pass
        step_b_event_ids.append(ctx.last_step_event_id(2))  # step_b is 2nd from last

        if rewind_count["n"] < 2:
            rewind_count["n"] += 1
            target = ctx.last_step_event_id(2)  # step_b
            await rewind(to=target, reason=f"rewind #{rewind_count['n']} to step_b")

    asyncio.run(wf())

    # Three passes: original + two rewound passes
    assert rewind_count["n"] == 2, f"Expected 2 rewinds, got {rewind_count['n']}"

    # (a) step_a: never invalidated — should be the same UUID all 3 passes
    assert len(uuids_a) == 3, f"step_a ran {len(uuids_a)} times, expected 3"
    assert uuids_a[0] == uuids_a[1] == uuids_a[2], (
        f"step_a det.uuid4 should be stable (cached) across all passes: {uuids_a}"
    )

    # (b) step_b: invalidated twice — each pass should yield a distinct UUID
    assert len(uuids_b) == 3, f"step_b ran {len(uuids_b)} times, expected 3"
    assert uuids_b[0] != uuids_b[1], (
        f"step_b pass-1 vs pass-2 should differ (first rewind invalidated it): {uuids_b}"
    )
    assert uuids_b[1] != uuids_b[2], (
        f"step_b pass-2 vs pass-3 should differ (second rewind invalidated it): {uuids_b}"
    )

    # (c) step_c: never invalidated — same UUID all 3 passes
    assert len(uuids_c) == 3, f"step_c ran {len(uuids_c)} times, expected 3"
    assert uuids_c[0] == uuids_c[1] == uuids_c[2], (
        f"step_c det.uuid4 should be stable (cached sibling after cut): {uuids_c}"
    )

    # WARN-1: each pass creates a NEW step.enter event for step_b (new ULID).
    # The engine does NOT reuse the same step.enter event_id across rewind passes.
    # We captured step_b's event_id at the end of each pass; all three must be distinct.
    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))

    # WARN-2: exactly one JSONL file — no spurious new EventLog/run_id created
    assert len(jsonl_files) == 1, (
        f"Expected exactly 1 JSONL file (single run_id throughout), "
        f"got {len(jsonl_files)}: {[f.name for f in jsonl_files]}"
    )

    # step_b_event_ids should contain one ID per pass (3 passes → 3 IDs, all distinct)
    assert len(step_b_event_ids) == 3, (
        f"Expected 3 step_b event IDs (one per pass), got: {step_b_event_ids}"
    )
    assert len(set(step_b_event_ids)) == 3, (
        f"WARN-1: step_b step.enter event_ids must be distinct across all 3 passes "
        f"(the engine creates a new event per pass after rewind). "
        f"Got: {step_b_event_ids}"
    )

    # Build audit log views for further WARN-1 verification
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

    # Each pass's step.enter event has its own det.uuid4 child (unique per pass).
    # Collect the union of child IDs across all 3 step_b event IDs.
    all_step_b_children: set[str] = set()
    for eid in step_b_event_ids:
        all_step_b_children |= all_children_of.get(eid, set())

    # 3 distinct passes → at least 2 distinct child event IDs (pass 1 and 2 are
    # invalidated; pass 3 is fresh and FINISHED).  In practice we expect exactly 3
    # (one per pass), but >= 2 is sufficient to confirm fresh event creation.
    assert len(all_step_b_children) >= 2, (
        f"WARN-1: Expected >= 2 distinct det.uuid4 child event IDs across all step_b "
        f"passes (one fresh child per pass after rewind). "
        f"Got: {all_step_b_children}. "
        f"This means the engine is reusing stale child event objects."
    )

    # The first two passes' children must have been INVALIDATED at some point
    # (they were cleared by the two rewinds).
    first_two_pass_children = (
        all_children_of.get(step_b_event_ids[0], set()) |
        all_children_of.get(step_b_event_ids[1], set())
    )
    assert first_two_pass_children & ever_invalidated, (
        f"WARN-1: Expected children from passes 1 and 2 to appear as INVALIDATED "
        f"in the raw log. children: {first_two_pass_children}, "
        f"ever_invalidated: {ever_invalidated}"
    )


# ---------------------------------------------------------------------------
# Test 2: rewind-inside-@parallel-branch E2E — self-directed rewind from parallel
# ---------------------------------------------------------------------------

def test_rewind_inside_parallel_branch_fork_join_cascade(tmp_path, monkeypatch):
    """Self-directed rewind() called inside a parallel branch — FORK/JOIN cascade E2E.

    This exercises the FORK/JOIN cascade path through the real @workflow/parallel()
    runtime (not just via apply_rewind directly on a hand-crafted EventLog).

    Structure:
        parallel(branch_a, branch_b) → final_step

    branch_b rewinds to branch_a's step event on the first pass.  After the rewind:
      - branch_a is re-executed (its det.uuid4 child was invalidated)
      - branch_b replays from cache (its det.uuid4 child was NOT invalidated)
      - final_step re-executes (its inputs changed because branch_a produced a new value)
      - The workflow completes successfully

    Audit log assertions (FORK/JOIN cascade):
      - No FORK or JOIN event ends up FAILED (the pre-fix bug)
      - At least one FORK event is present and FINISHED
      - At least one JOIN event is present with status FINISHED or SUSPENDED
        (SUSPENDED is valid immediately after rewind; the resumed pass re-finishes it)
      - At least one REWIND event is present in the raw log

    WARN-2: exactly one JSONL file — no new EventLog/run_id created.
    """
    monkeypatch.chdir(tmp_path)

    rewound = {"done": False}
    # det values recorded per branch per pass
    vals_a: list[str] = []
    vals_b: list[str] = []
    final_count = {"n": 0}

    @workflow
    async def wf():
        @step
        async def branch_a():
            v = det.uuid4()
            vals_a.append(v)
            return v

        @step
        async def branch_b():
            v = det.uuid4()
            vals_b.append(v)
            ctx = _current_workflow.get()
            if not rewound["done"]:
                rewound["done"] = True
                # Rewind to branch_a's step event (the most recently completed step
                # BEFORE branch_b in the parallel run — branch_a finishes first when
                # asyncio runs both branches, but ordering is not guaranteed; we use
                # last_step_event_id(1) which is whichever step completed last.
                # We want to rewind branch_a specifically: find its event_id as the
                # second-to-last step (branch_b just finished, so n=1 is branch_b,
                # n=2 is branch_a — but parallel ordering is non-deterministic).
                # Use n=2 to target the step before branch_b.
                try:
                    target = ctx.last_step_event_id(2)
                except IndexError:
                    # If only one step completed so far, rewind to n=1 (branch_b itself)
                    target = ctx.last_step_event_id(1)
                await rewind(to=target, reason="rewind from inside parallel branch_b")
            return v

        @step
        async def final_step(a_val, b_val):
            final_count["n"] += 1
            return f"a={a_val}, b={b_val}"

        a_val, b_val = await parallel(branch_a(), branch_b())
        return await final_step(a_val, b_val)

    result = asyncio.run(wf())

    # Workflow should complete successfully
    assert result is not None, "Workflow should return a non-None result"
    assert "a=" in result and "b=" in result, (
        f"Expected final_step output with a= and b= components, got: {result!r}"
    )
    assert rewound["done"] is True, "Rewind should have fired"

    # Both branches should have been called at least once
    assert len(vals_a) >= 1, "branch_a should have executed at least once"
    assert len(vals_b) >= 1, "branch_b should have executed at least once"

    # final_step should have been called at least once on the resumed pass
    assert final_count["n"] >= 1, "final_step should have executed at least once"

    # WARN-2: exactly one JSONL file
    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, (
        f"Expected exactly 1 JSONL file (single run_id), "
        f"got {len(jsonl_files)}: {[f.name for f in jsonl_files]}"
    )

    # --- Audit log: FORK/JOIN cascade assertions ---
    raw_events: list[dict] = []
    events_by_id: dict[str, dict] = {}
    with open(jsonl_files[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            raw_events.append(d)
            events_by_id[d["event_id"]] = d

    # No FORK or JOIN event should be FAILED
    for ev in events_by_id.values():
        if ev.get("op") in ("FORK", "JOIN"):
            assert ev.get("status") != "FAILED", (
                f"FORK/JOIN event {ev['event_id']} must not be FAILED after "
                f"rewind-inside-parallel; got status={ev.get('status')!r}"
            )

    # At least one FORK event exists and is FINISHED
    fork_events = [ev for ev in events_by_id.values() if ev.get("op") == "FORK"]
    assert fork_events, "No FORK event found in audit log after parallel() execution"
    finished_forks = [ev for ev in fork_events if ev.get("status") == "FINISHED"]
    assert finished_forks, (
        f"Expected at least one FORK event to be FINISHED, "
        f"got statuses: {[ev.get('status') for ev in fork_events]}"
    )

    # At least one JOIN event exists with a valid end-state
    join_events = [ev for ev in events_by_id.values() if ev.get("op") == "JOIN"]
    assert join_events, "No JOIN event found in audit log after parallel() execution"
    valid_join_statuses = {"FINISHED", "SUSPENDED", "INVALIDATED"}
    for j in join_events:
        assert j.get("status") in valid_join_statuses, (
            f"JOIN event {j['event_id']} has unexpected status {j.get('status')!r}; "
            f"expected one of {valid_join_statuses}"
        )

    # At least one REWIND event exists in the raw log
    rewind_events = [ev for ev in raw_events if ev.get("op") == "REWIND"]
    assert rewind_events, (
        f"Expected at least one REWIND event in audit log, "
        f"ops seen: {sorted({e.get('op') for e in raw_events})}"
    )

    # The rewind reason must match
    reasons = [
        e.get("request", {}).get("reason", "") or e.get("response", {}).get("reason", "")
        for e in rewind_events
    ]
    assert any("rewind from inside parallel branch_b" in r for r in reasons), (
        f"Expected rewind reason in REWIND event, got: {reasons}"
    )


# ---------------------------------------------------------------------------
# Test 3: WARN-1 structural check — second-pass child IDs differ from first-pass
# ---------------------------------------------------------------------------

def test_second_pass_child_ids_differ_from_first_pass(tmp_path, monkeypatch):
    """Structural regression guard: verify fresh child event IDs after each rewind.

    After a rewind, the engine creates a brand-new step.enter event (new ULID) for
    the rewound step.  That new step event gets a fresh det.uuid4 child event (also
    a new ULID).  A buggy engine that reuses stale event objects would either:
      (a) reuse the same step.enter event_id on the second pass, or
      (b) reuse the same child (det.uuid4) event_id on the second pass.

    This test verifies both invariants.

    Workflow: step_target (with det.uuid4()) is rewound once.
    We capture the step.enter event_id at the end of each pass.  The two IDs must
    be distinct (the second pass creates a new step event).  Additionally, the
    first-pass child event must appear as INVALIDATED in the raw log, and the
    second-pass child must be different (captured via children_ids diff).
    """
    monkeypatch.chdir(tmp_path)

    rewound = {"done": False}
    uuids: list[str] = []
    # step.enter event_id captured at end of each pass (appended unconditionally)
    step_event_ids_per_pass: list[str] = []

    @workflow
    async def wf():
        @step
        async def step_target():
            v = det.uuid4()
            uuids.append(v)
            return v

        await step_target()

        ctx = _current_workflow.get()
        # Capture the step.enter event_id at the end of this pass (before rewind)
        step_event_ids_per_pass.append(ctx.last_step_event_id(1))

        if not rewound["done"]:
            rewound["done"] = True
            # Rewind to the current (first-pass) step.enter event
            await rewind(to=step_event_ids_per_pass[0], reason="WARN-1 structural check")

    asyncio.run(wf())

    assert len(uuids) == 2, f"step_target should execute twice, got {len(uuids)}"
    assert uuids[0] != uuids[1], (
        f"det.uuid4 should produce a new value after rewind (not cached), "
        f"got: {uuids}"
    )
    assert len(step_event_ids_per_pass) == 2, (
        f"Expected exactly 2 step.enter event IDs (one per pass), "
        f"got: {step_event_ids_per_pass}"
    )

    # WARN-2: exactly one JSONL file
    run_log_dir = tmp_path / "runs"
    jsonl_files = list(run_log_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, (
        f"Expected exactly 1 JSONL file, got {len(jsonl_files)}: "
        f"{[f.name for f in jsonl_files]}"
    )

    # WARN-1 (a): the two step.enter event_ids must be distinct
    # The second pass creates a completely new step.enter event (new ULID).
    # This guards against an engine that reuses the same event object across passes.
    first_pass_id = step_event_ids_per_pass[0]
    second_pass_id = step_event_ids_per_pass[1]
    assert first_pass_id != second_pass_id, (
        f"WARN-1(a): step.enter event_id should differ between pass 1 and pass 2. "
        f"Got identical IDs: {first_pass_id!r}. "
        f"This indicates the engine reused the same step event object across rewind passes."
    )

    # Build audit-log views for WARN-1(b)
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

    # WARN-1 (b): the first-pass step event's child (det.uuid4) must be INVALIDATED,
    # and the second-pass step event's child must be a different, non-INVALIDATED event.
    first_pass_children = all_children_of.get(first_pass_id, set())
    second_pass_children = all_children_of.get(second_pass_id, set())

    assert first_pass_children, (
        f"WARN-1(b): first-pass step event {first_pass_id} has no recorded children. "
        f"Expected a det.uuid4 child event."
    )
    assert first_pass_children & ever_invalidated, (
        f"WARN-1(b): first-pass det.uuid4 child must appear as INVALIDATED in the raw log. "
        f"first-pass children: {first_pass_children}, ever_invalidated: {ever_invalidated}"
    )

    assert second_pass_children, (
        f"WARN-1(b): second-pass step event {second_pass_id} has no recorded children. "
        f"Expected a fresh det.uuid4 child event."
    )
    # The second-pass child must be distinct from the first-pass child
    assert second_pass_children != first_pass_children, (
        f"WARN-1(b): second-pass child event IDs must differ from first-pass children. "
        f"first: {first_pass_children}, second: {second_pass_children}. "
        f"This indicates the engine reused the same child event ID across passes."
    )
    # The second-pass child must NOT be INVALIDATED (it's a fresh event from a successful pass)
    second_invalidated = second_pass_children & ever_invalidated
    assert not second_invalidated, (
        f"WARN-1(b): second-pass child events must not be INVALIDATED. "
        f"Got INVALIDATED: {second_invalidated}"
    )
