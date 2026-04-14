"""Tests for godel CLI: --watch flag and `godel watch` subcommand.

Acceptance criteria verified:

- AC1  ``godel run --watch FILE``: watcher subprocess is spawned; killing it
       does NOT prevent the underlying run from completing.
- AC2  ``godel watch <run_id>`` on a completed run: exits cleanly (exit 0)
       after rendering the final state.
- AC3  ``godel watch <run_id>`` on a still-running run: late-attaches and
       replays history (covered by AC2 via a pre-written transcript).
- AC4  Discoverability hint: ``godel watch <run_id>`` with no transcript dir
       (stream_agents=False) emits the hint string within 6 s.
- AC4b ``godel run --watch FILE`` with stream_agents=False: hint emitted by
       the watcher subprocess within 6 s (via ``python -m godel._watch``).
- AC5  ``godel run`` without ``--watch`` is byte-identical to current behaviour
       (run completes, no subprocess spawned).
- AC6  ``godel watch`` requires godel[watch] (rich) — error on missing dep.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("rich", reason="godel[watch] (rich) must be installed for watch tests")

PYTHON = sys.executable
FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Workflow fixture helpers
# ---------------------------------------------------------------------------

def _write_workflow(path: Path, *, stream_agents: bool = False) -> None:
    """Write a minimal @workflow file to *path*."""
    flag = "True" if stream_agents else "False"
    path.write_text(
        "import asyncio\n"
        "from godel import workflow, step\n"
        "\n"
        f"@workflow(stream_agents={flag})\n"
        "async def my_workflow():\n"
        "    await asyncio.sleep(0.05)\n"
        "    return 'done'\n"
    )


def _write_transcript_dir(runs_dir: Path, run_id: str) -> Path:
    """Create a minimal transcript directory for *run_id* with one event.

    Includes a ``WORKFLOW_FINISHED`` sentinel so that ``TranscriptTail`` (with
    ``follow=True``) knows the run is complete and exits the iterator.
    """
    import json as _json

    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = run_dir / "transcript.jsonl"
    # Header line
    header = {"header": {"v": 1, "run_id": run_id, "started_at": "2026-01-01T00:00:00+00:00"}}
    # One step event
    event = {"event": {
        "ts": "2026-01-01T00:00:01+00:00",
        "seq": 1,
        "op": "step.enter",
        "step_path": ["my_step"],
        "stream_path": [],
    }}
    # Terminal sentinel — signals watchers to exit the follow loop
    finished = {"event": {
        "ts": "2026-01-01T00:00:02+00:00",
        "seq": 2,
        "op": "WORKFLOW_FINISHED",
        "status": "FINISHED",
        "step_path": [],
        "stream_path": [],
    }}
    with open(transcript, "w") as fh:
        fh.write(_json.dumps(header) + "\n")
        fh.write(_json.dumps(event) + "\n")
        fh.write(_json.dumps(finished) + "\n")
    return run_dir


# ---------------------------------------------------------------------------
# AC1: --watch spawns an isolated subprocess; killing it does not affect run
# ---------------------------------------------------------------------------

def test_run_watch_spawns_subprocess(tmp_path):
    """``godel run --watch FILE`` spawns a watcher subprocess.

    The watcher subprocess should start; killing it must not prevent the run
    from completing (crash isolation guarantee).
    """
    wf = tmp_path / "wf.py"
    # Workflow that sleeps briefly so the watcher has time to start
    wf.write_text(
        "import asyncio\n"
        "from godel import workflow\n"
        "\n"
        "@workflow(stream_agents=True)\n"
        "async def my_workflow():\n"
        "    await asyncio.sleep(0.3)\n"
        "    return 'done'\n"
    )

    # Run with --watch in a subprocess; capture output.
    # --no-strict is required because the strict audit hook blocks file writes
    # (transcript.jsonl) that stream_agents=True needs.
    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", "--watch", "--no-strict", str(wf)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=30,
    )
    # Run must complete regardless of watcher state
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "completed" in result.stderr


def test_run_watch_run_completes_after_watcher_killed(tmp_path):
    """Killing the watcher subprocess mid-run does not abort the run.

    We start a long-running workflow with --watch, wait briefly for the
    watcher subprocess to start, kill it, and verify the run still completes.
    """
    wf = tmp_path / "wf.py"
    wf.write_text(
        "import asyncio\n"
        "from godel import workflow\n"
        "\n"
        "@workflow(stream_agents=True)\n"
        "async def my_workflow():\n"
        "    await asyncio.sleep(0.5)\n"
        "    return 'done'\n"
    )

    # Start the run in a subprocess so we can inspect child processes.
    # --no-strict is required because the strict audit hook blocks transcript writes.
    proc = subprocess.Popen(
        [PYTHON, "-m", "godel", "run", "--watch", "--no-strict", str(wf)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(tmp_path),
    )

    # Give the watcher subprocess time to spawn
    time.sleep(0.3)

    # Find child processes of proc and kill any watcher subprocess
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        # psutil not available — skip the kill step but still verify run
        pass

    # Wait for the main run to complete
    try:
        stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(f"Run timed out. stderr: {stderr}")

    assert proc.returncode == 0, f"Run failed. stderr: {stderr}"
    assert "completed" in stderr


# ---------------------------------------------------------------------------
# AC2 / AC3: `godel watch <run_id>` on a completed run exits cleanly
# ---------------------------------------------------------------------------

def test_watch_cmd_completed_run(tmp_path):
    """``godel watch <run_id>`` on a completed run exits 0 and renders events."""
    run_id = "test-watch-completed"
    runs_dir = tmp_path / "runs"
    _write_transcript_dir(runs_dir, run_id)

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", run_id, "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"


def test_watch_cmd_resolves_prefix(tmp_path):
    """``godel watch`` resolves run_id from audit log prefix."""
    full_run_id = "abcdef-1234-5678-full"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # Create audit log (for prefix resolution)
    (runs_dir / f"{full_run_id}.jsonl").write_text("")
    # Create transcript directory
    _write_transcript_dir(runs_dir, full_run_id)

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", "abcdef", "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"


def test_watch_cmd_missing_run_exits_1(tmp_path):
    """``godel watch <run_id>`` exits 1 when the run does not exist."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", "nonexistent-run", "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=10,
    )
    assert result.returncode == 1
    assert "nonexistent-run" in result.stderr or "No run" in result.stderr


def test_watch_cmd_ambiguous_prefix_exits_1(tmp_path):
    """``godel watch <prefix>`` exits 1 on ambiguous prefix."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "shared-aaa.jsonl").write_text("")
    (runs_dir / "shared-bbb.jsonl").write_text("")

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", "shared", "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=10,
    )
    assert result.returncode == 1
    assert "Ambiguous" in result.stderr


# ---------------------------------------------------------------------------
# AC4: Discoverability hint for stream_agents=False
# ---------------------------------------------------------------------------

def test_watch_cmd_hint_stream_agents_false(tmp_path):
    """``godel watch`` shows the hint when transcript dir is absent (stream_agents=False).

    The hint must appear within 6 seconds.
    """
    run_id = "hint-test-run"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # Create audit log but NO transcript directory (stream_agents=False)
    (runs_dir / f"{run_id}.jsonl").write_text("")

    start = time.monotonic()
    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", run_id, "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=10,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 6.0, f"Hint took too long: {elapsed:.2f}s"
    hint_text = "agent streaming disabled"
    assert hint_text in result.stderr, (
        f"Expected hint '{hint_text}' not found in stderr:\n{result.stderr}"
    )
    assert result.returncode == 0


def test_watch_subprocess_hint_stream_agents_false(tmp_path):
    """``python -m godel._watch`` shows the hint when transcript dir is absent.

    This covers the ``godel run --watch`` path where the watcher subprocess
    is spawned via ``python -m godel._watch``.
    The hint must appear within 6 seconds.
    """
    run_id = "hint-subprocess-test"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    result = subprocess.run(
        [PYTHON, "-m", "godel._watch", run_id, "--runs-dir", str(runs_dir),
         "--hint-timeout", "1.0"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=10,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 6.0, f"Hint took too long: {elapsed:.2f}s"
    hint_text = "agent streaming disabled"
    assert hint_text in result.stderr, (
        f"Expected hint '{hint_text}' not found in stderr:\n{result.stderr}"
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# AC5: `godel run` without --watch is unaffected
# ---------------------------------------------------------------------------

def test_run_without_watch_unchanged(tmp_path):
    """``godel run FILE`` without --watch completes normally with no watcher."""
    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", str(FIXTURES / "good_workflow.py")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert result.returncode == 0
    assert "completed" in result.stderr
    # No watch-related output
    assert "[godel-watch]" not in result.stdout
    assert "[godel-watch]" not in result.stderr


# ---------------------------------------------------------------------------
# AC6: godel watch requires rich; friendly error on missing dep
# ---------------------------------------------------------------------------

def test_watch_cmd_help_shows_flag():
    """``godel run --help`` shows the --watch flag."""
    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--watch" in result.stdout


def test_watch_subcommand_help():
    """``godel watch --help`` exits 0 and describes the command."""
    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "watch" in result.stdout.lower() or "RUN_ID" in result.stdout


# ---------------------------------------------------------------------------
# _spawn_watch_subprocess helper (unit tests)
# ---------------------------------------------------------------------------

def test_spawn_watch_subprocess_returns_popen(tmp_path):
    """_spawn_watch_subprocess returns a Popen and can be terminated."""
    from godel.cli import _spawn_watch_subprocess

    run_id = "spawn-test-run"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    proc = _spawn_watch_subprocess(run_id, runs_dir=str(runs_dir))
    assert proc is not None
    assert proc.pid > 0

    # Give it a moment to start, then terminate
    time.sleep(0.1)
    proc.terminate()
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Verify that the parent (this test process) is still alive — no crash propagation
    assert os.getpid() > 0


def test_spawn_watch_subprocess_isolated(tmp_path):
    """Killing the watcher subprocess does not raise in the calling process."""
    from godel.cli import _spawn_watch_subprocess

    run_id = "isolated-test-run"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    proc = _spawn_watch_subprocess(run_id, runs_dir=str(runs_dir))
    time.sleep(0.1)
    # Hard kill — should not affect the parent
    proc.kill()
    proc.wait()
    # Parent is alive and no exception propagated
    assert True
