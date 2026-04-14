"""Tests for TranscriptTail — rotation-chain-aware tail reader (godel-py-5pl.2).

Acceptance criteria:
  AC-1  Rotation-during-read: force rotation mid-tail; every event appears
        exactly once in read order; no duplicates, no gaps.
  AC-2  Late-attach: start run, write 1000 events across >=3 rotations, attach
        tail; reader emits all 1000 in order then catches up to live.
  AC-3  Missing current file: reader raises TranscriptTailError within one poll
        interval; does not hang.
  AC-4  Inode re-use: reader recovers rather than silently reading wrong file.

Additional:
  - Header lines are skipped; only event dicts are yielded.
  - Sentinel events (op=="rotate") are yielded so callers can observe them.
  - follow=False stops at first EOF.
  - from_run() discovers archives correctly.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from godel._tail import TranscriptTail, TranscriptTailError
from godel._transcript import TranscriptWriter, _FILENAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_n_events(tw: TranscriptWriter, n: int, op: str = "tick") -> list[int]:
    """Write *n* events and return their seq numbers."""
    return [tw.write_event(op, idx=i) for i in range(n)]


def _collect_events(
    tail: TranscriptTail,
    stop_after: int | None = None,
    timeout: float = 10.0,
) -> list[dict]:
    """Collect events from *tail* into a list.

    Runs in the current thread.  If *stop_after* is set, stops after that
    many events (useful when the writer is still live and follow=True).
    Times out after *timeout* seconds.
    """
    results: list[dict] = []
    deadline = time.monotonic() + timeout
    for evt in tail:
        results.append(evt)
        if stop_after is not None and len(results) >= stop_after:
            break
        if time.monotonic() > deadline:
            pytest.fail(f"Timed out after {timeout}s; collected {len(results)} events")
    return results


def _collect_in_thread(
    tail: TranscriptTail,
    stop_after: int | None = None,
    timeout: float = 10.0,
) -> tuple[list[dict], list[Exception]]:
    """Run _collect_events in a background thread.

    Returns (results, errors).  Call .join() on the thread yourself or use
    the returned container once the thread finishes.
    """
    results: list[dict] = []
    errors: list[Exception] = []

    def _worker():
        try:
            deadline = time.monotonic() + timeout
            for evt in tail:
                results.append(evt)
                if stop_after is not None and len(results) >= stop_after:
                    break
                if time.monotonic() > deadline:
                    errors.append(TimeoutError(f"Timed out; got {len(results)} events"))
                    break
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return results, errors, t  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Basic smoke tests
# ---------------------------------------------------------------------------


def test_reads_single_file_no_rotation(tmp_path):
    """follow=False: reads all events from a completed single-file transcript."""
    run_dir = tmp_path / "run0"
    with TranscriptWriter(run_dir, run_id="r0", max_bytes=10_000_000) as tw:
        seqs = _write_n_events(tw, 10)

    tail = TranscriptTail(run_dir, follow=False)
    evts = _collect_events(tail)
    real = [e for e in evts if e.get("op") != "rotate"]
    assert [e["seq"] for e in real] == seqs


def test_header_lines_are_skipped(tmp_path):
    """Header dicts (key 'header') are never yielded."""
    run_dir = tmp_path / "run_hdr"
    with TranscriptWriter(run_dir, run_id="rhdr") as tw:
        tw.write_event("noop")

    tail = TranscriptTail(run_dir, follow=False)
    evts = _collect_events(tail)
    for e in evts:
        assert "header" not in e, f"Header leaked into events: {e}"


def test_follow_false_stops_at_eof(tmp_path):
    """follow=False exits at first EOF without hanging."""
    run_dir = tmp_path / "run_nofollow"
    with TranscriptWriter(run_dir, run_id="rnf") as tw:
        _write_n_events(tw, 5)

    start = time.monotonic()
    tail = TranscriptTail(run_dir, follow=False, poll_interval=0.05)
    evts = _collect_events(tail)
    elapsed = time.monotonic() - start
    # Should finish well under 2 seconds (no indefinite poll)
    assert elapsed < 2.0
    real = [e for e in evts if e.get("op") != "rotate"]
    assert len(real) == 5


# ---------------------------------------------------------------------------
# AC-1: Rotation-during-read — no duplicates, no gaps
# ---------------------------------------------------------------------------


def test_ac1_rotation_during_read_no_gaps_no_dups(tmp_path):
    """AC-1: Force rotation mid-tail; every event appears exactly once."""
    run_dir = tmp_path / "run_ac1"
    n_events = 60
    # Small max_bytes to force multiple rotations
    poll = 0.02

    # We write in a background thread while the tail reads live.
    results: list[dict] = []
    errors: list[Exception] = []
    tail = TranscriptTail(run_dir, poll_interval=poll, follow=True)

    def _reader():
        try:
            deadline = time.monotonic() + 15.0
            for evt in tail:
                if evt.get("op") != "rotate":
                    results.append(evt)
                if len(results) >= n_events:
                    break
                if time.monotonic() > deadline:
                    errors.append(TimeoutError(f"Reader timed out at {len(results)} events"))
                    break
        except Exception as exc:
            errors.append(exc)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Give the reader a head start, then start writing with rotations.
    time.sleep(0.05)
    with TranscriptWriter(run_dir, run_id="rac1", max_bytes=400) as tw:
        for i in range(n_events):
            tw.write_event("tick", idx=i)
            time.sleep(0.001)  # tiny pause so reader interleaves

    reader_thread.join(timeout=15.0)
    assert not errors, f"Reader errors: {errors}"
    assert len(results) == n_events, (
        f"Expected {n_events} events, got {len(results)}"
    )

    seqs = [e["seq"] for e in results]
    assert seqs == sorted(seqs), "Events not in order"
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), (
        "Gaps or duplicates in seq sequence"
    )
    assert len(set(seqs)) == len(seqs), "Duplicate seqs"


# ---------------------------------------------------------------------------
# AC-2: Late-attach — replay 1000 events across >=3 rotations, then live
# ---------------------------------------------------------------------------


def test_ac2_late_attach_1000_events_3_rotations(tmp_path):
    """AC-2: Late-attach; reader emits all events in order then catches up live.

    We use a single TranscriptWriter that stays open for the entire run so the
    seq counter remains contiguous.  The writer pauses mid-run to let us
    attach the reader, then continues writing live events.
    """
    # run_id must be the directory name under runs_dir for from_run() to find it.
    run_id = "rac2"
    runs_dir = tmp_path
    run_dir = runs_dir / run_id  # transcript files live here
    n_archived = 980   # events written before reader attaches
    n_live = 20        # events written while reader is live
    n_total = n_archived + n_live

    writer_pause = threading.Event()
    writer_resumed = threading.Event()

    # Write n_archived events in a background thread, then wait for the test
    # to attach the reader, then write n_live more events.
    writer_errors: list[Exception] = []

    def _writer():
        try:
            with TranscriptWriter(run_dir, run_id=run_id, max_bytes=5000) as tw:
                for i in range(n_archived):
                    tw.write_event("batch", idx=i)
                writer_pause.set()   # signal: archived events done
                writer_resumed.wait(timeout=10.0)  # wait for reader to attach
                for i in range(n_archived, n_total):
                    tw.write_event("live", idx=i)
                    time.sleep(0.001)
        except Exception as exc:
            writer_errors.append(exc)
            writer_pause.set()

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    # Wait for archived events to be written.
    writer_pause.wait(timeout=15.0)
    assert not writer_errors, f"Writer errors: {writer_errors}"

    # Verify rotations happened.
    rotations = sum(
        1 for i in range(1, 100)
        if (run_dir / f"{_FILENAME}.{i}").exists()
    )
    assert rotations >= 3, f"Expected >= 3 rotations, got {rotations}"

    # Attach late reader BEFORE signalling writer to resume.
    tail = TranscriptTail.from_run(run_id, runs_dir=runs_dir, poll_interval=0.02, follow=True)
    results: list[dict] = []
    reader_errors: list[Exception] = []
    done_event = threading.Event()

    def _reader():
        try:
            for evt in tail:
                if evt.get("op") != "rotate":
                    results.append(evt)
                if len(results) >= n_total:
                    done_event.set()
                    break
        except Exception as exc:
            reader_errors.append(exc)
            done_event.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Give reader a moment to start, then signal writer to write live events.
    time.sleep(0.1)
    writer_resumed.set()

    done_event.wait(timeout=20.0)
    writer_thread.join(timeout=5.0)
    reader_thread.join(timeout=2.0)

    assert not writer_errors, f"Writer errors: {writer_errors}"
    assert not reader_errors, f"Reader errors: {reader_errors}"
    assert len(results) == n_total, (
        f"Expected {n_total} events, got {len(results)}"
    )

    seqs = [e["seq"] for e in results]
    assert seqs == sorted(seqs), "Events not in ascending seq order"
    # Seqs must be contiguous (no gaps)
    assert len(set(seqs)) == len(seqs), "Duplicate seqs"
    for i in range(1, len(seqs)):
        assert seqs[i] == seqs[i - 1] + 1, (
            f"Gap at index {i}: {seqs[i-1]} → {seqs[i]}"
        )


# ---------------------------------------------------------------------------
# AC-3: Missing current file (writer crash) — raises TranscriptTailError, does not hang
# ---------------------------------------------------------------------------


def test_ac3_missing_current_file_follow_false_raises_immediately(tmp_path):
    """AC-3 (follow=False): raises immediately if transcript.jsonl absent."""
    run_dir = tmp_path / "run_ac3"
    run_dir.mkdir(parents=True, exist_ok=True)
    # No transcript.jsonl created.

    tail = TranscriptTail(run_dir, poll_interval=0.5, follow=False)
    start = time.monotonic()
    with pytest.raises(TranscriptTailError):
        list(tail)
    elapsed = time.monotonic() - start
    # Should be essentially instant (no sleep before raising in follow=False)
    assert elapsed < 0.3, f"Expected immediate raise, took {elapsed:.2f}s"


def test_ac3_file_disappears_during_read_raises(tmp_path):
    """AC-3: If transcript.jsonl disappears mid-read, raises TranscriptTailError."""
    run_dir = tmp_path / "run_ac3b"
    run_dir.mkdir(parents=True, exist_ok=True)

    poll = 0.05

    # Write some events so the reader can open the file.
    with TranscriptWriter(run_dir, run_id="rac3b", max_bytes=10_000_000) as tw:
        for i in range(5):
            tw.write_event("ev", idx=i)

    tail = TranscriptTail(run_dir, poll_interval=poll, follow=True)
    errors: list[Exception] = []

    def _reader():
        try:
            for _ in tail:
                pass
        except TranscriptTailError as exc:
            errors.append(exc)
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    # Let reader start and consume events, then delete the file.
    time.sleep(0.2)
    current = run_dir / _FILENAME
    current.unlink()

    t.join(timeout=5.0)
    assert errors, "Expected TranscriptTailError when file disappears"
    assert isinstance(errors[0], TranscriptTailError), (
        f"Expected TranscriptTailError, got {type(errors[0])}: {errors[0]}"
    )


def test_ac3_error_carries_path(tmp_path):
    """AC-3: TranscriptTailError.path points to the missing file."""
    run_dir = tmp_path / "run_ac3c"
    run_dir.mkdir(parents=True, exist_ok=True)

    tail = TranscriptTail(run_dir, poll_interval=0.01, follow=False)
    with pytest.raises(TranscriptTailError) as exc_info:
        list(tail)
    assert exc_info.value.path is not None
    assert exc_info.value.path.name == _FILENAME


# ---------------------------------------------------------------------------
# AC-4: Inode re-use — reader recovers without silently reading wrong file
# ---------------------------------------------------------------------------


def test_ac4_inode_reuse_recovery(tmp_path, caplog):
    """AC-4: If inode changes without sentinel, reader reopens and continues."""
    import logging

    run_dir = tmp_path / "run_ac4"
    run_dir.mkdir(parents=True, exist_ok=True)
    current = run_dir / _FILENAME

    # Write first "generation" of file.
    with TranscriptWriter(run_dir, run_id="rac4a", max_bytes=10_000_000) as tw:
        for i in range(5):
            tw.write_event("gen1", idx=i)

    # Simulate inode change: delete + recreate (most filesystems reuse inodes
    # eventually, but for the test we just replace the file).
    current_ino_before = current.stat().st_ino

    # Read first generation with the tail open, then swap file under it.
    results: list[dict] = []
    errors: list[Exception] = []
    swap_done = threading.Event()

    tail = TranscriptTail(run_dir, poll_interval=0.05, follow=True)

    def _reader():
        try:
            deadline = time.monotonic() + 10.0
            for evt in tail:
                if evt.get("op") != "rotate":
                    results.append(evt)
                if swap_done.is_set() and len(results) >= 10:
                    break
                if time.monotonic() > deadline:
                    errors.append(TimeoutError("Timed out"))
                    break
        except Exception as exc:
            errors.append(exc)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Wait for reader to consume gen1 events.
    time.sleep(0.3)

    # Atomically replace the file (simulates inode reuse without sentinel).
    # On Linux, this changes the inode of the path.
    current.unlink()
    with TranscriptWriter(run_dir, run_id="rac4b", max_bytes=10_000_000) as tw2:
        for i in range(5):
            tw2.write_event("gen2", idx=i)

    swap_done.set()
    reader_thread.join(timeout=10.0)

    assert not errors, f"Reader errors: {errors}"
    # Reader should have picked up gen2 events via inode-change recovery.
    gen2_events = [e for e in results if e.get("op") == "gen2"]
    assert len(gen2_events) == 5, (
        f"Expected 5 gen2 events after inode reuse, got {len(gen2_events)}: "
        f"{results}"
    )


# ---------------------------------------------------------------------------
# Sentinel events are yielded (callers can observe rotation points)
# ---------------------------------------------------------------------------


def test_rotate_sentinel_yielded(tmp_path):
    """Rotate sentinel events (op=='rotate') are yielded by the iterator.

    Sentinels live in the rotated-out archive files (.1, .2, ...).
    Use from_run() to replay archives so sentinels are included.
    """
    run_id = "run_sentinel"
    run_dir = tmp_path / run_id
    with TranscriptWriter(run_dir, run_id=run_id, max_bytes=300) as tw:
        for i in range(20):
            tw.write_event("ev", i=i)

    assert (run_dir / f"{_FILENAME}.1").exists(), "Rotation did not happen"

    tail = TranscriptTail.from_run(run_id, runs_dir=tmp_path, follow=False)
    evts = _collect_events(tail)
    sentinels = [e for e in evts if e.get("op") == "rotate"]
    assert sentinels, "Expected at least one rotate sentinel event from archives"


# ---------------------------------------------------------------------------
# from_run() discovers archives in correct order
# ---------------------------------------------------------------------------


def test_from_run_discovers_archives(tmp_path):
    """from_run() replays archives in write order before attaching live."""
    run_id = "run_from"
    run_dir = tmp_path / run_id
    n_events = 60

    with TranscriptWriter(run_dir, run_id=run_id, max_bytes=400) as tw:
        for i in range(n_events):
            tw.write_event("archival", idx=i)

    rotations = sum(
        1 for i in range(1, 100)
        if (run_dir / f"{_FILENAME}.{i}").exists()
    )
    assert rotations >= 2, f"Need >= 2 rotations for this test, got {rotations}"

    tail = TranscriptTail.from_run(run_id, runs_dir=tmp_path, follow=False)
    evts = _collect_events(tail)
    real = [e for e in evts if e.get("op") != "rotate"]
    seqs = [e["seq"] for e in real]

    assert len(real) == n_events
    assert seqs == sorted(seqs), "Events from from_run() not in order"
    for i in range(1, len(seqs)):
        assert seqs[i] == seqs[i - 1] + 1, (
            f"Gap after from_run at index {i}: {seqs[i-1]} → {seqs[i]}"
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_transcript_file_follow_false(tmp_path):
    """An empty transcript.jsonl (header only) yields zero events."""
    run_dir = tmp_path / "run_empty"
    with TranscriptWriter(run_dir, run_id="rempty") as _tw:
        pass  # close without writing any events

    tail = TranscriptTail(run_dir, follow=False)
    evts = _collect_events(tail)
    real = [e for e in evts if e.get("op") != "rotate"]
    assert real == []


def test_single_rotation_complete_chain(tmp_path):
    """Single rotation: all events from .1 then current are readable."""
    run_dir = tmp_path / "run_single_rot"
    with TranscriptWriter(run_dir, run_id="rsr", max_bytes=300) as tw:
        for i in range(10):
            tw.write_event("ev", i=i)

    assert (run_dir / f"{_FILENAME}.1").exists()

    tail = TranscriptTail.from_run("run_single_rot", runs_dir=tmp_path, follow=False)
    evts = _collect_events(tail)
    real = [e for e in evts if e.get("op") != "rotate"]
    seqs = [e["seq"] for e in real]
    assert len(real) == 10
    assert seqs == list(range(1, 11))
