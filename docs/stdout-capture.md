# Stdout Capture

> **Interactive debuggers (`breakpoint()`, `pdb`) do not work inside a step that has
> `capture_stdout=True`.** The capture pipe intercepts everything written to `sys.stdout`,
> including debugger prompts. If you need to debug a captured step, set `GODEL_NO_CAPTURE=1`
> or remove `capture_stdout=True` temporarily.

Stdout capture attaches a per-step pipe to `sys.stdout` while the step body executes. When
the step finishes, the captured output is attached to the step's transcript event as an
additional field.

---

## Enabling capture per step

```python
from godel import workflow, step

@step(capture_stdout=True)
async def analyse(text: str) -> str:
    print("running analysis...")   # captured
    result = text.upper()
    print("done")                  # captured
    return result

@workflow
async def my_workflow():
    output = await analyse("hello")
```

When `capture_stdout=True` is set, every `print()` call (and any direct write to
`sys.stdout`) inside the step body is intercepted and buffered. The buffer is flushed and
attached to the `step_end` event as `stdout`:

```json
{"event": {"ts": "...", "seq": 4, "op": "step_end",
           "step_path": ["analyse"], "stream_path": [],
           "stdout": "running analysis...\ndone\n"}}
```

Output is **not** echoed to the terminal while the step runs. If you want to see progress
in real time and capture it, write to `sys.stderr` instead — `stderr` is not captured.

---

## Pipe-per-step model

Each step with `capture_stdout=True` gets its own pipe. The pipe is:

1. Installed at `step` entry by replacing `sys.stdout` with a `StringIO` (or equivalent)
   buffer.
2. Torn down at `step` exit (success or failure) and the captured text is attached to the
   event payload.
3. Restored to the caller's `sys.stdout` after teardown.

Steps without `capture_stdout=True` are unaffected — they write to whatever `sys.stdout`
their caller holds at the time.

---

## Incompatibility with `parallel()`

`capture_stdout=True` on a `@step` raises `ConfigError` **at call time** (before any
coroutine runs) if that step is passed to `parallel()`:

```python
@step(capture_stdout=True)
async def fetch(url: str): ...

@workflow
async def bad_workflow():
    # ConfigError is raised here — before either coroutine is awaited
    await parallel(fetch("https://a.example"), fetch("https://b.example"))
```

Error message:

```
ConfigError: capture_stdout=True is not allowed inside parallel() —
each branch would require its own pipe.
Use capture_stdout on the enclosing @workflow instead.
```

**Why?** `parallel()` runs branches concurrently in a shared event loop. If two branches
both replace `sys.stdout` with their own buffer, writes from one branch land in the other
branch's buffer. There is no safe interleaving.

**Workaround:** Use `capture_stdout=True` on the enclosing `@workflow` decorator instead.
Workflow-level capture applies a single pipe for the entire run and is safe across
concurrent branches because all branches share the same `sys.stdout` scope.

```python
@workflow(capture_stdout=True)   # safe: single pipe for the whole workflow
async def good_workflow():
    await parallel(fetch("https://a.example"), fetch("https://b.example"))
```

---

## `GODEL_NO_CAPTURE` escape hatch

Set the environment variable `GODEL_NO_CAPTURE=1` to disable all stdout capture globally,
regardless of `capture_stdout=True` settings in code:

```bash
GODEL_NO_CAPTURE=1 godel run my_workflow.py
```

This is useful when:

- Debugging with `breakpoint()` or `pdb` (the primary use case — see the warning at the
  top of this page).
- Running inside a test harness that captures stdout at the process level and would be
  confused by double-capture.
- Diagnosing a hang where the capture pipe itself is suspected.

When `GODEL_NO_CAPTURE=1` is set, `stdout` fields are omitted from transcript events.

---

## Known limitations

| Limitation | Detail |
|---|---|
| **Interactive debuggers** | `breakpoint()`, `pdb.set_trace()`, and any tool that reads from stdin while prompting on stdout will hang or produce garbled output. Use `GODEL_NO_CAPTURE=1`. |
| **C-extension writes** | Output written directly to the underlying file descriptor (bypassing `sys.stdout`) is not captured. This includes some C-extension libraries and `os.write(1, ...)` calls. |
| **Large output** | Captured output is held in memory until step exit. Steps that emit large volumes of stdout (e.g. streaming model output) should not use `capture_stdout=True`. |
| **Nested steps** | If a captured step `await`s another captured step, the inner step's capture pipe nests inside the outer one. The outer event's `stdout` field will contain only the outer step's own writes; the inner step's writes appear in the inner event. |

---

## See also

- [Transcript Format](transcript-format.md) — where captured stdout lands in the JSONL stream.
- [Redaction](redaction.md) — filtering secrets from event payloads (including captured stdout).
- [API Reference](api-reference.md) — `@step` and `@workflow` signatures.
