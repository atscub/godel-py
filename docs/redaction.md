# Redaction

> **Status: option accepted at decoration time; runtime behavior is TODO.**
> In master today, `@workflow(redact=[...])` accepts a list of callables and validates
> them at decoration time (callable + one-positional-arg check). **The runtime pipeline
> that actually applies redactors to transcript events does not exist yet.** It is
> tracked by `godel-py-5pl.6` (redaction infrastructure). This page documents the
> intended contract so users who write redactors today will be compatible when the
> pipeline lands. Until then, redactors are never invoked and no events are filtered.

> **Godel does not guess at secrets.** There will be no built-in redactors, no
> allow-lists, no heuristic patterns. Secret classification is your responsibility. Godel
> provides the registration hook; you supply the logic.

When the pipeline lands (`5pl.6`), redactors are applied to transcript **events** before
they are serialised to disk. Redaction will be a transcript-only feature — the audit log
(`runs/<run_id>.jsonl`) is not redacted.

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

### What is validated today

`@workflow` currently performs these checks at decoration time (see
`godel/_decorators.py`):

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

### Signature validated today

Per `godel/_decorators.py` (the `@workflow` decorator's docstring and arity validator):
**a redactor takes a single `str` and returns a `str`.** It is invoked on event
**payloads** — the serialized string representation of an event's content — not on the
full event dict. The decorator enforces this shape at decoration time via the arity
rules above.

```python
Redactor = Callable[[str], str]  # contract validated today
```

### Intended runtime contract (TODO, tracked in `5pl.6`)

Note: ticket `godel-py-5pl.6` proposes evolving the contract to operate on event dicts
(`Callable[[dict], dict | None]`) with richer return semantics. **That evolution has not
landed and has not yet been reconciled with the string contract the decorator currently
validates.** Until `5pl.6` merges and the decorator validation is updated to match,
write redactors against the string signature above. The semantics below describe the
target behavior once the pipeline lands — they are aspirational, not current.

- **If the contract stays string → string:** a raising redactor substitutes a
  `redactor.error` event containing only the redactor's name and exception class (no
  message, no payload); subsequent redactors continue processing.
- **If `5pl.6` lands the dict evolution:** `None` return drops the event silently;
  raising substitutes a `redactor.error` event with the same name-plus-class payload;
  `BaseException` is caught (not just `Exception`) so `KeyboardInterrupt` / `SystemExit`
  inside a redactor cannot exfiltrate data by bypassing the catch.

Anticipated substituted event shape (either contract):

```json
{"event": {"ts": "...", "seq": 12, "op": "redactor.error",
           "step_path": [], "stream_path": [],
           "redactor": "strip_api_keys", "error_class": "KeyError"}}
```

---

## Composition order (intended)

Redactors will be applied **left-to-right** in registration order. The output of
redactor `i` is the input to redactor `i+1`:

```
raw_payload (str)
  → strip_api_keys(raw_payload)       # redactor 0 → str
  → drop_pii(result_of_0)             # redactor 1 → str
  → written to transcript
```

Ordering matters when redactors interact — e.g. a URL-stripping redactor should run
before one that scans query-string contents for email addresses. The `5pl.6` acceptance
criteria pin this ordering to an explicit test.

---

## Writing a redactor

Against the string contract validated today:

- Accept a single `str` positional argument (the serialized event payload).
- Return a `str` (possibly unchanged).
- Be **pure and fast**: redactors will run on the transcript writer's hot path once
  wiring lands. Avoid I/O, network calls, or any blocking operation inside a redactor.
- Do not rely on the exception message surviving. Any raise will be swallowed once
  `5pl.6` lands, and your redactor's name + exception class are all the operator will
  see.

```python
def redact_bearer(payload: str) -> str:
    """Remove Bearer tokens from Authorization header values."""
    import re
    return re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ***", payload)
```

---

## What redaction will NOT cover

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

- [Transcript Format](transcript-format.md) — the JSONL wire format that redaction will
  protect.
- [Stdout Capture](stdout-capture.md) — per-step stdout capture (status: runtime plumbing
  in flight).
- Ticket `godel-py-5pl.6` — the redaction-infrastructure implementation that will make
  this page live.
