# Stdout Capture

> **Interactive debuggers (`breakpoint()`, `pdb`) will not work inside a captured step.**
> The capture mechanism redirects the process-wide stdout file descriptor into a pipe,
> swallowing any prompt a debugger would normally write to the terminal. Use
> `GODEL_NO_CAPTURE=1` whenever you need to drop into a debugger in code that opts into
> stdout capture.

Stdout written inside a captured step (or captured workflow) is routed through a
dedicated pipe, tagged with the step's `step_path` / `stream_path`, and written as
`stdout` events to the transcript.

---

## Enabling capture

```python
from godel import workflow, step

@step(capture_stdout=True)
async def analyse(text: str) -> str:
    print("running analysis...")   # captured → transcript
    result = text.upper()
    print("done")                  # captured → transcript
    return result

@workflow
async def my_workflow():
    output = await analyse("hello")
```

Each captured line appears in the transcript as:

```json
{"event": {"ts": "...", "seq": 7, "op": "stdout",
           "step_path": ["analyse"], "stream_path": [],
           "chunk": "running analysis..."}}
```

Subprocess children launched from inside a captured step inherit the redirected fd
and their stdout lands in the same stream.

---

## Pipe-per-step model

- At step entry, a fresh `os.pipe()` is opened. The write end replaces file descriptor 1
  (`os.dup2(w, 1)`); the saved original fd is kept for restoration.
- A daemon reader thread consumes the pipe line-by-line, emitting `stdout` transcript
  events tagged with the step's `step_path` and `stream_path`.
- At step exit (success or failure), fd 1 is restored via `os.dup2(saved, 1)` and the
  reader thread is joined (with a 1-second timeout).

This is a **process-wide fd-level** redirect, not a `sys.stdout` override — which is why
subprocess children inherit the redirect, and why it is incompatible with concurrency
(see below).

Steps without `capture_stdout=True` are unaffected.

---

## `parallel()` incompatibility

`capture_stdout=True` on a step passed to `parallel()` raises `ConfigError` **at call
time** — when `parallel()` is invoked, before any branch coroutine runs. The check lives
in `godel/_decorators.py::parallel()` and inspects the `_step_options` attached to each
`_StepCoroutine` wrapper:

```python
@step(capture_stdout=True)
async def fetch(url: str): ...

@workflow
async def bad_workflow():
    # ConfigError is raised here, at the parallel() call site,
    # before either fetch() coroutine is awaited.
    await parallel(fetch("https://a.example"), fetch("https://b.example"))
```

Error message:

```
capture_stdout=True is not allowed inside parallel() —
each branch would require its own pipe.
Use capture_stdout on the enclosing @workflow instead.
```

**Why?** File descriptor 1 is process-global. Two concurrent captures racing to
`dup2(w, 1)` would interleave their redirects — writes from one branch would land in
another branch's pipe. There is no safe interleaving.

> **Note.** This check fires at the `parallel()` call site — it happens before any
> branch is scheduled. Both "registration-time" and "at call time" phrasings describe
> the same check; this page uses "at call time" consistently.

**Workaround:** Use `capture_stdout=True` on the enclosing `@workflow` decorator
instead. A workflow-level capture installs a single pipe for the entire run and is safe
across concurrent branches because every branch shares the same redirected fd.

```python
@workflow(capture_stdout=True)   # safe: single pipe for the whole workflow
async def good_workflow():
    await parallel(fetch("https://a.example"), fetch("https://b.example"))
```

---

## `GODEL_NO_CAPTURE` escape hatch

Setting `GODEL_NO_CAPTURE=1` disables all stdout capture globally, regardless of
`capture_stdout=True` settings in code:

```bash
GODEL_NO_CAPTURE=1 godel run my_workflow.py
```

When this env var is set, the capture context manager becomes a no-op — `sys.stdout` is
not touched, fd 1 is not swapped, and no `stdout` events are emitted.

This is useful when:

- Debugging with `breakpoint()` or `pdb` (the primary use case — see the warning at the
  top of this page).
- Running inside a test harness that captures stdout at the process level and would be
  confused by double-capture.
- Diagnosing a hang where the capture pipe itself is suspected.

> `GODEL_NO_CAPTURE` is the only supported escape hatch. A `sys.gettrace()` heuristic
> for auto-disabling under debuggers/profilers was explicitly rejected — it misfires
> under `coverage` and `pytest-cov`. Set the env var explicitly.

---

## Known limitations

| Limitation | Detail |
|---|---|
| **Interactive debuggers** | `breakpoint()`, `pdb.set_trace()`, and any tool that writes prompts to stdout while reading from stdin will not display correctly. Use `GODEL_NO_CAPTURE=1`. |
| **`print` / `logging` semantics** | Capture changes the apparent destination of `print()` and anything logging to a handler bound to `sys.stdout`. Handlers bound to `sys.stderr` are unaffected. |
| **Large output** | Captured output is streamed line-by-line through a pipe, not buffered indefinitely — but steps that emit enormous stdout volumes will still pay the serialisation cost on the reader thread. |
| **`parallel()`** | See [above](#parallel-incompatibility). |
| **pytest stdout capture** | pytest replaces `sys.stdout` at the Python object level; the fd-level redirect captures subprocess output but `print()` calls go through pytest's capture instead. Use `capsys.disabled()` or `os.write(1, ...)` in tests that exercise capture. |

---

## See also

- [Transcript Format](transcript-format.md) — where captured stdout events land in the
  JSONL stream (as `op: "stdout"`).
- [Redaction](redaction.md) — filtering event payloads including captured stdout.
