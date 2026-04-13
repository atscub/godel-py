# Stdout Capture

> **Interactive debuggers (`breakpoint()`, `pdb`) will not work inside a captured step.**
> The capture mechanism redirects the process-wide stdout file descriptor into a pipe,
> swallowing any prompt a debugger would normally write to the terminal. Use
> `GODEL_NO_CAPTURE=1` whenever you need to drop into a debugger in code that opts into
> stdout capture.

> **Status: option accepted at decoration time; runtime behavior is TODO.**
> In master today, `@step(capture_stdout=True)` and `@workflow(capture_stdout=True)` are
> accepted as kwargs and stored on the decorator's options dict. **The capture pipe,
> reader thread, and transcript wiring do not exist yet.** They are tracked by
> `godel-py-5pl.7` (stdout capture: pipe-per-step, parallel-safe). One guard is already
> enforced — see [`parallel()` incompatibility](#parallel-incompatibility) — but until
> `5pl.7` lands, setting `capture_stdout=True` on a step outside of `parallel()` has no
> effect.

When the runtime support lands (`5pl.7`), stdout written inside a captured step (or
captured workflow) will be routed through a dedicated pipe, tagged with the step's
`step_path` / `stream_path`, and written as `stdout` events to the transcript.

---

## Enabling capture (intended)

```python
from godel import workflow, step

@step(capture_stdout=True)
async def analyse(text: str) -> str:
    print("running analysis...")   # will be captured once 5pl.7 lands
    result = text.upper()
    print("done")                  # will be captured once 5pl.7 lands
    return result

@workflow
async def my_workflow():
    output = await analyse("hello")
```

Once `5pl.7` lands, each captured line will appear in the transcript as:

```json
{"event": {"ts": "...", "seq": 7, "op": "stdout",
           "step_path": ["analyse"], "stream_path": [],
           "chunk": "running analysis..."}}
```

Subprocess children launched from inside a captured step will inherit the redirected fd
and their stdout will land in the same stream.

---

## Pipe-per-step model (intended design)

Per the `5pl.7` design:

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

> **Note.** The `5pl.7` design refers to this as "registration-time" enforcement in some
> places, meaning at the moment the parallel block registers its branches (i.e. the call
> to `parallel()`). In the implementation that lives in master today, this is literally
> the `parallel()` call site — it happens before any branch is scheduled. Both phrasings
> describe the same check; this page uses "at call time" consistently to avoid
> ambiguity.

**Workaround:** Use `capture_stdout=True` on the enclosing `@workflow` decorator
instead. A workflow-level capture installs a single pipe for the entire run and is safe
across concurrent branches because every branch shares the same redirected fd.

```python
@workflow(capture_stdout=True)   # safe: single pipe for the whole workflow
async def good_workflow():
    await parallel(fetch("https://a.example"), fetch("https://b.example"))
```

---

## `GODEL_NO_CAPTURE` escape hatch (intended)

> **Not available today.** `GODEL_NO_CAPTURE` is not referenced anywhere in `godel/` in
> master — it is part of the `5pl.7` implementation and will only take effect once that
> lands. Setting it today has no effect, because there is no capture pipeline to disable.

Once `5pl.7` lands, setting `GODEL_NO_CAPTURE=1` will disable all stdout capture
globally, regardless of `capture_stdout=True` settings in code:

```bash
GODEL_NO_CAPTURE=1 godel run my_workflow.py
```

The `5pl.7` design specifies that the capture context manager becomes a no-op when this
env var is set — `sys.stdout` is not touched, fd 1 is not swapped, and no `stdout`
events are emitted.

This is useful when:

- Debugging with `breakpoint()` or `pdb` (the primary use case — see the warning at the
  top of this page).
- Running inside a test harness that captures stdout at the process level and would be
  confused by double-capture.
- Diagnosing a hang where the capture pipe itself is suspected.

> `5pl.7` explicitly rejects a `sys.gettrace()` heuristic for auto-disabling under
> debuggers/profilers — it misfires under `coverage` and `pytest-cov`. The env var is
> the only supported escape hatch.

---

## Known limitations (intended)

| Limitation | Detail |
|---|---|
| **Interactive debuggers** | `breakpoint()`, `pdb.set_trace()`, and any tool that writes prompts to stdout while reading from stdin will not display correctly. Use `GODEL_NO_CAPTURE=1`. |
| **`print` / `logging` semantics** | Capture changes the apparent destination of `print()` and anything logging to a handler bound to `sys.stdout`. Handlers bound to `sys.stderr` are unaffected. |
| **Large output** | Captured output is streamed line-by-line through a pipe, not buffered indefinitely — but steps that emit enormous stdout volumes will still pay the serialisation cost on the reader thread. |
| **`parallel()`** | See [above](#parallel-incompatibility). |

---

## See also

- [Transcript Format](transcript-format.md) — where captured stdout events will land in
  the JSONL stream (as `op: "stdout"`, post-`5pl.7`).
- [Redaction](redaction.md) — filtering event payloads including, once wired, captured
  stdout (status: runtime plumbing in flight, `godel-py-5pl.6`).
- Ticket `godel-py-5pl.7` — the stdout-capture implementation that will make this page
  live.
