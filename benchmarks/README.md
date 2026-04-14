# Benchmarks

## Owner

The **releaser** is responsible for running the observability benchmark before each release and committing the result. This is a manual step — it is **not** run by CI.

## Observability benchmark (`observability.py`)

Exercises the `EventLog` write path under sustained peak load: 4 parallel fake agents each emitting 200 events/s (800 events/s aggregate) for 30 seconds. Measures:

| Metric | Description |
|---|---|
| `write_latency_ms` | Wall-clock time for each `_append_event` call (p50 / p95 / p99 / max) |
| `tail_latency_ms` | Time from event `ts_start` to reader observation (p50 / p99) |
| `rotation_count` | Number of log file re-opens detected by the tail reader |
| `transcript_total_bytes` | Total bytes written to all JSONL logs |

### Running

```bash
python benchmarks/observability.py
```

Optional flags:

```
--agents N    Number of parallel fake agents (default: 4)
--rate R      Events/s per agent (default: 200)
--duration D  Duration in seconds (default: 30)
```

The script must complete in under 120 seconds on default settings.

### Results

Results are written to `benchmarks/results/<YYYY-MM-DD>-<git-sha>.json` as
newline-delimited JSON (one object per run).  The first baseline result is
committed with the harness.  Subsequent runs should be committed before
tagging a release.

### Release gate

Before tagging a release:

1. Run `python benchmarks/observability.py` on the target machine.
2. Commit the new result file: `git add benchmarks/results/ && git commit -m "chore: add benchmark result for vX.Y.Z"`.
3. Verify p99 write latency has not regressed beyond the previous baseline by more than 2×.
