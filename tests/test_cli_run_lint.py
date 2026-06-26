"""Tests for godel run/resume pre-flight lint check."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = str(Path(__file__).parent.parent)


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run godel CLI in a subprocess, pointing PYTHONPATH at the worktree."""
    return subprocess.run(
        [sys.executable, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )


def test_run_rejects_lint_errors(tmp_path):
    """run command exits 1 with lint errors and prints diagnostic info."""
    (tmp_path / "bad.py").write_text('''\
from godel import workflow, run

@workflow
async def main():
    run("echo hi")  # missing await — PL001 error
''')
    result = _run_godel(["run", "bad.py", "--no-strict"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert "PL001" in result.stderr or "Lint errors" in result.stderr


def test_run_no_lint_skips_check(tmp_path):
    """--no-lint bypasses the lint pre-flight check."""
    (tmp_path / "bad.py").write_text('''\
from godel import workflow, run

@workflow
async def main():
    run("echo hi")  # missing await — but --no-lint skips the lint check
''')
    result = _run_godel(["run", "bad.py", "--no-strict", "--no-lint"], cwd=str(tmp_path))
    # Should NOT fail with lint errors — lint was skipped
    assert "Lint errors" not in result.stderr


def test_run_lint_warnings_dont_block(tmp_path):
    """Lint warnings are printed but do not block execution."""
    (tmp_path / "warn.py").write_text('''\
from godel import workflow, step

@step
async def s():
    try:
        return 1
    except:  # PL007 warning
        return 2

@workflow
async def main():
    return await s()
''')
    result = _run_godel(["run", "warn.py", "--no-strict"], cwd=str(tmp_path))
    # Warnings should not block execution — exit 0
    assert result.returncode == 0
    # Warnings must still be visible on stderr (PL007)
    assert "PL007" in result.stderr


def test_run_lint_error_message_format(tmp_path):
    """Lint error output includes 'Lint errors found' header and fix hint."""
    (tmp_path / "bad.py").write_text('''\
from godel import workflow, run

@workflow
async def main():
    run("echo hi")
''')
    result = _run_godel(["run", "bad.py", "--no-strict"], cwd=str(tmp_path))
    assert result.returncode == 1
    assert "Lint errors found" in result.stderr
    assert "--no-lint" in result.stderr


def test_run_clean_file_lint_passes(tmp_path):
    """A clean workflow file passes lint and runs normally."""
    (tmp_path / "good.py").write_text('''\
from godel import workflow, step

@step
async def do_work():
    return 42

@workflow
async def main():
    return await do_work()
''')
    result = _run_godel(["run", "good.py", "--no-strict"], cwd=str(tmp_path))
    # Lint passes — execution should proceed (exit 0 on success)
    assert result.returncode == 0
    assert "Lint errors" not in result.stderr


def test_run_lint_flag_present_in_help(tmp_path):
    """--no-lint flag appears in godel run --help output."""
    result = _run_godel(["run", "--help"], cwd=str(tmp_path))
    assert "--no-lint" in result.stdout


def test_resume_lint_flag_present_in_help(tmp_path):
    """--no-lint flag appears in godel resume --help output."""
    result = _run_godel(["resume", "--help"], cwd=str(tmp_path))
    assert "--no-lint" in result.stdout


def test_no_strict_suppresses_pl003(tmp_path):
    """--no-strict suppresses PL003 in lint so non-deterministic calls are allowed."""
    (tmp_path / "nondeterministic.py").write_text('''\
import datetime
from godel import workflow, step

@step
async def s():
    return str(datetime.datetime.now())

@workflow
async def main():
    return await s()
''')
    # With --no-strict alone, PL003 must NOT fire (and lint must not block)
    result = _run_godel(["run", "nondeterministic.py", "--no-strict"], cwd=str(tmp_path))
    assert "PL003" not in result.stderr
    # Lint errors should not be present (PL003 suppressed)
    assert "Lint errors" not in result.stderr
