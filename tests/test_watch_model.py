"""Tests for godel._watch_model — pure WatchModel + reduce() reducer.

Acceptance criteria verified:
- Purity: reduce(m, e) returns a new model object for state-changing events;
  the input model is never mutated (frozen dataclasses + identity checks).
- Table-driven fixture tests: feeding a recorded stream produces expected
  model checkpoints.
- Unknown ops are no-ops (model identity after).
- Ring buffer respects max size: feeding >N events keeps only the last N.
- Idempotent on replay: applying the same step-state event twice yields the
  same end state as applying it once.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from godel._watch_model import (
    StreamPanel,
    StepNode,
    WatchModel,
    _MAX_LINE_LEN,
    _summarize_tool_call,
    _summarize_tool_result,
    reduce,
    reduce_header,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "event_streams"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(op: str, **kwargs) -> dict:
    """Build a minimal transcript event dict."""
    base = {"op": op, "step_path": [], "stream_path": [], "ts": "2026-04-14T00:00:00+00:00"}
    base.update(kwargs)
    return base


def _feed_stream(fixture_name: str, *, ring_size: int = 200) -> WatchModel:
    """Load a fixture JSONL and replay all events, returning the final model."""
    path = FIXTURES / fixture_name
    model = WatchModel.empty(ring_size=ring_size)
    with open(path) as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            obj = json.loads(raw_line)
            if "header" in obj:
                model = reduce_header(model, obj["header"])
            elif "event" in obj:
                model = reduce(model, obj["event"])
    return model


# ---------------------------------------------------------------------------
# WatchModel.empty
# ---------------------------------------------------------------------------

def test_empty_model_defaults():
    m = WatchModel.empty()
    assert m.run_meta == {}
    assert dict(m.steps) == {}
    assert dict(m.panels) == {}
    assert m.ring_size == 200


def test_empty_model_custom_ring_size():
    m = WatchModel.empty(ring_size=50)
    assert m.ring_size == 50


# ---------------------------------------------------------------------------
# Purity: frozen dataclasses + new-object-on-change
# ---------------------------------------------------------------------------

def test_reduce_returns_new_object_for_step_enter():
    m = WatchModel.empty()
    e = _make_event("step.enter", step_path=["fetch"])
    m2 = reduce(m, e)
    assert m2 is not m, "reduce must return a new model for step.enter"


def test_reduce_returns_same_object_for_unknown_op():
    m = WatchModel.empty()
    e = _make_event("totally.unknown.op")
    m2 = reduce(m, e)
    assert m2 is m, "reduce must return the same model for unknown ops"


def test_reduce_returns_same_object_for_rotate():
    m = WatchModel.empty()
    e = _make_event("rotate", last_seq=5)
    m2 = reduce(m, e)
    assert m2 is m, "rotate is a no-op"


def test_input_model_not_mutated_on_step_enter():
    m = WatchModel.empty()
    e = _make_event("step.enter", step_path=["fetch"])
    m2 = reduce(m, e)
    # Input model must be unchanged.
    assert len(m.steps) == 0
    assert len(m2.steps) == 1


def test_input_model_not_mutated_on_stream_line():
    m = WatchModel.empty()
    e = _make_event("stdout", stream_path=["work"], line="hello")
    m2 = reduce(m, e)
    assert len(m.panels) == 0
    assert len(m2.panels) == 1


def test_frozen_step_node_cannot_be_mutated():
    """StepNode is frozen=True; direct attribute assignment must raise."""
    node = StepNode(path=("a",), status="running")
    with pytest.raises((TypeError, AttributeError)):
        node.status = "done"  # type: ignore[misc]


def test_frozen_stream_panel_cannot_be_mutated():
    panel = StreamPanel(stream_path=("work",))
    with pytest.raises((TypeError, AttributeError)):
        panel.ring = ("new_line",)  # type: ignore[misc]


def test_frozen_watch_model_cannot_be_mutated():
    model = WatchModel.empty()
    with pytest.raises((TypeError, AttributeError)):
        model.ring_size = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# step.enter
# ---------------------------------------------------------------------------

def test_step_enter_creates_step_node():
    m = WatchModel.empty()
    e = _make_event("step.enter", step_path=["fetch_data"], ts="2026-04-14T00:00:01+00:00")
    m2 = reduce(m, e)
    key = ("fetch_data",)
    assert key in m2.steps
    node = m2.steps[key]
    assert node.status == "running"
    assert node.started_at == "2026-04-14T00:00:01+00:00"
    assert node.finished_at is None


def test_step_enter_nested_path():
    m = WatchModel.empty()
    e = _make_event("step.enter", step_path=["fetch_data", "call_api"])
    m2 = reduce(m, e)
    key = ("fetch_data", "call_api")
    assert key in m2.steps
    assert m2.steps[key].status == "running"


def test_step_enter_empty_path_is_noop():
    m = WatchModel.empty()
    e = _make_event("step.enter", step_path=[])
    m2 = reduce(m, e)
    assert m2 is m


# ---------------------------------------------------------------------------
# step.exit
# ---------------------------------------------------------------------------

def test_step_exit_marks_done():
    m = WatchModel.empty()
    e1 = _make_event("step.enter", step_path=["work"], ts="2026-04-14T00:00:01+00:00")
    m = reduce(m, e1)
    e2 = _make_event("step.exit", step_path=["work"], ts="2026-04-14T00:00:02+00:00", status="done")
    m = reduce(m, e2)
    node = m.steps[("work",)]
    assert node.status == "done"
    assert node.finished_at == "2026-04-14T00:00:02+00:00"


def test_step_exit_custom_status():
    m = WatchModel.empty()
    e1 = _make_event("step.enter", step_path=["work"])
    m = reduce(m, e1)
    e2 = _make_event("step.exit", step_path=["work"], status="failed")
    m = reduce(m, e2)
    assert m.steps[("work",)].status == "failed"


def test_step_exit_default_status_done():
    m = WatchModel.empty()
    e1 = _make_event("step.enter", step_path=["work"])
    m = reduce(m, e1)
    # Exit without explicit status — should default to "done"
    e2 = _make_event("step.exit", step_path=["work"])
    m = reduce(m, e2)
    assert m.steps[("work",)].status == "done"


def test_step_exit_empty_path_is_noop():
    m = WatchModel.empty()
    e = _make_event("step.exit", step_path=[])
    m2 = reduce(m, e)
    assert m2 is m


# ---------------------------------------------------------------------------
# Stream panels / ring buffer
# ---------------------------------------------------------------------------

def test_stdout_creates_panel():
    m = WatchModel.empty()
    e = _make_event("stdout", stream_path=["work"], line="Hello")
    m = reduce(m, e)
    key = ("work",)
    assert key in m.panels
    assert m.panels[key].ring == ("Hello",)


def test_agent_thought_line():
    m = WatchModel.empty()
    e = _make_event("agent.thought", stream_path=["agent"], text="I think...")
    m = reduce(m, e)
    assert m.panels[("agent",)].ring == ("I think...",)


def test_agent_tool_call_line():
    m = WatchModel.empty()
    e = _make_event("agent.tool_call", stream_path=["agent"], tool="search", input="query")
    m = reduce(m, e)
    line = m.panels[("agent",)].ring[0]
    # Unknown tool falls back to generic format; line must be ≤120 chars.
    assert "search" in line
    assert len(line) <= _MAX_LINE_LEN


def test_agent_tool_result_line():
    m = WatchModel.empty()
    e = _make_event("agent.tool_result", stream_path=["agent"], tool="search", output="results")
    m = reduce(m, e)
    line = m.panels[("agent",)].ring[0]
    assert "[tool_result]" in line
    assert "search" in line
    assert len(line) <= _MAX_LINE_LEN


def test_agent_raw_line():
    m = WatchModel.empty()
    e = _make_event("agent.raw", stream_path=["agent"], text="raw output")
    m = reduce(m, e)
    assert m.panels[("agent",)].ring == ("raw output",)


def test_multiple_lines_accumulate():
    m = WatchModel.empty()
    for i in range(5):
        e = _make_event("stdout", stream_path=["work"], line=f"line{i}")
        m = reduce(m, e)
    assert m.panels[("work",)].ring == ("line0", "line1", "line2", "line3", "line4")


def test_ring_buffer_respects_max_size():
    """Feeding >N lines keeps only the last N."""
    ring_size = 5
    m = WatchModel.empty(ring_size=ring_size)
    for i in range(10):
        e = _make_event("stdout", stream_path=["work"], line=f"line{i}")
        m = reduce(m, e)
    ring = m.panels[("work",)].ring
    assert len(ring) == ring_size
    # Must be the LAST 5 lines.
    assert ring == ("line5", "line6", "line7", "line8", "line9")


def test_ring_buffer_exact_size_no_eviction():
    """Feeding exactly N lines should not evict any."""
    ring_size = 3
    m = WatchModel.empty(ring_size=ring_size)
    for i in range(ring_size):
        e = _make_event("stdout", stream_path=["work"], line=f"line{i}")
        m = reduce(m, e)
    assert len(m.panels[("work",)].ring) == ring_size


def test_ring_buffer_size_1():
    """Extreme case: ring_size=1 keeps only the last line."""
    m = WatchModel.empty(ring_size=1)
    for i in range(5):
        e = _make_event("stdout", stream_path=["work"], line=f"line{i}")
        m = reduce(m, e)
    assert m.panels[("work",)].ring == ("line4",)


def test_distinct_stream_paths_separate_panels():
    m = WatchModel.empty()
    e1 = _make_event("stdout", stream_path=["alpha"], line="A")
    e2 = _make_event("stdout", stream_path=["beta"], line="B")
    m = reduce(m, e1)
    m = reduce(m, e2)
    assert ("alpha",) in m.panels
    assert ("beta",) in m.panels
    assert m.panels[("alpha",)].ring == ("A",)
    assert m.panels[("beta",)].ring == ("B",)


def test_last_event_ts_updated():
    m = WatchModel.empty()
    e = _make_event("stdout", stream_path=["work"], line="x", ts="2026-04-14T12:00:00+00:00")
    m = reduce(m, e)
    assert m.panels[("work",)].last_event_ts == "2026-04-14T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Unknown ops are no-ops
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", [
    "totally.unknown",
    "det.now",
    "det.random",
    "WORKFLOW_STARTED",
    "FORK",
    "JOIN",
    "run",
    "agent.call",
    "print",
    "input",
    "",
])
def test_unknown_op_returns_same_model(op):
    m = WatchModel.empty()
    e = _make_event(op)
    m2 = reduce(m, e)
    assert m2 is m, f"op={op!r} should be a no-op"


# ---------------------------------------------------------------------------
# reduce_header
# ---------------------------------------------------------------------------

def test_reduce_header_sets_run_meta():
    m = WatchModel.empty()
    header = {"v": 1, "run_id": "run-abc", "started_at": "2026-04-14T00:00:00+00:00"}
    m2 = reduce_header(m, header)
    assert m2.run_meta["run_id"] == "run-abc"
    assert m2.run_meta["v"] == 1


def test_reduce_header_merges_with_existing():
    m = WatchModel.empty()
    m = reduce_header(m, {"run_id": "run-abc"})
    m = reduce_header(m, {"started_at": "2026-04-14T00:00:00+00:00"})
    assert m.run_meta["run_id"] == "run-abc"
    assert "started_at" in m.run_meta


def test_reduce_header_returns_new_object():
    m = WatchModel.empty()
    m2 = reduce_header(m, {"run_id": "x"})
    assert m2 is not m


# ---------------------------------------------------------------------------
# Idempotent on replay (step-state events)
# ---------------------------------------------------------------------------

def test_step_enter_idempotent():
    """Applying the same step.enter event twice equals applying it once."""
    m = WatchModel.empty()
    e = _make_event("step.enter", step_path=["fetch"], ts="2026-04-14T00:00:01+00:00")
    m1 = reduce(m, e)
    m2 = reduce(m1, e)
    # Both should show the step as running with the same started_at.
    assert m1.steps[("fetch",)].status == m2.steps[("fetch",)].status == "running"
    assert m1.steps[("fetch",)].started_at == m2.steps[("fetch",)].started_at


def test_step_exit_idempotent():
    """Applying step.exit twice should leave the step in the same final state."""
    m = WatchModel.empty()
    m = reduce(m, _make_event("step.enter", step_path=["work"]))
    e_exit = _make_event("step.exit", step_path=["work"], status="done",
                         ts="2026-04-14T00:00:05+00:00")
    m1 = reduce(m, e_exit)
    m2 = reduce(m1, e_exit)
    assert m1.steps[("work",)].status == m2.steps[("work",)].status == "done"
    assert m1.steps[("work",)].finished_at == m2.steps[("work",)].finished_at


# ---------------------------------------------------------------------------
# Table-driven fixture tests
# ---------------------------------------------------------------------------

class TestSimpleWorkflowFixture:
    """Feed simple_workflow.jsonl and assert final model checkpoints."""

    @pytest.fixture(autouse=True)
    def model(self):
        self._model = _feed_stream("simple_workflow.jsonl")

    def test_run_meta_populated(self):
        assert self._model.run_meta.get("run_id") == "run-simple-001"

    def test_steps_have_correct_status(self):
        steps = self._model.steps
        assert ("fetch_data",) in steps
        assert ("summarize",) in steps
        assert steps[("fetch_data",)].status == "done"
        assert steps[("summarize",)].status == "done"

    def test_fetch_data_timing(self):
        node = self._model.steps[("fetch_data",)]
        assert node.started_at == "2026-04-14T00:00:01+00:00"
        assert node.finished_at == "2026-04-14T00:00:06+00:00"

    def test_panels_populated(self):
        panels = self._model.panels
        # stdout and agent lines land in separate stream_path panels.
        assert ("fetch_data",) in panels or ("fetch_data", "agent") in panels

    def test_fetch_data_panel_has_stdout(self):
        panel = self._model.panels.get(("fetch_data",))
        assert panel is not None
        assert any("Fetching" in line for line in panel.ring)

    def test_agent_panel_has_tool_call(self):
        panel = self._model.panels.get(("fetch_data", "agent"))
        assert panel is not None
        assert any("[tool_call]" in line for line in panel.ring)

    def test_agent_panel_has_tool_result(self):
        panel = self._model.panels.get(("fetch_data", "agent"))
        assert panel is not None
        assert any("[tool_result]" in line for line in panel.ring)

    def test_summarize_panel_has_raw(self):
        panel = self._model.panels.get(("summarize", "agent"))
        assert panel is not None
        assert any("Processing" in line for line in panel.ring)


class TestWithRotationFixture:
    """Feed with_rotation.jsonl (includes rotate sentinel) and verify model."""

    @pytest.fixture(autouse=True)
    def model(self):
        self._model = _feed_stream("with_rotation.jsonl")

    def test_run_meta_run_id(self):
        assert self._model.run_meta.get("run_id") == "run-rotate-001"

    def test_step_work_done(self):
        assert self._model.steps.get(("work",)) is not None
        assert self._model.steps[("work",)].status == "done"

    def test_both_lines_captured(self):
        """Lines before AND after rotation must appear in the ring."""
        panel = self._model.panels.get(("work",))
        assert panel is not None
        lines = panel.ring
        assert any("before rotation" in l for l in lines)
        assert any("after rotation" in l for l in lines)

    def test_rotate_is_noop_for_model_state(self):
        """Rotation sentinel must not add a panel or step."""
        # There should be exactly 1 step and 1 panel.
        assert len(self._model.steps) == 1
        assert len(self._model.panels) == 1


# ---------------------------------------------------------------------------
# Ring buffer with fixture — large stream
# ---------------------------------------------------------------------------

def test_ring_buffer_respects_size_over_fixture_stream():
    """Feeding a fixture with small ring_size evicts old lines."""
    ring_size = 3
    model = _feed_stream("simple_workflow.jsonl", ring_size=ring_size)
    for panel in model.panels.values():
        assert len(panel.ring) <= ring_size


# ---------------------------------------------------------------------------
# Read-only mapping guarantees (W1 fix)
# ---------------------------------------------------------------------------

def test_run_meta_is_read_only():
    """Callers must not be able to mutate run_meta on a returned model."""
    m = WatchModel.empty()
    m = reduce_header(m, {"run_id": "abc"})
    with pytest.raises(TypeError):
        m.run_meta["run_id"] = "hacked"  # type: ignore[index]


def test_steps_is_read_only():
    m = WatchModel.empty()
    m = reduce(m, _make_event("step.enter", step_path=["s"]))
    with pytest.raises(TypeError):
        m.steps[("s",)] = StepNode(path=("x",), status="running")  # type: ignore[index]


def test_panels_is_read_only():
    m = WatchModel.empty()
    m = reduce(m, _make_event("stdout", stream_path=["w"], line="x"))
    with pytest.raises(TypeError):
        m.panels[("w",)] = StreamPanel(stream_path=("x",))  # type: ignore[index]


def test_mutating_caller_copy_does_not_corrupt_model():
    """Even if a caller copies run_meta into a local dict and mutates it, the
    model's run_meta and subsequent merges must remain uncorrupted."""
    m = WatchModel.empty()
    m = reduce_header(m, {"run_id": "abc", "v": 1})
    # Caller constructs a dict view and mutates it — model must be unaffected.
    local = dict(m.run_meta)
    local["run_id"] = "hacked"
    assert m.run_meta["run_id"] == "abc"
    # Further merge still operates on the original.
    m2 = reduce_header(m, {"started_at": "2026-04-14T00:00:00+00:00"})
    assert m2.run_meta["run_id"] == "abc"


def test_empty_model_mappings_are_read_only():
    """Even the default empty model must expose read-only mappings."""
    m = WatchModel.empty()
    with pytest.raises(TypeError):
        m.run_meta["x"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        m.steps[("a",)] = StepNode(path=("a",), status="running")  # type: ignore[index]
    with pytest.raises(TypeError):
        m.panels[("a",)] = StreamPanel(stream_path=("a",))  # type: ignore[index]


# ---------------------------------------------------------------------------
# Schema tolerance (N2)
# ---------------------------------------------------------------------------

def test_stream_event_missing_stream_path_skipped():
    """Stream-op event lacking stream_path must not crash or add a () panel."""
    m = WatchModel.empty()
    e = {"op": "stdout", "line": "orphan", "ts": "2026-04-14T00:00:00+00:00"}
    m2 = reduce(m, e)
    assert m2 is m, "missing stream_path should be a same-object no-op"
    assert () not in m2.panels
    assert len(m2.panels) == 0


def test_stream_event_empty_stream_path_skipped():
    """Empty-list stream_path must not create a () panel key."""
    m = WatchModel.empty()
    e = _make_event("stdout", stream_path=[], line="orphan")
    m2 = reduce(m, e)
    assert m2 is m
    assert () not in m2.panels


def test_stream_event_null_stream_path_skipped():
    """stream_path=None must not crash or pollute."""
    m = WatchModel.empty()
    e = {"op": "agent.thought", "stream_path": None, "text": "x",
         "ts": "2026-04-14T00:00:00+00:00"}
    m2 = reduce(m, e)
    assert m2 is m
    assert () not in m2.panels


def test_step_event_missing_step_path_still_skipped():
    """step.enter without step_path must not crash or create a () step key.

    (Covered at the op level already; this pins the schema-tolerance contract.)
    """
    m = WatchModel.empty()
    e = {"op": "step.enter", "ts": "2026-04-14T00:00:00+00:00"}
    m2 = reduce(m, e)
    assert m2 is m
    assert () not in m2.steps


# ---------------------------------------------------------------------------
# _summarize_tool_call — well-known tools
# ---------------------------------------------------------------------------

def test_summarize_tool_call_read():
    line = _summarize_tool_call("Read", {"file_path": "godel/_watch_model.py"})
    assert line == "Read: godel/_watch_model.py"
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_bash():
    line = _summarize_tool_call("Bash", {"command": "pytest tests/"})
    assert line == "Bash: pytest tests/"
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_bash_multiline_command():
    cmd = "cd /some/dir\npytest tests/ -v --tb=short"
    line = _summarize_tool_call("Bash", {"command": cmd})
    # Must use the first non-blank line
    assert "cd /some/dir" in line
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_edit():
    line = _summarize_tool_call("Edit", {"file_path": "src/main.py"})
    assert line == "Edit: src/main.py"
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_write():
    line = _summarize_tool_call("Write", {"file_path": "output/result.json"})
    assert line == "Write: output/result.json"
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_grep():
    line = _summarize_tool_call("Grep", {"pattern": "_event_to_line", "path": "godel/"})
    assert "Grep:" in line
    assert "_event_to_line" in line
    assert "godel/" in line
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_glob():
    line = _summarize_tool_call("Glob", {"pattern": "**/*.py"})
    assert line == "Glob: **/*.py"
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_unknown_tool_fallback():
    line = _summarize_tool_call("http_get", {"url": "https://api.example.com/data"})
    assert "http_get" in line
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_unknown_tool_large_input():
    """Unknown tool with huge input dict must still be capped at _MAX_LINE_LEN."""
    large_input = {"data": "x" * 5000}
    line = _summarize_tool_call("SomeTool", large_input)
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_call_read_large_path():
    """Read with a very long file path must be capped at _MAX_LINE_LEN."""
    long_path = "a/b/c/" + "d" * 200 + ".py"
    line = _summarize_tool_call("Read", {"file_path": long_path})
    assert len(line) <= _MAX_LINE_LEN
    assert line.endswith("…")


# ---------------------------------------------------------------------------
# _summarize_tool_result
# ---------------------------------------------------------------------------

def test_summarize_tool_result_single_line():
    line = _summarize_tool_result("Read", "file contents here")
    assert "[tool_result]" in line
    assert "Read" in line
    assert "file contents here" in line
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_result_multiline_shows_first_line_and_count():
    output = "line one\nline two\nline three"
    line = _summarize_tool_result("Bash", output)
    assert "line one" in line
    assert "(+2 lines)" in line
    assert len(line) <= _MAX_LINE_LEN


def test_summarize_tool_result_large_output_capped():
    """Multi-line output with megabytes of data must produce a line ≤120 chars."""
    big_output = "first line\n" + ("x" * 2_000_000 + "\n") * 3
    line = _summarize_tool_result("Read", big_output)
    assert len(line) <= _MAX_LINE_LEN
    assert "first line" in line


def test_summarize_tool_result_blank_lines_skipped():
    """Leading blank lines must be skipped to find the first non-blank line."""
    output = "\n\n  \nactual content\nmore content"
    line = _summarize_tool_result("Grep", output)
    assert "actual content" in line
    assert len(line) <= _MAX_LINE_LEN


# ---------------------------------------------------------------------------
# Integration: reduce() + _event_to_line — line length guarantee
# ---------------------------------------------------------------------------

def test_tool_call_read_line_capped_in_ring():
    """A Read tool_call with a huge file path produces a ring entry ≤120 chars."""
    m = WatchModel.empty()
    e = _make_event(
        "agent.tool_call",
        stream_path=["agent"],
        tool="Read",
        input={"file_path": "/very/long/" + "dir/" * 50 + "bigfile.py"},
    )
    m = reduce(m, e)
    line = m.panels[("agent",)].ring[0]
    assert len(line) <= _MAX_LINE_LEN
    assert "Read:" in line


def test_tool_result_large_output_capped_in_ring():
    """A tool_result with 2 MB of output produces a ring entry ≤120 chars."""
    m = WatchModel.empty()
    big_output = "first line\n" + "x" * 2_000_000
    e = _make_event(
        "agent.tool_result",
        stream_path=["agent"],
        tool="Read",
        output=big_output,
    )
    m = reduce(m, e)
    line = m.panels[("agent",)].ring[0]
    assert len(line) <= _MAX_LINE_LEN
    assert "[tool_result]" in line
