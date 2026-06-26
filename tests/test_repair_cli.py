"""Tests for godel repair CLI command."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from click.testing import CliRunner

PROJECT_ROOT = str(Path(__file__).parent.parent)
sys.path.insert(0, PROJECT_ROOT)

from godel.cli import main  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _make_event(event_id, run_id, seq, op, status, request=None, response=None,
                children_ids=None, step_path=None):
    return {
        "event_id": event_id,
        "run_id": run_id,
        "seq": seq,
        "children_ids": children_ids or [],
        "step_path": step_path or [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": op,
        "request_hash": "",
        "request": request or {},
        "response": response,
        "status": status,
        "ts_start": "2024-01-01T00:00:00+00:00",
        "ts_end": "2024-01-01T00:00:01+00:00",
    }


def _write_run(runs_dir: Path, run_id: str, events: list[dict]) -> Path:
    runs_dir.mkdir(exist_ok=True)
    log_file = runs_dir / f"{run_id}.jsonl"
    with open(log_file, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return log_file


def _make_failed_run(runs_dir: Path, run_id: str = "repair-failed-run") -> str:
    """Create a run in FAILED state."""
    events = [
        _make_event("evt-ws", run_id, 0, "WORKFLOW_STARTED", "STARTED",
                    request={"function": "test"}),
        _make_event("evt-step", run_id, 1, "step.exit", "FAILED",
                    response={
                        "error": "ValueError: bad schema",
                        "error_type": "ValueError",
                        "source_location": "workflow.py:10",
                        "remediation_hint": "Fix the schema",
                    },
                    step_path=["my_step"]),
    ]
    _write_run(runs_dir, run_id, events)
    return run_id


def _make_paused_run(runs_dir: Path, run_id: str = "repair-paused-run") -> str:
    """Create a run in PAUSED state."""
    events = [
        _make_event("evt-ws", run_id, 0, "WORKFLOW_STARTED", "STARTED",
                    request={"function": "test"}),
        _make_event("evt-pause", run_id, 1, "PAUSED", "FINISHED",
                    request={"reason": "CLI pause"}),
    ]
    _write_run(runs_dir, run_id, events)
    return run_id


def _make_finished_run(runs_dir: Path, run_id: str = "repair-finished-run") -> str:
    """Create a run in FINISHED state."""
    events = [
        _make_event("evt-ws", run_id, 0, "WORKFLOW_STARTED", "FINISHED",
                    request={"function": "test"}),
    ]
    _write_run(runs_dir, run_id, events)
    return run_id


def _make_stub_agent(outcome: dict):
    """Return an async function that acts as a @workflow stub returning outcome."""
    async def _agent(ctx, tools, *, model="opus", max_iterations=8):
        return outcome

    # Mark it as a @workflow so the agent_spec validation passes
    _agent._is_workflow = True
    return _agent


def _make_crashing_agent():
    """Return an async function that raises an unexpected exception."""
    async def _agent(ctx, tools, *, model="opus", max_iterations=8):
        raise RuntimeError("agent exploded")

    _agent._is_workflow = True
    return _agent


def _invoke(tmp_path, args, catch_exceptions=False):
    """Invoke the CLI with args, setting cwd to tmp_path.

    Returns a result object with a combined `combined` attribute
    (stdout + stderr) for convenient assertion, plus the original
    `output` (stdout) and `stderr` attributes from CliRunner.
    """
    runner = CliRunner(mix_stderr=False)
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(main, args, catch_exceptions=catch_exceptions)
    finally:
        os.chdir(old_cwd)
    # Attach a convenience attribute merging both streams
    result.combined = (result.output or "") + (result.stderr or "")
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_repair_rejects_finished_run(tmp_path, monkeypatch):
    """A finished run must be rejected with exit 2 and 'refusing to repair'."""
    runs_dir = tmp_path / "runs"
    run_id = _make_finished_run(runs_dir)

    # Use a stub agent so we never hit the real one
    stub = _make_stub_agent({"outcome": "resume", "reason": "done"})
    monkeypatch.setattr(
        "godel.intervention.default_agent.default_intervention_agent", stub
    )

    result = _invoke(tmp_path, ["repair", run_id])
    assert result.exit_code == 2, f"Expected 2, got {result.exit_code}\n{result.combined}"
    assert "refusing to repair" in result.combined


def test_repair_resume_path(tmp_path, monkeypatch):
    """A crashed run with stub agent returning resume exits 0 with godel resume hint."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    stub = _make_stub_agent({"outcome": "resume", "reason": "fixed it"})
    monkeypatch.setattr(
        "godel.intervention.default_agent.default_intervention_agent", stub
    )

    result = _invoke(tmp_path, ["repair", run_id])
    assert result.exit_code == 0, f"Expected 0, got {result.exit_code}\n{result.combined}"
    assert f"godel resume {run_id}" in result.combined


def test_repair_give_up_path(tmp_path, monkeypatch):
    """A crashed run with stub agent returning give_up exits 1."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    stub = _make_stub_agent({"outcome": "give_up", "reason": "cannot fix"})
    monkeypatch.setattr(
        "godel.intervention.default_agent.default_intervention_agent", stub
    )

    result = _invoke(tmp_path, ["repair", run_id])
    assert result.exit_code == 1, f"Expected 1, got {result.exit_code}\n{result.combined}"
    assert "gave up" in result.combined


def test_repair_agent_crash(tmp_path, monkeypatch):
    """An agent that raises an unexpected exception exits 3 and prints traceback."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    crash_agent = _make_crashing_agent()
    monkeypatch.setattr(
        "godel.intervention.default_agent.default_intervention_agent", crash_agent
    )

    # catch_exceptions=True so the CLI's own try/except handles the crash (exit 3)
    result = _invoke(tmp_path, ["repair", run_id], catch_exceptions=True)

    assert result.exit_code == 3, f"Expected 3, got {result.exit_code}\n{result.combined}"
    assert "crashed" in result.combined


def test_repair_custom_agent(tmp_path, monkeypatch):
    """--agent MODULE:FUNCTION loads a custom @workflow agent."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    # Inject a custom module into sys.modules
    mod_name = "_test_custom_repair_agent"
    mod = types.ModuleType(mod_name)

    async def my_agent(ctx, tools, *, model="opus", max_iterations=8):
        return {"outcome": "resume", "reason": "custom agent fixed it"}

    my_agent._is_workflow = True
    mod.my_agent = my_agent
    monkeypatch.setitem(sys.modules, mod_name, mod)

    result = _invoke(tmp_path, ["repair", run_id, "--agent", f"{mod_name}:my_agent"])
    assert result.exit_code == 0, f"Expected 0, got {result.exit_code}\n{result.combined}"
    assert f"godel resume {run_id}" in result.combined


def test_repair_bad_agent_spec_no_colon(tmp_path):
    """--agent without a colon separator exits 2."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    result = _invoke(tmp_path, ["repair", run_id, "--agent", "no_colon"])
    assert result.exit_code == 2, f"Expected 2, got {result.exit_code}\n{result.combined}"
    assert "MODULE:FUNCTION" in result.combined


def test_repair_bad_agent_spec_missing_module(tmp_path):
    """--agent with an unimportable module exits 2."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    result = _invoke(tmp_path, ["repair", run_id, "--agent", "missing.module.xyz:fn"])
    assert result.exit_code == 2, f"Expected 2, got {result.exit_code}\n{result.combined}"
    assert "Failed to import" in result.combined


def test_repair_bad_agent_spec_not_workflow(tmp_path, monkeypatch):
    """--agent pointing to a non-@workflow function exits 2."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    mod_name = "_test_not_workflow_agent"
    mod = types.ModuleType(mod_name)

    def plain_fn(ctx, tools):
        pass  # NOT decorated with @workflow

    mod.plain_fn = plain_fn
    monkeypatch.setitem(sys.modules, mod_name, mod)

    result = _invoke(tmp_path, ["repair", run_id, "--agent", f"{mod_name}:plain_fn"])
    assert result.exit_code == 2, f"Expected 2, got {result.exit_code}\n{result.combined}"
    assert "not a @workflow" in result.combined


def test_repair_ambiguous_prefix(tmp_path, monkeypatch):
    """Two runs sharing a prefix cause exit 1 with 'Ambiguous'."""
    runs_dir = tmp_path / "runs"
    _make_failed_run(runs_dir, run_id="shared-prefix-alpha")
    _make_failed_run(runs_dir, run_id="shared-prefix-beta")

    result = _invoke(tmp_path, ["repair", "shared-prefix"])
    assert result.exit_code == 1, f"Expected 1, got {result.exit_code}\n{result.combined}"
    assert "Ambiguous" in result.combined


def test_repair_dry_run(tmp_path, monkeypatch):
    """--dry-run prints JSON context and exits 0 without invoking the agent."""
    runs_dir = tmp_path / "runs"
    run_id = _make_failed_run(runs_dir)

    # Patch the agent so we detect if it gets called
    called = []

    async def _should_not_call(ctx, tools, **kwargs):
        called.append(True)
        return {"outcome": "resume", "reason": ""}

    _should_not_call._is_workflow = True
    monkeypatch.setattr(
        "godel.intervention.default_agent.default_intervention_agent", _should_not_call
    )

    result = _invoke(tmp_path, ["repair", run_id, "--dry-run"])
    assert result.exit_code == 0, f"Expected 0, got {result.exit_code}\n{result.combined}"
    assert not called, "Agent should not be invoked during --dry-run"
    # stdout should be valid JSON containing the run_id (dry-run outputs to stdout)
    parsed = json.loads(result.output)
    assert parsed["run_id"] == run_id


def test_repair_paused_run_allowed(tmp_path, monkeypatch):
    """A PAUSED run is accepted by the state guard."""
    runs_dir = tmp_path / "runs"
    run_id = _make_paused_run(runs_dir)

    stub = _make_stub_agent({"outcome": "resume", "reason": "unpaused"})
    monkeypatch.setattr(
        "godel.intervention.default_agent.default_intervention_agent", stub
    )

    result = _invoke(tmp_path, ["repair", run_id])
    assert result.exit_code == 0, f"Expected 0, got {result.exit_code}\n{result.combined}"
    assert f"godel resume {run_id}" in result.combined
