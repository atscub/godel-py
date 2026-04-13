"""Tests for godel resume CLI command and workflow decorator resume path."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from godel._context import _pending_replay, _current_workflow
from godel._decorators import workflow
from godel._event_log import EventLog
from godel._replay import ReplayWalker
from godel import det

PROJECT_ROOT = str(Path(__file__).parent.parent)


SIMPLE_WORKFLOW = '''\
from godel import workflow
from godel import det

@workflow
async def wf():
    t = det.now()
    return t
'''

FAILING_WORKFLOW = '''\
from godel import workflow

@workflow
async def wf():
    raise RuntimeError("boom")
'''


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )


def test_resume_completed_run(tmp_path):
    """godel resume <id> <file> on a completed run replays everything, exits 0."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(SIMPLE_WORKFLOW)

    # First: run the workflow (--no-strict to avoid strict checks on test file)
    result = _run_godel(["run", "--no-strict", str(wf_file)], cwd=str(tmp_path))
    assert result.returncode == 0, f"Initial run failed: {result.stderr}"

    # Extract run_id from stderr — now printed at start: "[godel] run <uuid>"
    for line in result.stderr.strip().split("\n"):
        if line.startswith("[godel] run ") and "completed" not in line and "resume" not in line:
            # [godel] run <uuid>
            run_id = line.split("run ")[1].strip()
            break
    else:
        raise AssertionError(f"Could not find run_id in: {result.stderr}")

    # Resume with at least 8 chars of prefix (--no-strict to avoid strict checks)
    prefix = run_id[:8]
    result2 = _run_godel(["resume", "--no-strict", prefix, str(wf_file)], cwd=str(tmp_path))
    assert result2.returncode == 0, f"Resume failed: {result2.stderr}"
    assert "resumed run completed" in result2.stderr


def test_resume_missing_run_id(tmp_path):
    """Missing run_id gives helpful error."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(SIMPLE_WORKFLOW)

    result = _run_godel(["resume", "nonexistent", str(wf_file)], cwd=str(tmp_path))
    assert result.returncode != 0
    assert "No run" in result.stderr or "No runs" in result.stderr


def test_workflow_decorator_resume_path(tmp_path, monkeypatch):
    """Unit test: @workflow picks up _pending_replay and reuses run_id."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        t = det.now()
        return t

    # First run
    asyncio.run(wf())
    original_run_id = wf._last_run_id
    assert original_run_id is not None

    # Load and create walker
    event_log = EventLog.load(original_run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    # Set pending replay and run again
    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    # The resumed run should reuse the same run_id
    assert wf._last_run_id == original_run_id

    # Replay should NOT append duplicate events to the log
    # (deduplicate by event_id — parent re-persistence for children_ids
    # produces multiple snapshots of the same event_id, which is expected)
    log_path = tmp_path / "runs" / f"{original_run_id}.jsonl"
    reloaded = EventLog.load(original_run_id, runs_dir=str(tmp_path / "runs"))
    workflow_starts = [e for e in reloaded.all_events() if e.op == "WORKFLOW_STARTED"]
    assert len(workflow_starts) == 1  # no duplicates from replay


def test_workflow_decorator_no_replay_fresh_id(tmp_path, monkeypatch):
    """Without _pending_replay, @workflow generates a fresh run_id."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        return 42

    asyncio.run(wf())
    id1 = wf._last_run_id

    asyncio.run(wf())
    id2 = wf._last_run_id

    assert id1 != id2


def test_resume_does_not_duplicate_events(tmp_path, monkeypatch):
    """Resuming a completed run does not append duplicate events."""
    monkeypatch.chdir(tmp_path)

    @workflow
    async def wf():
        t = det.now()
        return t

    asyncio.run(wf())
    run_id = wf._last_run_id

    log_path = tmp_path / "runs" / f"{run_id}.jsonl"
    original_line_count = len(log_path.read_text().strip().split("\n"))

    event_log = EventLog.load(run_id, runs_dir=str(tmp_path / "runs"))
    walker = ReplayWalker(event_log)

    token = _pending_replay.set(walker)
    try:
        asyncio.run(wf())
    finally:
        _pending_replay.reset(token)

    new_line_count = len(log_path.read_text().strip().split("\n"))
    assert new_line_count == original_line_count
