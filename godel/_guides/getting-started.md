# Getting Started

## Install

Godel is not yet on PyPI. Install from source:

```bash
git clone https://github.com/atscub/godel-py.git
cd godel-py
pip install -e .
```

Requires Python **3.10+**. For the live TUI renderer used by `godel watch` /
`godel run --watch`, install the extra: `pip install -e '.[watch]'`.

For the bundled `claude_code` agent you also need the [`claude` CLI](https://docs.claude.com/en/docs/claude-code)
installed and authenticated (via a claude.ai subscription or `ANTHROPIC_API_KEY`).

Alternatively, use the bundled `copilot` agent — it wraps the `copilot` CLI from the
[`@github/copilot-cli`](https://www.npmjs.com/package/@github/copilot-cli) npm package
(v0.0.337+). The two agents are interchangeable.

## Your first workflow

Create `hello.py`:

```python
from godel import workflow, print
from godel.agents import claude_code

@workflow
async def hello():
    agent = claude_code(model="sonnet")
    greeting = await agent("Say hello in three languages, one per line.")
    await print(greeting)
```

Run it:

```bash
godel run hello.py
```

You'll see something like:

```
[godel] run 01JQ5Z...
[godel] audit log: runs/01JQ5Z....jsonl
Hello
Bonjour
Hola
[godel] completed in 2.3s
```

## What just happened

1. **`@workflow`** marked `hello` as the entry point. `godel run` discovers it automatically.
2. **`claude_code(...)`** returned a callable that wraps the `claude` CLI.
3. **`await agent(...)`** executed the agent through Godel's audited `run()` primitive.
4. Every event (agent call, stdout write) was appended to `runs/<run_id>.jsonl`.
5. **`godel.print`** shadows the builtin so output is both shown to the user and recorded.

## Add a step

Wrap reusable sub-procedures in `@step` to make them durable checkpoints:

```python
from godel import workflow, step, print
from godel.agents import claude_code

@step
async def translate(agent, phrase: str, lang: str) -> str:
    return await agent(f"Translate {phrase!r} into {lang}. Reply with just the translation.")

@workflow
async def greetings():
    agent = claude_code(model="sonnet")
    for lang in ("French", "Spanish", "Japanese"):
        await print(await translate(agent, "Good morning", lang))
```

If this workflow crashes on the Japanese translation, `godel resume <run_id>` will
re-use the cached French and Spanish results and only re-run the failing step.

## Typed outputs

Pass a Pydantic model as `schema=` to have the agent's reply parsed and validated:

```python
from pydantic import BaseModel
from godel import workflow
from godel.agents import claude_code

class Summary(BaseModel):
    title: str
    bullets: list[str]

@workflow
async def summarize_readme():
    agent = claude_code(model="sonnet")
    result = await agent("Summarize README.md", schema=Summary)
    for b in result.bullets:
        print("-", b)
```

If the agent replies with invalid JSON, Godel raises `SchemaValidationFailure`.

## Next steps

- Read [Concepts](concepts.md) to understand the audit log, replay, and strict mode.
- Browse the [API Reference](api-reference.md).
- Study the real-world [PR-review example](../examples/pr_review.py).
- Ready to scale past a single file? [Best Practices](best-practices.md) covers
  project layout, composable blocks, YAML config, and deterministic-first design.
