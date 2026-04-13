"""Tests for godel/_transcript.py — TranscriptWriter acceptance criteria."""
from __future__ import annotations

import json
import os
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
    """Return [transcript.jsonl.N, ..., transcript.jsonl.1, transcript.jsonl]
    in oldest-first order so that concatenating them yields the write order.
    """
    result: list[Path] = []
    n = 1
    # walk up to find how many suffixed files exist
    suffixed: list[Path] = []
    i = 1
    while True:
        p = run_dir / f"{_FILENAME}.{i}"
        if p.exists():
            suffixed.append(p)
            i += 1
        else:
            break
    # suffixed[0] == .1 (most recent rotate-out), suffixed[-1] == .N (oldest)
    # we want oldest first
    result.extend(reversed(suffixed))
    current = run_dir / _FILENAME
    if current.exists():
        result.append(current)
    return result


def _all_events_in_order(run_dir: Path) -> list[dict]:
    """Concatenate all transcript files in write-chronological order,
    return only the `event` entries (skip headers and sentinels for ordering check).
    """
    all_lines: list[dict] = []
    for path in _collect_all_files_in_order(run_dir):
        all_lines.extend(_read_jsonl(path))
    return all_lines


# ---------------------------------------------------------------------------
# 1. Header shape-distinctness
# ---------------------------------------------------------------------------


def test_header_is_first_line_and_shape_distinct(tmp_path):
    run_dir = tmp_path / "run1"
    with TranscriptWriter(run_dir, run_id="r1") as tw:
        tw.write_event("step_start", step_path=["fetch"])

    lines = _read_jsonl(run_dir / _FILENAME)
    assert len(lines) >= 2
    # Line 1 must have "header" key, not "event"
    assert "header" in lines[0], f"First line lacks 'header' key: {lines[0]}"
    assert "event" not in lines[0]
    # Line 2 must have "event" key
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
# 2. seq strictly monotonic starting at 1, across N rotations
# ---------------------------------------------------------------------------


def test_seq_monotonic_across_5_rotations(tmp_path):
    run_dir = tmp_path / "runA"
    # Use a tiny max_bytes so we rotate many times.
    tw = TranscriptWriter(run_dir, run_id="rA", max_bytes=200)
    n_events = 50
    for i in range(n_events):
        tw.write_event("tick", extra_i=i)
    tw.close()

    all_lines = _all_events_in_order(run_dir)
    # Filter to real events (not sentinels, not headers)
    real_events = [
        line["event"]
        for line in all_lines
        if "event" in line and line["event"].get("op") != "rotate"
    ]

    seqs = [e["seq"] for e in real_events]
    assert seqs[0] == 1, f"First seq should be 1, got {seqs[0]}"
    for i in range(1, len(seqs)):
        assert seqs[i] == seqs[i - 1] + 1, (
            f"seq gap at index {i}: {seqs[i - 1]} → {seqs[i]}"
        )
    assert len(real_events) == n_events

    # Confirm we actually rotated at least 5 times
    rotations = sum(
        1
        for line in all_lines
        if "event" in line and line["event"].get("op") == "rotate"
    )
    assert rotations >= 5, f"Expected >= 5 rotations, got {rotations}"


# ---------------------------------------------------------------------------
# 3. Rotation sentinel is last line of the outgoing .1 file
# ---------------------------------------------------------------------------


def test_sentinel_is_last_line_of_rotated_file(tmp_path):
    run_dir = tmp_path / "runB"
    tw = TranscriptWriter(run_dir, run_id="rB", max_bytes=200)
    # Write enough to trigger at least one rotation
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


def test_sentinel_prev_field(tmp_path):
    run_dir = tmp_path / "runC"
    tw = TranscriptWriter(run_dir, run_id="rC", max_bytes=200)
    for i in range(20):
        tw.write_event("step", x=i)
    tw.close()

    rotated = run_dir / f"{_FILENAME}.1"
    lines = _read_jsonl(rotated)
    sentinel = lines[-1]["event"]
    assert sentinel.get("prev") == f"{_FILENAME}.1", (
        f"sentinel.prev should be '{_FILENAME}.1', got {sentinel.get('prev')!r}"
    )


# ---------------------------------------------------------------------------
# 4. Concatenation of all files in order equals full write order
# ---------------------------------------------------------------------------


def test_full_concat_matches_write_order(tmp_path):
    run_dir = tmp_path / "runD"
    tw = TranscriptWriter(run_dir, run_id="rD", max_bytes=200)
    written_seqs: list[int] = []
    for i in range(40):
        seq = tw.write_event("ev", i=i)
        written_seqs.append(seq)
    tw.close()

    all_lines = _all_events_in_order(run_dir)
    observed_seqs = [
        line["event"]["seq"]
        for line in all_lines
        if "event" in line and line["event"].get("op") != "rotate"
    ]
    assert observed_seqs == written_seqs


# ---------------------------------------------------------------------------
# 5. Version check: refuses v=2, accepts unknown minor (v=1 only right now)
# ---------------------------------------------------------------------------


def test_version_check_rejects_unknown_major():
    with pytest.raises(TranscriptVersionError, match="not supported"):
        TranscriptWriter.check_version({"v": TRANSCRIPT_FORMAT_VERSION + 1})


def test_version_check_accepts_current_major():
    # Should not raise
    TranscriptWriter.check_version({"v": TRANSCRIPT_FORMAT_VERSION})


def test_version_check_accepts_missing_v():
    # Missing v is treated as version 1 — should not raise
    TranscriptWriter.check_version({})


# ---------------------------------------------------------------------------
# 6. Concurrent writers: 8 threads, no corruption, no lost/duplicate seqs
# ---------------------------------------------------------------------------


def test_concurrent_8_threads(tmp_path):
    run_dir = tmp_path / "runE"
    # Small max_bytes to also exercise concurrent rotation paths
    tw = TranscriptWriter(run_dir, run_id="rE", max_bytes=1024)

    n_threads = 8
    events_per_thread = 50
    results: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            for _ in range(events_per_thread):
                seq = tw.write_event("thread_event")
                with lock:
                    results.append(seq)
        except Exception as exc:
            with lock:
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

    # All seqs must be unique and in range [1, total]
    assert sorted(results) == list(range(1, total + 1)), (
        "seqs are not a contiguous range 1..N — duplicates or gaps exist"
    )

    # Verify on-disk lines are uncorrupted (parseable JSON, no duplicates)
    all_lines = _all_events_in_order(run_dir)
    disk_seqs = [
        line["event"]["seq"]
        for line in all_lines
        if "event" in line and line["event"].get("op") != "rotate"
    ]
    assert sorted(disk_seqs) == list(range(1, total + 1)), (
        "On-disk seqs don't match expected range"
    )


# ---------------------------------------------------------------------------
# 7. GODEL_TRANSCRIPT_MAX_BYTES env var forces rotation at 1KB
# ---------------------------------------------------------------------------


def test_env_var_forces_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("GODEL_TRANSCRIPT_MAX_BYTES", "512")
    run_dir = tmp_path / "runF"
    # max_bytes from env (no explicit kwarg)
    tw = TranscriptWriter(run_dir, run_id="rF")
    assert tw._max_bytes == 512

    for i in range(30):
        tw.write_event("ev", payload="x" * 20, idx=i)
    tw.close()

    # At 512 bytes limit we should have rotated at least once
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
    tw = TranscriptWriter(run_dir, run_id="rH", max_bytes=200)
    for i in range(40):
        tw.write_event("ev", i=i)
    tw.close()

    for path in _collect_all_files_in_order(run_dir):
        lines = _read_jsonl(path)
        assert lines, f"{path} is empty"
        assert "header" in lines[0], f"{path} first line is not a header: {lines[0]}"


# ---------------------------------------------------------------------------
# 9. event fields: step_path, stream_path defaults
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
            stream_path=["stdout"],
            retval=42,
        )

    lines = _read_jsonl(run_dir / _FILENAME)
    ev = lines[1]["event"]
    assert ev["step_path"] == ["outer", "inner"]
    assert ev["stream_path"] == ["stdout"]
    assert ev["retval"] == 42
