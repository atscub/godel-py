# Best Practices

For users scaling past a single-file workflow. This guide is **architectural** —
how to structure, compose, and optimize Godel programs. It complements
[engineer](skills/godel-engineer.md) (which is procedural — how to author
`@workflow` / `@step`).

These are **conventions**, not framework-enforced rules. Adopt what fits.

## 1. Project layout

A single file is fine when:

- The workflow is under ~300 lines.
- There is one agent role.
- There's no external configuration worth pinning in a file.

See [`examples/pr_review.py`](../examples/pr_review.py) and
[`examples/feature_factory.py`](../examples/feature_factory.py) — both are
correctly monolithic.

Split the program when any of the above stops holding:

```
my-project/
├── .godel/
│   ├── settings.json                  # harness permissions, runs_dir, redact
│   ├── config/
│   │   └── my_workflow.yaml           # per-workflow config (optional)
│   └── workflows/
│       └── my_workflow/
│           ├── main.py                # @workflow entry
│           ├── settings.py            # pydantic config loader (yaml + env)
│           ├── schemas.py             # shared Pydantic IO models
│           ├── agents.py              # agent factories
│           ├── steps/                 # one @step per file (or tight cluster)
│           │   ├── __init__.py
│           │   ├── ticket.py
│           │   ├── implement.py
│           │   └── quality.py
│           └── scripts/               # deterministic shell helpers
│               └── fetch_threads.sh
```

Rule of thumb: split when any file exceeds ~300 lines, or when the program has
≥ 2 agent roles, or when ops want to tune behavior without a code push.

## 2. Composable blocks — a design pattern

Treat a Godel program as a composition of **named, reusable blocks**. A block
is a coherent sub-procedure that:

- Accepts its agents and config as parameters (never captures them from scope).
- Takes typed inputs and returns a typed output (Pydantic `BaseModel`).
- Has a single, nameable responsibility you could put on a sticky note.
- Holds no hidden global state.

Blocks are how workflows stay readable past the ~300-line mark. They also make
the workflow diagrammable: `brainstorm → plan → code_review_loop → quality_gates → open_pr`.

> Godel does not ship a `patterns` library today. Copy these into your project
> and adapt. If canonical shapes emerge across users, we may later promote them
> to an opt-in `godel.contrib.patterns` module.

### `code_review_loop`

```python
from godel import step, WorkflowFail

class ReviewResult(BaseModel):
    approved: bool
    required_changes: list[str]

@step
async def code_review_loop(engineer, reviewer, artifact_ref: str, max_iters: int = 3) -> str:
    for i in range(max_iters):
        review = await reviewer(
            f"Review {artifact_ref}. Return approved + required_changes.",
            schema=ReviewResult,
        )
        if review.approved:
            return artifact_ref
        await engineer(
            f"Address these review comments on {artifact_ref}: {review.required_changes}",
        )
    raise WorkflowFail(f"code review did not converge in {max_iters} iterations")
```

### `quality_gates`

```python
from godel import step, run, WorkflowFail

@step
async def quality_gates(commands: list[str], cwd: str) -> None:
    for cmd in commands:
        result = await run(cmd, cwd=cwd)
        if result.returncode != 0:
            raise WorkflowFail(f"{cmd!r} failed:\n{result.stderr}")
```

Prefer deterministic `run()` here — lint/test/typecheck have predictable exit
codes and don't need an agent's judgement.

### `human_gate`

For decisions a workflow should pause on:

```python
from godel import step, input

@step
async def human_gate(prompt: str) -> bool:
    reply = await input(f"{prompt} [y/N]: ")
    return reply.strip().lower() == "y"
```

For long waits (minutes-to-days), wire in an out-of-band channel (Slack,
Telegram, email) that posts the question and polls for a reply, rather than
blocking on `input()`. Keeps the event log clean and lets the workflow survive
process restart.

## 3. YAML config (one valid pattern)

Adopt an external YAML when:

- There are ≥ 2 agent roles with distinct models or tool allowlists.
- The workflow integrates with external systems that have stable IDs (Linear
  team, Slack channel, GitHub repo).
- Ops want to tune limits or toggles without a code push.

One shape that works well in practice — treat it as illustrative, not a blessed
schema:

```yaml
agents:
  engineer:
    backend: claude_code
    model: sonnet
    tools: [bash, edit, read]
    skip_permissions: true
  reviewer:
    backend: copilot
    model: default

prompts:
  implement: |
    Implement ticket {ticket_id}. Acceptance criteria: {criteria}.
  review: |
    Review the diff at {pr_url}. Focus on correctness and tests.

limits:
  max_review_iterations: 3
  poll_interval_seconds: 30
  quality_gate_retries: 2

behavior:
  dry_run: false
  post_status_updates: true
```

Load with Pydantic + env overlay for secrets:

```python
from pathlib import Path
import os, yaml
from pydantic import BaseModel

class AgentCfg(BaseModel):
    backend: str
    model: str = "sonnet"
    tools: list[str] = []
    skip_permissions: bool = False

class Settings(BaseModel):
    agents: dict[str, AgentCfg]
    prompts: dict[str, str]
    limits: dict[str, int] = {}
    behavior: dict[str, bool] = {}

def load_settings(path: str | None = None) -> Settings:
    path = path or os.environ.get("MY_WORKFLOW_CONFIG", ".godel/config/my_workflow.yaml")
    data = yaml.safe_load(Path(path).read_text())
    # Secrets are overlaid from env, not committed to YAML.
    return Settings(**data)
```

Interpolate placeholders with `str.format` — deterministic, no template engine
required:

```python
prompt = settings.prompts["implement"].format(ticket_id="BLU-123", criteria="...")
```

## 4. Module decomposition

- **`main.py`** holds only the `@workflow` entry and top-level step calls.
  Nothing else.
- **`agents.py`** exports factories (`make_engineer(cfg)`, `make_reviewer(cfg)`).
  Never construct agents at module scope — module-level state leaks across
  invocations and complicates replay.
- **`schemas.py`** centralizes Pydantic models shared across steps. Step-local
  schemas stay next to the step.
- **`steps/*.py`** — one `@step` per file once a step exceeds ~40 lines or owns
  its own schemas. Tightly-coupled steps can share a file.
- **`scripts/*.sh`** — shell pipelines live in files and are invoked via `await
  run(["./scripts/foo.sh", ...])`. Keeps quoting out of Python and makes the
  shell code version-controllable on its own.

## 5. Deterministic-first

If the output is structurally predictable from the input, use `run()` or plain
Python. Reserve agents for judgement or generation.

| Task                                    | Tool                                        |
|-----------------------------------------|---------------------------------------------|
| Git metadata (sha, branch, user)        | `run(["git", "rev-parse", "HEAD"])`         |
| GitHub REST (read PR info, post comment)| `run(["gh", "api", ...])`                   |
| Fetch typed JSON from a known API       | `run(...)` + `json.loads`                   |
| Classify free-text feedback             | agent with `schema=`                        |
| Generate code                           | agent                                       |
| Summarize / extract from prose          | agent (haiku for short, sonnet for long)    |
| Transform typed data                    | plain Python — no `@step` needed            |

Practical win from a real workflow: a "fetch unresolved review threads" step
that started as an agent call (flaky, ~$0.10 per iteration) moved to a
`gh api` shell script. Removed both cost and flakiness.

## 6. Auto-discovery and sensible defaults

Users should **not** have to configure:

- The current git branch, sha, or repo root — discover via `run(["git", ...])`.
- The default agent — `claude_code(model="sonnet")` is a reasonable baseline.
- The working directory — falls back to the process `cwd`.
- Harness paths like `runs/` — Godel provides them.

Users **must** configure:

- External-system identifiers (Linear team prefix, Slack channel, GitHub repo
  owner).
- Model overrides when cost matters.
- Tool allowlists per agent role.
- Retry budgets and polling intervals.

A typical auto-discovered context block:

```python
class RepoCtx(BaseModel):
    sha: str
    branch: str
    root: str

@step(idempotent=True)
async def repo_ctx() -> RepoCtx:
    sha    = (await run(["git", "rev-parse", "HEAD"])).stdout.strip()
    branch = (await run(["git", "branch", "--show-current"])).stdout.strip()
    root   = (await run(["git", "rev-parse", "--show-toplevel"])).stdout.strip()
    return RepoCtx(sha=sha, branch=branch, root=root)
```

## 7. Optimization and cost

- **Model tiering.** `claude_code(model="haiku")` for classify/triage,
  `"sonnet"` for implementation, `"opus"` for architecture and hard reasoning.
  Picking the right model per call is the single biggest cost lever.
- **`parallel()` for independent branches.** Two agent calls that don't depend
  on each other should run concurrently. See `parallel()` usage in
  [`examples/feature_factory.py`](../examples/feature_factory.py).
- **Idempotent marking.** Three levels of opt-in idempotency let resume
  re-execute a STARTED-only operation instead of raising `UnsafeResumeError`:

  | Level | Syntax | Scope | When to use |
  |-------|--------|-------|-------------|
  | Per-call | `run(cmd, idempotent=True)` | single `run()` call | Pure reads: `git log`, `gh api GET`, read-only scripts |
  | Per-call | `agent(prompt, assume_idempotent=True)` | single agent call | Read-only agent calls: code review, plan critique, risk analysis |
  | Per-step | `@step(idempotent=True)` | all `run()` and `agent()` within the step | Steps that only query state and have no write side-effects |
  | Global | `godel resume --assume-idempotent` | every STARTED-only entry in the run | Emergency recovery when you are certain nothing wrote irreversibly; emits a WARNING |

  The default is always safe: a STARTED-only entry raises `UnsafeResumeError`
  until you explicitly opt in. Prefer the narrowest scope that fits.

- **Trim prompt boilerplate.** Every call hashes the full prompt into
  `request_hash`. Shorter prompts = smaller cache keys = cheaper retries. Don't
  paste the schema into the prompt — `schema=` handles it.
- **Reuse agent sessions where available.** `claude_code` auto-resumes via
  `session_id` across calls to the same instance — cheaper than starting a new
  conversation each time.
- **`source_hash` caveat.** Step source hashing normalizes whitespace. A
  whitespace-only edit won't invalidate the cache (feature, mostly — but keep
  in mind when debugging "why didn't my change take effect on resume").

## 8. Error recovery

Escalation ladder:

1. **`@retry(n)`** — transient failures (rate limits, flaky lints).
2. **`WorkflowFail`** — explicit, user-visible failure. Caught by `@retry`.
3. **`RewindSignal`** — rewind to an earlier checkpoint and re-run forward.
4. **`PauseSignal`** — yield to a human via `godel resume`.
5. **Out-of-band channel** — post to Slack/Telegram/email via a `human_gate`
   block for waits longer than a session.

Fallback-model pattern:

```python
try:
    return await sonnet_agent(prompt, schema=Result)
except WorkflowFail:
    return await opus_agent(prompt, schema=Result)
```

Never catch `Exception` unconditionally in workflow code — it will swallow
`RewindSignal` and `PauseSignal`, which are control-flow, not errors.

## 9. Testing

Three tiers, in order of cost:

1. **Unit with mocked agents.** Use `AsyncMock` to return fixture schema
   instances. Fast, cheap, covers schema shapes and control flow.
2. **Replay-as-test.** Run once against live agents, commit the resulting
   `runs/<run_id>.jsonl` as a golden. Re-run in replay mode; the test asserts
   no live agent calls are made. Catches regressions in step shape without
   re-paying for agent time.
3. **`godel lint` in CI.** Cheap, catches determinism violations statically.

Test helpers: `godel.testing` exposes mocks for `run()` and the built-in
agents.

## 10. Anti-patterns

| Don't                                      | Do                                          |
|--------------------------------------------|---------------------------------------------|
| Direct I/O in a `@step` body               | Wrap in `run()` or use `godel.print/input`  |
| Construct agents at module scope           | Use factories called inside `@workflow`     |
| `except Exception: pass`                   | Catch specific types; never swallow signals |
| Non-deterministic imports (network/file at import time) | Move into a `@step`               |
| Mutable module-level counters              | Return values; pass through args            |
| `time.sleep(...)`                          | Rethink the poll; or `await asyncio.sleep`  |
| Paste schemas into prompts                 | Use `schema=` — the agent handles it        |

## See also

- [engineer](skills/godel-engineer.md) — authoring `@step` / `@workflow`.
- [concepts](concepts.md) — event log, replay, strict mode.
- [api-reference](api-reference.md) — decorator and primitive signatures.
- [monitoring](monitoring.md) — observe a live run without burning context.
- [runner](skills/godel-runner.md) — pause / resume / rewind / repair.
