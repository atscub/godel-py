"""E2E test: godel pause + edit + godel resume on a live run.

M7 exit criterion (b): launch a workflow as a subprocess; while blocked on a
deliberately slow @step, call ``godel pause``, wait for the PAUSED event,
edit the workflow file to change an upcoming (uncached) step, run
``godel resume``.

Assertions:
- Cached steps do not re-execute (idempotent run() count stays at 1).
- The tail step uses the edited code (response contains "EDITED_TAIL").
- The run reaches FINISHED (no FAILED events).
- WORKFLOW_STARTED appears exactly once.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).parent.parent)
FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _godel(
    args: list[str],
    cwd: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run godel synchronously with a generous timeout."""
    env = {**os.environ, "PYTHONPATH": PROJECT_ROOT}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
        env=env,
    )


def _godel_bg(
    args: list[str],
    cwd: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Launch godel as a background subprocess."""
    env = {**os.environ, "PYTHONPATH": PROJECT_ROOT}
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-m", "godel"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )


def _wait_for(pred, timeout: float, interval: float = 0.1) -> bool:
    """Poll *pred* until it returns truthy or *timeout* seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _events(runs_dir: Path, run_id: str) -> tuple[str, list[dict]]:
    """Return (run_id, events) from the runs/ directory.

    Accepts a prefix for *run_id*; resolves to the unique matching file.
    Returns events as plain dicts (last-write-wins per event_id).
    """
    matches = list(runs_dir.glob(f"{run_id}*.jsonl"))
    assert len(matches) == 1, (
        f"Expected 1 run file matching {run_id!r}, found: {[m.name for m in matches]}"
    )
    log_path = matches[0]
    full_id = log_path.stem

    raw: dict[str, dict] = {}
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        raw[ev["event_id"]] = ev

    return full_id, list(raw.values())


# ---------------------------------------------------------------------------
# Main E2E test
# ---------------------------------------------------------------------------

def test_pause_edit_resume_live_run(tmp_path):
    """Full pause → edit → resume cycle with live subprocess orchestration."""
    wf_file = tmp_path / "wf.py"
    shutil.copy(str(FIXTURES / "pause_edit_wf.py"), str(wf_file))
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    runs_dir = tmp_path / "runs"

    # C1: counter file for step_slow body-re-execution assertion.
    body_counter_file = tmp_path / "slow_body_count.txt"

    # (a) Launch workflow subprocess; it will block in step_slow.
    proc = _godel_bg(
        ["run", "--no-strict", str(wf_file)],
        cwd=str(tmp_path),
        env_extra={
            "GODEL_PAUSE_DIR": str(sync_dir),
            "GODEL_BODY_COUNTER_FILE": str(body_counter_file),
        },
    )
    try:
        # Wait for step_slow to signal it has started.
        assert _wait_for(lambda: (sync_dir / "ready").exists(), timeout=15), (
            "Timed out waiting for step_slow 'ready' signal"
        )

        # Runs directory must now exist and contain exactly one run file.
        assert _wait_for(
            lambda: runs_dir.exists() and any(runs_dir.glob("*.jsonl")),
            timeout=5,
        ), "Timed out waiting for runs/ directory to appear"

        run_file = next(runs_dir.glob("*.jsonl"))
        run_id = run_file.stem

        # (b) Request pause via godel pause.
        rp = _godel(["pause", run_id], cwd=str(tmp_path))
        assert rp.returncode == 0, f"godel pause failed: {rp.stderr}"

        # Release step_slow so it can finish; the pause fires at the next
        # @step boundary (step_tail).
        (sync_dir / "release").write_text("1")

        # (c) Wait for PAUSED event to appear in the audit log.
        def _has_paused() -> bool:
            try:
                for line in run_file.read_text().splitlines():
                    line = line.strip()
                    if line and json.loads(line).get("op") == "PAUSED":
                        return True
            except (OSError, json.JSONDecodeError):
                pass
            return False

        assert _wait_for(_has_paused, timeout=20), (
            "Timed out waiting for PAUSED event in audit log"
        )

        # Wait for the subprocess to exit.
        # W3: Use a generous timeout; the PAUSED event can take up to ~20 s to
        # arrive, and the subprocess may need a further moment to flush and exit.
        # W1: Do NOT call proc.stderr.read() here — it blocks until EOF on a
        # PIPE and can deadlock if the pipe buffer still has data.  Capture
        # stderr via communicate() only on unexpected exit codes.
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr_data = proc.communicate()
            pytest.fail(
                f"Workflow subprocess did not exit within 30 s after PAUSED; "
                f"stderr tail: {stderr_data[-500:] if stderr_data else '(empty)'}"
            )
        if rc not in (0, 2):
            _, stderr_data = proc.communicate(timeout=5)
            pytest.fail(
                f"Workflow subprocess exited with unexpected code {rc}; "
                f"stderr: {stderr_data}"
            )

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # Snapshot event IDs at pause point.
    _, ev_paused = _events(runs_dir, run_id)

    step_a_event_id = next(
        e["event_id"]
        for e in ev_paused
        if e.get("op") == "step.enter"
        and e.get("step_path") == ["step_a"]
        and e.get("status") == "FINISHED"
    )
    slow_event_id = next(
        e["event_id"]
        for e in ev_paused
        if e.get("op") == "step.enter"
        and e.get("step_path") == ["step_slow"]
        and e.get("status") == "FINISHED"
    )

    # (d) Edit the uncached tail step.
    original = wf_file.read_text()
    edited = original.replace('"ORIGINAL_TAIL"', '"EDITED_TAIL"')
    assert edited != original, "Edit did not change the file — check EDIT_TARGET comment"
    wf_file.write_text(edited)

    # (e) Resume the workflow.
    # Provide a GODEL_PAUSE_DIR for resume so step_slow's body can execute
    # (bodies always execute during replay even when cached); pre-write the
    # "release" sentinel so step_slow won't block on resume.
    resume_sync_dir = tmp_path / "sync_resume"
    resume_sync_dir.mkdir()
    (resume_sync_dir / "release").write_text("1")

    rr = _godel(
        ["resume", "--no-strict", run_id, str(wf_file)],
        cwd=str(tmp_path),
        env_extra={
            "GODEL_PAUSE_DIR": str(resume_sync_dir),
            "GODEL_BODY_COUNTER_FILE": str(body_counter_file),
        },
    )
    assert rr.returncode == 0, (
        f"godel resume failed (rc={rr.returncode}):\nstdout: {rr.stdout}\nstderr: {rr.stderr}"
    )

    # (f) Assertions on final event log.
    _, ev_final = _events(runs_dir, run_id)

    # Exactly one WORKFLOW_STARTED
    assert sum(1 for e in ev_final if e.get("op") == "WORKFLOW_STARTED") == 1, (
        "Expected exactly one WORKFLOW_STARTED event after resume"
    )

    # step_a and step_slow event_ids are preserved (they were cached).
    # Why event_ids are stable: EventLog._append_event is a no-op when
    # _replay_suppress=True (set during the replay phase of a resumed run).
    # Cached steps do not emit new JSONL lines, so their event_ids are
    # unchanged between the pre-pause snapshot and the post-resume log.
    # The _events() helper uses last-write-wins per event_id; for cached
    # steps there IS no second write, so the original line dominates.
    assert any(
        e["event_id"] == step_a_event_id and e.get("status") == "FINISHED"
        for e in ev_final
    ), "step_a FINISHED event_id changed after resume — was it replayed?"

    assert any(
        e["event_id"] == slow_event_id and e.get("status") == "FINISHED"
        for e in ev_final
    ), "step_slow FINISHED event_id changed after resume — was it replayed?"

    # C2: Prove that step_a's step.enter event_id is stable (not re-emitted).
    # Because _replay_suppress suppresses all writes for cached steps, there
    # is exactly ONE step.enter FINISHED event for step_a in the final log —
    # the original one from before the pause.  If the engine changed to
    # re-emit events under the same event_id, last-write-wins would hide the
    # duplicate; if it emitted a new event_id the count would rise above 1.
    step_a_enter_finished = [
        e for e in ev_final
        if e.get("op") == "step.enter"
        and e.get("step_path") == ["step_a"]
        and e.get("status") == "FINISHED"
    ]
    assert len(step_a_enter_finished) == 1, (
        f"Expected exactly 1 step.enter FINISHED event for step_a (event_ids are "
        f"stable under _replay_suppress), got {len(step_a_enter_finished)}"
    )

    # step_a's idempotent run() was NOT re-executed: exactly one "run" FINISHED
    # event with the step_a_ran command.
    # C2 note: this relies on _replay_suppress being True when step_a's run()
    # re-executes; run() hits the ReplayWalker cache and returns early WITHOUT
    # writing a new event (see _run.py try_match / FINISHED branch).  If the
    # engine changed the suppress behavior to re-emit same-id events, the
    # _events() last-write-wins would hide duplicates — but the step.enter
    # assertion above guards against that scenario.
    step_a_run_events = [
        e for e in ev_final
        if e.get("op") == "run"
        and "step_a_ran" in json.dumps(e.get("request", {}))
        and e.get("status") == "FINISHED"
    ]
    assert len(step_a_run_events) == 1, (
        f"Expected exactly 1 FINISHED run() event for 'step_a_ran', "
        f"got {len(step_a_run_events)} — cached run() re-executed on resume"
    )

    # C1: step_slow's body re-executed exactly once during resume replay.
    # The @step decorator calls fn() unconditionally for every invocation —
    # there is no short-circuit for cached steps at the Python-body level.
    # Only run() primitives inside the body are suppressed via ReplayWalker.
    # The counter file is written once per body invocation regardless of the
    # replay state, so it should contain 2 after the full lifecycle:
    #   invocation 1 — live first run (before pause)
    #   invocation 2 — replay during resume
    slow_body_count = int(body_counter_file.read_text().strip())
    assert slow_body_count == 2, (
        f"step_slow body should execute exactly twice (once live + once during "
        f"resume replay), but counter file contains {slow_body_count!r}. "
        f"If 1: body was skipped during replay (engine regression). "
        f"If >2: body ran extra times."
    )

    # W2: Prove that EDITED_TAIL came from the edited workflow file, not a
    # pre-existing value.  The audit log written by the pre-pause subprocess
    # must NOT contain EDITED_TAIL (it only ran with ORIGINAL_TAIL).
    # This assertion is checked against the snapshot taken at pause time.
    pre_pause_tail_events = [
        e for e in ev_paused
        if e.get("op") == "step.enter"
        and e.get("step_path") == ["step_tail"]
        and e.get("status") == "FINISHED"
    ]
    assert not pre_pause_tail_events, (
        "step_tail must NOT have a FINISHED event in the pre-pause log — "
        "it was not yet executed when the workflow paused"
    )

    # step_tail ran live with the edited code.
    tail_events = [
        e for e in ev_final
        if e.get("op") == "step.enter"
        and e.get("step_path") == ["step_tail"]
        and e.get("status") == "FINISHED"
    ]
    assert len(tail_events) == 1, (
        f"Expected exactly 1 FINISHED step.enter for step_tail, got {len(tail_events)}"
    )
    # W2: Use repr() match so we check the actual result value, not just a
    # substring that could appear for unrelated reasons.
    tail_result = (tail_events[0].get("response") or {}).get("result", "")
    assert repr("EDITED_TAIL") in tail_result, (
        f"step_tail response result does not contain repr('EDITED_TAIL'): {tail_result!r}. "
        f"The resume did not load the edited workflow file."
    )

    # No FAILED events anywhere.
    failed_events = [e for e in ev_final if e.get("status") == "FAILED"]
    assert not failed_events, (
        f"Unexpected FAILED events after resume: {failed_events}"
    )
