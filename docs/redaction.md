# Redaction

> **Godel does not guess at secrets.** Godel ships with no built-in redactors, no
> allow-lists, and no heuristic patterns. Secret classification is your responsibility.
> Godel provides the registration hook; you supply the logic.

Redactors are applied to transcript **events** before they are serialised to disk.
Redaction is a transcript-only feature — the audit log (`runs/<run_id>.jsonl`) is not
redacted.

> **Future compatibility.** The redactor contract is currently string-based
> (`Callable[[str], str | None]`). A future release may introduce a richer dict-based
> contract; if that happens, the string API will either be preserved alongside it or
> deprecated with an explicit migration path. Write redactors against the string
> signature documented below.

---

## Registering redactors

Pass a list of callables to `@workflow`:

```python
from godel import workflow, step

def strip_api_keys(payload: str) -> str:
    """Replace obvious API-key patterns in a serialized event payload."""
    import re
    return re.sub(r"sk-[A-Za-z0-9]{20,}", "sk-***REDACTED***", payload)

@workflow(redact=[strip_api_keys])
async def my_workflow():
    ...
```

### Decoration-time validation

`@workflow` performs these checks at decoration time (see `godel/_decorators.py`):

- Every entry must be **callable**; a non-callable entry raises `TypeError` immediately.
- Every entry must accept **exactly one required positional argument**, or be variadic
  (`*args`). Specifically:
  - **0 required positionals, no `*args`** → rejected (`TypeError`).
  - **1 required positional** → accepted.
  - **0 required positionals + `*args`** → accepted (for generic shim callables).
  - **1 required positional + `*args`** → accepted.
  - **2 or more required positionals** → rejected (`TypeError`).
  - Built-ins and C-implemented callables whose signature cannot be introspected skip
    the arity check (accepted without verification).

These checks fire at decoration time, not at call time, so wrong-shape redactors fail
fast before any workflow runs.

### Signature

```python
Redactor = Callable[[str], "str | None"]
```

A redactor takes a single `str` — the JSON-serialised event body (everything inside the
`{"event": ...}` wrapper, already compact-JSON-encoded) — and returns either:

- a `str` (possibly the same, possibly transformed) → written verbatim as the event
  payload, or
- `None` → **drops the event entirely**; nothing is written to disk and no error
  sentinel is emitted.

### Raising redactor → sentinel event

If a redactor raises `BaseException` (including `KeyboardInterrupt`, `SystemExit`, and
other non-`Exception` subclasses), the failing event is dropped and a minimal sentinel
event is written in its place:

```json
{"event": {"ts": "...", "seq": 12, "op": "redactor.error",
           "step_path": [], "stream_path": [],
           "redactor": "strip_api_keys", "error_class": "KeyError"}}
```

The sentinel intentionally carries **only** the redactor name and the exception class
name — never the exception message and never any part of the original event payload.
This prevents secret-leakage through exception reprs.

Subsequent redactors are **not** run on a failed event; the sentinel is the only output
for that event. Subsequent events continue through the full pipeline normally.

---

## Composition order

Redactors are applied **left-to-right** in registration order. The output of redactor
`i` is the input to redactor `i+1`:

```
raw_payload (str)
  → strip_api_keys(raw_payload)       # redactor 0 → str
  → drop_pii(result_of_0)             # redactor 1 → str
  → written to transcript
```

Ordering matters when redactors interact — e.g. a URL-stripping redactor should run
before one that scans query-string contents for email addresses.

If any redactor returns `None`, the pipeline short-circuits immediately: later redactors
are not called and the event is dropped.

---

## Writing a redactor

- Accept a single `str` positional argument (the serialized event payload).
- Return a `str` (possibly unchanged), or `None` to drop the event.
- Be **pure and fast**: redactors run on the transcript writer's hot path. Avoid I/O,
  network calls, or any blocking operation inside a redactor.
- Do not rely on the exception message surviving. Any raise is swallowed; your
  redactor's name + exception class are all the operator will see.

```python
def redact_bearer(payload: str) -> str:
    """Remove Bearer tokens from Authorization header values."""
    import re
    return re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ***", payload)
```

---

## What redaction does NOT cover

- **The audit log** (`runs/<run_id>.jsonl`). Redaction is a transcript-only feature. If
  your audit log needs redaction, handle it at the storage layer (encrypted volume,
  post-processing scrubber before shipping to a SIEM, etc.).
- **Agent prompts already in flight.** Redactors see only the string representation
  about to be written to disk. They cannot un-send a prompt to an LLM.
- **Stderr and subprocess output from `godel.run()`.** Until stdout-capture lands
  (`godel-py-5pl.7`) these are not in the transcript at all; once they are, they will be
  subject to the same redaction pipeline.

---

## See also

- [Transcript Format](transcript-format.md) — the JSONL wire format that redaction
  protects.
- [Stdout Capture](stdout-capture.md) — per-step stdout capture (status: runtime plumbing
  in flight).
