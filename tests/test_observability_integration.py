"""Integration test suite for observability — godel-py-5pl.16.

Four scenarios:

(a) rotation-during-read
    Force rotation with a reader attached; assert no missed/duplicated events
    and no gaps in seq.

(b) capture_stdout under parallel
    Two @step(capture_stdout=True) steps inside parallel() must raise
    ConfigError.  Two sequential capturing steps must work independently with
    distinct stream_paths (regression for the non-parallel path).

(c) late-attach
    Start a run writing to transcript; sleep 500 ms; attach
    TranscriptTail.from_run(run_id); assert all pre-attach events are
    replayed, then live events arrive.

(d) replay-with-watch
    Run a workflow to completion; replay the finished transcript through the
    WatchModel reducer; assert the final WatchModel equals the one built from
    the live run.

Wall-clock budget: the whole file must run in under 30 s.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from godel._decorators import parallel, step, workflow
from godel._exceptions import ConfigError
from godel._stdout_capture import capture
from godel._tail import TranscriptTail
from godel._transcript import TranscriptWriter, _FILENAME
from godel._watch_model import WatchModel, reduce, reduce_header


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_real_events(run_dir: Path) -> list[dict]:
    """Return all non-sentinel, non-header event dicts from a completed transcript."""
    events: list[dict] = []
    # Walk from oldest archive to current file.
    archives: list[Path] = []
    i = 1
    while True:
        p = run_dir / f"{_FILENAME}.{i}"
        if p.exists():
            archives.append(p)
            i += 1
        else:
            break
    archives.reverse()  # oldest-first
    archives.append(run_dir / _FILENAME)

    for path in archives:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "event" in obj:
                    evt = obj["event"]
                    if evt.get("op") != "rotate":
                        events.append(evt)
    return events


def _replay_transcript_to_model(run_dir: Path) -> WatchModel:
    """Replay every line of a completed transcript through the WatchModel reducer."""
    model = WatchModel.empty()
    archives: list[Path] = []
    i = 1
    while True:
        p = run_dir / f"{_FILENAME}.{i}"
        if p.exists():
            archives.append(p)
            i += 1
        else:
            break
    archives.reverse()  # oldest-first
    archives.append(run_dir / _FILENAME)

    for path in archives:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "header" in obj:
                    model = reduce_header(model, obj["header"])
                elif "event" in obj:
                    model = reduce(model, obj["event"])
    return model


# ---------------------------------------------------------------------------
# (a) Rotation-during-read
# ---------------------------------------------------------------------------


def test_a_rotation_during_read_no_gaps_no_dups(tmp_path):
    """Force rotation with a reader attached; assert no missed/duplicated events.

    Diagnostic: on failure, reports the first seq gap (or dup) found so the
    exact failure location is immediately actionable.
    """
    run_dir = tmp_path / "run_a"
    n_events = 5000
    # max_bytes=4096 forces many rotations over 5000 events.
    max_bytes = 4096
    poll = 0.01

    results: list[dict] = []
    reader_errors: list[Exception] = []
    done_event = threading.Event()

    tail = TranscriptTail(run_dir, poll_interval=poll, follow=True)

    def _reader():
        try:
            deadline = time.monotonic() + 60.0
            for evt in tail:
                if evt.get("op") == "rotate":
                    continue
                results.append(evt)
                if len(results) >= n_events:
                    done_event.set()
                    break
                if time.monotonic() > deadline:
                    reader_errors.append(
                        TimeoutError(
                            f"Reader timed out at {len(results)}/{n_events} events"
                        )
                    )
                    done_event.set()
                    break
        except Exception as exc:
            reader_errors.append(exc)
            done_event.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Give reader a head-start before the writer opens the file.
    time.sleep(0.02)

    writer_errors: list[Exception] = []

    def _writer():
        try:
            with TranscriptWriter(run_dir, run_id="run_a", max_bytes=max_bytes) as tw:
                for i in range(n_events):
                    tw.write_event("tick", idx=i)
        except Exception as exc:
            writer_errors.append(exc)
            done_event.set()

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    done_event.wait(timeout=60.0)
    writer_thread.join(timeout=5.0)
    reader_thread.join(timeout=5.0)

    assert not writer_errors, f"Writer errors: {writer_errors}"
    assert not reader_errors, f"Reader errors: {reader_errors}"

    got = len(results)
    assert got == n_events, (
        f"Expected {n_events} events, got {got}. "
        f"First 5 seqs: {[e.get('seq') for e in results[:5]]}, "
        f"Last 5 seqs: {[e.get('seq') for e in results[-5:]]}"
    )

    seqs = [e["seq"] for e in results]

    # Check ordering.
    assert seqs == sorted(seqs), (
        "Events not in ascending seq order. "
        f"First out-of-order pair: "
        + str(next((f"{seqs[i-1]}→{seqs[i]}" for i in range(1, len(seqs)) if seqs[i] < seqs[i-1]), "none"))
    )

    # Check for duplicates.
    seen: set[int] = set()
    dups = [s for s in seqs if s in seen or seen.add(s)]  # type: ignore[func-returns-value]
    assert not dups, f"Duplicate seq numbers found: {dups[:10]}"

    # Check for gaps.
    for i in range(1, len(seqs)):
        assert seqs[i] == seqs[i - 1] + 1, (
            f"Gap in seq at index {i}: {seqs[i-1]} → {seqs[i]}"
        )


# ---------------------------------------------------------------------------
# (b) capture_stdout under parallel
# ---------------------------------------------------------------------------


def test_b_capture_stdout_in_parallel_raises_config_error():
    """Two @step(capture_stdout=True) steps inside parallel() raise ConfigError."""

    @step(capture_stdout=True)
    async def cap_step_b1():
        return "b1"

    @step(capture_stdout=True)
    async def cap_step_b2():
        return "b2"

    @workflow
    async def bad_workflow_b():
        await parallel(cap_step_b1(), cap_step_b2())

    with pytest.raises(ConfigError) as exc_info:
        asyncio.run(bad_workflow_b())

    msg = str(exc_info.value)
    assert "capture_stdout" in msg, f"Expected 'capture_stdout' in error, got: {msg!r}"
    assert "parallel" in msg.lower(), f"Expected 'parallel' in error, got: {msg!r}"


def test_b_sequential_capturing_steps_independent_stream_paths(tmp_path):
    """Two sequential capturing steps produce distinct stream_paths — no cross-contamination.

    This is the regression guard for the non-parallel (sequential) path: each
    capturing step must land its stdout events under its own stream_path.
    """
    run_dir = tmp_path / "run_b_seq"
    run_dir.mkdir(parents=True, exist_ok=True)

    step1_lines: list[str] = []
    step2_lines: list[str] = []

    with TranscriptWriter(run_dir, run_id="b_seq") as tw:
        with capture(
            step_path=("step_one",),
            stream_path=["step_one", "stdout"],
            transcript=tw,
        ):
            # Write directly to fd 1 so it bypasses pytest's sys.stdout capture.
            import os as _os
            _os.write(1, b"hello from step one\n")

        with capture(
            step_path=("step_two",),
            stream_path=["step_two", "stdout"],
            transcript=tw,
        ):
            import os as _os
            _os.write(1, b"hello from step two\n")

    # Read all events.
    all_events = _read_real_events(run_dir)
    stdout_events = [e for e in all_events if e.get("op") == "stdout"]

    # Partition by stream_path.
    for evt in stdout_events:
        sp = tuple(evt.get("stream_path", []))
        if sp == ("step_one", "stdout"):
            step1_lines.append(evt.get("chunk", ""))
        elif sp == ("step_two", "stdout"):
            step2_lines.append(evt.get("chunk", ""))

    # Each step's output must appear in its own stream, not the other's.
    assert any("step one" in line for line in step1_lines), (
        f"step_one output not found in step_one stream. step1_lines={step1_lines}"
    )
    assert any("step two" in line for line in step2_lines), (
        f"step_two output not found in step_two stream. step2_lines={step2_lines}"
    )
    # No cross-contamination.
    assert all("step two" not in line for line in step1_lines), (
        f"step_two output leaked into step_one stream: {step1_lines}"
    )
    assert all("step one" not in line for line in step2_lines), (
        f"step_one output leaked into step_two stream: {step2_lines}"
    )


# ---------------------------------------------------------------------------
# (c) Late-attach
# ---------------------------------------------------------------------------


def test_c_late_attach_catches_up_from_file(tmp_path):
    """Attach TranscriptTail.from_run 500 ms after run starts; verify full replay.

    Pre-attach events must be replayed in order; post-attach events arrive live.
    No events missed, no duplicates, no gaps.

    Diagnostic: failure reports event count diff and first gap/dup location.
    """
    run_id = "run_c"
    runs_dir = tmp_path
    run_dir = runs_dir / run_id

    n_pre = 200   # events written before reader attaches
    n_post = 30   # events written after attach
    n_total = n_pre + n_post

    # Use small max_bytes so we get multiple rotations in the pre-attach phase.
    max_bytes = 2048

    writer_paused = threading.Event()
    writer_resume = threading.Event()
    writer_errors: list[Exception] = []
    writer_seqs: list[int] = []

    def _writer():
        try:
            with TranscriptWriter(run_dir, run_id=run_id, max_bytes=max_bytes) as tw:
                for i in range(n_pre):
                    seq = tw.write_event("pre", idx=i)
                    if seq is not None:
                        writer_seqs.append(seq)
                writer_paused.set()
                writer_resume.wait(timeout=10.0)
                for i in range(n_post):
                    seq = tw.write_event("post", idx=i)
                    if seq is not None:
                        writer_seqs.append(seq)
        except Exception as exc:
            writer_errors.append(exc)
            writer_paused.set()

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    # Wait for pre-attach events to be written (replaces fixed sleep with a
    # deterministic signal + 500 ms pause to simulate real late-attach).
    writer_paused.wait(timeout=15.0)
    assert not writer_errors, f"Writer errors before attach: {writer_errors}"

    # Simulate the real-world "late attach" delay.
    time.sleep(0.5)

    tail = TranscriptTail.from_run(
        run_id, runs_dir=runs_dir, poll_interval=0.02, follow=True
    )

    results: list[dict] = []
    reader_errors: list[Exception] = []
    done_event = threading.Event()

    def _reader():
        try:
            deadline = time.monotonic() + 20.0
            for evt in tail:
                if evt.get("op") == "rotate":
                    continue
                results.append(evt)
                if len(results) >= n_total:
                    done_event.set()
                    break
                if time.monotonic() > deadline:
                    reader_errors.append(
                        TimeoutError(
                            f"Late-attach reader timed out at {len(results)}/{n_total}"
                        )
                    )
                    done_event.set()
                    break
        except Exception as exc:
            reader_errors.append(exc)
            done_event.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Give the reader a moment to start replaying archives, then signal writer.
    time.sleep(0.05)
    writer_resume.set()

    done_event.wait(timeout=20.0)
    writer_thread.join(timeout=5.0)
    reader_thread.join(timeout=5.0)

    assert not writer_errors, f"Writer errors: {writer_errors}"
    assert not reader_errors, f"Reader errors: {reader_errors}"

    got = len(results)
    assert got == n_total, (
        f"Late-attach: expected {n_total} events, got {got}. "
        f"Writer produced seqs: first={writer_seqs[0] if writer_seqs else 'none'}, "
        f"last={writer_seqs[-1] if writer_seqs else 'none'}, count={len(writer_seqs)}. "
        f"Reader got: first_seq={results[0].get('seq') if results else 'none'}, "
        f"last_seq={results[-1].get('seq') if results else 'none'}."
    )

    seqs = [e["seq"] for e in results]
    assert seqs == sorted(seqs), (
        "Late-attach events not in order. "
        "First disorder: "
        + str(
            next(
                (f"idx={i}: {seqs[i-1]}→{seqs[i]}" for i in range(1, len(seqs)) if seqs[i] < seqs[i-1]),
                "none",
            )
        )
    )

    seen: set[int] = set()
    dups = [s for s in seqs if s in seen or seen.add(s)]  # type: ignore[func-returns-value]
    assert not dups, f"Late-attach: duplicate seqs: {dups[:10]}"

    for i in range(1, len(seqs)):
        assert seqs[i] == seqs[i - 1] + 1, (
            f"Late-attach: gap at index {i}: seq {seqs[i-1]} → {seqs[i]}"
        )

    # Verify pre-attach events are in the results (replay worked).
    pre_events = [e for e in results if e.get("op") == "pre"]
    assert len(pre_events) == n_pre, (
        f"Expected {n_pre} pre-attach events to be replayed, got {len(pre_events)}"
    )

    post_events = [e for e in results if e.get("op") == "post"]
    assert len(post_events) == n_post, (
        f"Expected {n_post} live (post-attach) events, got {len(post_events)}"
    )


# ---------------------------------------------------------------------------
# (d) Replay-with-watch renders identical model to live
# ---------------------------------------------------------------------------


def test_d_replay_with_watch_identical_to_live(tmp_path):
    """Replay finished transcript through WatchModel reducer == live model.

    Run a workflow to completion while building a live WatchModel via
    TranscriptTail.  Then open a second TranscriptTail on the finished
    transcript (follow=False) and replay it through the same reducer.
    The final WatchModel from replay must equal the live model.
    """
    run_id = "run_d"
    runs_dir = tmp_path
    run_dir = runs_dir / run_id

    n_events = 50
    max_bytes = 2048  # force some rotations to test multi-file replay

    # Write a realistic mix of events: step.enter, stdout lines, step.exit.
    ops_sequence = []
    with TranscriptWriter(run_dir, run_id=run_id, max_bytes=max_bytes) as tw:
        # Simulate a simple workflow: two sequential steps each emitting stdout.
        for step_name in ("fetch", "process"):
            tw.write_event(
                "step.enter",
                step_path=[step_name],
                stream_path=[],
            )
            ops_sequence.append(("step.enter", step_name))

            for j in range(n_events // 4):
                tw.write_event(
                    "stdout",
                    step_path=[step_name],
                    stream_path=[step_name, "stdout"],
                    line=f"line {j} from {step_name}",
                )
                ops_sequence.append(("stdout", step_name))

            tw.write_event(
                "step.exit",
                step_path=[step_name],
                stream_path=[],
                status="done",
            )
            ops_sequence.append(("step.exit", step_name))

    # --- Build live model via TranscriptTail.from_run (follow=False) ---
    live_model = WatchModel.empty()
    live_tail = TranscriptTail.from_run(run_id, runs_dir=runs_dir, follow=False)
    for evt in live_tail:
        live_model = reduce(live_model, evt)

    # --- Build replay model via _replay_transcript_to_model ---
    replay_model = _replay_transcript_to_model(run_dir)

    # The two models must be identical.
    assert live_model.steps == replay_model.steps, (
        f"Step state mismatch.\n"
        f"Live steps:   {dict(live_model.steps)}\n"
        f"Replay steps: {dict(replay_model.steps)}"
    )
    assert live_model.panels == replay_model.panels, (
        f"Panel state mismatch.\n"
        f"Live panels:   {dict(live_model.panels)}\n"
        f"Replay panels: {dict(replay_model.panels)}"
    )

    # Sanity: the model must have observed both steps.
    assert ("fetch",) in live_model.steps, "Expected 'fetch' step in live model"
    assert ("process",) in live_model.steps, "Expected 'process' step in live model"
    assert live_model.steps[("fetch",)].status == "done"
    assert live_model.steps[("process",)].status == "done"

    # Panels should exist for both steps' stdout streams.
    assert ("fetch", "stdout") in live_model.panels, (
        "Expected 'fetch.stdout' panel in live model"
    )
    assert ("process", "stdout") in live_model.panels, (
        "Expected 'process.stdout' panel in live model"
    )
