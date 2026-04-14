---
name: godel-engineer
description: Write and modify Godel workflow files. Use when the user asks to author a new .py workflow with @workflow/@step, add an agent call, design a Pydantic schema for agent output, introduce retries or parallelism, or refactor a workflow for better durability.
---

# godel-engineer

You write Python files that orchestrate AI agents via the Godel library. Your output
must satisfy `godel lint` and run correctly under strict mode.

## The golden rules

1. **Exactly one `@workflow` per file.** It's the entry point; `godel run` discovers it
   by decorator.
2. **All workflow code is `async`.** `@workflow`, `@step`, agents, `run`, `print`,
   `input` — everything you `await`.
3. **Non-determinism only through Godel primitives.** No `time.time()`, `uuid.uuid4()`,
   `random.random()`, `open()`, `subprocess.*`, `requests.*`, etc. in workflow code.
   Use `godel.det.*` for clocks/UUIDs/randomness and `godel.run(...)` for shell.
4. **Shadow the builtins.** Import `from godel import print, input` so output is
   recorded. Raw `print`/`input` trigger lint warning `PL004`.
5. **Agents are values, not globals.** Construct them inside the workflow (`engineer =
   claude_code(...)`) and pass them to steps as parameters.

## Skeleton

```python
from pydantic import BaseModel
from godel import workflow, step, retry, WorkflowFail
from godel import print, input
from godel.agents import claude_code


class Result(BaseModel):
    ok: bool
    summary: str


@step
@retry(3)
async def do_thing(agent, inputs: dict) -> Result:
    result = await agent(f"Do the thing with {inputs}. Return JSON.", schema=Result)
    if not result.ok:
        raise WorkflowFail(result.summary)
    return result


@workflow
async def main():
    agent = claude_code(model="sonnet")
    result = await do_thing(agent, {"key": "value"})
    await print(result.summary)
```

## When to use `@step`

Use `@step` when:

- The body makes **expensive agent calls** you don't want to repeat on resume.
- The body has **side effects** (pushing a branch, opening a PR) that should be
  bracketed by a single durable event.
- You want a **retry boundary** — combine with `@retry(n)`.

Don't wrap trivial helpers (string formatting, dict munging) in `@step`. The overhead
of caching argument hashes is wasted on deterministic code.

## Designing agent prompts

- **Write the prompt about the task, not the format.** When you pass `schema=Model`,
  the agent definition handles schema-shaping for you — you do not need to say *"reply
  with JSON"* or paste the schema into the prompt. Focus the prompt on *what to do*.
- **Pass structured inputs as text.** `f"Comments: {comments!r}"` is fine — the whole
  prompt is hashed into the event's `request_hash`.
- **Choose the model for cost/quality.** `claude_code(model="opus")` for hard reasoning,
  `"sonnet"` for implementation, `"haiku"` for simple classification. This is the one
  knob worth tuning per call — everything else the agent handles.
- **Pick the agent backend.** `claude_code` and `copilot` (from
  `godel.agents`) are interchangeable — same call signature, same schema behavior. Use
  `copilot(model="default")` if you prefer the GitHub Copilot CLI backend.

## Schemas

Use Pydantic `BaseModel` subclasses for every structured agent reply:

```python
class Feedback(BaseModel):
    fixes: list[str]
    has_unclear: bool
    comment_ids: list[int]

feedback = await engineer(prompt, schema=Feedback)
```

Keep fields **flat and typed**. Nested optional fields confuse the agent and make
validation errors harder to diagnose.

## Failure handling

- `raise WorkflowFail("message")` — user-visible failure. `@retry(n)` catches it.
- Let other exceptions propagate — they crash the run, but Godel records a `FAILED`
  event so `godel repair` / `godel resume` can recover.
- Always **clean up in `finally`** for externally-visible side effects (open PRs,
  temp files). The cleanup block runs both on success and on crash.

## Pitfalls

| Pitfall                                                     | Fix                                             |
|-------------------------------------------------------------|-------------------------------------------------|
| Using `time.sleep(...)`                                     | `await asyncio.sleep(...)` — but better, rethink; delays undermine determinism. |
| Polling an external system via agent calls                  | Use `godel.run("gh api ...")` — it's 100× cheaper and deterministic. |
| `await print(x)` inside f-string replay loops spamming logs | Print once per step; details go in event responses. |
| Top-level side effects in the module                        | Move them inside `@workflow`; the module is re-imported on resume. |
| `@step` on a function that takes non-serializable args      | Pass serializable data; pass agents explicitly as params. |

## Before you hand off

Run these locally before telling the user you're done:

```bash
godel lint path/to/workflow.py       # must pass
godel run  path/to/workflow.py       # optionally; costs real $$$ with live agents
```

If you're adding tests, put them under `py-library/tests/` and use the helpers in
`godel.testing` to mock `run()` and agents.

## References

- [API Reference](../api-reference.md) — every exported symbol.
- [Concepts](../concepts.md) — audit log, strict mode, replay semantics.
- [`examples/pr_review.py`](../../examples/pr_review.py) — canonical workflow.
