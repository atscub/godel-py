"""E2E test: godel repair auto-fixes schema-mismatch typo (M7 exit criterion a).

Sequence:
  1. ``godel run`` fails because step_two returns Count(count="one") where
     Count.count expects int  →  run in FAILED state.
  2. ``godel repair <run_id> --agent mock_intervention:intervene`` edits the
     workflow file (Count(count="one") → Count(count=1)) and signals resume.
  3. ``godel resume <run_id>`` completes the workflow without errors.

Assertions:
- step_one's idempotent run() fires exactly once (no duplicate run() side-effects).
- WORKFLOW_STARTED appears exactly once (no duplicate workflow launch).
- step_two and step_three each have exactly one FINISHED step.enter event.
- The final WORKFLOW_STARTED event has status FINISHED.
- No FAILED events remain in the final log.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


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
    """Run godel as a synchronous subprocess with a generous timeout."""
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


def _events(runs_dir: Path, run_id: str) -> list[dict]:
    """Load all events from the JSONL log for the given run_id prefix.

    Returns a flat list of event dicts (last-write-wins per event_id so
    FINISHED snapshots shadow STARTED entries for the same event).
    """
    matches = list(runs_dir.glob(f"{run_id}*.jsonl"))
    assert len(matches) == 1, (
        f"Expected 1 run file matching {run_id!r}, found: {[m.name for m in matches]}"
    )
    log_path = matches[0]

    seen: dict[str, dict] = {}
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        seen[ev["event_id"]] = ev

    return list(seen.values())


def _extract_run_id(stderr: str) -> str:
    """Parse the run_id from ``[godel] run <id>`` in stderr."""
    m = re.search(r"\[godel\] run ([0-9a-f-]+)", stderr)
    assert m, f"Could not find run_id in stderr:\n{stderr}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_repair_fixes_schema_typo(tmp_path):
    """Full repair cycle: run → fail → repair → resume → verify no duplicates."""
    # (a) Prepare the fixture workflow file in tmp_path.
    # wf.py is a copy of repair_schema_typo_wf.py (needed so the repair agent
    # can overwrite it in tmp_path without touching the fixtures directory).
    wf_file = tmp_path / "wf.py"
    shutil.copy(str(FIXTURES / "repair_schema_typo_wf.py"), str(wf_file))
    shutil.copy(
        str(FIXTURES / "mock_intervention.py"),
        str(tmp_path / "mock_intervention.py"),
    )

    # (b) Initial run — must fail because of the schema-mismatch typo.
    r1 = _godel(
        ["run", "--no-strict", "--no-lint", str(wf_file)],
        cwd=str(tmp_path),
    )
    assert r1.returncode != 0, (
        f"Expected non-zero exit from initial run, got 0.\n"
        f"stdout: {r1.stdout}\nstderr: {r1.stderr}"
    )

    run_id = _extract_run_id(r1.stderr)
    ev1 = _events(tmp_path / "runs", run_id)

    # Confirm the run is marked FAILED.
    assert any(e["status"] == "FAILED" for e in ev1), (
        f"Expected at least one FAILED event in initial run; events: {ev1}"
    )

    # step_one's idempotent run() fired exactly once before the crash.
    step_one_runs_pre = [
        e for e in ev1
        if e["op"] == "run"
        and "step_one_ran" in json.dumps(e.get("request", {}))
        and e["status"] == "FINISHED"
    ]
    assert len(step_one_runs_pre) == 1, (
        f"Expected exactly 1 FINISHED run() for 'step_one_ran' in initial run, "
        f"got {len(step_one_runs_pre)}"
    )

    # (c) godel repair — deterministic intervention edits the file and signals resume.
    # Add tmp_path to PYTHONPATH so mock_intervention.py is importable.
    r2 = _godel(
        ["repair", run_id, "--agent", "mock_intervention:intervene"],
        cwd=str(tmp_path),
        env_extra={"PYTHONPATH": f"{tmp_path}{os.pathsep}{PROJECT_ROOT}"},
    )
    assert r2.returncode == 0, (
        f"godel repair failed (rc={r2.returncode}):\n"
        f"stdout: {r2.stdout}\nstderr: {r2.stderr}"
    )
    # WARN-1: match on the UUID pattern so we don't depend on the exact hint message
    assert re.search(
        r"godel resume [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        r2.stderr,
    ), (
        f"Expected 'godel resume <uuid>' hint in repair stderr:\n{r2.stderr}"
    )

    # Confirm the workflow file was actually edited by the intervention.
    patched = wf_file.read_text()
    assert 'return Count(count=1)' in patched, (
        f"Expected 'return Count(count=1)' in patched workflow file, got:\n{patched}"
    )
    assert 'return Count(count="one")' not in patched, (
        "Expected the schema typo (return Count(count=\"one\")) to be removed from the workflow file"
    )

    # (d) godel resume — completes the workflow using the patched file.
    r3 = _godel(
        ["resume", "--no-strict", "--no-lint", run_id, str(wf_file)],
        cwd=str(tmp_path),
    )
    assert r3.returncode == 0, (
        f"godel resume failed (rc={r3.returncode}):\n"
        f"stdout: {r3.stdout}\nstderr: {r3.stderr}"
    )

    # (e) Assertions on final event log.
    ev_final = _events(tmp_path / "runs", run_id)

    # E1: Exactly one WORKFLOW_STARTED event (no duplicate workflow launch).
    workflow_started = [e for e in ev_final if e["op"] == "WORKFLOW_STARTED"]
    assert len(workflow_started) == 1, (
        f"Expected exactly 1 WORKFLOW_STARTED event, got {len(workflow_started)}"
    )

    # E2: step_one's idempotent run() fired exactly once total (cached on resume).
    step_one_runs = [
        e for e in ev_final
        if e["op"] == "run"
        and "step_one_ran" in json.dumps(e.get("request", {}))
        and e["status"] == "FINISHED"
    ]
    assert len(step_one_runs) == 1, (
        f"Expected exactly 1 FINISHED run() for 'step_one_ran' (cached on resume), "
        f"got {len(step_one_runs)} — cached run() re-executed"
    )

    # E3: step_one's step.enter event has exactly one distinct event_id in the
    # raw JSONL (the engine must not emit duplicate step.enter initiations).
    # Bypass _events() which deduplicates by event_id — count distinct event_ids
    # directly so that a second step.enter emission (different event_id, same
    # step_path) would be caught.  Each event_id may appear multiple times in the
    # JSONL (STARTED write + subsequent FINISHED/FAILED update — that is expected).
    run_matches = list((tmp_path / "runs").glob(f"{run_id}*.jsonl"))
    assert len(run_matches) == 1
    raw_step_one_enter_ids = {
        json.loads(line)["event_id"]
        for line in run_matches[0].read_text().splitlines()
        if line.strip()
        and json.loads(line).get("op") == "step.enter"
        and json.loads(line).get("step_path") == ["step_one"]
    }
    assert len(raw_step_one_enter_ids) == 1, (
        f"Expected exactly 1 distinct step.enter event_id for step_one in raw JSONL "
        f"(engine emitted duplicates), got {len(raw_step_one_enter_ids)}: "
        f"{raw_step_one_enter_ids}"
    )

    # E4: step_two re-executed cleanly (exactly one FINISHED step.enter after repair).
    step_two_finished = [
        e for e in ev_final
        if e["op"] == "step.enter"
        and e.get("step_path") == ["step_two"]
        and e["status"] == "FINISHED"
    ]
    assert len(step_two_finished) == 1, (
        f"Expected exactly 1 FINISHED step.enter for step_two, "
        f"got {len(step_two_finished)}"
    )

    # E5: step_three ran exactly once (no duplicate agent calls).
    step_three_finished = [
        e for e in ev_final
        if e["op"] == "step.enter"
        and e.get("step_path") == ["step_three"]
        and e["status"] == "FINISHED"
    ]
    assert len(step_three_finished) == 1, (
        f"Expected exactly 1 FINISHED step.enter for step_three, "
        f"got {len(step_three_finished)}"
    )

    # E6: The workflow completed successfully.
    ws = workflow_started[0]
    assert ws["status"] == "FINISHED", (
        f"Expected WORKFLOW_STARTED status=FINISHED, got {ws['status']!r}"
    )

    # E7: No NEW FAILED events after the repair+resume cycle.
    # The original crashed run leaves one historical FAILED event for step_two
    # in the append-only log (that's expected).  Any FAILED events that were
    # NOT present at the end of the initial run indicate a regression.
    pre_repair_event_ids = {e["event_id"] for e in ev1}
    new_failed_events = [
        e for e in ev_final
        if e["status"] == "FAILED"
        and e["event_id"] not in pre_repair_event_ids
    ]
    assert not new_failed_events, (
        f"Unexpected NEW FAILED events after repair+resume: {new_failed_events}"
    )

    # E8: step_three result matches "done:<integer>" confirming correct execution.
    step_three_result = (step_three_finished[0].get("response") or {}).get("result", "")
    assert re.search(r"done:\d+", step_three_result), (
        f"step_three result should match 'done:<int>', got: {step_three_result!r}"
    )
