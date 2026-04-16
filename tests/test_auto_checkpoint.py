"""Tests for GODEL_AUTO_CHECKPOINT scripted-stdin support.

Covers:
- auto_checkpoint annotated in event request when GODEL_AUTO_CHECKPOINT is set
- no annotation when env var is absent
- warning emitted when stdin is not a TTY and env var is unset
- warning suppressed when GODEL_AUTO_CHECKPOINT is set
- warning fires only once per process (sentinel reset between tests via fixture)
- replay is unaffected when auto_checkpoint mode changes between record and
  resume (request_hash must exclude auto_checkpoint)
"""
import asyncio
import io
import json
import sys
import pytest

import godel.io as godel_io
from godel._context import WorkflowContext, _current_workflow
from godel._decorators import workflow
from godel._event_log import EventLog
from godel._events import Event
from godel._replay import ReplayWalker
from godel.io import input as ainput


@pytest.fixture(autouse=True)
def reset_tty_warned(monkeypatch):
    """Reset the one-shot warning sentinel before each test."""
    monkeypatch.setattr(godel_io, "_tty_warned", False)


@pytest.fixture(autouse=True)
def clear_auto_checkpoint_env(monkeypatch):
    """Ensure GODEL_AUTO_CHECKPOINT is unset unless a test explicitly sets it."""
    monkeypatch.delenv("GODEL_AUTO_CHECKPOINT", raising=False)


# ---------------------------------------------------------------------------
# Event annotation
# ---------------------------------------------------------------------------

def test_auto_checkpoint_annotates_event(tmp_path, monkeypatch):
    """When GODEL_AUTO_CHECKPOINT is set, input events carry auto_checkpoint."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GODEL_AUTO_CHECKPOINT", "pipe")
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        return await ainput("proceed? ")

    result = asyncio.run(wf())
    assert result == "yes"

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    assert runs, "expected at least one run log"
    events = [json.loads(l) for l in runs[0].read_text().strip().splitlines()]
    input_started = [e for e in events if e["op"] == "input" and e["status"] == "STARTED"]
    assert input_started, "expected an STARTED input event"
    assert input_started[0]["request"].get("auto_checkpoint") == "pipe"


def test_no_auto_checkpoint_annotation_when_env_unset(tmp_path, monkeypatch):
    """When GODEL_AUTO_CHECKPOINT is absent, events must not contain the field."""
    monkeypatch.chdir(tmp_path)

    fake_stdin = io.StringIO("answer\n")
    fake_stdin.isatty = lambda: True  # pretend it's a TTY so no warning fires
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        return await ainput("q? ")

    asyncio.run(wf())

    runs = list((tmp_path / "runs").glob("*.jsonl"))
    events = [json.loads(l) for l in runs[0].read_text().strip().splitlines()]
    input_started = [e for e in events if e["op"] == "input" and e["status"] == "STARTED"]
    assert input_started
    assert "auto_checkpoint" not in input_started[0]["request"]


# ---------------------------------------------------------------------------
# TTY warning behaviour
# ---------------------------------------------------------------------------

def _make_non_tty_stdin(content: str) -> io.StringIO:
    """Return a StringIO whose isatty() returns False (default for StringIO)."""
    buf = io.StringIO(content)
    # StringIO.isatty() already returns False — no patching needed.
    return buf


def test_warning_emitted_when_not_tty_and_no_env(monkeypatch, capsys):
    """Warn once when stdin is not a TTY and GODEL_AUTO_CHECKPOINT is unset."""
    monkeypatch.setattr(sys, "stdin", _make_non_tty_stdin("answer\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        return await ainput("q? ")

    asyncio.run(wf())

    captured = capsys.readouterr()
    assert "stdin is not a TTY" in captured.err


def test_warning_suppressed_when_auto_checkpoint_set(monkeypatch, capsys):
    """No warning when GODEL_AUTO_CHECKPOINT is set, even if stdin is not a TTY."""
    monkeypatch.setenv("GODEL_AUTO_CHECKPOINT", "1")
    monkeypatch.setattr(sys, "stdin", _make_non_tty_stdin("answer\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        return await ainput("q? ")

    asyncio.run(wf())

    captured = capsys.readouterr()
    assert "stdin is not a TTY" not in captured.err


def test_warning_fires_only_once(monkeypatch, capsys):
    """The non-TTY warning should be emitted at most once per process."""
    monkeypatch.setattr(sys, "stdin", _make_non_tty_stdin("a\nb\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        await ainput("first? ")
        await ainput("second? ")

    asyncio.run(wf())

    captured = capsys.readouterr()
    # Count occurrences of the warning text in stderr
    count = captured.err.count("stdin is not a TTY")
    assert count == 1, f"expected 1 warning, got {count}"


def test_no_warning_when_stdin_is_tty(monkeypatch, capsys):
    """No warning when stdin appears to be a TTY."""
    fake_tty = io.StringIO("answer\n")
    fake_tty.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_tty)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        return await ainput("q? ")

    asyncio.run(wf())

    captured = capsys.readouterr()
    assert "stdin is not a TTY" not in captured.err


# ---------------------------------------------------------------------------
# Piped / file-redirect integration (no workflow context needed)
# ---------------------------------------------------------------------------

def test_input_reads_from_stringio_pipe(monkeypatch):
    """godel.input() works correctly when stdin is a StringIO (simulated pipe)."""
    monkeypatch.setenv("GODEL_AUTO_CHECKPOINT", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped-answer\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    result = asyncio.run(ainput("prompt: "))
    assert result == "piped-answer"


def test_input_multiple_answers_from_pipe(monkeypatch):
    """Multiple input() calls consume lines sequentially from piped stdin."""
    monkeypatch.setenv("GODEL_AUTO_CHECKPOINT", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("first\nsecond\nthird\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    @workflow
    async def wf():
        a = await ainput()
        b = await ainput()
        c = await ainput()
        return a, b, c

    assert asyncio.run(wf()) == ("first", "second", "third")


# ---------------------------------------------------------------------------
# Replay determinism: auto_checkpoint must NOT affect request_hash
# ---------------------------------------------------------------------------

def test_compute_request_hash_ignores_auto_checkpoint():
    """Auto-checkpoint mode is execution metadata, not request identity.

    Changing the value (or dropping it) must produce the same hash so resume
    never hits a spurious mismatch.
    """
    base = {"prompt": "proceed? "}
    h_bare = Event.compute_request_hash(base)
    h_pipe = Event.compute_request_hash({**base, "auto_checkpoint": "pipe"})
    h_file = Event.compute_request_hash({**base, "auto_checkpoint": "file"})
    h_one = Event.compute_request_hash({**base, "auto_checkpoint": "1"})
    assert h_bare == h_pipe == h_file == h_one


def _make_input_log(tmp_path, prompt: str, cached_value: str, auto_cp: str | None) -> EventLog:
    """Persist a single FINISHED input event, optionally tagged with
    auto_checkpoint, and return the reloaded log.
    """
    run_id = "auto-cp-replay"
    log = EventLog(run_id, runs_dir=str(tmp_path))
    req = {"prompt": prompt}
    if auto_cp is not None:
        req["auto_checkpoint"] = auto_cp
    started = log.emit_started(
        op="input",
        step_path=(),
        request=req,
        invocation_seq=0,
        step_local_seq=0,
    )
    log.emit_finished(started.event_id, response={"value": cached_value})
    log.close()
    return EventLog.load(run_id, runs_dir=str(tmp_path))


@pytest.fixture
def cleanup_ctx():
    token = _current_workflow.set(None)
    yield
    try:
        _current_workflow.reset(token)
    except Exception:
        _current_workflow.set(None)


def test_replay_unaffected_when_auto_checkpoint_dropped(tmp_path, monkeypatch, cleanup_ctx):
    """Record with GODEL_AUTO_CHECKPOINT=pipe, replay without it: cache hit."""
    # Stage a persisted log whose input event carries auto_checkpoint="pipe".
    loaded = _make_input_log(tmp_path, prompt="q? ", cached_value="yes", auto_cp="pipe")

    walker = ReplayWalker(loaded)
    ctx = WorkflowContext(
        run_id=loaded._run_id,
        event_log=loaded,
        replay_walker=walker,
    )
    _current_workflow.set(ctx)

    # Resume with NO env var — the request dict built by io.input() will not
    # include auto_checkpoint.  Before the _HASH_EXCLUDE_KEYS fix this produced
    # a different hash than the persisted event and the replay missed.
    monkeypatch.delenv("GODEL_AUTO_CHECKPOINT", raising=False)
    # stdin should never be read — if the replay misses we'd fall through to
    # a blocking readline().  Point stdin at an empty buffer so any fallthrough
    # surfaces as an empty string mismatch instead of hanging.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    result = asyncio.run(ainput("q? "))
    assert result == "yes"


def test_replay_unaffected_when_auto_checkpoint_changed(tmp_path, monkeypatch, cleanup_ctx):
    """Record with mode=pipe, replay with mode=file: still a cache hit."""
    loaded = _make_input_log(tmp_path, prompt="q? ", cached_value="approved", auto_cp="pipe")

    walker = ReplayWalker(loaded)
    ctx = WorkflowContext(
        run_id=loaded._run_id,
        event_log=loaded,
        replay_walker=walker,
    )
    _current_workflow.set(ctx)

    monkeypatch.setenv("GODEL_AUTO_CHECKPOINT", "file")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    result = asyncio.run(ainput("q? "))
    assert result == "approved"
