#!/usr/bin/env python3
"""Benchmark harness: observability write-latency under sustained peak load.

Synthetic workload
------------------
- 4 parallel fake agents, each emitting events at *rate* events/s (default 200).
- Aggregate target: 800 events/s sustained for *duration* seconds (default 30).
- Each event goes through the full ``EventLog._append_event`` path (JSONL write +
  flush), mirroring the hot path exercised during real workflow runs.

Metrics collected
-----------------
- ``write_latency_ms``: wall-clock time for each ``_append_event`` call (p50/p95/p99/max).
- ``tail_latency_ms``: time from write timestamp to reader observation (p50/p99).
- ``rotation_count``: how many times the tail reader re-opened the file.
- ``transcript_total_bytes``: total bytes written to the JSONL log.

Output
------
``benchmarks/results/<YYYY-MM-DD>-<git-sha>.json`` — committed alongside the harness.
Each run appends rather than overwrites so baseline history accumulates.

Usage
-----
    python benchmarks/observability.py [--agents N] [--rate R] [--duration D]

    N        number of parallel fake agents (default 4)
    R        events/s per agent (default 200)
    D        duration in seconds (default 30)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve repo root so the script can be run from any directory.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"

sys.path.insert(0, str(REPO_ROOT))

from godel._context import _privileged  # noqa: E402
from godel._event_log import EventLog  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_data[lo]
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


def _stats(data: list[float]) -> dict:
    if not data:
        return {"p50": 0, "p95": 0, "p99": 0, "max": 0, "count": 0}
    return {
        "p50": round(_percentile(data, 50), 4),
        "p95": round(_percentile(data, 95), 4),
        "p99": round(_percentile(data, 99), 4),
        "max": round(max(data), 4),
        "count": len(data),
    }


# ---------------------------------------------------------------------------
# Writer: one thread per fake agent
# ---------------------------------------------------------------------------

def _writer_thread(
    agent_idx: int,
    run_id: str,
    runs_dir: str,
    rate: float,
    duration: float,
    write_latencies: list[float],
    write_timestamps: list[float],  # monotonic time at write
    stop_event: threading.Event,
) -> None:
    """Emit events into an EventLog at *rate* events/s for *duration* seconds."""
    interval = 1.0 / rate
    token = _privileged.set(True)
    try:
        log = EventLog(run_id=run_id, runs_dir=runs_dir)
    finally:
        _privileged.reset(token)

    deadline = time.monotonic() + duration
    seq = 0
    try:
        while not stop_event.is_set() and time.monotonic() < deadline:
            t0 = time.monotonic()

            # Emit a STARTED event (full write path)
            ev = log.emit_started(
                op="STEP_STARTED",
                step_path=(f"agent_{agent_idx}", f"step_{seq}"),
                request={"agent": agent_idx, "seq": seq},
            )
            # Emit a FINISHED event immediately (simulates instant completion)
            log.emit_finished(ev.event_id, response={"ok": True})

            t1 = time.monotonic()
            latency_ms = (t1 - t0) * 1000
            write_latencies.append(latency_ms)
            write_timestamps.append(t0)

            seq += 1

            # Pace to target rate
            elapsed = t1 - t0
            sleep_for = interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        log.close()


# ---------------------------------------------------------------------------
# Reader: tail the log and record observation latency
# ---------------------------------------------------------------------------

async def _tail_reader(
    run_id: str,
    runs_dir: str,
    write_timestamps: list[float],
    tail_latencies: list[float],
    rotation_count_ref: list[int],
    stop_event: asyncio.Event,
) -> int:
    """Read events from the JSONL tail and record latency from write to observation."""
    from godel._tail import tail  # local import to avoid polluting module level

    # We can't easily correlate individual write timestamps to tail events without
    # embedding the write time in the event. Instead we use a conservative proxy:
    # for each event observed by the tail we record the delta between wall-clock
    # now and the ts_start embedded in the event (which is set just before the
    # actual write). This measures write-to-read pipeline latency.
    event_count = 0
    rotation_count = 0
    last_inode: int | None = None

    log_path = Path(runs_dir) / f"{run_id}.jsonl"

    async for event in tail(
        run_id,
        runs_dir=runs_dir,
        follow=True,
        poll_interval=0.02,
        stop_on_terminal=False,
    ):
        if stop_event.is_set():
            break

        if event.ts_start:
            try:
                ts_start = datetime.fromisoformat(event.ts_start).timestamp()
                latency_ms = (time.time() - ts_start) * 1000
                # Cap outliers from clock skew to 10 s
                if 0 <= latency_ms <= 10_000:
                    tail_latencies.append(latency_ms)
            except ValueError:
                pass

        # Detect inode changes (rotation proxy)
        try:
            ino = log_path.stat().st_ino
            if last_inode is not None and ino != last_inode:
                rotation_count += 1
            last_inode = ino
        except FileNotFoundError:
            pass

        event_count += 1

    rotation_count_ref.append(rotation_count)
    return event_count


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_benchmark(
    n_agents: int = 4,
    rate: float = 200.0,
    duration: float = 30.0,
) -> dict:
    """Run the benchmark and return the results dict."""
    run_id = f"bench-obs-{int(time.time())}"

    with tempfile.TemporaryDirectory(prefix="godel_bench_") as tmp:
        runs_dir = tmp

        write_latencies: list[list[float]] = [[] for _ in range(n_agents)]
        write_timestamps: list[list[float]] = [[] for _ in range(n_agents)]
        tail_latencies: list[float] = []
        rotation_count_ref: list[int] = []

        stop_threads = threading.Event()

        # Launch writer threads
        threads = []
        for i in range(n_agents):
            t = threading.Thread(
                target=_writer_thread,
                args=(
                    i,
                    f"{run_id}-agent{i}",
                    runs_dir,
                    rate,
                    duration,
                    write_latencies[i],
                    write_timestamps[i],
                    stop_threads,
                ),
                daemon=True,
            )
            t.start()
            threads.append(t)

        # Give writers a head start so the log file exists before tailing
        time.sleep(0.1)

        # Run tail reader on the first agent's log
        tail_run_id = f"{run_id}-agent0"
        stop_tail = asyncio.Event()

        async def _run_tail() -> None:
            # Stop tailing after duration + a grace period
            async def _stopper() -> None:
                await asyncio.sleep(duration + 2.0)
                stop_tail.set()

            await asyncio.gather(
                _tail_reader(
                    tail_run_id,
                    runs_dir,
                    write_timestamps[0],
                    tail_latencies,
                    rotation_count_ref,
                    stop_tail,
                ),
                _stopper(),
                return_exceptions=True,
            )

        loop = asyncio.new_event_loop()
        tail_thread = threading.Thread(target=loop.run_until_complete, args=(_run_tail(),), daemon=True)
        tail_thread.start()

        # Wait for all writers to finish
        for t in threads:
            t.join()

        stop_threads.set()
        stop_tail.set()  # signal tail to stop

        tail_thread.join(timeout=5.0)
        # Close the loop only after its thread has exited
        if not loop.is_running():
            loop.close()

        # Aggregate write latencies across all agents
        all_write_latencies: list[float] = []
        for wl in write_latencies:
            all_write_latencies.extend(wl)

        # Compute transcript byte count
        total_bytes = 0
        for i in range(n_agents):
            p = Path(runs_dir) / f"{run_id}-agent{i}.jsonl"
            try:
                total_bytes += p.stat().st_size
            except FileNotFoundError:
                pass

        total_events = len(all_write_latencies)
        elapsed = duration  # nominal

        return {
            "run_id": run_id,
            "config": {
                "n_agents": n_agents,
                "rate_per_agent": rate,
                "target_aggregate_rate": n_agents * rate,
                "duration_s": duration,
            },
            "write_latency_ms": _stats(all_write_latencies),
            "tail_latency_ms": _stats(tail_latencies),
            "rotation_count": rotation_count_ref[0] if rotation_count_ref else 0,
            "transcript_total_bytes": total_bytes,
            "total_events_written": total_events,
            "actual_aggregate_rate": round(total_events / elapsed, 1) if elapsed > 0 else 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=4, metavar="N",
                        help="Number of parallel fake agents (default: 4)")
    parser.add_argument("--rate", type=float, default=200.0, metavar="R",
                        help="Events/s per agent (default: 200)")
    parser.add_argument("--duration", type=float, default=30.0, metavar="D",
                        help="Duration in seconds (default: 30)")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR,
                        help="Directory for result JSON files")
    args = parser.parse_args()

    print(f"Starting benchmark: {args.agents} agents × {args.rate} ev/s for {args.duration}s …")
    t_start = time.monotonic()

    results = run_benchmark(
        n_agents=args.agents,
        rate=args.rate,
        duration=args.duration,
    )

    wall = time.monotonic() - t_start
    results["wall_time_s"] = round(wall, 2)
    results["timestamp"] = datetime.now(timezone.utc).isoformat()
    results["git_sha"] = _git_sha()

    # Pretty-print summary
    wl = results["write_latency_ms"]
    tl = results["tail_latency_ms"]
    print(
        f"\nResults:\n"
        f"  events written : {results['total_events_written']:,}\n"
        f"  agg rate       : {results['actual_aggregate_rate']} ev/s\n"
        f"  write_latency  : p50={wl['p50']} ms  p95={wl['p95']} ms  "
        f"p99={wl['p99']} ms  max={wl['max']} ms\n"
        f"  tail_latency   : p50={tl['p50']} ms  p99={tl['p99']} ms\n"
        f"  transcript     : {results['transcript_total_bytes']:,} bytes\n"
        f"  rotations      : {results['rotation_count']}\n"
        f"  wall time      : {wall:.1f}s\n"
    )

    # Write result file
    args.output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sha = results["git_sha"]
    out_path = args.output_dir / f"{date_str}-{sha}.json"

    # Accumulate runs in the same file (one JSON object per line)
    with open(out_path, "a") as f:
        f.write(json.dumps(results, separators=(",", ":")) + "\n")

    print(f"Result written to: {out_path}")


if __name__ == "__main__":
    main()
