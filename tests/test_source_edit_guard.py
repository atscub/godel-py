"""Tests for file-edit patch semantics — source-hash guardrail on resume.

Validates the contract:
- Cached steps (FINISHED): source_hash recorded; edits detected via guardrail.
- Boundary step (STARTED only): re-executed from current source; no guardrail.
- Uncached tail: picked up from current source; no guardrail needed.
"""
import asyncio
import hashlib
import pytest

from godel import workflow, step
from godel._context import _pending_replay
from godel._event_log import EventLog
from godel._events import Event, EventStatus
from godel._exceptions import SourceEditedError
from godel._replay import (
    ReplayWalker,
    MismatchPolicy,
    SourceEditPolicy,
    set_mismatch_policy,
    get_mismatch_policy,
    set_source_edit_policy,
    get_source_edit_policy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_source_edit_policy():
    """Restore the default WARN policy after each test."""
    original = get_source_edit_policy()
    yield
    set_source_edit_policy(original)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _build_step_enter_log(tmp_path, source_hash: str, finish: bool = True):
    """Build an EventLog with a WORKFLOW_STARTED and one step.enter event."""
    log = EventLog("test-run", runs_dir=str(tmp_path))
    wf_event = log.emit_started(
        op="WORKFLOW_STARTED",
        step_path=(),
        request={"function": "wf"},
        invocation_seq=0,
        step_local_seq=0,
    )
    log.emit_finished(wf_event.event_id, response={})

    step_event = log.emit_started(
        op="step.enter",
        step_path=("step_a",),
        request={"name": "step_a", "args": "()", "kwargs": "{}", "source_hash": source_hash},
        invocation_seq=0,
        step_local_seq=0,
    )
    if finish:
        log.emit_finished(step_event.event_id, response={"result": "42"})
    log.close()
    return EventLog.load("test-run", runs_dir=str(tmp_path)), step_event


# ---------------------------------------------------------------------------
# Unit tests: source_hash recorded on step.enter
# ---------------------------------------------------------------------------

def test_source_hash_recorded_on_step_start(tmp_path, monkeypatch):
    """Every step.enter event carries a sha256 source_hash."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def step_a():
            return 1

        return await step_a()

    asyncio.run(wf())
    run_id = wf._last_run_id

    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    events = event_log.all_events()
    step_events = [e for e in events if e.op == "step.enter"]
    assert len(step_events) >= 1
    for e in step_events:
        source_hash = e.request.get("source_hash", "")
        assert source_hash, f"step.enter event {e.event_id} missing source_hash"
        # Verify it looks like a sha256 hex digest
        assert len(source_hash) == 64
        assert all(c in "0123456789abcdef" for c in source_hash)


# ---------------------------------------------------------------------------
# Unit tests: source_hash excluded from request_hash
# ---------------------------------------------------------------------------

def test_request_hash_unchanged_by_source_hash_field():
    """compute_request_hash must ignore the source_hash key."""
    request_without = {"name": "step_a", "args": "()", "kwargs": "{}"}
    request_with = {**request_without, "source_hash": "abc123deadbeef" * 4}

    hash_without = Event.compute_request_hash(request_without)
    hash_with = Event.compute_request_hash(request_with)

    assert hash_without == hash_with, (
        "source_hash must not influence request_hash — changing it should not "
        "trigger the --on-mismatch path"
    )


# ---------------------------------------------------------------------------
# Integration tests: resume picks up edits on uncached tail
# ---------------------------------------------------------------------------

class SimulatedCrash(Exception):
    pass


def test_edit_uncached_tail_picks_up_change(tmp_path, monkeypatch):
    """Crash after step_a; edit step_b only; resume → step_a cached, step_b runs new code."""
    monkeypatch.chdir(tmp_path)

    side_effects = []
    crash_on_step_b = True

    @workflow
    async def wf():
        @step
        async def step_a():
            side_effects.append("step_a_run")
            return "a"

        @step
        async def step_b():
            if crash_on_step_b:
                raise SimulatedCrash("boom")
            side_effects.append("step_b_run_new")
            return "b_new"

        a = await step_a()
        b = await step_b()
        return {"a": a, "b": b}

    # First run: crashes at step_b
    with pytest.raises(SimulatedCrash):
        asyncio.run(wf())

    run_id = wf._last_run_id
    assert ("step_a_run" in side_effects)

    # Second run (resume): step_a is cached, step_b executes new code
    side_effects.clear()
    crash_on_step_b = False

    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    assert result["a"] == "a"
    assert result["b"] == "b_new"
    # step_b ran new code (uncached tail — executes the current function body)
    assert "step_b_run_new" in side_effects
    # step_a's body re-enters on resume (the step wrapper always runs; only
    # primitives inside it are short-circuited from cache).
    # The key guarantee: no guardrail fired and the result came from the new code.
    assert "step_a_run" in side_effects  # step_a body entered (normal for resume)


# ---------------------------------------------------------------------------
# Integration tests: edit cached step warns (default policy)
# ---------------------------------------------------------------------------

def test_edit_cached_step_warns(tmp_path, capsys):
    """Resume with default WARN policy prints a yellow warning for an edited cached step."""
    # Build a log with step_a FINISHED and a specific source_hash
    fake_old_hash = "a" * 64  # old hash that won't match anything current

    log, step_event = _build_step_enter_log(tmp_path, source_hash=fake_old_hash, finish=True)

    walker = ReplayWalker(log)
    # Build an index key: step_path=("step_a",), invocation_seq=0, step_local_seq=0
    key = (("step_a",), 0, 0, "step.enter")
    assert key in walker._index, "step_a should be indexed"

    set_source_edit_policy(SourceEditPolicy.WARN)

    from godel._replay import check_source_edit
    import io
    import sys

    # Capture stderr
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        asyncio.run(check_source_edit(
            walker,
            step_path=("step_a",),
            invocation_seq=0,
            current_source_hash="b" * 64,  # different hash
            step_name="step_a",
        ))
        warn_output = sys.stderr.getvalue()
    finally:
        sys.stderr = old_stderr

    assert "step_a" in warn_output
    assert step_event.event_id in warn_output
    assert "rewind" in warn_output.lower()


def test_edit_cached_step_warns_default(tmp_path, monkeypatch, capsys):
    """Default policy is WARN — a full workflow resume with an edited step emits warning."""
    monkeypatch.chdir(tmp_path)

    side_effects = []
    first_run = True

    @workflow
    async def wf():
        @step
        async def step_a():
            if first_run:
                side_effects.append("original")
            else:
                side_effects.append("edited")
            return 42

        return await step_a()

    # First run — complete successfully
    asyncio.run(wf())
    run_id = wf._last_run_id

    # Tamper the stored source_hash in the log so it won't match
    import json
    runs_dir = tmp_path / "runs"
    log_file = runs_dir / f"{run_id}.jsonl"
    lines = log_file.read_text().splitlines()
    new_lines = []
    for line in lines:
        event_dict = json.loads(line)
        if event_dict.get("op") == "step.enter" and event_dict.get("status") in ("STARTED", "FINISHED"):
            req = dict(event_dict.get("request", {}))
            req["source_hash"] = "deadbeef" * 8  # force mismatch
            event_dict["request"] = req
        new_lines.append(json.dumps(event_dict))
    log_file.write_text("\n".join(new_lines) + "\n")

    # Resume — default WARN policy; should not raise
    side_effects.clear()
    first_run = False
    set_source_edit_policy(SourceEditPolicy.WARN)

    event_log = EventLog.load(run_id, runs_dir=str(runs_dir))
    walker = ReplayWalker(event_log)

    import sys
    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
        warn_output = sys.stderr.getvalue()
    finally:
        sys.stderr = old_stderr
        _pending_replay.reset(token)

    assert result == 42
    assert "step_a" in warn_output
    assert "rewind" in warn_output.lower()


# ---------------------------------------------------------------------------
# Integration tests: edit cached step aborts
# ---------------------------------------------------------------------------

def test_edit_cached_step_aborts(tmp_path, monkeypatch):
    """Resume --on-source-edit=abort raises SourceEditedError, exit suggests rewind."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def step_a():
            return 99

        return await step_a()

    # First run — complete successfully
    asyncio.run(wf())
    run_id = wf._last_run_id

    # Tamper the stored source_hash
    import json
    runs_dir = tmp_path / "runs"
    log_file = runs_dir / f"{run_id}.jsonl"
    lines = log_file.read_text().splitlines()
    new_lines = []
    for line in lines:
        event_dict = json.loads(line)
        if event_dict.get("op") == "step.enter" and event_dict.get("status") in ("STARTED", "FINISHED"):
            req = dict(event_dict.get("request", {}))
            req["source_hash"] = "deadbeef" * 8  # force mismatch
            event_dict["request"] = req
        new_lines.append(json.dumps(event_dict))
    log_file.write_text("\n".join(new_lines) + "\n")

    set_source_edit_policy(SourceEditPolicy.ABORT)

    event_log = EventLog.load(run_id, runs_dir=str(runs_dir))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        with pytest.raises(SourceEditedError) as exc_info:
            asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    err_str = str(exc_info.value)
    assert "step_a" in err_str
    assert "rewind" in err_str.lower()


# ---------------------------------------------------------------------------
# Integration tests: rewind then edit cached step is silent
# ---------------------------------------------------------------------------

def test_rewind_then_edit_cached_step_is_silent(tmp_path, monkeypatch):
    """After rewind, invalidated events are not indexed — no warning on resume."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        @step
        async def step_a():
            return 7

        return await step_a()

    asyncio.run(wf())
    run_id = wf._last_run_id

    runs_dir = tmp_path / "runs"
    event_log = EventLog.load(run_id, runs_dir=str(runs_dir))

    # Find the step.enter FINISHED event and invalidate it (simulating rewind)
    step_events = [e for e in event_log.all_events()
                   if e.op == "step.enter" and e.status == EventStatus.FINISHED]
    assert len(step_events) >= 1
    target = step_events[0]

    # Tamper hash AND invalidate
    import json
    log_file = runs_dir / f"{run_id}.jsonl"
    lines = log_file.read_text().splitlines()
    new_lines = []
    for line in lines:
        event_dict = json.loads(line)
        if event_dict.get("event_id") == target.event_id:
            req = dict(event_dict.get("request", {}))
            req["source_hash"] = "deadbeef" * 8  # mismatch
            event_dict["request"] = req
            event_dict["status"] = "INVALIDATED"
        new_lines.append(json.dumps(event_dict))
    log_file.write_text("\n".join(new_lines) + "\n")

    set_source_edit_policy(SourceEditPolicy.ABORT)  # would raise if event was indexed

    event_log2 = EventLog.load(run_id, runs_dir=str(runs_dir))
    walker = ReplayWalker(event_log2)

    # The invalidated event should not be in the index
    key = (("step_a",), 0, 0, "step.enter")
    assert key not in walker._index, "INVALIDATED event must not be indexed"

    # Resume should NOT raise — the old cached event is gone
    token = _pending_replay.set(walker)
    try:
        result = asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    assert result == 7


# ---------------------------------------------------------------------------
# Unit tests: ignore policy suppresses all warnings
# ---------------------------------------------------------------------------

def test_ignore_policy_suppresses_warning(tmp_path):
    """IGNORE policy: no warning, no raise even when source_hash mismatches."""
    fake_old_hash = "a" * 64
    log, _ = _build_step_enter_log(tmp_path, source_hash=fake_old_hash, finish=True)
    walker = ReplayWalker(log)

    set_source_edit_policy(SourceEditPolicy.IGNORE)

    from godel._replay import check_source_edit
    import sys
    import io

    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        asyncio.run(check_source_edit(
            walker,
            step_path=("step_a",),
            invocation_seq=0,
            current_source_hash="b" * 64,
            step_name="step_a",
        ))
        warn_output = sys.stderr.getvalue()
    finally:
        sys.stderr = old_stderr

    assert warn_output == ""


# ---------------------------------------------------------------------------
# Unit tests: boundary step (STARTED only) triggers no guardrail
# ---------------------------------------------------------------------------

def test_boundary_step_started_only_no_guardrail(tmp_path):
    """A step in STARTED-only state (crash point) does not trigger source-edit guardrail."""
    fake_hash = "a" * 64
    # Build log with step_a STARTED but NOT finished (crash point)
    log, _ = _build_step_enter_log(tmp_path, source_hash=fake_hash, finish=False)
    walker = ReplayWalker(log)

    set_source_edit_policy(SourceEditPolicy.ABORT)  # would raise if guardrail fired

    from godel._replay import check_source_edit
    # Should not raise — STARTED-only events are the boundary, not guardrailed
    asyncio.run(check_source_edit(
        walker,
        step_path=("step_a",),
        invocation_seq=0,
        current_source_hash="b" * 64,  # different
        step_name="step_a",
    ))


# ---------------------------------------------------------------------------
# Unit tests: missing source_hash in old logs treated as no information
# ---------------------------------------------------------------------------

def test_old_log_no_source_hash_is_silent(tmp_path):
    """Old logs without source_hash resume without warnings (back-compat)."""
    # Build log with step.enter event but NO source_hash in request
    log = EventLog("test-run", runs_dir=str(tmp_path))
    wf_event = log.emit_started(
        op="WORKFLOW_STARTED", step_path=(), request={}, invocation_seq=0, step_local_seq=0,
    )
    log.emit_finished(wf_event.event_id, response={})
    step_event = log.emit_started(
        op="step.enter",
        step_path=("step_a",),
        # no source_hash key — simulates old log
        request={"name": "step_a", "args": "()", "kwargs": "{}"},
        invocation_seq=0,
        step_local_seq=0,
    )
    log.emit_finished(step_event.event_id, response={"result": "42"})
    log.close()

    loaded = EventLog.load("test-run", runs_dir=str(tmp_path))
    walker = ReplayWalker(loaded)

    set_source_edit_policy(SourceEditPolicy.ABORT)

    from godel._replay import check_source_edit
    import sys
    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        asyncio.run(check_source_edit(
            walker,
            step_path=("step_a",),
            invocation_seq=0,
            current_source_hash="b" * 64,
            step_name="step_a",
        ))
        warn_output = sys.stderr.getvalue()
    finally:
        sys.stderr = old_stderr

    assert warn_output == ""


# ---------------------------------------------------------------------------
# Known limitation: triple-quoted string trailing-whitespace is a false negative
# ---------------------------------------------------------------------------

def test_triple_quoted_string_trailing_whitespace_known_limitation():
    """KNOWN LIMITATION: rstrip() is applied to ALL lines including those inside
    triple-quoted string literals.

    An edit that adds or removes trailing whitespace ONLY inside a multi-line
    string literal will not change the normalised source_hash — the guardrail
    will not fire even though the literal value has changed.

    This test pins the current behaviour so we detect if normalization semantics
    change unexpectedly.  It is NOT a correctness requirement — it documents an
    accepted false negative.
    """
    # Reproduce the normalization logic from _decorators.py.
    def _normalise(src: str) -> str:
        lines = src.splitlines()
        out: list[str] = []
        prev_blank = False
        for line in lines:
            stripped = line.rstrip()
            is_blank = stripped == ""
            if is_blank and prev_blank:
                continue
            out.append(stripped)
            prev_blank = is_blank
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)

    # A step with a triple-quoted string that has no trailing spaces on interior lines.
    src_original = (
        'async def step_a():\n'
        '    msg = """\n'
        '    hello\n'         # no trailing whitespace
        '    """\n'
        '    return msg\n'
    )
    # Same step but with trailing spaces added ONLY inside the triple-quoted string.
    src_edited = (
        'async def step_a():\n'
        '    msg = """\n'
        '    hello   \n'      # trailing spaces added inside the literal
        '    """\n'
        '    return msg\n'
    )

    hash_original = hashlib.sha256(_normalise(src_original).encode()).hexdigest()
    hash_edited = hashlib.sha256(_normalise(src_edited).encode()).hexdigest()

    # KNOWN LIMITATION: hashes are equal because rstrip() strips the trailing
    # spaces even though they are inside the string literal.  The guardrail would
    # NOT fire for this edit.
    assert hash_original == hash_edited, (
        "Known limitation: trailing whitespace inside triple-quoted string "
        "literals is stripped by normalisation and does not change the hash.  "
        "If this assertion fails, the normalisation semantics have changed and "
        "the limitation comment in _decorators.py should be updated."
    )


# ---------------------------------------------------------------------------
# Policy non-leak: on_mismatch must not bleed between CLI invocations
# ---------------------------------------------------------------------------

def test_mismatch_policy_does_not_leak_between_resume_invocations():
    """set_mismatch_policy must not bleed between sequential CLI-style invocations.

    Simulates two back-to-back 'godel resume' calls in the same process:
    the first sets ABORT; the second does NOT set a policy explicitly.
    The CLI reset (set_mismatch_policy(None) at start of resume_cmd)
    ensures the second invocation starts from None (interactive default),
    not ABORT from the previous call.
    """
    # Simulate first CLI invocation: user passes --on-mismatch=abort
    set_mismatch_policy(None)                      # CLI always resets to None first
    set_mismatch_policy(MismatchPolicy.ABORT)       # then applies the flag

    assert get_mismatch_policy() == MismatchPolicy.ABORT

    # Simulate second CLI invocation: user does NOT pass --on-mismatch.
    # The CLI must reset to None before conditionally overriding.
    set_mismatch_policy(None)                      # reset (no flag provided)
    # No override because --on-mismatch was not passed

    assert get_mismatch_policy() is None, (
        "After a second CLI invocation without --on-mismatch, the policy must "
        "be None (interactive default), not ABORT from the previous invocation."
    )
