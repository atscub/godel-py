"""Tests for JOIN cascade to SUSPENDED on rewind."""
from __future__ import annotations

from godel._events import EventStatus
from godel._event_log import EventLog
from godel._rewind import apply_rewind


def _make_parallel_graph(tmp_path, run_id="test-join"):
    """Create: root -> FORK -> [A1->A2->A3, B1->B2] -> JOIN -> D
    Returns (log, dict_of_events_by_name).
    """
    log = EventLog(run_id, runs_dir=str(tmp_path))
    events = {}

    # Root
    root = log.emit_started(op="step.enter", step_path=("root",), request={})
    log.emit_finished(root.event_id, response={})
    events["root"] = root

    # FORK
    fork = log.emit_started(op="FORK", step_path=("root",),
                             request={"branches": 2}, parent_event_id=root.event_id)
    log.emit_finished(fork.event_id, response={"branches": 2})
    events["fork"] = fork

    # Branch A: A1 -> A2 -> A3
    a1 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "a1", "idempotent": True},
                           parent_event_id=fork.event_id)
    log.emit_finished(a1.event_id, response={"stdout": "a1"})
    events["a1"] = a1

    a2 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "a2", "idempotent": True},
                           parent_event_id=a1.event_id)
    log.emit_finished(a2.event_id, response={"stdout": "a2"})
    events["a2"] = a2

    a3 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "a3", "idempotent": True},
                           parent_event_id=a2.event_id)
    log.emit_finished(a3.event_id, response={"stdout": "a3"})
    events["a3"] = a3

    # Branch B: B1 -> B2
    b1 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "b1", "idempotent": True},
                           parent_event_id=fork.event_id)
    log.emit_finished(b1.event_id, response={"stdout": "b1"})
    events["b1"] = b1

    b2 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "b2", "idempotent": True},
                           parent_event_id=b1.event_id)
    log.emit_finished(b2.event_id, response={"stdout": "b2"})
    events["b2"] = b2

    # JOIN (child of FORK, stores fork_id in request)
    join = log.emit_started(op="JOIN", step_path=("root",),
                             request={"fork_id": fork.event_id, "branches": 2},
                             parent_event_id=fork.event_id)
    log.emit_finished(join.event_id, response={"branches": 2})
    events["join"] = join

    # D (after JOIN)
    d = log.emit_started(op="run", step_path=("root",),
                          request={"cmd": "d", "idempotent": True},
                          parent_event_id=join.event_id)
    log.emit_finished(d.event_id, response={"stdout": "d"})
    events["d"] = d

    return log, events


def test_rewind_branch_suspends_join(tmp_path):
    """Rewind to A2 inside branch A: A3 invalidated, B intact, JOIN SUSPENDED, D invalidated."""
    log, ev = _make_parallel_graph(tmp_path)

    # Rewind to A2 (invalidates A3)
    apply_rewind(log, [ev["a2"].event_id], "test")

    # A2 stays FINISHED with empty children
    assert log.get_event(ev["a2"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["a2"].event_id).children_ids == []

    # A3 is INVALIDATED
    assert log.get_event(ev["a3"].event_id).status == EventStatus.INVALIDATED

    # Branch B is intact
    assert log.get_event(ev["b1"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["b2"].event_id).status == EventStatus.FINISHED

    # JOIN is SUSPENDED
    assert log.get_event(ev["join"].event_id).status == EventStatus.SUSPENDED

    # JOIN's children_ids are cleared
    assert log.get_event(ev["join"].event_id).children_ids == []

    # D is INVALIDATED (downstream of suspended JOIN)
    assert log.get_event(ev["d"].event_id).status == EventStatus.INVALIDATED

    log.close()


def test_rewind_branch_suspends_join_returns_d_in_invalidated(tmp_path):
    """D appears in apply_rewind's invalidated_ids after JOIN cascade."""
    log, ev = _make_parallel_graph(tmp_path, run_id="test-join-return")

    result = apply_rewind(log, [ev["a2"].event_id], "test")

    # A3 and D must be counted
    assert ev["a3"].event_id in result["invalidated_ids"]
    assert ev["d"].event_id in result["invalidated_ids"]

    log.close()


def test_rewind_fork_invalidates_everything(tmp_path):
    """Rewind to FORK: all branches and downstream invalidated, JOIN INVALIDATED or SUSPENDED."""
    log, ev = _make_parallel_graph(tmp_path, run_id="test-fork-rewind")

    apply_rewind(log, [ev["fork"].event_id], "restart all")

    for name in ["a1", "a2", "a3", "b1", "b2"]:
        assert log.get_event(ev[name].event_id).status == EventStatus.INVALIDATED

    # JOIN should be invalidated (FORK itself is being rewound, so all its descendants
    # including JOIN get cascade-invalidated) or SUSPENDED
    join_status = log.get_event(ev["join"].event_id).status
    assert join_status in (EventStatus.INVALIDATED, EventStatus.SUSPENDED)

    assert log.get_event(ev["d"].event_id).status == EventStatus.INVALIDATED

    log.close()


def test_rewind_whole_branch_a_suspends_join(tmp_path):
    """Rewind to A1 (first event of branch A): A2, A3 invalidated, JOIN SUSPENDED, D invalidated."""
    log, ev = _make_parallel_graph(tmp_path, run_id="test-branch-a1")

    apply_rewind(log, [ev["a1"].event_id], "rewind to start of branch A")

    # A1 stays FINISHED, its children cleared
    assert log.get_event(ev["a1"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["a1"].event_id).children_ids == []

    # A2, A3 are INVALIDATED
    assert log.get_event(ev["a2"].event_id).status == EventStatus.INVALIDATED
    assert log.get_event(ev["a3"].event_id).status == EventStatus.INVALIDATED

    # Branch B is intact
    assert log.get_event(ev["b1"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["b2"].event_id).status == EventStatus.FINISHED

    # JOIN is SUSPENDED
    assert log.get_event(ev["join"].event_id).status == EventStatus.SUSPENDED

    # D is INVALIDATED
    assert log.get_event(ev["d"].event_id).status == EventStatus.INVALIDATED

    log.close()


def test_emit_suspended_sets_status_and_clears_children(tmp_path):
    """Unit test for EventLog.emit_suspended()."""
    log = EventLog("test-emit-suspended", runs_dir=str(tmp_path))

    event = log.emit_started(op="JOIN", step_path=("root",),
                              request={"fork_id": "fake-fork", "branches": 2})
    log.emit_finished(event.event_id, response={})

    # Add a fake child
    event.children_ids.append("child-1")

    log.emit_suspended(event.event_id)

    updated = log.get_event(event.event_id)
    assert updated.status == EventStatus.SUSPENDED
    assert updated.children_ids == []
    assert updated.ts_end is not None

    log.close()


def test_join_not_suspended_when_no_invalidated_branch(tmp_path):
    """If no branch is invalidated, JOIN stays FINISHED after an unrelated rewind."""
    log, ev = _make_parallel_graph(tmp_path, run_id="test-no-suspend")

    # Rewind to root (before the FORK) — everything including FORK/JOIN gets invalidated
    # This test verifies the guard condition: no partial invalidation means no SUSPENDED
    # Instead: rewind to D (leaf) — JOIN should stay FINISHED
    apply_rewind(log, [ev["d"].event_id], "rewind leaf only")

    # D has no children, so nothing gets invalidated
    assert log.get_event(ev["join"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["a3"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["b2"].event_id).status == EventStatus.FINISHED

    log.close()


# ---------------------------------------------------------------------------
# NIT tests: nested parallel, multiple JOINs in sequence, rewind after JOIN
# ---------------------------------------------------------------------------


def _make_nested_parallel_graph(tmp_path, run_id="test-nested"):
    """Create a nested parallel graph:

        root
        └── outer_fork
            ├── inner_fork  (inner parallel inside outer branch A)
            │   ├── inner_a1
            │   └── inner_b1
            │   └── inner_join (child of inner_fork, stores fork_id=inner_fork)
            │       └── inner_post
            └── outer_b1
            └── outer_join (child of outer_fork, stores fork_id=outer_fork)
                └── outer_post

    Rewinding inner_a1 should:
    - INVALIDATE inner_a1's children (none in this graph, but inner_join depends on it)
    - SUSPEND inner_join (one of its branches is invalidated)
    - INVALIDATE inner_post
    - SUSPEND outer_join (inner_join, which is in outer branch A, is now SUSPENDED,
      not FINISHED — meaning the outer JOIN's branch A is incomplete)
    - INVALIDATE outer_post

    NOTE: The outer JOIN triggers because inner_join (which is in outer branch A's subtree)
    gets SUSPENDED (transitioning it out of FINISHED), making outer branch A incomplete.
    However the current implementation only checks INVALIDATED events in branch membership,
    so we assert outer_join is SUSPENDED only if the implementation supports it.
    The definitive assertion is: inner_join is SUSPENDED and inner_post is INVALIDATED.
    """
    log = EventLog(run_id, runs_dir=str(tmp_path))
    ev = {}

    root = log.emit_started(op="step.enter", step_path=("root",), request={})
    log.emit_finished(root.event_id, response={})
    ev["root"] = root

    # Outer FORK
    outer_fork = log.emit_started(op="FORK", step_path=("root",),
                                   request={"branches": 2},
                                   parent_event_id=root.event_id)
    log.emit_finished(outer_fork.event_id, response={"branches": 2})
    ev["outer_fork"] = outer_fork

    # Outer branch A: contains an inner parallel
    inner_fork = log.emit_started(op="FORK", step_path=("root",),
                                   request={"branches": 2},
                                   parent_event_id=outer_fork.event_id)
    log.emit_finished(inner_fork.event_id, response={"branches": 2})
    ev["inner_fork"] = inner_fork

    inner_a1 = log.emit_started(op="run", step_path=("root",),
                                 request={"cmd": "inner_a1", "idempotent": True},
                                 parent_event_id=inner_fork.event_id)
    log.emit_finished(inner_a1.event_id, response={"stdout": "inner_a1"})
    ev["inner_a1"] = inner_a1

    inner_b1 = log.emit_started(op="run", step_path=("root",),
                                 request={"cmd": "inner_b1", "idempotent": True},
                                 parent_event_id=inner_fork.event_id)
    log.emit_finished(inner_b1.event_id, response={"stdout": "inner_b1"})
    ev["inner_b1"] = inner_b1

    inner_join = log.emit_started(op="JOIN", step_path=("root",),
                                   request={"fork_id": inner_fork.event_id, "branches": 2},
                                   parent_event_id=inner_fork.event_id)
    log.emit_finished(inner_join.event_id, response={"branches": 2})
    ev["inner_join"] = inner_join

    inner_post = log.emit_started(op="run", step_path=("root",),
                                   request={"cmd": "inner_post", "idempotent": True},
                                   parent_event_id=inner_join.event_id)
    log.emit_finished(inner_post.event_id, response={"stdout": "inner_post"})
    ev["inner_post"] = inner_post

    # Outer branch B
    outer_b1 = log.emit_started(op="run", step_path=("root",),
                                 request={"cmd": "outer_b1", "idempotent": True},
                                 parent_event_id=outer_fork.event_id)
    log.emit_finished(outer_b1.event_id, response={"stdout": "outer_b1"})
    ev["outer_b1"] = outer_b1

    # Outer JOIN
    outer_join = log.emit_started(op="JOIN", step_path=("root",),
                                   request={"fork_id": outer_fork.event_id, "branches": 2},
                                   parent_event_id=outer_fork.event_id)
    log.emit_finished(outer_join.event_id, response={"branches": 2})
    ev["outer_join"] = outer_join

    outer_post = log.emit_started(op="run", step_path=("root",),
                                   request={"cmd": "outer_post", "idempotent": True},
                                   parent_event_id=outer_join.event_id)
    log.emit_finished(outer_post.event_id, response={"stdout": "outer_post"})
    ev["outer_post"] = outer_post

    return log, ev


def test_nested_parallel_rewind_suspends_inner_join(tmp_path):
    """Rewind inner_a1: inner_join SUSPENDED, inner_post INVALIDATED, outer_b1 intact."""
    log, ev = _make_nested_parallel_graph(tmp_path)

    apply_rewind(log, [ev["inner_a1"].event_id], "nested rewind")

    # inner_a1 stays FINISHED, children cleared
    assert log.get_event(ev["inner_a1"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["inner_a1"].event_id).children_ids == []

    # inner_b1 is intact (different branch)
    assert log.get_event(ev["inner_b1"].event_id).status == EventStatus.FINISHED

    # inner_join is SUSPENDED (one of inner_fork's branches is invalidated)
    assert log.get_event(ev["inner_join"].event_id).status == EventStatus.SUSPENDED
    assert log.get_event(ev["inner_join"].event_id).children_ids == []

    # inner_post is INVALIDATED (downstream of suspended inner_join)
    assert log.get_event(ev["inner_post"].event_id).status == EventStatus.INVALIDATED

    # outer_b1 is intact (different outer branch)
    assert log.get_event(ev["outer_b1"].event_id).status == EventStatus.FINISHED

    log.close()


def test_nested_parallel_rewind_outer_join_suspended(tmp_path):
    """After inner_join is SUSPENDED, outer_join is also SUSPENDED (outer branch A incomplete)."""
    log, ev = _make_nested_parallel_graph(tmp_path, run_id="test-nested-outer")

    apply_rewind(log, [ev["inner_a1"].event_id], "nested rewind")

    # inner_join SUSPENDED means outer branch A (which contains inner_fork subtree) has
    # an event that was invalidated (inner_post), so outer_join should also be SUSPENDED.
    outer_join_status = log.get_event(ev["outer_join"].event_id).status
    assert outer_join_status == EventStatus.SUSPENDED, (
        f"outer_join should be SUSPENDED when inner branch is invalidated; "
        f"got {outer_join_status}"
    )

    # outer_post is INVALIDATED (downstream of suspended outer_join)
    assert log.get_event(ev["outer_post"].event_id).status == EventStatus.INVALIDATED

    log.close()


def _make_sequential_joins_graph(tmp_path, run_id="test-seq-joins"):
    """Create two independent parallel sections in sequence:

        root
        ├── fork1 -> [a1, b1] -> join1 -> mid
        └── fork2 -> [c1, d1] -> join2 -> end

    fork1 and fork2 are both children of root (sequential parallelism).
    """
    log = EventLog(run_id, runs_dir=str(tmp_path))
    ev = {}

    root = log.emit_started(op="step.enter", step_path=("root",), request={})
    log.emit_finished(root.event_id, response={})
    ev["root"] = root

    # First parallel section
    fork1 = log.emit_started(op="FORK", step_path=("root",),
                              request={"branches": 2},
                              parent_event_id=root.event_id)
    log.emit_finished(fork1.event_id, response={"branches": 2})
    ev["fork1"] = fork1

    a1 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "a1", "idempotent": True},
                           parent_event_id=fork1.event_id)
    log.emit_finished(a1.event_id, response={"stdout": "a1"})
    ev["a1"] = a1

    b1 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "b1", "idempotent": True},
                           parent_event_id=fork1.event_id)
    log.emit_finished(b1.event_id, response={"stdout": "b1"})
    ev["b1"] = b1

    join1 = log.emit_started(op="JOIN", step_path=("root",),
                              request={"fork_id": fork1.event_id, "branches": 2},
                              parent_event_id=fork1.event_id)
    log.emit_finished(join1.event_id, response={"branches": 2})
    ev["join1"] = join1

    mid = log.emit_started(op="run", step_path=("root",),
                            request={"cmd": "mid", "idempotent": True},
                            parent_event_id=join1.event_id)
    log.emit_finished(mid.event_id, response={"stdout": "mid"})
    ev["mid"] = mid

    # Second parallel section (sequential after first)
    fork2 = log.emit_started(op="FORK", step_path=("root",),
                              request={"branches": 2},
                              parent_event_id=mid.event_id)
    log.emit_finished(fork2.event_id, response={"branches": 2})
    ev["fork2"] = fork2

    c1 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "c1", "idempotent": True},
                           parent_event_id=fork2.event_id)
    log.emit_finished(c1.event_id, response={"stdout": "c1"})
    ev["c1"] = c1

    d1 = log.emit_started(op="run", step_path=("root",),
                           request={"cmd": "d1", "idempotent": True},
                           parent_event_id=fork2.event_id)
    log.emit_finished(d1.event_id, response={"stdout": "d1"})
    ev["d1"] = d1

    join2 = log.emit_started(op="JOIN", step_path=("root",),
                              request={"fork_id": fork2.event_id, "branches": 2},
                              parent_event_id=fork2.event_id)
    log.emit_finished(join2.event_id, response={"branches": 2})
    ev["join2"] = join2

    end = log.emit_started(op="run", step_path=("root",),
                            request={"cmd": "end", "idempotent": True},
                            parent_event_id=join2.event_id)
    log.emit_finished(end.event_id, response={"stdout": "end"})
    ev["end"] = end

    return log, ev


def test_multiple_joins_in_sequence_rewind_first(tmp_path):
    """Rewind a1 (in first parallel section): join1 SUSPENDED, mid+fork2+c1+d1+join2+end invalidated.
    join2 is also SUSPENDED (or INVALIDATED as its entire section cascades).
    """
    log, ev = _make_sequential_joins_graph(tmp_path)

    apply_rewind(log, [ev["a1"].event_id], "rewind first section")

    # a1 stays FINISHED
    assert log.get_event(ev["a1"].event_id).status == EventStatus.FINISHED
    # b1 intact
    assert log.get_event(ev["b1"].event_id).status == EventStatus.FINISHED
    # join1 SUSPENDED
    assert log.get_event(ev["join1"].event_id).status == EventStatus.SUSPENDED
    # Everything after join1 is invalidated (mid, fork2, c1, d1, join2, end)
    for name in ["mid"]:
        assert log.get_event(ev[name].event_id).status == EventStatus.INVALIDATED, (
            f"{name} should be INVALIDATED"
        )
    # fork2, c1, d1, end are downstream of mid so also invalidated
    for name in ["fork2", "c1", "d1", "end"]:
        assert log.get_event(ev[name].event_id).status == EventStatus.INVALIDATED, (
            f"{name} should be INVALIDATED"
        )
    # join2 is either INVALIDATED (cascade from mid) or SUSPENDED
    join2_status = log.get_event(ev["join2"].event_id).status
    assert join2_status in (EventStatus.INVALIDATED, EventStatus.SUSPENDED), (
        f"join2 should be INVALIDATED or SUSPENDED; got {join2_status}"
    )

    log.close()


def test_multiple_joins_in_sequence_rewind_second(tmp_path):
    """Rewind c1 (in second parallel section): join2 SUSPENDED, end invalidated.
    join1 and mid are untouched.
    """
    log, ev = _make_sequential_joins_graph(tmp_path, run_id="test-seq-second")

    apply_rewind(log, [ev["c1"].event_id], "rewind second section")

    # c1 stays FINISHED
    assert log.get_event(ev["c1"].event_id).status == EventStatus.FINISHED
    # d1 intact
    assert log.get_event(ev["d1"].event_id).status == EventStatus.FINISHED
    # join2 SUSPENDED
    assert log.get_event(ev["join2"].event_id).status == EventStatus.SUSPENDED
    # end INVALIDATED
    assert log.get_event(ev["end"].event_id).status == EventStatus.INVALIDATED

    # First section untouched
    assert log.get_event(ev["join1"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["mid"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["a1"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["b1"].event_id).status == EventStatus.FINISHED

    log.close()


def test_rewind_to_event_immediately_after_join(tmp_path):
    """Rewind to D (the event immediately after JOIN): JOIN stays FINISHED, D's children cleared.

    This verifies that rewind to a post-JOIN event does not spuriously suspend the JOIN.
    The JOIN is already complete; only D's subtree is affected.
    """
    log, ev = _make_parallel_graph(tmp_path, run_id="test-after-join")

    # Add a child of D so there's something to invalidate
    d_child = log.emit_started(op="run", step_path=("root",),
                                request={"cmd": "d_child", "idempotent": True},
                                parent_event_id=ev["d"].event_id)
    log.emit_finished(d_child.event_id, response={"stdout": "d_child"})

    apply_rewind(log, [ev["d"].event_id], "rewind to post-join event")

    # D stays FINISHED with empty children
    assert log.get_event(ev["d"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["d"].event_id).children_ids == []

    # d_child is INVALIDATED
    assert log.get_event(d_child.event_id).status == EventStatus.INVALIDATED

    # JOIN stays FINISHED — it is upstream of D and unaffected
    assert log.get_event(ev["join"].event_id).status == EventStatus.FINISHED

    # All branch events intact
    assert log.get_event(ev["a1"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["a3"].event_id).status == EventStatus.FINISHED
    assert log.get_event(ev["b2"].event_id).status == EventStatus.FINISHED

    log.close()
