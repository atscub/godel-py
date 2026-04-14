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
    Start a run writing to transcript; attach TranscriptTail.from_run(run_id)
    after pre-attach events are durable; assert all pre-attach events are
    replayed, then live events arrive.  Also invokes ``godel watch <run_id>``
    via subprocess on a completed run to exercise the CLI resolution path.

(d) replay-with-watch
    Run a workflow to completion (with enough events to force many
    rotations); replay the finished transcript through the WatchModel
    reducer; assert the final WatchModel (steps, panels, run_meta) equals the
    one built from the live tail.

Wall-clock budget: the whole file must run in under 30 s.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
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


PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ordered_transcript_files(run_dir: Path) -> list[Path]:
    """Return transcript files in write order (oldest archive → current).

    Walks ``transcript.jsonl.N`` from highest N down to ``.1``, then appends
    ``transcript.jsonl`` (the live/current file).  Only paths that exist are
    returned.
    """
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
    live = run_dir / _FILENAME
    if live.exists():
        archives.append(live)
    return archives


def _iter_lines(run_dir: Path):
    """Yield (kind, obj) tuples across all transcript files in write order.

    ``kind`` is ``"header"`` or ``"event"``; ``obj`` is the parsed inner dict.
    """
    for path in _ordered_transcript_files(run_dir):
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
                    yield "header", obj["header"]
                elif "event" in obj:
                    yield "event", obj["event"]


def _read_real_events(run_dir: Path) -> list[dict]:
    """Return all non-sentinel event dicts from a completed transcript."""
    return [
        evt
        for kind, evt in _iter_lines(run_dir)
        if kind == "event" and evt.get("op") != "rotate"
    ]


def _replay_transcript_to_model(run_dir: Path) -> WatchModel:
    """Replay every line of a completed transcript through the WatchModel reducer."""
    model = WatchModel.empty()
    for kind, obj in _iter_lines(run_dir):
        if kind == "header":
            model = reduce_header(model, obj)
        else:
            model = reduce(model, obj)
    return model


def _count_rotations(run_dir: Path) -> int:
    n = 0
    while (run_dir / f"{_FILENAME}.{n + 1}").exists():
        n += 1
    return n


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

    assert seqs == sorted(seqs), (
        "Events not in ascending seq order. "
        "First out-of-order pair: "
        + str(
            next(
                (f"{seqs[i-1]}→{seqs[i]}" for i in range(1, len(seqs)) if seqs[i] < seqs[i - 1]),
                "none",
            )
        )
    )

    seen: set[int] = set()
    dups = [s for s in seqs if s in seen or seen.add(s)]  # type: ignore[func-returns-value]
    assert not dups, f"Duplicate seq numbers found: {dups[:10]}"

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

    Ordering contract (important — do not reorder):
      1. Both ``capture()`` contexts must fully exit before reading the
         transcript on disk.  capture() restores fd 1 in its __exit__, then
         joins the reader thread (up to 1 s) to drain the pipe into the
         TranscriptWriter.
      2. TranscriptWriter.__exit__ runs AFTER both captures have returned,
         closing the file and flushing any buffered state.
      3. Only then do we call ``_read_real_events`` — this guarantees every
         ``os.write(1, ...)`` issued inside a capture() context is durable on
         disk before we assert on it.
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
            # Write directly to fd 1 to bypass pytest's sys.stdout capture.
            os.write(1, b"hello from step one\n")
        # capture() __exit__ has returned here → reader thread joined, events
        # for step_one are durable in the transcript file.

        with capture(
            step_path=("step_two",),
            stream_path=["step_two", "stdout"],
            transcript=tw,
        ):
            os.write(1, b"hello from step two\n")
        # Same ordering guarantee for step_two.
    # TranscriptWriter closed → file flushed before we open it for reading.

    all_events = _read_real_events(run_dir)
    stdout_events = [e for e in all_events if e.get("op") == "stdout"]

    for evt in stdout_events:
        sp = tuple(evt.get("stream_path", []))
        if sp == ("step_one", "stdout"):
            step1_lines.append(evt.get("chunk", ""))
        elif sp == ("step_two", "stdout"):
            step2_lines.append(evt.get("chunk", ""))

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
    """Attach TranscriptTail.from_run after a run is underway; verify full replay.

    Deterministic synchronisation (no timing-based sleeps on the hot path):
      * writer_paused:  writer signals pre-attach events are durable.
      * reader_started: reader signals the first live event has been consumed
                        (proves replay of archives is complete and the reader
                        has attached to the live file).
      * writer_resume:  test signals writer to emit the live events.

    Diagnostic: failure reports event count diff and first gap/dup location.
    """
    run_id = "run_c"
    runs_dir = tmp_path
    run_dir = runs_dir / run_id

    n_pre = 200   # events written before reader attaches
    n_post = 30   # events written after attach
    n_total = n_pre + n_post

    # Small max_bytes so we get multiple rotations in the pre-attach phase.
    max_bytes = 2048

    writer_paused = threading.Event()
    writer_resume = threading.Event()
    writer_errors: list[Exception] = []
    writer_seqs: list[int] = []

    def _writer():
        try:
            with TranscriptWriter(run_dir, run_id=run_id, max_bytes=max_bytes) as tw:
                for i in range(n_pre):
                    writer_seqs.append(tw.write_event("pre", idx=i))
                writer_paused.set()
                writer_resume.wait(timeout=10.0)
                for i in range(n_post):
                    writer_seqs.append(tw.write_event("post", idx=i))
        except Exception as exc:
            writer_errors.append(exc)
            writer_paused.set()

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    # Wait for pre-attach events to be durable on disk.
    writer_paused.wait(timeout=15.0)
    assert not writer_errors, f"Writer errors before attach: {writer_errors}"

    # Attach the late reader.
    tail = TranscriptTail.from_run(
        run_id, runs_dir=runs_dir, poll_interval=0.02, follow=True
    )

    results: list[dict] = []
    reader_errors: list[Exception] = []
    reader_started = threading.Event()  # signals: first non-rotate event consumed
    done_event = threading.Event()

    def _reader():
        try:
            deadline = time.monotonic() + 20.0
            for evt in tail:
                if evt.get("op") == "rotate":
                    continue
                results.append(evt)
                if not reader_started.is_set():
                    reader_started.set()
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

    # Deterministic: wait for reader to actually start consuming (first event
    # from the archive replay) before signalling the writer to emit live events.
    # This replaces a fragile time.sleep(0.05).
    assert reader_started.wait(timeout=10.0), (
        "Reader failed to consume any events within 10s"
    )
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

    pre_events = [e for e in results if e.get("op") == "pre"]
    assert len(pre_events) == n_pre, (
        f"Expected {n_pre} pre-attach events to be replayed, got {len(pre_events)}"
    )
    post_events = [e for e in results if e.get("op") == "post"]
    assert len(post_events) == n_post, (
        f"Expected {n_post} live (post-attach) events, got {len(post_events)}"
    )


def test_c_godel_watch_cli_late_attaches_to_completed_run(tmp_path):
    """Exercise the ``godel watch <run_id>`` CLI resolution + archive replay.

    AC(c) calls out ``godel watch <run_id>`` specifically.  Asserting only on
    returncode == 0 is insufficient: a bug that skipped ``_start_files`` (wrong
    inode list, wrong sort order, empty archive scan) would also produce
    exit 0 via the WORKFLOW_FINISHED sentinel in the live file.

    Stronger assertion: run the CLI in plain-log mode (``TERM=dumb``), which
    prints exactly one prefixed line per event (see ``_PlainLineLog``).  Then:

      * Every event written to every archive file has a unique ``step_path``
        (``step_0``, ``step_1``, …).
      * Parse the subprocess's stdout and extract distinct ``step_path`` values.
      * Assert that the set equals the complete expected set.  A bug that
        drops archives would produce strictly fewer distinct step_paths and
        this assertion would fail.
      * As an additional guard, verify ``step_0`` lives in the oldest archive
        (highest ``.N``) and is present in the CLI output — specifically
        catches bugs where ``_start_files`` sort order is reversed.

    Skipped if ``rich`` (required by ``godel[watch]``) is not importable.
    """
    pytest.importorskip("rich", reason="godel[watch] requires rich")

    run_id = "cli-late-attach"
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / run_id

    # Build a multi-file transcript that represents a completed run (rotations
    # happened, then a terminal WORKFLOW_FINISHED sentinel was written).
    # Each step.enter carries a unique, parseable step_path so we can verify
    # per-event delivery from the CLI's plain-mode output.
    n_events = 150
    with TranscriptWriter(run_dir, run_id=run_id, max_bytes=2048) as tw:
        for i in range(n_events):
            tw.write_event(
                "step.enter",
                step_path=[f"step_{i}"],
                stream_path=[],
            )
        tw.write_workflow_finished(status="FINISHED")

    rotations = _count_rotations(run_dir)
    assert rotations >= 2, (
        f"Test needs >= 2 rotations to meaningfully verify archive replay; "
        f"got {rotations}.  Lower max_bytes or add more events."
    )

    # Sanity: confirm step_0 lives in the oldest archive (highest .N suffix).
    # If _start_files sort order ever regresses, step_0 will be missed by a
    # reverse-order bug but not by a simple drop-last-archive bug.
    oldest_archive = run_dir / f"{_FILENAME}.{rotations}"
    oldest_contents = oldest_archive.read_text(encoding="utf-8")
    assert "step_0" in oldest_contents, (
        f"Test setup error: step_0 not in oldest archive {oldest_archive.name}"
    )

    # Invoke the CLI from a cwd where godel is importable (the repo root).
    # Using cwd=tmp_path would break because godel is installed as an editable
    # package and needs to resolve via the repo's pyproject.toml.
    repo_root = Path(__file__).resolve().parent.parent

    # TERM=dumb forces _PlainLineLog: one "[godel-watch] ... step_path='step_N'"
    # line per event on stdout.
    env = {**os.environ, "TERM": "dumb"}

    result = subprocess.run(
        [PYTHON, "-m", "godel", "watch", run_id, "--runs-dir", str(runs_dir)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
        timeout=20,
    )
    assert result.returncode == 0, (
        f"`godel watch {run_id}` failed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Parse the plain-log output: lines for step.enter events embed
    # step_path='step_N'.  Match them all and collect the indices.
    import re
    step_path_re = re.compile(r"step_path='step_(\d+)'")
    seen_indices: set[int] = set()
    for line in result.stdout.splitlines():
        m = step_path_re.search(line)
        if m:
            seen_indices.add(int(m.group(1)))

    # Every unique step_i (0..n_events-1) must appear in stdout.  A bug that
    # drops any archive file would leave a contiguous range of indices missing.
    expected = set(range(n_events))
    missing = expected - seen_indices
    extra = seen_indices - expected
    assert not missing, (
        f"`godel watch` missed {len(missing)} events (archive replay broken?). "
        f"First 10 missing step indices: {sorted(missing)[:10]}.  "
        f"Rotations in transcript: {rotations}.  "
        f"stdout line count: {len(result.stdout.splitlines())}.  "
        f"stderr tail: {result.stderr[-400:]!r}"
    )
    assert not extra, f"Unexpected step indices in CLI stdout: {sorted(extra)[:10]}"

    # Oldest-archive guard: step_0 lives in the oldest archive.  This is
    # redundant with the set-equality check above, but makes the failure mode
    # ("archive sort order reversed") immediately obvious in the assertion
    # message if it ever regresses.
    assert 0 in seen_indices, (
        "step_0 (oldest archive's first event) missing from `godel watch` "
        "output — archive-replay ordering is likely broken."
    )


# ---------------------------------------------------------------------------
# (d) Replay-with-watch renders identical model to live
# ---------------------------------------------------------------------------


def test_d_replay_with_watch_identical_to_live(tmp_path):
    """Replay a rotation-heavy finished transcript through WatchModel == live.

    Uses enough events (and a small max_bytes) to force many rotations, so
    the multi-file replay path is actually stressed.  Verifies both steps,
    panels, AND run_meta are equal between live and replay models.
    """
    run_id = "run_d"
    runs_dir = tmp_path
    run_dir = runs_dir / run_id

    # Force many rotations: small max_bytes + a realistic mix of events.
    max_bytes = 512
    n_steps = 8
    stdout_per_step = 30  # total ≈ 16 step events + 240 stdout = 256 events

    with TranscriptWriter(run_dir, run_id=run_id, max_bytes=max_bytes) as tw:
        for step_idx in range(n_steps):
            step_name = f"step_{step_idx}"
            tw.write_event(
                "step.enter",
                step_path=[step_name],
                stream_path=[],
            )
            for j in range(stdout_per_step):
                tw.write_event(
                    "stdout",
                    step_path=[step_name],
                    stream_path=[step_name, "stdout"],
                    line=f"line {j} from {step_name}",
                )
            tw.write_event(
                "step.exit",
                step_path=[step_name],
                stream_path=[],
                status="done",
            )

    # Sanity: ensure the multi-file replay path is actually exercised.
    rotations = _count_rotations(run_dir)
    assert rotations >= 3, (
        f"Test is meant to stress multi-file replay — expected >= 3 rotations, "
        f"got {rotations}.  Lower max_bytes or add more events."
    )

    # --- Build "live" model via TranscriptTail.from_run (follow=False) ---
    # NOTE: TranscriptTail yields event dicts only (header lines are silently
    # skipped by design).  To match the on-disk replay below we inject header
    # reduction explicitly: one reduce_header call per file in write order
    # (oldest archive → current), mirroring the stream a live tail would
    # logically encounter.  Each rotation produces a fresh header with its own
    # ``started_at`` timestamp, so run_meta reflects the most recent file's
    # header after the merge — matching replay.
    live_model = WatchModel.empty()
    for path in _ordered_transcript_files(run_dir):
        with open(path, encoding="utf-8") as fh:
            first = fh.readline().strip()
            if not first:
                continue
            first_obj = json.loads(first)
            if "header" in first_obj:
                live_model = reduce_header(live_model, first_obj["header"])

    live_tail = TranscriptTail.from_run(run_id, runs_dir=runs_dir, follow=False)
    for evt in live_tail:
        live_model = reduce(live_model, evt)

    # --- Build replay model via the on-disk helper (handles headers too) ---
    replay_model = _replay_transcript_to_model(run_dir)

    # Steps, panels, AND run_meta must match.
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
    assert dict(live_model.run_meta) == dict(replay_model.run_meta), (
        f"run_meta mismatch.\n"
        f"Live run_meta:   {dict(live_model.run_meta)}\n"
        f"Replay run_meta: {dict(replay_model.run_meta)}"
    )

    # Sanity: the model observed every step and every panel.
    for step_idx in range(n_steps):
        step_name = f"step_{step_idx}"
        assert (step_name,) in live_model.steps, (
            f"Expected step {step_name!r} in live model"
        )
        assert live_model.steps[(step_name,)].status == "done"
        assert (step_name, "stdout") in live_model.panels, (
            f"Expected panel for {step_name!r} in live model"
        )
