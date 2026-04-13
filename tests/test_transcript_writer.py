"""Tests for godel/_transcript.py — TranscriptWriter acceptance criteria.

All acceptance criteria from godel-py-5pl.1 are covered here.  Additional
tests cover reviewer-identified defects fixed in the same pass.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from godel._transcript import (
    TRANSCRIPT_FORMAT_VERSION,
    TranscriptVersionError,
    TranscriptWriter,
    _FILENAME,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    """Read all non-empty lines from a JSONL file and return parsed dicts."""
    lines = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
    return lines


def _collect_all_files_in_order(run_dir: Path) -> list[Path]:
    """Return files in oldest-first order (highest suffix → lowest → current).

    Concatenating them in this order reproduces the original write order.
    """
    suffixed: list[Path] = []
    i = 1
    while True:
        p = run_dir / f"{_FILENAME}.{i}"
        if p.exists():
            suffixed.append(p)
            i += 1
        else:
            break
    # suffixed[0] == .1 (most-recent rotate-out), suffixed[-1] == .N (oldest)
    result: list[Path] = list(reversed(suffixed))
    current = run_dir / _FILENAME
    if current.exists():
        result.append(current)
    return result


def _all_lines_in_order(run_dir: Path) -> list[dict]:
    """All parsed JSONL lines across all transcript files, chronological order."""
    all_lines: list[dict] = []
    for path in _collect_all_files_in_order(run_dir):
        all_lines.extend(_read_jsonl(path))
    return all_lines


def _real_event_seqs(run_dir: Path) -> list[int]:
    """Seqs of all non-sentinel, non-header lines, in write order."""
    return [
        line["event"]["seq"]
        for line in _all_lines_in_order(run_dir)
        if "event" in line and line["event"].get("op") != "rotate"
    ]


# ---------------------------------------------------------------------------
# 1. Header shape-distinctness
# ---------------------------------------------------------------------------


def test_header_is_first_line_and_shape_distinct(tmp_path):
    run_dir = tmp_path / "run1"
    with TranscriptWriter(run_dir, run_id="r1") as tw:
        tw.write_event("step_start", step_path=["fetch"])

    lines = _read_jsonl(run_dir / _FILENAME)
    assert len(lines) >= 2
    # Line 1 must have "header" key, NOT "event"
    assert "header" in lines[0], f"First line lacks 'header' key: {lines[0]}"
    assert "event" not in lines[0]
    # Line 2 must have "event" key, NOT "header"
    assert "event" in lines[1]
    assert "header" not in lines[1]


def test_header_fields(tmp_path):
    run_dir = tmp_path / "run2"
    with TranscriptWriter(run_dir, run_id="my-run") as tw:
        tw.write_event("noop")

    lines = _read_jsonl(run_dir / _FILENAME)
    hdr = lines[0]["header"]
    assert hdr["v"] == TRANSCRIPT_FORMAT_VERSION
    assert hdr["run_id"] == "my-run"
    assert "started_at" in hdr


# ---------------------------------------------------------------------------
# 2. seq strictly monotonic starting at 1, across N rotations (N>=5 required)
# ---------------------------------------------------------------------------


def test_seq_monotonic_across_5_rotations(tmp_path):
    run_dir = tmp_path / "runA"
    # Small max_bytes to force many rotations.
    # Each event line is ~120 bytes; header ~80 bytes; 300 bytes → ~1-2 events/file.
    tw = TranscriptWriter(run_dir, run_id="rA", max_bytes=300)
    n_events = 50
    returned_seqs: list[int] = []
    for i in range(n_events):
        returned_seqs.append(tw.write_event("tick", extra_i=i))
    tw.close()

    assert returned_seqs == list(range(1, n_events + 1)), (
        "write_event() return values are not contiguous 1..N"
    )

    disk_seqs = _real_event_seqs(run_dir)
    assert disk_seqs[0] == 1, f"First on-disk seq should be 1, got {disk_seqs[0]}"
    for i in range(1, len(disk_seqs)):
        assert disk_seqs[i] == disk_seqs[i - 1] + 1, (
            f"seq gap at index {i}: {disk_seqs[i - 1]} → {disk_seqs[i]}"
        )
    assert len(disk_seqs) == n_events

    rotations = sum(
        1
        for line in _all_lines_in_order(run_dir)
        if "event" in line and line["event"].get("op") == "rotate"
    )
    assert rotations >= 5, f"Expected >= 5 rotations, got {rotations}"


# ---------------------------------------------------------------------------
# 3. Rotation sentinel is verifiably the LAST line of the outgoing .1 file
# ---------------------------------------------------------------------------


def test_sentinel_is_last_line_of_rotated_file(tmp_path):
    run_dir = tmp_path / "runB"
    tw = TranscriptWriter(run_dir, run_id="rB", max_bytes=300)
    for i in range(20):
        tw.write_event("step", x=i)
    tw.close()

    rotated = run_dir / f"{_FILENAME}.1"
    assert rotated.exists(), ".1 file should exist after rotation"

    lines = _read_jsonl(rotated)
    last = lines[-1]
    assert "event" in last, f"Last line of rotated file is not an event: {last}"
    assert last["event"]["op"] == "rotate", (
        f"Last line op should be 'rotate', got {last['event']['op']!r}"
    )


# ---------------------------------------------------------------------------
# C1 FIX: sentinel.last_seq == seq of the last real event in that file
# (not self._seq which is the NEXT unwritten event's seq)
# ---------------------------------------------------------------------------


def test_sentinel_last_seq_matches_last_real_event(tmp_path):
    """sentinel.last_seq must equal the seq of the last written real event."""
    run_dir = tmp_path / "runC1"
    tw = TranscriptWriter(run_dir, run_id="rC1", max_bytes=300)
    for i in range(20):
        tw.write_event("ev", i=i)
    tw.close()

    rotated = run_dir / f"{_FILENAME}.1"
    lines = _read_jsonl(rotated)

    real_events_in_rotated = [
        ln["event"] for ln in lines if "event" in ln and ln["event"]["op"] != "rotate"
    ]
    assert real_events_in_rotated, "Expected real events in rotated file"
    last_real_seq = real_events_in_rotated[-1]["seq"]

    sentinel = lines[-1]["event"]
    assert sentinel["last_seq"] == last_real_seq, (
        f"sentinel.last_seq={sentinel['last_seq']} != last real event seq={last_real_seq}"
    )


def test_sentinel_has_no_seq_field(tmp_path):
    """Sentinel events must carry NO 'seq' field (reader contract: godel-py-vaz fix).

    Before the fix, sentinel.seq == self._seq which had already been
    pre-incremented for the triggering event, causing it to collide with the
    first real event written into the next file.  The fix drops 'seq' from
    sentinels entirely; readers must use 'last_seq' for the file boundary.
    """
    run_dir = tmp_path / "runC1b"
    tw = TranscriptWriter(run_dir, run_id="rC1b", max_bytes=300)
    for i in range(20):
        tw.write_event("ev", i=i)
    tw.close()

    all_lines = _all_lines_in_order(run_dir)
    sentinels = [
        ln["event"]
        for ln in all_lines
        if "event" in ln and ln["event"].get("op") == "rotate"
    ]
    assert sentinels, "Expected at least one sentinel (rotation) event"
    for s in sentinels:
        assert "seq" not in s, (
            f"Sentinel must NOT carry a 'seq' field, but got: {s!r}"
        )


def test_sentinel_seq_not_shared_with_next_file_first_event(tmp_path):
    """The first real event in each new file must not collide with the preceding sentinel.

    This is the precise collision described in godel-py-vaz: before the fix,
    sentinel.seq == write_event's pre-incremented self._seq, which was then
    also assigned to the event in the new file.  We verify the invariant by
    asserting that every real event's seq is unique across the entire run.
    """
    run_dir = tmp_path / "runC1c"
    tw = TranscriptWriter(run_dir, run_id="rC1c", max_bytes=300)
    for i in range(20):
        tw.write_event("ev", i=i)
    tw.close()

    files_in_order = _collect_all_files_in_order(run_dir)
    # For each pair of adjacent files: verify the sentinel's last_seq + 1 equals
    # the first real event's seq in the next file.  This confirms no gap or overlap.
    for idx in range(len(files_in_order) - 1):
        outgoing_lines = _read_jsonl(files_in_order[idx])
        incoming_lines = _read_jsonl(files_in_order[idx + 1])

        sentinel = outgoing_lines[-1]["event"]
        assert sentinel["op"] == "rotate", (
            f"Expected last line of {files_in_order[idx]} to be a sentinel"
        )
        assert "seq" not in sentinel, (
            f"Sentinel in {files_in_order[idx]} must not have 'seq'; got {sentinel!r}"
        )

        # First real event in the next file (skip header)
        first_real = next(
            ln["event"]
            for ln in incoming_lines
            if "event" in ln and ln["event"].get("op") != "rotate"
        )
        expected_first_seq = sentinel["last_seq"] + 1
        assert first_real["seq"] == expected_first_seq, (
            f"First event in {files_in_order[idx + 1]} has seq={first_real['seq']}, "
            f"expected {expected_first_seq} (sentinel.last_seq={sentinel['last_seq']})"
        )


# ---------------------------------------------------------------------------
# vaz review W2: do NOT rotate a file that has no real events yet.
# Prevents sentinel with last_seq=0 (misleading file boundary).
# ---------------------------------------------------------------------------


def test_no_rotation_on_header_only_file(tmp_path):
    """Pathologically small max_bytes must not produce a header-only rotated file.

    If max_bytes is smaller than even the header+first event, the writer must
    still produce a file containing at least one real event before rotating.
    No sentinel with last_seq=0 should ever appear on disk.
    """
    run_dir = tmp_path / "runW2vaz"
    # max_bytes=10 is smaller than any real encoded event line.
    tw = TranscriptWriter(run_dir, run_id="rW2vaz", max_bytes=10)
    for i in range(5):
        tw.write_event("ev", i=i)
    tw.close()

    all_lines = _all_lines_in_order(run_dir)
    sentinels = [
        ln["event"]
        for ln in all_lines
        if "event" in ln and ln["event"].get("op") == "rotate"
    ]
    for s in sentinels:
        assert s["last_seq"] >= 1, (
            f"Sentinel with last_seq={s['last_seq']} found — header-only rotation leaked: {s!r}"
        )

    # Every rotated file (i.e. all but possibly the final active file if empty)
    # must contain at least one real event.
    for path in _collect_all_files_in_order(run_dir):
        lines = _read_jsonl(path)
        real_events = [
            ln for ln in lines
            if "event" in ln and ln["event"].get("op") != "rotate"
        ]
        has_sentinel = any(
            "event" in ln and ln["event"].get("op") == "rotate" for ln in lines
        )
        if has_sentinel:
            assert real_events, (
                f"{path} rotated out with no real events (header-only file leaked)"
            )


# ---------------------------------------------------------------------------
# vaz review W3: headers must not carry a 'seq' field either (contract parity).
# ---------------------------------------------------------------------------


def test_header_has_no_seq_field(tmp_path):
    """Headers must never carry a 'seq' field; reader contract parity with sentinels."""
    run_dir = tmp_path / "runHdrSeq"
    tw = TranscriptWriter(run_dir, run_id="rHdrSeq", max_bytes=300)
    for i in range(20):
        tw.write_event("ev", i=i)
    tw.close()

    for path in _collect_all_files_in_order(run_dir):
        lines = _read_jsonl(path)
        assert "header" in lines[0]
        assert "seq" not in lines[0]["header"], (
            f"Header in {path} must not carry 'seq': {lines[0]['header']!r}"
        )


# ---------------------------------------------------------------------------
# W3 FIX: sentinel.prev points to the correct next-older file
# ---------------------------------------------------------------------------


def test_sentinel_prev_after_first_rotation(tmp_path):
    """First rotation: no older file → prev=None."""
    run_dir = tmp_path / "runW3a"
    tw = TranscriptWriter(run_dir, run_id="rW3a", max_bytes=300)
    for i in range(10):
        tw.write_event("ev", i=i)
    tw.close()

    rotated_1 = run_dir / f"{_FILENAME}.1"
    assert rotated_1.exists()
    lines = _read_jsonl(rotated_1)
    # Find the first sentinel
    sentinels = [ln for ln in lines if "event" in ln and ln["event"]["op"] == "rotate"]
    assert sentinels

    # Verify: if this is the very first rotation there was no .1 before,
    # so prev should be None.  If more rotations happened, .1's sentinel should
    # point to .2.
    # We only assert for the earliest sentinel (in the oldest file).
    files_in_order = _collect_all_files_in_order(run_dir)
    oldest = files_in_order[0]
    oldest_lines = _read_jsonl(oldest)
    oldest_sentinel = next(
        ln["event"] for ln in oldest_lines if "event" in ln and ln["event"]["op"] == "rotate"
    )
    assert oldest_sentinel["prev"] is None, (
        f"Oldest file's sentinel.prev should be None, got {oldest_sentinel['prev']!r}"
    )


def test_sentinel_prev_after_second_rotation(tmp_path):
    """Second rotation: outgoing current→.1 has older neighbour .1→.2.
    The sentinel in the new .1 must point to .2.
    """
    run_dir = tmp_path / "runW3b"
    tw = TranscriptWriter(run_dir, run_id="rW3b", max_bytes=300)
    for i in range(40):
        tw.write_event("ev", i=i)
    tw.close()

    assert (run_dir / f"{_FILENAME}.2").exists(), "Expected at least 2 rotations"

    # .1 is the most recently rotated-out file; it should point to .2
    lines_1 = _read_jsonl(run_dir / f"{_FILENAME}.1")
    sentinel_1 = next(
        ln["event"] for ln in reversed(lines_1) if "event" in ln and ln["event"]["op"] == "rotate"
    )
    assert sentinel_1["prev"] == f"{_FILENAME}.2", (
        f".1 sentinel should point to .2, got {sentinel_1['prev']!r}"
    )


# ---------------------------------------------------------------------------
# 4. Concatenation of all files in order equals full write order
# ---------------------------------------------------------------------------


def test_full_concat_matches_write_order(tmp_path):
    run_dir = tmp_path / "runD"
    tw = TranscriptWriter(run_dir, run_id="rD", max_bytes=300)
    written_seqs: list[int] = []
    for i in range(40):
        written_seqs.append(tw.write_event("ev", i=i))
    tw.close()

    assert _real_event_seqs(run_dir) == written_seqs


# ---------------------------------------------------------------------------
# 5. Version check: refuses v>1 with clear error; accepts v=1 and missing v
# ---------------------------------------------------------------------------


def test_version_check_rejects_unknown_major():
    with pytest.raises(TranscriptVersionError, match="not supported"):
        TranscriptWriter.check_version({"v": TRANSCRIPT_FORMAT_VERSION + 1})


def test_version_check_error_message_is_actionable():
    with pytest.raises(TranscriptVersionError, match="Upgrade godel"):
        TranscriptWriter.check_version({"v": TRANSCRIPT_FORMAT_VERSION + 1})


def test_version_check_accepts_current_major():
    # Must not raise
    TranscriptWriter.check_version({"v": TRANSCRIPT_FORMAT_VERSION})


def test_version_check_accepts_missing_v():
    # Missing v treated as version 1 — must not raise
    TranscriptWriter.check_version({})


# ---------------------------------------------------------------------------
# 6. Concurrent writers: 8 threads, no corruption, no lost/duplicate seqs
# ---------------------------------------------------------------------------


def test_concurrent_8_threads(tmp_path):
    run_dir = tmp_path / "runE"
    # Small max_bytes to exercise concurrent rotation paths
    tw = TranscriptWriter(run_dir, run_id="rE", max_bytes=1024)

    n_threads = 8
    events_per_thread = 50
    results: list[int] = []
    errors: list[Exception] = []
    result_lock = threading.Lock()

    def worker():
        try:
            for _ in range(events_per_thread):
                seq = tw.write_event("thread_event")
                with result_lock:
                    results.append(seq)
        except Exception as exc:
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tw.close()

    assert not errors, f"Worker errors: {errors}"

    total = n_threads * events_per_thread
    assert len(results) == total

    # All returned seqs must be unique and cover [1, total]
    assert sorted(results) == list(range(1, total + 1)), (
        "Returned seqs are not a contiguous range 1..N — duplicates or gaps"
    )

    # On-disk real events must also be a contiguous range
    disk_seqs = _real_event_seqs(run_dir)
    assert sorted(disk_seqs) == list(range(1, total + 1)), (
        "On-disk seqs don't match expected range"
    )

    # All lines must be valid JSON (no corruption from concurrent writes)
    for path in _collect_all_files_in_order(run_dir):
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                raw = raw.strip()
                if raw:
                    try:
                        json.loads(raw)
                    except json.JSONDecodeError as exc:
                        pytest.fail(f"Corrupt JSON in {path} line {lineno}: {exc}")


# ---------------------------------------------------------------------------
# 7. GODEL_TRANSCRIPT_MAX_BYTES env var forces rotation at small threshold
# ---------------------------------------------------------------------------


def test_env_var_forces_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("GODEL_TRANSCRIPT_MAX_BYTES", "512")
    run_dir = tmp_path / "runF"
    tw = TranscriptWriter(run_dir, run_id="rF")
    assert tw._max_bytes == 512

    for i in range(30):
        tw.write_event("ev", payload="x" * 20, idx=i)
    tw.close()

    assert (run_dir / f"{_FILENAME}.1").exists(), (
        "Expected at least one rotated file with GODEL_TRANSCRIPT_MAX_BYTES=512"
    )


def test_explicit_max_bytes_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GODEL_TRANSCRIPT_MAX_BYTES", "9999999")
    run_dir = tmp_path / "runG"
    tw = TranscriptWriter(run_dir, run_id="rG", max_bytes=1234)
    assert tw._max_bytes == 1234
    tw.close()


# ---------------------------------------------------------------------------
# 8. Each rotated file starts with a header
# ---------------------------------------------------------------------------


def test_each_file_starts_with_header(tmp_path):
    run_dir = tmp_path / "runH"
    tw = TranscriptWriter(run_dir, run_id="rH", max_bytes=300)
    for i in range(40):
        tw.write_event("ev", i=i)
    tw.close()

    for path in _collect_all_files_in_order(run_dir):
        lines = _read_jsonl(path)
        assert lines, f"{path} is empty"
        assert "header" in lines[0], f"{path} first line is not a header: {lines[0]}"


# ---------------------------------------------------------------------------
# 9. event fields: step_path and stream_path defaults and custom values
# ---------------------------------------------------------------------------


def test_event_default_paths(tmp_path):
    run_dir = tmp_path / "runI"
    with TranscriptWriter(run_dir, run_id="rI") as tw:
        tw.write_event("noop")

    lines = _read_jsonl(run_dir / _FILENAME)
    ev = lines[1]["event"]  # lines[0] is header
    assert ev["step_path"] == []
    assert ev["stream_path"] == []


def test_event_custom_paths(tmp_path):
    run_dir = tmp_path / "runJ"
    with TranscriptWriter(run_dir, run_id="rJ") as tw:
        tw.write_event(
            "step_end",
            step_path=["outer", "inner"],
            stream_path=["agent", "claude"],
            retval=42,
        )

    lines = _read_jsonl(run_dir / _FILENAME)
    ev = lines[1]["event"]
    assert ev["step_path"] == ["outer", "inner"]
    assert ev["stream_path"] == ["agent", "claude"]
    assert ev["retval"] == 42


# ---------------------------------------------------------------------------
# W2 FIX: write_event after close() raises RuntimeError (not AssertionError)
# and works correctly with python -O (optimised, asserts disabled)
# ---------------------------------------------------------------------------


def test_write_after_close_raises_runtime_error(tmp_path):
    run_dir = tmp_path / "runW2"
    tw = TranscriptWriter(run_dir, run_id="rW2")
    tw.close()
    with pytest.raises(RuntimeError, match="closed"):
        tw.write_event("late_event")


# ---------------------------------------------------------------------------
# N1 FIX: file_size tracking uses a single encode path (no double encode).
# Verify via correctness: internal counter must match on-disk size.
# ---------------------------------------------------------------------------


def test_size_tracking_accurate(tmp_path):
    """Internal _file_size must equal the actual on-disk byte count."""
    run_dir = tmp_path / "runN1"
    tw = TranscriptWriter(run_dir, run_id="rN1", max_bytes=10_000_000)
    for i in range(10):
        tw.write_event("ev", payload="hello world", idx=i)

    actual_size = (run_dir / _FILENAME).stat().st_size
    assert tw._file_size == actual_size, (
        f"Internal file_size={tw._file_size} != on-disk size={actual_size}"
    )
    tw.close()


# ---------------------------------------------------------------------------
# Context-manager usage
# ---------------------------------------------------------------------------


def test_context_manager_closes_file(tmp_path):
    run_dir = tmp_path / "runCM"
    with TranscriptWriter(run_dir, run_id="rCM") as tw:
        tw.write_event("ev")
    assert tw._file is None


# ---------------------------------------------------------------------------
# Smoke test: single rotation at 1KB via constructor kwarg
# ---------------------------------------------------------------------------


def test_rotation_at_1kb(tmp_path):
    run_dir = tmp_path / "run1kb"
    tw = TranscriptWriter(run_dir, run_id="r1kb", max_bytes=1024)
    for i in range(30):
        tw.write_event("ev", payload="A" * 30, idx=i)
    tw.close()
    assert (run_dir / f"{_FILENAME}.1").exists()
