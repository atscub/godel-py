"""E2E audit log integration test — M1 exit criterion validation.

Runs a complete workflow exercising all instrumented primitives and verifies
the JSONL audit log is complete and structurally sound.
"""
import asyncio
import json
import io
import sys
from pathlib import Path

from godel import workflow, step, parallel, run
from godel.io import print as godel_print
from godel import det


@workflow
async def sample_workflow():
    """Simplified workflow exercising all instrumented primitives."""

    @step
    async def quality_gates():
        result = await run("echo tests_passed", idempotent=True)
        await godel_print(f"Quality: {result.stdout.strip()}")
        return result.stdout.strip()

    @step
    async def parallel_work():
        @step
        async def branch_a():
            return (await run("echo A", idempotent=True)).stdout.strip()

        @step
        async def branch_b():
            return (await run("echo B", idempotent=True)).stdout.strip()

        return await parallel(branch_a(), branch_b())

    @step
    async def use_det():
        ts = det.now()
        uid = det.uuid4()
        return {"ts": ts, "uuid": uid}

    gates = await quality_gates()
    results = await parallel_work()
    det_vals = await use_det()
    return {"gates": gates, "results": results, "det": det_vals}


def test_full_audit_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    result = asyncio.run(sample_workflow())
    assert result["gates"] == "tests_passed"

    # Verify JSONL file exists
    runs_dir = tmp_path / "runs"
    assert runs_dir.exists()
    jsonl_files = list(runs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1

    # Parse all event snapshots
    lines = jsonl_files[0].read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    assert len(events) > 0

    # Collect unique ops from STARTED events
    started_ops = {e["op"] for e in events if e["status"] == "STARTED"}
    assert "WORKFLOW_STARTED" in started_ops
    assert "step.enter" in started_ops
    assert "run" in started_ops
    assert "print" in started_ops
    assert "FORK" in started_ops
    assert "JOIN" in started_ops
    assert "det.now" in started_ops
    assert "det.uuid4" in started_ops


def test_every_started_has_matching_completion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    asyncio.run(sample_workflow())

    runs_dir = tmp_path / "runs"
    jsonl_files = list(runs_dir.glob("*.jsonl"))
    lines = jsonl_files[0].read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]

    # Group by event_id — each should have STARTED + FINISHED/FAILED
    by_id: dict[str, list] = {}
    for e in events:
        by_id.setdefault(e["event_id"], []).append(e)

    for eid, snapshots in by_id.items():
        statuses = [s["status"] for s in snapshots]
        assert "STARTED" in statuses, f"Event {eid} has no STARTED"
        assert any(s in statuses for s in ("FINISHED", "FAILED")), \
            f"Event {eid} has STARTED but no FINISHED/FAILED"


def test_workflow_started_is_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    asyncio.run(sample_workflow())

    runs_dir = tmp_path / "runs"
    jsonl_files = list(runs_dir.glob("*.jsonl"))
    lines = jsonl_files[0].read_text().strip().split("\n")
    first = json.loads(lines[0])
    assert first["op"] == "WORKFLOW_STARTED"
    assert first["status"] == "STARTED"


def test_fork_join_structure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    asyncio.run(sample_workflow())

    runs_dir = tmp_path / "runs"
    jsonl_files = list(runs_dir.glob("*.jsonl"))
    lines = jsonl_files[0].read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]

    fork_starts = [e for e in events if e["op"] == "FORK" and e["status"] == "STARTED"]
    join_starts = [e for e in events if e["op"] == "JOIN" and e["status"] == "STARTED"]
    assert len(fork_starts) >= 1
    assert len(join_starts) >= 1
    # JOIN references its FORK
    for j in join_starts:
        assert "fork_id" in j["request"]
        fork_ids = {f["event_id"] for f in fork_starts}
        assert j["request"]["fork_id"] in fork_ids


def test_godel_show_works(tmp_path, monkeypatch):
    """Verify godel show can display the produced audit log."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    asyncio.run(sample_workflow())

    run_id = sample_workflow._last_run_id
    from click.testing import CliRunner
    from godel.cli import main as cli_main
    runner = CliRunner()

    # Test basic show
    result = runner.invoke(cli_main, ["show", run_id])
    assert result.exit_code == 0
    assert "WORKFLOW_STARTED" in result.output

    # Test show --graph
    result = runner.invoke(cli_main, ["show", run_id, "--graph"])
    assert result.exit_code == 0
    assert "WORKFLOW_STARTED" in result.output
