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
    assert registry._redactors == ()
    assert registry.redactors == ()
    assert len(registry.redactors) == 0


def test_registry_is_immutable_post_init():
    """Registered redactors cannot be mutated after construction (NIT-2)."""
    def r(s):
        return s
    registry = RedactorRegistry([r])
    # Tuple has no append / mutating list methods.
    assert isinstance(registry._redactors, tuple)
    with pytest.raises(AttributeError):
        registry._redactors.append(r)  # type: ignore[attr-defined]


def test_registry_isolated_from_input_list():
    """Mutating the caller's list after construction does not affect the registry."""
    def r1(s):
        return s + "_1"
    def r2(s):
        return s + "_2"
    caller_list = [r1]
    registry = RedactorRegistry(caller_list)
    caller_list.append(r2)  # mutate AFTER construction
    # Registry still sees only r1 — the tuple was frozen at construction time.
    assert registry.redactors == (r1,)
    assert registry.apply("x") == "x_1"


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
        r1 = tw.write_event("step_start", step_path=["my_step"])
        r2 = tw.write_event("step_end", step_path=["my_step"])
        # WARN-1 fix: dropped events return None, not a (rolled-back) seq.
        assert r1 is None
        assert r2 is None

    events = _read_events(tmp_path / "run")
    assert events == []


def test_dropped_event_returns_none_then_next_write_reuses_seq(tmp_path):
    """WARN-1 regression: a dropped event returns None; the NEXT real write
    gets a clean contiguous seq (no gap, no collision)."""
    drop_next = {"flag": True}

    def drop_when_flagged(payload: str) -> "str | None":
        if drop_next["flag"]:
            return None
        return payload

    with TranscriptWriter(tmp_path / "run", run_id="test", redactors=[drop_when_flagged]) as tw:
        seq_dropped = tw.write_event("dropped_event")
        assert seq_dropped is None, "Dropped event must return None, not a seq"

        drop_next["flag"] = False
        seq_written = tw.write_event("written_event")
        assert seq_written == 1, "Next real write reuses seq 1 (contiguous, no gap)"

        seq_second = tw.write_event("second_written")
        assert seq_second == 2

    events = _read_events(tmp_path / "run")
    assert [e["op"] for e in events] == ["written_event", "second_written"]
    assert [e["seq"] for e in events] == [1, 2]


def test_written_event_returns_int_seq(tmp_path):
    """Non-dropped events still return an int seq (no regression)."""
    with TranscriptWriter(tmp_path / "run", run_id="test") as tw:
        seq = tw.write_event("normal_event")
        assert isinstance(seq, int)
        assert seq == 1


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
# sentinel_extras guard (NIT-1)
# ---------------------------------------------------------------------------


def test_sentinel_extras_rejects_unknown_keys():
    """NIT-1: apply() must reject sentinel_extras with keys outside the allowlist.

    Prevents accidental payload content from leaking into a sentinel event via
    caller-side extras.
    """
    registry = RedactorRegistry([lambda s: s])
    with pytest.raises(ValueError, match="unknown key"):
        registry.apply("x", sentinel_extras={"ts": "now", "payload": "SECRET"})


def test_sentinel_extras_accepts_all_allowed_keys():
    """All four allowlisted keys can be supplied together without error."""
    def raises(payload: str) -> str:
        raise ValueError()

    raises.__name__ = "raises"
    registry = RedactorRegistry([raises])
    result = registry.apply(
        '{"op":"x"}',
        sentinel_extras={
            "ts": "2026-01-01T00:00:00Z",
            "seq": 42,
            "step_path": ["a"],
            "stream_path": ["b"],
        },
    )
    sentinel = json.loads(result)
    assert sentinel["seq"] == 42
    assert sentinel["ts"] == "2026-01-01T00:00:00Z"
    assert sentinel["step_path"] == ["a"]
    assert sentinel["stream_path"] == ["b"]


def test_sentinel_extras_empty_dict_is_fine():
    """Passing an empty dict (or None) must not raise."""
    registry = RedactorRegistry([lambda s: s])
    assert registry.apply("x", sentinel_extras={}) == "x"
    assert registry.apply("x", sentinel_extras=None) == "x"


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
    def identity(p):
        return p

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
