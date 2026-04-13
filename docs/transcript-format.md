# Transcript Format (v1)

The **transcript** is Godel's advisory observability stream — a JSONL file written alongside
the audit log that external tools (dashboards, log shippers, debuggers) can tail in real
time without needing to parse the internal audit-log schema.

> **Advisory vs. authoritative.** The transcript is best-effort. The audit log
> (`runs/<run_id>.jsonl`) is the authoritative record used by `godel resume`, `godel rewind`,
> and all replay machinery. Do not build correctness-critical logic on the transcript alone.

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
older still. Follow the rotation sentinel (see [Rotation](#rotation)) to walk the chain in
chronological order.

---

## Line 1: header

Every file (including rotated segments) begins with a **header** on line 1:

```json
{"header": {"v": 1, "run_id": "01HX...", "started_at": "2026-04-13T10:00:00.000000+00:00"}}
```

The top-level key `"header"` is intentionally distinct from the `"event"` key used on every
subsequent line. A reader can detect the header with a single key-presence check:

```python
obj = json.loads(line)
if "header" in obj:
    # it's the header
elif "event" in obj:
    # it's an event or a rotation sentinel
```

### Header fields

| Field        | Type   | Description                                      |
|--------------|--------|--------------------------------------------------|
| `v`          | int    | Format major version. Currently `1`.             |
| `run_id`     | string | UUID identifying the run. Same across rotations. |
| `started_at` | string | ISO 8601 UTC timestamp when the file was opened. |

---

## Events

Every non-header line is an **event**:

```json
{"event": {"ts": "2026-04-13T10:00:01.234567+00:00", "seq": 1, "op": "step_start",
           "step_path": ["fetch", "parse"], "stream_path": ["agent", "claude"], "duration_ms": 42}}
```

### Core event fields

| Field         | Type           | Description                                                                                     |
|---------------|----------------|-------------------------------------------------------------------------------------------------|
| `ts`          | string         | ISO 8601 UTC timestamp of the event.                                                            |
| `seq`         | int            | Strictly monotonic sequence number, starting at `1`. Never resets across rotations.            |
| `op`          | string         | Operation name. See [Operations](#operations).                                                  |
| `step_path`   | list\[string\] | Hierarchical step address, e.g. `["fetch", "parse"]`. Empty list `[]` for workflow-level ops. |
| `stream_path` | list\[string\] | Hierarchical stream address. Empty list `[]` for the root stream.                              |

Op-specific fields appear alongside the core fields in the same `"event"` object.

### `stream_path`

`stream_path` is a **list** of strings that identifies which observability stream an event
belongs to. A workflow that launches a sub-agent which in turn calls a tool would emit events
with `stream_path` values like:

```
[]                          # root workflow
["agent", "claude"]         # the Claude agent layer
["agent", "claude", "tool"] # a tool call inside Claude
```

The list grows as nested launches are initiated. Readers that care only about a specific
depth can filter by prefix-matching.

> **Why a list, not a scalar?** An earlier design used a scalar `stream_id`. The ticket
> superseding that design (godel-py-5pl.1) mandated list-typed `stream_path` for
> hierarchical addressing. Downstream reader tooling is authored against the list shape.

### Operations

Common `op` values:

| `op`             | Description                                 |
|------------------|---------------------------------------------|
| `step_start`     | A `@step`-decorated function began.         |
| `step_end`       | A `@step` completed successfully.           |
| `step_error`     | A `@step` raised an exception.              |
| `workflow_start` | The `@workflow` began.                      |
| `workflow_end`   | The `@workflow` completed.                  |
| `agent_call`     | An agent callable was invoked.              |
| `agent_response` | An agent callable returned.                 |
| `redactor_error` | A redactor callable raised. Payload omitted — see [Redaction](redaction.md). |
| `rotate`         | Rotation sentinel. See [Rotation](#rotation). |

Additional op-specific fields (e.g. `duration_ms`, `error_type`, `exit_code`) may appear
and MUST be ignored by readers that do not recognise them (minor-version compatibility).

---

## Rotation

Rotation fires when the size of the active file plus the encoded upcoming line would reach
or exceed the configured limit (default: **50 MB**; override with `GODEL_TRANSCRIPT_MAX_BYTES`).

### Rotation protocol

1. A **rotation sentinel** event is appended as the **last line** of the outgoing file:

   ```json
   {"event": {"ts": "...", "seq": 47, "last_seq": 46, "op": "rotate",
              "step_path": [], "stream_path": [],
              "prev": "transcript.jsonl.2"}}
   ```

   | Sentinel field | Description                                                                 |
   |----------------|-----------------------------------------------------------------------------|
   | `seq`          | The next seq that will be assigned (informational).                         |
   | `last_seq`     | The seq of the last **real** (non-sentinel) event written to this segment.  |
   | `op`           | Always `"rotate"`.                                                          |
   | `prev`         | Filename of the next-older segment after the rename cascade, or `null` if this is the first rotation. |

2. The file is flushed and `fsync`-ed before any rename, ensuring the sentinel is durable.

3. Existing suffixed files are shifted: `.N` → `.(N+1)`, ..., `.1` → `.2`.

4. The outgoing file is renamed to `.1`.

5. A fresh `transcript.jsonl` is opened with a new header. The `seq` counter is **not**
   reset — it continues from where it left off.

### Walking the chain

To read a complete run chronologically, walk **backwards** through suffixes by following
`sentinel.prev`, then reverse the collected segments:

```
transcript.jsonl   ← newest (tail this live)
  sentinel.prev → "transcript.jsonl.2"   ← segment that preceded .1
transcript.jsonl.1 ← most-recently rotated
transcript.jsonl.2 ← older still
```

A reader can also stitch segments by `seq`: events are globally ordered by `seq` regardless
of which file they live in.

### Crash recovery

Crash recovery (reopening a pre-existing `transcript.jsonl` from a prior crashed run) is
**not supported in v1**. Reusing a run directory after a crash produces duplicate `seq`
numbers. Always use a fresh run directory per run.

---

## Semver policy

`v` in the header follows the **major component of semver**:

- A reader **must** raise `TranscriptVersionError` (or equivalent) if `v` exceeds the
  highest major version it understands.
- A reader **must silently accept** unknown minor-version additions (new fields in event
  objects). Do not reject events whose `op` or extra fields you do not recognise.
- A major bump happens when the change would break a reader written against the previous
  major: for example, renaming a required field, changing the type of `seq` from int to
  string, or removing `step_path`.
- Additive changes (new optional fields, new `op` values) are minor bumps and do not
  require a version check.

The Python helper for readers:

```python
from godel._transcript import TranscriptWriter, TranscriptVersionError

header = json.loads(first_line)["header"]
TranscriptWriter.check_version(header)  # raises TranscriptVersionError if major too high
```

---

## Annotated example

A run with three steps followed by a rotation:

**`transcript.jsonl.1`** (rotated segment):

```
{"header": {"v": 1, "run_id": "01HX-ABC", "started_at": "2026-04-13T10:00:00+00:00"}}
{"event": {"ts": "2026-04-13T10:00:01+00:00", "seq": 1, "op": "workflow_start", "step_path": [], "stream_path": []}}
{"event": {"ts": "2026-04-13T10:00:02+00:00", "seq": 2, "op": "step_start", "step_path": ["fetch"], "stream_path": []}}
{"event": {"ts": "2026-04-13T10:00:03+00:00", "seq": 3, "op": "step_end",   "step_path": ["fetch"], "stream_path": [], "duration_ms": 980}}
{"event": {"ts": "2026-04-13T10:00:03+00:00", "seq": 3, "last_seq": 3, "op": "rotate", "step_path": [], "stream_path": [], "prev": null}}
```

**`transcript.jsonl`** (active):

```
{"header": {"v": 1, "run_id": "01HX-ABC", "started_at": "2026-04-13T10:00:03+00:00"}}
{"event": {"ts": "2026-04-13T10:00:04+00:00", "seq": 4, "op": "step_start", "step_path": ["summarise"], "stream_path": []}}
{"event": {"ts": "2026-04-13T10:00:05+00:00", "seq": 5, "op": "step_end",   "step_path": ["summarise"], "stream_path": [], "duration_ms": 1200}}
{"event": {"ts": "2026-04-13T10:00:06+00:00", "seq": 6, "op": "workflow_end", "step_path": [], "stream_path": []}}
```

Note that `seq` is globally monotonic across both files (1 → 3 in `.1`, 4 → 6 in the
active file).

---

## See also

- [Redaction](redaction.md) — filtering secrets from event payloads before they are written.
- [Stdout capture](stdout-capture.md) — capturing per-step stdout into the event stream.
- [Concepts](concepts.md) — audit log, workflows, steps.
