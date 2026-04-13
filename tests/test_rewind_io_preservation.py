"""Tests for print/input preservation across rewind boundaries (awl-bm6).

Verifies:
- print() before the cut point: displays text during replay
- print() after the cut point: executes fresh (new event)
- input() before the cut point: returns cached value on replay
- input() after the cut point: re-prompts the user
"""
from __future__ import annotations

import asyncio
import io
import sys

import pytest

from godel import workflow, step
from godel._context import _current_workflow
from godel._rewind import rewind
from godel.io import print as godel_print, input as godel_input


# ---------------------------------------------------------------------------
# print() tests
# ---------------------------------------------------------------------------


def test_print_before_cut_displays_during_replay(tmp_path, monkeypatch):
    """print() before the rewind cut point must display its text during replay.

    The rewind target is s2's step.enter event (the last step).  s2's step.enter
    stays FINISHED; its children (none in this case) are invalidated.  s1's
    print event is a child of s1's step.enter — NOT the rewind target — so it
    remains in the replay index.  On the second run s1 re-executes its body
    (steps always do), the print replay guard fires, and the text is written to
    stdout again without emitting a new audit event.
    """
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def s1():
            await godel_print("before cut")
            return "s1"

        @step
        async def s2():
            return "s2"

        await s1()
        await s2()

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            # Rewind to s2 (last step): s1's print is before the cut and replays.
            target = ctx.last_step_event_id(1)  # s2 step event (last completed)
            await rewind(to=target, reason="test before-cut print")

    asyncio.run(wf())

    output = buf.getvalue()
    # "before cut" should appear exactly twice: original run + replay after rewind
    assert output.count("before cut") == 2, (
        f"Expected 'before cut' exactly twice in output, got {output.count('before cut')}. "
        f"Full output: {output!r}"
    )


def test_print_after_cut_executes_fresh(tmp_path, monkeypatch):
    """print() after the rewind cut point executes fresh — it has no replay match.

    The rewind target is s2's step.enter event.  s2's step.enter stays FINISHED
    but its CHILDREN — including the print event — are INVALIDATED.  The
    ReplayWalker excludes INVALIDATED events, so the replay guard misses and the
    print runs for real, emitting a fresh audit event each time.
    """
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def s1():
            return "s1"

        @step
        async def s2():
            await godel_print("after cut")
            return "s2"

        await s1()
        await s2()

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            # Rewind to s2 (last step): s2's children (print event) are INVALIDATED.
            # On re-execution the print has no replay match → runs fresh.
            target = ctx.last_step_event_id(1)  # s2 step event (last completed)
            await rewind(to=target, reason="test after-cut print")

    asyncio.run(wf())

    output = buf.getvalue()
    # "after cut" should appear exactly twice: first run + fresh re-execution
    assert output.count("after cut") == 2, (
        f"Expected 'after cut' exactly twice in output, got {output.count('after cut')}. "
        f"Full output: {output!r}"
    )


# ---------------------------------------------------------------------------
# input() tests
# ---------------------------------------------------------------------------


def test_input_before_cut_returns_cached_value(tmp_path, monkeypatch):
    """input() before the rewind cut point returns the cached response on replay.

    The rewind target is s2's step.enter event (the last step).  s2 stays
    FINISHED but its children (none) are cleared.  s1 and all its children —
    including the input event — are NOT invalidated, so the replay walker
    returns the cached "Alice" on the second run.

    Rewind semantics: the TARGET event stays FINISHED; only its CHILDREN are
    invalidated.  s1's input is a child of s1's step.enter, which is NOT the
    rewind target, so it is preserved in the index.
    """
    monkeypatch.chdir(tmp_path)
    # stdin returns "Alice" only once — a second readline would return ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("Alice\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    rewound = {"done": False}
    input_values: list[str] = []

    @workflow
    async def wf():
        @step
        async def s1():
            val = await godel_input("name? ")
            input_values.append(val)
            return val

        @step
        async def s2():
            return "s2"

        await s1()
        await s2()

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            # Rewind to s2 (last step): s2's children are invalidated (none here),
            # but s1's input event is preserved — it is before the cut point.
            target = ctx.last_step_event_id(1)  # s2 step event (last completed)
            await rewind(to=target, reason="test before-cut input cache")

    asyncio.run(wf())

    # Prove stdin was NOT consumed a second time: after the run stdin must be exhausted
    # (the StringIO was "Alice\n"; one read consumed it; a second read would return "").
    # If the replay guard silently failed and re-read stdin, this assertion would still
    # be vacuously satisfied — but combined with the input_values check below it forms a
    # complete proof: cached value AND stdin untouched after the single read.
    assert sys.stdin.read() == "", (
        "stdin should be exhausted after the workflow — a second readline would have "
        "returned '' not 'Alice', proving the replay guard fired on the second call"
    )

    # Both calls should return "Alice" — first from stdin, second from cache
    assert len(input_values) >= 2, f"Expected at least 2 input calls, got {input_values!r}"
    assert all(v == "Alice" for v in input_values), (
        f"All input() results should be 'Alice' (cached), got {input_values!r}"
    )


def test_input_after_cut_re_prompts(tmp_path, monkeypatch):
    """input() after the rewind cut point re-prompts — it has no replay match.

    The rewind target is s2's step.enter event.  s2's step.enter stays FINISHED
    but its CHILDREN — including the input event — are INVALIDATED.  The
    ReplayWalker excludes INVALIDATED events, so on the second run the input
    replay guard misses and a fresh readline happens.

    Rewind semantics: target event stays FINISHED; its children are invalidated.
    The input event is a child of s2's step.enter, so it is invalidated.
    """
    monkeypatch.chdir(tmp_path)
    # Provide two responses: first run + post-rewind re-execution
    monkeypatch.setattr(sys, "stdin", io.StringIO("first\nsecond\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    rewound = {"done": False}
    input_values: list[str] = []

    @workflow
    async def wf():
        @step
        async def s1():
            return "s1"

        @step
        async def s2():
            val = await godel_input("answer? ")
            input_values.append(val)
            return val

        await s1()
        await s2()

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            # Rewind to s2 (last step, which has the input as a child):
            # s2's children (the input event) are INVALIDATED → fresh read on replay.
            target = ctx.last_step_event_id(1)  # s2 step event (last completed)
            await rewind(to=target, reason="test after-cut input re-prompt")

    asyncio.run(wf())

    # First call read "first", second (fresh) call read "second"
    assert len(input_values) == 2, f"Expected 2 input calls, got {input_values!r}"
    assert input_values[0] == "first", f"First input should be 'first', got {input_values[0]!r}"
    assert input_values[1] == "second", f"Second input should be 'second', got {input_values[1]!r}"


# ---------------------------------------------------------------------------
# Edge case: multiple operations in one step straddle the cut boundary
# ---------------------------------------------------------------------------


def test_print_ordering_multiple_calls_in_step(tmp_path, monkeypatch):
    """Multiple print() calls in one step must replay in the original order.

    The replay index key includes step_local_seq (auto-incremented per call).
    This test verifies that three sequential prints in one step are replayed
    in the same order (line1, line2, line3) and not scrambled.
    """
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def s1():
            await godel_print("line1")
            await godel_print("line2")
            await godel_print("line3")
            return "s1"

        @step
        async def s2():
            return "s2"

        await s1()
        await s2()

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            # Rewind to s2: s1's prints are before the cut and must replay in order.
            target = ctx.last_step_event_id(1)
            await rewind(to=target, reason="test print ordering")

    asyncio.run(wf())

    output = buf.getvalue()
    # Each line appears exactly twice (original + replay)
    assert output.count("line1") == 2, f"Expected 'line1' twice, got {output.count('line1')}. Output: {output!r}"
    assert output.count("line2") == 2, f"Expected 'line2' twice, got {output.count('line2')}. Output: {output!r}"
    assert output.count("line3") == 2, f"Expected 'line3' twice, got {output.count('line3')}. Output: {output!r}"
    # Verify ordering: each pair must appear in the same sequence (1 before 2 before 3)
    # Check that in both occurrences the order is preserved by scanning positions.
    positions = {
        label: [i for i in range(len(output)) if output[i:].startswith(label)]
        for label in ("line1", "line2", "line3")
    }
    for run_idx in range(2):
        p1 = positions["line1"][run_idx]
        p2 = positions["line2"][run_idx]
        p3 = positions["line3"][run_idx]
        assert p1 < p2 < p3, (
            f"On run {run_idx + 1}: expected line1 < line2 < line3 in output, "
            f"got positions {p1}, {p2}, {p3}. Output: {output!r}"
        )


def test_failed_step_print_reruns_fresh_after_rewind(tmp_path, monkeypatch):
    """print() inside a FAILED step is re-executed fresh after the step is rewound.

    When a step raises, its print child events are emitted but the step itself is
    FAILED.  After apply_rewind on that FAILED step.enter, its children (including
    the print event) are INVALIDATED.  INVALIDATED events are excluded from the
    replay index, so on re-execution the print runs fresh (not replayed from cache).

    This test verifies the FAILED-step rewind path so future refactors cannot
    silently break it.

    Note: FAILED steps are NOT added to _step_event_history (only FINISHED steps
    are), so we retrieve the target event_id by scanning the event log for the
    FAILED step.enter event.
    """
    from godel._events import EventStatus

    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    fail_once = {"done": False}
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def s1():
            await godel_print("from failing step")
            if not fail_once["done"]:
                fail_once["done"] = True
                raise RuntimeError("intentional failure")
            return "s1"

        try:
            await s1()
        except Exception:
            pass

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            # FAILED steps are not in _step_event_history, so scan the event log
            # for the most recent FAILED step.enter event to use as rewind target.
            failed_step_event = next(
                (
                    ev
                    for ev in reversed(ctx.event_log.all_events())
                    if ev.op == "step.enter" and ev.status == EventStatus.FAILED
                ),
                None,
            )
            assert failed_step_event is not None, "Expected a FAILED step.enter event in the log"
            # Rewind to the FAILED s1 step.enter — its children (the print event)
            # are INVALIDATED.  On re-execution the print runs fresh.
            await rewind(to=failed_step_event.event_id, reason="test failed step rewind")

    asyncio.run(wf())

    output = buf.getvalue()
    # "from failing step" should appear exactly twice:
    # - once during the initial (failing) run
    # - once fresh after rewind: FAILED step.enter returns hit=False from try_match
    #   (the else branch in ReplayWalker.try_match), so s1's body re-executes.
    #   Its print child was INVALIDATED, so the print guard finds no match → prints fresh.
    assert output.count("from failing step") == 2, (
        f"Expected 'from failing step' exactly twice, got {output.count('from failing step')}. "
        f"Output: {output!r}"
    )


def test_print_both_sides_of_cut(tmp_path, monkeypatch):
    """A step before the cut can print safely; a step after re-prints fresh.

    This is the combined scenario: one step before the cut (s1) prints during
    replay, and one step after the cut (s2) prints fresh.  Neither should be
    suppressed or duplicated incorrectly.
    """
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rewound = {"done": False}

    @workflow
    async def wf():
        @step
        async def s1():
            await godel_print("from s1")
            return "s1"

        @step
        async def s2():
            await godel_print("from s2")
            return "s2"

        await s1()
        await s2()

        ctx = _current_workflow.get()
        if not rewound["done"]:
            rewound["done"] = True
            # Rewind to s2 (last step): s2's children (print event) are INVALIDATED.
            # s1's print event (child of s1's step.enter, not the rewind target) is preserved.
            # Result: s1's print replays from cache; s2's print executes fresh.
            target = ctx.last_step_event_id(1)  # s2 step event (last completed)
            await rewind(to=target, reason="test both sides")

    asyncio.run(wf())

    output = buf.getvalue()
    # "from s1" replays during replay phase: exactly 2 occurrences (original + replay)
    assert output.count("from s1") == 2, (
        f"Expected 'from s1' exactly 2 times, got {output.count('from s1')}. Output: {output!r}"
    )
    # "from s2" re-executes fresh: exactly 2 occurrences (first run + re-run after rewind)
    assert output.count("from s2") == 2, (
        f"Expected 'from s2' exactly 2 times, got {output.count('from s2')}. Output: {output!r}"
    )
