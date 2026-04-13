"""Tests for godel run CLI."""
import os
import subprocess
import sys

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PYTHON = sys.executable


def test_run_help():
    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", "--help"],
        capture_output=True, text=True,
        cwd=os.path.dirname(FIXTURES),
    )
    assert result.returncode == 0
    assert "Execute" in result.stdout or "FILE" in result.stdout


def test_run_good_workflow():
    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", os.path.join(FIXTURES, "good_workflow.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "completed" in result.stderr


def test_run_no_workflow():
    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", os.path.join(FIXTURES, "no_workflow.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "No @workflow" in result.stderr


def test_run_failing_workflow():
    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", os.path.join(FIXTURES, "failing_workflow.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "WorkflowFail" in result.stderr
    assert "intentional failure" in result.stderr
