"""Tests for 'godel run FILE -- args' and resume arg recovery (awl-0pd)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).parent.parent)
PYTHON = sys.executable


def _run_godel(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "godel"] + args,
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )


# ---------------------------------------------------------------------------
# Fixtures: workflow files written at test time
# ---------------------------------------------------------------------------

POSITIONAL_WORKFLOW = '''\
import json
from godel import workflow

@workflow
async def wf(a, b, c):
    with open("out.json", "w") as f:
        json.dump({"a": a, "b": b, "c": c}, f)
'''

KWARGS_WORKFLOW = '''\
import json
from godel import workflow

@workflow
async def wf(x, y):
    with open("out.json", "w") as f:
        json.dump({"x": x, "y": y}, f)
'''

MIXED_WORKFLOW = '''\
import json
from godel import workflow

@workflow
async def wf(a, b, x="default"):
    with open("out.json", "w") as f:
        json.dump({"a": a, "b": b, "x": x}, f)
'''

NO_ARG_WORKFLOW = '''\
import json
from godel import workflow

@workflow
async def wf():
    with open("out.json", "w") as f:
        json.dump({"called": True}, f)
'''

# Alias: identical fixture, kept as a single constant (NIT-1 fix)
ZERO_ARG_WORKFLOW = NO_ARG_WORKFLOW


# ---------------------------------------------------------------------------
# parse_workflow_args unit tests
# ---------------------------------------------------------------------------

def test_parse_workflow_args_positional_only():
    from godel.cli import parse_workflow_args
    args, kwargs = parse_workflow_args(("a", "b", "c"))
    assert args == ["a", "b", "c"]
    assert kwargs == {}


def test_parse_workflow_args_kwargs_only():
    from godel.cli import parse_workflow_args
    args, kwargs = parse_workflow_args(("x=1", "y=hello"))
    assert args == []
    assert kwargs == {"x": "1", "y": "hello"}


def test_parse_workflow_args_mixed():
    from godel.cli import parse_workflow_args
    args, kwargs = parse_workflow_args(("a", "b", "x=1"))
    assert args == ["a", "b"]
    assert kwargs == {"x": "1"}


def test_parse_workflow_args_empty():
    from godel.cli import parse_workflow_args
    args, kwargs = parse_workflow_args(())
    assert args == []
    assert kwargs == {}


def test_parse_workflow_args_first_equals_split():
    """q=a=b → key='q', value='a=b'."""
    from godel.cli import parse_workflow_args
    args, kwargs = parse_workflow_args(("q=a=b",))
    assert args == []
    assert kwargs == {"q": "a=b"}


def test_parse_workflow_args_empty_value():
    """x= → kwargs['x'] = ''."""
    from godel.cli import parse_workflow_args
    args, kwargs = parse_workflow_args(("x=",))
    assert kwargs == {"x": ""}


def test_parse_workflow_args_non_identifier_lhs():
    """'1=foo' → treated as positional (LHS not a valid identifier)."""
    from godel.cli import parse_workflow_args
    args, kwargs = parse_workflow_args(("1=foo",))
    assert args == ["1=foo"]
    assert kwargs == {}


def test_parse_workflow_args_duplicate_key_raises():
    """Duplicate kwarg key must raise ValueError."""
    from godel.cli import parse_workflow_args
    with pytest.raises(ValueError, match="Duplicate"):
        parse_workflow_args(("x=1", "x=2"))


# ---------------------------------------------------------------------------
# Integration: godel run FILE -- args
# ---------------------------------------------------------------------------

def test_run_positional_args(tmp_path):
    """godel run FILE -- a b c passes positional args to the workflow."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(POSITIONAL_WORKFLOW)

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file), "--", "hello", "world", "42"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"Expected exit 0: {result.stderr}"
    out = json.loads((tmp_path / "out.json").read_text())
    assert out == {"a": "hello", "b": "world", "c": "42"}


def test_run_kwargs_only(tmp_path):
    """godel run FILE -- x=foo y=bar passes keyword args."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(KWARGS_WORKFLOW)

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file), "--", "x=foo", "y=bar"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"Expected exit 0: {result.stderr}"
    out = json.loads((tmp_path / "out.json").read_text())
    assert out == {"x": "foo", "y": "bar"}


def test_run_mixed_args(tmp_path):
    """godel run FILE -- a b x=1 passes both positional and keyword args."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(MIXED_WORKFLOW)

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file), "--", "hello", "world", "x=override"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"Expected exit 0: {result.stderr}"
    out = json.loads((tmp_path / "out.json").read_text())
    assert out == {"a": "hello", "b": "world", "x": "override"}


def test_run_no_args_regression(tmp_path):
    """godel run FILE with no trailing '--' still works (regression)."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(NO_ARG_WORKFLOW)

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file)],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"Expected exit 0: {result.stderr}"
    out = json.loads((tmp_path / "out.json").read_text())
    assert out == {"called": True}


def test_run_arity_mismatch(tmp_path):
    """Workflow takes 0 args but user passes extra → exit 2, clean error, no resume hint."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(ZERO_ARG_WORKFLOW)

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file), "--", "unexpected"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 2, f"Expected exit 2: {result.stderr}"
    assert "[godel] argument error:" in result.stderr
    assert "Traceback" not in result.stderr
    assert "resume with:" not in result.stderr


def test_run_missing_required_arg(tmp_path):
    """Workflow requires args but none supplied → exit 2, clean error, no resume hint."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(KWARGS_WORKFLOW)  # requires x and y

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file)],
        cwd=str(tmp_path),
    )
    assert result.returncode == 2, f"Expected exit 2: {result.stderr}"
    assert "[godel] argument error:" in result.stderr
    assert "Traceback" not in result.stderr
    assert "resume with:" not in result.stderr


def test_run_duplicate_kwarg_error(tmp_path):
    """Duplicate kwarg key → exit 2 with clear error message."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(KWARGS_WORKFLOW)

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file), "--", "x=1", "x=2"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 2, f"Expected exit 2: {result.stderr}"
    assert "[godel] argument error:" in result.stderr
    assert "Duplicate" in result.stderr


def test_run_workflow_started_contains_structured_args(tmp_path):
    """WORKFLOW_STARTED event stores args as list and kwargs as dict (not repr strings)."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(MIXED_WORKFLOW)

    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file), "--", "pos1", "pos2", "x=kw1"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"Expected exit 0: {result.stderr}"

    # Find the run_id from stderr
    run_id = None
    for line in result.stderr.strip().split("\n"):
        if line.startswith("[godel] run ") and "completed" not in line and "resume" not in line:
            run_id = line.split("run ")[1].strip()
            break
    assert run_id is not None, f"Could not find run_id in: {result.stderr}"

    # Read the JSONL log and find WORKFLOW_STARTED
    log_path = tmp_path / "runs" / f"{run_id}.jsonl"
    wf_started = None
    for line in log_path.read_text().strip().split("\n"):
        event = json.loads(line)
        if event.get("op") == "WORKFLOW_STARTED":
            wf_started = event
            break

    assert wf_started is not None, "No WORKFLOW_STARTED event found"
    req = wf_started["request"]
    assert req["args"] == ["pos1", "pos2"], f"Expected list, got: {req['args']!r}"
    assert req["kwargs"] == {"x": "kw1"}, f"Expected dict, got: {req['kwargs']!r}"
    assert "args_repr_only" not in req


# ---------------------------------------------------------------------------
# Integration: godel resume recovers args
# ---------------------------------------------------------------------------

RESUME_WORKFLOW = '''\
import json
from godel import workflow
from godel import det

@workflow
async def wf(name, greeting="hello"):
    ts = det.now()
    out_path = "out.json"
    with open(out_path, "w") as f:
        json.dump({"name": name, "greeting": greeting, "ts": str(ts)}, f)
'''


def test_resume_recovers_args(tmp_path):
    """godel resume replays the original args without re-supplying them."""
    wf_file = tmp_path / "wf.py"
    wf_file.write_text(RESUME_WORKFLOW)

    # Initial run with args
    result = _run_godel(
        ["run", "--no-strict", "--no-lint", str(wf_file), "--", "alice", "greeting=hi"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"Initial run failed: {result.stderr}"
    original_out = json.loads((tmp_path / "out.json").read_text())
    assert original_out["name"] == "alice"
    assert original_out["greeting"] == "hi"

    # Extract run_id
    run_id = None
    for line in result.stderr.strip().split("\n"):
        if line.startswith("[godel] run ") and "completed" not in line and "resume" not in line:
            run_id = line.split("run ")[1].strip()
            break
    assert run_id is not None, f"Could not find run_id in: {result.stderr}"

    # Resume — no trailing args needed
    result2 = _run_godel(
        ["resume", "--no-strict", "--no-lint", run_id[:8], str(wf_file)],
        cwd=str(tmp_path),
    )
    assert result2.returncode == 0, f"Resume failed: {result2.stderr}"
    assert "resumed run completed" in result2.stderr

    # Output must be identical (deterministic replay)
    resumed_out = json.loads((tmp_path / "out.json").read_text())
    assert resumed_out == original_out


# ---------------------------------------------------------------------------
# get_workflow_args: args_repr_only sentinel
# ---------------------------------------------------------------------------

def _make_fake_walker(request_dict: dict):
    """Create a ReplayWalker with a single fake WORKFLOW_STARTED event in memory."""
    from unittest.mock import MagicMock
    from godel._events import Event, EventStatus
    from godel._replay import ReplayWalker
    import uuid
    import datetime

    event = Event(
        event_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        seq=0,
        step_path=(),
        invocation_seq=0,
        step_local_seq=0,
        op="WORKFLOW_STARTED",
        request_hash="",
        request=request_dict,
        response=None,
        status=EventStatus.STARTED,
        ts_start=datetime.datetime.utcnow().isoformat(),
        ts_end=None,
    )

    # Build a minimal mock EventLog that ReplayWalker can use
    mock_log = MagicMock()
    mock_log.all_events.return_value = [event]
    mock_log._run_id = event.run_id
    mock_log._replay_suppress = False

    walker = ReplayWalker.__new__(ReplayWalker)
    walker._log = mock_log
    walker._events = [event]
    walker._index = {}
    walker._replaying = True
    walker._build_index()
    return walker


def test_get_workflow_args_repr_only_raises():
    """ReplayWalker.get_workflow_args() raises ValueError for args_repr_only logs."""
    walker = _make_fake_walker({
        "function": "wf",
        "args": "(<MyObj>,)",
        "kwargs": "{}",
        "args_repr_only": True,
        "source_file": "",
    })
    with pytest.raises(ValueError, match="non-serialisable"):
        walker.get_workflow_args()


def test_get_workflow_args_legacy_format_warns(capsys):
    """ReplayWalker.get_workflow_args() warns and normalises old repr-string format."""
    walker = _make_fake_walker({
        "function": "wf",
        "args": "('old',)",    # repr string — not a list
        "kwargs": "{}",
        "source_file": "/some/file.py",
    })
    result = walker.get_workflow_args()

    # Should normalise to empty list/dict
    assert result["args"] == []
    assert result["kwargs"] == {}
    # source_file should be preserved
    assert result["source_file"] == "/some/file.py"

    # Should have printed a warning to stderr
    captured = capsys.readouterr()
    assert "legacy" in captured.err.lower() or "WARNING" in captured.err
