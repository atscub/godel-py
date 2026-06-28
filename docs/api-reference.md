# API Reference

All symbols are importable directly from `godel` unless noted.

## Decorators

### `@workflow`
Marks an `async def` as the workflow entry point. At most one per file. The decorated
function, when called, sets up a `WorkflowContext`, opens an `EventLog`, and records
`workflow.start` / `workflow.end` events. Positional and keyword arguments passed to the
decorated function are forwarded to the original coroutine and recorded in the
`WORKFLOW_STARTED` event (JSON-serialisable args are stored structurally; other values
fall back to `repr()` and disable programmatic-only `godel resume`). From the CLI, args
are supplied after `--`: `godel run file.py -- arg1 key=value` (see `docs/cli.md`).

### `@step`
Marks an `async def` as a cacheable, replayable checkpoint. On replay, a completed step
returns its recorded result without executing. Arguments must be JSON-serializable
(Pydantic models, dicts, lists, primitives) — they're hashed into `request_hash`.

## Agent factories

### `godel.agents.claude_code(*, model="sonnet", cwd=None, tools=None, skip_permissions=False, system_prompt=None, session_id=None)`
Returns an async callable wrapping the `claude` CLI.

- `model` — `"sonnet"` (default), `"opus"`, `"haiku"`, or a full model ID.
- `cwd` — working directory for the subprocess (default: workflow CWD).
- `tools` — list of tool names to allow (default: claude's defaults).
- `skip_permissions` — pass `--dangerously-skip-permissions`.
- `system_prompt` — briefing text prepended to the first prompt. Not repeated on subsequent calls. Ignored if `session_id` is supplied (assumed already delivered in that session).
- `session_id` — resume a prior CLI session across process boundaries. Passes `--resume <id>` on the first call. Empty/whitespace normalised to `None`.

**Retrieving the session id** for later resumption:

```python
eng = claude_code(system_prompt="You are the engineer for ticket X.")
await eng("implement feature A")
sid = eng.session_id   # persist this string externally

# --- later process ---
eng = claude_code(session_id=sid)
await eng("implement feature B")   # continues same session
```

> **Replay note.** When a `@workflow` replays from its event log the stored session id overwrites any ctor-supplied value — deterministic replay is always preserved.

Call signature:
```python
await agent(prompt: str) -> str
await agent(prompt: str, *, schema: Type[BaseModel]) -> BaseModel
```

Raises `SchemaValidationFailure` if `schema=` is set and the reply cannot be parsed.

> **Schema design guidance.** Keep schemas minimal and flat. Models reliably
> emit JSON for small, single-purpose schemas with primitive fields and short
> string lists. Risks rise sharply with: deep nesting, many sibling models,
> long free-text fields, optional/union fields, or schemas that mix
> control-flow signals with prose. Prefer one schema per decision (a verdict,
> a report, a ref) over one schema that bundles several outputs. If a model
> repeatedly fails to produce valid JSON, simplify the schema before
> reaching for retries.

### `godel.agents.copilot(*, model="default", cwd=None, tools=None, skip_permissions=False, system_prompt=None, session_id=None)`
Returns an async callable wrapping the `copilot` CLI (from the `@github/copilot-cli` npm
package, v0.0.337+).

- `model` — `"default"` (→ `gpt-5`), `"gpt-5"`, `"sonnet"` (→ `claude-sonnet-4.5`),
  `"sonnet-4"` (→ `claude-sonnet-4`), or a full Copilot model ID.
- `cwd`, `tools`, `skip_permissions` — same semantics as `claude_code`.
- `system_prompt`, `session_id` — same semantics as `claude_code`.

Call signature and `SchemaValidationFailure` behavior are identical to `claude_code`, so
the two agents are interchangeable in workflows.

### `godel.agents.codex(...)`
Stub — not yet implemented.

## Primitives

### `run(cmd, *, cwd=None, timeout=None, idempotent=False)`
Audited async subprocess. Returns `CommandResult(returncode, stdout, stderr)`. Raises
`CommandFailure` on non-zero exit. All arguments and output are recorded.

- `cmd` — a shell string (passed to `create_subprocess_shell`) or an argv list
  (passed to `create_subprocess_exec`). Agent factories use the list form to
  avoid shell interpretation of prompts containing metacharacters.
- `idempotent` — when `True`, a `STARTED`-only event on resume is safe to re-execute.

### `parallel(*awaitables, max_concurrency=None) -> tuple`
Awaits coroutines concurrently (variadic args). Emits one `FORK` and one
`JOIN` event bracketing the group.

- `max_concurrency` — optional `int`. When set, at most this many branches
  execute simultaneously (internally uses an `asyncio.Semaphore`). `None`
  (default) means unlimited — all branches run at once.

### `retry(n)(fn)` or `@retry(n)`
Decorator that retries on `WorkflowFail` up to `n` times. Each attempt is recorded; only
the successful attempt contributes to the effective DAG, but `godel show --all` displays
the failures.

### `godel.print(*args, sep=" ", end="\n")`
Async shadow of `print`. Records a `print` event and writes to stdout.

### `godel.input(prompt="") -> str`
Async shadow of `input`. Blocks for human input, writes a `SUSPENDED` → `FINISHED`
`input` event pair. Durable: on resume, returns the recorded answer without re-prompting.

### `godel.read_text(path, *, encoding="utf-8", replay="reread") -> str`
Async audited file read. Resolves `path` to an absolute form (so replay matches are
cwd-independent), reads the file, and emits a `read_text` event. A partial
`STARTED`-only event (crash between open and finish) causes a re-read on resume —
reads are idempotent so this is safe.

The `replay` parameter controls what happens on resume:

- `"reread"` (default) — re-reads the file from disk on resume. Always sees the
  current file state. Safe for all file sizes.
- `"file"` — stores a full snapshot of the content in the run's data directory
  (`<runs_dir>/<run_id>/snapshots/<event_id>.content`). On resume, the snapshot is
  returned without touching the original file — deterministic replay even if the
  source changed. No size truncation.

```python
content = await godel.read_text("data/input.json")
content = await godel.read_text("data/big.jsonl", replay="file")
```

### `godel.write_text(path, content, *, encoding="utf-8") -> None`
Async audited atomic file write. Writes via a sibling temp-file + `os.replace`, so
SIGKILL / OOM / disk-full mid-write never leaves the destination partially written. Emits
a `write_text` event. On replay, the filesystem write is skipped entirely. A
`STARTED`-only event on resume raises `UnsafeResumeError` because a partial write may
have corrupted the target — use `godel rewind` to invalidate and re-execute if needed.
Parent directories are created automatically.

```python
await godel.write_text("output/result.txt", content)
```

## Determinism escape hatches (`godel.det`)

- `godel.det.now() -> datetime` — wall-clock time, recorded.
- `godel.det.uuid4() -> str` — random UUID, recorded.
- `godel.det.random() -> float` — `[0.0, 1.0)`, recorded.
- `godel.det.randint(a, b) -> int` — inclusive, recorded.
- `godel.det.choice(seq)` — recorded.

On replay these return the recorded value.

## Exceptions

| Exception                     | Raised when                                                  |
|-------------------------------|--------------------------------------------------------------|
| `WorkflowFail`                | User-raised failure; triggers retry if wrapped.              |
| `GodelStrictError`            | Strict-mode violation detected before/during execution.      |
| `StrictViolation`             | Individual violation record inside `GodelStrictError`.       |
| `NonDeterministicEscape`      | A banned operation reached the audit hook.                   |
| `SchemaValidationFailure`     | `agent(..., schema=M)` reply failed to parse as `M`.         |
| `AgentRefusal`                | Agent refused the request.                                   |
| `HumanTimeout`                | `input()` timeout exceeded.                                  |
| `PauseSignal`                 | Pause sentinel seen at a `@step` boundary (internal).        |
| `RewindSignal`                | Raised during rewind (internal; exposed for `isinstance`).   |
| `ResumeError`, `UnsafeResumeError`, `SourceEditedError`, `RewindUnsafe` | Replay / rewind safety violations. |

## Event log

### `Event`
Dataclass with fields: `event_id`, `op`, `status`, `step_path`, `ts_start`, `ts_end`,
`request`, `response`, `request_hash`, `parent_id`, `error`, `error_type`.

### `EventStatus`
Enum: `STARTED`, `FINISHED`, `FAILED`, `INVALIDATED`, `SUSPENDED`, `PAUSED`.

### `EventLog`
- `EventLog.load(run_id) -> EventLog` — load from `./runs/<run_id>.jsonl`.
- `.all_events() -> list[Event]`
- `.get_event(event_id) -> Event | None`
- `.emit_started(op, step_path, request) -> Event`
- `.emit_finished(event, response)` / `.emit_failed(event, error)`
- `.close()` — flush and close the file handle.

### `get_event_log() -> EventLog | None`
Returns the current workflow's event log (only non-`None` inside a running workflow).

## Live introspection

### `godel.tail(run_id, *, runs_dir="./runs", follow=True) -> AsyncIterator[Event]`
Async iterator that yields events as they're written. Respects `follow=False` to stop at
EOF. Powers `godel tail`.

### Pause API
- `godel.pause(run_id, *, reason="") -> str` — write sentinel, return full run ID.
- `godel.check_pause_request(run_id) -> PauseRequest | None`
- `godel.clear_pause_request(run_id)` / `godel.write_pause_request(...)`

### `godel.rewind(event_log, event_ids, reason="") -> dict`
Programmatic counterpart to `godel rewind`. Returns `{invalidated_count, invalidated_ids}`.

## Testing utilities (`godel.testing`)

Helpers for mocking `run()` and agents in unit tests. See `godel/testing.py` for the
current surface (stability: experimental).
