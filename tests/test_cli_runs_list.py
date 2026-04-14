"""Tests for `godel runs list` CLI command."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from godel.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_run(
    runs_dir: Path,
    run_id: str,
    workflow_name: str,
    wf_status: str,  # status for WORKFLOW_STARTED event
    ts_start: str = "2026-04-14T09:00:00+00:00",
    ts_end: str | None = None,
    extra_events: list[dict] | None = None,
) -> None:
    """Write a minimal JSONL file for a synthetic run."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    events = []
    wf_event = {
        "event_id": f"{run_id}-wf",
        "run_id": run_id,
        "seq": 0,
        "children_ids": [],
        "step_path": [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": "WORKFLOW_STARTED",
        "request_hash": "",
        "request": {"function": workflow_name},
        "response": None,
        "status": wf_status,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "stream_path": [],
    }
    events.append(wf_event)
    if extra_events:
        events.extend(extra_events)
    with open(runs_dir / f"{run_id}.jsonl", "w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _make_step_event(run_id, status, ts_start="2026-04-14T09:00:01+00:00", ts_end=None):
    return {
        "event_id": f"{run_id}-step",
        "run_id": run_id,
        "seq": 1,
        "children_ids": [],
        "step_path": ["my_step"],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": "step.exit",
        "request_hash": "",
        "request": {},
        "response": None,
        "status": status,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "stream_path": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_list_all_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-aaa", "deploy_pipe", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:03:42+00:00")
    _write_run(runs_dir, "run-bbb", "pr_review", "STARTED",
               ts_start="2026-04-14T08:30:00+00:00",
               extra_events=[_make_step_event("run-bbb", "FAILED")])
    _write_run(runs_dir, "run-ccc", "research", "STARTED",
               ts_start="2026-04-14T07:45:00+00:00")

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0, result.output
    assert "deploy_pipe" in result.output
    assert "pr_review" in result.output
    assert "research" in result.output
    # All three runs present
    assert result.output.count("run-") >= 3


def test_list_sorted_most_recent_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-early", "wf_early", "FINISHED",
               ts_start="2026-04-13T06:00:00+00:00", ts_end="2026-04-13T06:01:00+00:00")
    _write_run(runs_dir, "run-late", "wf_late", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:01:00+00:00")

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert result.output.index("run-late") < result.output.index("run-early")


def test_list_status_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-fin", "wf1", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:01:00+00:00")
    _write_run(runs_dir, "run-fail", "wf2", "STARTED",
               ts_start="2026-04-14T08:00:00+00:00",
               extra_events=[_make_step_event("run-fail", "FAILED")])

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir), "--status=failed"])
    assert result.exit_code == 0
    assert "run-fail" in result.output
    assert "run-fin" not in result.output


def test_list_status_filter_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-fin", "wf1", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:01:00+00:00")

    runner = CliRunner()
    r1 = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir), "--status=FINISHED"])
    r2 = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir), "--status=finished"])
    assert r1.exit_code == 0
    assert r2.exit_code == 0
    assert "run-fin" in r1.output
    assert "run-fin" in r2.output


def test_list_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    for i in range(5):
        ts = f"2026-04-14T0{i}:00:00+00:00"
        _write_run(runs_dir, f"run-{i:03d}", f"wf_{i}", "FINISHED",
                   ts_start=ts, ts_end=ts.replace(":00:00", ":01:00"))

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir), "--limit=2"])
    assert result.exit_code == 0
    # Only 2 data rows (excluding header + separator)
    data_lines = [
        ln for ln in result.output.splitlines()
        if "run-" in ln
    ]
    assert len(data_lines) == 2


def test_list_empty_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert "RUN ID" in result.output  # header present
    # No data rows
    data_lines = [ln for ln in result.output.splitlines() if "run-" in ln]
    assert len(data_lines) == 0


def test_list_no_runs_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nonexistent = tmp_path / "no_such_dir"

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(nonexistent)])
    assert result.exit_code != 0


def test_list_malformed_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    # Write a corrupt JSONL file
    (runs_dir / "run-bad.jsonl").write_text("not json\n{broken\n")

    # Write a good run
    _write_run(runs_dir, "run-good", "wf_good", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:01:00+00:00")

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert "run-good" in result.output
    assert "run-bad" in result.output  # row present but UNKNOWN status


def test_list_duration_format_finished(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-timed", "wf1", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:03:42+00:00")

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert "3m" in result.output  # 3 minutes 42 seconds


def test_list_duration_format_short(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-quick", "wf1", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:00:45+00:00")

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert "45s" in result.output


def test_list_workflow_name_extraction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-xyz", "my_custom_workflow", "FINISHED",
               ts_start="2026-04-14T09:00:00+00:00", ts_end="2026-04-14T09:00:10+00:00")

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert "my_custom_workflow" in result.output


def test_list_combined_filters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    for i in range(3):
        ts = f"2026-04-14T0{i}:00:00+00:00"
        ts_end = f"2026-04-14T0{i}:01:00+00:00"
        _write_run(runs_dir, f"run-fin-{i}", f"wf_{i}", "FINISHED",
                   ts_start=ts, ts_end=ts_end)
    _write_run(runs_dir, "run-fail", "wf_fail", "STARTED",
               ts_start="2026-04-14T05:00:00+00:00",
               extra_events=[_make_step_event("run-fail", "FAILED")])

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["runs", "list", "--runs-dir", str(runs_dir), "--status=finished", "--limit=1"],
    )
    assert result.exit_code == 0
    data_lines = [ln for ln in result.output.splitlines() if "run-" in ln]
    assert len(data_lines) == 1
    assert "FINISHED" in result.output


def test_list_paused_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "run-paused", "wf_paused", "PAUSED",
               ts_start="2026-04-14T09:00:00+00:00")

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir), "--status=paused"])
    assert result.exit_code == 0
    assert "run-paused" in result.output


def test_list_header_always_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert "RUN ID" in result.output
    assert "WORKFLOW" in result.output
    assert "STATUS" in result.output
    assert "STARTED" in result.output
    assert "DURATION" in result.output
