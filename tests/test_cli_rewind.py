"""Tests for godel rewind CLI command."""
import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = str(Path(__file__).parent.parent)


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )


def _create_test_run(tmp_path):
    """Create a minimal test JSONL file with a chain of events."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    run_id = "test-rewind-run"
    events = []

    # WORKFLOW_STARTED
    events.append({
        "event_id": "evt-ws",
        "run_id": run_id,
        "seq": 0,
        "children_ids": ["evt-a"],
        "step_path": [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": "WORKFLOW_STARTED",
        "request_hash": "",
        "request": {"function": "test"},
        "response": None,
        "status": "FINISHED",
        "ts_start": "2024-01-01T00:00:00+00:00",
        "ts_end": "2024-01-01T00:00:01+00:00",
    })

    # Event A — idempotent run
    events.append({
        "event_id": "evt-a",
        "run_id": run_id,
        "seq": 1,
        "children_ids": ["evt-b"],
        "step_path": ["s"],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": "run",
        "request_hash": "",
        "request": {"cmd": "echo a", "idempotent": True},
        "response": {"stdout": "a"},
        "status": "FINISHED",
        "ts_start": "2024-01-01T00:00:01+00:00",
        "ts_end": "2024-01-01T00:00:02+00:00",
    })

    # Event B — idempotent run
    events.append({
        "event_id": "evt-b",
        "run_id": run_id,
        "seq": 2,
        "children_ids": [],
        "step_path": ["s"],
        "invocation_seq": 0,
        "step_local_seq": 1,
        "op": "run",
        "request_hash": "",
        "request": {"cmd": "echo b", "idempotent": True},
        "response": {"stdout": "b"},
        "status": "FINISHED",
        "ts_start": "2024-01-01T00:00:02+00:00",
        "ts_end": "2024-01-01T00:00:03+00:00",
    })

    log_file = runs_dir / f"{run_id}.jsonl"
    with open(log_file, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    return run_id


def _create_non_idempotent_run(tmp_path):
    """Create a test JSONL with a non-idempotent run event as a child."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(exist_ok=True)

    run_id = "test-non-idempotent-run"
    events = []

    # WORKFLOW_STARTED
    events.append({
        "event_id": "evt-start",
        "run_id": run_id,
        "seq": 0,
        "children_ids": ["evt-unsafe"],
        "step_path": [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": "WORKFLOW_STARTED",
        "request_hash": "",
        "request": {"function": "test"},
        "response": None,
        "status": "FINISHED",
        "ts_start": "2024-01-01T00:00:00+00:00",
        "ts_end": "2024-01-01T00:00:01+00:00",
    })

    # Non-idempotent run event as child of WORKFLOW_STARTED
    events.append({
        "event_id": "evt-unsafe",
        "run_id": run_id,
        "seq": 1,
        "children_ids": [],
        "step_path": ["s"],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": "run",
        "request_hash": "",
        "request": {"cmd": "rm -rf /tmp/something", "idempotent": False},
        "response": {"stdout": ""},
        "status": "FINISHED",
        "ts_start": "2024-01-01T00:00:01+00:00",
        "ts_end": "2024-01-01T00:00:02+00:00",
    })

    log_file = runs_dir / f"{run_id}.jsonl"
    with open(log_file, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    return run_id


def test_rewind_success(tmp_path):
    """Rewinding to a valid event exits 0 and reports invalidated count."""
    run_id = _create_test_run(tmp_path)
    result = _run_godel(
        ["rewind", run_id, "--to", "evt-a"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    combined = result.stdout + result.stderr
    assert "invalidated" in combined.lower()
    assert "rewound" in combined.lower()


def test_rewind_suggests_resume(tmp_path):
    """Rewind prints a 'godel resume' hint as next step."""
    run_id = _create_test_run(tmp_path)
    result = _run_godel(
        ["rewind", run_id, "--to", "evt-a"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    assert "godel resume" in result.stderr


def test_rewind_invalid_event_id(tmp_path):
    """Passing a non-existent event ID exits non-zero with an error."""
    run_id = _create_test_run(tmp_path)
    result = _run_godel(
        ["rewind", run_id, "--to", "nonexistent-evt"],
        cwd=str(tmp_path),
    )
    assert result.returncode != 0
    assert "not found" in result.stderr.lower() or "nonexistent" in result.stderr.lower()


def test_rewind_no_runs_dir(tmp_path):
    """Missing runs/ directory exits non-zero."""
    result = _run_godel(
        ["rewind", "anything", "--to", "evt"],
        cwd=str(tmp_path),
    )
    assert result.returncode != 0
    assert "runs" in result.stderr.lower()


def test_rewind_ambiguous_prefix(tmp_path):
    """An ambiguous run_id prefix exits non-zero."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    # Create two runs with similar prefixes
    for name in ["abc-run-1", "abc-run-2"]:
        (runs_dir / f"{name}.jsonl").write_text(
            json.dumps({
                "event_id": "e1", "run_id": name, "seq": 0,
                "children_ids": [], "step_path": [], "invocation_seq": 0,
                "step_local_seq": 0, "op": "WORKFLOW_STARTED", "request_hash": "",
                "request": {}, "response": None, "status": "FINISHED",
                "ts_start": "2024-01-01T00:00:00+00:00",
                "ts_end": "2024-01-01T00:00:01+00:00",
            }) + "\n"
        )
    result = _run_godel(
        ["rewind", "abc", "--to", "e1"],
        cwd=str(tmp_path),
    )
    assert result.returncode != 0
    assert "ambiguous" in result.stderr.lower()


def test_rewind_no_match(tmp_path):
    """Unknown run_id prefix exits non-zero."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    result = _run_godel(
        ["rewind", "zzz-unknown", "--to", "evt"],
        cwd=str(tmp_path),
    )
    assert result.returncode != 0
    assert "no run" in result.stderr.lower()


def test_rewind_multi_target(tmp_path):
    """Rewinding to multiple comma-separated event IDs should succeed."""
    run_id = _create_test_run(tmp_path)
    result = _run_godel(
        ["rewind", run_id, "--to", "evt-a,evt-ws"],
        cwd=str(tmp_path),
    )
    # Both are valid idempotent events — should succeed
    assert result.returncode == 0, (
        f"Expected exit 0\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "invalidated" in combined.lower()


def test_rewind_unsafe_rejected(tmp_path):
    """Rewinding past a non-idempotent run() command is refused."""
    run_id = _create_non_idempotent_run(tmp_path)
    result = _run_godel(
        ["rewind", run_id, "--to", "evt-start"],
        cwd=str(tmp_path),
    )
    assert result.returncode != 0
    assert "rewind failed" in result.stderr.lower() or "unsafe" in result.stderr.lower()


def test_rewind_missing_to_option(tmp_path):
    """Missing --to option exits non-zero (click requirement)."""
    run_id = _create_test_run(tmp_path)
    result = _run_godel(
        ["rewind", run_id],
        cwd=str(tmp_path),
    )
    assert result.returncode != 0


def test_rewind_shows_run_id_in_output(tmp_path):
    """Rewind output includes the full run_id."""
    run_id = _create_test_run(tmp_path)
    result = _run_godel(
        ["rewind", run_id, "--to", "evt-a"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    # The full run_id should appear somewhere in stdout or stderr
    assert run_id in result.stdout or run_id in result.stderr
