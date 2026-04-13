"""Tests for godel pause CLI command and programmatic API."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).parent.parent)


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )


def _create_run_jsonl(tmp_path, run_id: str) -> Path:
    """Create a minimal .jsonl file so prefix resolution finds the run."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(exist_ok=True)
    log_file = runs_dir / f"{run_id}.jsonl"
    event = {
        "event_id": "evt-ws",
        "run_id": run_id,
        "seq": 0,
        "children_ids": [],
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
    }
    with open(log_file, "w") as f:
        f.write(json.dumps(event) + "\n")
    return log_file


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

def test_pause_cmd_writes_sentinel(tmp_path):
    """pause_cmd writes a sentinel JSON file with reason and requested_ts."""
    run_id = "pause-test-run-001"
    _create_run_jsonl(tmp_path, run_id)

    result = _run_godel(["pause", run_id, "--reason", "manual halt"], cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr

    sentinel = tmp_path / "runs" / f"{run_id}.pause"
    assert sentinel.exists(), "sentinel file not written"

    payload = json.loads(sentinel.read_text())
    assert payload["reason"] == "manual halt"
    assert "requested_ts" in payload


def test_pause_cmd_default_reason(tmp_path):
    """pause_cmd uses 'CLI pause' as the default reason."""
    run_id = "pause-default-reason"
    _create_run_jsonl(tmp_path, run_id)

    result = _run_godel(["pause", run_id], cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr

    payload = json.loads((tmp_path / "runs" / f"{run_id}.pause").read_text())
    assert payload["reason"] == "CLI pause"


def test_pause_cmd_stderr_messages(tmp_path):
    """pause_cmd echoes confirmation messages to stderr."""
    run_id = "pause-msgs-run"
    _create_run_jsonl(tmp_path, run_id)

    result = _run_godel(["pause", run_id], cwd=str(tmp_path))
    assert result.returncode == 0
    assert f"pause requested for run {run_id}" in result.stderr
    assert f"runs/{run_id}.pause" in result.stderr
    assert f"godel resume {run_id}" in result.stderr


def test_pause_cmd_no_run_exits_1(tmp_path):
    """pause_cmd exits 1 when no matching run is found."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    result = _run_godel(["pause", "no-such-run"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert 'No run matching "no-such-run"' in result.stderr


def test_pause_cmd_no_runs_dir_exits_1(tmp_path):
    """pause_cmd exits 1 when runs/ directory doesn't exist."""
    result = _run_godel(["pause", "any-run"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert "No runs/" in result.stderr


def test_pause_cmd_ambiguous_prefix_exits_3(tmp_path):
    """pause_cmd exits 3 when the prefix matches more than one run."""
    _create_run_jsonl(tmp_path, "alpha-run-one")
    _create_run_jsonl(tmp_path, "alpha-run-two")

    result = _run_godel(["pause", "alpha-run"], cwd=str(tmp_path))
    assert result.returncode == 3
    assert 'Ambiguous prefix "alpha-run"' in result.stderr


def test_pause_cmd_prefix_resolution(tmp_path):
    """pause_cmd resolves a unique prefix to the full run_id."""
    full_run_id = "prefix-resolution-run-abc123"
    _create_run_jsonl(tmp_path, full_run_id)

    result = _run_godel(["pause", "prefix-resolution"], cwd=str(tmp_path))
    assert result.returncode == 0
    assert full_run_id in result.stderr

    sentinel = tmp_path / "runs" / f"{full_run_id}.pause"
    assert sentinel.exists()


# ---------------------------------------------------------------------------
# Programmatic API tests
# ---------------------------------------------------------------------------

def test_programmatic_pause_returns_full_run_id(tmp_path):
    """godel.pause() returns the resolved full run_id."""
    import godel
    run_id = "prog-pause-run-xyz"
    _create_run_jsonl(tmp_path, run_id)

    full = godel.pause(run_id, runs_dir=str(tmp_path / "runs"))
    assert full == run_id


def test_programmatic_pause_writes_sentinel(tmp_path):
    """godel.pause() writes the sentinel file with the given reason."""
    import godel
    run_id = "prog-pause-sentinel"
    _create_run_jsonl(tmp_path, run_id)

    godel.pause(run_id, reason="intervention agent", runs_dir=str(tmp_path / "runs"))

    sentinel = tmp_path / "runs" / f"{run_id}.pause"
    assert sentinel.exists()
    payload = json.loads(sentinel.read_text())
    assert payload["reason"] == "intervention agent"


def test_programmatic_pause_prefix_resolution(tmp_path):
    """godel.pause() resolves unique prefix and returns full run_id."""
    import godel
    full_run_id = "full-run-id-12345"
    _create_run_jsonl(tmp_path, full_run_id)

    returned = godel.pause("full-run", runs_dir=str(tmp_path / "runs"))
    assert returned == full_run_id


def test_programmatic_pause_not_found_raises(tmp_path):
    """godel.pause() raises FileNotFoundError when no run matches."""
    import godel
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    with pytest.raises(FileNotFoundError, match='No run matching "nope"'):
        godel.pause("nope", runs_dir=str(runs_dir))


def test_programmatic_pause_no_runs_dir_raises(tmp_path):
    """godel.pause() raises FileNotFoundError when runs/ dir is absent."""
    import godel

    with pytest.raises(FileNotFoundError, match="No runs/"):
        godel.pause("any", runs_dir=str(tmp_path / "runs"))


def test_programmatic_pause_ambiguous_raises(tmp_path):
    """godel.pause() raises ValueError on an ambiguous prefix."""
    import godel
    _create_run_jsonl(tmp_path, "shared-prefix-one")
    _create_run_jsonl(tmp_path, "shared-prefix-two")

    with pytest.raises(ValueError, match='Ambiguous prefix "shared-prefix"'):
        godel.pause("shared-prefix", runs_dir=str(tmp_path / "runs"))
