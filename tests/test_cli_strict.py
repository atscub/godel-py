"""Tests for godel run --strict CLI flag.

All --strict tests run in subprocesses because the audit hook (Layer 3)
is permanent per PEP 578 and would contaminate the test process.
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).parent.parent)

CLEAN_WORKFLOW = '''\
from godel import workflow

@workflow
async def wf():
    return 42
'''

BAD_IMPORT = '''\
import requests
from godel import workflow

@workflow
async def wf():
    pass
'''

BAD_DATETIME = '''\
from datetime import datetime
from godel import workflow

@workflow
async def wf():
    return datetime.now()
'''


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**__import__("os").environ, "PYTHONPATH": PROJECT_ROOT},
    )


def test_strict_clean_workflow(tmp_path):
    """Strict is on by default — clean workflow should pass."""
    (tmp_path / "clean.py").write_text(CLEAN_WORKFLOW)
    result = _run_godel(["run", "clean.py"], cwd=str(tmp_path))
    assert result.returncode == 0


def test_strict_catches_banned_import(tmp_path):
    """Strict is on by default — banned import should fail."""
    (tmp_path / "bad.py").write_text(BAD_IMPORT)
    result = _run_godel(["run", "bad.py"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert "requests" in result.stderr


def test_strict_catches_datetime_now(tmp_path):
    """Strict is on by default — datetime.now should fail."""
    (tmp_path / "bad.py").write_text(BAD_DATETIME)
    result = _run_godel(["run", "bad.py"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert "datetime" in result.stderr or "banned" in result.stderr


def test_no_strict_runs_normally(tmp_path):
    """--no-strict disables strict mode, allowing banned imports."""
    (tmp_path / "clean.py").write_text(CLEAN_WORKFLOW)
    result = _run_godel(["run", "--no-strict", "clean.py"], cwd=str(tmp_path))
    assert result.returncode == 0


def test_no_strict_allows_banned_import(tmp_path):
    """--no-strict allows banned imports that strict would reject."""
    (tmp_path / "bad.py").write_text(BAD_IMPORT)
    result = _run_godel(["run", "--no-strict", "bad.py"], cwd=str(tmp_path))
    # Should not fail due to strict violations (may fail for other reasons like missing module)
    assert result.returncode != 1 or "requests" not in result.stderr


def test_strict_error_format(tmp_path):
    """Strict error messages include violation info and [ast] tag."""
    (tmp_path / "bad.py").write_text(BAD_IMPORT)
    result = _run_godel(["run", "bad.py"], cwd=str(tmp_path))
    assert "violation" in result.stderr.lower()
    assert "[ast]" in result.stderr
