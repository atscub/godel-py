"""Strict mode integration tests (M2 exit criterion validation).

All tests use subprocess isolation because PEP 578 audit hooks are permanent.
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).parent.parent)


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**__import__("os").environ, "PYTHONPATH": PROJECT_ROOT},
    )


def _write_workflow(tmp_path, name, code):
    f = tmp_path / name
    f.write_text(code)
    return f


# --- Test workflows ---

WF_DATETIME_NOW = '''\
from datetime import datetime
from godel import workflow

@workflow
async def wf():
    return datetime.now()
'''

WF_IMPORT_REQUESTS = '''\
import requests
from godel import workflow

@workflow
async def wf():
    pass
'''

WF_FILE_WRITE = '''\
from godel import workflow

@workflow
async def wf():
    with open("evil.txt", "w") as f:
        f.write("bad")
'''

WF_CLEAN_WITH_DET = '''\
from godel import workflow, det

@workflow
async def wf():
    ts = det.now()
    r = det.random()
    uid = det.uuid4()
    return {"ts": ts, "random": r, "uuid": uid}
'''

WF_CLEAN_SIMPLE = '''\
from godel import workflow

@workflow
async def wf():
    return 42
'''


# --- Tests ---

def test_ast_catches_datetime_now(tmp_path):
    """Strict is on by default — datetime.now should be caught."""
    _write_workflow(tmp_path, "wf.py", WF_DATETIME_NOW)
    result = _run_godel(["run", "wf.py"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert "datetime" in result.stderr or "banned" in result.stderr


def test_ast_catches_import_requests(tmp_path):
    """Strict is on by default — banned import should be caught."""
    _write_workflow(tmp_path, "wf.py", WF_IMPORT_REQUESTS)
    result = _run_godel(["run", "wf.py"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert "requests" in result.stderr


def test_audit_catches_file_write(tmp_path):
    """Strict is on by default — file writes should be caught."""
    _write_workflow(tmp_path, "wf.py", WF_FILE_WRITE)
    result = _run_godel(["run", "wf.py"], cwd=str(tmp_path))
    assert result.returncode != 0
    assert "write" in result.stderr.lower() or "strict" in result.stderr.lower()


def test_clean_workflow_with_det_succeeds(tmp_path):
    """Clean workflow using det should pass strict mode (default)."""
    _write_workflow(tmp_path, "wf.py", WF_CLEAN_WITH_DET)
    result = _run_godel(["run", "wf.py"], cwd=str(tmp_path))
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_clean_simple_workflow_succeeds(tmp_path):
    """Clean simple workflow should pass strict mode (default)."""
    _write_workflow(tmp_path, "wf.py", WF_CLEAN_SIMPLE)
    result = _run_godel(["run", "wf.py"], cwd=str(tmp_path))
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_det_events_recorded_in_strict_mode(tmp_path):
    """Det events should be recorded in strict mode (default)."""
    _write_workflow(tmp_path, "wf.py", WF_CLEAN_WITH_DET)
    result = _run_godel(["run", "wf.py"], cwd=str(tmp_path))
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # Check JSONL for det events
    import json
    runs_dir = tmp_path / "runs"
    assert runs_dir.exists(), "runs/ directory should be created"
    jsonl_files = list(runs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, f"Expected 1 JSONL file, got {len(jsonl_files)}"
    lines = jsonl_files[0].read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]
    det_ops = [e["op"] for e in events if e["op"].startswith("det.")]
    assert "det.now" in det_ops
    assert "det.random" in det_ops
    assert "det.uuid4" in det_ops


def test_error_message_is_actionable(tmp_path):
    """Strict error messages should include file:line:col and remediation hints."""
    _write_workflow(tmp_path, "wf.py", WF_DATETIME_NOW)
    result = _run_godel(["run", "wf.py"], cwd=str(tmp_path))
    assert result.returncode == 1
    # Should have file:line:col format
    assert "wf.py:" in result.stderr
    # Should mention what to use instead
    assert "godel.det" in result.stderr or "violation" in result.stderr.lower()


def test_no_strict_allows_everything(tmp_path):
    """--no-strict + --no-lint disables all checks, allowing datetime.now etc."""
    _write_workflow(tmp_path, "wf.py", WF_DATETIME_NOW)
    result = _run_godel(["run", "--no-strict", "--no-lint", "wf.py"], cwd=str(tmp_path))
    # With --no-strict --no-lint, should just run (datetime.now returns, workflow completes)
    assert result.returncode == 0, f"stderr: {result.stderr}"
