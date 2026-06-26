"""Tests for godel show CLI command."""
import json
from pathlib import Path
from click.testing import CliRunner
from godel.cli import main


def _write_test_jsonl(runs_dir: Path, run_id: str, events: list[dict]):
    """Write test JSONL file."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    with open(runs_dir / f"{run_id}.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _make_event(event_id, op, status="STARTED", step_path=None, **kwargs):
    return {
        "event_id": event_id,
        "run_id": "test-run",
        "seq": 0,
        "children_ids": [],
        "step_path": step_path or [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": op,
        "request_hash": "",
        "request": {},
        "response": None,
        "status": status,
        "ts_start": "2026-01-01T00:00:00+00:00",
        "ts_end": "2026-01-01T00:00:01+00:00" if status == "FINISHED" else None,
        **kwargs,
    }


def test_show_displays_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event("AAAA0001", "WORKFLOW_STARTED"),
        _make_event("AAAA0001", "WORKFLOW_STARTED", status="FINISHED"),
        _make_event("BBBB0002", "step.enter", step_path=["quality_gates"]),
        _make_event("BBBB0002", "step.enter", status="FINISHED", step_path=["quality_gates"]),
    ]
    _write_test_jsonl(tmp_path / "runs", "test-run-123", events)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "test-run-123"])
    assert result.exit_code == 0
    assert "WORKFLOW_STARTED" in result.output
    assert "step.enter" in result.output


def test_show_prefix_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    events = [_make_event("AAAA0001", "WORKFLOW_STARTED", status="FINISHED")]
    _write_test_jsonl(tmp_path / "runs", "test-run-123", events)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "test-run"])
    assert result.exit_code == 0
    assert "WORKFLOW_STARTED" in result.output


def test_show_missing_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["show", "nonexistent"])
    assert result.exit_code != 0


def test_show_ambiguous_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    events = [_make_event("AAAA0001", "WORKFLOW_STARTED")]
    _write_test_jsonl(runs_dir, "run-aaa", events)
    _write_test_jsonl(runs_dir, "run-aab", events)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "run-aa"])
    assert result.exit_code != 0
    assert "Ambiguous" in result.output


def test_show_no_runs_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "anything"])
    assert result.exit_code != 0
    assert "No runs" in result.output


def test_show_graph_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event("AAAA0001", "WORKFLOW_STARTED", status="FINISHED"),
        _make_event("BBBB0002", "step.enter", status="FINISHED", step_path=["s1"]),
    ]
    _write_test_jsonl(tmp_path / "runs", "test-run-123", events)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "test-run-123", "--graph"])
    assert result.exit_code == 0
    assert "WORKFLOW_STARTED" in result.output
    assert "\u2713" in result.output


def test_show_hides_retried_failures(tmp_path, monkeypatch):
    """Default view filters out FAILED events that were later retried."""
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event("AAA1", "WORKFLOW_STARTED", status="FAILED"),
        _make_event("AAA1", "WORKFLOW_STARTED", status="FAILED"),
        _make_event("BBB1", "step.enter", status="FAILED", step_path=["work"]),
        _make_event("BBB1", "step.enter", status="FAILED", step_path=["work"]),
        _make_event("CCC1", "step.enter", status="FINISHED", step_path=["work"]),
        _make_event("DDD1", "WORKFLOW_STARTED", status="FINISHED"),
    ]
    _write_test_jsonl(tmp_path / "runs", "retry-run", events)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "retry-run"])
    assert result.exit_code == 0
    # FAILED should not appear in default view
    assert "FAILED" not in result.output
    assert "FINISHED" in result.output
    # Only one WORKFLOW_STARTED
    assert result.output.count("WORKFLOW_STARTED") == 1


def test_show_all_flag(tmp_path, monkeypatch):
    """--all flag shows retries grouped."""
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event("AAA1", "step.enter", status="FAILED", step_path=["work"]),
        _make_event("AAA1", "step.enter", status="FAILED", step_path=["work"]),
        _make_event("BBB1", "step.enter", status="FINISHED", step_path=["work"]),
    ]
    _write_test_jsonl(tmp_path / "runs", "all-run", events)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "all-run", "--all"])
    assert result.exit_code == 0
    assert "prior attempt" in result.output
    assert "succeeded" in result.output


def test_show_all_graph_flag(tmp_path, monkeypatch):
    """--all --graph shows retries grouped in graph view."""
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event("AAA1", "step.enter", status="FAILED", step_path=["work"]),
        _make_event("AAA1", "step.enter", status="FAILED", step_path=["work"]),
        _make_event("BBB1", "step.enter", status="FINISHED", step_path=["work"]),
    ]
    _write_test_jsonl(tmp_path / "runs", "all-graph-run", events)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "all-graph-run", "--all", "--graph"])
    assert result.exit_code == 0
    assert "prior attempt" in result.output
