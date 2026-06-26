"""Tests for opt-in idempotency:
  - @step(idempotent=True) lets resume re-execute STARTED-only steps
  - agent(prompt, assume_idempotent=True) per-call hint
  - godel resume --assume-idempotent global override with WARNING
  - default behavior unchanged (non-idempotent raises UnsafeResumeError)
"""
from __future__ import annotations

import asyncio
import pytest

from godel._context import WorkflowContext, _current_workflow, _step_idempotent
from godel._event_log import EventLog
from godel._replay import (
    ReplayWalker,
    set_assume_idempotent_all,
    get_assume_idempotent_all,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log_with_started_run(tmp_path, cmd: str = "echo hello", idempotent: bool = False) -> EventLog:
    """Create an EventLog with a single STARTED (not FINISHED) run() event."""
    run_id = "test-idempotent-run"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    log.emit_started(
        op="run",
        step_path=("my_step",),
        request={"cmd": cmd, "cwd": None, "timeout": None, "idempotent": idempotent},
        invocation_seq=0,
        step_local_seq=0,
    )
    log.close()
    return EventLog.load(run_id, runs_dir=str(tmp_path))


def _install_replay_ctx(loaded_log: EventLog) -> WorkflowContext:
    """Build a WorkflowContext with a ReplayWalker and install it."""
    walker = ReplayWalker(loaded_log)
    ctx = WorkflowContext(
        run_id=loaded_log._run_id,
        step_stack=["my_step"],
        event_log=loaded_log,
        replay_walker=walker,
    )
    ctx._invocation_counts[("my_step",)] = 1
    ctx._step_local_seq[("my_step",)] = 0
    _current_workflow.set(ctx)
    return ctx


@pytest.fixture(autouse=True)
def _cleanup():
    """Reset global state after each test."""
    yield
    _current_workflow.set(None)
    set_assume_idempotent_all(False)
    # Reset _step_idempotent contextvar
    token = _step_idempotent.set(False)
    _step_idempotent.reset(token)


# ---------------------------------------------------------------------------
# Default behavior unchanged: non-idempotent STARTED raises UnsafeResumeError
# ---------------------------------------------------------------------------

class TestDefaultBehaviorUnchanged:
    def test_started_only_raises_without_idempotent(self, tmp_path):
        from godel._run import run
        from godel._exceptions import UnsafeResumeError

        loaded = _make_log_with_started_run(tmp_path, idempotent=False)
        _install_replay_ctx(loaded)

        with pytest.raises(UnsafeResumeError):
            asyncio.run(run("echo hello"))

    def test_finished_returns_cached_regardless(self, tmp_path):
        """FINISHED events always replay, idempotent flag doesn't matter."""
        from godel._run import run

        run_id = "test-finished"
        log = EventLog(run_id, runs_dir=str(tmp_path))
        started = log.emit_started(
            op="run",
            step_path=("my_step",),
            request={"cmd": "echo hello", "cwd": None, "timeout": None, "idempotent": False},
            invocation_seq=0,
            step_local_seq=0,
        )
        log.emit_finished(started.event_id, response={"stdout": "hello\n", "stderr": "", "returncode": 0})
        log.close()

        loaded = EventLog.load(run_id, runs_dir=str(tmp_path))
        walker = ReplayWalker(loaded)
        ctx = WorkflowContext(
            run_id=run_id,
            step_stack=["my_step"],
            event_log=loaded,
            replay_walker=walker,
        )
        ctx._invocation_counts[("my_step",)] = 1
        ctx._step_local_seq[("my_step",)] = 0
        _current_workflow.set(ctx)

        result = asyncio.run(run("echo hello"))
        assert result.stdout == "hello\n"


# ---------------------------------------------------------------------------
# Layer 1: run() per-call idempotent=True (existing behavior, verify unchanged)
# ---------------------------------------------------------------------------

class TestRunPerCallIdempotent:
    def test_idempotent_run_executes_on_started(self, tmp_path):
        from godel._run import run

        loaded = _make_log_with_started_run(tmp_path, cmd="echo safe", idempotent=True)
        _install_replay_ctx(loaded)

        result = asyncio.run(run("echo safe", idempotent=True))
        assert "safe" in result.stdout

    def test_non_idempotent_run_raises_on_started(self, tmp_path):
        from godel._run import run
        from godel._exceptions import UnsafeResumeError

        loaded = _make_log_with_started_run(tmp_path, cmd="rm -rf /", idempotent=False)
        _install_replay_ctx(loaded)

        with pytest.raises(UnsafeResumeError):
            asyncio.run(run("rm -rf /"))


# ---------------------------------------------------------------------------
# Layer 2: @step(idempotent=True) propagates to child run() calls
# ---------------------------------------------------------------------------

class TestStepIdempotentPropagation:
    def test_step_idempotent_true_sets_contextvar(self, tmp_path):
        """@step(idempotent=True) should set _step_idempotent during execution."""
        from godel._decorators import step, workflow

        captured = []

        @step(idempotent=True)
        async def idempotent_step():
            captured.append(_step_idempotent.get())

        @workflow
        async def wf():
            await idempotent_step()

        asyncio.run(wf())
        assert captured == [True]

    def test_step_idempotent_false_leaves_contextvar_false(self, tmp_path):
        """@step without idempotent=True should leave _step_idempotent False."""
        from godel._decorators import step, workflow

        captured = []

        @step
        async def normal_step():
            captured.append(_step_idempotent.get())

        @workflow
        async def wf():
            await normal_step()

        asyncio.run(wf())
        assert captured == [False]

    def test_step_idempotent_contextvar_reset_after_step(self):
        """_step_idempotent should be False outside any step."""
        from godel._decorators import step, workflow

        @step(idempotent=True)
        async def idempotent_step():
            pass

        @workflow
        async def wf():
            await idempotent_step()
            # After step exits, contextvar should be False again
            assert _step_idempotent.get() is False

        asyncio.run(wf())

    def test_step_idempotent_allows_run_on_started(self, tmp_path):
        """When @step(idempotent=True) wraps a run() with STARTED-only entry, no error."""
        from godel._run import run
        from godel._context import _current_workflow

        # Build a log with a step.enter STARTED and a run STARTED inside it
        run_id = "test-step-idempotent"
        log = EventLog(run_id, runs_dir=str(tmp_path))
        step_ev = log.emit_started(
            op="step.enter",
            step_path=("safe_step",),
            request={"name": "safe_step", "args": "()", "kwargs": "{}", "source_hash": ""},
            invocation_seq=0,
            step_local_seq=0,
        )
        log.emit_finished(step_ev.event_id, response={"result": "None"})
        log.close()

        # We can't easily do a full end-to-end replay test without the workflow,
        # so test the contextvar interaction directly:
        # If _step_idempotent is True, run() with STARTED-only should not raise.
        loaded = _make_log_with_started_run(tmp_path / "run_log", cmd="echo ok", idempotent=False)

        walker = ReplayWalker(loaded)
        ctx = WorkflowContext(
            run_id=loaded._run_id,
            step_stack=["my_step"],
            event_log=loaded,
            replay_walker=walker,
        )
        ctx._invocation_counts[("my_step",)] = 1
        ctx._step_local_seq[("my_step",)] = 0
        _current_workflow.set(ctx)

        # Set _step_idempotent as the @step decorator would
        token = _step_idempotent.set(True)
        try:
            # Should NOT raise UnsafeResumeError because _step_idempotent is True
            result = asyncio.run(run("echo ok"))
            assert "ok" in result.stdout
        finally:
            _step_idempotent.reset(token)

    def test_step_idempotent_false_still_raises(self, tmp_path):
        """When @step(idempotent=False), run() with STARTED-only still raises."""
        from godel._run import run
        from godel._exceptions import UnsafeResumeError

        loaded = _make_log_with_started_run(tmp_path, cmd="echo ok", idempotent=False)
        _install_replay_ctx(loaded)

        # _step_idempotent is False (default), so should raise
        with pytest.raises(UnsafeResumeError):
            asyncio.run(run("echo ok"))


# ---------------------------------------------------------------------------
# Layer 2: agent(prompt, assume_idempotent=True) per-call hint
# ---------------------------------------------------------------------------

class TestAgentAssumeIdempotent:
    def test_assume_idempotent_sets_step_idempotent_contextvar(self):
        """agent(assume_idempotent=True) should set _step_idempotent during execution."""
        from unittest.mock import patch
        from godel.agents._claude import claude_code
        from godel._decorators import workflow
        from godel._run import CommandResult
        import json

        captured_idempotent = []

        async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
            captured_idempotent.append(_step_idempotent.get())
            return CommandResult(
                stdout=json.dumps({"result": "hello"}),
                stderr="",
                returncode=0,
            )

        @workflow
        async def wf():
            with patch("godel.agents._common.run", new=fake_run):
                agent = claude_code()
                await agent("say hi", assume_idempotent=True)

        asyncio.run(wf())
        assert captured_idempotent == [True], f"Expected [True], got {captured_idempotent}"

    def test_assume_idempotent_false_does_not_set_contextvar(self):
        """agent(assume_idempotent=False) should NOT set _step_idempotent."""
        from unittest.mock import patch
        from godel.agents._claude import claude_code
        from godel._decorators import workflow
        from godel._run import CommandResult
        import json

        captured_idempotent = []

        async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
            captured_idempotent.append(_step_idempotent.get())
            return CommandResult(
                stdout=json.dumps({"result": "hello"}),
                stderr="",
                returncode=0,
            )

        @workflow
        async def wf():
            with patch("godel.agents._common.run", new=fake_run):
                agent = claude_code()
                await agent("say hi")  # no assume_idempotent

        asyncio.run(wf())
        assert captured_idempotent == [False], f"Expected [False], got {captured_idempotent}"

    def test_assume_idempotent_resets_after_call(self):
        """_step_idempotent should be restored after agent call completes."""
        from unittest.mock import patch
        from godel.agents._claude import claude_code
        from godel._decorators import workflow
        from godel._run import CommandResult
        import json

        after_call = []

        async def fake_run(cmd, *, cwd=None, timeout=None, idempotent=False):
            return CommandResult(
                stdout=json.dumps({"result": "hello"}),
                stderr="",
                returncode=0,
            )

        @workflow
        async def wf():
            with patch("godel.agents._common.run", new=fake_run):
                agent = claude_code()
                await agent("say hi", assume_idempotent=True)
            after_call.append(_step_idempotent.get())

        asyncio.run(wf())
        assert after_call == [False], f"_step_idempotent not reset after call: {after_call}"


# ---------------------------------------------------------------------------
# Layer 3: Global assume-idempotent-all flag
# ---------------------------------------------------------------------------

class TestAssumeIdempotentAll:
    def test_get_default_is_false(self):
        assert get_assume_idempotent_all() is False

    def test_set_and_get(self):
        set_assume_idempotent_all(True)
        assert get_assume_idempotent_all() is True
        set_assume_idempotent_all(False)
        assert get_assume_idempotent_all() is False

    def test_global_override_allows_run_on_started(self, tmp_path):
        """set_assume_idempotent_all(True) lets run() proceed on STARTED-only."""
        from godel._run import run

        loaded = _make_log_with_started_run(tmp_path, cmd="echo global", idempotent=False)
        _install_replay_ctx(loaded)

        set_assume_idempotent_all(True)
        result = asyncio.run(run("echo global"))
        assert "global" in result.stdout

    def test_global_override_false_still_raises(self, tmp_path):
        """set_assume_idempotent_all(False) does NOT bypass UnsafeResumeError."""
        from godel._run import run
        from godel._exceptions import UnsafeResumeError

        loaded = _make_log_with_started_run(tmp_path, cmd="echo global", idempotent=False)
        _install_replay_ctx(loaded)

        set_assume_idempotent_all(False)
        with pytest.raises(UnsafeResumeError):
            asyncio.run(run("echo global"))

    def test_global_override_reset_after_use(self, tmp_path):
        """Global flag is properly isolated between tests (via fixture)."""
        assert get_assume_idempotent_all() is False  # should be reset by fixture


# ---------------------------------------------------------------------------
# Layer 3: CLI --assume-idempotent sets global flag and emits WARNING
# ---------------------------------------------------------------------------

class TestCliAssumeIdempotentFlag:
    def test_resume_assume_idempotent_sets_global_flag(self, tmp_path):
        """Verify set_assume_idempotent_all(True) is called when flag is set."""
        # We test the logic indirectly: set_assume_idempotent_all is a module-level
        # function that the CLI calls. We verify it works correctly in isolation.
        from godel._replay import set_assume_idempotent_all, get_assume_idempotent_all

        set_assume_idempotent_all(True)
        assert get_assume_idempotent_all() is True
        set_assume_idempotent_all(False)
        assert get_assume_idempotent_all() is False

    def test_resume_cmd_has_assume_idempotent_option(self):
        """Verify the CLI command registers the --assume-idempotent option."""
        from click.testing import CliRunner
        from godel.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["resume", "--help"])
        assert result.exit_code == 0
        assert "--assume-idempotent" in result.output

    def test_resume_cmd_emits_warning_when_flag_set(self, tmp_path):
        """--assume-idempotent should emit a WARNING and properly set/reset the global flag."""
        import subprocess
        import sys
        from pathlib import Path
        import textwrap

        # The worktree root (parent of tests/)
        worktree_root = str(Path(__file__).parent.parent)

        # Build a minimal workflow file
        wf_file = tmp_path / "wf.py"
        wf_file.write_text(textwrap.dedent("""
            from godel import workflow

            @workflow
            async def my_wf():
                pass
        """))

        # First run the workflow normally to create a log.
        # Use PYTHONPATH to ensure worktree godel is used, not installed.
        env = {**__import__("os").environ, "PYTHONPATH": worktree_root}
        result = subprocess.run(
            [sys.executable, "-m", "godel", "run", "--no-strict", "--no-lint", str(wf_file)],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(tmp_path),
            env=env,
        )
        assert result.returncode == 0, f"Initial run failed: {result.stderr}"

        # Extract run_id
        run_id = None
        for line in result.stderr.strip().split("\n"):
            if line.startswith("[godel] run ") and "completed" not in line and "resume" not in line:
                run_id = line.split("run ")[1].strip()
                break
        assert run_id is not None, f"Could not find run_id in: {result.stderr}"

        # Resume with --assume-idempotent and check WARNING is emitted
        result2 = subprocess.run(
            [sys.executable, "-m", "godel", "resume",
             "--no-strict", "--no-lint", "--assume-idempotent",
             run_id[:8], str(wf_file)],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(tmp_path),
            env=env,
        )
        combined = result2.stdout + result2.stderr
        assert "WARNING" in combined, f"Expected WARNING in output, got: {combined!r}"
        assert "assume-idempotent" in combined.lower(), (
            f"Expected 'assume-idempotent' in output, got: {combined!r}"
        )
