"""Tests for the formatter registry (godel/_formatters.py).

Acceptance criteria:
- Regression: old-format events.jsonl fixture renders via _fmt_event with
  byte-identical output (snapshot test).
- Unknown op renders via the default formatter without raising, and without
  a ``?op`` prefix.
- Adding a new op via @register works without touching cli.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from godel._events import Event, EventStatus
from godel._formatters import FORMATTERS, _default_formatter, register
from godel.cli import _fmt_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture_events(filename: str) -> list[Event]:
    path = FIXTURES_DIR / filename
    events = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(Event.from_dict(json.loads(line)))
    return events


def _make_event(
    op: str,
    status: str = "FINISHED",
    step_path: tuple[str, ...] = (),
    ts_start: str = "2025-01-01T00:00:00+00:00",
    ts_end: str | None = "2025-01-01T00:00:01+00:00",
    event_id: str = "ABCDEF1234567890ABCDEF12",
) -> Event:
    return Event(
        event_id=event_id,
        run_id="test-run",
        seq=0,
        step_path=step_path,
        op=op,
        status=EventStatus(status),
        ts_start=ts_start,
        ts_end=ts_end,
    )


# ---------------------------------------------------------------------------
# Snapshot / regression test
# ---------------------------------------------------------------------------

# Expected output lines for events_old_format.jsonl, byte-identical to what
# the old monolithic _fmt_event produced.  The format is:
#   [event_id[:8]] op<20  step_str<30  STATUS  (Xs)
_EXPECTED_SNAPSHOT = [
    "[01ABCDEF] WORKFLOW_STARTED     (root)                         FINISHED  (2.000s)",
    "[01ABCDEF] step.enter           fetch_data                     FINISHED  (1.000s)",
    "[01ABCDEF] run                  fetch_data/call_api            FINISHED  (0.500s)",
    "[01ABCDEF] agent.call           summarize                      FINISHED  (0.800s)",
    "[01ABCDEF] step.exit            fetch_data                     STARTED",
]


def test_old_format_fixture_snapshot():
    """Regression: old-format events render byte-identically through _fmt_event."""
    events = _load_fixture_events("events_old_format.jsonl")
    assert len(events) == len(_EXPECTED_SNAPSHOT), (
        f"Fixture has {len(events)} events but snapshot has {len(_EXPECTED_SNAPSHOT)}"
    )
    for event, expected in zip(events, _EXPECTED_SNAPSHOT):
        actual = _fmt_event(event)
        assert actual == expected, (
            f"op={event.op!r} mismatch:\n  got:      {actual!r}\n  expected: {expected!r}"
        )


# ---------------------------------------------------------------------------
# Unknown op — default formatter
# ---------------------------------------------------------------------------

def test_unknown_op_renders_without_raising():
    """Unknown op must not raise an exception."""
    event = _make_event(op="some.future.op", step_path=("wf", "step"))
    result = _fmt_event(event)
    assert "some.future.op" in result
    assert "FINISHED" in result


def test_unknown_op_no_question_mark_prefix():
    """Unknown op must NOT produce a '?op' prefix in the output."""
    event = _make_event(op="totally.unknown.op")
    result = _fmt_event(event)
    assert "?op" not in result
    assert "?totally" not in result


def test_unknown_op_contains_step_path():
    """Default formatter includes the step path in output."""
    event = _make_event(op="new.op", step_path=("alpha", "beta"))
    result = _fmt_event(event)
    assert "alpha/beta" in result


def test_unknown_op_contains_status():
    """Default formatter includes the status in output."""
    event = _make_event(op="new.op", status="FAILED", ts_end="2025-01-01T00:00:02+00:00")
    result = _fmt_event(event)
    assert "FAILED" in result


def test_unknown_op_root_path():
    """Default formatter renders (root) when step_path is empty."""
    event = _make_event(op="mystery.op", step_path=())
    result = _fmt_event(event)
    assert "(root)" in result


# ---------------------------------------------------------------------------
# Registry extensibility — no cli.py touch required
# ---------------------------------------------------------------------------

def test_register_new_op_without_touching_cli():
    """Registering a new formatter via @register works immediately."""
    # Capture the pre-registration state
    assert "agent.thought" not in FORMATTERS

    @register("agent.thought")
    def _fmt_thought(event: Event) -> str:
        return f"THOUGHT: {event.event_id[:8]}"

    event = _make_event(op="agent.thought", event_id="DEADBEEF00000000DEADBEEF")
    result = _fmt_event(event)
    assert result == "THOUGHT: DEADBEEF"

    # Cleanup so we don't pollute other tests
    del FORMATTERS["agent.thought"]


def test_register_overwrites_existing():
    """Re-registering an op replaces the old formatter."""
    original = FORMATTERS.get("WORKFLOW_STARTED")
    try:
        @register("WORKFLOW_STARTED")
        def _custom(event: Event) -> str:
            return "custom-output"

        event = _make_event(op="WORKFLOW_STARTED")
        assert _fmt_event(event) == "custom-output"
    finally:
        if original is not None:
            FORMATTERS["WORKFLOW_STARTED"] = original
        else:
            del FORMATTERS["WORKFLOW_STARTED"]


# ---------------------------------------------------------------------------
# Canonical ops are registered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", [
    "WORKFLOW_STARTED",
    "step.enter",
    "step.exit",
    "run",
    "agent.call",
    "FORK",
    "JOIN",
    "REWIND",
    "PAUSED",
    "print",
    "input",
    "det.now",
    "det.random",
    "det.uuid4",
    "UNRECOVERABLE",
])
def test_canonical_ops_have_formatters(op):
    """Every canonical op must be registered in FORMATTERS."""
    assert op in FORMATTERS, f"op {op!r} not found in FORMATTERS"


@pytest.mark.parametrize("op", [
    "WORKFLOW_STARTED",
    "step.enter",
    "step.exit",
    "run",
    "agent.call",
])
def test_canonical_ops_produce_output(op):
    """Canonical formatters must produce a non-empty string for a typical event."""
    event = _make_event(op=op, step_path=("my_step",))
    result = _fmt_event(event)
    assert result
    assert op in result


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def test_duration_appears_in_output():
    """Events with ts_start and ts_end include duration in output."""
    event = _make_event(
        op="step.enter",
        ts_start="2025-01-01T00:00:00+00:00",
        ts_end="2025-01-01T00:00:03.500000+00:00",
    )
    result = _fmt_event(event)
    assert "3.500s" in result


def test_no_duration_when_ts_end_missing():
    """Events without ts_end must not include a duration."""
    event = _make_event(op="step.enter", status="STARTED", ts_end=None)
    result = _fmt_event(event)
    assert "s)" not in result


def test_default_formatter_duration():
    """_default_formatter also includes duration for unknown ops."""
    event = _make_event(
        op="brand.new.op",
        ts_start="2025-01-01T00:00:00+00:00",
        ts_end="2025-01-01T00:00:01+00:00",
    )
    result = _default_formatter(event)
    assert "1.000s" in result
