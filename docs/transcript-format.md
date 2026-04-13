# Transcript Format (v1)

The **transcript** is Godel's advisory observability stream — a JSONL file written alongside
the authoritative audit log that external tools (dashboards, log shippers, debuggers) can
tail in real time.

> **Advisory vs. authoritative.** The transcript is best-effort. The audit log
> (`runs/<run_id>.jsonl`) is the authoritative record used by `godel resume`,
> `godel rewind`, and all replay machinery. Do not build correctness-critical logic on the
> transcript alone.

> **Status: v1 wire format is frozen; runtime wiring is landing incrementally.**
> `godel/_transcript.TranscriptWriter` implements the format described here. Wiring it
> into workflow execution (so `@workflow`/`@step`/`agent.call` events flow through the
> transcript) is tracked under the live-observability epic (`godel-py-5pl`, subtasks
> `5pl.2`–`5pl.7`). Until those land, the writer is exercised directly by the test suite
> and downstream reader tickets.

---

## File layout

Inside a run directory (typically `runs/<run_id>/`):

```
transcript.jsonl        # active file — tailable
transcript.jsonl.1      # most-recently rotated-out file
transcript.jsonl.2      # next-older file
...
```

The chain is ordered: `transcript.jsonl` is newest; `.1` is the previous segment; `.2` is
older still. Follow the rotation sentinel's `prev` field (see [Rotation](#rotation)) to
walk the chain in chronological order.

---

## Line 1: header

Every file (including rotated segments) begins with a **header** on line 1:

```json
{"header": {"v": 1, "run_id": "01HX7ABCDEF0123456789GHJKM", "started_at": "2026-04-13T10:00:00.000000+00:00"}}
```

The top-level key `"header"` is intentionally distinct from the `"event"` key used on
every subsequent line. A reader can detect the header with a single key-presence check:

```python
obj = json.loads(line)
if "header" in obj:
    # it's the header
elif "event" in obj:
    # it's an event or a rotation sentinel
```

### Header fields

| Field        | Type   | Description                                                 |
|--------------|--------|-------------------------------------------------------------|
| `v`          | int    | Format major version. Currently `1`.                        |
| `run_id`     | string | Identifier for the run. Same across all rotated segments.   |
| `started_at` | string | ISO 8601 UTC timestamp when this file was opened.           |

---

## Events

Every non-header line is an **event**:

```json
{"event": {"ts": "2026-04-13T10:00:01.234567+00:00", "seq": 1, "op": "step.enter",
           "step_path": ["fetch", "parse"],
           "stream_path": ["01HX7LAUNCH0000000000000001"]}}
```

### Core event fields

| Field         | Type            | Description                                                                                            |
|---------------|-----------------|--------------------------------------------------------------------------------------------------------|
| `ts`          | string          | ISO 8601 UTC timestamp of the event.                                                                   |
| `seq`         | int             | Strictly monotonic sequence number on **real** events, starting at `1`. Never resets across rotations. Rotation sentinels do **not** carry `seq` (see below). |
| `op`          | string          | Operation name. See [Operations](#operations).                                                         |
| `step_path`   | list\[string\]  | Hierarchical step address, e.g. `["fetch", "parse"]`. Empty list `[]` for workflow-level events.       |
| `stream_path` | list\[string\]  | Hierarchical stream address — a list of **launch-site ULIDs** stamped at each subprocess/agent launch (see below). Empty list `[]` for the root stream. |

Op-specific fields appear alongside the core fields in the same `"event"` object.

### `stream_path`

`stream_path` is a **list of ULIDs**, one per nested launch boundary. A ULID is stamped
by `godel/_run.py` (and by the agent wrappers) at every subprocess / agent launch and
pushed onto a `ContextVar`, so events emitted under that launch inherit the extended path.

Example: a workflow that launches a Claude agent which shells out to `git`:

```
[]                                                                       # root workflow
["01HX7LAUNCH0000000000000001"]                                          # inside the Claude launch
["01HX7LAUNCH0000000000000001", "01HX7LAUNCH0000000000000002"]           # git subprocess inside Claude
```

Readers that care only about a specific depth can filter by list length or by prefix.

> **Historical note.** An earlier design used a scalar `stream_id`. Ticket
> `godel-py-5pl.1` superseded that design and mandated list-typed `stream_path` for
> hierarchical addressing. Downstream reader tooling is authored against the list shape.

### Operations

The transcript writer itself is op-agnostic: `TranscriptWriter.write_event(op=..., ...)`
accepts any string and serialises it verbatim. The op **vocabulary** is defined by the
emit sites in the rest of the library. The ops currently emitted by the audit log (and
expected to mirror into the transcript as wiring is completed under `5pl.2`–`5pl.7`) are:

| `op`                | Emitted by                 | Meaning                                                   |
|---------------------|----------------------------|-----------------------------------------------------------|
| `WORKFLOW_STARTED`  | `@workflow` entry          | Workflow invocation began.                                |
| `step.enter`        | `@step` entry              | Step function began.                                      |
| `FORK`              | `parallel()`               | Concurrent branches forked.                               |
| `JOIN`              | `parallel()`               | Concurrent branches joined.                               |
| `PAUSED`            | `@workflow` pause path     | Workflow paused via `PauseSignal`.                        |
| `REWIND`            | `godel.rewind()`           | Graph-cut rewind applied.                                 |
| `agent.call`        | `godel.agents`             | Agent invocation.                                         |
| `run`               | `godel.run()`              | Audited subprocess invocation.                            |
| `print`             | `godel.io.print`           | Captured `print()` call.                                  |
| `input`             | `godel.io.input`           | Captured `input()` call.                                  |
| `det.now`           | `godel.det.now()`          | Deterministic clock read.                                 |
| `det.random`        | `godel.det.random()`       | Deterministic RNG draw.                                   |
| `det.uuid4`         | `godel.det.uuid4()`        | Deterministic UUID draw.                                  |
| `UNRECOVERABLE`     | `godel.intervention`       | Intervention tooling marked a failure unrecoverable.      |
| `rotate`            | `TranscriptWriter` itself  | Rotation sentinel (transcript-only; see below).           |

Additional op-specific fields (e.g. `duration_ms`, `error_type`, `exit_code`) may appear
alongside the core fields and MUST be ignored by readers that do not recognise them
(minor-version compatibility).

---

## Rotation

Rotation fires when the size of the active file plus the encoded upcoming line would
reach or exceed the configured limit (default: **50 MB**; override with
`GODEL_TRANSCRIPT_MAX_BYTES`).

### Rotation sentinel

A **rotation sentinel** event is appended as the **last line** of the outgoing file.
Sentinels intentionally carry **no `seq` field** — see the collision note below.

```json
{"event": {"ts": "2026-04-13T10:05:00+00:00", "op": "rotate",
           "step_path": [], "stream_path": [],
           "last_seq": 46, "prev": "transcript.jsonl.2"}}
```

| Sentinel field | Description                                                                                                    |
|----------------|----------------------------------------------------------------------------------------------------------------|
| `op`           | Always `"rotate"`.                                                                                             |
| `last_seq`     | The seq of the last **real** (non-sentinel) event written to this segment. **Authoritative** for locating file boundaries by seq. |
| `prev`         | Filename of the next-older segment after the rename cascade, or `null` if this is the first rotation.          |
| `step_path`    | Always `[]`.                                                                                                   |
| `stream_path`  | Always `[]`.                                                                                                   |

> **Why no `seq` on sentinels?** A sentinel written with `seq = self._seq` would share its
> value with the first real event in the next rotated file (because `_seq` is not
> incremented until the next real `write_event` call). Readers that consume every line —
> including sentinels — would see duplicate `seq` values at file boundaries. The fix is
> to omit `seq` entirely on sentinels; `last_seq` is the sole authoritative seq reference
> on a rotate-op event. Readers MUST NOT expect a `seq` key on `op="rotate"` events — its
> absence is part of the reader contract (see `godel-py-vaz`).

### Rotation protocol

1. Write the sentinel as the final line of the outgoing file.
2. `flush()` + `os.fsync()` to make the sentinel durable.
3. Rename cascade: `.N` → `.(N+1)`, ..., `.1` → `.2`.
4. Rename the outgoing file (current) → `.1`.
5. Open a fresh `transcript.jsonl` and write a new header. The `seq` counter is **not**
   reset — it continues from where it left off.

If any step after writing the sentinel fails (e.g. `os.rename` raises), the writer is
left in an unusable state; the next `write_event` call will propagate the original
exception.

### Walking the chain

To read a complete run chronologically:

```
transcript.jsonl   ← newest (tail this live)
transcript.jsonl.1 ← most-recently rotated segment
transcript.jsonl.2 ← older still
...
```

A reader can follow `sentinel.prev` to walk backwards from `.1` through the chain, or
stitch segments by `last_seq` (every segment's `last_seq` is exactly one less than the
next-newer segment's first real event `seq`).

### Crash recovery

Crash recovery (reopening a pre-existing `transcript.jsonl` from a prior crashed run) is
**not supported in v1**. Reusing a run directory after a crash produces duplicate `seq`
numbers. Always use a fresh run directory per run.

---

## Semver policy

`v` in the header follows the **major component of semver**:

- A reader **must** raise a version error (`godel._transcript.TranscriptVersionError`, or
  an equivalent for non-Python readers) if `v` exceeds the highest major version it
  understands.
- A reader **must silently accept** unknown minor-version additions — new optional fields
  in event objects, new `op` values, new sentinel fields. Do not reject events whose
  shape you do not recognise.
- A major bump happens when the change would break a reader written against the previous
  major: renaming a required field, changing the type of `seq` from int to string,
  removing `step_path`, etc.
- Additive changes are minor bumps and do not require a version check.

---

## Annotated example

A run with three events followed by a rotation:

**`transcript.jsonl.1`** (rotated segment):

```
{"header": {"v": 1, "run_id": "01HX7ABCDEF0123456789GHJKM", "started_at": "2026-04-13T10:00:00+00:00"}}
{"event": {"ts": "2026-04-13T10:00:01+00:00", "seq": 1, "op": "WORKFLOW_STARTED", "step_path": [], "stream_path": []}}
{"event": {"ts": "2026-04-13T10:00:02+00:00", "seq": 2, "op": "step.enter", "step_path": ["fetch"], "stream_path": []}}
{"event": {"ts": "2026-04-13T10:00:03+00:00", "seq": 3, "op": "agent.call", "step_path": ["fetch"], "stream_path": ["01HX7LAUNCH0000000000000001"]}}
{"event": {"ts": "2026-04-13T10:00:03+00:00", "op": "rotate", "step_path": [], "stream_path": [], "last_seq": 3, "prev": null}}
```

**`transcript.jsonl`** (active):

```
{"header": {"v": 1, "run_id": "01HX7ABCDEF0123456789GHJKM", "started_at": "2026-04-13T10:00:03+00:00"}}
{"event": {"ts": "2026-04-13T10:00:04+00:00", "seq": 4, "op": "step.enter", "step_path": ["summarise"], "stream_path": []}}
```

Note that `seq` is globally monotonic across real events in both files (1 → 3 in `.1`,
then 4 onwards in the active file). The sentinel carries `last_seq: 3` and `prev: null`
(this was the first rotation, so there was no prior `.1` to point to).

---

## See also

- [Redaction](redaction.md) — filtering events before they are written (status: runtime
  plumbing in flight, `godel-py-5pl.6`).
- [Stdout capture](stdout-capture.md) — per-step stdout capture (status: runtime plumbing
  in flight, `godel-py-5pl.7`).
- [Concepts](concepts.md) — audit log, workflows, steps.
