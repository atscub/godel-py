# Monitoring godel runs (agent guide)

Lessons from monitoring `examples/feature_factory.py` runs. Aimed at future
agents that need to babysit a long workflow without burning context or losing
the run to a terminal mishap.

## TL;DR

- Run bare (`python -m godel run FILE`), **not** `--watch` / `--plain`. The
  watchers can spam terminals enough to SIGHUP the workflow (see
  `godel-py-7kd`). Until that's fixed, monitor from a separate channel.
- Tail `runs/<id>.jsonl` directly. It is append-only JSON-per-line and is
  the source of truth for everything godel did.
- Filter aggressively. The raw stream contains tool calls, streamed agent
  chunks, and per-step entry/exit; ~95% is noise for high-level oversight.
- Use the harness's event-monitor primitive (one notification per filtered
  line) instead of polling — fewer cache misses, instant on state change.

## Files written per run

```
runs/<id>.jsonl           # canonical audit log (truncated payloads)
runs/<id>/transcript.jsonl  # full chunked stream: prompts, tool calls,
                            # streamed responses, run.start
```

`request.prompt` and `response.value` in `<id>.jsonl` are clipped to ~500
chars. Full content lives in `transcript.jsonl` as chunked
`agent.response` events grouped by `stream_path`. Reassemble by
concatenating `text` fields per `stream_path`. (Tracked: `godel-py-6wg`.)

## High-signal event shapes

For oversight, these are the only events worth surfacing by default:

| op                  | status   | meaning                                    |
| ------------------- | -------- | ------------------------------------------ |
| `print`             | FINISHED | workflow's own narrative log lines         |
| `input`             | STARTED  | checkpoint waiting on stdin                |
| `agent.call`        | FINISHED | one agent decision completed (with model + schema name) |
| `agent.call`        | FAILED   | agent error or schema validation fail      |
| `step.enter`        | FAILED   | step body raised                           |
| `WORKFLOW_*`        | FINISHED / FAILED | run lifecycle                              |

Skip `run STARTED`, `step.enter STARTED`, intermediate `agent.call STARTED`
unless debugging — they are very chatty in any non-trivial workflow.

## Monitor recipe (event-driven)

**Preferred: `godel tail`.** It is a native Python CLI that follows
`runs/<id>.jsonl` without shell pipe buffering, waits for new events,
and exits at run completion.

```
godel tail <id> --format=json   # one JSON object per line, stable schema
godel tail <id> --format=pretty # human-readable table (step_path + status + duration)
godel tail <id> --no-follow     # drain once and exit
godel tail <id> --no-wait       # fail if log file doesn't exist yet
```

Pipe `--format=json` into a filter and wire into the harness `Monitor`:

```bash
Monitor(persistent=true,
        command="godel tail <id> --format=json | python -u /tmp/godel_filter.py")
```

Where the filter is just the per-line logic below (no seek/tell bookkeeping).

**Fallback: pure-python file seek.** If for some reason `godel tail` isn't
available, read the file directly — do *not* use `tail -F | python`,
shell pipe buffering can delay events by minutes.

```python
# /tmp/godel_monitor.py
import json, os, time, sys

P = sys.argv[1]                   # path to runs/<id>.jsonl
pos = os.path.getsize(P) if os.path.exists(P) else 0
while True:
    try: size = os.path.getsize(P)
    except FileNotFoundError: time.sleep(1); continue
    if size > pos:
        with open(P) as f:
            f.seek(pos)
            for line in f:
                if not line.strip(): continue
                try: e = json.loads(line)
                except: continue
                op, st = e.get("op"), e.get("status")
                sp = "/".join(e.get("step_path") or [])
                if op == "print" and st == "FINISHED":
                    t = (e.get("request") or {}).get("text","").strip()
                    if t: print("LOG", t, flush=True)
                elif op == "input" and st == "STARTED":
                    print("CHECKPOINT", sp, flush=True)
                elif op == "agent.call" and st == "FINISHED":
                    r = e.get("request") or {}
                    print(f"AGENT {r.get('model')} schema={r.get('schema_name')} @ {sp}", flush=True)
                elif st == "FAILED":
                    err = str((e.get("response") or {}).get("error",""))[:160]
                    print(f"FAIL {op} @ {sp} :: {err}", flush=True)
                elif op == "WORKFLOW_FINISHED" or (op=="WORKFLOW_STARTED" and st=="FINISHED"):
                    print(f"WORKFLOW {st}", flush=True)
            pos = f.tell()
    time.sleep(1)
```

Wire that to `Monitor(persistent=true, command="python -u /tmp/godel_monitor.py runs/<id>.jsonl")`.
Each filtered line becomes one harness notification — no polling, no cache
churn, instant on event.

## Polling fallback (when Monitor isn't available)

If you must poll, do it cache-aware: every 250–270s (stay inside the 5-min
prompt cache) is the right cadence for slow agent calls. Compute a small
summary, never dump raw events:

```python
import json, collections
ev=[json.loads(l) for l in open(p)]
last=ev[-1]
ac=[e for e in ev if e.get("op")=="agent.call"]
finished=collections.Counter(
    (e.get("step_path") or ["?"])[-1]
    for e in ev if e.get("op")=="step.enter" and e.get("status")=="FINISHED"
)
# print: ev count, last op/status/step, agent fin/fail, finished steps
```

Don't poll faster than ~120s; agent calls regularly take 1–3 min.

## Identifying the in-flight agent

A `STARTED` event with no matching `FINISHED`/`FAILED` (compare on
`request_hash`) is in flight:

```python
inflight = [e for e in ev
            if e.get("op")=="agent.call" and e.get("status")=="STARTED"
            and not any(x.get("request_hash")==e.get("request_hash")
                        and x.get("status") in ("FINISHED","FAILED")
                        for x in ev)]
```

Surfaces model + schema_name + step path = enough to tell the user "opus
crunching PlanReview right now."

## Reading what an agent actually said

Schema'd responses appear in `<id>.jsonl` as a truncated `response.value`
string repr of the pydantic model. To get the full structured output:

1. From `<id>.jsonl`, grab the event's `step_path` and `stream_path`.
2. In `transcript.jsonl`, collect `agent.response` events with the same
   `step_path` and matching `stream_path` (a tuple).
3. Concatenate their `text` fields in seq order — that's the raw model
   output (typically JSON).
4. `json.JSONDecoder().raw_decode(text.lstrip())` to parse (the model may
   continue after the JSON closes; raw_decode stops at the first object).

## Recovery patterns

- **Terminal died, run cancelled (`CancelledError`)**: try
  `python -m godel resume <id>` first. If it aborts with
  `UnsafeResumeError`, the dead step had a non-idempotent in-flight
  side-effect call. Three options, in order of preference:
  1. **Rewind then resume.** Identify the last `step.enter FINISHED`
     (or `agent.call FINISHED`) in `runs/<id>.jsonl`, then
     `godel rewind <id> --to <event_id>` followed by
     `godel resume <id>`. The rewind trims the log past that point so
     resume has a clean tail to replay from. Useful when the failed
     call was unrecoverable but the step above it can be re-executed.
  2. `godel repair <id>` — drops an intervention agent into the
     crashed run to unstick manually.
  3. Fresh run (last resort — loses all prior agent tokens).

  (`godel-py-ddt` proposes opt-in idempotency to make option 1 less
  necessary.)
- **Run looks stuck**: check whether last event is `input STARTED` (waiting
  on stdin) before assuming a hang. `input()` is `sys.stdin.readline()` —
  pipe stdin or press enter in the controlling terminal.
- **Run still alive?**: `ps -ef | grep "godel run"`. No process + no
  recent jsonl writes = dead.

## Token thrift while monitoring

- Never read the full audit log. Tail offsets, last N lines, or summary
  counters only.
- Never paste raw transcript chunks into your context — reassemble the
  small slice you need (one agent's response), then summarize.
- Monitor notifications are cheap; one line per event. Polling snapshots
  are expensive; keep them under ~10 lines of formatted output.
- Each schedule wakeup invalidates the prompt cache if it lands past 5
  min. Choose 270s (cache-warm) or 1200s+ (one cold fetch buys a long
  wait); avoid the 300–600s sour spot.
