# Redaction

> **Godel does not guess at secrets.** There are no built-in redactors, no allow-lists,
> no heuristic patterns. Secret classification is your responsibility. Godel provides the
> registration hook; you supply the logic.

Redactors are applied to event **payloads** (string values inside the `request` and
`response` dicts) before those payloads are written to the transcript. They are **not**
applied to the audit log — redaction is a transcript-only feature.

---

## Registering redactors

Pass a list of callables to `@workflow`:

```python
from godel import workflow, step

def mask_api_key(s: str) -> str:
    import re
    return re.sub(r"sk-[A-Za-z0-9]{20,}", "sk-***REDACTED***", s)

def drop_pii(s: str) -> str:
    import re
    # Replace anything that looks like an email address
    return re.sub(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", "[email redacted]", s)

@workflow(redact=[mask_api_key, drop_pii])
async def my_workflow():
    ...
```

The `redact` list is validated **at decoration time**, not at call time:

- Every entry must be callable.
- Every entry must accept exactly one positional argument (the string to redact).
  Variadic signatures (`*args`) are also accepted.
- A wrong-arity or non-callable entry raises `TypeError` immediately, not on the first
  event write.

---

## Composition order

Redactors are applied **left-to-right** in registration order. The output of redactor `i`
is the input to redactor `i+1`:

```
raw_string
  → mask_api_key(raw_string)          # redactor 0
  → drop_pii(result_of_0)             # redactor 1
  → written to transcript
```

Ordering matters when redactors interact. For example, a redactor that strips URLs should
run before a redactor that scans for email addresses embedded in query strings.

---

## `redactor_error` events

If a redactor callable raises an exception, Godel:

1. Does **not** write the payload that was being redacted (no partial or unredacted data
   reaches the transcript).
2. Emits a `redactor_error` event to the transcript. This event contains **no message and
   no payload** — only the `op`, `ts`, `seq`, `step_path`, and `stream_path` core fields.
   The error detail is intentionally suppressed to avoid leaking the very secret the
   redactor was supposed to protect.
3. Continues execution. A failing redactor does not abort the workflow.

```json
{"event": {"ts": "...", "seq": 12, "op": "redactor_error",
           "step_path": ["fetch"], "stream_path": []}}
```

If you need to diagnose why a redactor failed, instrument the callable itself with
try/except and write to a separate, controlled log.

---

## Writing a redactor

A redactor must:

- Accept a single `str` argument.
- Return a `str`.
- Be **pure and fast**: redactors are called synchronously on the writer's hot path.
  Avoid I/O, network calls, or blocking operations inside a redactor.
- Not raise — or handle its own exceptions internally. If your redactor raises, the event
  payload is silently dropped (see above).

```python
def redact_bearer(s: str) -> str:
    """Remove Bearer tokens from Authorization header values."""
    import re
    return re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ***", s)
```

### Sharing a single redactor across multiple workflows

Because the list is validated per-decoration, you can reuse the same callable freely:

```python
REDACTORS = [mask_api_key, drop_pii, redact_bearer]

@workflow(redact=REDACTORS)
async def workflow_a(): ...

@workflow(redact=REDACTORS)
async def workflow_b(): ...
```

---

## What redaction does NOT cover

- **The audit log** (`runs/<run_id>.jsonl`). Redaction applies to the transcript only.
  If your audit log needs redaction, handle it at the storage layer (e.g. write to
  an encrypted volume, or post-process with a purpose-built log scrubber before
  shipping to a SIEM).
- **Agent prompts and responses already in memory.** Redactors see only the string
  representation that is about to be written. They cannot un-send a prompt to an LLM.
- **Stderr and subprocess output from `godel.run()`.** These are written by the
  subprocess itself; the transcript captures them as opaque blobs after the fact.

---

## See also

- [Transcript Format](transcript-format.md) — the JSONL wire format that redactors protect.
- [Stdout capture](stdout-capture.md) — per-step stdout capture and its interaction with
  the transcript.
- [API Reference](api-reference.md) — `@workflow` signature.
