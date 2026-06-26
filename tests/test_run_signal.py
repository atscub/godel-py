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
    except PermissionError:
        # Process exists but is owned by another user — still "alive" for
        # our purposes. Unlikely in same-user tests, but don't let it
        # surface as a spurious test failure.
        return True


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
    """SIGINT to the workflow process must kill the child subprocess AND its
    grandchild (the actual long-running process) within 5 s.

    The bug report concern is that agent grandchildren (e.g. claude CLI)
    survive, not just the immediate sh wrapper.  We therefore record both
    the sh PID and the sleep grandchild PID and assert both die.
    """
    marker_sh = f"/tmp/godel_signal_sh_{os.getpid()}.pid"
    marker_gc = f"/tmp/godel_signal_gc_{os.getpid()}.pid"
    # sh writes its own PID, backgrounds sleep (recording $! as the grandchild
    # PID), then `wait`s so the sh process stays alive until sleep finishes.
    script = textwrap.dedent(f"""\
        from godel._decorators import workflow
        from godel._run import run

        @workflow
        async def wf():
            await run(
                "sh -c 'echo $$ > {marker_sh}; sleep 60 & echo $! > {marker_gc}; wait'",
            )
    """)
    path = _write_script(script)
    workflow_proc = _spawn_godel_run(path)

    try:
        # Wait for BOTH marker files to appear.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(marker_sh) and os.path.exists(marker_gc):
                break
            time.sleep(0.1)
        else:
            workflow_proc.kill()
            pytest.fail("Child processes did not write marker files in time")

        with open(marker_sh) as f:
            sh_pid = int(f.read().strip())
        with open(marker_gc) as f:
            gc_pid = int(f.read().strip())

        assert _pid_alive(sh_pid) and _pid_alive(gc_pid), (
            "Both sh and sleep grandchild should be alive before SIGINT"
        )

        # Send SIGINT to the workflow process.
        workflow_proc.send_signal(signal.SIGINT)

        # Workflow should exit within 5 s.
        try:
            workflow_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            workflow_proc.kill()
            pytest.fail("Workflow did not exit within 5 s after SIGINT")

        # Both PIDs should be dead within 2 s of workflow exit.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not _pid_alive(sh_pid) and not _pid_alive(gc_pid):
                break
            time.sleep(0.1)

        survivors = [
            ("sh", sh_pid) if _pid_alive(sh_pid) else None,
            ("sleep-grandchild", gc_pid) if _pid_alive(gc_pid) else None,
        ]
        survivors = [s for s in survivors if s]
        assert not survivors, (
            f"Processes still alive after SIGINT to workflow: {survivors}"
        )
    finally:
        for p in (marker_sh, marker_gc, path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Test 3: parallel run() calls both die on SIGINT
# ---------------------------------------------------------------------------

def test_sigint_kills_parallel_children():
    """A single SIGINT must kill all parallel run() children AND grandchildren."""
    pid = os.getpid()
    marker_sh1 = f"/tmp/godel_par_sh1_{pid}.pid"
    marker_gc1 = f"/tmp/godel_par_gc1_{pid}.pid"
    marker_sh2 = f"/tmp/godel_par_sh2_{pid}.pid"
    marker_gc2 = f"/tmp/godel_par_gc2_{pid}.pid"
    script = textwrap.dedent(f"""\
        from godel._decorators import workflow, parallel
        from godel._run import run

        @workflow
        async def wf():
            await parallel(
                run("sh -c 'echo $$ > {marker_sh1}; sleep 60 & echo $! > {marker_gc1}; wait'"),
                run("sh -c 'echo $$ > {marker_sh2}; sleep 60 & echo $! > {marker_gc2}; wait'"),
            )
    """)
    path = _write_script(script)
    workflow_proc = _spawn_godel_run(path)

    try:
        # Wait for all four marker files.
        all_markers = (marker_sh1, marker_gc1, marker_sh2, marker_gc2)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(os.path.exists(m) for m in all_markers):
                break
            time.sleep(0.1)
        else:
            workflow_proc.kill()
            pytest.fail("Parallel children did not write marker files in time")

        pids = {}
        for label, m in (
            ("sh1", marker_sh1), ("gc1", marker_gc1),
            ("sh2", marker_sh2), ("gc2", marker_gc2),
        ):
            with open(m) as f:
                pids[label] = int(f.read().strip())

        assert all(_pid_alive(p) for p in pids.values()), (
            "All children+grandchildren should be alive before SIGINT"
        )

        workflow_proc.send_signal(signal.SIGINT)

        try:
            workflow_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            workflow_proc.kill()
            pytest.fail("Workflow did not exit within 5 s after SIGINT")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not any(_pid_alive(p) for p in pids.values()):
                break
            time.sleep(0.1)

        survivors = {label: p for label, p in pids.items() if _pid_alive(p)}
        assert not survivors, f"Processes still alive after SIGINT: {survivors}"
    finally:
        for p in (marker_sh1, marker_gc1, marker_sh2, marker_gc2, path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Test 4: double SIGINT triggers immediate exit
# ---------------------------------------------------------------------------

def test_double_sigint_panic_exit():
    """Two rapid SIGINTs within 1 s should trigger the panic path specifically.

    Both ``os._exit(130)`` (panic) and ``sys.exit(130)`` (normal
    KeyboardInterrupt handling in ``run_cmd``) yield exit code 130, so exit
    code alone does not discriminate.  To verify the panic path we:
      1. Use a marker-file sync to guarantee the subprocess has reached
         ``proc.communicate()`` before we SIGINT (flake-free on slow CI).
      2. Measure elapsed time after the SECOND SIGINT.  The panic path exits
         near-instantly (< 500 ms).  The non-panic path would need to wait
         for ``_kill_process_group``'s SIGTERM→SIGKILL grace window (~2 s).
    """
    marker = f"/tmp/godel_panic_{os.getpid()}.pid"
    script = textwrap.dedent(f"""\
        from godel._decorators import workflow
        from godel._run import run

        @workflow
        async def wf():
            # Marker file is written by the shell BEFORE sleep, so its
            # presence proves the subprocess is running and proc.communicate()
            # is blocked — i.e., the workflow task is awaiting inside run().
            await run("sh -c 'echo $$ > {marker}; sleep 60'")
    """)
    path = _write_script(script)
    workflow_proc = _spawn_godel_run(path)

    try:
        # Wait for the marker file — deterministic startup sync.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(marker):
                break
            time.sleep(0.05)
        else:
            workflow_proc.kill()
            pytest.fail("Subprocess did not write marker file in time")

        # First SIGINT — schedules task cancellation + cleanup.
        workflow_proc.send_signal(signal.SIGINT)
        # Second SIGINT < 1 s later — triggers os._exit(130) panic path.
        time.sleep(0.05)
        sigint2_at = time.monotonic()
        workflow_proc.send_signal(signal.SIGINT)

        # Panic path exits near-instantly.  Give it up to 3 s in case of
        # scheduler noise, but assert < 500 ms actually elapsed for the panic
        # path to be the one that fired (the graceful path would wait for
        # _kill_process_group's 2 s grace window).
        try:
            rc = workflow_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            workflow_proc.kill()
            pytest.fail("Workflow did not exit within 3 s after double SIGINT")

        elapsed = time.monotonic() - sigint2_at
        assert rc == 130, f"Expected exit code 130, got {rc}"
        assert elapsed < 0.5, (
            f"Panic path should exit in < 500 ms after second SIGINT; "
            f"got {elapsed*1000:.0f} ms — graceful cleanup path likely fired instead"
        )
    finally:
        for p in (marker, path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
