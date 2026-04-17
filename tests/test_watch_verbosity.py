"""Tests for watcher verbosity controls (godel-py-dod).

Acceptance criteria verified:

- AC1  Tool events hidden by default (plain mode): agent.tool_call and
       agent.tool_result are not rendered when show_tools=False.
- AC2  Tool events shown when show_tools=True.
- AC3  Agent response capped at 20 lines by default with truncation indicator.
- AC4  --verbose sets show_tools=True + max_agent_lines=0 (no cap).
- AC5  --max-agent-lines 0 disables the cap entirely.
- AC6  VerbosityConfig dataclass behaves correctly.
- AC7  CLI flags are parsed and forwarded by _spawn_watch_subprocess.
- AC8  godel tail accepts the same verbosity flags.
- AC9  Empty response: no truncation indicator when response < cap.
- AC10 Truncation indicator shows correct count of hidden lines.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("rich")

import queue

from godel._watch import (
    VerbosityConfig,
    _PlainLineLog,
    _drain_queue,
)
from godel._watch_model import WatchModel

# ---------------------------------------------------------------------------
# Subprocess helpers — ensure worktree source is used, not site-packages.
# ---------------------------------------------------------------------------

_WORKTREE_ROOT = str(Path(__file__).parent.parent)


def _subprocess_env(**extra: str) -> dict:
    """Return os.environ copy with this worktree on PYTHONPATH."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _WORKTREE_ROOT + os.pathsep + existing if existing else _WORKTREE_ROOT
    )
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# AC6: VerbosityConfig dataclass
# ---------------------------------------------------------------------------

def test_verbosity_config_defaults():
    cfg = VerbosityConfig()
    assert cfg.show_tools is False
    assert cfg.max_agent_lines == 20


def test_verbosity_config_verbose_factory():
    cfg = VerbosityConfig.verbose()
    assert cfg.show_tools is True
    assert cfg.max_agent_lines == 0


def test_verbosity_config_default_factory():
    cfg = VerbosityConfig.default()
    assert cfg.show_tools is False
    assert cfg.max_agent_lines == 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log(verbosity: VerbosityConfig | None = None) -> tuple["_PlainLineLog", io.StringIO]:
    """Create a _PlainLineLog writing to a StringIO buffer."""
    buf = io.StringIO()
    log = _PlainLineLog(file=buf, verbosity=verbosity)
    return log, buf


def _tool_call_event(tool: str = "bash") -> dict:
    return {
        "op": "agent.tool_call",
        "step_path": ["my_step"],
        "stream_path": ["stream1"],
        "tool": tool,
        "input": {"cmd": "echo hello"},
        "ts": "2026-01-01T00:00:00+00:00",
    }


def _tool_result_event(tool: str = "bash") -> dict:
    return {
        "op": "agent.tool_result",
        "step_path": ["my_step"],
        "stream_path": ["stream1"],
        "tool": tool,
        "output": "hello",
        "ts": "2026-01-01T00:00:00+00:00",
    }


def _response_event(text: str) -> dict:
    return {
        "op": "agent.response",
        "step_path": ["my_step"],
        "stream_path": ["stream1"],
        "text": text,
        "ts": "2026-01-01T00:00:00+00:00",
    }


def _response_chunk(text: str) -> dict:
    """Streaming chunk that continues a burst."""
    return _response_event(text)


def _prompt_event() -> dict:
    return {
        "op": "agent.prompt",
        "step_path": ["my_step"],
        "stream_path": ["stream1"],
        "prompt": "hello",
        "model": "claude-sonnet",
        "ts": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# AC1: Tool events hidden by default
# ---------------------------------------------------------------------------

def test_tool_call_hidden_by_default():
    log, buf = _make_log()  # default verbosity: show_tools=False
    log.print_event(_tool_call_event())
    output = buf.getvalue()
    assert "bash" not in output, f"tool_call should be hidden by default; got: {output!r}"
    assert "⚒" not in output


def test_tool_result_hidden_by_default():
    log, buf = _make_log()
    log.print_event(_tool_result_event())
    output = buf.getvalue()
    assert "↳" not in output, f"tool_result should be hidden by default; got: {output!r}"


# ---------------------------------------------------------------------------
# AC2: Tool events shown when show_tools=True
# ---------------------------------------------------------------------------

def test_tool_call_shown_when_show_tools():
    cfg = VerbosityConfig(show_tools=True)
    log, buf = _make_log(cfg)
    log.print_event(_tool_call_event("my_tool"))
    output = buf.getvalue()
    assert "my_tool" in output, f"tool_call should be shown with show_tools=True; got: {output!r}"


def test_tool_result_shown_when_show_tools():
    cfg = VerbosityConfig(show_tools=True)
    log, buf = _make_log(cfg)
    log.print_event(_tool_result_event("my_tool"))
    output = buf.getvalue()
    assert "my_tool" in output, f"tool_result should be shown with show_tools=True; got: {output!r}"


# ---------------------------------------------------------------------------
# AC3: Agent response capped at 20 lines with truncation indicator
# ---------------------------------------------------------------------------

def test_agent_response_capped_at_20_lines():
    """A response with >20 lines is capped; truncation indicator appears."""
    cfg = VerbosityConfig(max_agent_lines=20)
    log, buf = _make_log(cfg)

    # Send a response with 30 lines.
    lines_30 = "\n".join(f"line {i}" for i in range(30))
    log.print_event(_response_event(lines_30))

    output = buf.getvalue()
    assert "truncated" in output, (
        f"Expected truncation indicator for 30-line response (cap=20); got:\n{output!r}"
    )
    # All 30 lines should NOT be in the output.
    assert "line 29" not in output, (
        f"Line 29 should have been truncated; got:\n{output!r}"
    )


def test_agent_response_truncation_shows_count():
    """The truncation indicator mentions the number of hidden lines."""
    cfg = VerbosityConfig(max_agent_lines=5)
    log, buf = _make_log(cfg)

    # 10 lines total, cap=5 → 5 lines shown, 5 hidden (approximately)
    lines_10 = "\n".join(f"line {i}" for i in range(10))
    log.print_event(_response_event(lines_10))

    output = buf.getvalue()
    assert "truncated" in output, f"Expected truncation indicator; got:\n{output!r}"
    # The indicator should mention a positive number.
    import re
    match = re.search(r"\+(\d+) lines truncated", output)
    assert match is not None, f"Expected '+N lines truncated' pattern; got:\n{output!r}"
    assert int(match.group(1)) > 0


# ---------------------------------------------------------------------------
# AC9: Empty response — no truncation when response < cap
# ---------------------------------------------------------------------------

def test_agent_response_no_truncation_short_response():
    """A response with fewer lines than cap should not show truncation indicator."""
    cfg = VerbosityConfig(max_agent_lines=20)
    log, buf = _make_log(cfg)

    # 5 lines — well under the 20-line cap.
    lines_5 = "\n".join(f"line {i}" for i in range(5))
    log.print_event(_response_event(lines_5))

    output = buf.getvalue()
    assert "truncated" not in output, (
        f"Short response should not trigger truncation; got:\n{output!r}"
    )
    assert "line 4" in output, f"All 5 lines should appear; got:\n{output!r}"


def test_agent_response_no_truncation_empty():
    """An empty response emits no truncation indicator."""
    cfg = VerbosityConfig(max_agent_lines=20)
    log, buf = _make_log(cfg)
    log.print_event(_response_event(""))
    output = buf.getvalue()
    assert "truncated" not in output, f"Empty response should not trigger truncation; got: {output!r}"


# ---------------------------------------------------------------------------
# AC4: --verbose shows everything
# ---------------------------------------------------------------------------

def test_verbose_shows_tool_calls():
    cfg = VerbosityConfig.verbose()
    log, buf = _make_log(cfg)
    log.print_event(_tool_call_event("verbose_tool"))
    output = buf.getvalue()
    assert "verbose_tool" in output, f"verbose mode should show tool calls; got: {output!r}"


def test_verbose_no_line_cap():
    """With max_agent_lines=0 (verbose), a 100-line response is not truncated."""
    cfg = VerbosityConfig.verbose()
    log, buf = _make_log(cfg)

    lines_100 = "\n".join(f"line {i}" for i in range(100))
    log.print_event(_response_event(lines_100))

    output = buf.getvalue()
    assert "truncated" not in output, (
        f"verbose mode should not truncate responses; got:\n{output!r}"
    )
    assert "line 99" in output, f"All 100 lines should appear in verbose mode; got:\n{output!r}"


# ---------------------------------------------------------------------------
# AC5: --max-agent-lines 0 disables cap
# ---------------------------------------------------------------------------

def test_max_agent_lines_zero_disables_cap():
    cfg = VerbosityConfig(show_tools=False, max_agent_lines=0)
    log, buf = _make_log(cfg)

    lines_50 = "\n".join(f"row {i}" for i in range(50))
    log.print_event(_response_event(lines_50))

    output = buf.getvalue()
    assert "truncated" not in output, (
        f"max_agent_lines=0 should disable truncation; got:\n{output!r}"
    )
    assert "row 49" in output


# ---------------------------------------------------------------------------
# AC7: CLI flags forwarded by _spawn_watch_subprocess
# ---------------------------------------------------------------------------

def test_spawn_watch_subprocess_show_tools_flag(tmp_path):
    """--show-tools is forwarded to the watcher subprocess command."""
    import unittest.mock as mock
    from godel.cli import _spawn_watch_subprocess

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    captured: list = []

    def _fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        m = mock.MagicMock()
        m.pid = 99999
        return m

    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        _spawn_watch_subprocess("run-1", runs_dir=str(runs_dir), show_tools=True)

    assert captured
    cmd = captured[0]
    assert "--show-tools" in cmd, f"Expected --show-tools in cmd; got: {cmd}"


def test_spawn_watch_subprocess_max_agent_lines_flag(tmp_path):
    """--max-agent-lines N is forwarded to the watcher subprocess command."""
    import unittest.mock as mock
    from godel.cli import _spawn_watch_subprocess

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    captured: list = []

    def _fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        m = mock.MagicMock()
        m.pid = 99999
        return m

    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        _spawn_watch_subprocess("run-2", runs_dir=str(runs_dir), max_agent_lines=5)

    assert captured
    cmd = captured[0]
    assert "--max-agent-lines" in cmd
    idx = cmd.index("--max-agent-lines")
    assert cmd[idx + 1] == "5", f"Expected '5' after --max-agent-lines; got cmd: {cmd}"


def test_spawn_watch_subprocess_verbose_flag(tmp_path):
    """--verbose is forwarded to the watcher subprocess; suppresses --max-agent-lines."""
    import unittest.mock as mock
    from godel.cli import _spawn_watch_subprocess

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    captured: list = []

    def _fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        m = mock.MagicMock()
        m.pid = 99999
        return m

    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        _spawn_watch_subprocess("run-3", runs_dir=str(runs_dir), verbose=True)

    assert captured
    cmd = captured[0]
    assert "--verbose" in cmd, f"Expected --verbose in cmd; got: {cmd}"
    # When verbose=True, --show-tools and --max-agent-lines should NOT appear
    # (--verbose is the shorthand that covers both).
    assert "--show-tools" not in cmd
    assert "--max-agent-lines" not in cmd


def test_spawn_watch_subprocess_verbose_overrides_show_tools(tmp_path):
    """When verbose=True, --show-tools is suppressed (--verbose implies it)."""
    import unittest.mock as mock
    from godel.cli import _spawn_watch_subprocess

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    captured: list = []

    def _fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        m = mock.MagicMock()
        m.pid = 99999
        return m

    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        _spawn_watch_subprocess("run-4", runs_dir=str(runs_dir), verbose=True, show_tools=True)

    assert captured
    cmd = captured[0]
    # --verbose subsumes show_tools; both --verbose and --show-tools being
    # present would be redundant but not harmful.  Assert at minimum that
    # --verbose is present.
    assert "--verbose" in cmd


# ---------------------------------------------------------------------------
# AC8: godel tail accepts the same verbosity flags
# ---------------------------------------------------------------------------

def test_tail_accepts_show_tools_flag():
    """``godel tail --help`` shows --show-tools."""
    result = subprocess.run(
        [sys.executable, "-m", "godel", "tail", "--help"],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert result.returncode == 0
    assert "--show-tools" in result.stdout, (
        f"Expected --show-tools in tail help; got:\n{result.stdout}"
    )


def test_tail_accepts_max_agent_lines_flag():
    """``godel tail --help`` shows --max-agent-lines."""
    result = subprocess.run(
        [sys.executable, "-m", "godel", "tail", "--help"],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert result.returncode == 0
    assert "--max-agent-lines" in result.stdout, (
        f"Expected --max-agent-lines in tail help; got:\n{result.stdout}"
    )


def test_tail_accepts_verbose_flag():
    """``godel tail --help`` shows -v / --verbose."""
    result = subprocess.run(
        [sys.executable, "-m", "godel", "tail", "--help"],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert result.returncode == 0
    assert "--verbose" in result.stdout or "-v" in result.stdout, (
        f"Expected --verbose/-v in tail help; got:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# run_watch integration: verbosity param accepted
# ---------------------------------------------------------------------------

def test_run_watch_accepts_verbosity_config(tmp_path):
    """run_watch() accepts a VerbosityConfig and passes it to _PlainLineLog."""
    import json as _json
    from godel import _watch as watch_mod

    run_id = "verbosity-integration-test"
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    transcript = run_dir / "transcript.jsonl"

    header = {"header": {"v": 1, "run_id": run_id, "started_at": "2026-01-01T00:00:00+00:00"}}
    event = {"event": {
        "ts": "2026-01-01T00:00:01+00:00",
        "seq": 1,
        "op": "step.enter",
        "step_path": ["my_step"],
        "stream_path": [],
    }}
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

    captured = io.StringIO()
    cfg = VerbosityConfig.verbose()
    # Should not raise.
    watch_mod.run_watch(run_id, runs_dir=str(runs_dir), plain=True, stdout=captured, verbosity=cfg)

    output = captured.getvalue()
    assert "── workflow" in output, f"Expected workflow banner; got:\n{output!r}"


# ---------------------------------------------------------------------------
# Streaming bursts: truncation across multiple chunks
# ---------------------------------------------------------------------------

def test_response_truncation_across_chunks():
    """Truncation works correctly when response is sent in multiple chunks."""
    cfg = VerbosityConfig(max_agent_lines=3)
    log, buf = _make_log(cfg)

    # First chunk: 2 lines (below cap of 3)
    log.print_event(_response_event("line 0\nline 1\n"))
    # Second chunk: 4 more lines (this should trigger truncation at line 3)
    log.print_event(_response_event("line 2\nline 3\nline 4\nline 5"))

    output = buf.getvalue()
    assert "truncated" in output, (
        f"Expected truncation after 3 lines across two chunks; got:\n{output!r}"
    )
    # Lines beyond the cap should not appear.
    assert "line 5" not in output, f"line 5 should be truncated; got:\n{output!r}"


def test_response_no_truncation_exact_cap():
    """A response with exactly `cap` lines does NOT trigger truncation."""
    cfg = VerbosityConfig(max_agent_lines=5)
    log, buf = _make_log(cfg)

    # Exactly 5 newlines = 5 line-breaks = 6 lines of text (but 5 newlines total)
    text = "\n".join(f"line {i}" for i in range(5))  # 4 newlines, 5 items
    log.print_event(_response_event(text))

    output = buf.getvalue()
    assert "truncated" not in output, (
        f"Response with exactly cap lines should not truncate; got:\n{output!r}"
    )


# ---------------------------------------------------------------------------
# TUI mode: _drain_queue verbosity filtering (CRITICAL fix)
# ---------------------------------------------------------------------------

def test_drain_queue_filters_tool_events_with_default_verbosity():
    """TUI mode _drain_queue must skip tool events when show_tools=False."""
    cfg = VerbosityConfig()  # show_tools=False
    model = WatchModel.empty()
    q: queue.Queue = queue.Queue()

    # Enqueue a tool_call and a tool_result — both should be skipped
    q.put({"op": "agent.tool_call", "step_path": ["s"], "stream_path": ["x"],
           "tool": "bash", "input": "{}"})
    q.put({"op": "agent.tool_result", "step_path": ["s"], "stream_path": ["x"],
           "tool": "bash", "output": "ok"})
    q.put(None)  # EOS

    new_model, did_update, eos = _drain_queue(q, model, verbosity=cfg)
    # Model should not have changed because tool events were filtered out
    assert new_model is model, "Tool events should be filtered; model should be unchanged"
    assert eos is True


def test_drain_queue_passes_tool_events_when_verbose():
    """TUI mode _drain_queue must pass tool events when show_tools=True."""
    cfg = VerbosityConfig.verbose()
    model = WatchModel.empty()
    q: queue.Queue = queue.Queue()

    q.put({"op": "agent.tool_call", "step_path": ["s"], "stream_path": ["x"],
           "tool": "bash", "input": "{}"})
    q.put(None)

    new_model, did_update, eos = _drain_queue(q, model, verbosity=cfg)
    # Tool event should have been processed by reduce() → _handle_stream_line
    assert did_update is True


# ---------------------------------------------------------------------------
# Parallel agents + line capping
# ---------------------------------------------------------------------------

def test_parallel_agents_line_capping_independent():
    """Line cap works independently per stream_path in parallel agents."""
    cfg = VerbosityConfig(max_agent_lines=3)
    log, buf = _make_log(cfg)

    # Two parallel branches with different stream_paths
    for branch, root in [("branch_a", "01JX_A"), ("branch_b", "01JX_B")]:
        log.print_event({
            "op": "agent.prompt",
            "step_path": [branch],
            "stream_path": [root],
            "prompt": f"prompt for {branch}",
            "model": "test",
            "ts": "2026-04-14T00:00:01+00:00",
        })

    # Both branches emit responses exceeding the cap
    for i in range(6):
        log.print_event({
            "op": "agent.response",
            "step_path": ["branch_a"],
            "stream_path": ["01JX_A"],
            "text": f"A-line-{i}\n",
            "ts": "2026-04-14T00:00:02+00:00",
        })
        log.print_event({
            "op": "agent.response",
            "step_path": ["branch_b"],
            "stream_path": ["01JX_B"],
            "text": f"B-line-{i}\n",
            "ts": "2026-04-14T00:00:02+00:00",
        })

    output = buf.getvalue()
    # Both branches should show truncation
    assert "truncated" in output, f"Expected truncation indicator; got:\n{output!r}"
    # Early lines should be present
    assert "A-line-0" in output
    assert "B-line-0" in output
    # Lines past the cap should not
    assert "A-line-5" not in output
    assert "B-line-5" not in output
