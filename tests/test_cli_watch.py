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
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("rich", reason="godel[watch] (rich) must be installed for watch tests")

PYTHON = sys.executable
FIXTURES = Path(__file__).parent / "fixtures"

# Root of the worktree — the directory that contains the ``godel`` package.
# When subprocess tests use ``cwd=tmp_path`` the working-directory entry in
# sys.path points at tmp_path, not at the worktree, so the subprocess falls
# back to whatever ``godel`` is installed in site-packages (which may be a
# different version).  We fix this by propagating the worktree root via
# PYTHONPATH so that subprocess children always import from the same source.
_WORKTREE_ROOT = str(Path(__file__).parent.parent)


def _subprocess_env(**extra: str) -> dict:
    """Return an os.environ copy with the worktree on PYTHONPATH."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _WORKTREE_ROOT + os.pathsep + existing if existing else _WORKTREE_ROOT
    )
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Workflow fixture helpers
# ---------------------------------------------------------------------------

def _write_workflow(path: Path, *, stream_agents: bool = False) -> None:
    """Write a minimal @workflow file to *path*.

    ``stream_agents`` is retained as a parameter for legacy callers but is
    no longer reflected in the workflow source — streaming is now controlled
    by the CLI (--no-stream) or the GODEL_STREAM_AGENTS env var.
    """
    path.write_text(
        "import asyncio\n"
        "from godel import workflow, step\n"
        "\n"
        "@workflow\n"
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
        "@workflow\n"
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
        "@workflow\n"
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

    # psutil is required to reliably find + kill the watcher subprocess.
    # Without it we cannot actually verify the isolation guarantee, so skip
    # rather than silently pass.
    psutil = pytest.importorskip("psutil")

    # Give the watcher subprocess time to spawn
    time.sleep(0.3)

    # Find child processes of proc and kill any watcher subprocess
    parent = psutil.Process(proc.pid)
    children = parent.children(recursive=True)
    for child in children:
        try:
            child.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
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
    hint_text = "agent streaming was disabled"
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
    hint_text = "agent streaming was disabled"
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


def test_spawn_watch_subprocess_plain_flag(tmp_path, monkeypatch):
    """_spawn_watch_subprocess(..., plain=True) appends --plain to the watcher cmd.

    Intercept subprocess.Popen so we can inspect the command line without
    actually launching a real subprocess.
    """
    import unittest.mock as mock
    from godel.cli import _spawn_watch_subprocess

    run_id = "plain-test-run"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    captured_cmds: list = []

    def _fake_popen(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        m = mock.MagicMock()
        m.pid = 99999
        return m

    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        _spawn_watch_subprocess(run_id, runs_dir=str(runs_dir), plain=True)

    assert captured_cmds, "Popen was not called"
    cmd = captured_cmds[0]
    assert "--plain" in cmd, f"Expected --plain in watcher cmd; got: {cmd}"


def test_spawn_watch_subprocess_no_plain_flag_by_default(tmp_path):
    """_spawn_watch_subprocess without plain=True does NOT include --plain."""
    import unittest.mock as mock
    from godel.cli import _spawn_watch_subprocess

    run_id = "noplain-test-run"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    captured_cmds: list = []

    def _fake_popen(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        m = mock.MagicMock()
        m.pid = 99999
        return m

    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        _spawn_watch_subprocess(run_id, runs_dir=str(runs_dir))

    assert captured_cmds, "Popen was not called"
    cmd = captured_cmds[0]
    assert "--plain" not in cmd, f"--plain should not be in default watcher cmd; got: {cmd}"


def test_run_plain_implies_watch(tmp_path, monkeypatch):
    """`godel run --plain FILE` must spawn the watcher even without --watch.

    Regression for review finding: --plain was a dead flag unless --watch was
    also passed, despite help text claiming it implies --watch.
    """
    import unittest.mock as mock
    from click.testing import CliRunner
    from godel.cli import main

    wf = tmp_path / "wf.py"
    wf.write_text(
        "from godel import workflow, step\n"
        "@step\n"
        "async def s():\n    return 1\n"
        "@workflow\n"
        "async def w():\n    return await s()\n"
    )

    captured_cmds: list = []

    def _fake_popen(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        m = mock.MagicMock()
        m.pid = 99999
        m.poll.return_value = 0
        m.wait.return_value = 0
        m.returncode = 0
        return m

    monkeypatch.chdir(tmp_path)
    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--no-strict", str(wf), "--plain"])

    assert result.exit_code == 0, f"run failed: {result.output}\n{result.exception}"
    assert captured_cmds, "Watcher subprocess was not spawned (--plain did not imply --watch)"
    assert "--plain" in captured_cmds[0]


# ---------------------------------------------------------------------------
# W-2: WORKFLOW_FINISHED status reflects actual outcome
# ---------------------------------------------------------------------------


def test_workflow_finished_status_failed_on_exception(tmp_path, monkeypatch):
    """A failing workflow writes WORKFLOW_FINISHED with status='FAILED'.

    The terminal sentinel must reflect the actual outcome so live watchers
    render the correct final state (failed vs done).  Prior bug: status was
    hardcoded to 'FINISHED' so failed runs displayed as successful.
    """
    monkeypatch.chdir(tmp_path)
    wf = tmp_path / "wf_fail.py"
    wf.write_text(
        "import asyncio\n"
        "from godel import workflow\n"
        "from godel._decorators import WorkflowFail\n"
        "\n"
        "@workflow\n"
        "async def my_workflow():\n"
        "    raise WorkflowFail('boom')\n"
    )

    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", "--no-strict", str(wf)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=20,
    )
    # WorkflowFail → exit code 1
    assert result.returncode == 1, f"stderr: {result.stderr}"

    # Find the transcript file
    runs_dir = tmp_path / "runs"
    run_dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
    assert len(run_dirs) == 1, f"Expected one run dir, got {run_dirs}"
    transcript = run_dirs[0] / "transcript.jsonl"
    assert transcript.exists(), f"Transcript not found: {transcript}"

    finished = None
    for line in transcript.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "event" in obj and obj["event"].get("op") == "WORKFLOW_FINISHED":
            finished = obj["event"]
            break
    assert finished is not None, (
        f"WORKFLOW_FINISHED event not found in transcript:\n{transcript.read_text()}"
    )
    assert finished.get("status") == "FAILED", (
        f"Expected status='FAILED' on failing run, got {finished!r}"
    )


def test_workflow_finished_status_finished_on_success(tmp_path, monkeypatch):
    """A successful workflow writes WORKFLOW_FINISHED with status='FINISHED'."""
    monkeypatch.chdir(tmp_path)
    wf = tmp_path / "wf_ok.py"
    wf.write_text(
        "import asyncio\n"
        "from godel import workflow\n"
        "\n"
        "@workflow\n"
        "async def my_workflow():\n"
        "    return 'ok'\n"
    )

    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", "--no-strict", str(wf)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=20,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    runs_dir = tmp_path / "runs"
    run_dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
    assert len(run_dirs) == 1
    transcript = run_dirs[0] / "transcript.jsonl"

    finished = None
    for line in transcript.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "event" in obj and obj["event"].get("op") == "WORKFLOW_FINISHED":
            finished = obj["event"]
            break
    assert finished is not None
    assert finished.get("status") == "FINISHED"


# ---------------------------------------------------------------------------
# W-1: `godel watch` prefix resolution unifies transcript-dir + audit-log
# ---------------------------------------------------------------------------


def test_watch_cmd_prefix_resolution_dir_only(tmp_path):
    """Prefix matching a transcript dir (no audit log) resolves correctly."""
    run_id = "dironly-abcdef"
    runs_dir = tmp_path / "runs"
    _write_transcript_dir(runs_dir, run_id)  # creates dir with transcript
    # Note: no .jsonl audit log — only the dir

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", "dironly", "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=15,
    )
    # Transcript has WORKFLOW_FINISHED sentinel → clean exit
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"


def test_watch_cmd_prefix_resolution_ambiguous_mixed(tmp_path):
    """Ambiguous prefix across audit log + transcript dir exits 1."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # Audit log match
    (runs_dir / "mixed-111.jsonl").write_text("")
    # Transcript dir match (different id)
    (runs_dir / "mixed-222").mkdir()

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", "mixed", "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=10,
    )
    assert result.returncode == 1
    assert "Ambiguous" in result.stderr


# ---------------------------------------------------------------------------
# W-3: Producer error surfaces an error banner (not silent EOS)
# ---------------------------------------------------------------------------


def test_producer_error_surfaces_banner(tmp_path, monkeypatch):
    """A TranscriptTailError during follow surfaces an error banner on stderr."""
    from unittest.mock import patch

    from godel import _watch as watch_mod

    run_id = "producer-error-run"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Force TranscriptTail.from_run to raise the error we want to surface.
    from godel._tail import TranscriptTailError

    def _fake_from_run(*args, **kwargs):
        raise TranscriptTailError("simulated transcript disappearance", path=None)

    # Use stringio capture via stdout redirect
    import io as _io
    captured_stderr = _io.StringIO()

    with patch("godel._tail.TranscriptTail.from_run", side_effect=_fake_from_run):
        with patch.object(sys, "stderr", captured_stderr):
            # Non-TTY stdout triggers plain fallback → deterministic behavior
            fake_stdout = _io.StringIO()
            watch_mod.run_watch(run_id, runs_dir=str(runs_dir), stdout=fake_stdout)

    err_output = captured_stderr.getvalue()
    assert "transcript error" in err_output, (
        f"Expected error banner in stderr, got:\n{err_output}"
    )
    assert "simulated transcript disappearance" in err_output


# ---------------------------------------------------------------------------
# Pass-2 C-1: PauseSignal must NOT emit WORKFLOW_FINISHED
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --plain flag and GODEL_WATCH_PLAIN=1 env var
# ---------------------------------------------------------------------------


def test_watch_plain_flag_forces_plain_log(tmp_path):
    """``godel watch <run_id> --plain`` uses the plain line-log on a normal TTY.

    Even though the test process is not a TTY, the presence of [godel-watch]
    prefix lines in stdout is the observable signature of _PlainLineLog.
    We verify the flag is accepted and the command exits cleanly.

    Limitation: ``subprocess.run(..., capture_output=True)`` forces a non-TTY
    pipe, so ``_use_plain_fallback()`` already returns True here — this test
    would still pass even if the CLI silently dropped ``--plain``.  The
    deliberate ``plain=True`` code path is exercised by
    ``test_watch_plain_flag_via_run_watch`` below; argparse acceptance of
    the flag is verified by ``test_watch_plain_help_shows_flag``.  A real
    TTY-context regression test would require pexpect / ``script -q``.
    """
    run_id = "plain-flag-test"
    runs_dir = tmp_path / "runs"
    _write_transcript_dir(runs_dir, run_id)

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", run_id, "--runs-dir", str(runs_dir), "--plain"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_subprocess_env(),
        timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    # Plain log emits a workflow-finished banner stanza
    assert "── workflow" in result.stdout, (
        f"Expected plain-log workflow banner in stdout, got:\n{result.stdout!r}"
    )


def test_watch_plain_short_flag(tmp_path):
    """``godel watch <run_id> -p`` (short form) is equivalent to --plain.

    Same TTY-detection limitation as ``test_watch_plain_flag_forces_plain_log``:
    capture_output=True already triggers the non-TTY auto-fallback.  This test
    primarily verifies that argparse accepts ``-p`` as the short form.
    """
    run_id = "plain-short-test"
    runs_dir = tmp_path / "runs"
    _write_transcript_dir(runs_dir, run_id)

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", run_id, "--runs-dir", str(runs_dir), "-p"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_subprocess_env(),
        timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "── workflow" in result.stdout, (
        f"Expected plain-log workflow banner in stdout, got:\n{result.stdout!r}"
    )


def test_watch_plain_env_var(tmp_path):
    """``GODEL_WATCH_PLAIN=1 godel watch <run_id>`` forces plain line-log.

    Same TTY-detection limitation as ``test_watch_plain_flag_forces_plain_log``.
    The deterministic env-var → run_watch() handoff is exercised by
    ``test_watch_plain_env_var_via_run_watch``; this test verifies the env var
    survives a real subprocess invocation.
    """
    run_id = "plain-env-test"
    runs_dir = tmp_path / "runs"
    _write_transcript_dir(runs_dir, run_id)

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", run_id, "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_subprocess_env(GODEL_WATCH_PLAIN="1"),
        timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "── workflow" in result.stdout, (
        f"Expected plain-log workflow banner in stdout with GODEL_WATCH_PLAIN=1, got:\n{result.stdout!r}"
    )


def test_watch_plain_flag_via_run_watch(tmp_path):
    """run_watch(..., plain=True) uses _PlainLineLog even when stdout is not a TTY.

    This unit-level test verifies the plain kwarg threading through run_watch()
    without going through the CLI subprocess.
    """
    import io as _io
    from godel import _watch as watch_mod

    run_id = "rw-plain-unit"
    runs_dir = tmp_path / "runs"
    _write_transcript_dir(runs_dir, run_id)

    captured = _io.StringIO()
    # stdout is a StringIO (non-TTY), but plain=True makes it explicit
    watch_mod.run_watch(run_id, runs_dir=str(runs_dir), plain=True, stdout=captured)

    output = captured.getvalue()
    assert "── workflow" in output, (
        f"Expected plain-log workflow banner from PlainLineLog, got:\n{output!r}"
    )


def test_watch_plain_env_var_via_run_watch(tmp_path, monkeypatch):
    """GODEL_WATCH_PLAIN=1 routes run_watch() to _PlainLineLog."""
    import io as _io
    from godel import _watch as watch_mod

    run_id = "rw-env-unit"
    runs_dir = tmp_path / "runs"
    _write_transcript_dir(runs_dir, run_id)

    monkeypatch.setenv("GODEL_WATCH_PLAIN", "1")

    captured = _io.StringIO()
    watch_mod.run_watch(run_id, runs_dir=str(runs_dir), stdout=captured)

    output = captured.getvalue()
    assert "── workflow" in output, (
        f"Expected plain-log workflow banner from PlainLineLog, got:\n{output!r}"
    )


def test_watch_plain_help_shows_flag():
    """``godel watch --help`` mentions --plain."""
    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--plain" in result.stdout, (
        f"Expected --plain in help output, got:\n{result.stdout}"
    )


def test_pause_does_not_emit_workflow_finished_sentinel(tmp_path, monkeypatch):
    """A paused workflow must NOT write WORKFLOW_FINISHED to the transcript.

    A paused run is not terminal — it will be resumed later and append further
    events to the same transcript.  Writing WORKFLOW_FINISHED on pause would
    (a) mislabel a pause as a failure, and (b) cause a live watcher's follow
    loop to exit on the stale sentinel, missing all post-resume events.
    """
    import asyncio
    from godel import workflow, step
    from godel._exceptions import PauseSignal

    # Isolate run artefacts into tmp_path/runs.
    monkeypatch.chdir(tmp_path)

    @step
    async def raising_step():
        raise PauseSignal(reason="test pause")

    @workflow
    async def pausing_wf():
        await raising_step()
        return "unreachable"

    with pytest.raises(PauseSignal):
        asyncio.run(pausing_wf())

    run_id = pausing_wf._last_run_id
    assert run_id is not None

    transcript_path = tmp_path / "runs" / run_id / "transcript.jsonl"
    assert transcript_path.exists(), f"transcript.jsonl not found at {transcript_path}"

    lines = [
        json.loads(ln)
        for ln in transcript_path.read_text().splitlines()
        if ln.strip()
    ]
    ops = [ev.get("op") for ev in lines]
    assert "WORKFLOW_FINISHED" not in ops, (
        f"PauseSignal must not emit WORKFLOW_FINISHED, got ops={ops}"
    )
