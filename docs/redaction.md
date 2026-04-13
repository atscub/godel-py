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

def strip_api_keys(event: dict) -> dict | None:
    """Replace obvious API-key patterns in event payload strings."""
    import re, json
    payload = json.dumps(event)
    payload = re.sub(r"sk-[A-Za-z0-9]{20,}", "sk-***REDACTED***", payload)
    return json.loads(payload)

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

### Intended runtime contract (TODO, tracked in `5pl.6`)

- **Signature:** `Redactor = Callable[[dict], dict | None]`. A redactor receives the
  pending event dict and returns either a (possibly modified) dict or `None`.
- **Return `None`** → event is **dropped silently**. No transcript line is written; no
  error event is emitted. A redactor may use this to censor an event entirely.
- **Redactor raises** → the pending event is replaced with a `redactor.error` event
  containing ONLY the redactor's name and the exception class. The exception **message**
  and the **original event payload** are deliberately omitted so a buggy redactor cannot
  leak the very secret it was supposed to mask.
  ```json
  {"event": {"ts": "...", "seq": 12, "op": "redactor.error",
             "step_path": [], "stream_path": [],
             "redactor": "strip_api_keys", "error_class": "KeyError"}}
  ```
- **`BaseException` is caught**, not just `Exception`. A redactor raising
  `KeyboardInterrupt` or `SystemExit` is contained and substituted with a
  `redactor.error` event; subsequent redactors and events continue processing.

---

## Composition order (intended)

Redactors will be applied **left-to-right** in registration order. The output of
redactor `i` is the input to redactor `i+1`:

```
raw_event
  → strip_api_keys(raw_event)         # redactor 0
  → drop_pii(result_of_0)             # redactor 1
  → written to transcript
```

Ordering matters when redactors interact — e.g. a URL-stripping redactor should run
before one that scans query-string contents for email addresses. The `5pl.6` acceptance
criteria pin this ordering to an explicit test.

---

## Writing a redactor (guidance for 5pl.6)

When writing redactors for use once `5pl.6` lands:

- Accept a single event `dict` argument.
- Return a `dict` (possibly the same one, mutated in place) or `None` to drop the event.
- Be **pure and fast**: redactors will run on the transcript writer's hot path.
  Avoid I/O, network calls, or any blocking operation inside a redactor.
- Do not rely on the exception message surviving. Any raise will be swallowed, and your
  redactor's name + exception class are all the operator will see.

```python
def redact_bearer(event: dict) -> dict:
    """Remove Bearer tokens from Authorization header values inside request payloads."""
    import re, json
    payload = json.dumps(event)
    payload = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ***", payload)
    return json.loads(payload)
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
