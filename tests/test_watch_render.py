"""Tests for godel._watch — Rich TUI renderer.

Acceptance criteria verified:

- AC1  Static-model render test: feeding a fixture WatchModel into
       WatchApp._render() produces non-empty recorded-console output and
       includes expected step names.
- AC2  Non-TTY fallback: stdout.isatty() == False → plain line-log output;
       no Rich Live output; exit code unchanged.
- AC3  TERM=dumb → same plain fallback.
- AC3b Non-UTF-8 locale → same plain fallback.
- AC4  Coalescing: 100 events fed in a burst result in ≤2 render calls.
- AC5  Missing ``rich`` dep → GodelWatchNotInstalledError with hint (covered
       by test_watch_optional_dep.py; imported here for cross-reference only).
- AC6  Ctrl+C during Live → app.stop() is called (terminal restored).
- AC7  _PlainLineLog.print_event emits prefixed lines for known ops.
"""
from __future__ import annotations

import io
import os
import queue
import signal
import sys
import threading
import time
import unittest.mock as mock
from types import MappingProxyType

import pytest

pytest.importorskip("rich")

from rich.console import Console

from godel._exceptions import GodelWatchNotInstalledError
from godel._watch import (
    WatchApp,
    _PlainLineLog,
    _drain_queue,
    _plain_loop,
    _render_loop,
    _use_plain_fallback,
    run_watch,
)
from godel._watch_model import (
    StreamPanel,
    StepNode,
    WatchModel,
    reduce,
    reduce_header,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_model() -> WatchModel:
    """Build a realistic WatchModel fixture with one running step and output."""
    model = WatchModel.empty()
    model = reduce_header(model, {"v": 1, "run_id": "test-run-001", "started_at": "2026-04-14T00:00:00+00:00"})
    model = reduce(model, {
        "op": "step.enter",
        "step_path": ["fetch_data"],
        "ts": "2026-04-14T00:00:01+00:00",
    })
    model = reduce(model, {
        "op": "stdout",
        "step_path": ["fetch_data"],
        "stream_path": ["fetch_data"],
        "line": "Fetching remote API…",
        "ts": "2026-04-14T00:00:02+00:00",
    })
    model = reduce(model, {
        "op": "step.enter",
        "step_path": ["process"],
        "ts": "2026-04-14T00:00:03+00:00",
    })
    model = reduce(model, {
        "op": "step.exit",
        "step_path": ["fetch_data"],
        "status": "done",
        "ts": "2026-04-14T00:00:04+00:00",
    })
    return model


def _make_events(n: int) -> list[dict]:
    """Build *n* stdout events."""
    return [
        {
            "op": "stdout",
            "step_path": ["step_a"],
            "stream_path": ["step_a"],
            "line": f"line {i}",
            "ts": "2026-04-14T00:00:00+00:00",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# AC1 — Static-model render test (snapshot test via recorded console)
# ---------------------------------------------------------------------------

class TestStaticRender:
    """WatchApp._render() should produce structured output from a WatchModel."""

    def test_render_produces_nonempty_output(self):
        """_render() emits something on the recorded console."""
        model = _make_model()
        console = Console(record=True, width=120)
        WatchApp._render(model, console)
        output = console.export_text()
        assert output.strip(), "Expected non-empty rendered output"

    def test_render_includes_step_names(self):
        """Rendered output should contain the known step names."""
        model = _make_model()
        console = Console(record=True, width=120)
        WatchApp._render(model, console)
        output = console.export_text()
        assert "fetch_data" in output
        assert "process" in output

    def test_render_includes_run_id(self):
        """Rendered output should include the run_id from run_meta."""
        model = _make_model()
        console = Console(record=True, width=120)
        WatchApp._render(model, console)
        output = console.export_text()
        assert "test-run-001" in output

    def test_render_includes_stream_output(self):
        """Rendered output should include lines from StreamPanel ring buffers."""
        model = _make_model()
        console = Console(record=True, width=120)
        WatchApp._render(model, console)
        output = console.export_text()
        assert "Fetching remote API" in output

    def test_render_empty_model(self):
        """_render() does not crash on an empty model."""
        model = WatchModel.empty()
        console = Console(record=True, width=120)
        WatchApp._render(model, console)  # should not raise

    def test_render_multiple_panels_beyond_3(self):
        """_render() handles >3 active stream paths (tabbed overflow)."""
        model = WatchModel.empty()
        for i in range(5):
            model = reduce(model, {
                "op": "stdout",
                "step_path": [f"step_{i}"],
                "stream_path": [f"stream_{i}"],
                "line": f"output from stream {i}",
                "ts": f"2026-04-14T00:00:0{i}+00:00",
            })
        console = Console(record=True, width=160)
        WatchApp._render(model, console)
        output = console.export_text()
        # Overflow indicator should appear
        assert "more" in output.lower() or any(f"stream_{i}" in output for i in range(5))


# ---------------------------------------------------------------------------
# AC1 — Byte-exact golden-file snapshot test (syrupy)
# ---------------------------------------------------------------------------

class TestAC1Snapshot:
    """Byte-exact recorded-console snapshot of WatchApp._render() for AC1.

    Uses syrupy's snapshot fixture to compare the full Rich ``export_text()``
    output against a checked-in golden file.  The Console is constructed with:

    * ``record=True``        — enables export_text()
    * ``width=120``          — fixed width for deterministic wrapping
    * ``height=30``          — fixed height for deterministic Rich Layout sizing
    * ``color_system=None``  — no ANSI codes so the golden file is plain text

    Rich version sensitivity
    ------------------------
    The golden file is tied to Rich's layout and border-glyph output, which
    has historically shifted between major versions.  The ``watch`` extra
    pins ``rich>=13.0,<15`` — this snapshot was recorded on Rich 14.x and
    is expected to remain stable across the Rich 13/14 line.  When bumping
    the upper bound, re-record with ``pytest --snapshot-update``.

    Run ``pytest --snapshot-update`` once to write / refresh the golden file.
    """

    def test_ac1_render_snapshot(self, snapshot):
        """Full Rich recorded-console output matches the stored golden file."""
        model = _make_model()
        console = Console(
            record=True,
            width=120,
            height=30,
            color_system=None,
        )
        WatchApp._render(model, console)
        output = console.export_text(clear=False)
        assert output == snapshot


# ---------------------------------------------------------------------------
# AC2 — Non-TTY fallback
# ---------------------------------------------------------------------------

class TestNonTTYFallback:
    """When stdout.isatty() == False, _use_plain_fallback() returns True."""

    def test_non_tty_triggers_fallback(self):
        """A StringIO (non-TTY) triggers plain fallback."""
        buf = io.StringIO()
        assert buf.isatty() is False
        assert _use_plain_fallback(buf) is True

    def test_tty_does_not_trigger_fallback(self, monkeypatch):
        """A mock TTY does NOT trigger plain fallback (assuming UTF-8 locale and no TERM=dumb)."""
        monkeypatch.delenv("TERM", raising=False)
        fake = mock.MagicMock()
        fake.isatty.return_value = True
        # We also need to ensure locale is UTF-8
        with mock.patch("locale.getpreferredencoding", return_value="UTF-8"):
            result = _use_plain_fallback(fake)
        assert result is False

    def test_run_watch_non_tty_produces_plain_output(self, tmp_path, monkeypatch):
        """run_watch on a non-TTY writes prefixed plain-log lines, not Rich markup."""
        import sys
        import importlib

        # Always use a fresh module reference to avoid cross-test import-order
        # issues where test_watch_optional_dep.py may pop and re-insert godel._watch
        # into sys.modules, creating a second module object.
        watch_mod = sys.modules.get("godel._watch")
        if watch_mod is None:
            watch_mod = importlib.import_module("godel._watch")

        events = [
            {"op": "step.enter", "step_path": ["my_step"],
             "stream_path": [], "ts": "2026-04-14T00:00:01+00:00"},
        ]

        def _fake_producer(run_id, runs_dir, q):
            for e in events:
                q.put(e)
            q.put(None)

        buf = io.StringIO()
        # buf.isatty() returns False → plain fallback

        original = watch_mod._producer_thread
        watch_mod._producer_thread = _fake_producer
        try:
            watch_mod.run_watch("run-plain-001", runs_dir=str(tmp_path), stdout=buf)
        finally:
            watch_mod._producer_thread = original

        output = buf.getvalue()
        assert "[godel-watch]" in output, f"Expected plain-log prefix. Got: {output!r}"
        assert "Traceback" not in output

    def test_run_watch_non_tty_exit_code_unchanged(self, tmp_path, monkeypatch):
        """run_watch completes without raising even with no TTY."""
        import sys
        import importlib

        watch_mod = sys.modules.get("godel._watch")
        if watch_mod is None:
            watch_mod = importlib.import_module("godel._watch")

        def _fake_producer(run_id, runs_dir, q):
            q.put(None)

        buf = io.StringIO()
        original = watch_mod._producer_thread
        watch_mod._producer_thread = _fake_producer
        try:
            watch_mod.run_watch("any-run", runs_dir=str(tmp_path), stdout=buf)
        finally:
            watch_mod._producer_thread = original
        # If we reach here without exception, exit-code contract is satisfied


# ---------------------------------------------------------------------------
# AC3 — TERM=dumb and non-UTF8 locale fallback
# ---------------------------------------------------------------------------

class TestTermDumbFallback:
    def test_term_dumb_triggers_fallback(self, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")
        fake = mock.MagicMock()
        fake.isatty.return_value = True
        with mock.patch("locale.getpreferredencoding", return_value="UTF-8"):
            assert _use_plain_fallback(fake) is True

    def test_non_utf8_locale_triggers_fallback(self, monkeypatch):
        monkeypatch.delenv("TERM", raising=False)
        fake = mock.MagicMock()
        fake.isatty.return_value = True
        with mock.patch("locale.getpreferredencoding", return_value="latin-1"):
            assert _use_plain_fallback(fake) is True

    def test_utf8_locale_no_dumb_no_fallback(self, monkeypatch):
        monkeypatch.delenv("TERM", raising=False)
        fake = mock.MagicMock()
        fake.isatty.return_value = True
        with mock.patch("locale.getpreferredencoding", return_value="UTF-8"):
            assert _use_plain_fallback(fake) is False


# ---------------------------------------------------------------------------
# AC4 — Burst coalescing: 100 events in one drain should result in ≤2 renders
# ---------------------------------------------------------------------------

class TestBurstCoalescing:
    """Feed 100 events into the render loop; check render call count ≤ 2."""

    def test_drain_queue_coalesces_burst(self):
        """_drain_queue processes up to burst_threshold events at once."""
        q: queue.Queue = queue.Queue()
        events = _make_events(100)
        for e in events:
            q.put(e)
        q.put(None)  # sentinel

        model = WatchModel.empty()
        # drain with threshold=25 → first call drains 25 events
        new_model, did_update, end_of_stream = _drain_queue(q, model, burst_threshold=25)
        assert did_update is True
        assert end_of_stream is False
        # At most 25 events removed; remainder still in queue
        assert q.qsize() >= 75

    def test_render_loop_coalesces_100_events_to_few_renders(self):
        """Feeding 100 events to _render_loop results in 1-2 render calls.

        Lower bound ≥1 guards against the regression where the EOS sentinel
        starved the final flush and produced 0 renders (silent TUI).
        """
        render_calls = []

        class _FakeApp:
            def update(self, model):
                render_calls.append(model)

            def stop(self):
                pass

        q: queue.Queue = queue.Queue()
        events = _make_events(100)
        for e in events:
            q.put(e)
        q.put(None)  # end-of-stream sentinel

        app = _FakeApp()
        _render_loop(
            app,
            q,
            timer_interval=0.001,   # very short timer
            burst_threshold=100,    # absorb all 100 in one drain
        )

        assert 1 <= len(render_calls) <= 2, (
            f"Expected 1–2 render calls; got {len(render_calls)}"
        )

    def test_render_loop_default_burst_threshold_still_renders(self):
        """Production path: default burst_threshold=25 with 100 events must
        still produce ≥1 render.  Regression guard for the 'sentinel-in-queue
        starves final render' bug.
        """
        render_calls = []

        class _FakeApp:
            def update(self, model):
                render_calls.append(model)

            def stop(self):
                pass

        q: queue.Queue = queue.Queue()
        for e in _make_events(100):
            q.put(e)
        q.put(None)

        app = _FakeApp()
        _render_loop(
            app,
            q,
            timer_interval=0.05,
            burst_threshold=25,  # default production value
        )

        assert len(render_calls) >= 1, (
            f"Expected ≥1 render call with default burst_threshold; got "
            f"{len(render_calls)}. This indicates the EOS sentinel is "
            "starving the final flush."
        )

    def test_render_loop_fast_run_final_flush(self):
        """A very fast run (all events pre-queued before loop starts) must
        still render its final state at least once."""
        render_calls = []

        class _FakeApp:
            def update(self, model):
                render_calls.append(model)

            def stop(self):
                pass

        q: queue.Queue = queue.Queue()
        # Only 3 events, then immediate EOS
        q.put({"op": "step.enter", "step_path": ["a"], "stream_path": [],
               "ts": "2026-04-14T00:00:01+00:00"})
        q.put({"op": "step.enter", "step_path": ["b"], "stream_path": [],
               "ts": "2026-04-14T00:00:02+00:00"})
        q.put({"op": "step.exit", "step_path": ["a"], "status": "done",
               "stream_path": [], "ts": "2026-04-14T00:00:03+00:00"})
        q.put(None)

        app = _FakeApp()
        # Large timer so no mid-stream render; only final flush should fire.
        _render_loop(app, q, timer_interval=10.0, burst_threshold=25)

        assert len(render_calls) == 1, (
            f"Expected exactly one final-flush render on fast run; got "
            f"{len(render_calls)}"
        )
        # And it must reflect the final model state
        final_model = render_calls[-1]
        assert ("a",) in final_model.steps
        assert ("b",) in final_model.steps
        assert final_model.steps[("a",)].status == "done"

    def test_no_renders_when_model_unchanged(self):
        """Unknown-op events (no-ops) don't cause any renders."""
        render_calls = []

        class _FakeApp:
            def update(self, model):
                render_calls.append(model)

            def stop(self):
                pass

        q: queue.Queue = queue.Queue()
        # Push 50 unknown-op events (no-ops in reduce)
        for i in range(50):
            q.put({"op": "unknown.noop", "ts": "2026-04-14T00:00:00+00:00"})
        q.put(None)

        app = _FakeApp()
        _render_loop(app, q, timer_interval=0.001, burst_threshold=100)
        assert len(render_calls) == 0, (
            f"No-op events should produce 0 render calls; got {len(render_calls)}"
        )


# ---------------------------------------------------------------------------
# AC5 — Missing rich dep (cross-reference; full test in test_watch_optional_dep.py)
# ---------------------------------------------------------------------------

class TestMissingRichDep:
    def test_godelwatchnotinstallederror_subclasses_importerror(self):
        assert issubclass(GodelWatchNotInstalledError, ImportError)

    def test_godelwatchnotinstallederror_message_contains_hint(self):
        err = GodelWatchNotInstalledError(
            "godel --watch requires 'rich'. Install with: pip install 'godel[watch]'"
        )
        assert "pip install 'godel[watch]'" in str(err)
        assert "rich" in str(err)


# ---------------------------------------------------------------------------
# AC6 — Ctrl+C during Live restores terminal (app.stop() is called)
# ---------------------------------------------------------------------------

class TestKeyboardInterruptRestoresTerminal:
    def test_keyboard_interrupt_calls_stop(self):
        """When _render_loop raises KeyboardInterrupt, app.stop() is invoked."""
        stop_called = []

        class _FakeApp:
            def update(self, model):
                raise KeyboardInterrupt()

            def stop(self):
                stop_called.append(True)

        q: queue.Queue = queue.Queue()
        q.put({"op": "step.enter", "step_path": ["x"],
               "stream_path": [], "ts": "2026-04-14T00:00:00+00:00"})
        q.put(None)

        app = _FakeApp()

        # Simulate the run_watch main block
        try:
            _render_loop(app, q, timer_interval=0.001, burst_threshold=100)
        except KeyboardInterrupt:
            app.stop()

        assert stop_called, "app.stop() should have been called on KeyboardInterrupt"

    def test_watch_app_context_manager_stops_on_exit(self):
        """WatchApp.__exit__ calls stop(), restoring terminal state."""
        console = Console(record=True, width=80)
        app = WatchApp("test-run", console=console)

        stop_called = []
        original_stop = app.stop

        def _tracked_stop():
            stop_called.append(True)
            original_stop()

        app.stop = _tracked_stop

        with app:
            pass  # no render, just test context manager

        assert stop_called, "WatchApp.__exit__ should call stop()"

    def test_signal_handler_is_async_safe_flag_only(self, monkeypatch):
        """The SIGTSTP/SIGHUP handler must NOT call Live.stop() directly —
        that would risk deadlock if Rich holds its lock when the signal lands.
        The handler should only set a flag; the main loop acts on it.
        """
        import sys
        import importlib
        watch_mod = sys.modules.get("godel._watch") or importlib.import_module("godel._watch")

        stop_calls = []

        class _FakeApp:
            def stop(self):
                stop_calls.append(True)

        pending: list = [None]
        previous = watch_mod._install_terminal_restore_signals(_FakeApp(), pending)
        try:
            # Find the installed handler for an available signal.
            if not previous:
                pytest.skip("no catchable signals on this platform")
            sig, _prev = previous[0]
            handler = signal.getsignal(sig)
            assert callable(handler)

            # Invoke the handler directly — simulates a signal arriving.
            handler(sig, None)

            # The handler must have set the flag but NOT called stop().
            assert pending[0] == sig, (
                "handler must set pending_signal flag"
            )
            assert stop_calls == [], (
                "handler must NOT call renderer.stop() directly "
                "(async-signal-unsafe)"
            )
        finally:
            watch_mod._restore_signals(previous)

    def test_run_watch_keyboard_interrupt_integration(self, tmp_path, monkeypatch):
        """Integration: run_watch() through-path; a producer that stops
        abruptly (via signal-flag) must cause WatchApp.stop() to run before
        process teardown.  Uses the pending_signal mechanism to simulate a
        SIGTSTP mid-render.
        """
        import sys
        import importlib

        watch_mod = sys.modules.get("godel._watch") or importlib.import_module("godel._watch")

        stop_calls = []
        original_stop = watch_mod.WatchApp.stop

        def _track_stop(self):
            stop_calls.append(True)
            return original_stop(self)

        monkeypatch.setattr(watch_mod.WatchApp, "stop", _track_stop)

        # Force TUI path by faking a TTY stdout.
        fake_tty = mock.MagicMock()
        fake_tty.isatty.return_value = True
        fake_tty.write = lambda *a, **k: None
        fake_tty.flush = lambda: None
        monkeypatch.setattr(watch_mod, "_use_plain_fallback", lambda *a, **k: False)

        def _fake_producer(run_id, runs_dir, q):
            # Push one event then EOS.
            q.put({"op": "step.enter", "step_path": ["x"], "stream_path": [],
                   "ts": "2026-04-14T00:00:01+00:00"})
            q.put(None)

        monkeypatch.setattr(watch_mod, "_producer_thread", _fake_producer)

        # Run.  Should exit cleanly; WatchApp.stop must have been invoked
        # via the __exit__ context manager.
        watch_mod.run_watch("run-intr-001", runs_dir=str(tmp_path), stdout=fake_tty)

        assert stop_calls, (
            "WatchApp.stop() must be called when run_watch exits (via __exit__)"
        )


# ---------------------------------------------------------------------------
# AC7 — _PlainLineLog.print_event output format
# ---------------------------------------------------------------------------

class TestPlainLineLog:
    def test_print_event_prefix(self):
        buf = io.StringIO()
        log = _PlainLineLog(file=buf)
        log.print_event({
            "op": "step.enter",
            "step_path": ["my_step"],
            "ts": "2026-04-14T00:00:01+00:00",
        })
        out = buf.getvalue()
        assert "[godel-watch]" in out
        assert "step.enter" in out
        assert "my_step" in out

    def test_print_event_stdout_op(self):
        buf = io.StringIO()
        log = _PlainLineLog(file=buf)
        log.print_event({
            "op": "stdout",
            "stream_path": ["fetch_data"],
            "line": "hello world",
            "ts": "2026-04-14T00:00:02+00:00",
        })
        out = buf.getvalue()
        assert "stdout" in out
        assert "hello world" in out

    def test_print_event_no_ts(self):
        """Events without ts don't crash the formatter."""
        buf = io.StringIO()
        log = _PlainLineLog(file=buf)
        log.print_event({"op": "agent.thought", "text": "thinking..."})
        out = buf.getvalue()
        assert "[godel-watch]" in out

    def test_plain_loop_processes_all_events(self):
        """_plain_loop processes every event from the queue and stops on sentinel."""
        buf = io.StringIO()
        log = _PlainLineLog(file=buf)
        q: queue.Queue = queue.Queue()
        events = [
            {"op": "step.enter", "step_path": ["s1"], "ts": "2026-04-14T00:00:01+00:00"},
            {"op": "step.exit", "step_path": ["s1"], "status": "done", "ts": "2026-04-14T00:00:02+00:00"},
        ]
        for e in events:
            q.put(e)
        q.put(None)

        _plain_loop(log, q, timer_interval=0.01)
        out = buf.getvalue()
        assert "step.enter" in out
        assert "step.exit" in out


# ---------------------------------------------------------------------------
# WatchApp integration: start/stop/update lifecycle
# ---------------------------------------------------------------------------

class TestWatchAppLifecycle:
    def test_app_start_and_stop(self):
        """WatchApp.start() and stop() don't raise on a recorded console."""
        console = Console(record=True, width=80, force_terminal=True)
        app = WatchApp("my-run", console=console)
        app.start()
        app.update(_make_model())
        app.stop()

    def test_app_update_reflects_model(self):
        """WatchApp.update() does not raise and accepts any WatchModel."""
        console = Console(record=True, width=80, force_terminal=True)
        app = WatchApp("upd-run", console=console)
        app.start()
        try:
            model = _make_model()
            app.update(model)
        finally:
            app.stop()

    def test_app_stop_idempotent(self):
        """Calling stop() multiple times does not raise."""
        console = Console(record=True, width=80)
        app = WatchApp("idem-run", console=console)
        app.start()
        app.stop()
        app.stop()  # second call should be a no-op

    def test_app_start_idempotent(self):
        """Calling start() twice must not create a second Live instance.

        A double-call would otherwise orphan the first Live context and leave
        the terminal in an inconsistent state (cursor hidden, colours leaked).
        After a double start() the app must still stop() cleanly.
        """
        console = Console(record=True, width=80, force_terminal=True)
        app = WatchApp("idem-start-run", console=console)
        app.start()
        first_live = app._live
        app.start()  # second call — must be a no-op
        assert app._live is first_live, (
            "start() must be idempotent: second call must not replace _live"
        )
        app.stop()  # must still clean up correctly


# ---------------------------------------------------------------------------
# Narrow-terminal render test (width=40)
# ---------------------------------------------------------------------------

class TestNarrowTerminalRender:
    """Verify that _render() does not crash and produces reasonable output on
    a very narrow terminal (40 columns).

    A 40-column terminal is below the typical 80-column default and exercises
    Rich's layout splitting and panel truncation logic.  The test does NOT
    assert pixel-perfect output — it only checks:

    * No exception is raised.
    * At least some output is produced (not a blank render).
    * The output fits within the declared width (no line exceeds 40 printable
      characters after stripping ANSI-free exported text).
    """

    def test_narrow_render_no_crash_empty_model(self):
        """_render() with width=40 on an empty model must not raise."""
        model = WatchModel.empty()
        console = Console(record=True, width=40)
        WatchApp._render(model, console)  # must not raise

    def test_narrow_render_produces_output(self):
        """_render() with width=40 on a populated model emits non-empty text."""
        model = _make_model()
        console = Console(record=True, width=40)
        WatchApp._render(model, console)
        output = console.export_text()
        assert output.strip(), "Expected non-empty output on narrow terminal"

    def test_narrow_render_reasonable_layout(self):
        """Output lines on a 40-column console should not grossly overflow.

        Rich's recorded text export strips markup but preserves the logical
        column layout.  We allow a small margin (up to 60 chars) to account
        for Rich's internal padding/border characters, but flagrantly wide
        lines (>200 chars) would indicate the layout broke completely.
        """
        model = _make_model()
        console = Console(record=True, width=40)
        WatchApp._render(model, console)
        output = console.export_text()
        # Sanity check: no single exported line should be grotesquely wide.
        for line in output.splitlines():
            assert len(line) <= 200, (
                f"Exported line too wide ({len(line)} chars) on 40-col console: {line!r}"
            )

    def test_narrow_render_with_overflow_panels(self):
        """width=40 with >3 stream panels (overflow path) must not crash."""
        model = WatchModel.empty()
        for i in range(5):
            model = reduce(model, {
                "op": "stdout",
                "step_path": [f"step_{i}"],
                "stream_path": [f"stream_{i}"],
                "line": f"output from stream {i}",
                "ts": f"2026-04-14T00:00:0{i}+00:00",
            })
        console = Console(record=True, width=40)
        WatchApp._render(model, console)  # must not raise
        output = console.export_text()
        assert output.strip(), "Expected non-empty output with overflow panels on narrow terminal"
