"""Tests for godel tail CLI command."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from godel.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event_dict(
    event_id: str,
    op: str,
    status: str = "STARTED",
    run_id: str = "test-run",
    seq: int = 0,
    step_path: list | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "run_id": run_id,
        "seq": seq,
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
        "ts_end": "2026-01-01T00:00:01+00:00" if status in ("FINISHED", "FAILED") else None,
    }


def _write_test_jsonl(runs_dir: Path, run_id: str, events: list[dict]) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run_id}.jsonl"
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tail_pretty_output(tmp_path, monkeypatch):
    """Default pretty format contains event op and status strings."""
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event_dict("EVT0001", "WORKFLOW_STARTED", status="FINISHED"),
    ]
    _write_test_jsonl(tmp_path / "runs", "tail-run-pretty", events)

    runner = CliRunner()
    result = runner.invoke(main, ["tail", "tail-run-pretty", "--no-follow"])
    assert result.exit_code == 0, result.output
    assert "WORKFLOW_STARTED" in result.output
    assert "FINISHED" in result.output


def test_tail_json_output(tmp_path, monkeypatch):
    """--format json emits valid JSON objects per line."""
    monkeypatch.chdir(tmp_path)
    # Put the terminal WORKFLOW_STARTED event LAST so stop_on_terminal doesn't
    # cut short before EVT0003 is emitted.
    events = [
        _make_event_dict("EVT0003", "step.enter", status="FINISHED", seq=0),
        _make_event_dict("EVT0002", "WORKFLOW_STARTED", status="FINISHED", seq=1),
    ]
    _write_test_jsonl(tmp_path / "runs", "tail-run-json", events)

    runner = CliRunner()
    result = runner.invoke(main, ["tail", "tail-run-json", "--no-follow", "--format", "json"])
    assert result.exit_code == 0, result.output

    lines = [ln for ln in result.output.strip().splitlines() if ln]
    assert len(lines) == 2
    for line in lines:
        d = json.loads(line)
        assert "event_id" in d
        assert "op" in d


def test_tail_no_follow_exits_at_eof(tmp_path, monkeypatch):
    """--no-follow exits after reading existing events."""
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event_dict("EVT0010", "step.enter", status="STARTED"),
        _make_event_dict("EVT0011", "step.enter", status="FINISHED", seq=1),
    ]
    _write_test_jsonl(tmp_path / "runs", "tail-run-nofollow", events)

    runner = CliRunner()
    result = runner.invoke(main, ["tail", "tail-run-nofollow", "--no-follow"])
    assert result.exit_code == 0
    assert "EVT0010"[:8] in result.output
    assert "EVT0011"[:8] in result.output


def test_tail_ambiguous_prefix_exits_1(tmp_path, monkeypatch):
    """Ambiguous prefix produces exit code 1 and error message."""
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "shared-abc.jsonl").write_text("")
    (runs_dir / "shared-xyz.jsonl").write_text("")

    runner = CliRunner()
    result = runner.invoke(main, ["tail", "shared", "--no-follow"])
    assert result.exit_code != 0
    assert "Ambiguous" in result.output or "Ambiguous" in (result.exception and str(result.exception) or "")


def test_tail_missing_run_with_no_wait_exits_1(tmp_path, monkeypatch):
    """--no-wait exits 1 when no matching run file exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()

    runner = CliRunner()
    result = runner.invoke(main, ["tail", "nonexistent", "--no-wait", "--no-follow"])
    assert result.exit_code == 1
    assert 'No run matching "nonexistent"' in result.output or \
           'No run matching "nonexistent"' in result.stderr


def test_tail_stop_on_terminal(tmp_path, monkeypatch):
    """Tail stops after WORKFLOW_STARTED FINISHED; trailing event not shown."""
    monkeypatch.chdir(tmp_path)
    events = [
        _make_event_dict("EVT0020", "WORKFLOW_STARTED", status="FINISHED"),
        # This event comes after terminal — should not appear
        _make_event_dict("EVT0021", "step.enter", status="FINISHED", seq=1),
    ]
    _write_test_jsonl(tmp_path / "runs", "tail-run-stop", events)

    runner = CliRunner()
    result = runner.invoke(main, ["tail", "tail-run-stop", "--no-follow"])
    assert result.exit_code == 0
    # EVT0020 should appear; EVT0021 should not (iterator stopped after terminal)
    assert "WORKFLOW_STARTED" in result.output
    assert "EVT0021"[:8] not in result.output


def test_tail_export():
    """godel.tail is exported from the top-level package."""
    import godel
    assert hasattr(godel, "tail")
    assert callable(godel.tail)
