# Concepts

## Workflows and steps

A **workflow** is an `async` function decorated with `@workflow`. It is the top of the
call tree and the unit of durable execution. Each file passed to `godel run` must contain
exactly one `@workflow` function.

A **step** is an `async` function decorated with `@step`. Steps are the unit of **caching
during replay**: when you `godel resume` a run, each step that completed in the prior run
returns its recorded result without executing. Steps that crashed or never started are
re-executed.

Steps compose freely — you can `await` a step from anywhere, including from inside another
step or from `parallel()` / `retry()` wrappers. Step identity is derived from the call
path (e.g. `pr_review/handle_feedback/quality_gates`) and an argument hash, so the same
step invoked from two places does not collide.

## Agents

An **agent** is any async callable of the form `agent(prompt: str, *, schema=None)`.
Godel ships with two built-in agent factories, both thin closures over the audited
`run()` primitive:

- `godel.agents.claude_code(model=..., skip_permissions=..., tools=...)` — wraps the
  `claude` CLI.
- `godel.agents.copilot(model=..., skip_permissions=..., tools=...)` — wraps the
  `copilot` CLI (from `@github/copilot-cli`).

The two agents are interchangeable — same call signature, same event shape, same
schema-coercion behavior. You can also write your own.

Agents are **values**: they can be passed as parameters, stored in lists, constructed
per-task. This is how Godel avoids "agent config" as a first-class concept — it's all
just Python.

```python
from godel.agents import claude_code

@workflow
async def review():
    engineer = claude_code(model="sonnet", skip_permissions=True)
    reviewer = claude_code(model="opus")
    # engineer and reviewer are independent agent instances with different configs
```

## The audit log

Every workflow run writes an append-only JSONL file at `./runs/<run_id>.jsonl`. One line
per event. Events have the shape:

```json
{"event_id": "...", "op": "agent.call", "status": "FINISHED",
 "step_path": ["review", "handle_feedback"], "ts_start": "...", "ts_end": "...",
 "request": {...}, "response": {...}, "request_hash": "..."}
```

Operations recorded include: `workflow.start`, `step.start`, `agent.call`, `run`
(subprocess), `print`, `input`, `parallel.fork/join`, `retry.attempt`, `pause`,
`rewind`. Status is one of `STARTED`, `FINISHED`, `FAILED`, `INVALIDATED`, `SUSPENDED`.

The audit log is the **source of truth** for replay, rewind, and repair. The in-memory
`EventLog` object builds a DAG over these events and is available inside a workflow via
`godel.get_event_log()`.

## Strict mode

By default, `godel run` enforces **determinism** in user code via three layers:

1. **AST pre-scan** — rejects the file if it imports banned modules (`random`, `time`,
   `datetime`, `os.urandom`, direct `subprocess`, `socket`, etc.) or uses top-level
   non-determinism.
2. **Import guard** — intercepts runtime imports of the same banned modules.
3. **Audit hook** — catches escape attempts at `sys.audit` level.

Non-deterministic operations must go through the **explicit escape hatches**:

| Need                 | Use                               |
|----------------------|-----------------------------------|
| current time         | `godel.det.now()`                 |
| a UUID               | `godel.det.uuid4()`               |
| randomness           | `godel.det.random()`              |
| shell command        | `godel.run("cmd", ...)`           |
| a file read/write    | currently: `godel.run("cat ...")` |

All escapes are recorded in the audit log with their inputs and outputs, so replay can
reproduce them deterministically.

Disable strict mode with `--no-strict` when prototyping — but remember that only strict
runs are safely resumable.

## Replay and resume

`godel resume <run_id>` re-executes the workflow from the beginning, but every recorded
event short-circuits to its cached result. When the replay "walker" runs out of events,
the workflow continues live.

Two mismatch scenarios can occur:

- **`request_hash` mismatch** — the arguments to an operation differ from the recorded
  arguments. Policy: `continue` (trust cache), `invalidate` (re-run this and descendants),
  `abort` (fail). Controlled by `--on-mismatch`.
- **`@step` source edited** — the file containing a cached step was modified since the
  recording. Policy: `warn` (default), `abort`, `ignore`. Controlled by `--on-source-edit`.

## Pause, rewind, repair

- **Pause** — `godel pause <run_id>` drops a sentinel file. The next `@step` boundary
  the live run reaches will raise `PauseSignal` and exit cleanly, ready for resume.
- **Rewind** — `godel rewind <run_id> --to <event_id>` marks an event and its descendants
  `INVALIDATED`. A subsequent resume will re-execute from that point. Rewinds are refused
  if they would invalidate a non-reversible operation (e.g. a completed `run` that
  modified the filesystem and has no compensating inverse).
- **Repair** — `godel repair <run_id>` launches an intervention agent against a `PAUSED`
  or `FAILED` run. The agent can inspect events, propose fixes, and request resume. Ship
  your own via `--agent module:function`.

## Parallel and retry

```python
from godel import parallel, retry, step

@step
@retry(3)
async def flaky():
    ...

@step
async def fan_out():
    results = await parallel([task(i) for i in range(5)])
```

Both are regular Python helpers that emit audit events and integrate with replay. Retries
record every failed attempt; `godel show --all` shows them grouped under the successful
event.

## I/O

Godel provides async shadows of the builtins that record to the audit log:

```python
from godel import print, input   # shadow the builtins

await print("visible to user AND recorded")
name = await input("your name: ")  # blocks, durable across resume
```

Using the raw builtins in a `@workflow` context is a lint error (`PL004`), because their
output is invisible to replay.

### Scripting checkpoint answers

`godel.input()` reads `sys.stdin.readline()`.  That means checkpoints can be
driven from any standard UNIX mechanism without changing your workflow code:

```bash
# Pipe a single canned answer
echo "yes" | godel run review.py

# Supply multiple answers from a file (one per input() call, in order)
godel run review.py < answers.txt

# Feed answers from a FIFO so another process controls timing
mkfifo /tmp/ctl
godel run review.py < /tmp/ctl &
echo "approve" > /tmp/ctl
```

When running non-interactively, declare your intent with `--auto-checkpoint`
or the `GODEL_AUTO_CHECKPOINT` env var:

```bash
# Using the flag (value is recorded in the audit log)
godel run review.py --auto-checkpoint=pipe < answers.txt

# Using the env var (equivalent)
GODEL_AUTO_CHECKPOINT=pipe godel run review.py < answers.txt
```

**Why declare intent?**  Detection is **lazy**: when the first live (non-
replayed) `godel.input()` call is about to read stdin and finds it is not a
TTY — and `GODEL_AUTO_CHECKPOINT` is not set — Godel emits a one-shot
warning:

```
[godel] warning: godel.input() called but stdin is not a TTY.
To script checkpoint answers, pipe answers or set
GODEL_AUTO_CHECKPOINT=<mode> (e.g. pipe, file, fifo) to suppress this warning.
```

Workflows that never call `godel.input()` — or whose `input()` calls are all
satisfied from the replay cache on resume — never trigger the check.

Setting `GODEL_AUTO_CHECKPOINT` suppresses the warning and records the value
in each `input` event's `request.auto_checkpoint` field, making the audit log
self-documenting about how answers were supplied.  The `auto_checkpoint`
value is **excluded from the replay `request_hash`** — it's execution-context
metadata, not part of the workflow's logical request identity.

**Replay is unaffected.**  A run scripted with piped stdin can still be
resumed normally — the recorded answers are replayed from the audit log, so
the new run does not need stdin to be re-piped.  You can even change or drop
the `--auto-checkpoint` mode on the resume command line; the cached answers
still match.

## Where next

- [Getting Started](getting-started.md) if you haven't run your first workflow.
- [Best Practices](best-practices.md) for structuring non-trivial programs —
  project layout, composable blocks, YAML config, deterministic-first.
- [engineer](skills/godel-engineer.md) for authoring workflows step-by-step.
