"""Tests for godel/_redact.py — RedactorRegistry acceptance criteria.

Covers godel-py-5pl.6:

1. Composition order: [A, B] executes A then B; B sees A's output; asserts
   exact pipeline ordering.
2. Raising redactor: substituted event has ONLY ``redactor`` name and
   ``error_class``; no message field, no input content; subsequent redactors
   and events continue processing.
3. ``BaseException`` (not just ``Exception``) is caught: test uses a mock
   redactor raising ``KeyboardInterrupt``.
4. Redactor returning ``None``: event is dropped; no transcript line written;
   no error emitted.
5. Zero built-in patterns shipped: ``RedactorRegistry()`` with no args produces
   an empty list.

Integration: tests also exercise ``TranscriptWriter`` with ``redactors=``
wiring to confirm end-to-end behavior.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from godel._redact import RedactorRegistry
from godel._transcript import TranscriptWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_events(run_dir: Path) -> list[dict]:
    """Read all event lines from transcript.jsonl, skipping the header."""
    lines = (run_dir / "transcript.jsonl").read_text().splitlines()
    events = []
    for line in lines:
        parsed = json.loads(line)
        if "event" in parsed:
            events.append(parsed["event"])
    return events


# ---------------------------------------------------------------------------
# Acceptance criterion 5: zero built-in patterns
# ---------------------------------------------------------------------------


def test_empty_registry_has_no_redactors():
    """RedactorRegistry() with no args ships zero built-in patterns."""
    registry = RedactorRegistry()
    assert registry._redactors == []


def test_empty_registry_passthrough():
    """An empty registry returns the payload unchanged."""
    registry = RedactorRegistry()
    payload = '{"op":"step_start","seq":1}'
    assert registry.apply(payload) == payload


# ---------------------------------------------------------------------------
# Acceptance criterion 1: composition / pipeline ordering
# ---------------------------------------------------------------------------


def test_composition_order_a_then_b():
    """[A, B] executes A then B; B sees A's output; pipeline order is exact."""
    call_log: list[str] = []
    received_inputs: list[str] = []

    def redactor_a(payload: str) -> str:
        call_log.append("A")
        received_inputs.append(payload)
        return payload.replace("secret", "***")

    def redactor_b(payload: str) -> str:
        call_log.append("B")
        received_inputs.append(payload)
        return payload.replace("token", "REDACTED")

    registry = RedactorRegistry([redactor_a, redactor_b])
    original = '{"op":"test","data":"secret token"}'
    result = registry.apply(original)

    # Order: A ran first, B ran second
    assert call_log == ["A", "B"]
    # B received A's output (not the original)
    assert received_inputs[0] == original          # A's input is the original
    assert received_inputs[1] == original.replace("secret", "***")  # B sees A's output
    # Final result has both replacements applied
    assert result == '{"op":"test","data":"*** REDACTED"}'


def test_pipeline_b_sees_a_output():
    """Explicitly assert B's input equals A's return value, not the original."""
    b_inputs: list[str] = []

    def redactor_a(payload: str) -> str:
        return payload + "_A"

    def redactor_b(payload: str) -> str:
        b_inputs.append(payload)
        return payload + "_B"

    registry = RedactorRegistry([redactor_a, redactor_b])
    result = registry.apply("X")
    assert b_inputs == ["X_A"]
    assert result == "X_A_B"


def test_three_redactors_chain():
    """Three redactors chain in strict registration order."""
    order: list[int] = []

    def r1(p: str) -> str:
        order.append(1)
        return p

    def r2(p: str) -> str:
        order.append(2)
        return p

    def r3(p: str) -> str:
        order.append(3)
        return p

    RedactorRegistry([r1, r2, r3]).apply("data")
    assert order == [1, 2, 3]


# ---------------------------------------------------------------------------
# Acceptance criterion 4: None return drops the event
# ---------------------------------------------------------------------------


def test_none_return_drops_event():
    """A redactor returning None causes apply() to return None."""
    def drop_all(payload: str) -> None:
        return None

    registry = RedactorRegistry([drop_all])
    assert registry.apply('{"op":"sensitive"}') is None


def test_none_return_short_circuits_pipeline():
    """Once a redactor returns None, subsequent redactors are NOT called."""
    called: list[str] = []

    def first_drops(payload: str) -> None:
        called.append("first")
        return None

    def should_not_run(payload: str) -> str:
        called.append("second")
        return payload

    registry = RedactorRegistry([first_drops, should_not_run])
    result = registry.apply("data")
    assert result is None
    assert called == ["first"]


def test_none_return_no_transcript_line(tmp_path):
    """TranscriptWriter with a drop-all redactor writes no event lines."""
    def drop_all(payload: str) -> None:
        return None

    with TranscriptWriter(tmp_path / "run", run_id="test", redactors=[drop_all]) as tw:
        tw.write_event("step_start", step_path=["my_step"])
        tw.write_event("step_end", step_path=["my_step"])

    events = _read_events(tmp_path / "run")
    assert events == []


# ---------------------------------------------------------------------------
# Acceptance criterion 2: raising redactor — sentinel shape
# ---------------------------------------------------------------------------


def test_raising_redactor_returns_sentinel_string():
    """A raising redactor causes apply() to return a sentinel JSON string."""
    def bad_redactor(payload: str) -> str:
        raise ValueError("this message must not appear in output")

    bad_redactor.__name__ = "bad_redactor"
    registry = RedactorRegistry([bad_redactor])
    result = registry.apply('{"op":"test","secret":"data"}')

    assert result is not None, "Expected sentinel string, got None"
    sentinel = json.loads(result)
    # Must have ONLY the required sentinel keys (plus any extras from caller).
    assert sentinel["op"] == "redactor.error"
    assert sentinel["redactor"] == "bad_redactor"
    assert sentinel["error_class"] == "ValueError"


def test_sentinel_has_no_message_field():
    """Sentinel must NOT contain any exception message or input payload."""
    def raises(payload: str) -> str:
        raise RuntimeError("super secret message that must not leak")

    registry = RedactorRegistry([raises])
    result = registry.apply('{"op":"test","secret":"TOP SECRET DATA"}')

    assert result is not None
    sentinel = json.loads(result)
    # No 'message', 'error_message', or similar field
    assert "message" not in sentinel
    assert "error_message" not in sentinel
    # No trace of original payload
    assert "TOP SECRET DATA" not in result
    assert "super secret message" not in result


def test_sentinel_has_no_original_payload():
    """Input payload content must NOT appear anywhere in the sentinel."""
    def raises(payload: str) -> str:
        raise KeyError("leaked_key")

    registry = RedactorRegistry([raises])
    original = '{"op":"test","api_key":"sk-abc123"}'
    result = registry.apply(original)

    assert result is not None
    assert "sk-abc123" not in result
    assert "api_key" not in result


def test_raising_redactor_subsequent_events_continue(tmp_path):
    """After a redactor error on event N, event N+1 is processed normally."""
    event_count = 0

    def raises_on_first(payload: str) -> str:
        nonlocal event_count
        event_count += 1
        if event_count == 1:
            raise RuntimeError("boom")
        return payload

    with TranscriptWriter(tmp_path / "run", run_id="test", redactors=[raises_on_first]) as tw:
        tw.write_event("event_one")   # redactor raises → sentinel written
        tw.write_event("event_two")   # redactor passes → normal event written

    events = _read_events(tmp_path / "run")
    assert len(events) == 2
    assert events[0]["op"] == "redactor.error"
    assert events[1]["op"] == "event_two"


def test_sentinel_written_to_transcript(tmp_path):
    """When a redactor raises, the sentinel event is written to the transcript."""
    def always_raises(payload: str) -> str:
        raise ValueError("boom")

    always_raises.__name__ = "always_raises"

    with TranscriptWriter(tmp_path / "run", run_id="test", redactors=[always_raises]) as tw:
        tw.write_event("step_start", step_path=["s"])

    events = _read_events(tmp_path / "run")
    assert len(events) == 1
    sentinel = events[0]
    assert sentinel["op"] == "redactor.error"
    assert sentinel["redactor"] == "always_raises"
    assert sentinel["error_class"] == "ValueError"


# ---------------------------------------------------------------------------
# Acceptance criterion 3: BaseException (not just Exception) is caught
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_is_caught():
    """BaseException subclasses like KeyboardInterrupt must be caught."""
    def raises_keyboard_interrupt(payload: str) -> str:
        raise KeyboardInterrupt()

    raises_keyboard_interrupt.__name__ = "raises_keyboard_interrupt"
    registry = RedactorRegistry([raises_keyboard_interrupt])
    result = registry.apply("data")

    assert result is not None
    sentinel = json.loads(result)
    assert sentinel["op"] == "redactor.error"
    assert sentinel["error_class"] == "KeyboardInterrupt"


def test_system_exit_is_caught():
    """SystemExit (BaseException) must be caught, not propagated."""
    def raises_system_exit(payload: str) -> str:
        raise SystemExit(1)

    raises_system_exit.__name__ = "raises_system_exit"
    registry = RedactorRegistry([raises_system_exit])
    result = registry.apply("data")

    assert result is not None
    sentinel = json.loads(result)
    assert sentinel["error_class"] == "SystemExit"


def test_base_exception_subclass_does_not_propagate():
    """Any BaseException subclass must be swallowed, not leaked to caller."""
    class WeirdSignal(BaseException):
        pass

    def raises_weird(payload: str) -> str:
        raise WeirdSignal("secret info")

    raises_weird.__name__ = "raises_weird"
    registry = RedactorRegistry([raises_weird])
    # Must NOT raise — must return a sentinel string
    result = registry.apply("payload")
    assert result is not None
    sentinel = json.loads(result)
    assert sentinel["error_class"] == "WeirdSignal"
    assert "secret info" not in result


# ---------------------------------------------------------------------------
# Sentinel with extras: TranscriptWriter injects ts, seq, step_path
# ---------------------------------------------------------------------------


def test_sentinel_includes_standard_event_fields(tmp_path):
    """Sentinel written by TranscriptWriter must include ts, seq, step_path."""
    def bad(payload: str) -> str:
        raise RuntimeError("boom")

    bad.__name__ = "bad"

    with TranscriptWriter(tmp_path / "run", run_id="test", redactors=[bad]) as tw:
        tw.write_event("some_op", step_path=["parent", "child"])

    events = _read_events(tmp_path / "run")
    assert len(events) == 1
    sentinel = events[0]
    assert "ts" in sentinel
    assert "seq" in sentinel
    assert sentinel["step_path"] == ["parent", "child"]
    assert sentinel["op"] == "redactor.error"


# ---------------------------------------------------------------------------
# Redactor name resolution
# ---------------------------------------------------------------------------


def test_redactor_name_from_dunder_name():
    """Redactor name is taken from __name__ when available."""
    def my_custom_redactor(p: str) -> str:
        raise ValueError()

    registry = RedactorRegistry([my_custom_redactor])
    result = registry.apply("x")
    sentinel = json.loads(result)
    assert sentinel["redactor"] == "my_custom_redactor"


def test_redactor_name_fallback_for_nameless_callable():
    """When __name__ is absent, name falls back to repr(type(callable))."""
    class NoName:
        def __call__(self, p: str) -> str:
            raise ValueError()
        # Deliberately no __name__ attribute

    instance = NoName()
    registry = RedactorRegistry([instance])
    result = registry.apply("x")
    sentinel = json.loads(result)
    # Should not be empty; should reference the class somehow
    assert sentinel["redactor"]  # non-empty
    assert "NoName" in sentinel["redactor"] or "<" in sentinel["redactor"]


# ---------------------------------------------------------------------------
# No-op: registry with redactors that all pass-through
# ---------------------------------------------------------------------------


def test_passthrough_redactors_write_all_events(tmp_path):
    """Passthrough redactors (return payload unchanged) write all events."""
    identity = lambda p: p

    with TranscriptWriter(tmp_path / "run", run_id="test", redactors=[identity]) as tw:
        tw.write_event("e1")
        tw.write_event("e2")

    events = _read_events(tmp_path / "run")
    assert len(events) == 2
    assert events[0]["op"] == "e1"
    assert events[1]["op"] == "e2"


# ---------------------------------------------------------------------------
# Redactor that transforms content
# ---------------------------------------------------------------------------


def test_redactor_transforms_payload(tmp_path):
    """A redactor that modifies the payload string is reflected in the transcript."""
    def redact_key(payload: str) -> str:
        return payload.replace("sk-secret", "sk-***")

    with TranscriptWriter(tmp_path / "run", run_id="test", redactors=[redact_key]) as tw:
        tw.write_event("api_call", key="sk-secret")

    events = _read_events(tmp_path / "run")
    assert len(events) == 1
    raw = (tmp_path / "run" / "transcript.jsonl").read_text()
    assert "sk-***" in raw
    assert "sk-secret" not in raw
