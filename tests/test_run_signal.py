"""Tests for SIGINT → process-group cleanup behaviour of run().

These tests verify that:
  1. Each subprocess started by run() lives in its own process group.
  2. Sending SIGINT to the workflow process (via ``godel run``) kills all
     spawned children within 5 s — no orphan processes survive.
  3. Two parallel run() calls both die on a single SIGINT.
  4. A second SIGINT within 1 s of the first triggers immediate process exit
     (exit code 130) even if cleanup is hung.

All POSIX-specific assertions are skipped on Windows.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Process-group signal tests are POSIX-only",
)

import godel as _godel_pkg

# Root of the godel-py worktree (parent of the godel package directory).
# Prepended to PYTHONPATH so spawned subprocesses load the *modified* package.
_WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_godel_pkg.__file__)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env() -> dict:
    """Return an env dict with the worktree prepended to PYTHONPATH."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_WORKTREE_ROOT}:{existing}" if existing else _WORKTREE_ROOT
    return env


def _write_script(content: str) -> str:
    """Write *content* to a temp .py file and return its path."""
    f = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w")
    f.write(content)
    f.flush()
    f.close()
    return f.name


def _spawn_godel_run(script_path: str, *, no_strict: bool = True, no_lint: bool = True) -> subprocess.Popen:
    """Launch ``godel run <script_path>`` via ``python -m godel`` and return the Popen handle."""
    cmd = [sys.executable, "-m", "godel", "run"]
    if no_strict:
        cmd.append("--no-strict")
    if no_lint:
        cmd.append("--no-lint")
    cmd.append(script_path)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_make_env(),
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ---------------------------------------------------------------------------
# Test 1: process group isolation (direct asyncio.run — no CLI needed)
# ---------------------------------------------------------------------------

def test_run_process_group_isolation():
    """Child started by run() must be in a different process group than the workflow."""
    script = textwrap.dedent("""\
        import asyncio, os
        from godel._decorators import workflow
        from godel._run import run

        @workflow
        async def wf():
            result = await run(
                "python3 -c 'import os; print(os.getpid(), os.getpgrp())'",
            )
            child_pid, child_pgid = result.stdout.strip().split()
            workflow_pgid = str(os.getpgrp())
            print(f"child_pgid={child_pgid} workflow_pgid={workflow_pgid}", flush=True)
            if child_pgid == workflow_pgid:
                raise AssertionError(
                    f"Child process group {child_pgid!r} should differ from "
                    f"workflow process group {workflow_pgid!r}"
                )

        asyncio.run(wf())
        print("OK", flush=True)
    """)
    path = _write_script(script)
    try:
        proc = subprocess.Popen(
            [sys.executable, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_make_env(),
        )
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, (
            f"Workflow exited {proc.returncode}\n"
            f"stdout: {stdout.decode()}\n"
            f"stderr: {stderr.decode()}"
        )
        assert b"OK" in stdout
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Test 2: SIGINT kills the spawned subprocess
# ---------------------------------------------------------------------------

def test_sigint_kills_child():
    """SIGINT to the workflow process must kill the child subprocess within 5 s."""
    marker_file = f"/tmp/godel_signal_test_{os.getpid()}.pid"
    script = textwrap.dedent(f"""\
        from godel._decorators import workflow
        from godel._run import run

        @workflow
        async def wf():
            await run(
                "sh -c 'echo $$ > {marker_file} && sleep 60'",
            )
    """)
    path = _write_script(script)
    workflow_proc = _spawn_godel_run(path)

    try:
        # Wait for the marker file to appear (child has started).
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(marker_file):
                break
            time.sleep(0.1)
        else:
            workflow_proc.kill()
            pytest.fail("Child process did not write marker file in time")

        with open(marker_file) as f:
            child_pid = int(f.read().strip())

        assert _pid_alive(child_pid), "Child should be alive before SIGINT"

        # Send SIGINT to the workflow process.
        workflow_proc.send_signal(signal.SIGINT)

        # Workflow should exit within 5 s.
        try:
            workflow_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            workflow_proc.kill()
            pytest.fail("Workflow did not exit within 5 s after SIGINT")

        # Child should be dead within 2 s of workflow exit.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not _pid_alive(child_pid):
                break
            time.sleep(0.1)

        assert not _pid_alive(child_pid), (
            f"Child process {child_pid} is still alive after SIGINT to workflow"
        )
    finally:
        for p in (marker_file, path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Test 3: parallel run() calls both die on SIGINT
# ---------------------------------------------------------------------------

def test_sigint_kills_parallel_children():
    """A single SIGINT must kill all parallel run() children."""
    marker1 = f"/tmp/godel_par1_{os.getpid()}.pid"
    marker2 = f"/tmp/godel_par2_{os.getpid()}.pid"
    script = textwrap.dedent(f"""\
        from godel._decorators import workflow, parallel
        from godel._run import run

        @workflow
        async def wf():
            await parallel(
                run("sh -c 'echo $$ > {marker1} && sleep 60'"),
                run("sh -c 'echo $$ > {marker2} && sleep 60'"),
            )
    """)
    path = _write_script(script)
    workflow_proc = _spawn_godel_run(path)

    try:
        # Wait for both marker files.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(marker1) and os.path.exists(marker2):
                break
            time.sleep(0.1)
        else:
            workflow_proc.kill()
            pytest.fail("Parallel children did not write marker files in time")

        with open(marker1) as f:
            child1_pid = int(f.read().strip())
        with open(marker2) as f:
            child2_pid = int(f.read().strip())

        assert _pid_alive(child1_pid) and _pid_alive(child2_pid), "Both children should be alive"

        workflow_proc.send_signal(signal.SIGINT)

        try:
            workflow_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            workflow_proc.kill()
            pytest.fail("Workflow did not exit within 5 s after SIGINT")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not _pid_alive(child1_pid) and not _pid_alive(child2_pid):
                break
            time.sleep(0.1)

        survivors = [p for p in (child1_pid, child2_pid) if _pid_alive(p)]
        assert not survivors, f"Processes still alive after SIGINT: {survivors}"
    finally:
        for p in (marker1, marker2, path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Test 4: double SIGINT triggers immediate exit
# ---------------------------------------------------------------------------

def test_double_sigint_panic_exit():
    """Two rapid SIGINTs within 1 s should cause immediate exit (code 130)."""
    script = textwrap.dedent("""\
        from godel._decorators import workflow
        from godel._run import run

        @workflow
        async def wf():
            await run("sleep 60")
    """)
    path = _write_script(script)
    workflow_proc = _spawn_godel_run(path)

    try:
        # Give it a moment to start the subprocess.
        time.sleep(0.5)

        # First SIGINT — triggers task cancellation + cleanup.
        workflow_proc.send_signal(signal.SIGINT)
        # Second SIGINT < 1 s later — triggers os._exit(130).
        time.sleep(0.05)
        workflow_proc.send_signal(signal.SIGINT)

        # Should exit quickly (< 3 s) due to os._exit(130).
        try:
            rc = workflow_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            workflow_proc.kill()
            pytest.fail("Workflow did not exit within 3 s after double SIGINT")

        assert rc == 130, f"Expected exit code 130, got {rc}"
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
